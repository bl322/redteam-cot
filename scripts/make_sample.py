"""从全量数据集做分层随机抽样，产出领域均衡的小样本评测集。

为什么需要：全量 dataset.csv 是按一级领域排序的，直接 `--limit 100` 只会取到
前 100 行 —— 实测全部落在「违反社会主义核心价值观」一个领域，报告里的领域对比
会完全失真。本脚本按「一级领域比例分配 + 每域保底 N 条、领域内再按二级领域比例
分配（最大余数法）」抽样，固定随机种子，结果可复现。

用法：
    python scripts/make_sample.py --size 100 --out data/dataset_sample100.csv
"""

from __future__ import annotations

import argparse
import csv
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List

DEFAULT_DATASET = Path(__file__).resolve().parents[1] / "data" / "dataset.csv"


def largest_remainder(counts: Dict[str, int], total: int) -> Dict[str, int]:
    """按 counts 的比例把 total 个名额分下去（最大余数法，保证和为 total）。"""
    population = sum(counts.values())
    if population == 0:
        return {k: 0 for k in counts}
    exact = {k: total * v / population for k, v in counts.items()}
    alloc = {k: int(v) for k, v in exact.items()}
    remainder = total - sum(alloc.values())
    order = sorted(counts, key=lambda k: (-(exact[k] - alloc[k]), k))
    for key in order[:remainder]:
        alloc[key] += 1
    return alloc


def stratified_sample(
    rows: List[dict],
    size: int,
    floor: int,
    seed: int,
    primary_key: str = "一级领域",
    secondary_key: str = "二级领域",
) -> List[dict]:
    rng = random.Random(seed)

    by_primary: Dict[str, List[dict]] = defaultdict(list)
    for row in rows:
        by_primary[row.get(primary_key, "unknown")].append(row)

    primary_counts = {k: len(v) for k, v in by_primary.items()}
    alloc = largest_remainder(primary_counts, size)

    # 保底：小领域至少 floor 条，多出来的名额按原比例从大领域里扣
    if floor > 0:
        deficit = {k: max(0, floor - v) for k, v in alloc.items()}
        need = sum(deficit.values())
        if need:
            donors = {k: v for k, v in alloc.items() if alloc[k] > floor}
            donor_pool = sum(donors[k] - floor for k in donors)
            for key in donors:
                take = int(round(need * (donors[key] - floor) / donor_pool)) if donor_pool else 0
                take = min(take, donors[key] - floor)
                alloc[key] -= take
                need -= take
            # 余数仍不为 0 时，从最大的域继续扣
            for key in sorted(donors, key=lambda k: -alloc[k]):
                while need > 0 and alloc[key] > floor:
                    alloc[key] -= 1
                    need -= 1
            for key, extra in deficit.items():
                alloc[key] = max(alloc[key], floor)
        # 再校准回 size
        diff = sum(alloc.values()) - size
        while diff > 0:
            key = max((k for k in alloc if alloc[k] > floor), key=lambda k: alloc[k], default=None)
            if key is None:
                break
            alloc[key] -= 1
            diff -= 1

    picked: List[dict] = []
    for primary in sorted(by_primary):
        pool = list(by_primary[primary])
        quota = alloc.get(primary, 0)
        if quota <= 0:
            continue
        by_secondary: Dict[str, List[dict]] = defaultdict(list)
        for row in pool:
            by_secondary[row.get(secondary_key, "unknown")].append(row)
        sub_alloc = largest_remainder({k: len(v) for k, v in by_secondary.items()}, quota)
        for secondary in sorted(by_secondary):
            rng.shuffle(by_secondary[secondary])
            picked.extend(by_secondary[secondary][: sub_alloc.get(secondary, 0)])

    rng.shuffle(picked)
    return picked


def main() -> int:
    parser = argparse.ArgumentParser(description="分层随机抽样生成小样本评测集")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--size", type=int, default=100)
    parser.add_argument("--floor", type=int, default=10, help="每个一级领域保底条数（0 表示纯比例）")
    parser.add_argument("--seed", type=int, default=20260927)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    with args.dataset.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    picked = stratified_sample(rows, args.size, args.floor, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys())
    with args.out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(picked)

    print(f"写出 {len(picked)} 条 -> {args.out}")
    print("一级领域分布：")
    for k, v in Counter(r.get("一级领域", "") for r in picked).most_common():
        base = Counter(r.get("一级领域", "") for r in rows)[k]
        print(f"  {k}: {v} (全量 {base})")
    print(f"二级领域覆盖：{len(set(r.get('二级领域', '') for r in picked))} / "
          f"{len(set(r.get('二级领域', '') for r in rows))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
