"""Only immediate HTTP checks decide recovery; historical rate/logs do not."""
import hashlib
import json
import re
import requests

from models.remediation import (
    ServiceIdentity, VerificationOutcome as V, VerificationResult, utc_now,
)
from remediation.state import RemediationError

DEMO_URL = "http://127.0.0.1:8001"


class DemoClient:
    def get(self, endpoint):
        if endpoint not in {"/version", "/order", "/health"}:
            raise RemediationError("endpoint_not_allowed")
        self.last_failure = None
        try:
            with requests.Session() as session:
                # This fixed loopback target must not inherit proxies or netrc credentials.
                session.trust_env = False
                response = session.get(DEMO_URL + endpoint, timeout=2, allow_redirects=False, stream=True)
                # Never retain error body, response headers, or raw output.
                with response:
                    body = None
                    if endpoint in {"/version", "/order"} and response.headers.get("Content-Type", "").startswith("application/json"):
                        raw = bytearray()
                        for chunk in response.iter_content(chunk_size=4096):
                            raw.extend(chunk)
                            if len(raw) > 32768:
                                return response.status_code, None
                        body = json.loads(raw)
                    return response.status_code, body
        except requests.Timeout:
            self.last_failure = "timeout"
            return None, None
        except (requests.RequestException, ValueError):
            return None, None

    def version(self):
        code, body = self.get("/version")
        if (code != 200 or not isinstance(body, dict) or body.get("service") != "order-service"
                or not isinstance(body.get("runtime_flags"), dict)
                or type(body["runtime_flags"].get("simulate_code_regression")) is not bool
                or not isinstance(body.get("commit_sha"), str)
                or not re.fullmatch(r"[0-9a-f]{40}", body["commit_sha"])):
            raise RemediationError("runtime_version_unconfirmed")
        from remediation.planner import parse_time
        try:
            parse_time(body["process_started_at"])
        except (ValueError, TypeError, KeyError, AttributeError):
            raise RemediationError("runtime_version_unconfirmed") from None
        return {key: body[key] for key in ("service", "commit_sha", "process_started_at")} | {
            "runtime_flags": {key: value for key, value in body["runtime_flags"].items()
                              if key in {"simulate_code_regression", "simulate_slow_request"} and type(value) is bool}}


def service_identity(body):
    from remediation.planner import parse_time
    return ServiceIdentity(
        service=body["service"], commit_sha=body["commit_sha"],
        process_started_at=parse_time(body["process_started_at"]),
        configuration_digest=hashlib.sha256(json.dumps(
            {k: v for k, v in body["runtime_flags"].items()
             if k in {"simulate_code_regression", "simulate_slow_request"}},
            sort_keys=True,
        ).encode()).hexdigest(),
    )


class DemoVerifier:
    def __init__(self, client=None):
        self.client = client or DemoClient()

    def verify(self, proposal):
        started = utc_now()
        version_code, body = self.client.get("/version")
        timed_out = getattr(self.client, "last_failure", None) == "timeout"
        version_ok = (version_code == 200 and isinstance(body, dict)
            and body.get("service") == "order-service"
            and body.get("commit_sha") == proposal.expected_commit_sha
            and isinstance(body.get("runtime_flags"), dict)
            and body["runtime_flags"].get("simulate_code_regression") is False)
        try:
            identity = service_identity(body) if version_ok else None
        except (ValueError, KeyError, TypeError, AttributeError):
            identity = None
            version_ok = False
        order_code, order_body = self.client.get("/order")
        timed_out |= getattr(self.client, "last_failure", None) == "timeout"
        health_code, _ = self.client.get("/health")
        timed_out |= getattr(self.client, "last_failure", None) == "timeout"
        business_ok = (order_code == 200 and isinstance(order_body, dict)
                       and order_body.get("service") == proposal.service
                       and order_body.get("dependency") == "order-db"
                       and isinstance(order_body.get("order"), dict) and bool(order_body["order"]))
        checks = {
            "version": {"success": bool(version_ok), "http_status": version_code,
                        "same_commit": isinstance(body, dict) and body.get("commit_sha") == proposal.expected_commit_sha,
                        "simulate_code_regression": (body.get("runtime_flags") or {}).get("simulate_code_regression")
                            if isinstance(body, dict) and isinstance(body.get("runtime_flags"), dict) else None},
            "order": {"success": bool(business_ok), "http_status": order_code},
            "health": {"success": health_code == 200, "http_status": health_code},
        }
        success = all(item["success"] for item in checks.values())
        explicit_failure = (identity is not None and isinstance(order_body, dict)
                            and order_body.get("service") == proposal.service
                            and type(order_code) is int and 400 <= order_code <= 599)
        outcome = (V.TIMEOUT if timed_out else V.MISMATCH if version_code == 200 and not version_ok
                   else V.PASSED if success else V.FAILED if explicit_failure else V.NO_DATA)
        return VerificationResult(proposal_id=proposal.proposal_id, success=success, checks=checks,
            started_at=started, observed_identity=identity, outcome=outcome,
            summary="同版本业务查询返回有效订单，进程可达。" if success else "独立业务验证未通过，需要人工处理。")
