"""Deterministic metric evidence analysis without I/O or current-state claims.

Callers must supply trusted tool outputs or records loaded by RuntimeStore. Validation
here checks consistency, not authenticity. Worker summaries are not raw evidence.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
from typing import Literal, Sequence

from pydantic import ValidationError

from clients.prometheus_client import (
    MAX_ALIGNED_METRICS,
    MAX_RANGE_POINTS,
    MAX_RANGE_RESPONSE_BYTES,
    parse_metric_evaluation_time,
    parse_metric_range,
)
from investigation.models import Checkpoint, ToolObservation
from services.metric_profiles import HTTP_METRICS, POSTGRES_METRICS


MAX_ANALYSIS_OBSERVATIONS = 64
MAX_ANALYSIS_INPUT_BYTES = 262144
MAX_ANALYSIS_OUTPUT_BYTES = 32768
MAX_ANALYSIS_SAMPLES = 480
MAX_REPORTED_CHANGES = 16
_MAX_INPUT_NODES = 16384
_MAX_INPUT_DEPTH = 12
_METRIC_TOOLS = {"query_metrics", "query_metric_range", "query_metrics_aligned"}
_METRICS = frozenset(HTTP_METRICS + POSTGRES_METRICS)
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,120}")
_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class MetricToolResult:
    """Attach a stable caller-owned reference to an already validated tool result."""

    observation_id: str
    name: str
    result: dict


@dataclass(frozen=True)
class MetricSample:
    evaluated_at: str
    value: float


@dataclass(frozen=True)
class ConfirmedMetricObservation:
    observation_id: str
    name: str
    service: str
    metric: str
    evidence_scope: str | None
    promql: str | None
    samples: tuple[MetricSample, ...]
    runtime_identity_key: str | None
    use_for_current_state: Literal[False] = False


@dataclass(frozen=True)
class EvidenceNote:
    code: str
    message: str
    source_observation_ids: tuple[str, ...] = ()
    metrics: tuple[str, ...] = ()


@dataclass(frozen=True)
class ComparisonFinding:
    code: str
    source_observation_ids: tuple[str, ...]
    metrics: tuple[str, ...]
    evaluated_times: tuple[str, ...]
    strict_time_alignment: bool
    comparison_complete: bool | None = None
    missing_metrics: tuple[str, ...] = ()
    comparison_time: str | None = None


@dataclass(frozen=True)
class ObservedChange:
    before: MetricSample
    after: MetricSample
    missing_points_between: int


@dataclass(frozen=True)
class RangeFinding:
    source_observation_ids: tuple[str, ...]
    metric: str
    first: MetricSample | None
    last: MetricSample | None
    observed_min: float | None
    observed_max: float | None
    expected_point_count: int
    sample_count: int
    missing_point_count: int
    missing_evaluated_at: tuple[str, ...]
    observed_change_count: int
    changes: tuple[ObservedChange, ...]
    changes_truncated: bool
    code: Literal["range_trend"] = "range_trend"


@dataclass(frozen=True)
class MetricAnalysis:
    status: Literal["complete", "partial", "insufficient_evidence"]
    confirmed_observations: tuple[ConfirmedMetricObservation, ...] = ()
    temporal_findings: tuple[ComparisonFinding | RangeFinding, ...] = ()
    limitations: tuple[EvidenceNote, ...] = ()
    hypotheses_to_verify: tuple[EvidenceNote, ...] = ()
    suggested_checks: tuple[EvidenceNote, ...] = ()
    source_observation_ids: tuple[str, ...] = ()
    use_for_current_state: Literal[False] = False

    def to_dict(self) -> dict:
        """Return a detached, JSON-serializable report."""
        return asdict(self)


_BASE_LIMITATIONS = (
    EvidenceNote(
        "evaluation_not_scrape_time",
        "相同 Prometheus 评估时刻不保证原始样本抓取时刻一致。",
    ),
    EvidenceNote("queries_not_atomic", "多个 HTTP 查询不具有事务原子性。"),
    EvidenceNote(
        "not_current_state_evidence",
        "分析只说明输入评估时刻或历史时间窗；不校验当前新鲜度，也不证明当前健康。",
    ),
    EvidenceNote(
        "trusted_source_required",
        "结构校验不能认证来源；调用方必须提供可信工具结果或已校验的 Checkpoint 原始记录。",
    ),
)


class _InvalidEvidence(ValueError):
    pass


@dataclass(frozen=True)
class _Evidence:
    observation_id: str
    name: str
    result: dict
    identity_key: str | None = None
    arguments: dict | None = None
    success: bool = True
    call_id: str | None = None


def _bounded_input(value, byte_limit=MAX_ANALYSIS_INPUT_BYTES):
    """Check size/depth before copying or Pydantic validation; stop on first excess."""
    nodes = 0
    size = 0
    active = set()

    def visit(item, depth):
        nonlocal nodes, size
        nodes += 1
        if depth > _MAX_INPUT_DEPTH or nodes > _MAX_INPUT_NODES:
            raise _InvalidEvidence("analysis_input_limit_exceeded")
        if isinstance(item, (MetricToolResult, ToolObservation, Checkpoint)):
            item = item.__dict__
        if isinstance(item, (dict, list, tuple)):
            if id(item) in active:
                raise _InvalidEvidence("invalid_input_structure")
            if len(item) > _MAX_INPUT_NODES:
                raise _InvalidEvidence("analysis_input_limit_exceeded")
            size += 2 + len(item)
            active.add(id(item))
            children = item.items() if isinstance(item, dict) else enumerate(item)
            for key, child in children:
                if isinstance(item, dict):
                    if not isinstance(key, str):
                        raise _InvalidEvidence("invalid_input_structure")
                    visit(key, depth + 1)
                    size += 1
                visit(child, depth + 1)
            active.remove(id(item))
        elif isinstance(item, str):
            if len(item) > 8192:
                raise _InvalidEvidence("analysis_input_limit_exceeded")
            try:
                size += len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
            except UnicodeError:
                raise _InvalidEvidence("invalid_input_encoding") from None
        elif isinstance(item, datetime):
            size += len(item.isoformat()) + 2
        elif item is None or isinstance(item, (bool, float)):
            size += 32
        elif isinstance(item, int):
            if item.bit_length() > 1024:
                raise _InvalidEvidence("invalid_input_structure")
            size += len(str(item))
        else:
            raise _InvalidEvidence("invalid_input_structure")
        if size > byte_limit:
            raise _InvalidEvidence("analysis_input_limit_exceeded")

    visit(value, 0)


def _time(value) -> datetime:
    # Actual tool outputs have at most microseconds. Reject higher precision rather
    # than silently rounding distinct instants into apparent strict alignment.
    if (
        not isinstance(value, str)
        or _TIME.fullmatch(value) is None
        or value.endswith("-00:00")
    ):
        raise _InvalidEvidence("invalid_evaluation_time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        parsed = parsed.astimezone(timezone.utc)
        if parsed < _EPOCH:
            raise ValueError
        return parsed
    except (ValueError, OverflowError):
        raise _InvalidEvidence("invalid_evaluation_time") from None


def _identifier(value):
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise _InvalidEvidence("invalid_observation_identity")
    return value


def _request_interval(result, required=False):
    if not required:
        if not {"query_started_at", "query_finished_at"} & result.keys():
            return None
        if (
            result.get("status") == "no_data"
            and result.get("query_started_at") is None
            and result.get("query_finished_at") is None
        ):
            return None
    start = _time(result.get("query_started_at"))
    end = _time(result.get("query_finished_at"))
    if start > end:
        raise _InvalidEvidence("invalid_request_interval")
    return start, end


def _value(metric, value):
    if type(value) not in {int, float}:
        raise _InvalidEvidence("invalid_metric_value")
    raw_value = value
    try:
        value = float(raw_value)
    except OverflowError:
        raise _InvalidEvidence("invalid_metric_value") from None
    if (
        not math.isfinite(value) or value < 0
        or (type(raw_value) is int and value != raw_value)
    ):
        raise _InvalidEvidence("invalid_metric_value")
    if (
        metric in {"service_scrape_up", "exporter_scrape_up", "postgres_up"}
        and value not in {0, 1}
    ) or (
        metric in {"http_5xx_rate", "postgres_connection_usage"} and value > 1
    ):
        raise _InvalidEvidence("metric_value_outside_semantic_domain")
    return value


def _metric(value):
    if not isinstance(value, str) or value not in _METRICS:
        raise _InvalidEvidence("unsupported_metric")
    return value


def _observation(
    evidence,
    service,
    metric,
    samples,
    item=None,
):
    item = item if item is not None else evidence.result
    promql = item.get("promql")
    if promql is not None and (
        not isinstance(promql, str) or not promql or len(promql) > 4096
    ):
        raise _InvalidEvidence("invalid_expression_metadata")
    return ConfirmedMetricObservation(
        observation_id=evidence.observation_id,
        name=evidence.name,
        service=service,
        metric=metric,
        evidence_scope=evidence.result.get("evidence_scope"),
        promql=promql,
        samples=tuple(samples),
        runtime_identity_key=evidence.identity_key,
    )


def _evidence(source, service):
    if isinstance(source, MetricToolResult):
        evidence = _Evidence(source.observation_id, source.name, source.result)
    else:
        raw = (
            source.model_dump(mode="python")
            if isinstance(source, ToolObservation) else source
        )
        if not isinstance(raw, dict) or type(raw.get("success")) is not bool:
            raise _InvalidEvidence("invalid_checkpoint_observation")
        collected = raw.get("collected_at")
        _time(collected.isoformat() if isinstance(collected, datetime) else collected)
        try:
            record = ToolObservation.model_validate(raw)
        except ValidationError:
            raise _InvalidEvidence("invalid_checkpoint_observation") from None
        _identifier(record.call_id)
        if re.fullmatch(r"[0-9a-f]{64}", record.cache_key) is None:
            raise _InvalidEvidence("invalid_checkpoint_observation")
        if record.collected_at < _EPOCH:
            raise _InvalidEvidence("invalid_checkpoint_observation")
        arguments = record.arguments
        required = {
            "query_metrics": {"service", "metric"},
            "query_metric_range": {"service", "metric", "start", "end", "step_seconds"},
            "query_metrics_aligned": {"service", "metrics", "at"},
        }.get(record.name)
        if (
            required is None or set(arguments) != required
            or arguments.get("service") != service
        ):
            raise _InvalidEvidence("checkpoint_target_or_arguments_mismatch")
        try:
            identity_key = None if record.identity is None else hashlib.sha256(
                json.dumps(
                    record.identity,
                    sort_keys=True,
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
        except ValueError:
            raise _InvalidEvidence("invalid_checkpoint_identity") from None
        evidence = _Evidence(
            record.observation_id,
            record.name,
            record.result,
            identity_key,
            arguments,
            record.success,
            record.call_id,
        )
    _identifier(evidence.observation_id)
    if (
        not isinstance(evidence.name, str) or evidence.name not in _METRIC_TOOLS
        or not isinstance(evidence.result, dict)
    ):
        raise _InvalidEvidence("invalid_metric_evidence")
    return evidence


def _header(evidence, service):
    result = evidence.result
    if result.get("service") != service:
        raise _InvalidEvidence("cross_service_evidence")
    if any(key.startswith("summary_") for key in result):
        raise _InvalidEvidence("summary_is_not_raw_evidence")
    if evidence.name in {"query_metric_range", "query_metrics_aligned"}:
        if (
            result.get("evidence_scope") != "time_window"
            or result.get("use_for_current_state") is not False
        ):
            raise _InvalidEvidence("invalid_historical_scope")
    elif (
        "evidence_scope" in result and result["evidence_scope"] != "time_window"
    ):
        raise _InvalidEvidence("invalid_evidence_scope")
    if (
        "use_for_current_state" in result
        and type(result["use_for_current_state"]) is not bool
    ):
        raise _InvalidEvidence("invalid_evidence_scope")
    if (
        result.get("evidence_scope") == "time_window"
        and result.get("use_for_current_state") is not False
    ):
        raise _InvalidEvidence("invalid_historical_scope")
    _request_interval(result)


def _missing(evidence, metric, executed=True):
    return EvidenceNote(
        "missing_metric_sample" if executed else "request_not_executed",
        "未取得有效样本，不能视为数值零或健康。" if executed else "请求未执行，没有有效观测。",
        (evidence.observation_id,),
        (metric,),
    )


def _instant(evidence, service):
    result = evidence.result
    if {"samples", "results"} & result.keys():
        raise _InvalidEvidence("metric_tool_shape_mismatch")
    metric = _metric(result.get("metric"))
    if evidence.arguments is not None and evidence.arguments["metric"] != metric:
        raise _InvalidEvidence("checkpoint_metric_mismatch")
    if result["status"] == "no_data":
        if "value" in result or "evaluated_at" in result:
            raise _InvalidEvidence("missing_sample_has_measurement")
        return [], [], [_missing(evidence, metric)]
    if result["status"] != "ok" or result.get("request_executed", True) is not True:
        raise _InvalidEvidence("invalid_instant_observation")
    sample = MetricSample(
        _time(result.get("evaluated_at")).isoformat(),
        _value(metric, result.get("value")),
    )
    return [_observation(evidence, service, metric, [sample])], [], []


def _aligned(evidence, service):
    result = evidence.result
    names = result.get("requested_metrics")
    items = result.get("results")
    if result["status"] == "no_data" and items == {}:
        return [], [], [EvidenceNote(
            "no_valid_observation",
            "未完成有效指标比较。",
            (evidence.observation_id,),
        )]
    if (
        not isinstance(names, list) or not 2 <= len(names) <= MAX_ALIGNED_METRICS
        or any(not isinstance(name, str) or name not in _METRICS for name in names)
        or len(set(names)) != len(names)
        or not isinstance(items, dict) or set(items) != set(names)
        or type(result.get("comparison_complete")) is not bool
    ):
        raise _InvalidEvidence("invalid_aligned_comparison")
    at = _time(result.get("evaluation_time"))
    requested_at = result.get("requested_evaluation_time")
    if evidence.arguments is not None:
        arguments = evidence.arguments
        if (
            not isinstance(arguments["metrics"], str)
            or len(arguments["metrics"]) > 259
            or not isinstance(arguments["at"], str)
            or [name.strip() for name in arguments["metrics"].split(",")] != names
            or arguments["at"] != requested_at
        ):
            raise _InvalidEvidence("checkpoint_comparison_mismatch")
    if requested_at is not None:
        interval = _request_interval(result)
        reference = interval[0] if interval else at + timedelta(seconds=1)
        parsed, _ = parse_metric_evaluation_time(requested_at, reference)
        if parsed != at:
            raise _InvalidEvidence("declared_evaluation_time_mismatch")
    observations = []
    notes = []
    missing = []
    for metric in names:
        item = items[metric]
        try:
            if (
                not isinstance(item, dict) or item.get("metric") != metric
                or type(item.get("request_executed")) is not bool
                or ("service" in item and item["service"] != service)
            ):
                raise _InvalidEvidence("invalid_aligned_item")
            executed = item["request_executed"]
            if executed:
                _request_interval(item, required=True)
            elif (
                item.get("query_started_at") is not None
                or item.get("query_finished_at") is not None
            ):
                raise _InvalidEvidence("unexecuted_request_has_timing")
            if item.get("status") == "no_data":
                if "value" in item or "evaluated_at" in item:
                    raise _InvalidEvidence("missing_sample_has_measurement")
                missing.append(metric)
                notes.append(_missing(evidence, metric, executed))
                continue
            if item.get("status") != "ok" or not executed:
                raise _InvalidEvidence("invalid_aligned_item")
            stamp = _time(item.get("evaluated_at"))
            if stamp != at:
                raise _InvalidEvidence("sample_evaluation_time_mismatch")
            sample = MetricSample(
                stamp.isoformat(),
                _value(metric, item.get("value")),
            )
            observations.append(_observation(
                evidence, service, metric, [sample], item,
            ))
        except _InvalidEvidence as exc:
            missing.append(metric)
            notes.append(EvidenceNote(
                str(exc),
                "该项证据未通过校验。",
                (evidence.observation_id,),
                (metric,),
            ))
    complete = len(observations) == len(names)
    expected_status = (
        "ok" if complete else ("incomplete" if observations else "no_data")
    )
    if result["comparison_complete"] != complete or result["status"] != expected_status:
        # An inconsistent envelope cannot certify even apparently valid members.
        raise _InvalidEvidence("comparison_metadata_mismatch")
    finding = ComparisonFinding(
        code=(
            "aligned_comparison_complete"
            if complete else "aligned_comparison_incomplete"
        ),
        source_observation_ids=(evidence.observation_id,),
        metrics=tuple(names),
        evaluated_times=(at.isoformat(),) if observations else (),
        strict_time_alignment=complete,
        comparison_complete=complete,
        missing_metrics=tuple(missing),
        comparison_time=at.isoformat(),
    )
    return observations, [finding], notes


def _range(evidence, service):
    result = evidence.result
    metric = _metric(result.get("metric"))
    if evidence.arguments is not None and evidence.arguments["metric"] != metric:
        raise _InvalidEvidence("checkpoint_metric_mismatch")
    if result["status"] == "no_data" and result.get("missing_point_count") is None:
        return [], [], [_missing(evidence, metric)]
    window = result.get("query_range")
    if not isinstance(window, dict):
        raise _InvalidEvidence("invalid_range_metadata")
    start, end = _time(window.get("start")), _time(window.get("end"))
    step = result.get("step_seconds")
    actual_start, actual_end, expected = parse_metric_range(
        window["start"], window["end"], step, end,
    )
    if (
        (actual_start, actual_end) != (start, end)
        or window.get("bounds", "[start, end]") != "[start, end]"
        or result.get("truncated") is not False
        or type(result.get("expected_point_count")) is not int
        or result["expected_point_count"] != expected
    ):
        raise _InvalidEvidence("invalid_range_metadata")
    requested = result.get("requested_range")
    if evidence.arguments is not None:
        arguments = evidence.arguments
        if (
            requested != {"start": arguments["start"], "end": arguments["end"]}
            or type(arguments["step_seconds"]) is not int
            or arguments["step_seconds"] != step
        ):
            raise _InvalidEvidence("checkpoint_range_mismatch")
    if requested is not None:
        if not isinstance(requested, dict) or set(requested) != {"start", "end"}:
            raise _InvalidEvidence("invalid_range_metadata")
        interval = _request_interval(result)
        reference = interval[0] if interval else end + timedelta(milliseconds=1)
        requested_start, requested_end, _ = parse_metric_range(
            requested["start"], requested["end"], step, reference,
        )
        if (requested_start, requested_end) != (start, end):
            raise _InvalidEvidence("requested_range_mismatch")
    raw_samples = result.get("samples")
    if not isinstance(raw_samples, list) or len(raw_samples) > MAX_RANGE_POINTS:
        raise _InvalidEvidence("invalid_range_samples")
    samples = []
    previous = None
    for item in raw_samples:
        if not isinstance(item, dict) or set(item) != {"evaluated_at", "value"}:
            raise _InvalidEvidence("invalid_range_sample")
        stamp = _time(item["evaluated_at"])
        if (
            not start <= stamp <= end
            or (previous is not None and stamp <= previous)
            or (stamp - start) % timedelta(seconds=step) != timedelta(0)
        ):
            raise _InvalidEvidence("range_sample_time_mismatch")
        samples.append(MetricSample(stamp.isoformat(), _value(metric, item["value"])))
        previous = stamp
    observed = {sample.evaluated_at for sample in samples}
    expected_times = (
        (start + timedelta(seconds=index * step)).isoformat()
        for index in range(expected)
    )
    missing = tuple(stamp for stamp in expected_times if stamp not in observed)
    if (
        type(result.get("sample_count")) is not int
        or result["sample_count"] != len(samples)
        or type(result.get("missing_point_count")) is not int
        or result["missing_point_count"] != len(missing)
        or not isinstance(result.get("missing_evaluated_at"), list)
        or tuple(
            _time(stamp).isoformat() for stamp in result["missing_evaluated_at"]
        ) != missing
        or result["status"] != ("ok" if samples else "no_data")
    ):
        raise _InvalidEvidence("range_counts_or_gaps_mismatch")
    changes = [
        ObservedChange(
            before,
            after,
            (
                (_time(after.evaluated_at) - _time(before.evaluated_at))
                // timedelta(seconds=step) - 1
            ),
        )
        for before, after in zip(samples, samples[1:]) if before.value != after.value
    ]
    values = [sample.value for sample in samples]
    finding = RangeFinding(
        source_observation_ids=(evidence.observation_id,),
        metric=metric,
        first=samples[0] if samples else None,
        last=samples[-1] if samples else None,
        observed_min=min(values) if values else None,
        observed_max=max(values) if values else None,
        expected_point_count=expected,
        sample_count=len(samples),
        missing_point_count=len(missing),
        missing_evaluated_at=missing,
        observed_change_count=len(changes),
        changes=tuple(changes[:MAX_REPORTED_CHANGES]),
        changes_truncated=len(changes) > MAX_REPORTED_CHANGES,
    )
    notes = []
    if missing:
        notes.append(EvidenceNote(
            "range_has_gaps",
            "缺失评估点不插值、不补零，也不证明没有异常。",
            (evidence.observation_id,),
            (metric,),
        ))
    if finding.changes_truncated:
        notes.append(EvidenceNote(
            "change_details_limited",
            "变化总数保留，变化区间明细达到输出上限。",
            (evidence.observation_id,),
            (metric,),
        ))
    observations = [_observation(evidence, service, metric, samples)] if samples else []
    return observations, [finding], notes


def _comparisons(observations):
    groups = {}
    timelines = {}
    findings, notes, hypotheses = [], [], []
    for observation in observations:
        if observation.name == "query_metric_range":
            continue
        at = observation.samples[0].evaluated_at
        identity = (observation.runtime_identity_key, observation.evidence_scope)
        groups.setdefault((identity, at), []).append(observation)
        timelines.setdefault(identity, []).append(observation)
    if len({item.runtime_identity_key for item in observations}) > 1:
        notes.append(EvidenceNote(
            "runtime_identities_differ",
            "不同运行身份的观测不组成联合状态。",
            tuple(sorted({item.observation_id for item in observations})),
        ))
    for items in timelines.values():
        times = tuple(sorted({item.samples[0].evaluated_at for item in items}))
        metrics = tuple(sorted({item.metric for item in items}))
        if len(times) > 1 and len(metrics) > 1:
            findings.append(ComparisonFinding(
                "evaluation_times_differ",
                tuple(sorted({item.observation_id for item in items})),
                metrics,
                times,
                False,
                False,
            ))
            notes.append(EvidenceNote(
                "time_not_aligned",
                "不同评估时刻不能拼成同一时刻的系统快照。",
                findings[-1].source_observation_ids,
                metrics,
            ))
    for (_, at), items in groups.items():
        metrics = tuple(sorted({item.metric for item in items}))
        ids = tuple(sorted({item.observation_id for item in items}))
        by_metric = {
            metric: [item for item in items if item.metric == metric]
            for metric in metrics
        }
        conflicts = [
            metric for metric, repeated in by_metric.items()
            if len({(item.samples[0].value, item.promql) for item in repeated}) > 1
        ]
        if conflicts:
            findings.append(ComparisonFinding(
                "same_time_value_or_expression_conflict",
                ids,
                tuple(conflicts),
                (at,),
                True,
                False,
            ))
            notes.append(EvidenceNote(
                "conflicting_observations",
                "同刻重复指标存在不同值或表达式；保留各自观测，不选择其中一个作为联合状态。",
                ids,
                tuple(conflicts),
            ))
            continue
        if len(metrics) < 2:
            continue
        findings.append(ComparisonFinding(
            "strict_time_alignment", ids, metrics, (at,), True,
        ))
        values = {
            metric: repeated[0].samples[0].value
            for metric, repeated in by_metric.items()
        }
        if values.get("http_request_rate") == 0 and values.get("http_5xx_rate", 0) > 0:
            involved = ("http_request_rate", "http_5xx_rate")
            findings.append(ComparisonFinding(
                "numeric_consistency_question", ids, involved, (at,), True,
            ))
            hypotheses.append(EvidenceNote(
                "query_or_sample_consistency_to_verify",
                "候选解释：查询窗口、原始样本或后端读取一致性可能影响该数值组合；原因尚未验证。",
                ids,
                involved,
            ))
    return findings, notes, hypotheses


def _insufficient(code, ids=()):
    return MetricAnalysis(
        status="insufficient_evidence",
        limitations=_BASE_LIMITATIONS + (EvidenceNote(
            code,
            "无法在可信、完整且有界的输入内完成分析。",
            ids,
        ),),
        source_observation_ids=ids,
    )


def analyze_metric_evidence(
    observations: Sequence[MetricToolResult | ToolObservation | dict],
    *,
    service: str,
) -> MetricAnalysis:
    """Analyze a bounded list/tuple of raw results or full Checkpoint observations.

    No clock, registry lookup, cache eligibility, model call, tool call or mutation.
    Missing data and rejected inputs make a report partial; no valid values make
    it insufficient. Size/provenance ambiguity fails closed without partial facts.
    """
    try:
        _identifier(service)
        if not isinstance(observations, (list, tuple)):
            raise _InvalidEvidence("invalid_observation_collection")
        if len(observations) > MAX_ANALYSIS_OBSERVATIONS:
            raise _InvalidEvidence("analysis_input_limit_exceeded")
        _bounded_input(observations)
    except _InvalidEvidence as exc:
        return _insufficient(str(exc))
    confirmed, findings, notes, ids = [], [], [], []
    evidence_inputs = []
    call_ids = set()
    for source in observations:
        raw_id = (
            source.get("observation_id") if isinstance(source, dict)
            else getattr(source, "observation_id", None)
        )
        if isinstance(raw_id, str) and _IDENTIFIER.fullmatch(raw_id):
            if raw_id in ids:
                return _insufficient("duplicate_observation_id", tuple(sorted(ids)))
            ids.append(raw_id)
        try:
            _bounded_input(source, MAX_RANGE_RESPONSE_BYTES)
            evidence = _evidence(source, service)
            if evidence.call_id is not None:
                if evidence.call_id in call_ids:
                    return _insufficient("duplicate_call_id", tuple(sorted(ids)))
                call_ids.add(evidence.call_id)
            evidence_inputs.append(evidence)
        except _InvalidEvidence as exc:
            reference = (raw_id,) if raw_id in ids else ()
            notes.append(EvidenceNote(
                str(exc),
                "输入记录未通过校验，不能作为确认事实。",
                reference,
            ))
    for evidence in sorted(evidence_inputs, key=lambda item: item.observation_id):
        try:
            status = evidence.result.get("status")
            if not evidence.success or status not in {"ok", "incomplete", "no_data"}:
                notes.append(EvidenceNote(
                    "failed_or_unexecuted_observation",
                    "请求失败或未执行，没有完成有效观测。",
                    (evidence.observation_id,),
                ))
                continue
            _header(evidence, service)
            parser = {
                "query_metrics": _instant,
                "query_metrics_aligned": _aligned,
                "query_metric_range": _range,
            }[evidence.name]
            observed, temporal, limitations = parser(evidence, service)
            confirmed.extend(observed)
            findings.extend(temporal)
            notes.extend(limitations)
        except (ValueError, TypeError, OverflowError) as exc:
            code = (
                str(exc) if isinstance(exc, _InvalidEvidence)
                else "invalid_metric_evidence"
            )
            notes.append(EvidenceNote(
                code,
                "指标证据未通过校验，未升级为确认事实。",
                (evidence.observation_id,),
            ))
    if sum(len(item.samples) for item in confirmed) > MAX_ANALYSIS_SAMPLES:
        return _insufficient("analysis_sample_limit_exceeded", tuple(sorted(ids)))
    comparisons, comparison_notes, hypotheses = _comparisons(confirmed)
    findings.extend(comparisons)
    notes.extend(comparison_notes)
    checks = []
    if any(note.code == "time_not_aligned" for note in notes):
        checks.append(EvidenceNote(
            "obtain_same_evaluation_metrics",
            "建议在同一明确评估时刻获取相关指标；建议不会自动执行请求。",
            tuple(sorted(ids)),
        ))
    if any(note.code in {
        "range_has_gaps", "missing_metric_sample", "request_not_executed",
    } for note in notes):
        checks.append(EvidenceNote(
            "inspect_samples_and_scrapes",
            "建议检查原始样本和抓取记录，核实缺失原因。",
            tuple(sorted(ids)),
        ))
    if hypotheses:
        checks.append(EvidenceNote(
            "inspect_counters_scrapes_and_query_times",
            "建议核查原始计数器连续性、抓取记录、PromQL 窗口和实际查询时间；"
            "不能据此确认数据库故障、代码回归、重启或计数器重置。",
            tuple(sorted(ids)),
        ))
    if not observations:
        notes.append(EvidenceNote("no_observations", "没有提供原始指标观测。"))
    result = MetricAnalysis(
        status=(
            ("partial" if notes else "complete")
            if confirmed else "insufficient_evidence"
        ),
        confirmed_observations=tuple(confirmed),
        temporal_findings=tuple(findings),
        limitations=_BASE_LIMITATIONS + tuple(notes),
        hypotheses_to_verify=tuple(hypotheses),
        suggested_checks=tuple(checks),
        source_observation_ids=tuple(sorted(ids)),
    )
    output_bytes = len(json.dumps(
        result.to_dict(), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8"))
    if output_bytes > MAX_ANALYSIS_OUTPUT_BYTES:
        return _insufficient("analysis_output_limit_exceeded", tuple(sorted(ids)))
    return result


def analyze_checkpoint_metrics(
    checkpoint: Checkpoint | dict,
    *,
    service: str,
) -> MetricAnalysis:
    """Validate a bounded existing-schema Checkpoint and analyze metric records only.

    This does not authenticate a SQLite row or bind it to a Session. Callers must
    use RuntimeStore's existing row/session/schema checks before calling this API.
    """
    try:
        _bounded_input(checkpoint)
        raw = (
            checkpoint.model_dump(mode="python")
            if isinstance(checkpoint, Checkpoint) else checkpoint
        )
        if not isinstance(raw, dict):
            raise _InvalidEvidence("invalid_checkpoint")
        if (
            "schema_version" in raw
            and (type(raw["schema_version"]) is not int or raw["schema_version"] != 1)
        ):
            raise _InvalidEvidence("invalid_checkpoint")
        Checkpoint.model_validate(raw)
    except (ValidationError, _InvalidEvidence) as exc:
        code = str(exc) if isinstance(exc, _InvalidEvidence) else "invalid_checkpoint"
        return _insufficient(code)
    # Preserve original record types/flags: Pydantic coercion must not certify
    # e.g. success="true" as an actually executed Python observation.
    return analyze_metric_evidence(
        [record for record in raw.get("records", []) if (
            record.name if isinstance(record, ToolObservation) else record.get("name")
        ) in _METRIC_TOOLS],
        service=service,
    )
