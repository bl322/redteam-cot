from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import gradio as gr
import pandas as pd

from .analyze import build_report, render_report
from .core import AgentConfig
from .dataset import DatasetLoader
from .graph import build_graph
from .visualize import (
    GREEN,
    RED,
    _card,
    _esc,
    _wrap,
    render_batch_progress,
    render_chain,
    render_pipeline,
    render_progress,
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


def _normalize_base_url(url: str) -> Optional[str]:
    """容错处理 Base URL：缺协议自动补 https://，百炼类网关自动补兼容路径。"""
    value = (url or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = "https://" + value.lstrip("/")
    return value.rstrip("/")


def render_pipeline_card(attack_label: str) -> str:
    """架构 Pipeline 卡片：兼容带中文说明的下拉框标签。"""
    return render_pipeline(_parse_attack(attack_label))


def _invoke_with_thread(app, state: Dict[str, Any], thread_id: str):
    return app.invoke(state, config={"configurable": {"thread_id": thread_id}})


# ---------------------------------------------------------------------------
# 取消机制
#
# Gradio 的 cancels= 只中断前端，后端生成器仍阻塞在真实模型的 HTTP 调用上，
# 会一直占着队列槽位，导致下一次点击长时间排队（观感即"卡死"）。
# 这里用一个全局事件：点「取消」后在下一个节点边界立即退出，快速释放槽位。
# ---------------------------------------------------------------------------
_CANCEL_EVENT = threading.Event()


def request_cancel() -> tuple:
    """用户点击取消：置位，运行循环在下一个节点边界退出，并立即恢复 Run 按钮。"""
    _CANCEL_EVENT.set()
    return "已请求取消，将在当前环节结束后立即停止（无需等超时），随后可再次运行。", _button_idle()


def _button_running(label: str = "▶ Run"):
    """运行期间禁用按钮：Gradio 同一会话的事件串行调度，重复点击只会排队等待。"""
    return gr.Button(label, interactive=False, variant="primary")


def _button_idle(label: str = "▶ Run"):
    return gr.Button(label, interactive=True, variant="primary")


def _cancelled_html(elapsed: float) -> str:
    return _partial_outputs(
        _wrap(
            _card(
                "已取消",
                f'<div style="font-size:13px;line-height:1.7;color:#0f172a;">'
                f"已在节点边界停止，耗时 {elapsed:.1f}s。</div>",
                RED,
            )
        )
    )[0]


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


def _render_result(result: Dict[str, Any], config: AgentConfig, max_rounds: int) -> tuple:
    """把一次完整执行结果渲染为全部可视化组件。"""
    summary = _summarize(result)
    history = result.get("history", [])
    df = pd.DataFrame(history)

    candidate = result.get("prompt_candidate", {}) or {}
    steps = candidate.get("cot_steps") or result.get("cot_steps") or []
    labels = candidate.get("strategy_labels") or {}
    scores = candidate.get("scores") or {}

    cards_html = render_summary_cards(summary)
    report_html = render_report(build_report(result, attack=config.attack, max_rounds=max_rounds))
    chain_html = render_chain(steps, labels)
    score_html = render_score_bars(scores)
    trend_html = render_trend(_scores_by_round(result))
    timeline_html = render_timeline(history, attack=config.attack)
    resources_html = render_resources()
    raw_json = json.dumps(result, ensure_ascii=False, indent=2)
    return (
        cards_html,
        report_html,
        render_pipeline(config.attack),
        chain_html,
        score_html,
        trend_html,
        timeline_html,
        df,
        raw_json,
        resources_html,
    )


def run_single(
    seed_text: str,
    attack_label: str,
    model_name: str,
    base_url: str,
    api_key: str,
    population_size: int,
    max_rounds: int,
    max_tokens: int,
    use_mock: bool,
):
    """流式执行单样本评测：每完成一个节点即产出一次进度，避免界面长时间无反馈。"""
    import time

    started = time.time()
    _CANCEL_EVENT.clear()
    try:
        config = AgentConfig(
            model_name=model_name,
            base_url=_normalize_base_url(base_url),
            api_key=api_key or None,
            use_mock=use_mock,
            attack=_parse_attack(attack_label),
            population_size=population_size,
            max_rounds=max_rounds,
            max_tokens=int(max_tokens),
        )
    except Exception as exc:  # 配置错误（如不支持的策略名）
        yield _error_outputs(f"{type(exc).__name__}: {exc}")
        return

    app = build_graph(config)
    merged: Dict[str, Any] = {
        "seed_text": seed_text,
        "current_text": seed_text,
        "round_index": 0,
        "max_rounds": max_rounds,
        "trace": [],
        "history": [],
    }
    # 首次 yield：立即给出进度条，用户点击后马上有反馈
    yield _partial_outputs(render_progress(0, max_rounds, "准备中", "", 0.0))

    try:
        for update in app.stream(
            merged,
            config={"configurable": {"thread_id": f"single-{int(started * 1000)}"}},
            stream_mode="updates",
        ):
            if not isinstance(update, dict):
                continue
            for node, payload in update.items():
                if _CANCEL_EVENT.is_set():
                    yield _partial_outputs(_cancelled_html(time.time() - started))
                    return
                if isinstance(payload, dict):
                    merged.update(payload)
                round_index = int(merged.get("round_index", 0))
                last_status = ""
                if merged.get("history"):
                    last_status = str(merged["history"][-1].get("judge", {}).get("status", ""))
                yield _partial_outputs(render_progress(
                    round_index, max_rounds, node, last_status, time.time() - started
                ))
    except Exception as exc:
        yield _error_outputs(f"{type(exc).__name__}: {exc}")
        return

    yield _render_result(merged, config, max_rounds)


def _error_outputs(message: str) -> tuple:
    """执行异常时返回统一的错误提示组合。"""
    html = _wrap(
        _card(
            "执行失败",
            f'<div style="font-size:13px;line-height:1.7;color:#0f172a;white-space:pre-wrap;">{_esc(message)}</div>'
            '<div style="font-size:12px;color:#475569;margin-top:8px;">'
            "Base URL 只填域名即可，系统会自动探测 /compatible-mode/v1 等兼容路径。"
            "若仍失败，请核对模型名是否存在（报错信息会列出该端点可用模型）与 API Key 是否有效。",
            RED,
        )
    )
    return _partial_outputs(html)


# 占位的空 DataFrame：gr.Dataframe 的 postprocess 无法处理空字符串（pd.read_csv("") 会抛
# FileNotFoundError），因此中间态/错误态的 Dataframe 输出必须给空表而非 ""。
_EMPTY_DF = pd.DataFrame()


def _partial_outputs(progress_html: str) -> tuple:
    """中间态（进度条/错误卡片）：第一个组件更新，其余组件保持占位（Dataframe 用空表）。"""
    return (
        progress_html,
        "",
        "",
        "",
        "",
        "",
        "",
        _EMPTY_DF.copy(),
        "",
        "",
    )


def _batch_display_rows(records: Sequence[Dict[str, Any]], limit: int = 80) -> List[Dict[str, Any]]:
    """表格展示用：截断超长字段（数据集原始 goal 可达上千字），避免前端渲染卡顿。

    写盘仍是完整内容，此处只影响界面。
    """
    rows: List[Dict[str, Any]] = []
    for record in records:
        row: Dict[str, Any] = {}
        for key, value in record.items():
            if isinstance(value, str) and len(value) > limit:
                row[key] = value[:limit] + "…"
            else:
                row[key] = value
        rows.append(row)
    return rows


def _batch_meta_json(
    total: int,
    refusals: int,
    avg_rounds: Sequence[float],
    by_column: Dict[str, Dict[str, float]],
    by_primary_domain: Dict[str, Dict[str, float]],
    output_path: Path,
    summary_path: Path,
    checkpoint_path: Path,
) -> str:
    summary = {
        "num_samples": total,
        "num_refusals": refusals,
        "refusal_trigger_rate": round(refusals / total if total else 0.0, 4),
        "avg_rounds": round(sum(avg_rounds) / total, 4) if total else 0.0,
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
    return json.dumps(summary, ensure_ascii=False, indent=2)


def run_batch(
    csv_file: str,
    limit: int,
    attack_label: str,
    model_name: str,
    base_url: str,
    api_key: str,
    population_size: int,
    max_rounds: int,
    max_tokens: int,
    use_mock: bool,
    resume: bool = True,
):
    """批量评测：每处理完一条样本即 yield 一次，避免长时间黑屏无反馈。"""
    _CANCEL_EVENT.clear()
    dataset_path = Path(csv_file)
    rows = DatasetLoader(dataset_path).load_records(limit=limit)
    config = AgentConfig(
        model_name=model_name,
        base_url=_normalize_base_url(base_url),
        api_key=api_key or None,
        use_mock=use_mock,
        attack=_parse_attack(attack_label),
        population_size=population_size,
        max_rounds=max_rounds,
        max_tokens=int(max_tokens),
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
    pending = [row for row in rows if str(row["id"]) not in processed_ids]
    started_at = time.time()
    done_count = 0

    def _eta() -> Optional[float]:
        """按已完成样本的平均耗时估算剩余时间。"""
        if done_count <= 0:
            return None
        per_item = (time.time() - started_at) / done_count
        return per_item * (len(pending) - done_count)

    def _progress(index: int, node: str = "", status: str = "") -> str:
        return render_batch_progress(
            index, len(pending), node, time.time() - started_at, _eta(), status
        )

    yield (
        pd.DataFrame(_batch_display_rows(records)),
        _batch_meta_json(
            total, refusals, avg_rounds, by_column, by_primary_domain,
            output_path, summary_path, checkpoint_path,
        ),
        _progress(1, "准备中"),
    )

    with output_path.open(write_mode, encoding="utf-8") as sink:
        for index, row in enumerate(pending, start=1):
            if _CANCEL_EVENT.is_set():
                yield (
                    pd.DataFrame(_batch_display_rows(records + [{
                        "id": "-",
                        "status": f"已取消（{index - 1}/{len(pending)} 条已完成）",
                    }])),
                    _batch_meta_json(
                        total, refusals, avg_rounds, by_column, by_primary_domain,
                        output_path, summary_path, checkpoint_path,
                    ),
                    _cancelled_html(time.time() - started_at),
                )
                break
            row_id = str(row["id"])
            yield (
                pd.DataFrame(_batch_display_rows(records + [{
                    "id": row_id,
                    "status": f"processing {index}/{len(pending)}",
                }])),
                _batch_meta_json(
                    total, refusals, avg_rounds, by_column, by_primary_domain,
                    output_path, summary_path, checkpoint_path,
                ),
                _progress(index, "请求目标模型"),
            )

            result: Optional[Dict[str, Any]] = None
            error_text: Optional[str] = None
            last_status = ""
            delay = 1.0
            for attempt in range(1, 6):
                try:
                    state: Dict[str, Any] = {
                        "seed_text": row["goal"],
                        "current_text": row["goal"],
                        "round_index": 0,
                        "max_rounds": max_rounds,
                        "trace": [],
                        "history": [],
                    }
                    # 逐节点流式推进，界面能实时看到"第几条 · 当前节点"
                    for update in app.stream(
                        state,
                        config={"configurable": {"thread_id": f"batch-{row_id}"}},
                        stream_mode="updates",
                    ):
                        if _CANCEL_EVENT.is_set():
                            yield (
                                pd.DataFrame(_batch_display_rows(records)),
                                _batch_meta_json(
                                    total, refusals, avg_rounds, by_column, by_primary_domain,
                                    output_path, summary_path, checkpoint_path,
                                ),
                                _cancelled_html(time.time() - started_at),
                            )
                            return
                        if not isinstance(update, dict):
                            continue
                        for node, payload in update.items():
                            if isinstance(payload, dict):
                                state.update(payload)
                            if state.get("history"):
                                last_status = str(state["history"][-1].get("judge", {}).get("status", ""))
                            yield (
                                pd.DataFrame(_batch_display_rows(records + [{
                                    "id": row_id,
                                    "status": f"processing {index}/{len(pending)}",
                                }])),
                                _batch_meta_json(
                                    total, refusals, avg_rounds, by_column, by_primary_domain,
                                    output_path, summary_path, checkpoint_path,
                                ),
                                _progress(index, node, last_status),
                            )
                    result = state
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

            done_count += 1
            yield (
                pd.DataFrame(_batch_display_rows(records)),
                _batch_meta_json(
                    total, refusals, avg_rounds, by_column, by_primary_domain,
                    output_path, summary_path, checkpoint_path,
                ),
                _progress(index + 1, "已完成该条", last_status),
            )

    summary_json = _batch_meta_json(
        total, refusals, avg_rounds, by_column, by_primary_domain,
        output_path, summary_path, checkpoint_path,
    )
    summary_path.write_text(
        json.dumps(json.loads(summary_json), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    done_html = _wrap(
        _card(
            f"批量完成 · 共 {total} 条",
            f'<div style="font-size:13px;color:#0f172a;">本次处理 {len(pending)} 条，'
            f'总耗时 {time.time() - started_at:.1f}s，拒答 {refusals} 条。</div>'
            f'<div style="font-size:12px;color:#475569;margin-top:6px;">结果文件：{_esc(str(output_path))}</div>',
            GREEN if refusals else ACCENT,
        )
    )
    yield pd.DataFrame(_batch_display_rows(records)), summary_json, done_html


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
                        base_url = gr.Textbox(
                            label="Base URL",
                            value="",
                            placeholder="ws-m0wca....maas.aliyuncs.com（只填域名即可）",
                            info="只填域名即可：缺协议自动补 https://，兼容路径（如百炼的 /compatible-mode/v1）会自动探测",
                        )
                    with gr.Row():
                        api_key = gr.Textbox(label="API Key", type="password", value="")
                        use_mock = gr.Checkbox(label="Mock Mode (离线演示)", value=True)
                    with gr.Row():
                        population_size = gr.Slider(2, 16, value=8, step=1, label="Population Size")
                        max_rounds = gr.Slider(1, 10, value=2, step=1, label="Max Rounds")
                    with gr.Row():
                        max_tokens = gr.Slider(
                            256, 4096, value=1024, step=128,
                            label="Max Tokens（真实模型输出较长，过小会截断）",
                        )
                    with gr.Row():
                        run_btn = gr.Button("▶ Run CoT Attack", variant="primary")
                        cancel_btn = gr.Button("■ 取消", variant="stop")

            gr.Markdown("### 📈 评测结果")
            summary_cards = gr.HTML(label="Summary")
            report_html = gr.HTML(label="Report")
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

            run_event = (
                run_btn.click(lambda: _button_running("⏳ 运行中…"), outputs=[run_btn])
                .then(
                    run_single,
                    inputs=[seed, attack_single, model_name, base_url, api_key, population_size, max_rounds, max_tokens, use_mock],
                    outputs=[
                        summary_cards,
                        report_html,
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
                .then(lambda: _button_idle("▶ Run CoT Attack"), outputs=[run_btn])
            )
            cancel_note = gr.Markdown("")
            cancel_btn.click(request_cancel, outputs=[cancel_note, run_btn], cancels=[run_event])

        with gr.Tab("📊 批量评测"):
            dataset = gr.Textbox(label="Dataset Path (CSV/JSONL)", value=str(DEFAULT_DATASET))
            with gr.Row():
                limit = gr.Slider(1, 200, value=3, step=1, label="Limit")
                attack_batch = gr.Dropdown(choices=ATTACK_CHOICES, value=ATTACK_CHOICES[0], label="Attack Strategy")
                resume = gr.Checkbox(label="Resume Existing Output", value=True)
            with gr.Row():
                batch_run = gr.Button("▶ Run Batch", variant="primary")
                batch_cancel_btn = gr.Button("■ 取消", variant="stop")
            gr.Markdown(
                "<span style='font-size:12px;color:#475569;'>批量任务逐条实时落盘并刷新表格；"
                "真实模型下建议先设 Limit=3 试跑，确认单条耗时后再放大。</span>"
            )
            batch_progress = gr.HTML()
            batch_df = gr.Dataframe(label="Batch Results")
            batch_meta = gr.Code(label="Batch Meta", language="json")
            batch_event = (
                batch_run.click(lambda: _button_running("⏳ 批量运行中…"), outputs=[batch_run])
                .then(
                    run_batch,
                    inputs=[dataset, limit, attack_batch, model_name, base_url, api_key, population_size, max_rounds, max_tokens, use_mock, resume],
                    outputs=[batch_df, batch_meta, batch_progress],
                )
                .then(lambda: _button_idle("▶ Run Batch"), outputs=[batch_run])
            )
            batch_cancel_note = gr.Markdown("")
            batch_cancel_btn.click(request_cancel, outputs=[batch_cancel_note, batch_run], cancels=[batch_event])

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


def _build_theme():
    """使用系统本地字体，避免从 Google Fonts 加载外部样式。

    Gradio 默认主题会注入 https://fonts.googleapis.com/css2?... 到
    config.stylesheets，而前端是 **await 该样式表加载完成后才渲染页面**
    （见 Index-*.js 的 ze() 逻辑）。国内网络访问 fonts.googleapis.com
    往往长时间挂起，表现为页面一直转圈 / 点击无响应（观感即"卡死"）。
    改用系统字体后不再有外部请求。
    """
    try:
        return gr.themes.Soft(
            font=["Microsoft YaHei", "PingFang SC", "Hiragino Sans GB", "system-ui", "sans-serif"],
            font_mono=["Cascadia Mono", "Consolas", "Menlo", "monospace"],
        )
    except Exception:  # 主题构造失败时退回默认主题，不影响启动
        return None


def _resolve_port(preferred: int = 7860) -> int:
    """返回 preferred 起第一个可用的端口，避免旧实例未退出时启动失败。"""
    import socket

    for offset in range(0, 20):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise OSError(f"在 {preferred}-{preferred + 19} 范围内找不到可用端口，请先关闭占用的进程。")


def main() -> None:
    preferred = int(os.getenv("GRADIO_SERVER_PORT", "7860"))
    port = _resolve_port(preferred)
    if port != preferred:
        print(f"[redteam] 端口 {preferred} 已被占用，自动改用 {port}")
    print(f"[redteam] 服务地址: http://127.0.0.1:{port}")
    demo = build_demo()
    demo.queue(default_concurrency_limit=8, max_size=32).launch(
        css=CUSTOM_CSS,
        theme=_build_theme(),
        server_name="127.0.0.1",
        server_port=port,
        show_error=True,
        inbrowser=False,
        # 页面不依赖任何外部 CDN / 字体，纯离线可渲染
        head=None,
    )


if __name__ == "__main__":
    main()
