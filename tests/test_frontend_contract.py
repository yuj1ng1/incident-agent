"""Prevent an OpenAPI snapshot from masking backend response drift in CI."""
import json
from pathlib import Path

from frontend.tests.export_api_contract import public_contract


def test_frontend_contract_matches_live_openapi_and_response_serialization():
    path = Path(__file__).resolve().parents[1] / "frontend" / "tests" / "api-contract.json"
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert public_contract() == expected, (
        "Public API changed: review frontend types, then regenerate the OpenAPI "
        "snapshot with frontend/tests/export_api_contract.py"
    )
