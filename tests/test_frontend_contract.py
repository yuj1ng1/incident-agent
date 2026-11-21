"""Prevent an OpenAPI snapshot from masking backend response drift in CI."""
import json
from pathlib import Path

from pydantic import ValidationError
import pytest

from frontend.tests.export_api_contract import public_contract
from web_api.schemas import MetricSignal


def test_frontend_contract_matches_live_openapi_and_response_serialization():
    path = Path(__file__).resolve().parents[1] / "frontend" / "tests" / "api-contract.json"
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert public_contract() == expected, (
        "Public API changed: review frontend types, then regenerate the OpenAPI "
        "snapshot with frontend/tests/export_api_contract.py"
    )


@pytest.mark.parametrize("infos", [
    None, "info", [None], [True], [1], [""], ["x" * 513], ["info"] * 9,
])
def test_metric_signal_contract_rejects_invalid_or_oversized_infos(infos):
    with pytest.raises(ValidationError):
        MetricSignal(
            metric="http_latency_p95_seconds",
            status="ok",
            reason=None,
            samples=[],
            infos=infos,
        )


def test_metric_signal_contract_serializes_bounded_infos_without_samples():
    infos = ["x" * 512] * 8
    signal = MetricSignal(
        metric="http_latency_p95_seconds",
        status="no_data",
        reason="no_samples_in_metric_range",
        samples=[],
        infos=infos,
    )
    assert signal.model_dump(mode="json")["infos"] == infos
    assert signal.samples == []
