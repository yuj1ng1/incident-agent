"""Bounded, redacted checkpoints and conservative Responses chain validation."""
import json
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
