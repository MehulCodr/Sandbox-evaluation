from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from oneoxygen_sandbox.agent import AgentRunner
from oneoxygen_sandbox.config import configuration_hash
from oneoxygen_sandbox.errors import ModelError
from oneoxygen_sandbox.model_adapters.registry import ModelAdapterRegistry
from oneoxygen_sandbox.models import (
    AgentTaskSpec,
    ModelCapabilities,
    ModelErrorCode,
    ModelEvent,
    ModelProvider,
    ModelRunConfig,
    ModelTurnRequest,
    ModelTurnResponse,
    ModelUsage,
    NormalizedFinishReason,
    RunMetrics,
    SandboxSpec,
    SandboxTask,
    ToolCall,
    ToolDefinition,
    normalize_finish_reason,
)


class FakeAdapter:
    provider = ModelProvider.SCRIPTED
    capabilities = ModelCapabilities(tool_calling=True)

    def __init__(self, config: ModelRunConfig) -> None:
        self.config = config

    def validate_config(self, config: ModelRunConfig) -> ModelRunConfig:
        return config

    def start_conversation(self, request: ModelTurnRequest) -> None:
        return None

    def generate_next_turn(self, request: ModelTurnRequest) -> ModelTurnResponse:
        return ModelTurnResponse(
            provider=self.provider,
            requested_model=request.run_config.model,
            finish_reason=NormalizedFinishReason.COMPLETED,
        )

    def close(self) -> None:
        return None


def config(**updates: object) -> ModelRunConfig:
    values: dict[str, object] = {
        "provider": ModelProvider.SCRIPTED,
        "model": "scripted-demo",
    }
    values.update(updates)
    return ModelRunConfig.model_validate(values)


def test_model_run_config_is_frozen_and_rejects_secrets() -> None:
    run_config = config()

    with pytest.raises(ValidationError):
        run_config.model = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="credentials or secrets"):
        config(provider_settings={"api_key": "not-allowed"})


def test_model_run_config_provider_settings_are_deeply_immutable() -> None:
    source = {
        "nested": {
            "items": [{"value": 1}],
        }
    }
    run_config = config(provider_settings=source)
    source["nested"]["items"][0]["value"] = 99  # type: ignore[index]

    assert run_config.provider_settings["nested"]["items"][0]["value"] == 1
    with pytest.raises(TypeError, match="immutable"):
        run_config.provider_settings["nested"]["items"][0]["value"] = 2
    with pytest.raises(TypeError, match="immutable"):
        run_config.provider_settings["nested"]["new"] = "value"


def test_agent_task_is_optional_and_paths_are_safe() -> None:
    sandbox = SandboxSpec(image="image:tag", task_id="task", task_version="1")
    legacy = SandboxTask(sandbox=sandbox)
    agent = SandboxTask(sandbox=sandbox, agent=AgentTaskSpec(instruction_file="task.md"))

    assert legacy.agent is None
    assert agent.agent is not None
    with pytest.raises(ValidationError, match="relative POSIX path"):
        AgentTaskSpec(instruction_file="..\\secret.txt")


def test_legacy_configuration_hash_excludes_default_agent_field() -> None:
    task = SandboxTask(
        sandbox=SandboxSpec(image="image:tag", task_id="legacy-task", task_version="1"),
        commands=("printf ok",),
    )
    phase_two_shape = task.model_dump(mode="json")
    assert phase_two_shape.pop("agent") is None
    canonical = json.dumps(
        phase_two_shape,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")

    assert configuration_hash(task) == hashlib.sha256(canonical).hexdigest()


def test_usage_aggregation_preserves_unavailable_values() -> None:
    complete = ModelUsage(input_tokens=10, output_tokens=4, total_tokens=14)
    incomplete = ModelUsage(input_tokens=None, output_tokens=3, total_tokens=None)

    aggregated = ModelUsage.aggregate([complete, incomplete])

    assert aggregated.input_tokens is None
    assert aggregated.output_tokens == 7
    assert aggregated.total_tokens is None
    assert aggregated.reasoning_tokens is None


@pytest.mark.parametrize(
    ("provider_value", "expected"),
    [
        ("function_call", NormalizedFinishReason.TOOL_CALLS),
        ("stop", NormalizedFinishReason.COMPLETED),
        ("max_output_tokens", NormalizedFinishReason.LENGTH),
        ("unrecognized-provider-label", NormalizedFinishReason.UNKNOWN),
        (None, NormalizedFinishReason.UNKNOWN),
    ],
)
def test_finish_reason_normalization(
    provider_value: str | None, expected: NormalizedFinishReason
) -> None:
    assert normalize_finish_reason(provider_value) is expected


def test_model_event_bounds_text_and_metrics_aggregate() -> None:
    now = datetime.now(UTC)
    event = ModelEvent(
        sequence_number=1,
        turn_number=1,
        provider=ModelProvider.SCRIPTED,
        requested_model="scripted-demo",
        returned_model="scripted-demo",
        request_start_timestamp=now,
        request_end_timestamp=now,
        latency_seconds=0.25,
        attempt_count=1,
        finish_reason=NormalizedFinishReason.COMPLETED,
        text="x" * 70_000,
        usage=ModelUsage(input_tokens=5, output_tokens=2, total_tokens=7),
        requested_settings={},
        effective_settings={},
        tool_definitions_sha256="a" * 64,
        prompt_sha256="b" * 64,
    )

    restored = ModelEvent.model_validate_json(event.model_dump_json())
    metrics = RunMetrics.aggregate([event], [], total_wall_time_seconds=1.0)

    assert restored.text_truncated is True
    assert len(restored.text.encode("utf-8")) <= 64 * 1024
    assert len(restored.text_sha256) == 64
    assert metrics.model_turns == 1
    assert metrics.provider_attempts == 1
    assert metrics.total_tokens == 7
    assert metrics.total_model_latency_seconds == 0.25


def test_model_event_redacts_and_bounds_tool_call_traces() -> None:
    task = SandboxTask(
        sandbox=SandboxSpec(image="image:tag", task_id="trace-task", task_version="1")
    )
    runner = object.__new__(AgentRunner)
    runner.task = task
    write_content = "private write content with a credential-like payload"
    source_code = "print('private source with a credential-like payload')"
    calls = (
        ToolCall(
            call_id="call-write",
            tool_name="write_text_file",
            arguments={"path": "output/result.txt", "content": write_content},
            original_index=0,
        ),
        ToolCall(
            call_id="call-python",
            tool_name="execute_python",
            arguments={"source_code": source_code},
            original_index=1,
        ),
        ToolCall(
            call_id="call-large",
            tool_name="read_text_file",
            arguments={"path": "x" * 10_000},
            original_index=2,
        ),
    )
    traces = tuple(runner._tool_call_trace(call) for call in calls)
    now = datetime.now(UTC)
    response_secret = "sk" + "-response-secret-value"
    raw_text = f"Authorization: Bearer {response_secret}"
    event = ModelEvent(
        sequence_number=1,
        turn_number=1,
        provider=ModelProvider.SCRIPTED,
        requested_model="scripted-demo",
        returned_model="scripted-demo",
        request_start_timestamp=now,
        request_end_timestamp=now,
        latency_seconds=0,
        attempt_count=1,
        finish_reason=NormalizedFinishReason.TOOL_CALLS,
        text=raw_text,
        tool_calls=traces,
        requested_settings={},
        effective_settings={},
        tool_definitions_sha256="a" * 64,
        prompt_sha256="b" * 64,
    )
    serialized = event.model_dump_json()

    assert write_content not in serialized
    assert source_code not in serialized
    assert response_secret not in serialized
    assert event.text_sha256 == hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    assert event.tool_calls[0].arguments["content"] == {
        "sha256": hashlib.sha256(write_content.encode("utf-8")).hexdigest(),
        "size_bytes": len(write_content.encode("utf-8")),
    }
    assert event.tool_calls[1].arguments["source_code"] == {
        "sha256": hashlib.sha256(source_code.encode("utf-8")).hexdigest(),
        "size_bytes": len(source_code.encode("utf-8")),
    }
    assert event.tool_calls[2].arguments_truncated is True
    assert len(serialized.encode("utf-8")) < 20_000


def test_registry_is_deterministic_and_rejects_duplicate_provider() -> None:
    registry = ModelAdapterRegistry()
    registry.register(ModelProvider.SCRIPTED, FakeAdapter)

    assert registry.list_providers() == (ModelProvider.SCRIPTED,)
    assert registry.create(ModelProvider.SCRIPTED, config()).config == config()
    with pytest.raises(ModelError) as caught:
        registry.register(ModelProvider.SCRIPTED, FakeAdapter)
    assert caught.value.model_code is ModelErrorCode.INVALID_REQUEST


def test_registry_reports_missing_optional_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = ModelAdapterRegistry()
    registry.register(ModelProvider.OPENAI, FakeAdapter, optional_dependency="missing_sdk")
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    openai_config = ModelRunConfig(provider=ModelProvider.OPENAI, model="requested-model")

    assert registry.descriptions()[0].dependency_available is False
    with pytest.raises(ModelError) as caught:
        registry.create(ModelProvider.OPENAI, openai_config)
    assert caught.value.model_code is ModelErrorCode.MISSING_DEPENDENCY
    assert "openai" not in sys.modules


def test_model_error_redacts_secret_like_values() -> None:
    secret = "sk" + "-example-secret-value"
    error = ModelError(
        ModelErrorCode.AUTHENTICATION_FAILED,
        f"Authorization: Bearer {secret} request failed",
    )

    assert secret not in str(error)
    assert "Authorization: [REDACTED]" in str(error)


def test_turn_request_uses_canonical_tool_definitions() -> None:
    definition = ToolDefinition(name="example", description="Example.", arguments_schema={})
    request = ModelTurnRequest(
        turn_number=1,
        system_prompt="System prompt.",
        initial_task_instruction="Complete the task.",
        tool_definitions=(definition,),
        run_config=config(),
    )

    assert request.tool_definitions == (definition,)
    assert request.tool_results == ()
