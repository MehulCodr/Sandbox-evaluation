"""Structured exception hierarchy for sandbox operations."""

from __future__ import annotations

from typing import Any

from oneoxygen_sandbox.models import (
    ModelErrorCode,
    ModelErrorInformation,
    sanitize_model_trace_text,
)


class SandboxError(Exception):
    """Base class for expected, user-facing sandbox failures."""

    code = "sandbox_error"


class ConfigurationError(SandboxError):
    code = "configuration_error"


class PathSafetyError(SandboxError):
    code = "unsafe_path"


class PathTraversalError(PathSafetyError):
    code = "path_traversal"


class SymlinkRejectedError(PathSafetyError):
    code = "symlink_rejected"


class OutputSizeExceededError(PathSafetyError):
    code = "output_size_exceeded"


class DockerUnavailableError(SandboxError):
    code = "docker_unavailable"


class DockerOperationError(SandboxError):
    code = "docker_operation_error"


class SecurityPolicyError(DockerOperationError):
    code = "security_policy_not_enforced"


class LifecycleError(SandboxError):
    code = "invalid_lifecycle"


class SandboxTimeoutError(SandboxError):
    code = "sandbox_timeout"


class RecordPersistenceError(SandboxError):
    code = "record_persistence_error"


class CleanupError(SandboxError):
    code = "cleanup_error"


def sanitize_model_error_message(value: str) -> str:
    """Bound and redact a provider error before it reaches logs or run records."""
    message = sanitize_model_trace_text(value).strip()
    if not message:
        message = "model provider request failed"
    return message[:2_000]


class ModelError(SandboxError):
    """Stable, sanitized model-adapter failure safe for persistence and display."""

    code = "model_error"

    def __init__(
        self,
        model_code: ModelErrorCode | str,
        message: str,
        *,
        retryable: bool = False,
        provider_metadata: dict[str, Any] | None = None,
    ) -> None:
        normalized_code = ModelErrorCode(model_code)
        sanitized = sanitize_model_error_message(message)
        self.error = ModelErrorInformation(
            code=normalized_code,
            message=sanitized,
            retryable=retryable,
            provider_metadata=provider_metadata or {},
        )
        self.model_code = normalized_code
        self.error_code = normalized_code
        self.message = sanitized
        self.retryable = retryable
        self.provider_metadata = self.error.provider_metadata
        self.code = normalized_code.value
        super().__init__(sanitized)

    @classmethod
    def from_information(cls, error: ModelErrorInformation) -> ModelError:
        return cls(
            error.code,
            error.message,
            retryable=error.retryable,
            provider_metadata=error.provider_metadata,
        )


class ToolFailure(SandboxError):
    """Sanitized failure that can be returned to a model-facing tool caller."""

    code = "tool_failure"

    def __init__(
        self,
        tool_code: str,
        message: str,
        *,
        content: dict | None = None,
        metadata: dict | None = None,
        truncated: bool = False,
    ) -> None:
        super().__init__(message)
        self.tool_code = tool_code
        self.message = message
        self.content = content or {}
        self.metadata = metadata or {}
        self.truncated = truncated
