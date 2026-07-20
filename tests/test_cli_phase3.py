from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from oneoxygen_sandbox import cli
from oneoxygen_sandbox.model_adapters.registry import ModelAdapterRegistry
from oneoxygen_sandbox.models import (
    AgentTerminationReason,
    ModelProvider,
    ModelRunConfig,
    RunStatus,
)

runner = CliRunner()


def _fail_if_adapter_is_created(*args: Any, **kwargs: Any) -> None:
    raise AssertionError("provider listing and doctor must not create an adapter")


def test_models_list_is_deterministic_and_does_not_create_an_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ModelAdapterRegistry, "create", _fail_if_adapter_is_created)

    result = runner.invoke(cli.app, ["models", "list", "--json"])

    assert result.exit_code == 0, result.output
    assert result.output.index('"provider": "openai"') < result.output.index(
        '"provider": "scripted"'
    )
    assert '"optional_dependency": "openai"' in result.output


def test_models_doctor_checks_local_openai_configuration_without_api_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ModelAdapterRegistry, "create", _fail_if_adapter_is_created)
    monkeypatch.setattr(
        ModelAdapterRegistry,
        "_dependency_available",
        staticmethod(lambda dependency: True),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "doctor-only-secret")

    result = runner.invoke(cli.app, ["models", "doctor", "--provider", "openai"])

    assert result.exit_code == 0, result.output
    assert "no provider request was made" in result.output
    assert "doctor-only-secret" not in result.output


def test_models_doctor_has_distinct_missing_dependency_and_key_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "configured-but-unused")
    monkeypatch.setattr(
        ModelAdapterRegistry,
        "_dependency_available",
        staticmethod(lambda dependency: dependency is None),
    )

    missing_dependency = runner.invoke(
        cli.app,
        ["models", "doctor", "--provider", "openai"],
    )

    assert missing_dependency.exit_code == 4
    assert "dependency is not installed" in missing_dependency.output

    monkeypatch.setattr(
        ModelAdapterRegistry,
        "_dependency_available",
        staticmethod(lambda dependency: True),
    )
    monkeypatch.delenv("OPENAI_API_KEY")

    missing_key = runner.invoke(
        cli.app,
        ["models", "doctor", "--provider", "openai"],
    )

    assert missing_key.exit_code == 5
    assert "OPENAI_API_KEY is not configured" in missing_key.output


def test_scripted_agent_run_cli_reports_success_without_bypassing_runner(
    project_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeRegistry:
        def create(
            self,
            provider: ModelProvider,
            config: ModelRunConfig,
            **kwargs: Any,
        ) -> object:
            captured["provider"] = provider
            captured["config"] = config
            captured["adapter_kwargs"] = kwargs
            return object()

    class FakeAgentRunner:
        def __init__(
            self,
            task: Any,
            task_directory: Path,
            config: ModelRunConfig,
            adapter: object,
            *,
            runs_directory: Path,
        ) -> None:
            captured["task"] = task
            captured["task_directory"] = task_directory
            captured["runner_config"] = config
            captured["adapter"] = adapter
            self.record_path = runs_directory / "fake-run" / "run.json"

        def run(self) -> SimpleNamespace:
            return SimpleNamespace(
                final_status=RunStatus.SUCCEEDED,
                termination_reason=AgentTerminationReason.SUCCESSFUL_SUBMISSION,
            )

    monkeypatch.setattr(cli, "default_model_adapter_registry", FakeRegistry)
    monkeypatch.setattr(cli, "AgentRunner", FakeAgentRunner)
    task_path = project_root / "examples" / "agent_demo" / "task.yaml"
    script_path = project_root / "examples" / "agent_demo" / "model_script.yaml"

    result = runner.invoke(
        cli.app,
        [
            "agent-run",
            str(task_path),
            "--provider",
            "scripted",
            "--model",
            "scripted-demo",
            "--script",
            str(script_path),
            "--runs-dir",
            str(tmp_path / "runs"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Agent run succeeded" in result.output
    assert "successful_submission" in result.output
    assert captured["provider"] is ModelProvider.SCRIPTED
    assert captured["config"].model == "scripted-demo"
    assert captured["adapter_kwargs"] == {"script_path": script_path.resolve()}
