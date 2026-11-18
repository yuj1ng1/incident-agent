"""Official DeepSeek V4.1 Responses API pre-send input token counter.

Implements request conversion, V4.1 prompt rendering, and plain-vs-special
token encoding matching ``deepseek-recipe`` (0.2.0) and
``deepseek-ai/DeepSeek-V4.1-Flash-1017`` without network downloads.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from tokenizers import Tokenizer

ENCODER_VERSION = "deepseek-v4.1-responses-v1"
UPSTREAM_ENCODING_SOURCE = "deepseek-recipe@0.2.0"
UPSTREAM_MODEL_REVISION = (
    "deepseek-ai/DeepSeek-V4.1-Flash-1017@"
    "08c8657f02291e11b6f6b63e0e16014f8e748518"
)
SUPPORTED_MODEL_VERSIONS: dict[str, str] = {
    "deepseek-flash": "DeepSeek-V4.1-Flash-1017",
}
DEFAULT_TOKENIZER_PATH = (
    Path(__file__).resolve().parent / "tokenizers" / "v41" / "tokenizer.json"
)
DEFAULT_TOKENIZER_SHA256 = (
    "c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b"
)

BOS_TOKEN = "<｜begin▁of▁sentence｜>"
EOS_TOKEN = "<｜end▁of▁sentence｜>"
THINKING_START_TOKEN = "<think>"
THINKING_END_TOKEN = "</think>"
SYSTEM_TOKEN = "<｜System｜>"
USER_TOKEN = "<｜User｜>"
ASSISTANT_TOKEN = "<｜Assistant｜>"
LATEST_REMINDER_TOKEN = "<｜latest_reminder｜>"
DSML_TOKEN = "｜DSML｜"

ASSISTANT_SPECIAL_TOKENS = (
    "<｜ref｜>",
    "<｜/ref｜>",
    "<｜box｜>",
    "<｜/box｜>",
    "<｜point｜>",
    "<｜/point｜>",
    "<｜polygon｜>",
    "<｜/polygon｜>",
)
REQUIRED_SPECIAL_TOKENS = (
    BOS_TOKEN,
    EOS_TOKEN,
    THINKING_START_TOKEN,
    THINKING_END_TOKEN,
    SYSTEM_TOKEN,
    USER_TOKEN,
    ASSISTANT_TOKEN,
    LATEST_REMINDER_TOKEN,
    DSML_TOKEN,
    *ASSISTANT_SPECIAL_TOKENS,
)
_ASSISTANT_SPECIAL_RE = re.compile(
    "|".join(re.escape(token) for token in ASSISTANT_SPECIAL_TOKENS)
)
_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
_EFFORT_BUDGET_MAP = {
    "minimal": 50,
    "low": 50,
    "medium": 75,
    "high": 75,
    "xhigh": 75,
    "ultra": 100,
    "max": 100,
}
_ALLOWED_TOP_LEVEL_KEYS = frozenset({
    "model",
    "input",
    "instructions",
    "tools",
    "max_output_tokens",
    "reasoning",
    "tool_choice",
    "text",
    "temperature",
    "top_p",
    "stream",
})


class TokenCountError(ValueError):
    """Raised when tokenizer integrity fails or request shape is unsupported."""


@dataclass(frozen=True)
class TokenizerBundle:
    """Immutable loaded plain tokenizer and verified added vocabulary."""

    plain_tokenizer: Tokenizer
    added_vocabulary: dict[str, int]
    sha256: str
    path: Path


@lru_cache(maxsize=4)
def _load_bundle_cached(
    resolved_path: str,
    expected_sha256: str,
    mtime_ns: int,
    size: int,
) -> TokenizerBundle:
    del mtime_ns, size
    path = Path(resolved_path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise TokenCountError("tokenizer_file_unreadable") from exc

    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise TokenCountError("tokenizer_hash_mismatch")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TokenCountError("tokenizer_corrupt") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("added_tokens"), list):
        raise TokenCountError("tokenizer_corrupt")

    added_vocabulary: dict[str, int] = {}
    for entry in payload["added_tokens"]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("content"), str)
            or type(entry.get("id")) is not int
        ):
            raise TokenCountError("tokenizer_corrupt")
        added_vocabulary[entry["content"]] = entry["id"]

    missing = [token for token in REQUIRED_SPECIAL_TOKENS if token not in added_vocabulary]
    if missing:
        raise TokenCountError("tokenizer_missing_special_tokens")

    plain_payload = dict(payload)
    plain_payload["added_tokens"] = []
    try:
        plain_tokenizer = Tokenizer.from_str(
            json.dumps(plain_payload, ensure_ascii=False)
        )
    except Exception as exc:
        raise TokenCountError("tokenizer_corrupt") from exc

    if plain_tokenizer.get_added_tokens_decoder():
        raise TokenCountError("tokenizer_corrupt")

    return TokenizerBundle(
        plain_tokenizer=plain_tokenizer,
        added_vocabulary=added_vocabulary,
        sha256=actual_sha256,
        path=path,
    )


def load_v41_tokenizer(
    tokenizer_path: str | Path = DEFAULT_TOKENIZER_PATH,
    expected_sha256: str = DEFAULT_TOKENIZER_SHA256,
) -> TokenizerBundle:
    """Load and SHA-256-verify the local V4.1 tokenizer without network access."""
    if not isinstance(expected_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}",
        expected_sha256,
    ):
        raise TokenCountError("invalid_expected_tokenizer_sha256")
    path = Path(tokenizer_path)
    if path.is_symlink():
        raise TokenCountError("tokenizer_symlink_forbidden")
    if not path.is_file():
        raise TokenCountError("tokenizer_file_missing")
    stat = path.stat()
    return _load_bundle_cached(
        str(path.resolve()),
        expected_sha256,
        stat.st_mtime_ns,
        stat.st_size,
    )


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TokenCountError("invalid_json_value") from exc


def _resolve_thinking(reasoning: Any) -> tuple[bool, int | None]:
    if reasoning is None:
        return True, 75
    if not isinstance(reasoning, dict):
        raise TokenCountError("unsupported_reasoning_config")
    if set(reasoning) - {"effort", "summary", "generate_summary"}:
        raise TokenCountError("unsupported_reasoning_config")
    effort = reasoning.get("effort")
    if effort is None:
        return True, 75
    if effort == "none":
        return False, None
    if isinstance(effort, str) and effort in _EFFORT_BUDGET_MAP:
        return True, _EFFORT_BUDGET_MAP[effort]
    raise TokenCountError("unsupported_reasoning_effort")


def _validate_text_config(text: Any) -> None:
    if text is None:
        return
    if not isinstance(text, dict) or set(text) - {"format", "verbosity"}:
        raise TokenCountError("unsupported_text_format")
    fmt = text.get("format")
    if fmt is None:
        return
    if not isinstance(fmt, dict) or fmt.get("type", "text") != "text" or set(fmt) - {"type"}:
        raise TokenCountError("unsupported_text_format")


def _convert_tools(
    tools: Any,
    tool_choice: Any,
    thinking_mode: bool,
) -> tuple[list[dict[str, Any]], str]:
    if tool_choice is None:
        choice_kind = "auto"
        choice_name = None
    elif tool_choice in {"auto", "none", "required"}:
        choice_kind = tool_choice
        choice_name = None
    elif (
        isinstance(tool_choice, dict)
        and set(tool_choice) == {"type", "name"}
        and tool_choice.get("type") == "function"
        and isinstance(tool_choice.get("name"), str)
    ):
        choice_kind = "named"
        choice_name = tool_choice["name"]
    else:
        raise TokenCountError("unsupported_tool_choice")

    if tools is None:
        raw_tools: list[Any] = []
    elif isinstance(tools, list):
        raw_tools = tools
    else:
        raise TokenCountError("invalid_tools_list")

    definitions: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for tool in raw_tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise TokenCountError("unsupported_tool_type")
        if set(tool) - {"type", "name", "description", "parameters", "strict"}:
            raise TokenCountError("unsupported_tool_field")
        name = tool.get("name")
        if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
            raise TokenCountError("invalid_tool_name")
        description = tool.get("description")
        if description is not None and not isinstance(description, str):
            raise TokenCountError("invalid_tool_description")
        if tool.get("strict") not in {None, False}:
            raise TokenCountError("unsupported_strict_tool_schema")
        definitions.append({
            "name": name,
            "description": description or "",
            "parameters": tool.get("parameters"),
        })

    if choice_kind == "none":
        return [], "none"
    if not definitions:
        return [], "auto"

    for definition in definitions:
        name = definition["name"]
        if name in seen_names:
            raise TokenCountError("duplicate_tool_names")
        seen_names.add(name)
        parameters = definition["parameters"]
        if parameters is not None:
            if not isinstance(parameters, dict) or parameters.get("type") != "object":
                raise TokenCountError("invalid_tool_parameters_schema")
            _json_dumps(parameters)
        else:
            definition["parameters"] = {}

    if choice_kind == "named":
        if choice_name not in seen_names:
            raise TokenCountError("unknown_named_tool")
        definitions = [d for d in definitions if d["name"] == choice_name]
        resolved_choice = "required"
    elif choice_kind == "required":
        resolved_choice = "required"
    else:
        resolved_choice = "auto"

    if resolved_choice == "required" and thinking_mode:
        raise TokenCountError("thinking_mode_tool_choice_unsupported")

    return definitions, resolved_choice


def _extract_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise TokenCountError("invalid_message_content")
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            raise TokenCountError("invalid_content_block")
        block_type = block.get("type")
        if block_type not in {"input_text", "output_text"}:
            raise TokenCountError("unsupported_content_block")
        text = block.get("text")
        if not isinstance(text, str):
            raise TokenCountError("invalid_content_block")
        parts.append(text)
    return "\n\n".join(parts)


def _extract_tool_output_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if not isinstance(output, list):
        raise TokenCountError("invalid_tool_output")
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "input_text":
            raise TokenCountError("unsupported_content_block")
        text = item.get("text")
        if not isinstance(text, str):
            raise TokenCountError("invalid_tool_output")
        parts.append(text)
    return "\n\n".join(parts)


def _append_tool_group(
    group: dict[str, Any] | None,
    messages: list[dict[str, Any]],
) -> None:
    if group is None or not group["calls"]:
        return
    calls = sorted(group["calls"], key=lambda call: call["call_id"])
    if not messages or messages[-1]["role"] != "assistant":
        messages.append({
            "role": "assistant",
            "content": "",
            "reasoning_content": None,
            "tool_calls": [],
        })
    messages[-1]["tool_calls"].extend(calls)
    for call in calls:
        messages.append({
            "role": "tool",
            "content": group["outputs"][call["call_id"]],
            "tool_call_id": call["call_id"],
        })


def _convert_input_items(
    items: list[Any],
    messages: list[dict[str, Any]],
) -> None:
    if not items:
        raise TokenCountError("empty_input_not_allowed")

    pending_call_ids: set[str] = set()
    resolved_call_ids: set[str] = set()
    pending_group: dict[str, Any] | None = None

    for item in items:
        if not isinstance(item, dict):
            raise TokenCountError("invalid_input_item")
        item_type = item.get("type")
        if item_type == "message" or (item_type is None and "role" in item):
            if pending_call_ids:
                raise TokenCountError("unpaired_function_call")
            _append_tool_group(pending_group, messages)
            pending_group = None
            role = item.get("role")
            if role not in {"user", "assistant", "system", "developer"}:
                raise TokenCountError("unsupported_message_role")
            text = _extract_message_text(item.get("content"))
            if role == "developer":
                role = "system"
            if role == "assistant":
                if (
                    messages
                    and messages[-1]["role"] == "assistant"
                    and not messages[-1]["tool_calls"]
                ):
                    if text:
                        if messages[-1]["content"]:
                            messages[-1]["content"] += "\n\n"
                        messages[-1]["content"] += text
                else:
                    messages.append({
                        "role": "assistant",
                        "content": text,
                        "reasoning_content": None,
                        "tool_calls": [],
                    })
            else:
                messages.append({"role": role, "content": text})
        elif item_type == "reasoning":
            if pending_call_ids:
                raise TokenCountError("unpaired_function_call")
            _append_tool_group(pending_group, messages)
            pending_group = None
            raw_content = item.get("content")
            if raw_content is None:
                text = ""
            elif isinstance(raw_content, list):
                parts: list[str] = []
                for entry in raw_content:
                    if (
                        not isinstance(entry, dict)
                        or not isinstance(entry.get("text"), str)
                    ):
                        raise TokenCountError("unsupported_reasoning_content")
                    parts.append(entry["text"])
                text = "\n".join(parts)
            else:
                raise TokenCountError("unsupported_reasoning_content")
            if text:
                if (
                    not messages
                    or messages[-1]["role"] != "assistant"
                    or messages[-1]["content"] != ""
                ):
                    messages.append({
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": None,
                        "tool_calls": [],
                    })
                existing = messages[-1]["reasoning_content"]
                messages[-1]["reasoning_content"] = (
                    f"{existing}\n{text}" if existing is not None else text
                )
        elif item_type == "function_call":
            call_id = item.get("call_id")
            name = item.get("name")
            arguments = item.get("arguments")
            if (
                not isinstance(call_id, str)
                or not call_id
                or not isinstance(name, str)
                or not name
                or not isinstance(arguments, str)
            ):
                raise TokenCountError("invalid_function_call_item")
            if call_id in pending_call_ids or call_id in resolved_call_ids:
                raise TokenCountError("duplicate_call_id")
            pending_call_ids.add(call_id)
            if pending_group is None:
                pending_group = {"calls": [], "outputs": {}}
            pending_group["calls"].append({
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
            })
        elif item_type == "function_call_output":
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise TokenCountError("missing_function_call_output_call_id")
            if call_id not in pending_call_ids:
                raise TokenCountError("unpaired_or_duplicate_function_call_output")
            output_text = _extract_tool_output_text(item.get("output"))
            pending_call_ids.remove(call_id)
            resolved_call_ids.add(call_id)
            assert pending_group is not None
            pending_group["outputs"][call_id] = output_text
            if len(pending_group["outputs"]) >= len(pending_group["calls"]):
                _append_tool_group(pending_group, messages)
                pending_group = None
        else:
            raise TokenCountError("unsupported_input_item")

    if pending_call_ids:
        raise TokenCountError("unpaired_function_call")
    _append_tool_group(pending_group, messages)


def _normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for msg in messages:
        if normalized and normalized[-1]["role"] == "user" and msg["role"] == "user":
            normalized[-1]["content"] += "\n\n" + msg["content"]
        else:
            normalized.append(dict(msg))
    return normalized


class _PromptBuilder:
    """Collects explicit special-token and plain-text fragments, merging plain runs."""

    def __init__(self) -> None:
        self.fragments: list[tuple[bool, str]] = []

    def plain(self, text: str) -> None:
        if not text:
            return
        if self.fragments and not self.fragments[-1][0]:
            self.fragments[-1] = (False, self.fragments[-1][1] + text)
        else:
            self.fragments.append((False, text))

    def special(self, token: str) -> None:
        if not token:
            return
        self.fragments.append((True, token))

    def assistant_content(self, text: str) -> None:
        if not text:
            return
        cursor = 0
        for match in _ASSISTANT_SPECIAL_RE.finditer(text):
            if match.start() > cursor:
                self.plain(text[cursor:match.start()])
            self.special(match.group(0))
            cursor = match.end()
        if cursor < len(text):
            self.plain(text[cursor:])


def _render_tool_prompt(builder: _PromptBuilder, tools: list[dict[str, Any]]) -> None:
    builder.plain("## Tools\n\nYou have access to a set of tools to help answer the user's question. You can invoke tools by writing a \"<")
    builder.special(DSML_TOKEN)
    builder.plain(" calls>\" block like the following:\n\n<")
    builder.special(DSML_TOKEN)
    builder.plain(" calls>\n<")
    builder.special(DSML_TOKEN)
    builder.plain(" invoke name=\"$TOOL_NAME\">\n<")
    builder.special(DSML_TOKEN)
    builder.plain(" parameter name=\"$PARAMETER_NAME\" string=\"true|false\">$PARAMETER_VALUE</")
    builder.special(DSML_TOKEN)
    builder.plain(" parameter>\n...\n</")
    builder.special(DSML_TOKEN)
    builder.plain(" invoke>\n<")
    builder.special(DSML_TOKEN)
    builder.plain(" invoke name=\"$TOOL_NAME2\">\n...\n</")
    builder.special(DSML_TOKEN)
    builder.plain(" invoke>\n</")
    builder.special(DSML_TOKEN)
    builder.plain(" calls>\n\nString parameters should be specified as is and set `string=\"true\"`. For all other types (numbers, booleans, arrays, objects), pass the value in JSON format and set `string=\"false\"`.\n\nIf thinking_mode is enabled (triggered by ")
    builder.special(THINKING_START_TOKEN)
    builder.plain("), you MUST output your complete reasoning inside ")
    builder.special(THINKING_START_TOKEN)
    builder.plain("...")
    builder.special(THINKING_END_TOKEN)
    builder.plain(" BEFORE any tool calls or final response.\n\nOtherwise, output directly after ")
    builder.special(THINKING_END_TOKEN)
    builder.plain(" with tool calls or final response.\n\n### Available Tool Schemas\n\n")
    builder.plain("\n".join(_json_dumps(tool) for tool in tools))
    builder.plain("\n\nYou MUST strictly follow the above defined tool name and parameter schemas to invoke tool calls.\n")


def _parse_tool_arguments(raw_arguments: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_arguments)
    except (ValueError, TypeError):
        return {"arguments": raw_arguments}
    if isinstance(parsed, dict):
        return parsed
    return {"arguments": raw_arguments}


def _render_tool_calls(builder: _PromptBuilder, tool_calls: list[dict[str, Any]]) -> None:
    builder.plain("\n\n<")
    builder.special(DSML_TOKEN)
    builder.plain(" calls>\n")
    for idx, call in enumerate(tool_calls):
        if idx > 0:
            builder.plain("\n")
        builder.plain("<")
        builder.special(DSML_TOKEN)
        builder.plain(f" invoke name=\"{call['name']}\">\n")
        arguments = _parse_tool_arguments(call["arguments"])
        for arg_idx, (key, value) in enumerate(arguments.items()):
            if arg_idx > 0:
                builder.plain("\n")
            if isinstance(value, str):
                is_str, serialized = "true", value
            else:
                is_str, serialized = "false", _json_dumps(value)
            builder.plain("<")
            builder.special(DSML_TOKEN)
            builder.plain(f" parameter name=\"{key}\" string=\"{is_str}\">{serialized}</")
            builder.special(DSML_TOKEN)
            builder.plain(" parameter>")
        builder.plain("\n</")
        builder.special(DSML_TOKEN)
        builder.plain(" invoke>")
    builder.plain("\n</")
    builder.special(DSML_TOKEN)
    builder.plain(" calls>")


class DeepseekV41TokenCounter:
    """Counts exact V4.1 input tokens for a DeepSeek Responses API request."""

    def __init__(
        self,
        *,
        tokenizer_path: str | Path = DEFAULT_TOKENIZER_PATH,
        expected_sha256: str = DEFAULT_TOKENIZER_SHA256,
    ) -> None:
        self._bundle = load_v41_tokenizer(
            tokenizer_path=tokenizer_path,
            expected_sha256=expected_sha256,
        )

    def encoder_metadata(self) -> dict[str, Any]:
        """Return pinned encoder, model mapping, and tokenizer verification metadata."""
        return {
            "encoder_version": ENCODER_VERSION,
            "upstream_encoding_source": UPSTREAM_ENCODING_SOURCE,
            "upstream_model_revision": UPSTREAM_MODEL_REVISION,
            "tokenizer_path": str(self._bundle.path),
            "tokenizer_sha256": self._bundle.sha256,
            "model_versions": dict(SUPPORTED_MODEL_VERSIONS),
        }

    def render_fragments(self, body: dict[str, Any]) -> list[tuple[bool, str]]:
        """Validate and render a Responses API request into plain/special fragments."""
        if not isinstance(body, dict):
            raise TokenCountError("invalid_request_body")
        unknown_keys = set(body) - _ALLOWED_TOP_LEVEL_KEYS
        if unknown_keys:
            raise TokenCountError("unsupported_request_field")

        model = body.get("model")
        if not isinstance(model, str) or model not in SUPPORTED_MODEL_VERSIONS:
            raise TokenCountError("unsupported_model")

        max_output = body.get("max_output_tokens")
        if max_output is not None and (type(max_output) is not int or max_output <= 0):
            raise TokenCountError("invalid_max_output_tokens")

        instructions = body.get("instructions")
        if instructions is not None and not isinstance(instructions, str):
            raise TokenCountError("invalid_instructions")

        _validate_text_config(body.get("text"))
        thinking_mode, effort_budget = _resolve_thinking(body.get("reasoning"))
        tools, tool_choice = _convert_tools(
            body.get("tools"),
            body.get("tool_choice"),
            thinking_mode,
        )

        messages: list[dict[str, Any]] = []
        if isinstance(instructions, str) and instructions:
            messages.append({"role": "system", "content": instructions})

        raw_input = body.get("input")
        if isinstance(raw_input, str):
            messages.append({"role": "user", "content": raw_input})
        elif isinstance(raw_input, list):
            _convert_input_items(raw_input, messages)
        elif raw_input is None:
            if not messages:
                raise TokenCountError("input_or_instructions_required")
        else:
            raise TokenCountError("invalid_input_type")

        for msg in messages:
            if msg["role"] == "assistant":
                if not thinking_mode:
                    msg["reasoning_content"] = None
                elif msg["reasoning_content"] is None:
                    msg["reasoning_content"] = ""

        normalized = _normalize_messages(messages)
        renders_tool_prompt = tool_choice != "none" and bool(tools)
        prepend_empty_system = renders_tool_prompt and (
            not normalized or normalized[0]["role"] != "system"
        )
        if prepend_empty_system:
            normalized.insert(0, {"role": "system", "content": ""})

        drop_thinking = not renders_tool_prompt
        builder = _PromptBuilder()
        builder.special(BOS_TOKEN)

        for idx, msg in enumerate(normalized):
            role = msg["role"]
            effort_prompt = (
                f"Reasoning Effort: {effort_budget} "
                "(range 1-100, the higher the value, the more thorough the reasoning)\n\n"
                if idx == 0 and thinking_mode
                else ""
            )
            if idx == 0 and (effort_prompt or role == "system"):
                builder.special(SYSTEM_TOKEN)
            if effort_prompt:
                builder.plain(effort_prompt)

            if role == "system":
                if idx > 0:
                    builder.special(SYSTEM_TOKEN)
                builder.plain(msg["content"])
                if idx == 0 and renders_tool_prompt:
                    builder.plain("\n\n")
                    _render_tool_prompt(builder, tools)
            elif role == "user":
                prev_role = normalized[idx - 1]["role"] if idx > 0 else None
                if prev_role in {"user", "tool"}:
                    builder.plain("\n\n")
                else:
                    builder.special(USER_TOKEN)
                builder.plain(msg["content"])
            elif role == "tool":
                prev_role = normalized[idx - 1]["role"] if idx > 0 else None
                if prev_role in {"user", "tool"}:
                    builder.plain("\n\n")
                else:
                    builder.special(USER_TOKEN)
                builder.plain(f"<tool_result>{msg['content']}</tool_result>")
            elif role == "assistant":
                renders_reasoning = thinking_mode and idx > 0 and not drop_thinking
                reasoning_text = msg.get("reasoning_content") or ""
                has_reasoning_block = renders_reasoning
                builder.special(ASSISTANT_TOKEN)
                builder.special(
                    THINKING_START_TOKEN if has_reasoning_block else THINKING_END_TOKEN
                )
                if renders_reasoning:
                    builder.assistant_content(reasoning_text)
                    builder.special(THINKING_END_TOKEN)
                builder.assistant_content(msg["content"])
                if msg.get("tool_calls"):
                    _render_tool_calls(builder, msg["tool_calls"])
                builder.special(EOS_TOKEN)

        builder.special(ASSISTANT_TOKEN)
        builder.special(THINKING_START_TOKEN if thinking_mode else THINKING_END_TOKEN)
        if tool_choice == "required" and renders_tool_prompt:
            builder.plain("\n\n<")
            builder.special(DSML_TOKEN)
            builder.plain(" calls>\n")

        return builder.fragments

    def encode_ids(self, body: dict[str, Any]) -> list[int]:
        """Encode the full Responses API request into exact V4.1 token IDs."""
        fragments = self.render_fragments(body)
        plain_texts = [text for is_special, text in fragments if not is_special]
        encoded_plain = (
            self._bundle.plain_tokenizer.encode_batch(
                plain_texts,
                add_special_tokens=False,
            )
            if plain_texts
            else []
        )
        plain_iter = iter(encoded_plain)
        ids: list[int] = []
        for is_special, text in fragments:
            if is_special:
                token_id = self._bundle.added_vocabulary.get(text)
                if token_id is None:
                    raise TokenCountError("tokenizer_missing_special_tokens")
                ids.append(token_id)
            else:
                ids.extend(next(plain_iter).ids)
        return ids

    def count_tokens(self, body: dict[str, Any]) -> int:
        """Count exact V4.1 input tokens for the complete Responses request."""
        fragments = self.render_fragments(body)
        plain_texts = [text for is_special, text in fragments if not is_special]
        encoded_plain = (
            self._bundle.plain_tokenizer.encode_batch(
                plain_texts,
                add_special_tokens=False,
            )
            if plain_texts
            else []
        )
        plain_iter = iter(encoded_plain)
        total = 0
        for is_special, text in fragments:
            if is_special:
                if text not in self._bundle.added_vocabulary:
                    raise TokenCountError("tokenizer_missing_special_tokens")
                total += 1
            else:
                total += len(next(plain_iter).ids)
        return total

    def __call__(self, body: dict[str, Any]) -> int:
        return self.count_tokens(body)


def build_deepseek_input_token_counter(
    *,
    tokenizer_path: str | Path = DEFAULT_TOKENIZER_PATH,
    expected_sha256: str = DEFAULT_TOKENIZER_SHA256,
) -> DeepseekV41TokenCounter:
    """Build and verify the local V4.1 input token counter for live mode."""
    return DeepseekV41TokenCounter(
        tokenizer_path=tokenizer_path,
        expected_sha256=expected_sha256,
    )
