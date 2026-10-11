"""Deterministic business probes; raw bodies, headers and errors never persist."""
import hashlib
import json
import math
import time

import requests

from models.recovery import (
    RecoveryCheck, RecoveryCondition, RecoveryPolicy, RecoveryReceipt, RecoverySample, utc_now,
)
from services.profiles import HTTPRecovery
from services.registry import registry as default_registry

MAX_RESPONSE_BYTES = 16384


class _RedirectRefused(requests.RequestException):
    pass


def _reject_redirect(response, *args, **kwargs):
    # Requests prepares Response.next even with allow_redirects=False and drains
    # that body first. Reject at the response hook, before any library body read.
    if 300 <= response.status_code < 400:
        response.close()
        raise _RedirectRefused(response=response)
    return response


def recovery_policy(settings):
    settings = HTTPRecovery.model_validate(settings.model_dump())
    content = json.dumps(settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    checks = [RecoveryCondition(kind="http_status")]
    checks.extend(RecoveryCondition(kind="json_equals", field=key) for key in sorted(settings.json_equals))
    checks.extend(RecoveryCondition(kind="nonempty_object", field=key) for key in sorted(settings.required_json_objects))
    return RecoveryPolicy(
        target_id=settings.target_id, config_digest=hashlib.sha256(content.encode()).hexdigest(),
        expected_status=settings.expected_status, required_checks=checks,
        successes_required=settings.successes_required, max_attempts=settings.max_attempts,
        retry_seconds=settings.retry_seconds, evidence_max_age_seconds=settings.evidence_max_age_seconds,
        lease_seconds=math.ceil(settings.successes_required * settings.timeout_seconds * 2) + 10,
    )


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_field")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("nonfinite_json_number")


class RecoveryVerifier:
    def __init__(self, registry=default_registry, *, session_factory=requests.Session, clock=utc_now):
        self.registry = registry
        self.session_factory = session_factory
        self.clock = clock

    def settings(self, service):
        configured = self.registry.require_capability(service, "recovery")
        # Also validate when a Registry was built using a supplied Profile object.
        return HTTPRecovery.model_validate(configured.model_dump())

    def verify(self, incident):
        settings = self.settings(incident.service)
        state = incident.recovery_verification
        if state is None or state.status != "running" or state.policy != recovery_policy(settings):
            raise ValueError("registered_recovery_claim_required")
        attempt = state.attempts[-1]
        samples = []
        for _ in range(settings.successes_required):
            sample = self._sample(settings)
            samples.append(sample)
            if sample.outcome != "success":
                break
        last = samples[-1]
        return RecoveryReceipt(
            incident_id=incident.incident_id, service=incident.service, target_id=settings.target_id,
            config_digest=state.policy.config_digest, claim_token=attempt.token,
            cleared_at=state.cleared_at, started_at=attempt.started_at, completed_at=self.clock(),
            outcome=last.outcome, reason=last.reason, samples=samples,
        )

    def _sample(self, settings):
        http_status, checks = None, []

        def observed(outcome, reason):
            return RecoverySample(collected_at=self.clock(), http_status=http_status,
                                  checks=checks, outcome=outcome, reason=reason)

        # The target is assembled exclusively from a validated Registry entry.
        url = settings.base_url + settings.endpoint
        deadline = time.monotonic() + settings.timeout_seconds
        try:
            with self.session_factory() as session:
                session.trust_env = False  # no proxy, netrc credentials or cookies from the environment
                with session.get(
                    url, timeout=(settings.timeout_seconds, settings.timeout_seconds),
                    allow_redirects=False, stream=True,
                    headers={"Accept": "application/json", "Accept-Encoding": "identity", "Cache-Control": "no-cache"},
                    hooks={"response": _reject_redirect},
                ) as response:
                    http_status = response.status_code
                    checks.append(RecoveryCheck(kind="http_status", matched=http_status == settings.expected_status))
                    if 300 <= http_status < 400:
                        return observed("insufficient", "redirect_refused")
                    if http_status != settings.expected_status:
                        return observed("failed", "http_status_mismatch")
                    if response.url != url or response.headers.get("Content-Type", "").split(";")[0].lower() != "application/json":
                        return observed("insufficient", "invalid_response")
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        return observed("insufficient", "content_encoding_refused")
                    length = response.headers.get("Content-Length")
                    if length is not None and (not length.isdecimal() or int(length) > MAX_RESPONSE_BYTES):
                        return observed("insufficient", "response_too_large")
                    body_bytes = bytearray()
                    # Single bytes keep a slowly trickling body from resetting an
                    # indefinitely buffered read. Reject compression to bound memory.
                    for chunk in response.iter_content(chunk_size=1):
                        if time.monotonic() > deadline:
                            return observed("insufficient", "timeout")
                        body_bytes.extend(chunk)
                        if len(body_bytes) > MAX_RESPONSE_BYTES:
                            return observed("insufficient", "response_too_large")
                    if time.monotonic() > deadline:
                        return observed("insufficient", "timeout")
                    body = json.loads(body_bytes, object_pairs_hook=_json_object, parse_constant=_invalid_constant)
                    if not isinstance(body, dict):
                        return observed("insufficient", "invalid_response")
                    for key, expected in sorted(settings.json_equals.items()):
                        actual = body.get(key)
                        checks.append(RecoveryCheck(
                            kind="json_equals", field=key,
                            matched=type(actual) is type(expected) and actual == expected,
                        ))
                    for key in sorted(settings.required_json_objects):
                        actual = body.get(key)
                        checks.append(RecoveryCheck(kind="nonempty_object", field=key,
                                                    matched=isinstance(actual, dict) and bool(actual)))
                    if not all(item.matched for item in checks):
                        return observed("failed", "business_condition_mismatch")
                    return observed("success", "conditions_met")
        except _RedirectRefused as exc:
            http_status = exc.response.status_code
            checks = [RecoveryCheck(kind="http_status", matched=False)]
            return observed("insufficient", "redirect_refused")
        except requests.Timeout:
            return observed("insufficient", "timeout")
        except requests.RequestException:
            return observed("insufficient", "connection_failed")
        except (ValueError, UnicodeError, RecursionError):
            return observed("insufficient", "invalid_response")
