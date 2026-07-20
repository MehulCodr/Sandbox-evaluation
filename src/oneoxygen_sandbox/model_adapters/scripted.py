"""Deterministic, network-free model adapter driven by a validated script."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import AliasChoices, Field, field_validator, model_validator

from oneoxygen_sandbox.errors import ConfigurationError, ModelError
from oneoxygen_sandbox.models import (
    ModelCapabilities,
    ModelErrorCode,
    ModelProvider,
    ModelRunConfig,
    ModelTurnRequest,
    ModelTurnResponse,
    ModelUsage,
    NormalizedFinishReason,
    StrictModel,
    ToolCall,
    ToolErrorCode,
    ToolResult,
    ToolSchemaMode,
)

_SCRIPTED_METADATA_KEYS = frozenset(
    {
        "incomplete_reason",
        "original_finish_reason",
        "parameter",
        "provider_error_code",
        "request_id",
        "service_tier",
        "status",
        "status_code",
    }
)


def _validate_scripted_metadata(value: dict[str, Any]) -> dict[str, Any]:
    unsupported = sorted(set(value) - _SCRIPTED_METADATA_KEYS)
    if unsupported:
        raise ValueError(f"unsupported scripted provider metadata: {', '.join(unsupported)}")
    return value


class ScriptedToolResultExpectation(StrictModel):
    call_id: str = Field(min_length=1, max_length=128)
    success: bool | None = None
    error_code: ToolErrorCode | None = None

    @model_validator(mode="after")
    def validate_error_condition(self) -> ScriptedToolResultExpectation:
        if self.success is True and self.error_code is not None:
            raise ValueError("a successful expected tool result cannot have an error code")
        return self


class ScriptedErrorSpec(StrictModel):
    code: ModelErrorCode = ModelErrorCode.PROVIDER_UNAVAILABLE
    message: str = Field(default="simulated model provider failure", min_length=1, max_length=2_000)
    retryable: bool | None = None
    provider_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider_metadata")
    @classmethod
    def validate_provider_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_scripted_metadata(value)


class ScriptedTurn(StrictModel):
    text: str = Field(default="", max_length=1_000_000)
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: NormalizedFinishReason | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)
    expected_previous_tool_result_call_ids: tuple[str, ...] | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "expected_previous_tool_result_call_ids",
            "expected_tool_result_call_ids",
            "expected_previous_call_ids",
        ),
    )
    expected_previous_tool_results: tuple[ScriptedToolResultExpectation, ...] | None = None
    error: ScriptedErrorSpec | None = None
    simulate_timeout: bool = Field(
        default=False,
        validation_alias=AliasChoices("simulate_timeout", "timeout"),
    )
    latency_seconds: float = Field(default=0, ge=0, le=86_400)
    returned_model: str | None = Field(default=None, min_length=1, max_length=256)
    response_id: str | None = Field(default=None, min_length=1, max_length=512)
    warnings: tuple[str, ...] = Field(default=(), max_length=100)
    provider_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider_metadata")
    @classmethod
    def validate_provider_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_scripted_metadata(value)

    @model_validator(mode="before")
    @classmethod
    def normalize_tool_call_names(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        updated = dict(value)
        raw_calls = updated.get("tool_calls")
        if isinstance(raw_calls, (list, tuple)):
            calls: list[Any] = []
            for raw_call in raw_calls:
                if (
                    isinstance(raw_call, Mapping)
                    and "name" in raw_call
                    and "tool_name" not in raw_call
                ):
                    normalized = dict(raw_call)
                    normalized["tool_name"] = normalized.pop("name")
                    calls.append(normalized)
                else:
                    calls.append(raw_call)
            updated["tool_calls"] = calls
        return updated

    @field_validator("expected_previous_tool_result_call_ids")
    @classmethod
    def validate_expected_call_ids(cls, values: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if values is not None and len(values) != len(set(values)):
            raise ValueError("expected previous tool-result call IDs must be unique")
        return values

    @model_validator(mode="after")
    def validate_turn_shape(self) -> ScriptedTurn:
        if self.usage.provider_metadata:
            raise ValueError("scripted usage provider metadata is not supported")
        if (
            self.expected_previous_tool_result_call_ids is not None
            and self.expected_previous_tool_results is not None
        ):
            raise ValueError("configure only one previous tool-result expectation form")
        if self.error is not None and self.simulate_timeout:
            raise ValueError("a scripted turn cannot simulate both an error and a timeout")
        if self.error is not None or self.simulate_timeout:
            if self.text or self.tool_calls or self.finish_reason is not None:
                raise ValueError("a simulated failure cannot also contain a model response")
        elif self.tool_calls and self.finish_reason not in {
            None,
            NormalizedFinishReason.TOOL_CALLS,
        }:
            raise ValueError("a scripted turn with tool calls must finish with tool_calls")
        elif not self.tool_calls and self.finish_reason is NormalizedFinishReason.TOOL_CALLS:
            raise ValueError("tool_calls finish reason requires at least one tool call")
        return self


class ScriptedModelScript(StrictModel):
    schema_version: int = Field(
        default=1,
        validation_alias=AliasChoices("schema_version", "version", "script_version"),
    )
    turns: tuple[ScriptedTurn, ...] = Field(min_length=1, max_length=100_000)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("unsupported scripted model schema version")
        return value


def load_scripted_model_script(path: Path) -> ScriptedModelScript:
    script_path = path.expanduser()
    if not script_path.is_absolute():
        script_path = (Path.cwd() / script_path).absolute()
    if script_path.is_symlink():
        raise ConfigurationError("scripted model file may not be a symbolic link")
    try:
        raw = script_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError("cannot read scripted model file") from exc
    try:
        data = json.loads(raw) if script_path.suffix.lower() == ".json" else yaml.safe_load(raw)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ConfigurationError("scripted model file is not valid JSON or YAML") from exc
    if not isinstance(data, dict):
        raise ConfigurationError("scripted model file must contain a mapping")
    try:
        return ScriptedModelScript.model_validate(data)
    except ValueError as exc:
        raise ConfigurationError(f"invalid scripted model file: {exc}") from exc


class ScriptedModelAdapter:
    """Replay validated provider outcomes while still using the real agent/tool loop."""

    def __init__(
        self,
        config: ModelRunConfig,
        script: ScriptedModelScript | Mapping[str, Any] | Path | str | None = None,
        *,
        script_path: Path | None = None,
    ) -> None:
        if script is not None and script_path is not None:
            raise ConfigurationError("provide either script or script_path, not both")
        source = script_path if script_path is not None else script
        if source is None:
            raise ConfigurationError("a scripted model script is required")
        if isinstance(source, ScriptedModelScript):
            parsed = source
        elif isinstance(source, Mapping):
            try:
                parsed = ScriptedModelScript.model_validate(dict(source))
            except ValueError as exc:
                raise ConfigurationError(f"invalid scripted model data: {exc}") from exc
        else:
            parsed = load_scripted_model_script(Path(source))
        self.config = self.validate_config(config)
        self.script = parsed
        serialized = json.dumps(
            parsed.model_dump(
                mode="json",
                exclude={"turns": {"__all__": {"tool_calls": {"__all__": {"timestamp"}}}}},
            ),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        self.script_sha256 = hashlib.sha256(serialized).hexdigest()
        self._script_index = 0
        self._responses_generated = 0
        self._started = False
        self._closed = False

    @property
    def provider(self) -> ModelProvider:
        return ModelProvider.SCRIPTED

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            tool_calling=True,
            multiple_tool_calls_per_turn=True,
        )

    def validate_config(self, config: ModelRunConfig) -> ModelRunConfig:
        if config.provider is not ModelProvider.SCRIPTED:
            raise ModelError(
                ModelErrorCode.INVALID_REQUEST,
                "scripted adapter requires the scripted provider",
            )
        unsupported: list[str] = []
        if config.temperature is not None:
            unsupported.append("temperature")
        if config.provider_settings:
            unsupported.append("provider_settings")
        if config.tool_schema_mode is not ToolSchemaMode.PORTABLE:
            unsupported.append("tool_schema_mode")
        if config.store_provider_response:
            unsupported.append("store_provider_response")
        if unsupported:
            raise ModelError(
                ModelErrorCode.UNSUPPORTED_PARAMETER,
                f"scripted adapter does not support: {', '.join(unsupported)}",
            )
        return config

    def start_conversation(self, request: ModelTurnRequest) -> None:
        if self._closed:
            raise ModelError(
                ModelErrorCode.INTERNAL_ADAPTER_ERROR,
                "scripted adapter is already closed",
            )
        if self._started:
            raise ModelError(
                ModelErrorCode.INVALID_REQUEST,
                "scripted conversation has already started",
            )
        self._validate_request_config(request)
        if request.turn_number != 1 or request.tool_results:
            raise ModelError(
                ModelErrorCode.INVALID_REQUEST,
                "the first scripted request must be turn 1 without tool results",
            )
        self._started = True

    def generate_next_turn(self, request: ModelTurnRequest) -> ModelTurnResponse:
        if not self._started or self._closed:
            raise ModelError(
                ModelErrorCode.INTERNAL_ADAPTER_ERROR,
                "scripted conversation is not active",
            )
        self._validate_request_config(request)
        expected_turn_number = self._responses_generated + 1
        if request.turn_number != expected_turn_number:
            raise ModelError(
                ModelErrorCode.INVALID_REQUEST,
                "scripted request turn number is out of sequence",
            )
        if self._script_index >= len(self.script.turns):
            raise ModelError(
                ModelErrorCode.INVALID_PROVIDER_RESPONSE,
                "scripted model has too few turns",
            )

        script_index = self._script_index
        turn = self.script.turns[script_index]
        self._check_previous_tool_results(turn, request.tool_results)
        self._script_index += 1

        if turn.simulate_timeout:
            raise ModelError(
                ModelErrorCode.REQUEST_TIMEOUT,
                "scripted model request timed out",
                retryable=True,
                provider_metadata={"script_turn": script_index + 1},
            )
        if turn.error is not None:
            retryable = (
                turn.error.retryable
                if turn.error.retryable is not None
                else turn.error.code
                in {
                    ModelErrorCode.RATE_LIMITED,
                    ModelErrorCode.REQUEST_TIMEOUT,
                    ModelErrorCode.PROVIDER_UNAVAILABLE,
                }
            )
            raise ModelError(
                turn.error.code,
                turn.error.message,
                retryable=retryable,
                provider_metadata=turn.error.provider_metadata,
            )

        tool_calls = tuple(
            call.model_copy(update={"original_index": index})
            for index, call in enumerate(turn.tool_calls)
        )
        finish_reason = turn.finish_reason or (
            NormalizedFinishReason.TOOL_CALLS if tool_calls else NormalizedFinishReason.COMPLETED
        )
        self._responses_generated += 1
        return ModelTurnResponse(
            response_id=turn.response_id or f"scripted-response-{script_index + 1}",
            provider=self.provider,
            requested_model=self.config.model,
            returned_model=turn.returned_model or self.config.model,
            text=turn.text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=turn.usage,
            latency_seconds=turn.latency_seconds,
            attempt_count=1,
            warnings=turn.warnings,
            provider_metadata={
                **turn.provider_metadata,
                "script_schema_version": self.script.schema_version,
                "script_turn": script_index + 1,
                "script_sha256": self.script_sha256,
            },
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._started and self._script_index < len(self.script.turns):
            remaining = len(self.script.turns) - self._script_index
            raise ModelError(
                ModelErrorCode.INVALID_PROVIDER_RESPONSE,
                f"scripted model has {remaining} unconsumed turn(s)",
            )

    def _validate_request_config(self, request: ModelTurnRequest) -> None:
        validated = self.validate_config(request.run_config)
        if validated != self.config:
            raise ModelError(
                ModelErrorCode.INVALID_REQUEST,
                "model run configuration changed during the conversation",
            )
        if (
            request.request_timeout_seconds is not None
            and request.request_timeout_seconds > self.config.model_call_timeout_seconds
        ):
            raise ModelError(
                ModelErrorCode.INVALID_REQUEST,
                "effective request timeout exceeds the configured model-call timeout",
            )

    def _check_previous_tool_results(
        self,
        turn: ScriptedTurn,
        results: tuple[ToolResult, ...],
    ) -> None:
        actual_ids = tuple(result.call_id for result in results)
        if (
            turn.expected_previous_tool_result_call_ids is not None
            and actual_ids != turn.expected_previous_tool_result_call_ids
        ):
            raise ModelError(
                ModelErrorCode.INVALID_PROVIDER_RESPONSE,
                "previous tool-result call IDs did not match the scripted expectation",
            )
        expectations = turn.expected_previous_tool_results
        if expectations is None:
            return
        if actual_ids != tuple(expectation.call_id for expectation in expectations):
            raise ModelError(
                ModelErrorCode.INVALID_PROVIDER_RESPONSE,
                "previous tool results did not match the scripted expectation",
            )
        for expectation, result in zip(expectations, results, strict=True):
            if expectation.success is not None and result.success is not expectation.success:
                raise ModelError(
                    ModelErrorCode.INVALID_PROVIDER_RESPONSE,
                    "previous tool-result success state did not match the scripted expectation",
                )
            actual_error = result.error.code if result.error is not None else None
            if expectation.error_code is not None and actual_error is not expectation.error_code:
                raise ModelError(
                    ModelErrorCode.INVALID_PROVIDER_RESPONSE,
                    "previous tool-result error code did not match the scripted expectation",
                )
