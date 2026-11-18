"""Single SDK request with explicit generation and pre-send input limits."""
from copy import deepcopy


class RequestConstraintError(RuntimeError):
    """A request has no verified input bound, or exceeds its configured bound."""


def enforce_input_token_limit(
    body,
    *,
    input_token_counter,
    max_input_tokens=65536,
):
    """Validate and enforce the upper bound on a complete outgoing request body."""
    if type(max_input_tokens) is not int or max_input_tokens <= 0:
        raise ValueError("positive_token_limit_required")
    if input_token_counter is None:
        raise RequestConstraintError("verified_input_token_bound_required")
    try:
        upper_bound = input_token_counter(deepcopy(body))
    except Exception:
        raise RequestConstraintError("input_token_bound_unavailable") from None
    if type(upper_bound) is not int or upper_bound < 0:
        raise RequestConstraintError("invalid_input_token_bound")
    if upper_bound > max_input_tokens:
        raise RequestConstraintError("input_token_limit_exceeded")
    return upper_bound


class ModelClient:
    def __init__(
        self,
        client,
        model_name,
        *,
        max_output_tokens=8192,
        max_input_tokens=65536,
        input_token_counter=None,
    ):
        for value in (max_output_tokens, max_input_tokens):
            if type(value) is not int or value <= 0:
                raise ValueError("positive_token_limit_required")
        self.client = client
        self.model_name = model_name
        self.max_output_tokens = max_output_tokens
        self.max_input_tokens = max_input_tokens
        # Trusted composition code must supply a verified model-specific upper
        # bound for the WHOLE request, including instructions and tool schemas.
        # No tokenizer, character estimate or previous usage is assumed here.
        self.input_token_counter = input_token_counter

    def call_once(
        self,
        input_data,
        instructions,
        tools,
        timeout=60,
    ):
        """Runtime owns retries and counts every actual SDK request."""
        body = deepcopy({
            "model": self.model_name,
            "input": input_data,
            "instructions": instructions,
            "tools": tools,
            "max_output_tokens": self.max_output_tokens,
        })
        enforce_input_token_limit(
            body,
            input_token_counter=self.input_token_counter,
            max_input_tokens=self.max_input_tokens,
        )
        client = self.client.with_options(max_retries=0, timeout=timeout)
        return client.responses.create(**body)
