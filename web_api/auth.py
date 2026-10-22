"""Local operator authorization. No credentials in frontend bundles or requests' bodies."""
import hashlib
import hmac
import json
import os
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from remediation.approval import actor_name

LOCAL_ORIGINS = ("http://127.0.0.1:5173", "http://localhost:5173")
Permission = Literal["request", "approve", "execute"]


class Operator(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    actor: str = Field(min_length=1, max_length=200)
    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    permissions: frozenset[Permission] = Field(min_length=1)


class WebAuthorization:
    def __init__(self, configuration=None):
        self.operators = ()
        raw = os.environ.get("INCIDENT_AGENT_WEB_OPERATORS", "") if configuration is None else configuration
        try:
            if len(raw) > 32768:
                raise ValueError()
            items = json.loads(raw) if raw else []
            if not isinstance(items, list) or len(items) > 20:
                raise ValueError()
            operators = tuple(Operator.model_validate(item) for item in items)
            for operator in operators:
                actor_name(operator.actor)
            if len({o.token_sha256 for o in operators}) != len(operators):
                raise ValueError()
            self.operators = operators
        except (ValueError, TypeError, ValidationError):
            # Invalid/missing configuration disables writes; never print the configuration.
            self.operators = ()

    @property
    def enabled(self):
        return bool(self.operators)

    def authenticate(self, request: Request):
        if not self.enabled:
            raise HTTPException(503, "write_auth_not_configured")
        if (request.client is None or request.client.host not in {"127.0.0.1", "::1"}
                or request.headers.get("origin") not in {None, *LOCAL_ORIGINS}):
            raise HTTPException(403, "local_operator_required")
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if (scheme.lower() != "bearer" or not 32 <= len(token) <= 512
                or not token.isascii() or any(c.isspace() for c in token)):
            raise HTTPException(401, "operator_authentication_required", headers={"WWW-Authenticate": "Bearer"})
        digest = hashlib.sha256(token.encode()).hexdigest()
        operator = next((o for o in self.operators if hmac.compare_digest(o.token_sha256, digest)), None)
        if operator is None:
            raise HTTPException(401, "operator_authentication_failed", headers={"WWW-Authenticate": "Bearer"})
        return operator

    def require(self, permission: Permission):
        def dependency(request: Request):
            operator = self.authenticate(request)
            if permission not in operator.permissions:
                raise HTTPException(403, "operator_permission_denied")
            return operator
        return dependency
