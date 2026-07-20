from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

docker = pytest.importorskip("docker")

from docker.errors import NotFound  # noqa: E402

from oneoxygen_sandbox.agent import AgentRunner  # noqa: E402
from oneoxygen_sandbox.config import load_task  # noqa: E402
from oneoxygen_sandbox.docker_adapter import DockerSDKAdapter  # noqa: E402
from oneoxygen_sandbox.model_adapters.scripted import ScriptedModelAdapter  # noqa: E402
from oneoxygen_sandbox.models import (  # noqa: E402
    AgentTaskSpec,
    ModelProvider,
    ModelRunConfig,
    RunStatus,
    SandboxSpec,
    SandboxTask,
    ToolPolicy,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def docker_adapter(project_root: Path) -> DockerSDKAdapter:
    try:
        adapter = DockerSDKAdapter()
        adapter.check_available()
    except Exception as exc:
        pytest.skip(f"Docker Linux engine is unavailable: {exc}")
    adapter.build_image(project_root / "docker", "oneoxygen-sandbox:phase1")
    return adapter


def _config(**overrides: Any) -> ModelRunConfig:
    values: dict[str, Any] = {
        "provider": ModelProvider.SCRIPTED,
        "model": "scripted-integration",
        "maximum_retry_attempts": 0,
        "initial_retry_delay_seconds": 0,
    }
    values.update(overrides)
    return ModelRunConfig(**values)


def _task(task_directory: Path, *, agent_overrides: dict[str, Any] | None = None) -> SandboxTask:
    (task_directory / "task.md").write_text("Complete the scripted task.", encoding="utf-8")
    agent_values: dict[str, Any] = {
        "instruction_file": "task.md",
        "maximum_model_turns": 3,
        "maximum_provider_requests": 3,
        "overall_wall_time_seconds": 30,
    }
    agent_values.update(agent_overrides or {})
    return SandboxTask(
        sandbox=SandboxSpec(
            image="oneoxygen-sandbox:phase1",
            task_id="agent-docker-test",
            task_version="1",
            overall_timeout_seconds=45,
        ),
        tool_policy=ToolPolicy(
            allowed_tool_names=("execute_shell", "write_text_file", "submit_result"),
            shell_execution_allowed=True,
            max_total_tool_calls=8,
        ),
        agent=AgentTaskSpec(**agent_values),
    )


def _runner(
    task: SandboxTask,
    task_directory: Path,
    script: dict[str, Any],
    runs_directory: Path,
    docker_adapter: DockerSDKAdapter,
) -> AgentRunner:
    config = _config()
    return AgentRunner(
        task,
        task_directory,
        config,
        ScriptedModelAdapter(config, script),
        runs_directory=runs_directory,
        sandbox_adapter=docker_adapter,
        jitter=lambda _delay: 0,
    )


def test_scripted_agent_completes_in_one_persistent_sandbox(
    project_root: Path, tmp_path: Path, docker_adapter: DockerSDKAdapter
) -> None:
    task_path = project_root / "examples" / "agent_demo" / "task.yaml"
    task = load_task(task_path)
    config = _config(model="scripted-demo")
    runner = AgentRunner(
        task,
        task_path.parent,
        config,
        ScriptedModelAdapter(config, script_path=task_path.parent / "model_script.yaml"),
        runs_directory=tmp_path / "runs",
        sandbox_adapter=docker_adapter,
        jitter=lambda _delay: 0,
    )

    record = runner.run()

    assert runner.session is not None and runner.session.container is not None
    container_id = runner.session.container.id
    assert record.final_status is RunStatus.SUCCEEDED
    assert record.submission is not None
    assert len(record.model_events) == 4
    assert len(record.tool_events) == 6
    assert record.metrics.successful_tool_calls == 6
    assert record.model_configuration is not None
    artifact = runner.session.run_directory / "artifacts" / "findings.md"
    assert "Revenue grew **20.0%**" in artifact.read_text(encoding="utf-8")
    persisted = json.loads(runner.session.record_path.read_text(encoding="utf-8"))
    assert persisted["model_events"] and persisted["tool_events"]
    assert persisted["artifacts"][0]["sha256"] == record.artifacts[0].sha256
    with pytest.raises(NotFound):
        docker_adapter.client.containers.get(container_id)


def test_host_api_key_is_not_available_inside_agent_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, docker_adapter: DockerSDKAdapter
) -> None:
    host_secret = "integration-test-secret-must-not-enter-container"
    monkeypatch.setenv("OPENAI_API_KEY", host_secret)
    task_directory = tmp_path / "task"
    task_directory.mkdir()
    task = _task(task_directory)
    script = {
        "schema_version": 1,
        "turns": [
            {
                "tool_calls": [
                    {
                        "call_id": "call-env",
                        "tool_name": "execute_shell",
                        "arguments": {"command": 'test -z "${OPENAI_API_KEY+x}" && printf absent'},
                    }
                ]
            },
            {
                "expected_previous_call_ids": ["call-env"],
                "tool_calls": [
                    {
                        "call_id": "call-write",
                        "tool_name": "write_text_file",
                        "arguments": {
                            "path": "output/result.txt",
                            "content": "safe",
                            "create_parents": True,
                        },
                    },
                    {
                        "call_id": "call-submit",
                        "tool_name": "submit_result",
                        "arguments": {
                            "summary": "done",
                            "artifact_paths": ["output/result.txt"],
                        },
                    },
                ],
            },
        ],
    }
    runner = _runner(task, task_directory, script, tmp_path / "runs", docker_adapter)

    record = runner.run()

    assert record.final_status is RunStatus.SUCCEEDED
    assert record.tool_events[0].status.value == "succeeded"
    serialized = runner.session.record_path.read_text(encoding="utf-8")
    assert host_secret not in serialized


@pytest.mark.parametrize(
    ("script", "agent_overrides", "expected_status"),
    [
        (
            {
                "schema_version": 1,
                "turns": [
                    {
                        "error": {
                            "code": "provider_unavailable",
                            "message": "simulated failure",
                            "retryable": False,
                        }
                    }
                ],
            },
            {},
            RunStatus.PROVIDER_ERROR,
        ),
        (
            {
                "schema_version": 1,
                "turns": [
                    {
                        "tool_calls": [
                            {
                                "call_id": "call-unsubmitted",
                                "tool_name": "write_text_file",
                                "arguments": {
                                    "path": "output/unsubmitted.txt",
                                    "content": "do not collect",
                                    "create_parents": True,
                                },
                            }
                        ],
                        "usage": {
                            "input_tokens": 11,
                            "output_tokens": 1,
                            "total_tokens": 12,
                        },
                    }
                ],
            },
            {"maximum_total_input_tokens": 10},
            RunStatus.LIMIT_EXCEEDED,
        ),
        (
            {"schema_version": 1, "turns": [{"simulate_timeout": True}]},
            {},
            RunStatus.PROVIDER_ERROR,
        ),
    ],
)
def test_failure_limit_and_timeout_all_cleanup(
    script: dict[str, Any],
    agent_overrides: dict[str, Any],
    expected_status: RunStatus,
    tmp_path: Path,
    docker_adapter: DockerSDKAdapter,
) -> None:
    task_directory = tmp_path / expected_status.value
    task_directory.mkdir()
    task = _task(task_directory, agent_overrides=agent_overrides)
    runner = _runner(task, task_directory, script, tmp_path / "runs", docker_adapter)

    record = runner.run()

    assert runner.session is not None and runner.session.container is not None
    container_id = runner.session.container.id
    assert record.final_status is expected_status
    assert record.artifacts == []
    assert runner.session.record_path.exists()
    with pytest.raises(NotFound):
        docker_adapter.client.containers.get(container_id)


def test_no_oneoxygen_containers_remain(docker_adapter: DockerSDKAdapter) -> None:
    leftovers = docker_adapter.client.containers.list(
        all=True,
        filters={"label": "com.oneoxygen.sandbox=true"},
    )
    assert leftovers == []
