"""Filesystem boundary enforcement for inputs and approved outputs."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path, PurePosixPath

from oneoxygen_sandbox.errors import (
    OutputSizeExceededError,
    PathSafetyError,
    PathTraversalError,
    SymlinkRejectedError,
)
from oneoxygen_sandbox.models import ArtifactMetadata, InputAsset


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _reject_symlink_components(path: Path, root: Path) -> None:
    current = root
    relative = path.relative_to(root)
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                raise SymlinkRejectedError(f"symbolic links are not permitted: {current}")
        except OSError as exc:
            raise PathSafetyError(f"cannot inspect path component {current}: {exc}") from exc


def _copy_file(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise SymlinkRejectedError(f"symbolic links are not permitted in task inputs: {source}")
    mode = source.stat(follow_symlinks=False).st_mode
    if not stat.S_ISREG(mode):
        raise PathSafetyError(f"task input is not a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination, follow_symlinks=False)


def _copy_directory(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    with os.scandir(source) as entries:
        for entry in entries:
            entry_source = Path(entry.path)
            entry_destination = destination / entry.name
            if entry.is_symlink():
                raise SymlinkRejectedError(
                    f"symbolic links are not permitted in task inputs: {entry_source}"
                )
            if entry.is_dir(follow_symlinks=False):
                _copy_directory(entry_source, entry_destination)
            elif entry.is_file(follow_symlinks=False):
                _copy_file(entry_source, entry_destination)
            else:
                raise PathSafetyError(f"unsupported task input type: {entry_source}")


def _copy_input_assets(
    task_directory: Path, workspace: Path, assets: tuple[InputAsset, ...]
) -> None:
    task_root = task_directory.resolve(strict=True)
    workspace_root = workspace.resolve(strict=True)
    for asset in assets:
        source_candidate = task_root / Path(asset.source)
        _reject_symlink_components(source_candidate, task_root)
        try:
            source = source_candidate.resolve(strict=True)
        except OSError as exc:
            raise PathSafetyError(f"input asset does not exist: {asset.source}") from exc
        if not _inside(source, task_root):
            raise PathTraversalError(f"input asset escapes the task directory: {asset.source}")

        destination_relative = PurePosixPath(asset.destination)
        destination = workspace_root.joinpath(*destination_relative.parts)
        if not _inside(destination.resolve(strict=False), workspace_root):
            raise PathTraversalError(
                f"input destination escapes the workspace: {asset.destination}"
            )
        if destination.exists():
            raise PathSafetyError(f"duplicate input destination: {asset.destination}")
        if source.is_dir():
            _copy_directory(source, destination)
        else:
            _copy_file(source, destination)


def copy_input_assets(
    task_directory: Path, workspace: Path, assets: tuple[InputAsset, ...]
) -> None:
    try:
        _copy_input_assets(task_directory, workspace, assets)
    except OSError as exc:
        raise PathSafetyError(f"cannot safely copy task inputs: {exc}") from exc


def _approved_files(output_root: Path) -> list[tuple[Path, Path]]:
    if output_root.is_symlink():
        raise SymlinkRejectedError(f"output directory may not be a symbolic link: {output_root}")
    if not output_root.exists():
        return []
    if not output_root.is_dir():
        raise PathSafetyError(f"configured output path is not a directory: {output_root}")

    root = output_root.resolve(strict=True)
    approved: list[tuple[Path, Path]] = []
    stack = [output_root]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.is_symlink():
                    raise SymlinkRejectedError(f"output symbolic link rejected: {path}")
                resolved = path.resolve(strict=True)
                if not _inside(resolved, root):
                    raise PathTraversalError(f"output path escapes approved directory: {path}")
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                elif entry.is_file(follow_symlinks=False):
                    approved.append((path, resolved.relative_to(root)))
                else:
                    raise PathSafetyError(f"unsupported output artifact type: {path}")
    return sorted(approved, key=lambda item: item[1].as_posix())


def _collect_output_artifacts(
    output_root: Path,
    artifact_directory: Path,
    maximum_size_bytes: int,
) -> list[ArtifactMetadata]:
    files = _approved_files(output_root)
    total_size = 0
    sized_files: list[tuple[Path, Path, int]] = []
    for source, relative in files:
        size = source.stat(follow_symlinks=False).st_size
        total_size += size
        if total_size > maximum_size_bytes:
            raise OutputSizeExceededError(
                f"artifacts total {total_size} bytes, exceeding limit of {maximum_size_bytes} bytes"
            )
        sized_files.append((source, relative, size))

    artifact_directory.mkdir(parents=True, exist_ok=True)
    metadata: list[ArtifactMetadata] = []
    for source, relative, size in sized_files:
        destination = artifact_directory.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with source.open("rb") as input_stream, destination.open("xb") as output_stream:
            while chunk := input_stream.read(1024 * 1024):
                digest.update(chunk)
                output_stream.write(chunk)
        metadata.append(
            ArtifactMetadata(
                relative_path=relative.as_posix(),
                size_bytes=size,
                sha256=digest.hexdigest(),
            )
        )
    return metadata


def collect_output_artifacts(
    output_root: Path,
    artifact_directory: Path,
    maximum_size_bytes: int,
) -> list[ArtifactMetadata]:
    try:
        return _collect_output_artifacts(output_root, artifact_directory, maximum_size_bytes)
    except OSError as exc:
        raise PathSafetyError(f"cannot safely collect output artifacts: {exc}") from exc
