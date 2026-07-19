"""Small, testable adapter around the Docker SDK for Python."""

from __future__ import annotations

import os
import queue
import shutil
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import docker
from docker.credentials.errors import StoreError
from docker.errors import APIError, BuildError, DockerException, ImageNotFound

from oneoxygen_sandbox.errors import (
    DockerOperationError,
    DockerUnavailableError,
    SecurityPolicyError,
)
from oneoxygen_sandbox.models import ExecResult, SandboxSpec, is_forbidden_environment_name

Container = Any


class DockerAdapter(Protocol):
    def check_available(self) -> Mapping[str, Any]: ...

    def resolve_image(self, image: str) -> str: ...

    def create_container(
        self, spec: SandboxSpec, workspace: Path, environment: Mapping[str, str], run_id: str
    ) -> Container: ...

    def start_container(self, container: Container) -> None: ...

    def execute(
        self,
        container: Container,
        command: str,
        working_directory: str,
        timeout_seconds: float,
        maximum_output_bytes: int,
    ) -> ExecResult: ...

    def stop_container(self, container: Container) -> None: ...

    def remove_container(self, container: Container) -> None: ...


class _CapturedOutput:
    def __init__(self, maximum_bytes: int) -> None:
        self.maximum_bytes = maximum_bytes
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.truncated = False
        self._lock = threading.Lock()

    def append(self, target: bytearray, chunk: bytes | None) -> None:
        if not chunk:
            return
        with self._lock:
            remaining = self.maximum_bytes - len(self.stdout) - len(self.stderr)
            if remaining <= 0:
                self.truncated = True
                return
            target.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self.truncated = True

    def decoded(self) -> tuple[str, str, bool]:
        with self._lock:
            return (
                self.stdout.decode("utf-8", errors="replace"),
                self.stderr.decode("utf-8", errors="replace"),
                self.truncated,
            )


class DockerSDKAdapter:
    """Enforces the container policy and translates SDK failures."""

    sandbox_user = "10001:10001"

    def __init__(self, client: Any | None = None) -> None:
        if os.name == "posix" and os.getuid() != 0:
            self.sandbox_user = f"{os.getuid()}:{os.getgid()}"
        self._add_docker_desktop_helpers_to_process_path()
        try:
            self.client = client or docker.from_env()
        except DockerException as exc:
            raise DockerUnavailableError(f"cannot initialize Docker client: {exc}") from exc

    @staticmethod
    def _add_docker_desktop_helpers_to_process_path() -> None:
        """Find Docker Desktop helpers without changing the user's system environment."""
        if os.name != "nt" or shutil.which("docker-credential-desktop") is not None:
            return
        program_files = Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
        helper_directory = program_files / "Docker" / "Docker" / "resources" / "bin"
        helper = helper_directory / "docker-credential-desktop.exe"
        if not helper.is_file():
            return
        current_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{helper_directory}{os.pathsep}{current_path}"

    def check_available(self) -> Mapping[str, Any]:
        try:
            if not self.client.ping():
                raise DockerUnavailableError("Docker daemon did not respond to ping")
            info: Mapping[str, Any] = self.client.info()
        except DockerUnavailableError:
            raise
        except DockerException as exc:
            raise DockerUnavailableError(f"Docker is not available: {exc}") from exc
        if str(info.get("OSType", "")).lower() != "linux":
            raise DockerUnavailableError(
                "One Oxygen requires Docker to be running Linux containers"
            )
        return info

    def resolve_image(self, image: str) -> str:
        try:
            resolved = self.client.images.get(image)
        except ImageNotFound as exc:
            raise DockerOperationError(
                f"sandbox image {image!r} is not present; run the build command first"
            ) from exc
        except DockerException as exc:
            raise DockerOperationError(f"cannot inspect sandbox image {image!r}: {exc}") from exc
        repo_digests = resolved.attrs.get("RepoDigests") or []
        return str(repo_digests[0]) if repo_digests else str(resolved.id)

    def build_image(self, context_directory: Path, tag: str) -> str:
        self.check_available()
        try:
            image, _logs = self.client.images.build(
                path=str(context_directory.resolve()),
                tag=tag,
                rm=True,
                forcerm=True,
                pull=False,
            )
            self._smoke_test(image.id)
        except (BuildError, DockerException, StoreError) as exc:
            raise DockerOperationError(f"could not build sandbox image: {exc}") from exc
        return str(image.id)

    def _smoke_test(self, image: str) -> None:
        container: Container | None = None
        try:
            container = self.client.containers.run(
                image,
                [
                    "python",
                    "-c",
                    "import os,sys; sys.exit(0 if os.getuid() == 10001 else 1)",
                ],
                detach=True,
                network_mode="none",
                network_disabled=True,
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                tmpfs={"/tmp": "rw,noexec,nosuid,nodev,size=16m"},
                mem_limit=64 * 1024 * 1024,
                nano_cpus=500_000_000,
                pids_limit=16,
                user="10001:10001",
            )
            result = container.wait(timeout=30)
            if int(result.get("StatusCode", -1)) != 0:
                logs = container.logs().decode("utf-8", errors="replace")
                raise DockerOperationError(f"sandbox image smoke test failed: {logs}")
        finally:
            if container is not None:
                with suppress(DockerException):
                    container.remove(force=True)

    def create_container(
        self, spec: SandboxSpec, workspace: Path, environment: Mapping[str, str], run_id: str
    ) -> Container:
        try:
            container = self.client.containers.create(
                spec.image,
                detach=True,
                user=self.sandbox_user,
                working_dir=spec.working_directory,
                network_mode="none",
                network_disabled=True,
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                mem_limit=spec.memory_limit_bytes,
                nano_cpus=int(spec.cpu_limit * 1_000_000_000),
                pids_limit=spec.pid_limit,
                volumes={str(workspace.resolve()): {"bind": "/workspace", "mode": "rw"}},
                tmpfs={"/tmp": "rw,noexec,nosuid,nodev,size=64m"},
                environment=dict(environment),
                labels={
                    "com.oneoxygen.sandbox": "true",
                    "com.oneoxygen.run-id": run_id,
                    "com.oneoxygen.task-id": spec.task_id,
                },
                init=True,
            )
            self.validate_security_policy(container, spec)
            return container
        except SecurityPolicyError:
            if "container" in locals():
                with suppress(DockerException):
                    container.remove(force=True)
            raise
        except DockerException as exc:
            raise DockerOperationError(f"cannot create hardened sandbox container: {exc}") from exc

    def validate_security_policy(self, container: Container, spec: SandboxSpec) -> None:
        try:
            container.reload()
            attrs = container.attrs
        except DockerException as exc:
            raise DockerOperationError(f"cannot inspect sandbox security policy: {exc}") from exc

        config = attrs.get("Config", {})
        host = attrs.get("HostConfig", {})
        mounts = attrs.get("Mounts", [])
        failures: list[str] = []
        if config.get("User") in {None, "", "0", "root", "0:0"}:
            failures.append("non-root user")
        if config.get("WorkingDir") != spec.working_directory:
            failures.append("working directory")
        environment_names = {str(value).partition("=")[0] for value in (config.get("Env") or [])}
        if any(is_forbidden_environment_name(name) for name in environment_names):
            failures.append("model-provider secret exclusion")
        if not config.get("NetworkDisabled"):
            failures.append("network disabled flag")
        if host.get("NetworkMode") != "none":
            failures.append("network mode none")
        if not host.get("ReadonlyRootfs"):
            failures.append("read-only root filesystem")
        if set(host.get("CapDrop") or []) != {"ALL"}:
            failures.append("drop ALL capabilities")
        if "no-new-privileges:true" not in (host.get("SecurityOpt") or []):
            failures.append("no-new-privileges")
        if int(host.get("Memory") or 0) != spec.memory_limit_bytes:
            failures.append("memory limit")
        if int(host.get("NanoCpus") or 0) != int(spec.cpu_limit * 1_000_000_000):
            failures.append("CPU limit")
        if int(host.get("PidsLimit") or 0) != spec.pid_limit:
            failures.append("PID limit")
        tmpfs = host.get("Tmpfs") or {}
        if "/tmp" not in tmpfs:
            failures.append("/tmp tmpfs")
        else:
            tmpfs_options = str(tmpfs["/tmp"])
            if not all(option in tmpfs_options for option in ("noexec", "nosuid", "nodev")):
                failures.append("hardened /tmp tmpfs options")
        if len(mounts) != 1:
            failures.append("exactly one host mount")
        else:
            mount = mounts[0]
            source = str(mount.get("Source", "")).lower().replace("\\", "/")
            if (
                mount.get("Type") != "bind"
                or mount.get("Destination") != "/workspace"
                or not mount.get("RW")
            ):
                failures.append("writable /workspace bind mount")
            if "docker.sock" in source:
                failures.append("Docker socket exclusion")
        if failures:
            joined = ", ".join(failures)
            raise SecurityPolicyError(f"Docker did not enforce required controls: {joined}")

    def start_container(self, container: Container) -> None:
        try:
            container.start()
        except DockerException as exc:
            raise DockerOperationError(f"cannot start sandbox container: {exc}") from exc

    def execute(
        self,
        container: Container,
        command: str,
        working_directory: str,
        timeout_seconds: float,
        maximum_output_bytes: int,
    ) -> ExecResult:
        start_timestamp = datetime.now(UTC)
        started = time.monotonic()
        captured = _CapturedOutput(maximum_output_bytes)
        completed: queue.Queue[int | BaseException] = queue.Queue(maxsize=1)

        try:
            execution = self.client.api.exec_create(
                container.id,
                cmd=["/bin/sh", "-lc", command],
                stdout=True,
                stderr=True,
                stdin=False,
                tty=False,
                workdir=working_directory,
                user=self.sandbox_user,
            )
            execution_id = execution["Id"]
        except DockerException as exc:
            raise DockerOperationError(f"cannot create container execution: {exc}") from exc

        def consume_output() -> None:
            try:
                stream = self.client.api.exec_start(
                    execution_id, stream=True, demux=True, tty=False
                )
                for stdout_chunk, stderr_chunk in stream:
                    captured.append(captured.stdout, stdout_chunk)
                    captured.append(captured.stderr, stderr_chunk)
                inspection = self.client.api.exec_inspect(execution_id)
                completed.put(int(inspection.get("ExitCode", -1)))
            except BaseException as exc:  # passed safely back to the calling thread
                completed.put(exc)

        worker = threading.Thread(target=consume_output, daemon=True, name="sandbox-exec-output")
        worker.start()
        timed_out = False
        try:
            outcome = completed.get(timeout=timeout_seconds)
        except queue.Empty:
            timed_out = True
            self.stop_container(container)
            with suppress(queue.Empty):
                completed.get(timeout=5)
            outcome = 124

        if isinstance(outcome, BaseException) and not timed_out:
            if isinstance(outcome, DockerException):
                raise DockerOperationError(f"container execution failed: {outcome}") from outcome
            raise DockerOperationError(f"container output capture failed: {outcome}") from outcome

        stdout, stderr, truncated = captured.decoded()
        end_timestamp = datetime.now(UTC)
        return ExecResult(
            command=command,
            stdout=stdout,
            stderr=stderr,
            exit_code=124 if timed_out else int(outcome),
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            duration_seconds=time.monotonic() - started,
            timed_out=timed_out,
            output_truncated=truncated,
        )

    def stop_container(self, container: Container) -> None:
        try:
            container.reload()
            if container.status not in {"exited", "dead", "removing"}:
                container.stop(timeout=1)
        except APIError as exc:
            if exc.status_code != 304:
                raise DockerOperationError(f"cannot stop sandbox container: {exc}") from exc
        except DockerException as exc:
            raise DockerOperationError(f"cannot stop sandbox container: {exc}") from exc

    def remove_container(self, container: Container) -> None:
        try:
            container.remove(force=True, v=True)
        except DockerException as exc:
            raise DockerOperationError(f"cannot remove sandbox container: {exc}") from exc
