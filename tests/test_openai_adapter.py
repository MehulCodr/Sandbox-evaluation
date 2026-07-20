from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from oneoxygen_sandbox.errors import ModelError
from oneoxygen_sandbox.model_adapters.openai import OpenAIModelAdapter
from oneoxygen_sandbox.models import (
    ModelErrorCode,
    ModelProvider,
    ModelRunConfig,
    ModelTurnRequest,
    NormalizedFinishReason,
    ToolDefinition,
    ToolError,
    ToolErrorCode,
    ToolResult,
    ToolSchemaMode,
)


class FakeResponses:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.outcomes:
            raise AssertionError("unexpected Responses API call")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, outcomes: list[Any]) -> None:
        self.responses = FakeResponses(outcomes)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeSDK:
    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.constructor_calls: list[dict[str, Any]] = []

    def OpenAI(self, **kwargs: Any) -> FakeClient:
        self.constructor_calls.append(kwargs)
        return self.client


def model_config(**overrides: Any) -> ModelRunConfig:
    values: dict[str, Any] = {
        "provider": ModelProvider.OPENAI,
        "model": "openai-test-model",
        "maximum_output_tokens": 321,
        "model_call_timeout_seconds": 7.5,
    }
    values.update(overrides)
    return ModelRunConfig.model_validate(values)


def tool_definition() -> ToolDefinition:
    return ToolDefinition(
        name="read_text_file",
        description="Read a UTF-8 text file from the workspace.",
        arguments_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    )


def turn_request(
    config: ModelRunConfig,
    *,
    turn_number: int = 1,
    tool_results: tuple[ToolResult, ...] = (),
) -> ModelTurnRequest:
    return ModelTurnRequest(
        turn_number=turn_number,
        system_prompt="Use only the provided tools and submit the result.",
        initial_task_instruction="Inspect the synthetic input.",
        tool_definitions=(tool_definition(),),
        tool_results=tool_results,
        run_config=config,
    )


def successful_result(call_id: str = "call-1") -> ToolResult:
    now = datetime.now(UTC)
    return ToolResult(
        call_id=call_id,
        tool_name="read_text_file",
        success=True,
        content={"text": "synthetic evidence"},
        start_timestamp=now,
        end_timestamp=now,
        duration_seconds=0.01,
    )


def text_response(**overrides: Any) -> dict[str, Any]:
    response: dict[str, Any] = {
        "id": "resp-test",
        "model": "openai-returned-model",
        "status": "completed",
        "output_text": "Finished.",
        "output": [
            {
                "id": "msg-test",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Finished.",
                        "annotations": [],
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": 20,
            "output_tokens": 8,
            "total_tokens": 28,
            "input_tokens_details": {"cached_tokens": 3},
            "output_tokens_details": {"reasoning_tokens": 2},
        },
        "service_tier": "default",
    }
    response.update(overrides)
    return response


def started_adapter(
    config: ModelRunConfig,
    outcomes: list[Any],
    *,
    clock: Iterator[float] | None = None,
) -> tuple[OpenAIModelAdapter, FakeClient]:
    client = FakeClient(outcomes)
    adapter = OpenAIModelAdapter(
        config,
        client=client,
        environ={"OPENAI_API_KEY": "unit-test-secret"},
        clock=time_source(clock),
    )
    adapter.start_conversation(turn_request(config))
    return adapter, client


def time_source(values: Iterator[float] | None = None) -> Any:
    if values is None:
        values = iter((10.0, 10.25))
    return values.__next__


def test_text_response_uses_current_responses_shape_and_maps_usage() -> None:
    config = model_config()
    client = FakeClient([text_response()])
    sdk = FakeSDK(client)
    adapter = OpenAIModelAdapter(
        config,
        sdk_module=sdk,
        environ={"OPENAI_API_KEY": "host-only-secret"},
        clock=time_source(),
    )
    request = turn_request(config)

    adapter.start_conversation(request)
    response = adapter.generate_next_turn(request)

    assert sdk.constructor_calls == [
        {
            "api_key": "host-only-secret",
            "timeout": 7.5,
            "max_retries": 0,
        }
    ]
    call = client.responses.calls[0]
    assert call["model"] == "openai-test-model"
    assert call["instructions"] == request.system_prompt
    assert call["max_output_tokens"] == 321
    assert call["timeout"] == pytest.approx(7.5)
    assert call["store"] is False
    assert call["parallel_tool_calls"] is True
    assert call["include"] == ["reasoning.encrypted_content"]
    assert "previous_response_id" not in call
    assert "temperature" not in call
    assert call["tools"] == [
        {
            "type": "function",
            "name": "read_text_file",
            "description": "Read a UTF-8 text file from the workspace.",
            "parameters": tool_definition().arguments_schema,
            "strict": False,
        }
    ]
    assert response.response_id == "resp-test"
    assert response.requested_model == "openai-test-model"
    assert response.returned_model == "openai-returned-model"
    assert response.text == "Finished."
    assert response.finish_reason is NormalizedFinishReason.COMPLETED
    assert response.latency_seconds == pytest.approx(0.25)
    assert response.usage.input_tokens == 20
    assert response.usage.output_tokens == 8
    assert response.usage.reasoning_tokens == 2
    assert response.usage.cached_input_tokens == 3
    assert response.usage.total_tokens == 28


def test_request_specific_timeout_overrides_the_client_default() -> None:
    config = model_config()
    adapter, client = started_adapter(config, [text_response()])
    request = turn_request(config).model_copy(update={"request_timeout_seconds": 1.25})

    adapter.generate_next_turn(request)

    assert client.responses.calls[0]["timeout"] == pytest.approx(1.25)


def test_one_and_multiple_function_calls_are_normalized_in_provider_order() -> None:
    response = text_response(
        output_text="",
        output=[
            {
                "id": "fc-1",
                "type": "function_call",
                "status": "completed",
                "call_id": "call-1",
                "name": "read_text_file",
                "arguments": '{"path":"one.txt"}',
            },
            {
                "id": "fc-2",
                "type": "function_call",
                "status": "completed",
                "call_id": "call-2",
                "name": "read_text_file",
                "arguments": '{"path":"two.txt"}',
            },
        ],
    )
    config = model_config()
    adapter, _ = started_adapter(config, [response])

    normalized = adapter.generate_next_turn(turn_request(config))

    assert normalized.finish_reason is NormalizedFinishReason.TOOL_CALLS
    assert [call.call_id for call in normalized.tool_calls] == ["call-1", "call-2"]
    assert [call.original_index for call in normalized.tool_calls] == [0, 1]
    assert [call.arguments for call in normalized.tool_calls] == [
        {"path": "one.txt"},
        {"path": "two.txt"},
    ]


def test_stateless_continuation_replays_complete_output_before_tool_result() -> None:
    first = text_response(
        output_text="",
        output=[
            {
                "id": "rs-test",
                "type": "reasoning",
                "status": "completed",
                "summary": [],
                "content": [],
                "encrypted_content": "encrypted-reasoning-state",
            },
            {
                "id": "fc-test",
                "type": "function_call",
                "status": "completed",
                "call_id": "call-1",
                "name": "read_text_file",
                "arguments": '{"path":"input.txt"}',
            },
        ],
    )
    config = model_config()
    adapter, client = started_adapter(
        config,
        [first, text_response()],
        clock=iter((1.0, 1.1, 2.0, 2.2)),
    )
    adapter.generate_next_turn(turn_request(config))

    adapter.generate_next_turn(
        turn_request(config, turn_number=2, tool_results=(successful_result(),))
    )

    continuation = client.responses.calls[1]
    items = continuation["input"]
    assert [item.get("type", "message") for item in items] == [
        "message",
        "reasoning",
        "function_call",
        "function_call_output",
    ]
    assert items[1] == first["output"][0]
    assert items[2] == first["output"][1]
    assert items[3]["call_id"] == "call-1"
    assert set(json.loads(items[3]["output"])) == {"success", "content", "error"}
    assert json.loads(items[3]["output"])["content"] == {"text": "synthetic evidence"}
    assert "previous_response_id" not in continuation


def test_tool_error_continuation_is_sanitized_and_uses_only_allowed_fields(
    tmp_path: Path,
) -> None:
    first = text_response(
        output_text="",
        output=[
            {
                "id": "fc-test",
                "type": "function_call",
                "call_id": "call-1",
                "name": "read_text_file",
                "arguments": '{"path":"missing.txt"}',
            }
        ],
    )
    config = model_config()
    adapter, client = started_adapter(
        config,
        [first, text_response()],
        clock=iter((1.0, 1.1, 2.0, 2.1)),
    )
    adapter.generate_next_turn(turn_request(config))
    now = datetime.now(UTC)
    failed = ToolResult(
        call_id="call-1",
        tool_name="read_text_file",
        success=False,
        error=ToolError(
            code=ToolErrorCode.FILE_NOT_FOUND,
            message=f"missing host file {tmp_path}",
        ),
        start_timestamp=now,
        end_timestamp=now,
        duration_seconds=0,
    )

    adapter.generate_next_turn(turn_request(config, turn_number=2, tool_results=(failed,)))

    payload = json.loads(client.responses.calls[1]["input"][-1]["output"])
    assert set(payload) == {"success", "content", "error"}
    assert payload["error"]["code"] == ToolErrorCode.FILE_NOT_FOUND.value
    assert str(tmp_path) not in payload["error"]["message"]


def test_temperature_is_forwarded_in_portable_mode() -> None:
    config = model_config(temperature=0.2)
    adapter, client = started_adapter(config, [text_response()])

    adapter.generate_next_turn(turn_request(config))

    assert client.responses.calls[0]["tools"][0]["strict"] is False
    assert client.responses.calls[0]["temperature"] == pytest.approx(0.2)


def test_native_strict_mode_is_deferred_and_rejected() -> None:
    portable = OpenAIModelAdapter(
        model_config(),
        client=FakeClient([]),
        environ={"OPENAI_API_KEY": "unit-test-secret"},
    )

    assert portable.capabilities.strict_tool_schemas is False

    with pytest.raises(ModelError) as caught:
        OpenAIModelAdapter(
            model_config(tool_schema_mode=ToolSchemaMode.NATIVE_STRICT),
            client=FakeClient([]),
            environ={"OPENAI_API_KEY": "unit-test-secret"},
        )

    assert caught.value.model_code is ModelErrorCode.UNSUPPORTED_PARAMETER


@pytest.mark.parametrize(
    "arguments",
    ["{bad", "[]", 42, '{"value":NaN}', '{"path":"a","path":"b"}'],
)
def test_malformed_function_arguments_fail_without_repair(arguments: Any) -> None:
    response = text_response(
        output_text="",
        output=[
            {
                "id": "fc-test",
                "type": "function_call",
                "call_id": "call-1",
                "name": "read_text_file",
                "arguments": arguments,
            }
        ],
    )
    config = model_config()
    adapter, _ = started_adapter(config, [response])

    with pytest.raises(ModelError) as raised:
        adapter.generate_next_turn(turn_request(config))

    assert raised.value.model_code is ModelErrorCode.MALFORMED_TOOL_ARGUMENTS


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            text_response(
                output_text="",
                output=[
                    {
                        "id": "msg-refusal",
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": "I cannot help with that."}],
                    }
                ],
            ),
            NormalizedFinishReason.REFUSED,
        ),
        (
            text_response(
                status="incomplete",
                incomplete_details={"reason": "content_filter"},
            ),
            NormalizedFinishReason.CONTENT_FILTER,
        ),
        (
            text_response(
                status="incomplete",
                incomplete_details={"reason": "max_output_tokens"},
            ),
            NormalizedFinishReason.LENGTH,
        ),
        (text_response(status="cancelled"), NormalizedFinishReason.CANCELLED),
    ],
)
def test_finish_reasons_are_normalized(
    response: dict[str, Any], expected: NormalizedFinishReason
) -> None:
    config = model_config()
    adapter, _ = started_adapter(config, [response])

    normalized = adapter.generate_next_turn(turn_request(config))

    assert normalized.finish_reason is expected


@pytest.mark.parametrize(
    ("provider_code", "expected", "retryable"),
    [
        (
            "context_length_exceeded",
            ModelErrorCode.CONTEXT_LIMIT_EXCEEDED,
            False,
        ),
        ("server_error", ModelErrorCode.PROVIDER_UNAVAILABLE, True),
        ("unexpected_failure", ModelErrorCode.INVALID_PROVIDER_RESPONSE, False),
    ],
)
def test_failed_response_objects_are_classified(
    provider_code: str,
    expected: ModelErrorCode,
    retryable: bool,
) -> None:
    config = model_config()
    adapter, _ = started_adapter(
        config,
        [text_response(status="failed", error={"code": provider_code})],
    )

    with pytest.raises(ModelError) as caught:
        adapter.generate_next_turn(turn_request(config))

    assert caught.value.model_code is expected
    assert caught.value.retryable is retryable
    assert caught.value.provider_metadata == {
        "provider_error_code": provider_code,
        "request_id": "resp-test",
    }


def function_call_response(**overrides: Any) -> dict[str, Any]:
    return text_response(
        output_text="",
        output=[
            {
                "id": "fc-terminal",
                "type": "function_call",
                "call_id": "call-terminal",
                "name": "read_text_file",
                "arguments": '{"path":"input.txt"}',
            }
        ],
        **overrides,
    )


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            function_call_response(
                status="incomplete",
                incomplete_details={"reason": "max_output_tokens"},
            ),
            NormalizedFinishReason.LENGTH,
        ),
        (
            function_call_response(
                status="incomplete",
                incomplete_details={"reason": "content_filter"},
            ),
            NormalizedFinishReason.CONTENT_FILTER,
        ),
        (
            function_call_response(status="cancelled"),
            NormalizedFinishReason.CANCELLED,
        ),
        (
            function_call_response(
                status="failed",
                error={"code": "content_filter"},
            ),
            NormalizedFinishReason.CONTENT_FILTER,
        ),
    ],
)
def test_terminal_finish_reason_precedes_mixed_function_call(
    response: dict[str, Any],
    expected: NormalizedFinishReason,
) -> None:
    config = model_config()
    adapter, _ = started_adapter(config, [response])

    normalized = adapter.generate_next_turn(turn_request(config))

    assert normalized.finish_reason is expected
    assert [call.call_id for call in normalized.tool_calls] == ["call-terminal"]


def test_failed_response_precedes_mixed_function_call() -> None:
    config = model_config()
    adapter, _ = started_adapter(
        config,
        [function_call_response(status="failed", error={"code": "server_error"})],
    )

    with pytest.raises(ModelError) as caught:
        adapter.generate_next_turn(turn_request(config))

    assert caught.value.model_code is ModelErrorCode.PROVIDER_UNAVAILABLE
    assert caught.value.retryable is True


def provider_exception(
    class_name: str,
    *,
    status_code: int | None = None,
    code: str | None = None,
    parameter: str | None = None,
) -> Exception:
    exception_type = type(class_name, (Exception,), {})
    exception = exception_type("raw SDK detail containing a unit-test credential")
    exception.status_code = status_code  # type: ignore[attr-defined]
    exception.request_id = "req-sanitized"  # type: ignore[attr-defined]
    exception.body = {  # type: ignore[attr-defined]
        "error": {"code": code, "param": parameter}
    }
    return exception


@pytest.mark.parametrize(
    ("exception", "expected", "retryable"),
    [
        (
            provider_exception("AuthenticationError", status_code=401),
            ModelErrorCode.AUTHENTICATION_FAILED,
            False,
        ),
        (
            provider_exception("PermissionDeniedError", status_code=403),
            ModelErrorCode.PERMISSION_DENIED,
            False,
        ),
        (
            provider_exception("RateLimitError", status_code=429),
            ModelErrorCode.RATE_LIMITED,
            True,
        ),
        (
            provider_exception("APITimeoutError", status_code=408),
            ModelErrorCode.REQUEST_TIMEOUT,
            True,
        ),
        (
            provider_exception("APIConnectionError"),
            ModelErrorCode.PROVIDER_UNAVAILABLE,
            True,
        ),
        (
            provider_exception("InternalServerError", status_code=503),
            ModelErrorCode.PROVIDER_UNAVAILABLE,
            True,
        ),
        (
            provider_exception("BadRequestError", status_code=400),
            ModelErrorCode.INVALID_REQUEST,
            False,
        ),
        (
            provider_exception(
                "BadRequestError",
                status_code=400,
                code="context_length_exceeded",
            ),
            ModelErrorCode.CONTEXT_LIMIT_EXCEEDED,
            False,
        ),
        (
            provider_exception(
                "BadRequestError",
                status_code=400,
                code="unsupported_parameter",
                parameter="temperature",
            ),
            ModelErrorCode.UNSUPPORTED_PARAMETER,
            False,
        ),
    ],
)
def test_sdk_errors_are_sanitized_and_classified(
    exception: Exception,
    expected: ModelErrorCode,
    retryable: bool,
) -> None:
    config = model_config()
    adapter, _ = started_adapter(config, [exception])

    with pytest.raises(ModelError) as raised:
        adapter.generate_next_turn(turn_request(config))

    assert raised.value.model_code is expected
    assert raised.value.retryable is retryable
    assert "unit-test credential" not in str(raised.value)
    assert raised.value.provider_metadata.get("request_id") == "req-sanitized"


def test_missing_api_key_and_optional_dependency_are_distinct() -> None:
    config = model_config()
    request = turn_request(config)
    missing_key = OpenAIModelAdapter(config, client=FakeClient([]), environ={})

    with pytest.raises(ModelError) as key_error:
        missing_key.start_conversation(request)

    assert key_error.value.model_code is ModelErrorCode.MISSING_API_KEY

    missing_sdk = OpenAIModelAdapter(
        config,
        sdk_module=None,
        environ={"OPENAI_API_KEY": "unit-test-secret"},
    )
    with pytest.raises(ModelError) as dependency_error:
        missing_sdk.start_conversation(request)

    assert dependency_error.value.model_code is ModelErrorCode.MISSING_DEPENDENCY


def test_unsupported_provider_settings_fail_closed() -> None:
    with pytest.raises(ModelError) as settings_error:
        OpenAIModelAdapter(
            model_config(provider_settings={"unrecognized": True}),
            client=FakeClient([]),
            environ={"OPENAI_API_KEY": "unit-test-secret"},
        )
    assert settings_error.value.model_code is ModelErrorCode.UNSUPPORTED_PARAMETER


def test_explicit_response_storage_request_is_forwarded() -> None:
    config = model_config(store_provider_response=True)
    client = FakeClient([text_response()])
    adapter = OpenAIModelAdapter(
        config,
        client=client,
        environ={"OPENAI_API_KEY": "unit-test-secret"},
    )
    request = turn_request(config)

    adapter.start_conversation(request)
    adapter.generate_next_turn(request)

    assert client.responses.calls[0]["store"] is True
    assert "previous_response_id" not in client.responses.calls[0]


def test_missing_usage_stays_unavailable_and_api_key_is_not_serialized() -> None:
    config = model_config()
    adapter, _ = started_adapter(config, [text_response(usage=None)])

    response = adapter.generate_next_turn(turn_request(config))
    serialized = response.model_dump_json()

    assert response.usage.input_tokens is None
    assert response.usage.output_tokens is None
    assert response.usage.reasoning_tokens is None
    assert response.usage.cached_input_tokens is None
    assert response.usage.total_tokens is None
    assert "unit-test-secret" not in serialized
    assert "OPENAI_API_KEY" not in serialized


def test_close_is_idempotent_and_closes_injected_client() -> None:
    config = model_config()
    adapter, client = started_adapter(config, [])

    adapter.close()
    adapter.close()

    assert client.closed is True
