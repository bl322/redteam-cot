"""命令行批量评测（长时间运行用）。

浏览器页面跑 100 条（约 1.5-2 小时）容易因会话超时断连；本脚本在终端直接跑，
结果逐条落盘、支持 Ctrl+C 中断后原样续跑（resume 默认开启）。

API Key 通过环境变量传入，避免出现在命令行历史或日志里：

    # Windows PowerShell
    $env:LLM_BASE_URL = "ws-m0wca268n7l2xi1w.cn-beijing.maas.aliyuncs.com"
    $env:LLM_API_KEY  = "sk-xxxxxx"
    python scripts/run_batch_cli.py --limit 100 --attack cot

    # Git Bash / Linux
    export LLM_BASE_URL=... LLM_API_KEY=...
    python scripts/run_batch_cli.py --limit 100 --attack cot

也可加 --mock 离线试跑（不需要 Key）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from redteam.app import ATTACK_CHOICES, run_batch  # noqa: E402

DEFAULT_DATASET = Path(__file__).resolve().parents[1] / "data" / "dataset.csv"


THINKING_MODEL_HINTS = ("deepseek-r1", "deepseek-reasoner", "o1", "o3", "qwq", "thinking")


def _needs_thinking_budget(model: str) -> bool:
    """推理型模型会把 token 先消耗在思维链上，需要更大的 max_tokens 预算。"""
    lowered = (model or "").lower()
    return any(hint in lowered for hint in THINKING_MODEL_HINTS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量红队评测 CLI（支持断点续跑）")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET, help=f"数据集 CSV（默认 {DEFAULT_DATASET}）")
    parser.add_argument("--limit", type=int, default=100, help="本批评测样本条数（默认 100）")
    parser.add_argument("--offset", type=int, default=0, help="从第几条之后开始取样本（分批跑：第 2 组用 --offset 10）")
    parser.add_argument("--attack", default="cot", choices=["cot", "cc_bos", "flip"], help="攻击策略（默认 cot）")
    parser.add_argument(
        "--flip-mode",
        default="FCS",
        choices=["FWO", "FCW", "FCS", "FMM"],
        help="FlipAttack 翻转模式（默认 FCS 整句字符翻转，中文语料推荐 FCS/FCW）",
    )
    parser.add_argument("--flip-no-cot", action="store_true", help="FlipAttack 关闭 CoT 变体")
    parser.add_argument("--flip-no-lang-gpt", action="store_true", help="FlipAttack 关闭 LangGPT 角色化规则")
    parser.add_argument("--flip-no-few-shot", action="store_true", help="FlipAttack 关闭 Few-shot 演示")
    parser.add_argument(
        "--response-chars",
        type=int,
        default=200,
        help="落盘的末轮响应长度上限（默认 200；0 = 不截断，做 Judge 口径评分时必须用 0）",
    )
    parser.add_argument("--model", default="qwen3-max", help="目标模型名")
    parser.add_argument("--population", type=int, default=8, help="FOA 种群规模")
    parser.add_argument("--rounds", type=int, default=3, help="最大演化轮数")
    parser.add_argument("--max-tokens", type=int, default=1024, help="单次生成最大 token")
    parser.add_argument("--mock", action="store_true", help="离线 Mock 模式（不联网、不需要 Key）")
    parser.add_argument("--no-resume", action="store_true", help="忽略已有结果，从头重跑")
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="只重跑上次失败的样本（额度耗尽/403/超时的记录会被丢弃并重跑，成功的保留）",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    base_url = os.getenv("LLM_BASE_URL", "")
    api_key = os.getenv("LLM_API_KEY", "")
    if not args.mock and not api_key:
        print("错误：在线模式需要设置环境变量 LLM_API_KEY（离线试跑请加 --mock）", file=sys.stderr)
        return 2

    label = next((c for c in ATTACK_CHOICES if c.startswith(args.attack)), ATTACK_CHOICES[0])
    started = time.time()
    last_meta: dict = {}
    last_status = ""

    extras = ""
    if args.attack == "flip":
        extras = (
            f"flip-mode={args.flip_mode} cot={not args.flip_no_cot} "
            f"lang_gpt={not args.flip_no_lang_gpt} few_shot={not args.flip_no_few_shot} "
        )
    print(
        f"开始批量评测：attack={args.attack} model={args.model} limit={args.limit} "
        f"population={args.population} rounds={args.rounds} mock={args.mock}"
    )
    print(f"数据集：{args.dataset}")
    if extras:
        # FlipAttack 单次成型，rounds 会被强制改成 1，进度按「样本数」而非「轮次」看
        print(extras)
    if args.offset:
        print(f"分批模式：跳过前 {args.offset} 条，本批运行第 {args.offset + 1}–{args.offset + args.limit} 条")
    if _needs_thinking_budget(args.model) and args.max_tokens < 4096:
        print(
            f"⚠ {args.model} 是推理型模型：max_tokens 会先被思维链消耗，"
            f"当前 {args.max_tokens} 极易只返回空白推理。建议加 --max-tokens 8192。"
        )
    if args.response_chars == 0:
        print("响应留存：完整响应（不截断），可直接用于 Judge 口径评分")
    print("-" * 72)

    try:
        for outputs in run_batch(
            str(args.dataset),
            args.limit,
            label,
            args.model,
            base_url,
            api_key,
            args.population,
            args.rounds,
            args.max_tokens,
            args.mock,
            not args.no_resume,
            bool(args.retry_errors),
            args.offset,
            args.flip_mode,
            not args.flip_no_cot,
            not args.flip_no_lang_gpt,
            not args.flip_no_few_shot,
            args.response_chars,
        ):
            meta = json.loads(outputs[1])
            done = int(meta.get("num_samples", 0))
            if done != last_meta.get("num_samples"):
                in_batch = max(done - args.offset, 0)
                elapsed = time.time() - started
                eta = (elapsed / in_batch * (args.limit - in_batch)) if in_batch else 0
                print(
                    f"[本批 {min(in_batch, args.limit)}/{args.limit} · 累计 {done}] "
                    f"拒答 {meta.get('num_refusals', 0)} · "
                    f"耗时 {elapsed / 60:.1f}min · 预计剩余 {eta / 60:.1f}min",
                    flush=True,
                )
            last_meta = meta
            last_status = outputs[2]
    except KeyboardInterrupt:
        print("\n已中断。结果已逐条落盘，重新运行同一条命令即可从断点继续。", file=sys.stderr)
        return 130

    print("-" * 72)
    print(f"完成 · 共 {last_meta.get('num_samples', 0)} 条 · 总耗时 {(time.time() - started) / 60:.1f}min")
    print(json.dumps(last_meta, ensure_ascii=False, indent=2))
    print(f"\n结果文件：{last_meta.get('output_path')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
