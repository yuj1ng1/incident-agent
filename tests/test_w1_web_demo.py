"""The browser fixture uses actual API/approval/execution with TEMP files only."""
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

import remediation.compensation as compensation
import remediation.executor as execution
from scripts.w1_web_demo import create_demo


def test_demo_rejects_project_data_before_creating_anything():
    with pytest.raises(ValueError, match="temporary_directory"):
        create_demo(Path(__file__).resolve().parents[1] / "data" / "forbidden-demo")


def test_synthetic_workflows_preserve_approval_permissions_unknown_outcomes_and_effect_counts(tmp_path, monkeypatch):
    # create_demo changes only this test's process. Restore registries afterward.
    for module, name in ((compensation, "COMPENSATION_REGISTRY"),
                         (execution, "ACTION_REGISTRY"), (execution, "SERVICE_LOCK")):
        monkeypatch.setattr(module, name, getattr(module, name))
    root = tmp_path / "fixture"
    app = create_demo(root)
    tokens = json.loads((root / "operators.json").read_text())
    scenarios = json.loads((root / "scenarios.json").read_text())
    counter = 0

    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 32100)) as client:
        def post(identifier, operation, role, **extra):
            nonlocal counter
            counter += 1
            detail = client.get(f"/api/remediations/{identifier}").json()
            return client.post(
                f"/api/remediations/{identifier}/{operation}",
                json={"command_id": f"demo-test-{counter}", "proposal_digest": detail["proposal_digest"], **extra},
                headers={"Authorization": "Bearer " + tokens[role]},
            )

        assert len(client.get('/api/incidents').json()['items']) == 5
        for scenario in 'abcde':
            detail = client.get(f'/api/sessions/ses-synthetic-{scenario}')
            assert detail.status_code == 200, detail.text
            assert detail.json()['session']['total_model_requests'] == 0
        blocked = post(scenarios['A'], 'compensation-requests', 'requester', reason='合成人工申请')
        assert blocked.status_code == 200 and blocked.json()['compensation']['status'] == 'blocked'
        assert blocked.json()['requests'][-1]['requested_by'] == 'synthetic-requester'
        child = scenarios['B_compensation']
        before = (root / 'remediation.json').read_bytes()
        assert post(child, 'approve', 'requester').status_code == 403
        assert (root / 'remediation.json').read_bytes() == before
        assert client.post(f'/api/remediations/{child}/approve', json={}).status_code == 401
        assert post(child, 'execute', 'executor').status_code == 409
        assert post(child, 'approve', 'approver').status_code == 200
        response = post(child, 'execute', 'executor')
        assert response.status_code == 200, response.text
        assert response.json()['verifications'][0]['outcome'] == 'passed'
        assert json.loads((root / 'synthetic-b-service.json').read_text())['effects'] == 1
        requested = post(scenarios['C'], 'compensation-requests', 'requester', reason='人工主动回滚')
        assert requested.status_code == 200
        requested_id = requested.json()['compensation']['compensation_proposal_id']
        assert client.get(f'/api/remediations/{requested_id}').json()['status'] == 'pending'
        assert json.loads((root / 'synthetic-c-service.json').read_text())['effects'] == 0
        assert post(scenarios['D'], 'approve', 'requester').status_code == 403
        unknown = scenarios['E_compensation']
        assert post(unknown, 'execute', 'executor').status_code == 409
        checked = post(unknown, 'inspect-execution', 'executor')
        assert checked.status_code == 200 and checked.json()['execution_outcome'] == 'unknown'
        closed = post(unknown, 'close-execution', 'executor', reason='保留未知结局，仅人工收口')
        assert closed.status_code == 200 and closed.json()['status'] == 'closed'
        assert closed.json()['execution_outcome'] == 'unknown'
        assert json.loads((root / 'synthetic-e-service.json').read_text())['effects'] == 1
