"""Opt-in real loopback HTTP checks using temporary test principals, never humans.

Use with w2_demo.py --manual-approval. `prepare` leaves an approved test recovery
for a later real-time `expired` check. No approval here is human acceptance.
"""
import argparse
import http.client
import json
from pathlib import Path


def run(root, port, phase="full"):
    root = Path(root)
    tokens = json.loads((root / "operators.json").read_text(encoding="utf-8"))
    identifiers = json.loads((root / "identifiers.json").read_text(encoding="utf-8"))
    evidence_path = root / "w3-http-acceptance.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8")) if evidence_path.exists() else []

    def request(path, role=None, body=None, expected=200, wrong_token=False):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            headers = {"Content-Type": "application/json"}
            if wrong_token or role:
                headers["Authorization"] = "Bearer " + ("invalid-test-token" * 3 if wrong_token else tokens[role])
            connection.request("GET" if body is None else "POST", path,
                               body=json.dumps(body) if body is not None else None, headers=headers)
            response = connection.getresponse()
            data = json.loads(response.read())
            evidence.append({"path": path, "test_role": role, "wrong_token": wrong_token,
                             "http_status": response.status, "response": data,
                             "operation_origin": "automated_test_principal"})
            assert response.status == expected, (path, response.status, data)
            return data
        finally:
            connection.close()
            evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    def command(record, operation, role, command_id, expected=200, **extra):
        return request(f"/api/remediations/{record['proposal_id']}/{operation}", role, {
            "command_id": command_id, "proposal_digest": record["proposal_digest"], **extra,
        }, expected)

    original = request("/api/remediations/" + identifiers["original"])
    if phase in {"full", "prepare", "tamper"}:
        assert original["status"] == "verification_failed"
        assert not original["related_proposals"] and original["source_evidence"]["valid"]
        request_url = f"/api/remediations/{original['proposal_id']}/compensation-requests"
        body = {"command_id": "w3-request", "proposal_digest": original["proposal_digest"],
                "reason": "自动化验收：核对固定 A 制品，申请从受控错误配置 B 恢复。"}
        request(request_url, body=body, expected=401)
        request(request_url, body=body, expected=401, wrong_token=True)
        request(request_url, "requester", {**body, "approved_by": "forged-approver"}, 422)
        applied = request(request_url, "requester", body)
        duplicate = request(request_url, "requester", body)
        child_id = applied["compensation"]["compensation_proposal_id"]
        assert duplicate["compensation"]["compensation_proposal_id"] == child_id
        child = request("/api/remediations/" + child_id)
        assert child["requested_by"] == "w3-requester"
        command(child, "approve", "requester", "requester-denied", 403)
        command(child, "approve", "executor", "executor-denied", 403)
        command(child, "execute", "approver", "approver-denied", 403)
        command(child, "execute", "executor", "unapproved-denied", 409)
        child = command(child, "approve", "approver", "w3-approve")
        assert child["approval_valid"] and child["approved_by"] == "w3-approver"
        command(child, "approve", "approver", "w3-approve")
        command(child, "approve", "approver", "second-decision-denied", 409)
        identifiers["compensation"] = child_id
        (root / "w3-acceptance-state.json").write_text(json.dumps(identifiers, indent=2), encoding="utf-8")
        if phase == "prepare":
            return {"status": "automated_approved_waiting_for_real_expiry", "child": child_id,
                    "expires_at": child["source_evidence"]["expires_at"]}
    else:
        identifiers = json.loads((root / "w3-acceptance-state.json").read_text(encoding="utf-8"))
        child = request("/api/remediations/" + identifiers["compensation"])
        child_id = child["proposal_id"]

    if phase == "expired":
        assert not child["source_evidence"]["valid"] and child["source_evidence"]["status"] == "stale"
        denied = command(child, "execute", "executor", "expired-denied", 409)
        assert denied["detail"] == "verification_stale"
        result = request("/api/remediations/" + child_id)
        assert result["status"] == "approved" and not result["actions"]
    elif phase == "tamper":
        # Only this explicitly supplied disposable experiment's A file is changed.
        stored = json.loads((root / "remediation.json").read_text(encoding="utf-8"))
        artifact = Path(stored["proposals"][child_id]["parameters"]["artifact_source"]).resolve()
        assert artifact.parent == (root / "artifacts").resolve()
        with artifact.open("ab") as output:
            output.write(b"W3 automated artifact mutation")
        result = command(child, "execute", "executor", "tampered-denied")
        assert result["execution_outcome"] == "not_executed"
        assert result["actions"][0]["reason"] == "demo_artifact_hash_mismatch"
    else:
        request(f"/api/remediations/{child_id}/execute", "executor", {
            "command_id": "old-summary-denied", "proposal_digest": "0" * 64,
        }, 409)
        result = command(child, "execute", "executor", "w3-execute")
        assert result["status"] == "succeeded" and result["verifications"][0]["outcome"] == "passed"
        for command_id in ("w3-execute", "new-duplicate-execute"):
            repeated = command(result, "execute", "executor", command_id)
            assert repeated["actions"] == result["actions"] and repeated["verifications"] == result["verifications"]
        events = result["audit"]
        approval = next(e for e in events if e["event"] == "approved")
        execution = next(e for e in events if e["event"] == "execution_started")
        assert approval["actor"] == "w3-approver" and execution["actor"] == "w3-executor"
        assert approval["proposal_digest"] == execution["proposal_digest"] == result["proposal_digest"]

    observations = {}
    for path in ("/version", "/health", "/inventory"):
        connection = http.client.HTTPConnection("127.0.0.1", identifiers["application_port"], timeout=3)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            observations[path] = {"http_status": response.status, "body": json.loads(response.read())}
        finally:
            connection.close()
    expected_release = "A" if phase == "full" else "B"
    assert observations["/version"]["body"]["release_id"] == expected_release
    assert observations["/health"]["http_status"] == 200
    assert observations["/inventory"]["http_status"] == (200 if phase == "full" else 500)
    if phase == "full":
        assert observations["/inventory"]["body"] == {
            "service": "inventory-service", "items": [{"item": "demo-item", "quantity": 10}],
        }
    (root / f"w3-independent-{phase}.json").write_text(json.dumps(observations, indent=2), encoding="utf-8")
    summary = {"status": "automated_checks_passed", "phase": phase,
               "child": child_id, "human_acceptance": "pending", "observed_release": expected_release}
    (root / f"w3-result-{phase}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--phase", choices=("full", "prepare", "tamper", "expired"), default="full")
    args = parser.parse_args()
    print(json.dumps(run(args.root, args.port, args.phase)))
