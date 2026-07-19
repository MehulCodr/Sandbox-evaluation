from __future__ import annotations

import pytest
from pydantic import ValidationError

from oneoxygen_sandbox.models import InputAsset, NetworkPolicy, SandboxSpec, SandboxTask


def valid_spec(**overrides: object) -> SandboxSpec:
    values: dict[str, object] = {
        "image": "oneoxygen-sandbox:phase1",
        "task_id": "task-1",
        "task_version": "1.0",
    }
    values.update(overrides)
    return SandboxSpec.model_validate(values)


def test_secure_defaults_are_applied() -> None:
    spec = valid_spec()

    assert spec.network_policy is NetworkPolicy.DISABLED
    assert spec.working_directory == "/workspace"
    assert spec.output_directory == "/workspace/output"
    assert spec.cpu_limit > 0
    assert spec.memory_limit_bytes >= 16 * 1024 * 1024
    assert spec.pid_limit > 0


@pytest.mark.parametrize("task_id", ["../escape", "/absolute", "bad id", "a/b"])
def test_task_id_rejects_path_syntax(task_id: str) -> None:
    with pytest.raises(ValidationError):
        valid_spec(task_id=task_id)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("working_directory", "/etc"),
        ("working_directory", "/workspace/../etc"),
        ("output_directory", "/workspace"),
        ("output_directory", "../output"),
        ("output_directory", "C:\\output"),
    ],
)
def test_container_paths_stay_under_workspace(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        valid_spec(**{field: value})


@pytest.mark.parametrize(
    "source", ["../secret", "/etc/passwd", "C:\\secret.txt", "assets/../../secret"]
)
def test_asset_source_rejects_traversal(source: str) -> None:
    with pytest.raises(ValidationError):
        InputAsset(source=source, destination="input.txt")


def test_model_provider_keys_cannot_be_allowlisted() -> None:
    with pytest.raises(ValidationError, match="model-provider secret"):
        valid_spec(environment_allowlist=["OPENAI_API_KEY"])

    with pytest.raises(ValidationError, match="model-provider secret"):
        valid_spec(environment_allowlist=["DEEPSEEK_TOKEN"])

    with pytest.raises(ValidationError, match="model-provider secret"):
        valid_spec(environment_allowlist=["INTERNAL_API_KEY"])


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        SandboxTask.model_validate(
            {"sandbox": valid_spec().model_dump(), "commands": ["true"], "surprise": True}
        )
