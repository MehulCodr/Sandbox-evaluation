from __future__ import annotations

import os
import warnings
from pathlib import Path

import pytest

from oneoxygen_sandbox.agent import AgentRunner
from oneoxygen_sandbox.docker_adapter import DockerSDKAdapter
from oneoxygen_sandbox.model_adapters.openai import OpenAIModelAdapter
from oneoxygen_sandbox.models import (
    AgentTaskSpec,
    ModelProvider,
    ModelRunConfig,
    RunStatus,
    SandboxSpec,
    SandboxTask,
    ToolPolicy,
)

pytestmark = [pytest.mark.live_api, pytest.mark.integration]


def test_opted_in_openai_agent_run(project_root: Path, tmp_path: Path) -> None:
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        pytest.skip("OPENAI_API_KEY is not configured")
    if os.environ.get("ONEOXYGEN_RUN_LIVE_TESTS") != "1":
        pytest.skip("set ONEOXYGEN_RUN_LIVE_TESTS=1 to opt in to paid API usage")
    model = os.environ.get("ONEOXYGEN_LIVE_MODEL", "").strip()
    if not model:
        pytest.skip("ONEOXYGEN_LIVE_MODEL must name the caller-selected live model")
    warnings.warn("live_api test is making a paid OpenAI Responses API request", stacklevel=1)

    docker_adapter = DockerSDKAdapter()
    docker_adapter.check_available()
    docker_adapter.build_image(project_root / "docker", "oneoxygen-sandbox:phase1")
    task_directory = tmp_path / "task"
    task_directory.mkdir()
    (task_directory / "task.md").write_text(
        "Create output/result.txt containing exactly: live adapter ok. Then submit it.",
        encoding="utf-8",
    )
    task = SandboxTask(
        sandbox=SandboxSpec(
            image="oneoxygen-sandbox:phase1",
            task_id="openai-live-smoke",
            task_version="1",
            overall_timeout_seconds=90,
        ),
        tool_policy=ToolPolicy(
            allowed_tool_names=("write_text_file", "submit_result"),
            max_total_tool_calls=6,
        ),
        agent=AgentTaskSpec(
            instruction_file="task.md",
            maximum_model_turns=4,
            maximum_provider_requests=6,
            overall_wall_time_seconds=60,
        ),
    )
    config = ModelRunConfig(
        provider=ModelProvider.OPENAI,
        model=model,
        maximum_output_tokens=300,
        model_call_timeout_seconds=30,
        maximum_retry_attempts=1,
    )
    runner = AgentRunner(
        task,
        task_directory,
        config,
        OpenAIModelAdapter(config),
        runs_directory=tmp_path / "runs",
        sandbox_adapter=docker_adapter,
    )

    record = runner.run()

    assert record.final_status is RunStatus.SUCCEEDED
    assert (runner.session.run_directory / "artifacts" / "result.txt").read_text(
        encoding="utf-8"
    ) == "live adapter ok"
    assert os.environ["OPENAI_API_KEY"] not in runner.session.record_path.read_text(
        encoding="utf-8"
    )
