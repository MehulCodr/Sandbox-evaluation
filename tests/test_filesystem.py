from __future__ import annotations

from pathlib import Path

import pytest

from oneoxygen_sandbox.errors import OutputSizeExceededError, SymlinkRejectedError
from oneoxygen_sandbox.filesystem import collect_output_artifacts, copy_input_assets
from oneoxygen_sandbox.models import InputAsset


def test_copy_input_file_inside_task_directory(tmp_path: Path) -> None:
    task = tmp_path / "task"
    workspace = tmp_path / "workspace"
    task.mkdir()
    workspace.mkdir()
    (task / "input.txt").write_text("safe", encoding="utf-8")

    copy_input_assets(
        task, workspace, (InputAsset(source="input.txt", destination="data/input.txt"),)
    )

    assert (workspace / "data" / "input.txt").read_text(encoding="utf-8") == "safe"


def test_input_symlink_is_rejected(tmp_path: Path) -> None:
    task = tmp_path / "task"
    workspace = tmp_path / "workspace"
    task.mkdir()
    workspace.mkdir()
    target = tmp_path / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    link = task / "input.txt"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")

    with pytest.raises(SymlinkRejectedError):
        copy_input_assets(
            task, workspace, (InputAsset(source="input.txt", destination="input.txt"),)
        )


def test_nested_input_symlink_is_rejected(tmp_path: Path) -> None:
    task = tmp_path / "task"
    workspace = tmp_path / "workspace"
    source = task / "assets"
    source.mkdir(parents=True)
    workspace.mkdir()
    target = tmp_path / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    try:
        (source / "linked.txt").symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")

    with pytest.raises(SymlinkRejectedError):
        copy_input_assets(task, workspace, (InputAsset(source="assets", destination="assets"),))


def test_output_symlink_is_rejected(tmp_path: Path) -> None:
    output = tmp_path / "output"
    artifacts = tmp_path / "artifacts"
    output.mkdir()
    target = tmp_path / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    try:
        (output / "linked.txt").symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")

    with pytest.raises(SymlinkRejectedError):
        collect_output_artifacts(output, artifacts, 1024)
    assert not artifacts.exists()


def test_output_size_limit_is_enforced_before_copy(tmp_path: Path) -> None:
    output = tmp_path / "output"
    artifacts = tmp_path / "artifacts"
    output.mkdir()
    (output / "large.bin").write_bytes(b"x" * 11)

    with pytest.raises(OutputSizeExceededError, match="exceeding limit"):
        collect_output_artifacts(output, artifacts, 10)
    assert not artifacts.exists()


def test_artifact_metadata_and_nested_copy(tmp_path: Path) -> None:
    output = tmp_path / "output"
    artifacts = tmp_path / "artifacts"
    (output / "reports").mkdir(parents=True)
    (output / "reports" / "answer.txt").write_text("oxygen", encoding="utf-8")

    metadata = collect_output_artifacts(output, artifacts, 1024)

    assert metadata[0].relative_path == "reports/answer.txt"
    assert metadata[0].size_bytes == 6
    assert len(metadata[0].sha256) == 64
    assert (artifacts / "reports" / "answer.txt").read_text(encoding="utf-8") == "oxygen"
