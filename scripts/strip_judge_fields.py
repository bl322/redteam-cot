"""剥离历史跑批结果中的裁判评分字段，只保留关键词判定与流程观测字段。

处理范围：results/redteam_batch/ 下的 *.jsonl（逐条结果）、*_summary.json、
*_checkpoint.json。

- 删除：harmfulness / max_harmfulness / attack_success / llm 等评分字段
- 改写：final_status 若来自评分通道（deflection/partial/full/unclear），
        按关键词拒答结果回落为 refusal / answered

用法：
    python scripts/strip_judge_fields.py            # 默认处理 results/redteam_batch
    python scripts/strip_judge_fields.py --dry-run  # 只看会改什么
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

JUDGE_FIELDS = ("harmfulness", "max_harmfulness", "attack_success", "llm", "llm_verdicts")
JUDGE_STATUSES = {"deflection", "partial", "full", "unclear"}
SUMMARY_FIELDS = (
    "avg_harmfulness",
    "avg_max_harmfulness",
    "attack_success_rate",
    "num_judged",
    "llm_judge_enabled",
    "judged",
)


def _strip_record(record: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in record.items() if k not in JUDGE_FIELDS}
    status = str(out.get("final_status", ""))
    if status in JUDGE_STATUSES:
        out["final_status"] = "refusal" if int(out.get("refusals", 0) or 0) > 0 else "answered"
    return out


def _strip_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in summary.items() if k not in SUMMARY_FIELDS}


def process(path: Path, dry_run: bool) -> int:
    changed = 0
    if path.suffix == ".jsonl":
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        out_lines: List[str] = []
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                out_lines.append(line)
                continue
            stripped = _strip_record(record)
            if stripped != record:
                changed += 1
            out_lines.append(json.dumps(stripped, ensure_ascii=False))
        if not dry_run:
            path.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")
        return changed

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0
    if isinstance(payload, dict):
        if "summary" in payload and isinstance(payload["summary"], dict):
            before = payload["summary"]
            payload["summary"] = _strip_summary(before)
            changed += len(before) - len(payload["summary"])
        else:
            stripped = _strip_summary(payload)
            changed += len(payload) - len(stripped)
            payload = stripped
        if not dry_run:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description="剥离跑批结果中的裁判评分字段")
    parser.add_argument("target", nargs="?", type=Path, default=None, help="目标目录，默认 results/redteam_batch")
    parser.add_argument("--dry-run", action="store_true", help="只统计不落盘")
    args = parser.parse_args()

    target = args.target or (Path(__file__).resolve().parents[1] / "results" / "redteam_batch")
    if not target.exists():
        print(f"目录不存在：{target}")
        return 2

    total = 0
    for path in sorted(target.rglob("*")):
        if path.is_file() and path.suffix in (".jsonl", ".json"):
            n = process(path, args.dry_run)
            if n:
                total += n
                print(f"{'将改' if args.dry_run else '已改'} {n:>4} 处：{path.name}")
    print(f"\n合计 {total} 处{'（dry-run 未落盘）' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
