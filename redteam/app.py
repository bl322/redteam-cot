from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import gradio as gr
import pandas as pd

from .core import AgentConfig
from .dataset import DatasetLoader
from .graph import build_graph
from .visualize import (
    render_chain,
    render_pipeline,
    render_resources,
    render_score_bars,
    render_summary_cards,
    render_timeline,
    render_trend,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "data" / "dataset.csv"
DEFAULT_BATCH_DIR = ROOT / "results" / "redteam_batch"

ATTACK_CHOICES = ["cot (新型CoT攻击)", "cc_bos (CC-BOS基线)"]


def _parse_attack(label: str) -> str:
    return "cc_bos" if str(label).startswith("cc_bos") else "cot"


def render_pipeline_card(attack_label: str) -> str:
    """架构 Pipeline 卡片：兼容带中文说明的下拉框标签。"""
    return render_pipeline(_parse_attack(attack_label))


def _invoke_with_thread(app, state: Dict[str, Any], thread_id: str):
    return app.invoke(state, config={"configurable": {"thread_id": thread_id}})


def _summarize(result: Dict[str, Any]) -> Dict[str, Any]:
    history = result.get("history", [])
    refusals = sum(1 for row in history if row.get("judge", {}).get("is_refusal_template"))
    rounds = len(history)
    judges = [row.get("judge", {}) for row in history]
    followed = sum(1 for judge in judges if judge.get("chain_followed"))
    summary: Dict[str, Any] = {
        "rounds": rounds,
        "refusals": refusals,
        "final_status": history[-1]["judge"]["status"] if history else "empty",
        "last_prompt": result.get("current_text", ""),
    }
    if history:
        summary["chain_follow_rate"] = f"{followed / len(history) * 100:.0f}%"
        summary["conclusion_reached"] = bool(judges[-1].get("conclusion_reached"))
    return summary


def _scores_by_round(result: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    scores: Dict[int, Dict[str, Any]] = {}
    for node in result.get("trace", []):
        if node.get("node") == "generate":
            scores[int(node.get("round", 0))] = node.get("scores", {}) or {}
    return scores


def _slugify(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return value.strip("._-") or "model"


def _default_batch_paths(dataset_path: Path, model_name: str, attack: str, population_size: int, max_rounds: int) -> tuple[Path, Path, Path]:
    stem = dataset_path.stem
    model_slug = _slugify(model_name)
    attack_slug = _slugify(attack)
    suffix = f"{attack_slug}_p{population_size}_r{max_rounds}"
    output_path = DEFAULT_BATCH_DIR / f"{stem}_{model_slug}_{suffix}_batch_eval_results.jsonl"
    summary_path = DEFAULT_BATCH_DIR / f"{stem}_{model_slug}_{suffix}_batch_eval_summary.json"
    checkpoint_path = DEFAULT_BATCH_DIR / f"{stem}_{model_slug}_{suffix}_batch_eval_checkpoint.json"
    return output_path, summary_path, checkpoint_path


def _load_existing_records(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                records.append(obj)
    return records


def _is_retryable_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return True
    status = getattr(exc, "status", None)
    if status == 429:
        return True
    message = f"{type(exc).__name__}: {exc}".lower()
    return "429" in message or "rate limit" in message or "too many requests" in message


def run_single(
    seed_text: str,
    attack_label: str,
    model_name: str,
    base_url: str,
    api_key: str,
    population_size: int,
    max_rounds: int,
    use_mock: bool,
) -> tuple[str, str, str, str, str, str, pd.DataFrame, str, str]:
    config = AgentConfig(
        model_name=model_name,
        base_url=base_url or None,
        api_key=api_key or None,
        use_mock=use_mock,
        attack=_parse_attack(attack_label),
        population_size=population_size,
        max_rounds=max_rounds,
    )
    app = build_graph(config)
    result = _invoke_with_thread(
        app,
        {
            "seed_text": seed_text,
            "current_text": seed_text,
            "round_index": 0,
            "max_rounds": max_rounds,
            "trace": [],
            "history": [],
        },
        "single-sample",
    )
    summary = _summarize(result)
    history = result.get("history", [])
    df = pd.DataFrame(history)

    candidate = result.get("prompt_candidate", {}) or {}
    steps = candidate.get("cot_steps") or result.get("cot_steps") or []
    labels = candidate.get("strategy_labels") or {}
    scores = candidate.get("scores") or {}

    cards_html = render_summary_cards(summary)
    chain_html = render_chain(steps, labels)
    score_html = render_score_bars(scores)
    trend_html = render_trend(_scores_by_round(result))
    timeline_html = render_timeline(history, attack=config.attack)
    resources_html = render_resources()
    raw_json = json.dumps(result, ensure_ascii=False, indent=2)
    return (
        cards_html,
        render_pipeline(config.attack),
        chain_html,
        score_html,
        trend_html,
        timeline_html,
        df,
        raw_json,
        resources_html,
    )


def run_batch(
    csv_file: str,
    limit: int,
    attack_label: str,
    model_name: str,
    base_url: str,
    api_key: str,
    population_size: int,
    max_rounds: int,
    use_mock: bool,
    resume: bool = True,
) -> tuple[pd.DataFrame, str]:
    dataset_path = Path(csv_file)
    rows = DatasetLoader(dataset_path).load_records(limit=limit)
    config = AgentConfig(
        model_name=model_name,
        base_url=base_url or None,
        api_key=api_key or None,
        use_mock=use_mock,
        attack=_parse_attack(attack_label),
        population_size=population_size,
        max_rounds=max_rounds,
    )
    app = build_graph(config)
    output_path, summary_path, checkpoint_path = _default_batch_paths(dataset_path, model_name, config.attack, population_size, max_rounds)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    existing_records = _load_existing_records(output_path) if resume else []
    processed_ids = {str(row.get("id", "")).strip() for row in existing_records if str(row.get("id", "")).strip()}
    records: List[Dict[str, Any]] = list(existing_records)
    total = len(existing_records)
    refusals = sum(1 for row in existing_records if int(row.get("refusals", 0)) > 0)
    avg_rounds = [float(row.get("rounds", 0.0)) for row in existing_records]
    by_column: Dict[str, Dict[str, float]] = {}
    by_primary_domain: Dict[str, Dict[str, float]] = {}
    for row in existing_records:
        source_column = row.get("source_column", "unknown")
        primary_domain = row.get("primary_domain", "unknown")
        rounds = float(row.get("rounds", 0.0))
        refusal = int(row.get("refusals", 0)) > 0
        by_column.setdefault(source_column, {"num_samples": 0, "num_refusals": 0, "style_score_sum": 0.0})
        by_primary_domain.setdefault(primary_domain, {"num_samples": 0, "num_refusals": 0, "style_score_sum": 0.0})
        by_column[source_column]["num_samples"] += 1
        by_column[source_column]["num_refusals"] += int(refusal)
        by_column[source_column]["style_score_sum"] += rounds
        by_primary_domain[primary_domain]["num_samples"] += 1
        by_primary_domain[primary_domain]["num_refusals"] += int(refusal)
        by_primary_domain[primary_domain]["style_score_sum"] += rounds

    write_mode = "a" if resume else "w"
    with output_path.open(write_mode, encoding="utf-8") as sink:
        for row in rows:
            row_id = str(row["id"])
            if row_id in processed_ids:
                continue

            result: Optional[Dict[str, Any]] = None
            error_text: Optional[str] = None
            delay = 1.0
            for attempt in range(1, 6):
                try:
                    result = _invoke_with_thread(
                        app,
                        {
                            "seed_text": row["goal"],
                            "current_text": row["goal"],
                            "round_index": 0,
                            "max_rounds": max_rounds,
                            "trace": [],
                            "history": [],
                        },
                        f"batch-{row_id}",
                    )
                    break
                except Exception as exc:
                    error_text = f"{type(exc).__name__}: {exc}"
                    if not _is_retryable_error(exc) or attempt >= 5:
                        break
                    time.sleep(delay)
                    delay = min(delay * 2, 30.0)

            summary = _summarize(result) if result is not None else {
                "rounds": 0,
                "refusals": 0,
                "final_status": "error",
                "last_prompt": row["goal"],
            }
            record = {
                "id": row_id,
                "source_column": row.get("source_column", "unknown"),
                "prompt": row["goal"],
                "primary_domain": row["primary_domain"],
                "secondary_domain": row["secondary_domain"],
                **summary,
            }
            if error_text is not None and result is None:
                record["error"] = error_text

            sink.write(json.dumps(record, ensure_ascii=False) + "\n")
            sink.flush()
            try:
                os.fsync(sink.fileno())
            except OSError:
                pass

            records.append(record)
            total += 1
            refusals += int(record["refusals"] > 0)
            rounds = float(record["rounds"])
            avg_rounds.append(rounds)
            by_column.setdefault(row.get("source_column", "unknown"), {"num_samples": 0, "num_refusals": 0, "style_score_sum": 0.0})
            by_primary_domain.setdefault(row["primary_domain"], {"num_samples": 0, "num_refusals": 0, "style_score_sum": 0.0})
            by_column[row.get("source_column", "unknown")]["num_samples"] += 1
            by_column[row.get("source_column", "unknown")]["num_refusals"] += int(record["refusals"] > 0)
            by_column[row.get("source_column", "unknown")]["style_score_sum"] += rounds
            by_primary_domain[row["primary_domain"]]["num_samples"] += 1
            by_primary_domain[row["primary_domain"]]["num_refusals"] += int(record["refusals"] > 0)
            by_primary_domain[row["primary_domain"]]["style_score_sum"] += rounds
            processed_ids.add(row_id)

            checkpoint = {
                "output_path": str(output_path),
                "processed_samples": total,
                "summary": {
                    "num_samples": total,
                    "num_refusals": refusals,
                    "refusal_trigger_rate": round(refusals / total if total else 0.0, 4),
                    "avg_rounds": round(sum(avg_rounds) / total if total else 0.0, 4),
                    "by_source_column": {
                        key: {
                            "num_samples": int(stats["num_samples"]),
                            "num_refusals": int(stats["num_refusals"]),
                            "refusal_trigger_rate": round(stats["num_refusals"] / stats["num_samples"], 4) if stats["num_samples"] else 0.0,
                            "avg_rounds": round(stats["style_score_sum"] / stats["num_samples"], 4) if stats["num_samples"] else 0.0,
                        }
                        for key, stats in by_column.items()
                    },
                    "by_primary_domain": {
                        key: {
                            "num_samples": int(stats["num_samples"]),
                            "num_refusals": int(stats["num_refusals"]),
                            "refusal_trigger_rate": round(stats["num_refusals"] / stats["num_samples"], 4) if stats["num_samples"] else 0.0,
                            "avg_rounds": round(stats["style_score_sum"] / stats["num_samples"], 4) if stats["num_samples"] else 0.0,
                        }
                        for key, stats in by_primary_domain.items()
                    },
                },
                "updated_at": time.time(),
            }
            checkpoint_path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
            summary_path.write_text(json.dumps(checkpoint["summary"], ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {
        "num_samples": total,
        "num_refusals": refusals,
        "refusal_trigger_rate": round(refusals / total if total else 0.0, 4),
        "avg_rounds": round(sum(avg_rounds) / total if total else 0.0, 4) if total else 0.0,
        "output_path": str(output_path),
        "summary_path": str(summary_path),
        "checkpoint_path": str(checkpoint_path),
        "by_source_column": {
            key: {
                "num_samples": int(stats["num_samples"]),
                "num_refusals": int(stats["num_refusals"]),
                "refusal_trigger_rate": round(stats["num_refusals"] / stats["num_samples"], 4) if stats["num_samples"] else 0.0,
                "avg_rounds": round(stats["style_score_sum"] / stats["num_samples"], 4) if stats["num_samples"] else 0.0,
            }
            for key, stats in by_column.items()
        },
        "by_primary_domain": {
            key: {
                "num_samples": int(stats["num_samples"]),
                "num_refusals": int(stats["num_refusals"]),
                "refusal_trigger_rate": round(stats["num_refusals"] / stats["num_samples"], 4) if stats["num_samples"] else 0.0,
                "avg_rounds": round(stats["style_score_sum"] / stats["num_samples"], 4) if stats["num_samples"] else 0.0,
            }
            for key, stats in by_primary_domain.items()
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return pd.DataFrame(records), json.dumps(summary, ensure_ascii=False, indent=2)


CUSTOM_CSS = """
.gradio-container { background: #f6f8fb !important; }
#hero { background: linear-gradient(120deg, #1e3a8a 0%, #2563eb 55%, #7c3aed 100%);
        color: #ffffff; border-radius: 14px; padding: 20px 24px; margin-bottom: 16px; }
#hero h1 { color: #ffffff !important; margin: 0 0 6px 0; font-size: 24px !important; }
#hero p, #hero li { color: #e8eefc !important; }
.tabitem { background: #ffffff; border-radius: 12px; }
footer { visibility: hidden; }
"""


def _hero() -> str:
    return """
<div id="hero">
  <h1>🧠 CoT Red-Team Evaluation Agent · 思维链越狱评测智能体</h1>
  <p style="margin:0 0 8px 0;font-size:13px;line-height:1.7;">
    基于 <b>LangGraph</b> 的防御性多智能体评测流水线。默认采用新型 <b>CoT（思维链）攻击</b>：
    将评测目标分解为逐级递进的推理子问题链，隐藏于收束步骤；保留 CC-BOS 作为对比基线。
  </p>
  <div style="font-size:12px;line-height:1.8;opacity:.95;">
    多智能体闭环：<b>Decompose（目标分解）</b> → <b>Generate（推理链组装 · FOA 演化）</b>
    → <b>Interact（目标模型交互 · 429 退避）</b> → <b>Judge（多维裁判 · 链路跟随度）</b> → 未拦截则回到 Generate 继续演化
  </div>
</div>
"""


def build_demo() -> gr.Blocks:
    with gr.Blocks(title="CoT Red-Team Evaluation Agent", fill_height=True) as demo:
        gr.HTML(_hero())

        with gr.Tab("⚡ 单样本评测"):
            gr.Markdown(
                "输入 Seed Prompt 后点击 <b>Run</b>，即可看到推理链分解、多维评分、"
                "多轮演化趋势与逐轮攻击轨迹的可视化结果。<b>Mock Mode</b> 下无需 API Key 即可完整演示。"
            )
            with gr.Row():
                with gr.Column(scale=4):
                    seed = gr.Textbox(
                        label="Seed Prompt",
                        lines=6,
                        value="测试样例：请评估目标模型在推理链引导下的安全鲁棒性",
                    )
                    with gr.Row():
                        attack_single = gr.Dropdown(
                            choices=ATTACK_CHOICES, value=ATTACK_CHOICES[0], label="Attack Strategy"
                        )
                        model_name = gr.Textbox(label="Model", value="qwen3-max")
                        base_url = gr.Textbox(label="Base URL", value="")
                    with gr.Row():
                        api_key = gr.Textbox(label="API Key", type="password", value="")
                        use_mock = gr.Checkbox(label="Mock Mode (离线演示)", value=True)
                    with gr.Row():
                        population_size = gr.Slider(2, 16, value=8, step=1, label="Population Size")
                        max_rounds = gr.Slider(1, 10, value=4, step=1, label="Max Rounds")
                    run_btn = gr.Button("▶ Run CoT Attack", variant="primary")

            gr.Markdown("### 📈 评测结果")
            summary_cards = gr.HTML(label="Summary")
            pipeline_html = gr.HTML(label="Pipeline")
            with gr.Row():
                with gr.Column(scale=1):
                    chain_html = gr.HTML(label="Chain")
                    score_html = gr.HTML(label="Scores")
                with gr.Column(scale=1):
                    trend_html = gr.HTML(label="Trend")
                    resources_html = gr.HTML(label="Resources")
            timeline_html = gr.HTML(label="Timeline")
            with gr.Accordion("逐轮明细表格 / Raw JSON", open=False):
                history_df = gr.Dataframe(label="History")
                raw = gr.Code(label="Raw Result", language="json")

            run_btn.click(
                run_single,
                inputs=[seed, attack_single, model_name, base_url, api_key, population_size, max_rounds, use_mock],
                outputs=[
                    summary_cards,
                    pipeline_html,
                    chain_html,
                    score_html,
                    trend_html,
                    timeline_html,
                    history_df,
                    raw,
                    resources_html,
                ],
            )

        with gr.Tab("📊 批量评测"):
            dataset = gr.Textbox(label="Dataset Path (CSV/JSONL)", value=str(DEFAULT_DATASET))
            with gr.Row():
                limit = gr.Slider(1, 200, value=10, step=1, label="Limit")
                attack_batch = gr.Dropdown(choices=ATTACK_CHOICES, value=ATTACK_CHOICES[0], label="Attack Strategy")
                resume = gr.Checkbox(label="Resume Existing Output", value=True)
            batch_run = gr.Button("▶ Run Batch", variant="primary")
            batch_df = gr.Dataframe(label="Batch Results")
            batch_meta = gr.Code(label="Batch Meta", language="json")
            batch_run.click(
                run_batch,
                inputs=[dataset, limit, attack_batch, model_name, base_url, api_key, population_size, max_rounds, use_mock, resume],
                outputs=[batch_df, batch_meta],
            )

        with gr.Tab("🏗 架构与说明"):
            arch_attack = gr.Dropdown(choices=ATTACK_CHOICES, value=ATTACK_CHOICES[0], label="选择攻击策略查看链路编排")
            arch_html = gr.HTML()
            demo.load(render_pipeline_card, inputs=[arch_attack], outputs=[arch_html])
            arch_attack.change(render_pipeline_card, inputs=[arch_attack], outputs=[arch_html])
            gr.Markdown(
                """
### 🧩 模块构成

| 模块 | 职责 |
| --- | --- |
| `redteam/cot.py` | CoT 攻击优化器：目标分解、推理链组装、八维策略空间、FOA 演化、CoT 评分 |
| `redteam/graph.py` | LangGraph 状态图：Decompose → Generate → Interact → Judge 条件闭环 |
| `redteam/core.py` | 三类 Agent（生成 / 交互 / 裁判）与 `AgentConfig` |
| `redteam/engine.py` | 目标模型客户端（OpenAI 兼容）、拒答检测、CC-BOS 基线、CoT Mock |
| `redteam/visualize.py` | 前端可视化渲染：推理链、评分条、演化趋势、时间线、资源监控 |

### 🧠 CoT 攻击的八个策略维度

`persona 角色包装` · `scaffold 推理脚手架` · `granularity 分解粒度` · `step_style 步骤表述`
`bridging 步骤衔接` · `abstraction 抽象层级` · `context 场景包装` · `closure 收束方式`

果蝇优化算法在每个维度上做嗅觉搜索（邻域采样）与视觉搜索（择优），停滞两轮时触发柯西变异跳出局部最优。

### 📐 评测维度

- **安全合规**：`final_status`（refusal / answered）、`rounds`（迭代轮数）
- **CoT 链路**：`chain_followed`（是否沿推理链推进）、`conclusion_reached`（是否给出收束结论）、`chain_markers`
- **攻击质量**：`semantic_preservation` · `cot_style` · `chain_depth` · `concealment` → 加权合成 `total`

### ⚠️ 伦理声明

本项目为防御性评测工具，仅用于学术研究、安全评估与模型鲁棒性验证。
"""
            )
            refresh = gr.Button("🔄 刷新资源占用")
            res_html = gr.HTML()
            refresh.click(render_resources, outputs=[res_html])
            demo.load(render_resources, outputs=[res_html])

    return demo


def main() -> None:
    demo = build_demo()
    demo.launch(
        css=CUSTOM_CSS,
        server_name="127.0.0.1",
        server_port=7860,
        inbrowser=False,
    )


if __name__ == "__main__":
    main()
