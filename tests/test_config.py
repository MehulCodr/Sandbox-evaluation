from __future__ import annotations

from pathlib import Path

import pytest

from oneoxygen_sandbox.config import configuration_hash, load_task
from oneoxygen_sandbox.errors import ConfigurationError, PathTraversalError


def test_example_task_loads_and_hash_is_stable(project_root: Path) -> None:
    task_path = project_root / "examples" / "basic" / "task.yaml"

    first = load_task(task_path)
    second = load_task(task_path)

    assert len(first.commands) == 2
    assert configuration_hash(first) == configuration_hash(second)
    assert len(configuration_hash(first)) == 64


def test_invalid_yaml_is_structured_error(tmp_path: Path) -> None:
    task_path = tmp_path / "task.yaml"
    task_path.write_text("sandbox: [", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="invalid YAML"):
        load_task(task_path)


def test_task_configuration_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "real.yaml"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "task.yaml"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")

    with pytest.raises(PathTraversalError):
        load_task(link)
