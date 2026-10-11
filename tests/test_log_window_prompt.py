"""Log-window prompt contracts against registered tools; no external I/O."""
import re

import pytest

from clients.loki_client import parse_time_range
from prompts.agent_prompt import agent_prompt
from tools.logs import query_logs
from tools.tool_registry import tool_registry


@pytest.fixture
def log_window_guidance():
    # Keep these contracts scoped to the log-selection rule.
    return agent_prompt.split("\n2. ", 1)[1].split("\n3. ", 1)[0]


def test_log_window_prompt_call_matches_registered_schema(log_window_guidance):
    call = re.search(r"query_logs\(([^)]*)\)", log_window_guidance)
    assert call is not None
    parameters = [name.strip() for name in call[1].split(",")]
    schema = tool_registry[query_logs.__name__]["schema"]
    assert schema["name"] == "query_logs"
    assert parameters == list(schema["parameters"]["properties"])
    assert schema["parameters"]["required"] == ["service"]
    assert "query_logs(service)" in log_window_guidance
    for name in parameters:
        assert schema["parameters"]["properties"][name]["type"] == "string"
    for name, contract in (("start", "UTC ISO 8601"), ("end", "24 小时")):
        assert contract in log_window_guidance
        assert contract in schema["parameters"]["properties"][name]["description"]


def test_log_window_prompt_uses_observation_window_and_pre_trigger_margin(log_window_guidance):
    for contract in (
        "有效时间戳", "服务支持 logs 能力", "优先考虑",
        "AlertEvent.starts_at", "不是 Prometheus 的 1 分钟指标窗口起点",
        "AlertEvent.window", "observed_at - window", "告警起始阶段",
        "采集", "规则评估", "日志入库延迟", "向前扩展", "告警触发之前",
        "不默认所有告警都有 1m 窗口",
    ):
        assert contract in log_window_guidance


def test_log_window_prompt_utc_example_obeys_time_parser(log_window_guidance):
    examples = dict(re.findall(r'(start|end)="([^"]+)"', log_window_guidance))
    assert set(examples) == {"start", "end"}
    start_ns, end_ns = parse_time_range(**examples)
    assert start_ns < end_ns
    for contract in (
        "start/end 必须成对", "UTC ISO 8601", "Z 或 +00:00", "不能混用本地时间",
        "未知时区不能猜测", "start < end", "[start, end)", "最多 24 小时",
        "示例不是固定查询窗口",
    ):
        assert contract in log_window_guidance


def test_log_window_prompt_keeps_queries_optional_and_budgeted(log_window_guidance):
    for contract in (
        "日志能力不可用", "时间信息不足", "已有足够证据", "不机械固定调用 query_logs",
        "不能编造时间范围", "query_logs(service)", "最近一小时", "证据局限",
        "现有证据约束", "Python 提供的工具预算",
    ):
        assert contract in log_window_guidance


def test_log_window_prompt_limits_no_data_and_historical_evidence(log_window_guidance):
    for contract in (
        "query_logs 返回 no_data", "不能推断服务正常",
        "历史窗口日志只证明对应时间段的情况", "不能当作当前状态",
        "当前状态需要及时的指标、健康检查等证据",
    ):
        assert contract in log_window_guidance


def test_log_window_prompt_preserves_alternative_error_paths(log_window_guidance):
    for contract in (
        "不能为了确认某个假设而选择性过滤相反证据", "允许不同错误路径同时存在",
        "日志时间、路径和异常类型", "关联程度", "不把同时发生视为共同根因",
        "truncated", "summary_truncated", "未展示的错误不能视为不存在",
    ):
        assert contract in log_window_guidance
