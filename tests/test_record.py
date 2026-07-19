from __future__ import annotations

from datetime import UTC, datetime

from oneoxygen_sandbox.models import (
    ArtifactMetadata,
    ExecResult,
    RunRecord,
    RunStatus,
    SandboxPolicy,
    SandboxSpec,
)


def test_run_record_round_trip_serialization() -> None:
    now = datetime.now(UTC)
    spec = SandboxSpec(image="image:tag", task_id="task", task_version="1")
    result = ExecResult(
        command="printf ok",
        stdout="ok",
        stderr="",
        exit_code=0,
        start_timestamp=now,
        end_timestamp=now,
        duration_seconds=0.01,
    )
    record = RunRecord(
        run_id="a" * 32,
        task_id="task",
        task_version="1",
        requested_image="image:tag",
        resolved_image="sha256:abc",
        task_configuration_hash="b" * 64,
        start_timestamp=now,
        end_timestamp=now,
        sandbox_policy=SandboxPolicy.from_spec(spec),
        command_results=[result],
        final_status=RunStatus.SUCCEEDED,
        artifacts=[ArtifactMetadata(relative_path="result.txt", size_bytes=2, sha256="c" * 64)],
    )

    restored = RunRecord.model_validate_json(record.model_dump_json())

    assert restored == record
    assert restored.command_results[0].timed_out is False
    assert restored.sandbox_policy.read_only_root_filesystem is True
