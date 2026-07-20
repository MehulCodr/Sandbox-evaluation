"""Command-line interface for the One Oxygen sandbox."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated

import typer
import yaml
from pydantic import ValidationError

from oneoxygen_sandbox.agent import AgentRunner
from oneoxygen_sandbox.config import load_task
from oneoxygen_sandbox.docker_adapter import DockerSDKAdapter
from oneoxygen_sandbox.errors import ConfigurationError, ModelError, SandboxError
from oneoxygen_sandbox.model_adapters import default_model_adapter_registry
from oneoxygen_sandbox.models import (
    ModelErrorCode,
    ModelProvider,
    ModelRunConfig,
    RunStatus,
    ToolCall,
    ToolSchemaMode,
)
from oneoxygen_sandbox.session import SandboxSession
from oneoxygen_sandbox.tools import ToolDispatcher, default_tool_registry

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Secure local Docker sandbox runner for One Oxygen.",
)
tools_app = typer.Typer(help="Inspect provider-independent tool definitions.")
models_app = typer.Typer(help="Inspect and diagnose model adapters without network access.")
app.add_typer(tools_app, name="tools")
app.add_typer(models_app, name="models")

_EXIT_INVALID_TASK = 2
_EXIT_UNAVAILABLE_PROVIDER = 3
_EXIT_MISSING_DEPENDENCY = 4
_EXIT_MISSING_API_KEY = 5
_EXIT_PROVIDER_ERROR = 6
_EXIT_INCOMPLETE = 7
_EXIT_LIMIT_EXCEEDED = 8
_EXIT_SANDBOX_ERROR = 9
_EXIT_INTERNAL_ERROR = 10


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


@tools_app.command("list")
def list_tools(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit provider-independent JSON tool definitions."),
    ] = False,
) -> None:
    """List available sandbox tools."""
    registry = default_tool_registry()
    if json_output:
        typer.echo(json.dumps(registry.provider_schemas(), indent=2, sort_keys=True))
        return
    for definition in registry.definitions():
        typer.echo(f"{definition.name}: {definition.description}")


@models_app.command("list")
def list_models(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable adapter availability."),
    ] = False,
) -> None:
    """List registered adapters without importing SDKs or contacting providers."""
    descriptions = default_model_adapter_registry().descriptions()
    rows = [
        {
            "provider": item.provider.value,
            "optional_dependency": item.optional_dependency,
            "dependency_available": item.dependency_available,
        }
        for item in descriptions
    ]
    if json_output:
        typer.echo(json.dumps(rows, indent=2, sort_keys=True))
        return
    for item in descriptions:
        availability = "available" if item.dependency_available else "missing dependency"
        typer.echo(f"{item.provider.value}: {availability}")


@models_app.command("doctor")
def model_doctor(
    provider: Annotated[
        ModelProvider,
        typer.Option("--provider", help="Provider to check without making an API request."),
    ],
) -> None:
    """Check local dependency and credential configuration without a paid request."""
    registry = default_model_adapter_registry()
    info = next((item for item in registry.descriptions() if item.provider is provider), None)
    if info is None:
        typer.secho("Model provider is not configured", fg=typer.colors.RED, err=True)
        raise typer.Exit(_EXIT_UNAVAILABLE_PROVIDER)
    if not info.dependency_available:
        typer.secho(
            f"{provider.value} dependency is not installed",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(_EXIT_MISSING_DEPENDENCY)
    if provider is ModelProvider.OPENAI and not os.environ.get("OPENAI_API_KEY", "").strip():
        typer.secho("OPENAI_API_KEY is not configured", fg=typer.colors.RED, err=True)
        raise typer.Exit(_EXIT_MISSING_API_KEY)
    typer.secho(
        f"{provider.value} is locally configured; no provider request was made",
        fg=typer.colors.GREEN,
    )


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


@app.command("tool-demo")
def tool_demo(
    task_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    calls_file: Annotated[
        Path | None,
        typer.Option("--calls", help="Scripted ToolCall YAML file."),
    ] = None,
    runs_directory: Annotated[
        Path, typer.Option("--runs-dir", help="Directory for run records and approved artifacts.")
    ] = Path("runs"),
) -> None:
    """Run a scripted demonstration through the Phase 2 tool dispatcher."""
    session: SandboxSession | None = None
    failed = False
    try:
        task_path = task_file.resolve()
        task = load_task(task_path)
        calls_path = (
            calls_file.resolve() if calls_file else task_path.parent / "scripted_calls.yaml"
        )
        calls = _load_scripted_calls(calls_path)
        session = SandboxSession(task, task_path.parent, runs_directory.resolve())
        typer.echo(
            f"Tool demo {session.run_id} | task {task.sandbox.task_id}@{task.sandbox.task_version}"
        )
        with session:
            dispatcher = ToolDispatcher(session)
            for index, call in enumerate(calls, start=1):
                result = dispatcher.dispatch(call)
                status = "ok" if result.success else f"error {result.error.code.value}"
                typer.echo(f"  [{index}/{len(calls)}] {status} | {call.tool_name}")
                if not result.success:
                    failed = True
                    if result.error is not None:
                        typer.echo(f"    {result.error.message}", err=True)
                    break
            if not dispatcher.submission_state.submitted:
                failed = True
                typer.echo("  submission missing", err=True)
            artifacts = session.collect_artifacts()
            typer.echo(f"  collected {len(artifacts)} artifact(s)")
    except SandboxError as exc:
        typer.secho(f"Tool demo failed: {exc}", fg=typer.colors.RED, err=True)
        if session is not None:
            typer.echo(f"Run record: {session.record_path}", err=True)
        raise typer.Exit(1) from exc

    if session is None:
        raise typer.Exit(1)
    typer.echo(f"Run record: {session.record_path}")
    if failed or session.record.final_status is not RunStatus.SUCCEEDED:
        raise typer.Exit(1)
    typer.secho("Tool demo succeeded", fg=typer.colors.GREEN)


@app.command("agent-run")
def agent_run(
    task_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    provider: Annotated[
        ModelProvider,
        typer.Option("--provider", help="Model adapter provider."),
    ],
    model: Annotated[
        str,
        typer.Option("--model", help="Required provider model identifier."),
    ],
    script: Annotated[
        Path | None,
        typer.Option("--script", help="Scripted-provider YAML or JSON turn script."),
    ] = None,
    runs_directory: Annotated[
        Path,
        typer.Option("--runs-dir", help="Directory for run records and submitted artifacts."),
    ] = Path("runs"),
    maximum_output_tokens: Annotated[
        int,
        typer.Option("--max-output-tokens", help="Maximum provider output tokens per turn."),
    ] = 4_096,
    temperature: Annotated[
        float | None,
        typer.Option("--temperature", help="Optional provider sampling temperature."),
    ] = None,
    model_call_timeout_seconds: Annotated[
        float,
        typer.Option("--model-call-timeout", help="Timeout for each provider attempt."),
    ] = 60.0,
    maximum_retry_attempts: Annotated[
        int,
        typer.Option("--max-retries", help="Maximum central retries after an initial failure."),
    ] = 2,
    initial_retry_delay_seconds: Annotated[
        float,
        typer.Option("--initial-retry-delay", help="Initial retry backoff delay."),
    ] = 1.0,
    tool_schema_mode: Annotated[
        ToolSchemaMode,
        typer.Option("--tool-schema-mode", help="Portable or explicitly native strict schemas."),
    ] = ToolSchemaMode.PORTABLE,
    store_provider_response: Annotated[
        bool,
        typer.Option(
            "--store-provider-response",
            help="Request provider-side response storage when the adapter supports it.",
        ),
    ] = False,
) -> None:
    """Execute a task through the deterministic Phase 3A agent loop."""
    runner: AgentRunner | None = None
    try:
        task_path = task_file.resolve()
        task = load_task(task_path)
        if task.agent is None:
            raise ConfigurationError("task does not define an agent section")
        if provider is ModelProvider.OPENAI and not os.environ.get("OPENAI_API_KEY", "").strip():
            raise ModelError(
                ModelErrorCode.MISSING_API_KEY,
                "OPENAI_API_KEY is required for the OpenAI provider",
            )
        config = ModelRunConfig(
            provider=provider,
            model=model,
            maximum_output_tokens=maximum_output_tokens,
            temperature=temperature,
            model_call_timeout_seconds=model_call_timeout_seconds,
            maximum_retry_attempts=maximum_retry_attempts,
            initial_retry_delay_seconds=initial_retry_delay_seconds,
            tool_schema_mode=tool_schema_mode,
            store_provider_response=store_provider_response,
        )
        registry = default_model_adapter_registry()
        adapter_arguments: dict[str, object] = {}
        if provider is ModelProvider.SCRIPTED:
            adapter_arguments["script_path"] = (
                script.resolve() if script is not None else task_path.parent / "model_script.yaml"
            )
        elif script is not None:
            raise ConfigurationError("--script is only valid with the scripted provider")
        adapter = registry.create(provider, config, **adapter_arguments)
        runner = AgentRunner(
            task,
            task_path.parent,
            config,
            adapter,
            runs_directory=runs_directory.resolve(),
        )
        typer.echo(
            f"Agent run | task {task.sandbox.task_id}@{task.sandbox.task_version} "
            f"| provider {provider.value} | model {model}"
        )
        record = runner.run()
    except (ConfigurationError, ValidationError) as exc:
        typer.secho("Invalid agent task or configuration", fg=typer.colors.RED, err=True)
        raise typer.Exit(_EXIT_INVALID_TASK) from exc
    except ModelError as exc:
        exit_code = (
            _EXIT_MISSING_API_KEY
            if exc.model_code is ModelErrorCode.MISSING_API_KEY
            else _EXIT_MISSING_DEPENDENCY
            if exc.model_code is ModelErrorCode.MISSING_DEPENDENCY
            else _EXIT_UNAVAILABLE_PROVIDER
            if exc.model_code is ModelErrorCode.PROVIDER_NOT_CONFIGURED
            else _EXIT_PROVIDER_ERROR
        )
        typer.secho(f"Model adapter failed: {exc.message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(exit_code) from exc
    except SandboxError as exc:
        typer.secho(
            "Sandbox failed before the agent run could start", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(_EXIT_SANDBOX_ERROR) from exc

    if runner.record_path is not None:
        typer.echo(f"Run record: {runner.record_path}")
    termination = record.termination_reason.value if record.termination_reason else "unknown"
    typer.echo(f"Termination: {termination}")
    if record.final_status is RunStatus.SUCCEEDED:
        typer.secho("Agent run succeeded", fg=typer.colors.GREEN)
        return
    typer.secho(f"Agent run ended with status {record.final_status.value}", fg=typer.colors.RED)
    raise typer.Exit(_agent_exit_code(record.final_status))


def _agent_exit_code(status: RunStatus) -> int:
    if status is RunStatus.INCOMPLETE:
        return _EXIT_INCOMPLETE
    if status is RunStatus.LIMIT_EXCEEDED:
        return _EXIT_LIMIT_EXCEEDED
    if status is RunStatus.PROVIDER_ERROR or status is RunStatus.REFUSED:
        return _EXIT_PROVIDER_ERROR
    if status is RunStatus.SANDBOX_ERROR:
        return _EXIT_SANDBOX_ERROR
    if status is RunStatus.CANCELLED:
        return 130
    return _EXIT_INTERNAL_ERROR


def _load_scripted_calls(path: Path) -> tuple[ToolCall, ...]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigurationError(f"cannot read scripted tool calls: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"invalid scripted tool-call YAML: {exc}") from exc
    if isinstance(data, dict):
        data = data.get("calls")
    if not isinstance(data, list):
        raise ConfigurationError("scripted tool calls must be a YAML list or a mapping with calls")
    calls: list[ToolCall] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ConfigurationError("each scripted tool call must be a mapping")
        call_data = {
            "call_id": item.get("call_id", f"call-{index}"),
            "tool_name": item.get("tool_name"),
            "arguments": item.get("arguments", {}),
        }
        try:
            calls.append(ToolCall.model_validate(call_data))
        except ValueError as exc:
            raise ConfigurationError(f"invalid scripted tool call {index}: {exc}") from exc
    return tuple(calls)
