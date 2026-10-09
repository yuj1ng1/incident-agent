"""Only immediate HTTP checks decide recovery; historical rate/logs do not."""
import re
import requests

from models.remediation import VerificationResult
from remediation.state import RemediationError

DEMO_URL = "http://127.0.0.1:8001"


class DemoClient:
    def get(self, endpoint):
        if endpoint not in {"/version", "/order", "/health"}:
            raise RemediationError("endpoint_not_allowed")
        try:
            with requests.Session() as session:
                # This fixed loopback target must not inherit proxies or netrc credentials.
                session.trust_env = False
                response = session.get(DEMO_URL + endpoint, timeout=2, allow_redirects=False)
                # Never retain error body, response headers, or raw output.
                body = response.json() if endpoint == "/version" and response.status_code == 200 else None
                return response.status_code, body
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
            "runtime_flags": {"simulate_code_regression": body["runtime_flags"]["simulate_code_regression"]}}


class DemoVerifier:
    def __init__(self, client=None):
        self.client = client or DemoClient()

    def verify(self, proposal):
        version_code, body = self.client.get("/version")
        version_ok = (version_code == 200 and isinstance(body, dict)
            and body.get("service") == "order-service"
            and body.get("commit_sha") == proposal.expected_commit_sha
            and isinstance(body.get("runtime_flags"), dict)
            and body["runtime_flags"].get("simulate_code_regression") is False)
        order_code, _ = self.client.get("/order")
        health_code, _ = self.client.get("/health")
        checks = {
            "version": {"success": bool(version_ok), "http_status": version_code,
                        "same_commit": isinstance(body, dict) and body.get("commit_sha") == proposal.expected_commit_sha,
                        "simulate_code_regression": (body.get("runtime_flags") or {}).get("simulate_code_regression")
                            if isinstance(body, dict) and isinstance(body.get("runtime_flags"), dict) else None},
            "order": {"success": order_code == 200, "http_status": order_code},
            "health": {"success": health_code == 200, "http_status": health_code},
        }
        success = all(item["success"] for item in checks.values())
        return VerificationResult(proposal_id=proposal.proposal_id, success=success, checks=checks,
            summary="运行开关关闭，同版本 /order 和 /health 均返回 200。" if success else "立即恢复检查失败，需要人工处理。")
