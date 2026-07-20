"""Provider-independent model-adapter contract."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from oneoxygen_sandbox.models import (
    ModelCapabilities,
    ModelProvider,
    ModelRunConfig,
    ModelTurnRequest,
    ModelTurnResponse,
)


@runtime_checkable
class ModelAdapter(Protocol):
    """A provider adapter that privately owns state and honors each request timeout."""

    @property
    def provider(self) -> ModelProvider: ...

    @property
    def capabilities(self) -> ModelCapabilities: ...

    def validate_config(self, config: ModelRunConfig) -> ModelRunConfig: ...

    def start_conversation(self, request: ModelTurnRequest) -> None: ...

    def generate_next_turn(self, request: ModelTurnRequest) -> ModelTurnResponse: ...

    def close(self) -> None: ...
