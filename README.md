# One Oxygen Sandbox

One Oxygen is a local, provider-neutral agent runner built in phases:

- Phase 1 provides a hardened, persistent Docker sandbox.
- Phase 2 provides deterministic tools, policy enforcement, and bounded tool traces.
- Phase 3A adds a provider-independent model contract, a deterministic agent loop, a
  network-free scripted adapter, and an optional OpenAI Responses API adapter.

Phase 3A supports only the `scripted` and `openai` model providers. It does not use an agent
framework, the Assistants API, the OpenAI Agents SDK, hosted provider tools, RAG, graders,
databases, streaming, a web UI, or distributed execution.

## Architecture

`AgentRunner` owns orchestration. A provider may request function calls, but it cannot execute
them. Every request is normalized to a `ToolCall`, checked by `ToolDispatcher`, and executed
against the same active `SandboxSession`.

The main boundaries are:

- `SandboxSession`: creates the temporary workspace and hardened container, preserves state
  across tool calls, stops the container, collects artifacts, persists `run.json`, and removes
  temporary resources.
- `SecureWorkspace`: exposes a workspace-only filesystem view to file tools and rejects
  traversal, symbolic links, protected paths, binary text operations, and oversized data.
- `ToolRegistry` and `ToolDispatcher`: expose canonical provider-neutral schemas, enforce
  `ToolPolicy`, execute calls sequentially, normalize errors, track submission, and write
  bounded `ToolEvent` records.
- `ModelAdapter`: translates one provider's private conversation representation to and from
  `ModelTurnRequest`, `ModelTurnResponse`, and the canonical tool protocol.
- `ModelAdapterRegistry`: registers adapter factories deterministically and checks optional
  dependencies without importing SDKs or contacting providers during listing.
- `AgentRunner`: enforces turns, provider requests, tokens, wall time, retries, finish states,
  artifact-selection rules, and cleanup across every runtime exit path.

The model API client and its credentials remain in the host process. Only model-requested shell
and Python work is executed in Docker; the SDK is never installed in or passed to the sandbox.

## Task Configuration and Model-Run Configuration

Task content and model execution settings are separate experimental dimensions.

The task YAML contains sandbox policy, input assets, tool policy, and an optional
provider-independent `agent` section. It does not contain a provider, model ID, API key,
temperature, or retry settings. Existing Phase 1 and Phase 2 task files without `agent` continue
to load and work.

The Phase 3A example uses:

```yaml
agent:
  instruction_file: task.md
  system_prompt_version: standard_agent_v1
  maximum_model_turns: 6
  maximum_provider_requests: 8
  maximum_total_input_tokens: 20000
  maximum_total_output_tokens: 5000
  maximum_total_tokens: 25000
  overall_wall_time_seconds: 90
  required_submission: true
  final_text_without_submission: incomplete
```

`instruction_file` and an optional `system_prompt_file` must be safe task-relative UTF-8
files. With no custom prompt file, `standard_agent_v1` resolves to the packaged,
provider-neutral prompt in `src/oneoxygen_sandbox/prompts/standard_agent_v1.txt`.

`ModelRunConfig` is supplied independently by the caller; `agent-run` builds one from its
standard CLI options. The model contains:

- provider and required model ID;
- maximum output tokens per provider response;
- optional temperature;
- per-attempt model-call timeout;
- maximum central retry attempts and initial retry delay;
- bounded provider settings for programmatic callers (the standardized Phase 3A adapters reject
  non-empty provider settings);
- tool-schema mode; and
- the provider-response storage request.

All values are validated in immutable Pydantic models. Adapters reject unsupported settings with
`unsupported_parameter`; they do not silently drop them. Requested configuration and effective
adapter settings are recorded separately in `run.json`. No model name is hard-coded as a
default.

## Agent Lifecycle

For each agent run, One Oxygen:

1. Validates the task, instruction, prompt, tool policy, and model-run configuration.
2. Creates a fresh `SandboxSession`, copies declared inputs, and starts one hardened container.
3. Starts provider conversation state in the host adapter.
4. Sends the versioned system prompt, task instruction, allowed canonical tools, and previous
   turn's tool results.
5. Normalizes and records the model response before acting on it.
6. Rejects duplicate call IDs and executes returned calls sequentially in provider order.
7. Sends bounded, sanitized `ToolResult` values back through the adapter.
8. Repeats until submission or an explicit stop condition.
9. Stops the sandbox before artifact copying.
10. On successful submission, copies exactly the submitted output files and verifies their sizes
    and hashes. Unsubmitted files are not retained by an agent run.
11. Persists the final `RunRecord`, removes the container, and deletes the temporary workspace.

Several calls may be returned in one model turn. They are never executed concurrently. If
`submit_result` succeeds, later calls in the same response are still traced but rejected with
`already_submitted`.

Legacy `run` and `tool-demo` workflows retain their Phase 1/2 behavior and collect approved
files from the configured output directory.

## Model Adapter Contract

An adapter exposes:

- a stable `provider` identifier;
- factual `ModelCapabilities`;
- `validate_config()`;
- `start_conversation()`;
- `generate_next_turn()`; and
- `close()`.

The common request carries only the turn number, system prompt, initial instruction, canonical
tool definitions, previous tool results, and normalized run configuration. It never carries API
keys or host paths.

The normalized response carries requested and returned model IDs, bounded text, ordered tool
calls, a stable finish reason, normalized token usage, latency, retry attempts, warnings, and
allowlisted provider metadata. Provider-specific conversation state remains private to the
adapter so opaque response items can be round-tripped without flattening them into a generic
chat-message history.

The default registry includes:

- `scripted`, available in the base installation; and
- `openai`, available when the optional `openai` dependency is installed.

Listing adapters is deterministic, makes no network request, and does not import the optional
OpenAI SDK.

## Scripted Adapter

`ScriptedModelAdapter` is the deterministic primary test adapter. It needs no API key, SDK, or
network access. A versioned YAML or JSON script can define text, multiple tool calls, normalized
finish reasons, synthetic usage, previous-result expectations, simulated transient or permanent
provider errors, and simulated timeouts.

Run the complete synthetic due-diligence example through the real runner, dispatcher, persistent
Docker sandbox, submission flow, and artifact collector:

```powershell
python -m oneoxygen_sandbox agent-run examples/agent_demo/task.yaml `
  --provider scripted `
  --model scripted-demo `
  --script examples/agent_demo/model_script.yaml
```

If `--script` is omitted for the scripted provider, the CLI uses `model_script.yaml` beside
the task YAML. The example inspects synthetic company metrics, calculates revenue growth and
gross margin, creates `/workspace/output/findings.md`, and submits it.

## OpenAI Responses API Adapter

OpenAI support is optional:

```powershell
python -m pip install -e ".[dev,openai]"
```

The adapter uses the official Python SDK's Responses API, following the current
[Responses migration guide](https://developers.openai.com/api/docs/guides/migrate-to-responses)
and [function-calling guide](https://developers.openai.com/api/docs/guides/function-calling).
It does not use the Assistants API or the OpenAI Agents SDK.

For every request, the adapter:

- calls `client.responses.create` with the caller's model ID;
- sends only One Oxygen's custom function definitions;
- never enables web search, file search, code interpreter, computer use, MCP, or another hosted
  tool;
- sets `store=False` and does not use `previous_response_id`;
- requests encrypted reasoning content and locally replays all required output items when
  continuing a stateless conversation;
- correlates `function_call` and `function_call_output` items by provider `call_id`;
- parses function arguments as strict JSON and never repairs malformed arguments heuristically;
- serializes bounded tool results as `success`, `content`, and sanitized `error`;
- maps available input, output, reasoning, cached-input, and total-token fields without inventing
  missing values; and
- records both requested and returned model identifiers.

`parallel_tool_calls=True` allows the model to return multiple calls in a response. One Oxygen
still executes those calls deterministically and sequentially.

The OpenAI SDK's automatic retries are disabled with `max_retries=0`. One Oxygen supplies the
configured request timeout and owns all retry decisions and attempt tracing.

### API Key Setup in PowerShell

Set the key only in the host process:

```powershell
$env:OPENAI_API_KEY = "<YOUR_OPENAI_API_KEY>"
python -m oneoxygen_sandbox models doctor --provider openai
python -m oneoxygen_sandbox agent-run examples/agent_demo/task.yaml `
  --provider openai `
  --model "<MODEL_ID>"
```

The repository includes `.env.example`, but One Oxygen does not automatically load `.env`
files. Actual `.env` files are ignored by Git. Never put a key in task YAML, CLI arguments,
model settings, scripts, tool arguments, or run records.

## Portable and Native Tool Schemas

`portable` is the default standardized mode. It translates the Phase 2 `ToolDefinition`
envelope without changing tool names, descriptions, logical fields, required fields, allowed
values, or meanings. For OpenAI, portable mode explicitly uses non-strict function schemas so
future cross-provider comparisons do not receive an OpenAI-only schema advantage.

`native_strict` is reserved as a future provider-native mode, but no Phase 3A adapter claims to
implement it. Both OpenAI and scripted runs reject it with `unsupported_parameter`. A future
implementation must transform and validate the canonical schemas without changing their logical
meaning, record the mode explicitly, and keep its results separate from the portable track.

## Limits, Stop Conditions, and Retries

The task controls maximum model turns, provider requests, cumulative input tokens, cumulative
output tokens, cumulative total tokens, and overall agent wall time. The model-run configuration
controls maximum output tokens per response, model-call timeout, and central retry behavior.
Sandbox, command, tool-call, file-size, CPU, memory, and PID limits remain independently enforced.

Token limits are checked after each recorded response. If usage is absent, fields remain null and
the record warns that exact enforcement was unavailable; One Oxygen does not silently estimate
tokens.

Agent terminal statuses include:

- `succeeded`: a valid submission completed, or explicitly permitted final text completed;
- `incomplete`: final text arrived without a required submission;
- `refused`: the model refused or content filtering stopped the response;
- `limit_exceeded`: a turn, provider-request, token, output-length, or wall-time limit stopped
  the run;
- `provider_error`: provider failures exhausted the allowed policy;
- `sandbox_error`: sandbox startup, execution, artifact verification, or cleanup failed;
- `cancelled`: user interruption or provider cancellation; and
- `internal_error`: unexpected orchestration failure.

One Oxygen centrally retries only errors classified as transient: rate limits, request timeouts,
connection failures, provider unavailability, and retryable server failures. It uses bounded
exponential backoff with injectable jitter and sleep functions. Authentication, permission,
invalid-request, unsupported-parameter, malformed-response, malformed-argument, and
context-limit failures are not retried. Every attempt records its category, retryability, delay,
timestamps, and latency.

## Tool Protocol and Policy

Available provider-neutral tools are:

- `list_files`: list bounded workspace entries without following symbolic links.
- `read_text_file`: read bounded UTF-8 text with optional line ranges.
- `write_text_file`: atomically write UTF-8 text.
- `replace_text`: atomically replace exact text only when the expected count matches.
- `execute_shell`: execute a shell command inside the active sandbox container.
- `execute_python`: execute a temporary Python source file inside the active container.
- `submit_result`: submit final findings and approved output artifacts.

`execute_shell` and `execute_python` are disabled unless explicitly enabled by the task's
`ToolPolicy`. The default policy permits bounded file operations and submission.

Example:

```yaml
tool_policy:
  allowed_tool_names:
    - list_files
    - read_text_file
    - execute_python
    - write_text_file
    - submit_result
  max_total_tool_calls: 10
  per_tool_call_limits:
    execute_python: 1
    submit_result: 1
  max_read_size_bytes: 65536
  max_write_size_bytes: 65536
  max_file_list_entries: 100
  python_timeout_seconds: 10
  max_tool_result_size_bytes: 32768
  shell_execution_allowed: false
  python_execution_allowed: true
  protected_workspace_paths:
    - .oneoxygen
    - .oneoxygen/tool-runtime
```

Tool schemas are sorted deterministically. Tool arguments and results are bounded and hashed in
the trace; sensitive large content such as written file bodies and Python source is replaced by
its size and SHA-256 digest.

## Security and Fairness Guarantees

Each run uses:

- one fresh private temporary workspace;
- a non-root numeric container user;
- disabled container networking;
- a read-only root filesystem;
- one writable `/workspace` bind mount;
- a hardened in-memory `/tmp`;
- all Linux capabilities dropped and `no-new-privileges`;
- CPU, memory, PID, command, tool, and time limits;
- no Docker socket or repository mount; and
- cleanup on success, failure, refusal, limit, interruption, and timeout.

Additional Phase 3A guarantees are:

- provider SDKs and credentials stay in the host orchestrator;
- provider secret names, including `OPENAI_API_KEY`, cannot be allowlisted into Docker;
- only canonical custom functions are offered to models;
- every model-requested tool call passes through the same dispatcher and sandbox policy;
- multiple calls mutate the shared workspace sequentially in provider order;
- tool and model errors are sanitized before model-facing output, CLI output, or persistence;
- provider metadata is bounded; the OpenAI adapter records only allowlisted, sanitized fields;
- response bodies, SDK objects, HTTP headers, authorization values, host paths, and unrestricted
  stack traces are not persisted;
- only successfully submitted agent artifacts are retained; and
- no monetary cost is calculated because pricing is intentionally outside Phase 3A.

## Tracing and Reproducibility

Retained runs are written as:

```text
runs/<run-id>/
|-- run.json
`-- artifacts/
    `-- ...successfully submitted agent artifacts...
```

The version 3 `RunRecord` preserves the Phase 1/2 sandbox policy, command results, tool policy,
tool events, submission, final status, errors, and artifact hashes. Agent runs add:

- requested model configuration and effective settings;
- versioned `ModelEvent` entries with attempt history;
- requested and returned model IDs;
- normalized finish reasons and original provider status metadata;
- bounded model text plus its complete SHA-256 hash;
- ordered normalized tool calls and original indexes;
- usage, latency, warnings, and sanitized provider request IDs;
- system-prompt version, bounded content, and complete hash;
- task-instruction hash and canonical tool-definition hash;
- termination reason; and
- aggregate model, provider-attempt, tool, token, latency, and wall-time metrics.

Missing provider usage remains unavailable rather than being changed to zero. No raw provider
object, API key, unrestricted response, or host workspace is retained.

## CLI

Check and build Docker:

```powershell
python -m oneoxygen_sandbox doctor
python -m oneoxygen_sandbox build
```

Preserved Phase 1 and Phase 2 commands:

```powershell
python -m oneoxygen_sandbox run examples/basic/task.yaml
python -m oneoxygen_sandbox tools list
python -m oneoxygen_sandbox tools list --json
python -m oneoxygen_sandbox tool-demo examples/tool_demo/task.yaml
```

Inspect adapters without a network request:

```powershell
python -m oneoxygen_sandbox models list
python -m oneoxygen_sandbox models list --json
python -m oneoxygen_sandbox models doctor --provider scripted
python -m oneoxygen_sandbox models doctor --provider openai
```

Agent runs use distinct non-zero exit codes for invalid configuration, unavailable providers,
missing API keys, provider errors or refusals, incomplete runs, limit exhaustion, sandbox
failures, cancellation, and internal errors.

## Setup

PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m oneoxygen_sandbox doctor
python -m oneoxygen_sandbox build
```

Install OpenAI support only when needed:

```powershell
python -m pip install -e ".[dev,openai]"
```

Linux shell:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
python -m oneoxygen_sandbox doctor
python -m oneoxygen_sandbox build
```

Docker Desktop on Windows must be running Linux containers. The application does not depend on
Bash scripts.

## Tests

Run unit and mocked OpenAI tests with no Docker, network, or paid API call:

```powershell
python -m pytest -m "not integration and not live_api"
```

Run Docker integration tests while explicitly excluding live API tests:

```powershell
python -m pytest -m "integration and not live_api"
```

Run the complete local suite without live API usage:

```powershell
python -m pytest -m "not live_api"
```

The OpenAI adapter tests inject a mocked client and verify text, function calls, local output-item
replay, usage and model mapping, error normalization, strict JSON parsing, no hosted tools,
default `store=False` plus explicit storage forwarding, disabled SDK retries, and secret exclusion.
They make no external request. The repository's default pytest options deselect `live_api`, so an
ordinary `pytest` command cannot make a paid request even when live-test environment variables are
present.

Live OpenAI testing is marked `live_api`, requires a running Linux Docker engine, warns that it
incurs API usage, and requires all three host environment variables:

```powershell
$env:OPENAI_API_KEY = "<YOUR_OPENAI_API_KEY>"
$env:ONEOXYGEN_RUN_LIVE_TESTS = "1"
$env:ONEOXYGEN_LIVE_MODEL = "<MODEL_ID>"
python -m pytest tests/live -m live_api
```

Without an API key, the explicit `ONEOXYGEN_RUN_LIVE_TESTS=1` opt-in, and a caller-selected model
ID, the live test skips. No current model ID is hard-coded.

Format, lint, and check patch whitespace:

```powershell
python -m ruff format .
python -m ruff check .
git diff --check
```

## Current Limitations

- Only Linux containers are supported.
- Execution is local, synchronous, single-host, and non-streaming.
- Only the scripted and OpenAI adapters exist in Phase 3A.
- OpenAI provider-side response storage is disabled by default; an explicit
  `--store-provider-response` request is forwarded and recorded. Conversation state is still
  reconstructed locally rather than relying on `previous_response_id`.
- The standardized track has no seed option and no provider-specific generation settings.
- Model-specific parameter support can differ; explicit provider rejection is normalized rather
  than hidden by a hard-coded model capability table.
- Exact token-limit enforcement depends on the provider returning usage.
- Overall wall time is enforced at orchestration boundaries, and each OpenAI request timeout is
  reduced to the remaining wall time. Transport cancellation and scheduling can add small
  overhead after a deadline, but no later tool or provider request is started.
- There is no cost calculation, pricing table, grading, benchmark scoring, RAG, database,
  financial-document tooling, spreadsheet tooling, browser automation, or web UI.
- Retained run records and artifacts are not encrypted or signed.
- Base-image provenance is tag-based; deployments needing bit-for-bit provenance should mirror
  and digest-pin the image.

## Phase 3B Preview

Phase 3B is deferred. Its exact planned scope is additional provider adapters for Anthropic,
Gemini, xAI, and DeepSeek using the Phase 3A contract and runner. None of those adapters is
implemented or claimed by this repository, and Phase 3A does not begin that work.
