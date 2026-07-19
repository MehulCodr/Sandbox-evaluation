from __future__ import annotations

from pathlib import Path

import pytest

docker = pytest.importorskip("docker")

from docker.errors import NotFound  # noqa: E402

from oneoxygen_sandbox.config import load_task  # noqa: E402
from oneoxygen_sandbox.docker_adapter import DockerSDKAdapter  # noqa: E402
from oneoxygen_sandbox.models import RunStatus  # noqa: E402
from oneoxygen_sandbox.session import SandboxSession  # noqa: E402

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


def test_hardened_policy_and_cleanup(
    project_root: Path, tmp_path: Path, docker_adapter: DockerSDKAdapter
) -> None:
    task_path = project_root / "examples" / "basic" / "task.yaml"
    task = load_task(task_path)
    session = SandboxSession(task, task_path.parent, tmp_path / "runs", docker_adapter)

    with session:
        assert session.container is not None
        container_id = session.container.id
        session.container.reload()
        attrs = session.container.attrs
        host = attrs["HostConfig"]
        assert attrs["Config"]["NetworkDisabled"] is True
        assert host["NetworkMode"] == "none"
        assert host["ReadonlyRootfs"] is True
        assert host["Memory"] == task.sandbox.memory_limit_bytes
        assert host["NanoCpus"] == int(task.sandbox.cpu_limit * 1_000_000_000)
        assert host["PidsLimit"] == task.sandbox.pid_limit
        assert session.execute('test "$(id -u)" -ne 0').exit_code == 0
        assert session.execute("touch /workspace/writable-test").exit_code == 0
        assert session.execute("touch /rootfs-must-fail").exit_code != 0
        session.collect_artifacts()

    with pytest.raises(NotFound):
        docker_adapter.client.containers.get(container_id)


def test_example_task_collects_artifact_and_record(
    project_root: Path, tmp_path: Path, docker_adapter: DockerSDKAdapter
) -> None:
    task_path = project_root / "examples" / "basic" / "task.yaml"
    task = load_task(task_path)
    session = SandboxSession(task, task_path.parent, tmp_path / "runs", docker_adapter)

    with session:
        for command in task.commands:
            assert session.execute(command).exit_code == 0
        metadata = session.collect_artifacts()

    result = session.run_directory / "artifacts" / "result.txt"
    assert metadata[0].relative_path == "result.txt"
    assert "ONE OXYGEN" in result.read_text(encoding="utf-8")
    assert session.record.final_status is RunStatus.SUCCEEDED
    assert session.record_path.exists()
    with pytest.raises(NotFound):
        docker_adapter.client.containers.get(session.container.id)
