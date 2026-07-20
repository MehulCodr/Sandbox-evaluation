from __future__ import annotations

from datetime import UTC, datetime

import pytest

from oneoxygen_sandbox.errors import ConfigurationError, ModelError
from oneoxygen_sandbox.model_adapters.scripted import ScriptedModelAdapter
from oneoxygen_sandbox.models import (
    ModelErrorCode,
    ModelProvider,
    ModelRunConfig,
    ModelTurnRequest,
    NormalizedFinishReason,
    ToolCall,
    ToolError,
    ToolErrorCode,
    ToolResult,
)


def config(**updates: object) -> ModelRunConfig:
    values: dict[str, object] = {
        "provider": ModelProvider.SCRIPTED,
        "model": "scripted-demo",
    }
    values.update(updates)
    return ModelRunConfig.model_validate(values)


def request(
    run_config: ModelRunConfig,
    turn_number: int,
    results: tuple[ToolResult, ...] = (),
) -> ModelTurnRequest:
    return ModelTurnRequest(
        turn_number=turn_number,
        system_prompt="System prompt.",
        initial_task_instruction="Complete the task.",
        tool_definitions=(),
        tool_results=results,
        run_config=run_config,
    )


def result(call_id: str, *, success: bool = True) -> ToolResult:
    now = datetime.now(UTC)
    return ToolResult(
        call_id=call_id,
        tool_name="list_files",
        success=success,
        error=None if success else ToolError(code=ToolErrorCode.EXECUTION_FAILED, message="failed"),
        start_timestamp=now,
        end_timestamp=now,
        duration_seconds=0,
    )


def test_scripted_adapter_supports_multiple_calls_and_expected_results() -> None:
    run_config = config()
    adapter = ScriptedModelAdapter(
        run_config,
        {
            "schema_version": 1,
            "turns": [
                {
                    "tool_calls": [
                        {"call_id": "call-1", "name": "list_files", "arguments": {}},
                        {
                            "call_id": "call-2",
                            "tool_name": "read_text_file",
                            "arguments": {"path": "input.txt"},
                        },
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                },
                {
                    "expected_previous_tool_result_call_ids": ["call-1", "call-2"],
                    "text": "Finished.",
                    "finish_reason": "completed",
                },
            ],
        },
    )
    first_request = request(run_config, 1)
    adapter.start_conversation(first_request)

    first = adapter.generate_next_turn(first_request)
    second = adapter.generate_next_turn(
        request(run_config, 2, (result("call-1"), result("call-2")))
    )
    adapter.close()

    assert first.finish_reason is NormalizedFinishReason.TOOL_CALLS
    assert [call.original_index for call in first.tool_calls] == [0, 1]
    assert second.text == "Finished."
    assert second.finish_reason is NormalizedFinishReason.COMPLETED


def test_scripted_adapter_rejects_wrong_previous_result_ids() -> None:
    run_config = config()
    adapter = ScriptedModelAdapter(
        run_config,
        {
            "turns": [
                {"tool_calls": [{"call_id": "call-1", "tool_name": "list_files"}]},
                {
                    "expected_previous_tool_result_call_ids": ["call-1"],
                    "text": "done",
                },
            ]
        },
    )
    first_request = request(run_config, 1)
    adapter.start_conversation(first_request)
    adapter.generate_next_turn(first_request)

    with pytest.raises(ModelError) as caught:
        adapter.generate_next_turn(request(run_config, 2, (result("other-call"),)))
    assert caught.value.model_code is ModelErrorCode.INVALID_PROVIDER_RESPONSE


def test_scripted_adapter_reports_too_few_and_too_many_turns() -> None:
    run_config = config()
    too_few = ScriptedModelAdapter(run_config, {"turns": [{"text": "first"}]})
    first_request = request(run_config, 1)
    too_few.start_conversation(first_request)
    too_few.generate_next_turn(first_request)
    with pytest.raises(ModelError, match="too few turns"):
        too_few.generate_next_turn(request(run_config, 2))

    too_many = ScriptedModelAdapter(
        run_config,
        {"turns": [{"text": "first"}, {"text": "unused"}]},
    )
    too_many.start_conversation(first_request)
    too_many.generate_next_turn(first_request)
    with pytest.raises(ModelError, match="unconsumed turn"):
        too_many.close()


@pytest.mark.parametrize(
    ("turn", "expected_code", "retryable"),
    [
        ({"simulate_timeout": True}, ModelErrorCode.REQUEST_TIMEOUT, True),
        (
            {"error": {"code": "authentication_failed", "message": "denied"}},
            ModelErrorCode.AUTHENTICATION_FAILED,
            False,
        ),
        (
            {"error": {"code": "rate_limited", "message": "slow down"}},
            ModelErrorCode.RATE_LIMITED,
            True,
        ),
    ],
)
def test_scripted_adapter_simulates_failures(
    turn: dict[str, object], expected_code: ModelErrorCode, retryable: bool
) -> None:
    run_config = config()
    adapter = ScriptedModelAdapter(run_config, {"turns": [turn]})
    first_request = request(run_config, 1)
    adapter.start_conversation(first_request)

    with pytest.raises(ModelError) as caught:
        adapter.generate_next_turn(first_request)
    assert caught.value.model_code is expected_code
    assert caught.value.retryable is retryable


def test_scripted_adapter_rejects_unsupported_parameters() -> None:
    with pytest.raises(ModelError) as caught:
        ScriptedModelAdapter(
            config(temperature=0.2),
            {"turns": [{"text": "unused"}]},
        )
    assert caught.value.model_code is ModelErrorCode.UNSUPPORTED_PARAMETER


def test_scripted_script_validation_is_strict() -> None:
    with pytest.raises(ConfigurationError, match="invalid scripted model data"):
        ScriptedModelAdapter(
            config(),
            {
                "turns": [
                    {
                        "tool_calls": [
                            ToolCall(
                                call_id="call-1",
                                tool_name="list_files",
                                arguments={},
                            ).model_dump(),
                        ],
                        "finish_reason": "completed",
                    }
                ]
            },
        )


def test_script_hash_is_deterministic_across_two_file_loads(tmp_path) -> None:
    script_path = tmp_path / "model_script.yaml"
    script_path.write_text(
        """\
schema_version: 1
turns:
  - tool_calls:
      - call_id: call-1
        tool_name: list_files
        arguments: {}
""",
        encoding="utf-8",
    )

    first = ScriptedModelAdapter(config(), script_path=script_path)
    second = ScriptedModelAdapter(config(), script_path=script_path)

    assert first.script_sha256 == second.script_sha256


def test_scripted_metadata_allowlist_rejects_internal_field_spoofing() -> None:
    with pytest.raises(ConfigurationError, match="unsupported scripted provider metadata"):
        ScriptedModelAdapter(
            config(),
            {
                "turns": [
                    {
                        "text": "done",
                        "provider_metadata": {"script_turn": 999},
                    }
                ]
            },
        )


def test_scripted_metadata_and_warnings_are_sanitized() -> None:
    run_config = config()
    warning_secret = "sk" + "-warning-secret-value"
    metadata_secret = "sk" + "-metadata-secret-value"
    adapter = ScriptedModelAdapter(
        run_config,
        {
            "turns": [
                {
                    "text": "done",
                    "warnings": [f"Authorization: Bearer {warning_secret}"],
                    "provider_metadata": {
                        "request_id": metadata_secret,
                        "status": "completed",
                    },
                }
            ]
        },
    )
    first_request = request(run_config, 1)
    adapter.start_conversation(first_request)

    response = adapter.generate_next_turn(first_request)
    serialized = response.model_dump_json()

    assert warning_secret not in serialized
    assert metadata_secret not in serialized
    assert response.warnings == ("Authorization: [REDACTED]",)
    assert response.provider_metadata["request_id"] == "[REDACTED]"
    assert response.provider_metadata["status"] == "completed"
    assert response.provider_metadata["script_turn"] == 1
