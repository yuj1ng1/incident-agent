"""Replay must finish from persisted tool results after a context rebuild."""
import json

import pytest

from model_client.demo_replay import DemoReplay


@pytest.mark.parametrize("rebuilt", [False, True])
def test_replay_finishes_from_real_tool_output_in_both_worker_context_forms(rebuilt):
    provider = DemoReplay()
    tools = [{"name": "query_metrics"}, {"name": "query_logs"}]
    first = provider.responses.create(
        model="demo", input=[], instructions="", tools=tools,
    )
    assert {call.name for call in first.output} == {"query_metrics", "query_logs"}
    results = [
        {"call_id": "demo-rate", "result": {"status": "ok", "value": 0.25}},
        {"call_id": "demo-logs", "result": {
            "status": "ok",
            "logs": [{"line": "service=order-service error_type=application_error"}],
        }},
    ]
    if rebuilt:
        history = [{
            "role": "user",
            "content": "durable evidence\n" + json.dumps({
                "recent_function_call_results": results,
            }),
        }]
    else:
        history = [{
            "type": "function_call_output",
            "call_id": entry["call_id"],
            "output": json.dumps(entry["result"]),
        } for entry in results]
    final = provider.responses.create(
        model="demo", input=history, instructions="", tools=tools,
    )
    assert all(item.type != "function_call" for item in final.output)
    assert "25.0%" in final.output_text
    assert "日志包含订单应用异常" in final.output_text
    assert "根因仍待核实" in final.output_text


def test_replay_retains_missing_evidence_in_the_final_answer():
    history = [{
        "type": "function_call_output",
        "call_id": call_id,
        "output": json.dumps({"status": "no_data"}),
    } for call_id in ("demo-rate", "demo-logs")]
    response = DemoReplay().responses.create(
        model="demo", input=history, instructions="", tools=[],
    )
    assert "没有返回可用" in response.output_text
    assert "未取得可用" in response.output_text
    assert "根因仍待核实" in response.output_text
