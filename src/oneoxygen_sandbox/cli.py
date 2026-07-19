"""Command-line interface for the One Oxygen Phase 1 sandbox."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from oneoxygen_sandbox.config import load_task
from oneoxygen_sandbox.docker_adapter import DockerSDKAdapter
from oneoxygen_sandbox.errors import SandboxError
from oneoxygen_sandbox.models import RunStatus
from oneoxygen_sandbox.session import SandboxSession

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Secure local Docker sandbox runner for One Oxygen.",
)


def _print_captured_output(value: str, *, error: bool = False) -> None:
    maximum_display_characters = 4_000
    displayed = value.rstrip()
    if len(displayed) > maximum_display_characters:
        displayed = f"{displayed[:maximum_display_characters]}\n... CLI display truncated ..."
    if displayed:
        typer.echo(displayed, err=error)


@app.command()
def doctor() -> None:
    """Check that a compatible Docker engine is available."""
    try:
        info = DockerSDKAdapter().check_available()
    except SandboxError as exc:
        typer.secho(f"Docker unavailable: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    typer.secho(
        f"Docker ready: {info.get('ServerVersion', 'unknown')} "
        f"({info.get('OSType', 'unknown')}/{info.get('Architecture', 'unknown')})",
        fg=typer.colors.GREEN,
    )


@app.command()
def build(
    tag: Annotated[str, typer.Option(help="Docker tag to create.")] = "oneoxygen-sandbox:phase1",
) -> None:
    """Build and smoke-test the minimal Python sandbox image."""
    context = Path(__file__).resolve().parents[2] / "docker"
    try:
        image_id = DockerSDKAdapter().build_image(context, tag)
    except SandboxError as exc:
        typer.secho(f"Build failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    typer.secho(f"Built {tag} ({image_id}) and passed smoke test", fg=typer.colors.GREEN)


@app.command("run")
def run_task(
    task_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    runs_directory: Annotated[
        Path, typer.Option(help="Directory for run records and approved artifacts.")
    ] = Path("runs"),
) -> None:
    """Execute all commands from a task YAML in one fresh sandbox."""
    session: SandboxSession | None = None
    try:
        task_path = task_file.resolve()
        task = load_task(task_path)
        session = SandboxSession(task, task_path.parent, runs_directory.resolve())
        typer.echo(
            f"Run {session.run_id} | task {task.sandbox.task_id}@{task.sandbox.task_version}"
        )
        with session:
            for index, command in enumerate(task.commands, start=1):
                result = session.execute(command)
                label = "timeout" if result.timed_out else f"exit {result.exit_code}"
                typer.echo(f"  [{index}/{len(task.commands)}] {label} | {command}")
                _print_captured_output(result.stdout)
                _print_captured_output(result.stderr, error=True)
                if result.exit_code != 0:
                    break
            artifacts = session.collect_artifacts()
            typer.echo(f"  collected {len(artifacts)} artifact(s)")
    except SandboxError as exc:
        typer.secho(f"Sandbox failed: {exc}", fg=typer.colors.RED, err=True)
        if session is not None:
            typer.echo(f"Run record: {session.record_path}", err=True)
        raise typer.Exit(1) from exc

    if session is None:
        raise typer.Exit(1)
    typer.echo(f"Run record: {session.record_path}")
    if session.record.final_status is not RunStatus.SUCCEEDED:
        raise typer.Exit(1)
    typer.secho("Run succeeded", fg=typer.colors.GREEN)
