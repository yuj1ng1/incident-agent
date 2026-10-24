"""Run the explicitly started W2 API through separate disposable operator roles.

These are test principals, not a claim of real human approval. No model calls.
"""
import argparse
import http.client
import json
from pathlib import Path


def run(root, port):
    root = Path(root)
    tokens = json.loads((root / "operators.json").read_text(encoding="utf-8"))
    evidence = []

    def request(
        path,
        role=None,
        body=None,
        expected=200,
    ):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        try:
            headers = {"Content-Type": "application/json"}
            if role:
                headers["Authorization"] = "Bearer " + tokens[role]
            connection.request("GET" if body is None else "POST", path,
                               body=json.dumps(body) if body is not None else None,
                               headers=headers)
            response = connection.getresponse()
            data = json.loads(response.read())
            evidence.append({"path": path, "role": role, "http_status": response.status,
                             "response": data})
            assert response.status == expected, (path, response.status, data)
            return data
        finally:
            connection.close()
            (root / "api-acceptance.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8",
            )

    def command(
        record,
        operation,
        role,
        identifier,
        expected=200,
        **extra,
    ):
        return request(f"/api/remediations/{record['proposal_id']}/{operation}", role,
                       {"proposal_digest": record["proposal_digest"],
                        "command_id": identifier, **extra}, expected)

    original = request("/api/remediations/rem-w2-deploy-b")
    command(original, "execute", "executor", "original-unapproved", 409)
    command(original, "approve", "requester", "requester-cannot-approve", 403)
    original = command(original, "approve", "deploy-approver", "deploy-approval")
    original = command(original, "execute", "executor", "deploy-once")
    assert original["status"] == "verification_failed"
    child_id = original["compensation"]["compensation_proposal_id"]
    assert child_id
    child = request("/api/remediations/" + child_id)
    command(child, "execute", "executor", "rollback-unapproved", 409)
    child = command(child, "approve", "rollback-approver", "rollback-approval")
    request(f"/api/remediations/{child_id}/execute", "executor", {
        "proposal_digest": "0" * 64, "command_id": "wrong-digest",
    }, 409)
    child = command(child, "execute", "executor", "restore-once")
    assert child["status"] == "succeeded", child
    again = command(child, "execute", "executor", "restore-once")
    assert again["actions"] == child["actions"]
    assert again["verifications"] == child["verifications"]
    identifiers = json.loads((root / "identifiers.json").read_text(encoding="utf-8"))
    identifiers["compensation"] = child_id
    (root / "identifiers.json").write_text(json.dumps(identifiers, indent=2), encoding="utf-8")
    print(json.dumps({"original": original["status"], "compensation": child["status"],
                      "duplicate_effects": 0, "compensation_id": child_id}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--port", type=int, default=18020)
    args = parser.parse_args()
    run(args.root, args.port)
