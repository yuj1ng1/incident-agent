"""Bounded, redacted checkpoints and conservative Responses chain validation."""
import json
import hashlib
import os
import re

from investigation.state import RuntimeErrorBase

MAX_CHECKPOINT_BYTES = 4_194_304
MAX_TEXT = 65536
SECRET_FIELDS = re.compile(r"password|passwd|api[_-]?key|token|authorization|secret", re.I)


def protect(value, source_code=False):
    if isinstance(value, dict):
        source_result = value.get("status") == "ok" and bool(value.get("repository"))
        return {
            str(key): "[redacted]" if SECRET_FIELDS.search(str(key)) else protect(
                item, source_code=source_result and key in {"content", "patch"}
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [protect(item) for item in value]
    if isinstance(value, str):
        # Also scrub configured credential values embedded in exception/log text.
        for key, secret in os.environ.items():
            if SECRET_FIELDS.search(key) and len(secret) >= 6:
                value = value.replace(secret, "[redacted]")
        value = re.sub(r"(?i)(Bearer\s+)[^\s\"']+", r"\1[redacted]", value)
        if source_code:
            # Variable references are code, not credential values. Preserve syntax
            # so deterministic AST gates can still inspect the actual fault path.
            value = re.sub(
                r"(?i)((?:password|api_key|token)\s*=\s*)([\"'])([^\"'\n]+)(\2)",
                r"\1\2[redacted]\4", value,
            )
        else:
            value = re.sub(
                r"(?i)((?:password|api_key|token)\s*[=:]\s*)[^\s,;\"']+",
                r"\1[redacted]", value,
            )
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + "\n[truncated]"
    return value


def encode(value):
    encoded = json.dumps(protect(value), ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_CHECKPOINT_BYTES:
        raise RuntimeErrorBase("checkpoint_size_limit")
    return encoded


def projection_digest(session, payload):
    """Watermark of business content, excluding lease/phase/history bookkeeping."""
    from investigation.models import SessionStatus as S
    final = session.status in {S.COMPLETED, S.EXHAUSTED, S.FAILED}
    content = {
        "session": {key: getattr(session, key) for key in (
            "session_id", "incident_id", "alert_fingerprint", "service", "status",
            "budget_group_id", "attempt_count", "last_error_category",
        )},
        "observations": [
            {key: record[key] for key in ("observation_id", "name", "result", "collected_at")}
            for record in payload.get("records", [])
            if record.get("success") and record.get("result", {}).get("status") == "ok"
        ],
        "messages": payload.get("messages", []),
        "reopen_alert": payload.get("reopen_alert"),
        "proposal_evidence_gap": payload.get("proposal_evidence_gap"),
    }
    if final:
        content["final"] = {
            "result": payload.get("result"), "trace": payload.get("trace", []),
            "investigated_value": payload.get("investigated_value"),
            "investigated_state": payload.get("investigated_state"),
            "completed_at": session.completed_at.isoformat() if session.completed_at else None,
            "model_requests": session.total_model_requests,
            "tool_executions": session.total_tool_executions,
        }
    encoded = json.dumps(protect(content), sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def valid_chain(history):
    """A is opt-in after provider verification; missing outputs never synthesized."""
    pending = set()
    seen = set()
    if not isinstance(history, list) or not history:
        return False
    for item in history:
        if not isinstance(item, dict):
            return False
        kind = item.get("type")
        if kind == "function_call":
            call_id = item.get("call_id")
            if not call_id or call_id in seen or not item.get("name"):
                return False
            try:
                if not isinstance(json.loads(item["arguments"]), dict):
                    return False
            except (ValueError, KeyError, TypeError):
                return False
            pending.add(call_id)
            seen.add(call_id)
        elif kind == "function_call_output":
            call_id = item.get("call_id")
            if call_id not in pending or "output" not in item:
                return False
            pending.remove(call_id)
        elif kind in {"message", "reasoning"} or item.get("role") in {"user", "assistant", "system", "developer"}:
            if pending:
                return False
        else:
            return False
    return not pending


def restore_context(history, rebuilt, provider_verified=False):
    if provider_verified and valid_chain(history):
        return history, "complete_chain"
    return [{
        "role": "user",
        "content": rebuilt,
    }], "evidence_rebuild"
