"""Explicit, bounded collection from the verified local P6.1 test services.

Run in a dedicated process only after verifying ownership of the local services.
Imports perform no I/O. This entry point does not investigate, infer a root cause,
contact PostgreSQL, or test a business route.
"""
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch
from urllib.parse import urlsplit

import requests


ALLOWED_PATHS = {
    ("localhost", 8001): {"/health", "/version", "/metrics"},
    ("localhost", 9090): {
        "/-/ready", "/api/v1/targets", "/api/v1/query", "/api/v1/alerts",
    },
    ("localhost", 3100): {"/ready", "/loki/api/v1/query_range"},
}
MAX_RESPONSE_BYTES = 262144
REQUEST_TIMEOUT = (2, 5)
TOTAL_REQUEST_SECONDS = 8


class ReadOnlyBoundaryViolation(BaseException):
    """Do not let a tool's ordinary error handler hide forbidden I/O."""


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _write_json(path, value):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)


@contextmanager
def _capture_http(root):
    """Temporarily constrain existing requests-based tools and retain originals."""
    original_request = requests.sessions.Session.request
    records = []

    def request(session, method, url, **kwargs):
        identifier = f"http-{len(records) + 1:03d}"
        record = {
            "id": identifier,
            "method": method,
            "url": url,
            "query_parameters": kwargs.get("params"),
            "query_started_at": _utc_now(),
            "query_finished_at": None,
            "request_executed": False,
            "http_status": None,
            "body_complete": False,
        }
        records.append(record)
        body = bytearray()
        response = None
        try:
            target = urlsplit(url)
            allowed = ALLOWED_PATHS.get((target.hostname, target.port), set())
            if (
                method.upper() != "GET"
                or target.scheme != "http"
                or target.hostname != "localhost"
                or target.netloc != f"localhost:{target.port}"
                or target.path not in allowed
                or target.query
                or target.fragment
            ):
                raise ReadOnlyBoundaryViolation("p61_http_target_or_method_forbidden")
            if any(kwargs.get(key) for key in (
                "data", "json", "files", "auth", "cookies", "hooks", "proxies",
            )):
                raise ReadOnlyBoundaryViolation("p61_http_payload_or_auth_forbidden")
            # Ignore ambient proxies, netrc authentication, cookies and headers.
            session.trust_env = False
            session.auth = None
            session.cookies.clear()
            session.proxies.clear()
            session.headers.clear()
            session.hooks = {"response": []}
            kwargs.update(
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
                stream=True,
                proxies={},
                headers={"Accept-Encoding": "identity"},
            )
            deadline = time.monotonic() + TOTAL_REQUEST_SECONDS
            record["request_executed"] = True
            response = original_request(session, "GET", url, **kwargs)
            record["http_status"] = response.status_code
            record["response_url"] = response.url
            record["response_headers"] = {
                key: response.headers[key]
                for key in (
                    "Content-Type", "Content-Length", "Content-Encoding", "Location",
                )
                if key in response.headers
            }
            encoding = response.headers.get("Content-Encoding", "identity")
            if encoding.lower() != "identity":
                raise ValueError("p61_compressed_response_refused")
            length = response.headers.get("Content-Length")
            if length is not None and (
                not length.isdecimal() or int(length) > MAX_RESPONSE_BYTES
            ):
                raise ValueError("p61_response_size_limit_or_invalid_length")
            # Single-byte reads make the total deadline effective for trickles.
            for chunk in response.iter_content(chunk_size=1):
                if time.monotonic() > deadline:
                    raise requests.Timeout("p61_total_request_deadline")
                if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ValueError("p61_response_size_limit")
                body.extend(chunk)
            if time.monotonic() > deadline:
                raise requests.Timeout("p61_total_request_deadline")
            record["body_complete"] = True
            response._content = bytes(body)
            response._content_consumed = True
            if 300 <= response.status_code < 400:
                raise requests.HTTPError("p61_redirect_refused", response=response)
            return response
        except (ReadOnlyBoundaryViolation, requests.RequestException, ValueError) as exc:
            record["error_type"] = type(exc).__name__
            # Exception text can contain credentials; the type and raw status suffice.
            raise
        finally:
            if response is not None:
                response.close()
                raw_name = identifier + ".body"
                with (root / raw_name).open("xb") as handle:
                    handle.write(body)
                record["raw_body_file"] = raw_name
                record["raw_body_sha256"] = hashlib.sha256(body).hexdigest()
                record["captured_bytes"] = len(body)
            record["query_finished_at"] = _utc_now()
            _write_json(root / (identifier + ".json"), record)

    with patch("requests.sessions.Session.request", request):
        yield records


def run_collection(output_dir):
    """Collect only from local endpoints already verified as isolated test services."""
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=False)
    started = _utc_now()
    observations = {}
    supplemental = []

    def forbidden(*args, **kwargs):
        raise ReadOnlyBoundaryViolation("p61_database_process_or_thread_forbidden")

    with ExitStack() as stack:
        stack.enter_context(patch("psycopg.connect", forbidden))
        stack.enter_context(patch("subprocess.Popen", forbidden))
        stack.enter_context(patch("threading.Thread.start", forbidden))
        transport = stack.enter_context(_capture_http(root))
        from tools.health import check_service_health
        from tools.logs import query_logs
        from tools.metrics import check_scrape_target, query_metrics
        from tools.version import get_running_version

        probes = []
        for url in (
            "http://localhost:8001/metrics",
            "http://localhost:9090/-/ready",
            "http://localhost:9090/api/v1/alerts",
            "http://localhost:3100/ready",
        ):
            try:
                response = requests.get(url)
                response.raise_for_status()
                status = "ok"
            except (requests.RequestException, ValueError):
                status = "no_data"
            probes.append({"url": url, "status": status, "request_id": transport[-1]["id"]})

        def collect(name, function, arguments):
            first = len(transport)
            began = _utc_now()
            result = function(**arguments)
            requests_made = transport[first:]
            return {
                "tool": name,
                "arguments": arguments,
                "result": result,
                "query_started_at": began,
                "collected_at": _utc_now(),
                "source": requests_made[0]["url"] if requests_made else None,
                "request_ids": [row["id"] for row in requests_made],
                "provider_request_executed": any(
                    row["request_executed"] for row in requests_made
                ),
                "provenance": "real_tool_collection",
            }

        for name, function, arguments in (
            ("query_metrics", query_metrics, {
                "service": "order-service", "metric": "http_5xx_rate",
            }),
            ("query_logs", query_logs, {"service": "order-service"}),
            ("check_service_health", check_service_health, {"service": "order-service"}),
            ("get_running_version", get_running_version, {"service": "order-service"}),
        ):
            observations[name] = collect(name, function, arguments)
        for service, metric in (
            ("order-service", "service_scrape_up"),
            ("order-service", "http_request_rate"),
            ("order-db", "postgres_up"),
            ("order-db", "exporter_scrape_up"),
        ):
            supplemental.append(collect("query_metrics", query_metrics, {
                "service": service, "metric": metric,
            }))
        for service in ("order-service", "order-db"):
            supplemental.append(collect("check_scrape_target", check_scrape_target, {
                "service": service,
            }))

    _write_json(root / "observations.json", observations)
    _write_json(root / "supplemental_observations.json", supplemental)
    report = {
        "started_at": started,
        "finished_at": _utc_now(),
        "probes": probes,
        "http_requests": transport,
        "observations_file": "observations.json",
        "supplemental_observations_file": "supplemental_observations.json",
        "real_model_sdk_requests": 0,
        "database_connections": 0,
        "business_route_requests": 0,
        "business_state": "unknown",
        "business_recovery_verified": False,
        "alert_window": None,
        "log_query_scope": "recent_not_alert_window",
        "request_executed_semantics": (
            "Entered the local HTTP client; does not prove server receipt."
        ),
        "limitations": [
            "Readiness and health responses do not verify business health.",
            "PostgreSQL is observed only through Prometheus; no direct DB connection.",
            "An empty log query does not prove that no logs exist.",
            "Metric evaluation timestamps do not prove shared scrape timestamps.",
            "No model decision, business route, fault injection or remediation runs here.",
        ],
    }
    _write_json(root / "collection_report.json", report)
    _write_json(root / "sha256.json", {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.iterdir())
    })
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()
    report = run_collection(arguments.output_dir)
    print(json.dumps({
        "report": str(arguments.output_dir.resolve() / "collection_report.json"),
        "http_attempts": len(report["http_requests"]),
        "real_model_sdk_requests": report["real_model_sdk_requests"],
        "business_state": report["business_state"],
    }))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main()
