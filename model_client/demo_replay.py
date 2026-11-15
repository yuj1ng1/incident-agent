"""Offline model responses for the Compose order-service scenario."""
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from openai.types.responses import Response

CALL_IDS = {"demo-rate", "demo-logs"}


def replay_outputs(history):
    outputs = {}
    for item in history:
        if item.get("type") == "function_call_output" and item["call_id"] in CALL_IDS:
            outputs[item["call_id"]] = json.loads(item["output"])
        content = item.get("content")
        if not isinstance(content, str) or item.get("role") != "user":
            continue
        # The Worker rebuilds context with summaries when a prior tool call
        # was historical or the provider has no verified response chain.
        try:
            context = json.loads(content.rsplit("\n", 1)[-1])
        except ValueError:
            continue
        if isinstance(context, dict):
            for summary in context.get("recent_function_call_results", []):
                if summary["call_id"] in CALL_IDS:
                    outputs[summary["call_id"]] = summary["result"]
    return outputs


class DemoReplay:
    def with_options(self, *, max_retries, timeout):
        return self

    @property
    def responses(self):
        return self

    def create(self, *, model, input, instructions, tools):
        outputs = replay_outputs(input)
        names = {tool["name"] for tool in tools}
        if not outputs and {"query_metrics", "query_logs"} <= names:
            now = datetime.now(timezone.utc)
            plan = [
                ("demo-rate", "query_metrics", {"metric": "http_5xx_rate"}),
                ("demo-logs", "query_logs", {
                    "start": (now - timedelta(minutes=2)).isoformat(),
                    "end": now.isoformat(),
                }),
            ]
            output = [{
                "id": "fc-" + call_id,
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps({"service": "order-service"} | arguments),
            } for call_id, name, arguments in plan]
        else:
            rate, logs = outputs.get("demo-rate", {}), outputs.get("demo-logs", {})
            rate_text = (
                f"Prometheus 查询时的订单 HTTP 5xx 比例为 {rate['value']:.1%}。"
                if rate.get("status") == "ok" else "Prometheus 本轮没有返回可用的订单 HTTP 5xx 样本。"
            )
            if logs.get("status") == "ok":
                lines = logs.get("logs", [])
                log_text = (
                    "Loki 的近两分钟日志包含订单应用异常。"
                    if any("error_type=application_error" in row.get("line", "") for row in lines)
                    else "Loki 本轮返回了订单日志。"
                )
            else:
                log_text = "Loki 本轮未取得可用的订单日志。"
            output = [{
                "type": "message",
                "id": "msg-" + uuid4().hex,
                "role": "assistant",
                "status": "completed",
                "content": [{
                    "type": "output_text",
                    "text": f"演示回放。{rate_text}{log_text}根因仍待核实。",
                    "annotations": [],
                }],
            }]
        return Response.model_validate({
            "id": "resp-" + uuid4().hex,
            "created_at": datetime.now(timezone.utc).timestamp(),
            "model": "incident-demo-replay",
            "object": "response",
            "output": output,
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "status": "completed",
        })
