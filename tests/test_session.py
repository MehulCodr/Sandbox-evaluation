from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oneoxygen_sandbox.models import ExecResult, InputAsset, RunStatus, SandboxSpec, SandboxTask
from oneoxygen_sandbox.session import SandboxSession


class FakeContainer:
    id = "fake-container"


class FakeDockerAdapter:
    def __init__(self, *, timed_out: bool = False, delay_seconds: float = 0) -> None:
        self.container = FakeContainer()
        self.workspace: Path | None = None
        self.created = 0
        self.started = 0
        self.stopped = 0
        self.removed = 0
        self.timed_out = timed_out
        self.delay_seconds = delay_seconds

    def check_available(self) -> dict[str, Any]:
        return {"OSType": "linux"}

    def resolve_image(self, image: str) -> str:
        return "sha256:resolved"

    def create_container(
        self, spec: SandboxSpec, workspace: Path, environment: dict[str, str], run_id: str
    ) -> FakeContainer:
        self.workspace = workspace
        self.created += 1
        return self.container

    def start_container(self, container: FakeContainer) -> None:
        self.started += 1

    def execute(
        self,
        container: FakeContainer,
        command: str,
        working_directory: str,
        timeout_seconds: float,
        maximum_output_bytes: int,
    ) -> ExecResult:
        assert self.workspace is not None
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if command == "prepare":
            (self.workspace / ".state").write_text("persistent", encoding="utf-8")
        elif command == "finish":
            state = (self.workspace / ".state").read_text(encoding="utf-8")
            output = self.workspace / "output"
            output.mkdir()
            (output / "result.txt").write_text(state, encoding="utf-8")
        now = datetime.now(UTC)
        return ExecResult(
            command=command,
            stdout="",
            stderr="",
            exit_code=124 if self.timed_out else 0,
            start_timestamp=now,
            end_timestamp=now,
            duration_seconds=0,
            timed_out=self.timed_out,
        )

    def stop_container(self, container: FakeContainer) -> None:
        self.stopped += 1

    def remove_container(self, container: FakeContainer) -> None:
        self.removed += 1


def make_task(task_directory: Path) -> SandboxTask:
    (task_directory / "input.txt").write_text("input", encoding="utf-8")
    spec = SandboxSpec(
        image="image:tag",
        task_id="mock-task",
        task_version="1",
        input_assets=(InputAsset(source="input.txt", destination="input.txt"),),
    )
    return SandboxTask(sandbox=spec, commands=("prepare", "finish"))


def test_mocked_lifecycle_persists_session_state_and_cleans_up(tmp_path: Path) -> None:
    task_directory = tmp_path / "task"
    task_directory.mkdir()
    task = make_task(task_directory)
    adapter = FakeDockerAdapter()
    session = SandboxSession(task, task_directory, tmp_path / "runs", adapter)

    with session:
        workspace = session.workspace_path
        session.execute("prepare")
        session.execute("finish")
        artifacts = session.collect_artifacts()
        assert artifacts[0].relative_path == "result.txt"

    assert workspace is not None and not workspace.exists()
    assert adapter.created == 1
    assert adapter.started == 1
    assert adapter.stopped == 1
    assert adapter.removed == 1
    assert session.record.final_status is RunStatus.SUCCEEDED
    assert session.record_path.exists()
    assert (session.run_directory / "artifacts" / "result.txt").read_text() == "persistent"


def test_mocked_timeout_forces_failed_run_and_cleanup(tmp_path: Path) -> None:
    task_directory = tmp_path / "task"
    task_directory.mkdir()
    task = make_task(task_directory)
    adapter = FakeDockerAdapter(timed_out=True)
    session = SandboxSession(task, task_directory, tmp_path / "runs", adapter)

    with session:
        result = session.execute("prepare")
        assert result.timed_out

    assert adapter.removed == 1
    assert session.record.final_status is RunStatus.TIMED_OUT


def test_explicit_stop_finalizes_and_writes_record(tmp_path: Path) -> None:
    task_directory = tmp_path / "task"
    task_directory.mkdir()
    task = make_task(task_directory)
    adapter = FakeDockerAdapter()
    session = SandboxSession(task, task_directory, tmp_path / "runs", adapter)

    session.start()
    session.execute("prepare")
    workspace = session.workspace_path
    session.stop()

    assert workspace is not None and not workspace.exists()
    assert adapter.removed == 1
    assert session.record.final_status is RunStatus.SUCCEEDED
    assert session.record_path.exists()


def test_overall_watchdog_stops_container_and_marks_result(tmp_path: Path) -> None:
    task_directory = tmp_path / "task"
    task_directory.mkdir()
    original = make_task(task_directory)
    task = original.model_copy(
        update={
            "sandbox": original.sandbox.model_copy(
                update={"overall_timeout_seconds": 0.1, "command_timeout_seconds": 1}
            )
        }
    )
    adapter = FakeDockerAdapter(delay_seconds=0.2)
    session = SandboxSession(task, task_directory, tmp_path / "runs", adapter)

    with session:
        result = session.execute("prepare")

    assert result.timed_out is True
    assert result.exit_code == 124
    assert adapter.stopped == 1
    assert adapter.removed == 1
    assert session.record.final_status is RunStatus.TIMED_OUT
