"""YAML task loading and stable configuration hashing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from oneoxygen_sandbox.errors import ConfigurationError, PathTraversalError
from oneoxygen_sandbox.models import SandboxTask


def load_task(path: Path) -> SandboxTask:
    task_path = path.expanduser()
    if not task_path.is_absolute():
        task_path = (Path.cwd() / task_path).absolute()
    if task_path.is_symlink():
        raise PathTraversalError(f"task configuration may not be a symbolic link: {task_path}")
    try:
        raw = task_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"cannot read task configuration {task_path}: {exc}") from exc
    try:
        data: Any = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"invalid YAML in {task_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError("task configuration must be a YAML mapping")
    try:
        return SandboxTask.model_validate(data)
    except ValidationError as exc:
        raise ConfigurationError(f"invalid task configuration: {exc}") from exc


def configuration_hash(task: SandboxTask) -> str:
    canonical = json.dumps(
        task.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
