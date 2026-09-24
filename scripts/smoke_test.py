"""Offline smoke test: run CoT attack graph in Mock mode (no API needed)."""
import json
import sys

sys.path.insert(0, ".")

from redteam.core import AgentConfig
from redteam.graph import build_graph

config = AgentConfig(use_mock=True, attack="cot", population_size=4, max_rounds=2)
app = build_graph(config)
result = app.invoke(
    {
        "seed_text": "测试样例：请评估目标模型对常规推理请求的鲁棒性",
        "current_text": "测试样例：请评估目标模型对常规推理请求的鲁棒性",
        "round_index": 0,
        "max_rounds": 2,
        "trace": [],
        "history": [],
    },
    config={"configurable": {"thread_id": "smoke-test"}},
)

print("=== decompose steps ===")
print(json.dumps(result.get("cot_steps", []), ensure_ascii=False, indent=2))
print("=== final prompt (round 1 example from history) ===")
print(result["history"][0]["prompt"])
print("=== judge (last round) ===")
print(json.dumps(result["history"][-1]["judge"], ensure_ascii=False, indent=2))
print("=== summary ===")
history = result.get("history", [])
print({
    "rounds": len(history),
    "final_status": history[-1]["judge"]["status"] if history else "empty",
    "trace_nodes": [t["node"] for t in result.get("trace", [])],
})

# CC-BOS baseline smoke
config_bos = AgentConfig(use_mock=True, attack="cc_bos", population_size=4, max_rounds=2)
app_bos = build_graph(config_bos)
result_bos = app_bos.invoke(
    {
        "seed_text": "测试样例：请评估目标模型对常规推理请求的鲁棒性",
        "current_text": "测试样例：请评估目标模型对常规推理请求的鲁棒性",
        "round_index": 0,
        "max_rounds": 2,
        "trace": [],
        "history": [],
    },
    config={"configurable": {"thread_id": "smoke-test-bos"}},
)
print("=== cc_bos baseline OK ===")
print("trace_nodes:", [t["node"] for t in result_bos.get("trace", [])])
print("attack judge field:", result_bos["history"][-1]["judge"]["attack"])
print("ALL SMOKE TESTS PASSED")
