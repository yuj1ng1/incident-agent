"""Authorization tests verifying knowledge_write without breaking W3 roles."""
import hashlib
import json

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from web_api.auth import Operator, WebAuthorization


def _auth_config(entries):
    return json.dumps(
        [
            {
                "actor": actor,
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "permissions": permissions,
            }
            for actor, token, permissions in entries
        ]
    )


def _mini_app(auth: WebAuthorization) -> FastAPI:
    app = FastAPI()

    @app.post("/knowledge-write")
    def knowledge_write(operator: Operator = Depends(auth.require("knowledge_write"))):
        return {"actor": operator.actor}

    @app.post("/request")
    def request_op(operator: Operator = Depends(auth.require("request"))):
        return {"actor": operator.actor}

    @app.post("/approve")
    def approve_op(operator: Operator = Depends(auth.require("approve"))):
        return {"actor": operator.actor}

    @app.post("/execute")
    def execute_op(operator: Operator = Depends(auth.require("execute"))):
        return {"actor": operator.actor}

    return app


def test_knowledge_write_permission_enforced_and_isolated():
    writer_token = "knowledge-writer-token-" + "a" * 20
    requester_token = "remediation-requester-token-" + "b" * 20
    approver_token = "remediation-approver-token-" + "c" * 20
    executor_token = "remediation-executor-token-" + "d" * 20

    auth = WebAuthorization(
        _auth_config(
            [
                ("kb-writer", writer_token, ["knowledge_write"]),
                ("requester", requester_token, ["request"]),
                ("approver", approver_token, ["approve"]),
                ("executor", executor_token, ["execute"]),
            ]
        )
    )
    assert auth.enabled

    with TestClient(
        _mini_app(auth),
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50001),
    ) as client:
        assert (
            client.post(
                "/knowledge-write",
                headers={"Authorization": f"Bearer {writer_token}"},
            ).status_code
            == 200
        )
        for endpoint in ("/request", "/approve", "/execute"):
            assert (
                client.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {writer_token}"},
                ).status_code
                == 403
            )

        for token in (requester_token, approver_token, executor_token):
            assert (
                client.post(
                    "/knowledge-write",
                    headers={"Authorization": f"Bearer {token}"},
                ).status_code
                == 403
            )

        assert (
            client.post(
                "/request",
                headers={"Authorization": f"Bearer {requester_token}"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/approve",
                headers={"Authorization": f"Bearer {approver_token}"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/execute",
                headers={"Authorization": f"Bearer {executor_token}"},
            ).status_code
            == 200
        )


def test_invalid_permission_still_disables_authorization():
    token = "invalid-permission-token-" + "e" * 20
    auth = WebAuthorization(
        _auth_config([("bad-actor", token, ["knowledge_admin"])])
    )
    assert not auth.enabled
