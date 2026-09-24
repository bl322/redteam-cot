"""前端可视化渲染模块：把 CoT 攻击链路、评分与流程渲染为 HTML/CSS/SVG。

全部以字符串形式返回 HTML 片段，供 Gradio 的 gr.HTML 组件直接展示，
不引入 matplotlib / 前端构建工具等额外重依赖。
"""

from __future__ import annotations

import html
from typing import Any, Dict, List, Optional, Sequence


ACCENT = "#2563eb"
ACCENT_SOFT = "#dbeafe"
GREEN = "#16a34a"
RED = "#dc2626"
AMBER = "#d97706"
PURPLE = "#7c3aed"
SLATE = "#475569"
BORDER = "#e2e8f0"


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _wrap(content: str) -> str:
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\','
        "'Microsoft YaHei',sans-serif;color:#0f172a;\">" + content + "</div>"
    )


def _badge(text: str, color: str, bg: str) -> str:
    return (
        f'<span style="display:inline-block;padding:2px 10px;border-radius:999px;'
        f'font-size:12px;line-height:20px;color:{color};background:{bg};'
        f'border:1px solid {color}33;margin-right:6px;">{_esc(text)}</span>'
    )


def _card(title: str, body: str, accent: str = ACCENT) -> str:
    return (
        f'<div style="border:1px solid {BORDER};border-radius:10px;background:#ffffff;'
        f'padding:14px 16px;margin-bottom:12px;box-shadow:0 1px 2px rgba(15,23,42,.04);'
        f'border-left:4px solid {accent};">'
        f'<div style="font-size:13px;font-weight:600;color:{SLATE};margin-bottom:10px;'
        f'letter-spacing:.3px;">{_esc(title)}</div>{body}</div>'
    )


# ----------------------------------------------------------------------
# 关键指标卡片
# ----------------------------------------------------------------------
def render_summary_cards(summary: Dict[str, Any]) -> str:
    status = str(summary.get("final_status", "empty"))
    status_color = {
        "answered": GREEN,
        "refusal": RED,
        "error": AMBER,
    }.get(status, SLATE)
    items = [
        ("迭代轮数", summary.get("rounds", 0), ACCENT),
        ("拒答轮数", summary.get("refusals", 0), AMBER),
        ("链路跟随率", summary.get("chain_follow_rate", "-"), PURPLE),
        ("最终状态", status, status_color),
    ]
    cells = []
    for label, value, color in items:
        cells.append(
            f'<div style="flex:1 1 0;min-width:120px;border:1px solid {BORDER};'
            f'border-radius:10px;background:#ffffff;padding:12px 14px;text-align:center;">'
            f'<div style="font-size:11px;color:{SLATE};letter-spacing:.5px;">{_esc(label)}</div>'
            f'<div style="font-size:22px;font-weight:600;color:{color};margin-top:6px;">'
            f'{_esc(value)}</div></div>'
        )
    return _wrap(
        '<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:4px;">'
        + "".join(cells)
        + "</div>"
    )


# ----------------------------------------------------------------------
# 执行进度（流式）
# ----------------------------------------------------------------------
NODE_LABELS = {
    "decompose": "目标分解",
    "generate": "推理链组装",
    "interact": "请求目标模型",
    "judge": "裁判评估",
}


def render_progress(
    round_index: int,
    max_rounds: int,
    node: str = "",
    status: str = "",
    elapsed: float = 0.0,
) -> str:
    ratio = 0.0 if max_rounds <= 0 else max(0.0, min(1.0, round_index / float(max_rounds)))
    node_text = NODE_LABELS.get(node, node or "准备中")
    status_badge = ""
    if status:
        color = GREEN if status == "answered" else RED
        status_badge = _badge(f"上一轮：{status}", color, "#ffffff")
    bar = (
        f'<div style="height:10px;border-radius:999px;background:#eef2f7;overflow:hidden;">'
        f'<div style="height:10px;width:{ratio * 100:.1f}%;background:{ACCENT};'
        f'border-radius:999px;transition:width .3s;"></div></div>'
    )
    return _wrap(
        _card(
            f"执行中 · 第 {round_index} / {max_rounds} 轮",
            f'<div style="display:flex;justify-content:space-between;font-size:12px;'
            f'margin-bottom:6px;"><span style="color:{SLATE};">当前节点：<b>{_esc(node_text)}</b></span>'
            f'<span style="color:{SLATE};">已耗时 {elapsed:.1f}s</span></div>'
            + bar
            + f'<div style="margin-top:8px;">{status_badge}'
            + f'<span style="font-size:12px;color:{SLATE};">'
            "真实模型单次请求通常需要 10~30 秒，请耐心等待进度推进</span></div>",
            ACCENT,
        )
    )


# ----------------------------------------------------------------------
# CoT 推理链可视化
# ----------------------------------------------------------------------
def render_chain(steps: Sequence[str], labels: Optional[Dict[str, str]] = None) -> str:
    labels = labels or {}
    if not steps:
        return _wrap(
            _card(
                "CoT 推理链",
                f'<div style="font-size:13px;color:{SLATE};">'
                "当前为 CC-BOS 基线模式，未生成推理链；切换 Attack Strategy 为 "
                "<b>cot</b> 可查看思维链分解结构。</div>",
                AMBER,
            )
        )

    chips = "".join(
        _badge(f"{key}: {value}", SLATE, "#f1f5f9") for key, value in labels.items()
    )
    rows = []
    for idx, step in enumerate(steps, start=1):
        is_final = idx == len(steps)
        color = RED if is_final else ACCENT
        tag = "收束步（目标意图）" if is_final else f"子问题 {idx}"
        rows.append(
            f'<div style="display:flex;gap:12px;align-items:flex-start;">'
            f'<div style="flex:0 0 30px;height:30px;border-radius:50%;background:{color};'
            f'color:#ffffff;font-size:13px;font-weight:600;display:flex;'
            f'align-items:center;justify-content:center;">{idx}</div>'
            f'<div style="flex:1;border:1px solid {BORDER};border-radius:8px;padding:10px 12px;'
            f'background:{"#fff7f7" if is_final else "#f8fafc"};">'
            f'<div style="font-size:11px;color:{color};font-weight:600;margin-bottom:4px;">'
            f'{_esc(tag)}</div>'
            f'<div style="font-size:13px;line-height:1.6;color:#0f172a;">{_esc(step)}</div>'
            f"</div></div>"
        )
        if not is_final:
            rows.append(
                f'<div style="width:30px;display:flex;justify-content:center;">'
                f'<div style="width:2px;height:12px;background:{BORDER};"></div></div>'
            )

    return _wrap(
        _card(
            f"CoT 推理链分解（{len(steps)} 步）",
            f'<div style="margin-bottom:12px;line-height:22px;">{chips}</div>' + "".join(rows),
            PURPLE,
        )
    )


# ----------------------------------------------------------------------
# 多维评分条形图
# ----------------------------------------------------------------------
SCORE_LABELS = {
    "semantic_preservation": "语义保留度",
    "cot_style": "推理链风格度",
    "chain_depth": "分解深度",
    "concealment": "隐蔽度",
    "classical_style": "古文风格度",
    "brevity_balance": "简洁度",
    "lexical_diversity": "词汇多样性",
    "total": "综合适应度",
}


def render_score_bars(scores: Dict[str, float]) -> str:
    if not scores:
        return _wrap("")
    rows = []
    for key, value in scores.items():
        label = SCORE_LABELS.get(key, key)
        try:
            ratio = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            continue
        is_total = key == "total"
        color = ACCENT if not is_total else PURPLE
        rows.append(
            f'<div style="margin-bottom:10px;">'
            f'<div style="display:flex;justify-content:space-between;font-size:12px;'
            f'margin-bottom:4px;"><span style="color:{SLATE};">{_esc(label)}</span>'
            f'<span style="font-weight:600;color:{color};">{value:.4f}</span></div>'
            f'<div style="height:8px;border-radius:999px;background:#eef2f7;overflow:hidden;">'
            f'<div style="height:8px;width:{ratio * 100:.1f}%;background:{color};'
            f'border-radius:999px;"></div></div></div>'
        )
    return _wrap(_card("多维适应度评分（当前最佳候选）", "".join(rows), GREEN))


# ----------------------------------------------------------------------
# 多轮评分趋势（SVG 折线）
# ----------------------------------------------------------------------
def render_trend(scores_by_round: Dict[int, Dict[str, Any]]) -> str:
    rounds = sorted(scores_by_round.keys())
    if not rounds:
        return _wrap("")

    keys = ["total"]
    for candidate in ("semantic_preservation", "cot_style", "concealment"):
        if any(candidate in scores_by_round[r] for r in rounds):
            keys.append(candidate)
    keys = keys[:4]

    width, height = 640, 190
    pad_l, pad_r, pad_t, pad_b = 44, 16, 14, 26
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    step_x = plot_w / max(len(rounds) - 1, 1)

    colors = [PURPLE, ACCENT, GREEN, AMBER]
    polylines = []
    legend = []
    for color, key in zip(colors, keys):
        points = []
        for idx, rnd in enumerate(rounds):
            value = float(scores_by_round[rnd].get(key, 0.0))
            x = pad_l + idx * step_x
            y = pad_t + plot_h * (1.0 - max(0.0, min(1.0, value)))
            points.append(f"{x:.1f},{y:.1f}")
        polylines.append(
            f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" '
            f'stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>'
        )
        for point in points:
            x, y = point.split(",")
            polylines.append(
                f'<circle cx="{x}" cy="{y}" r="3" fill="#ffffff" stroke="{color}" stroke-width="2"/>'
            )
        legend.append(_badge(SCORE_LABELS.get(key, key), color, "#ffffff"))

    y_axis = []
    for tick in (0.0, 0.5, 1.0):
        y = pad_t + plot_h * (1.0 - tick)
        y_axis.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
            f'stroke="#e8eef5" stroke-width="1"/>'
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="10" '
            f'fill="{SLATE}">{tick:.1f}</text>'
        )
    x_axis = []
    for idx, rnd in enumerate(rounds):
        x = pad_l + idx * step_x
        x_axis.append(
            f'<text x="{x:.1f}" y="{height - 8}" text-anchor="middle" font-size="10" '
            f'fill="{SLATE}">R{rnd}</text>'
        )

    svg = (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'style="display:block;">'
        + "".join(y_axis)
        + "".join(polylines)
        + "".join(x_axis)
        + "</svg>"
    )
    return _wrap(
        _card("多轮适应度演化趋势", svg + f'<div style="margin-top:8px;">{"".join(legend)}</div>', ACCENT)
    )


# ----------------------------------------------------------------------
# 轮次时间线
# ----------------------------------------------------------------------
def render_timeline(history: List[Dict[str, Any]], attack: str = "cot") -> str:
    if not history:
        return _wrap("")

    blocks = []
    for entry in history:
        judge = entry.get("judge", {}) or {}
        status = str(judge.get("status", ""))
        color = GREEN if status == "answered" else RED
        metrics = []
        if attack == "cot":
            followed = bool(judge.get("chain_followed"))
            concluded = bool(judge.get("conclusion_reached"))
            metrics.append(
                _badge(
                    f"链路跟随: {'是' if followed else '否'}",
                    GREEN if followed else SLATE,
                    "#f0fdf4" if followed else "#f1f5f9",
                )
            )
            metrics.append(
                _badge(
                    f"收束结论: {'是' if concluded else '否'}",
                    GREEN if concluded else SLATE,
                    "#f0fdf4" if concluded else "#f1f5f9",
                )
            )
        markers = judge.get("chain_markers") or []
        if markers:
            metrics.append(_badge("标记: " + "/".join(markers[:4]), SLATE, "#f1f5f9"))

        prompt = str(entry.get("prompt", ""))
        response = str(entry.get("response", ""))
        blocks.append(
            f'<div style="border:1px solid {BORDER};border-radius:8px;padding:12px;'
            f'margin-bottom:10px;background:#ffffff;">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;'
            f'margin-bottom:8px;">'
            f'<span style="font-size:12px;font-weight:600;color:{SLATE};">'
            f'Round {_esc(entry.get("round", ""))}</span>'
            + _badge(status, color, "#ffffff")
            + "</div>"
            f'<div style="margin-bottom:8px;line-height:20px;">{"".join(metrics)}</div>'
            f'<div style="font-size:12px;color:{SLATE};margin-bottom:4px;">攻击 Prompt</div>'
            f'<div style="font-size:12px;background:#f8fafc;border:1px solid {BORDER};'
            f'border-radius:6px;padding:8px;white-space:pre-wrap;word-break:break-word;'
            f'max-height:120px;overflow:auto;">{_esc(prompt)}</div>'
            f'<div style="font-size:12px;color:{SLATE};margin:8px 0 4px;">目标模型回复</div>'
            f'<div style="font-size:12px;background:#f8fafc;border:1px solid {BORDER};'
            f'border-radius:6px;padding:8px;white-space:pre-wrap;word-break:break-word;'
            f'max-height:120px;overflow:auto;">{_esc(response)}</div>'
            "</div>"
        )
    return _wrap(_card("逐轮攻击轨迹时间线", "".join(blocks), SLATE))


# ----------------------------------------------------------------------
# 架构流程（纯 CSS）
# ----------------------------------------------------------------------
def render_pipeline(attack: str = "cot") -> str:
    if attack == "cot":
        nodes = [
            ("START", "输入 Seed Goal", SLATE),
            ("Decompose", "目标分解 → 推理子问题链", PURPLE),
            ("Generate", "FOA 演化 + 推理链组装", ACCENT),
            ("Interact", "请求目标大模型", ACCENT),
            ("Judge", "裁判 + 链路跟随度", ACCENT),
            ("END", "落盘/返回可视化", GREEN),
        ]
    else:
        nodes = [
            ("START", "输入 Seed Goal", SLATE),
            ("Generate", "FOA 演化 + 文言改写", ACCENT),
            ("Interact", "请求目标大模型", ACCENT),
            ("Judge", "裁判评估", ACCENT),
            ("END", "落盘/返回可视化", GREEN),
        ]

    cells = []
    for idx, (name, desc, color) in enumerate(nodes):
        cells.append(
            f'<div style="flex:0 0 auto;min-width:132px;border:1px solid {color}33;'
            f'border-radius:10px;background:#ffffff;padding:10px 12px;text-align:center;'
            f'border-top:3px solid {color};">'
            f'<div style="font-size:13px;font-weight:600;color:{color};">{_esc(name)}</div>'
            f'<div style="font-size:11px;color:{SLATE};margin-top:4px;">{_esc(desc)}</div>'
            "</div>"
        )
        if idx < len(nodes) - 1:
            cells.append(
                f'<div style="flex:0 0 auto;align-self:center;color:{SLATE};'
                f'font-size:18px;padding:0 4px;">→</div>'
            )

    loop = (
        f'<div style="margin-top:12px;font-size:12px;color:{SLATE};line-height:1.7;">'
        f'<b>闭环规则：</b>Judge 判定未拒答且未耗尽预算 ⇒ 回到 Generate 继续演化下一轮；'
        f'命中拒答模板或达到 Max Rounds ⇒ 进入 END 落盘。</div>'
    )
    return _wrap(
        _card(
            f"LangGraph 攻击链路编排（当前：{attack}）",
            '<div style="display:flex;align-items:stretch;gap:2px;flex-wrap:wrap;">'
            + "".join(cells)
            + "</div>"
            + loop,
            PURPLE if attack == "cot" else ACCENT,
        )
    )


# ----------------------------------------------------------------------
# 资源占用监控
# ----------------------------------------------------------------------
def render_resources() -> str:
    try:
        import psutil  # type: ignore
    except ImportError:
        return _wrap(
            _card(
                "资源占用监控",
                f'<div style="font-size:13px;color:{SLATE};">'
                "未安装 <code>psutil</code>，资源监控不可用。执行 "
                "<code>pip install psutil</code> 后可查看 CPU / 内存占用。</div>",
                AMBER,
            )
        )

    cpu = psutil.cpu_percent(interval=0.2)
    mem = psutil.virtual_memory()
    rows = [
        ("CPU 占用", f"{cpu:.1f}%", cpu / 100.0, ACCENT),
        ("内存占用", f"{mem.used / 1024 ** 3:.2f} GB / {mem.total / 1024 ** 3:.2f} GB", mem.percent / 100.0, GREEN),
    ]
    try:
        disk = psutil.disk_usage(".")
        rows.append(
            (
                "磁盘占用",
                f"{disk.used / 1024 ** 3:.1f} GB / {disk.total / 1024 ** 3:.1f} GB",
                disk.percent / 100.0,
                AMBER,
            )
        )
    except Exception:
        pass

    body = []
    for label, value, ratio, color in rows:
        body.append(
            f'<div style="margin-bottom:10px;">'
            f'<div style="display:flex;justify-content:space-between;font-size:12px;'
            f'margin-bottom:4px;"><span style="color:{SLATE};">{_esc(label)}</span>'
            f'<span style="font-weight:600;color:{color};">{_esc(value)}</span></div>'
            f'<div style="height:8px;border-radius:999px;background:#eef2f7;overflow:hidden;">'
            f'<div style="height:8px;width:{max(0.0, min(1.0, ratio)) * 100:.1f}%;'
            f'background:{color};border-radius:999px;"></div></div></div>'
        )
    return _wrap(_card("运行环境资源占用", "".join(body), SLATE))
