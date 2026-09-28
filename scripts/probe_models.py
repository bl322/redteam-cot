"""模型可用性探针：换 models / 换 key 后先跑它，再决定是否开批量评测。

用法（PowerShell）：
    $env:LLM_API_KEY = "sk-xxx"
    $env:LLM_BASE_URL = "dashscope.aliyuncs.com"   # 可选，默认阿里百炼兼容端点
    python scripts/probe_models.py                  # 探测默认候选表
    python scripts/probe_models.py --model qwen-max --model deepseek-r1

输出三块：
  1) 该端点上 Key 可见的模型清单（命中候选的会单独标注）
  2) 每个候选模型的一次真实小调用结果：OK / 错误类型 + 耗时
  3) 结论：哪个模型可直接用于 100 条批量跑
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_BASE_URL = "dashscope.aliyuncs.com"

# 常用候选：推理型（deepseek-r1 系）、通用商用（qwen-max/plus）、蒸馏小模型
CANDIDATES: List[str] = [
    "qwen3-max",
    "qwen-max",
    "qwen-plus",
    "qwen-turbo",
    "qwen-flash",
    "qwen3-plus",
    "deepseek-r1",
    "deepseek-r1-distill-qwen-7b",
    "deepseek-r1-distill-qwen-32b",
    "deepseek-v3",
    "qwen2.5-72b-instruct",
    "qwen2.5-32b-instruct",
    "qwen2.5-14b-instruct",
    "qwen2.5-7b-instruct",
]

PROMPT = "Reply with exactly: OK"


def _build_candidate_urls(base_url: str) -> List[str]:
    """与 engine.OpenAICompatibleLLM 保持一致的路径兜底顺序。"""
    value = base_url.strip().rstrip("/")
    if "://" not in value:
        value = "https://" + value.lstrip("/")
    match = None
    import re

    match = re.match(r"^(https?://[^/]+)(.*)$", value)
    if not match:
        return [value]
    origin, path = match.group(1), match.group(2)
    candidates: List[str] = []
    if path:
        candidates.append(origin + path)
    for suffix in ("/compatible-mode/v1", "/v1", "/api/v1", "/openai/v1"):
        candidate = origin + suffix
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def classify(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    lowered = text.lower()
    if "insufficient_quota" in lowered or "free quota exhausted" in lowered or "quota" in lowered:
        return "额度耗尽 / 需付费"
    if "401" in text or "unauthorized" in lowered or "invalid api key" in lowered:
        return "Key 无效"
    if "403" in text or "permission" in lowered or "access_denied" in lowered:
        return "无权限调用该模型"
    if "404" in text or "not_found" in lowered:
        return "模型不存在"
    if "429" in text or "rate" in lowered:
        return "限流（可重试）"
    if "400" in text or "invalid_request" in lowered:
        return "参数不兼容（需调整）"
    if "timeout" in lowered:
        return "超时"
    return "其他错误"


def probe(api_key: str, base_url: str, models: List[str], max_tokens: int, timeout: float) -> int:
    try:
        from openai import OpenAI
    except ImportError:
        print("缺少 openai 包：pip install openai>=1.108.0", file=sys.stderr)
        return 2

    urls = _build_candidate_urls(base_url)
    client = OpenAI(api_key=api_key, base_url=urls[0], timeout=timeout, max_retries=0)

    print(f"Base URL 候选：{urls[0]}（共 {len(urls)} 个兜底）")
    print("-" * 72)

    # 1) 列出该 Key 可见的模型
    visible: List[str] = []
    try:
        visible = sorted(m.id for m in client.models.list().data)
    except Exception as exc:
        print(f"[warn] 拉取模型清单失败：{classify(exc)}（不影响后续逐模型探测）")
    if visible:
        print(f"该端点可见模型 {len(visible)} 个，前 40 个：")
        print("  " + "、".join(visible[:40]))
        hit = [m for m in models if m in visible]
        if hit:
            print(f"  ✔ 候选命中：{ '、'.join(hit) }")
        miss = [m for m in models if m not in visible]
        if miss:
            print(f"  ✘ 不在清单中：{ '、'.join(miss) }")
    print("-" * 72)

    # 2) 逐个真实调用
    usable: List[str] = []
    print(f"{'模型':<32}{'状态':<22}{'耗时':>8}  备注")
    for model in models:
        started = time.time()
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": PROMPT}],
                temperature=0.2,
                max_tokens=max_tokens,
                timeout=timeout,
            )
            msg = resp.choices[0].message
            content = (msg.content or "").strip()
            if not content:
                content = (getattr(msg, "reasoning_content", "") or "").strip()
            elapsed = time.time() - started
            note = content.replace("\n", " ")[:24] or "(空响应)"
            if content:
                usable.append(model)
                print(f"{model:<32}{'✔ 可用':<22}{elapsed:>7.2f}s  {note}")
            else:
                print(f"{model:<32}{'⚠ 返回为空':<22}{elapsed:>7.2f}s  {note}")
        except Exception as exc:
            elapsed = time.time() - started
            print(f"{model:<32}{'✘ ' + classify(exc):<22}{elapsed:>7.2f}s  {str(exc)[:40]}")
    print("-" * 72)

    if usable:
        print("可直接用于 100 条批量跑的模型：")
        for name in usable:
            print(f"  python scripts/run_batch_cli.py --dataset data/dataset_sample100.csv "
                  f"--limit 100 --attack cot --model {name} --population 8 --rounds 3")
        return 0
    print("没有可用模型：请在控制台充值 / 关闭“仅使用免费额度”，或换一个有额度的模型名。")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="探测候选模型在当前 Key 下是否可调用")
    parser.add_argument("--model", action="append", default=[], help="要探测的模型（可重复）")
    parser.add_argument("--base-url", default="", help="Base URL 或域名，默认 env LLM_BASE_URL 或百炼兼容端点")
    parser.add_argument("--api-key", default="", help="API Key，默认读 env LLM_API_KEY")
    parser.add_argument("--max-tokens", type=int, default=32, help="探测用小调用的最大 token")
    parser.add_argument("--timeout", type=float, default=60.0, help="单次超时（秒）")
    args = parser.parse_args()

    api_key = args.api_key or os.getenv("LLM_API_KEY", "")
    if not api_key:
        print("错误：请设置 LLM_API_KEY（或 --api-key）", file=sys.stderr)
        return 2
    base_url = args.base_url or os.getenv("LLM_BASE_URL", "") or DEFAULT_BASE_URL
    models = list(args.model) if args.model else CANDIDATES
    return probe(api_key, base_url, models, args.max_tokens, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
