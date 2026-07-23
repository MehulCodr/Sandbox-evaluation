# One Oxygen Sandbox

One Oxygen is a local, provider-neutral agent runner built in phases:

- Phase 1 provides a hardened, persistent Docker sandbox.
- Phase 2 provides deterministic tools, policy enforcement, and bounded tool traces.
- Phase 3A adds a provider-independent model contract, a deterministic agent loop, a
  network-free scripted adapter, and an optional OpenAI Responses API adapter.
- Phase 3B adds explicit inference transports and provenance, official OpenAI Batch execution,
  durable turn-level suspension, immutable workspace checkpoints, a deterministic mock batch
  backend, and an experimental Api.Airforce direct gateway.
- Browser integration adds opt-in, provider-neutral, host-side web research against exact
  reviewed public-source profiles while preserving disabled networking in the execution
  container.

Phase 3B implements official batch execution only for OpenAI. Anthropic, Gemini, xAI, and
DeepSeek batch backends remain intentionally deferred. It does not use an agent framework, the
Assistants API, the OpenAI Agents SDK, hosted provider tools, RAG, graders, a web UI, production
cloud infrastructure, or distributed workers.

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
- `SecureBrowserClient`: provides host-side, read-only retrieval from exact selected public
  HTTPS hosts while the execution container remains network-disabled.
- `ToolRegistry` and `ToolDispatcher`: expose canonical provider-neutral schemas, enforce
  `ToolPolicy`, execute calls sequentially, normalize errors, track submission, and write
  bounded `ToolEvent` records.
- `ModelAdapter`: translates one provider's private conversation representation to and from
  `ModelTurnRequest`, `ModelTurnResponse`, and the canonical tool protocol.
- `ModelAdapterRegistry`: registers adapter factories deterministically and checks optional
  dependencies without importing SDKs or contacting providers during listing.
- `AgentRunner`: enforces turns, provider requests, tokens, wall time, retries, finish states,
  artifact-selection rules, and cleanup across every runtime exit path.
- `DurableAgentCoordinator`: persists ready turns and validated state transitions in SQLite,
  submits compatible turns through a `BatchBackend`, and resumes each run independently.
- `WorkspaceCheckpoint`: finalizes bounded immutable generations with a SHA-256 manifest and
  restores a logical workspace into a new hardened container for the next tool round.

The model API client and its credentials remain in the host process. Only model-requested shell
and Python work is executed in Docker; the SDK is never installed in or passed to the sandbox.

## Task Configuration and Model-Run Configuration

Task content and model execution settings are separate experimental dimensions.

The task YAML contains sandbox policy, input assets, tool policy, an optional
provider-independent `agent` section, and an optional exact-source `browser` section. It does not
contain a provider, model ID, API key, temperature, or retry settings. Existing Phase 1 and Phase
2 task files without `agent` or `browser` continue to load and work.

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

## Adding or Selecting a Model

Model IDs are runtime inputs, not entries in the repository. To run a model through an already
supported provider, install its optional SDK if needed, keep the API key in the host environment,
and pass the exact provider/model pair:

```powershell
python -m oneoxygen_sandbox models list
python -m oneoxygen_sandbox models doctor --provider openai
python -m oneoxygen_sandbox agent-run path/to/task.yaml `
  --provider openai `
  --model "<PROVIDER_MODEL_ID>"
```

The task YAML remains provider-independent, so the same task, browser source profiles, prompt,
tool policy, and scoring conditions can be reused with another adapter. `ModelRunConfig` records
the provider, requested model ID, inference transport, output limit, timeout, retry policy,
sampling settings, schema mode, and provider-storage request. No provider key or default model ID
is stored in the task.

Adding a new provider such as a direct Anthropic/Claude adapter requires these code-level
extension points:

1. Add its stable value to `ModelProvider` and define its allowed transport/provenance rules in
   `ModelRunConfig`.
2. Implement `ModelAdapter` in `src/oneoxygen_sandbox/model_adapters/<provider>.py`. The adapter
   validates configuration, declares factual capabilities, owns private conversation state,
   converts canonical `ToolDefinition` values to the provider's function/tool format, and
   normalizes provider output into `ModelTurnResponse`.
3. Keep authentication and SDK objects inside the host adapter. Never place credentials in task
   YAML, prompts, tool calls, Docker environment variables, or run records.
4. Add a lazy factory to `model_adapters/registry.py` and register it in
   `default_model_adapter_registry()`, with an optional dependency name when an SDK is required.
   Listing providers must continue to work without importing the SDK or making a network call.
5. Export the adapter where appropriate, add its optional package dependency and CLI credential
   checks, and add mocked contract tests for schemas, tool calls, usage, finish reasons, retries,
   timeouts, malformed output, sanitization, and secret separation.
6. Implement a separate `BatchBackend` only if the provider has a supported asynchronous batch
   API. A direct adapter does not automatically create batch support.

An adapter must expose only One Oxygen's canonical custom tools. Provider-hosted web search,
computer use, code execution, file search, MCP, or similar hosted tools are not silently enabled.
The browser integration therefore works unchanged for any new adapter: the provider sees
`browser_sources` and `browser_open` as ordinary canonical function definitions when the task
allows them, and their calls return through the same dispatcher as every other model.

## End-to-End Evaluation Lifecycle

The direct evaluation path is:

```mermaid
flowchart TD
    A[Task YAML, instruction, and declared assets] --> D[Validated SandboxTask]
    B[Provider, model ID, host credential, run options] --> C[ModelRunConfig]
    C --> E[ModelAdapterRegistry creates adapter]
    D --> F[AgentRunner]
    E --> F
    F --> G[SandboxSession and network-disabled container]
    F --> H[ModelTurnRequest with prompt and allowed tools]
    H --> I[Provider adapter and model API]
    I --> J[Normalized ModelTurnResponse]
    J --> K[ModelEvent recorded before action]
    K --> L{Tool calls returned?}
    L -- Yes --> M[ToolDispatcher validates and executes sequentially]
    M --> N[Bounded ToolResult and ToolEvent]
    N --> H
    L -- No or terminal --> O[Termination and cleanup]
    O --> P[Atomic run.json and approved artifacts]
```

For each run, One Oxygen performs the following:

1. `load_task()` parses the task YAML into immutable models and validates sandbox paths, agent
   limits, tool permissions, data classification, and optional browser configuration.
2. The CLI builds `ModelRunConfig` from `--provider`, `--model`, transport, timeout, retry, output,
   temperature, schema, and provider-storage options.
3. `ModelAdapterRegistry` resolves a registered provider, verifies its optional SDK, and creates
   the adapter. The adapter validates the requested settings before any provider call.
4. `AgentRunner` safely reads the instruction and system prompt. For browser-enabled tasks it
   appends the selected source profiles, exact hosts, and untrusted-web-content rule.
5. The runner filters the canonical registry to only the definitions allowed by `ToolPolicy`,
   hashes that exact schema set, and verifies that a required submission tool is present.
6. `SandboxSession` creates the initial version 4 `RunRecord`, a private temporary workspace, and
   one hardened network-disabled container. Declared input assets are copied into the workspace;
   provider credentials are not.
7. The runner initializes host-side adapter conversation state and sends a `ModelTurnRequest`
   containing the turn number, prompt, instruction, allowed tool definitions, normalized run
   configuration, and any results from the preceding turn.
8. The adapter translates that request to its provider API. One Oxygen applies the remaining wall
   time to the request timeout and centrally retries only classified transient failures.
9. Provider output is normalized and identity-checked for provider, model, transport, host, and
   provenance. Inconsistent routes are rejected before the response can be accepted.
10. Before executing any requested tool, the normalized response is appended as a `ModelEvent`
    containing bounded text, its complete SHA-256, ordered tool-call traces, finish reason, usage,
    attempts, latency, warnings, sanitized provider metadata, and requested/returned model IDs.
11. Duplicate call IDs are rejected. Otherwise, if the response contains tool calls,
    `ToolDispatcher` validates their arguments and processes them sequentially in provider order.
    Every returned call consumes the configured call budget, including unknown, invalid,
    disallowed, or over-limit calls. Required-submission reachability is rechecked as budgets are
    consumed.
12. Each call becomes a bounded `ToolResult` for the next model turn and a hashed `ToolEvent` for
    the run record. Model-visible failures use normalized error codes without internal paths,
    secrets, SDK objects, or stack traces.
13. The loop continues with the same workspace until successful submission, final text, refusal,
    content filtering, a provider failure, model/tool/token/turn/time limit, sandbox failure,
    cancellation, or an internal orchestration error.
14. A successful `submit_result` closes submission state. Later calls in the same provider
    response are still recorded but fail with `already_submitted`.
15. The container is stopped before artifact collection. Only explicitly submitted files under
    the permitted output directory are copied, size-checked, and SHA-256 verified.
16. Terminal status, reason, metrics, errors, timestamps, and artifact metadata are finalized.
    `run.json` is written atomically, the adapter is closed, the container is removed, and the
    temporary workspace is deleted on every terminal path.

Provider-batch evaluations use `eval enqueue` and `DurableAgentCoordinator` instead of keeping a
direct call open. The coordinator persists ready-turn state in SQLite, materializes compatible
batch requests, restores immutable workspace checkpoints before tool execution, applies results
by internal request ID, and eventually produces the same normalized model/tool events and final
run record. Only OpenAI currently has an official provider-batch backend.

### Tools a model can call

The model receives only definitions allowed by the task. Registration alone does not grant
permission.

| Tool | Availability and execution boundary |
|---|---|
| `list_files` | Default. Lists bounded workspace entries through `SecureWorkspace`; symbolic links and protected paths are excluded. |
| `read_text_file` | Default. Reads bounded UTF-8 workspace text with optional line ranges. |
| `write_text_file` | Default. Atomically writes bounded UTF-8 workspace files subject to path and overwrite rules. |
| `replace_text` | Default. Atomically performs an exact replacement only when the expected match count is satisfied. |
| `submit_result` | Default. Submits the final summary, optional structured findings, and explicit output artifact paths. |
| `execute_shell` | Optional. Runs inside the hardened container only when the tool name is allowed and `shell_execution_allowed` is true. Container networking remains disabled. |
| `execute_python` | Optional. Runs a temporary source file inside the hardened container only when allowed and `python_execution_allowed` is true. |
| `browser_sources` | Optional browser tool. Returns selected profiles, exact hosts, and policy hash without a network request. |
| `browser_open` | Optional browser tool. Performs a host-side, read-only request to one exact selected HTTPS host; it is unavailable without validated browser configuration. |

The model cannot call Docker, raw sockets, a generic HTTP client, provider SDKs, arbitrary host
filesystem APIs, browser DevTools, or any tool not present in its request. Shell or Python cannot
bypass the browser policy because those tools execute in the network-disabled container.

### How responses and artifacts are stored

Local normalized storage and provider-side storage are separate:

| Data | Storage behavior |
|---|---|
| Model response | Stored locally as a bounded, sanitized `ModelEvent` in `runs/<run-id>/run.json`. It includes response text up to the event limit, a SHA-256 of the complete original text, tool-call traces, usage, timing, finish reason, requested/returned model, attempts, warnings, and allowlisted metadata. |
| Tool call and result | Stored as a bounded `ToolEvent` with sequence, call ID, tool name, redacted/bounded arguments, status, normalized error code, timing, result preview, and complete argument/result hashes. The bounded `ToolResult` is also returned to the model on the next turn. |
| Browser response | Stored through the same `ToolEvent` path as bounded extracted text and metadata. Raw unrestricted response bodies, headers, cookies, and browser caches are not retained. URL query values in traces are replaced by size and SHA-256. |
| Prompt and task | The bounded system prompt and complete prompt hash are stored. The task instruction is represented by its SHA-256; host task paths are not persisted. |
| Configuration and provenance | Stores task/configuration hashes, requested and effective model settings, adapter route, API host, inference transport, provenance, browser configuration, exact allowed hosts, and browser policy hash. |
| Submission | Stores the submitted summary, optional structured findings, requested artifact paths, and verified artifact metadata. |
| Files | Only successfully submitted files are retained under `runs/<run-id>/artifacts/`. Other temporary workspace files are deleted during cleanup. |
| Metrics and terminal outcome | Stores aggregate model turns, provider attempts, tool counts, token fields when reported, latency, total wall time, final status, termination reason, sanitized error, and timestamps. |
| Provider-side response | Disabled by default. `--store-provider-response` requests provider storage only when the adapter supports it; for OpenAI the normal default is `store=False`. This option does not change the bounded local run record. |
| Secrets and raw provider objects | Never stored. API keys, authorization headers, unrestricted SDK responses, stack traces, Docker secrets, and host workspace paths are excluded or sanitized. |

The direct runner accumulates normalized events in the in-memory `RunRecord` and atomically writes
the complete JSON record during terminal session cleanup. Durable/batch runs additionally persist
coordinator state, batch correlation data, and immutable workspace checkpoint generations so a
run can resume without treating provider-private response objects as benchmark evidence.

Legacy `run` and `tool-demo` workflows retain their Phase 1/2 behavior and collect approved files
from the configured output directory.

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

- `scripted`, available in the base installation;
- `openai`, available when the optional `openai` dependency is installed; and
- `airforce`, the explicitly acknowledged experimental Api.Airforce gateway, which also uses the
  optional OpenAI-compatible SDK.

Listing adapters is deterministic, makes no network request, and does not import the optional
provider SDK.

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
- `browser_sources`: inspect the task-selected public source profiles and exact HTTPS hosts
  without making a network request.
- `browser_open`: retrieve one allowlisted HTTPS text page through the host-side browser broker.
- `submit_result`: submit final findings and approved output artifacts.

`execute_shell` and `execute_python` are disabled unless explicitly enabled by the task's
`ToolPolicy`. Browser tools require an explicit public/synthetic browser configuration and remain
disabled by the default policy. The default policy permits bounded file operations and submission.

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

## Browser Integration

The browser integration is implemented as host-side canonical tools rather than a provider-hosted
search feature. A model requests `browser_sources` or `browser_open`; `ToolDispatcher` applies the
task policy; and `SecureBrowserClient` performs a bounded read outside the network-disabled
execution container. This keeps browser behavior consistent across model providers and prevents
model API credentials from entering either the browser request or the container.

The current OpenAI Responses, Api.Airforce, and scripted adapters receive the same browser tool
definitions through `ModelTurnRequest`. The repository does not yet contain a direct
Anthropic/Claude adapter. A future Claude or other provider adapter implementing the existing
`ModelAdapter` contract receives the same canonical tools without browser-specific provider code.

The implemented backend is a text browser for HTML, JSON, XML, XBRL, and plain text. It does not
render JavaScript, launch a GUI browser, take screenshots, retain cookies, fill forms, upload
files, download binary documents, or expose a general HTTP client.

### Enabling browser access

Browser access is off by default. A task must select immutable source profiles, explicitly allow
`browser_open`, classify agent data as `public` or `synthetic`, and supply a truthful
publisher-facing user agent:

```yaml
tool_policy:
  allowed_tool_names:
    - list_files
    - read_text_file
    - write_text_file
    - browser_sources
    - browser_open
    - submit_result
  max_total_tool_calls: 30
  per_tool_call_limits:
    browser_open: 12

browser:
  mode: live_web
  source_profiles:
    - sec_edgar
    - us_macro
  request_timeout_seconds: 20
  maximum_redirects: 5
  maximum_response_size_bytes: 5242880
  maximum_text_characters: 60000
  maximum_links: 100
  requests_per_second: 2
  user_agent: "BenchmarkName/1.0 diligence-contact@example.com"

agent:
  instruction_file: task.md
  data_classification: public
```

`browser_sources` is optional but useful for allowing the model to inspect the selected profiles.
`browser_open` is mandatory whenever a `browser` block exists. Browser tools without browser
configuration fail task validation. Browser configuration on an agent task with missing,
`internal`, `confidential`, or `restricted` classification also fails validation. Existing tasks
without a browser block retain their previous configuration hash and behavior.

The defaults shown above apply unless a task chooses stricter values. `source_profiles` must
contain at least one built-in `BrowserSourceProfile`; duplicate profiles are removed. The model
cannot add hosts, supply headers or credentials, change the HTTP method, or expand the policy
through tool arguments.

### Built-in U.S. financial-due-diligence sources

Profiles contain exact hosts, not suffix wildcards. Selecting `sec_edgar` does not allow every
SEC or `.gov` subdomain.

| Profile | Exact allowed hosts | Typical diligence use |
|---|---|---|
| `sec_edgar` | `sec.gov`, `www.sec.gov`, `data.sec.gov`, `efts.sec.gov` | SEC filings, exhibits, submissions, filing search, and XBRL company facts. |
| `us_macro` | `fred.stlouisfed.org`, `api.stlouisfed.org`, `www.bls.gov`, `download.bls.gov`, `www.bea.gov`, `apps.bea.gov`, `www.census.gov`, `api.census.gov`, `data.census.gov`, `fiscaldata.treasury.gov`, `api.fiscaldata.treasury.gov` | Macroeconomic, labor, industry, demographic, and fiscal data. |
| `regulated_financial` | `banks.data.fdic.gov`, `www.ffiec.gov`, `www.occ.gov`, `www.consumerfinance.gov`, `files.consumerfinance.gov` | Bank identity, structure, financial trends, enforcement, and complaint data. |
| `federal_counterparty` | `sam.gov`, `api.sam.gov`, `open.gsa.gov`, `usaspending.gov`, `www.usaspending.gov`, `api.usaspending.gov` | Federal entity registration, exclusions, contracts, grants, and awards. |
| `ofac_sanctions` | `ofac.treasury.gov`, `sanctionssearch.ofac.treas.gov`, `sanctionslist.ofac.treas.gov` | OFAC sanctions-list screening and follow-up review. |
| `antitrust` | `www.ftc.gov`, `www.justice.gov` | FTC and DOJ antitrust cases, proceedings, and merger materials. |
| `workplace_environment` | `echo.epa.gov`, `www.osha.gov` | EPA facility enforcement and OSHA establishment-inspection data. |
| `us_ip` | `www.uspto.gov`, `ppubs.uspto.gov`, `data.uspto.gov`, `tsdr.uspto.gov`, `tmsearch.uspto.gov` | Patent and trademark search, status, and public documents. |
| `tax_exempt` | `www.irs.gov`, `apps.irs.gov` | Exempt-organization status and public Form 990 material. |
| `healthcare_public` | `www.fda.gov`, `open.fda.gov`, `api.fda.gov`, `www.cms.gov`, `data.cms.gov` | FDA approvals, recalls, safety data, providers, and reimbursement. |
| `energy_public` | `www.eia.gov`, `api.eia.gov`, `www.ferc.gov`, `elibrary.ferc.gov` | Energy data, tariffs, orders, and filings. |
| `telecom_public` | `www.fcc.gov`, `publicfiles.fcc.gov` | FCC licenses, proceedings, ownership reports, and public files. |

Licensed platforms, fee-bearing court systems, state registries with anti-automation terms,
general search engines, news sites, social networks, issuer sites, and user-supplied origins are
not enabled by these profiles. Some public APIs require publisher keys; key-required routes remain
unavailable because the current browser broker does not inject credentials. The source research,
licensed-platform review, and rules for adding future profiles are documented in
[the detailed browser integration specification](#browser-integration-for-us-financial-due-diligence-evaluations).

### Browser request and response policy

Every `browser_open` call enforces the following:

- only absolute `https://` URLs using port 443 and an exact selected hostname are accepted;
- user information, passwords, IP literals, wildcard hosts, unlisted subdomains, backslashes,
  whitespace, control characters, and non-default ports are rejected; fragments are removed;
- DNS is resolved by the broker, every answer must be globally routable, and mixed
  public/private results fail closed;
- the validated IP address is pinned for the connection while the original hostname remains in
  TLS SNI and certificate verification;
- every redirect is checked against the same policy before it is followed;
- requests use only `GET`, a fixed safe header set, no cookies, no session state, no body, and the
  task's declared user agent;
- time, redirect, response-byte, extracted-text, link, total-tool-call, per-tool-call, and
  per-host rate limits are enforced;
- only supported text content is returned, scripts and style content are omitted from HTML
  extraction, and returned links must pass the same exact-host policy; and
- page content is marked as untrusted evidence so web instructions cannot change the task,
  system prompt, tool policy, or source catalog.

A successful result contains the requested and final URL, redirect chain, HTTP status, content
type, title, bounded text, same-policy links, matching source profile, UTC retrieval timestamp,
captured byte count and SHA-256, truncation flags, and an explicit untrusted-content warning.
Unsupported content, network failure, blocked URLs, missing browser configuration, and redirect
exhaustion return normalized browser tool error codes.

Browser query values are removed from persisted tool-call arguments and replaced with their byte
count and SHA-256. `RunRecord` schema version 4 additionally stores the validated
`browser_configuration`, sorted `browser_allowed_hosts`, and `browser_policy_sha256`. The policy
digest covers the browser configuration, policy version, selected profiles, and exact hosts.

### Chrome, Firefox, Brave, and other desktop-browser policies

The policy compiler derives deterministic baselines for `chrome`, `chromium`, `edge`, `brave`,
`firefox`, `safari`, `opera`, and `vivaldi` from the same exact-host source catalog.

| Browser family | Generated controls |
|---|---|
| Chrome, Chromium, Edge, Opera, Vivaldi | Deny-all URL blocklist, exact-host allowlist, fixed loopback proxy, empty bypass list, QUIC and browser DNS-over-HTTPS disabled, DevTools disabled, sign-in/sync/password/autofill/search restrictions, and an extension-install blocklist. |
| Brave | Chromium controls plus Tor, Rewards, Wallet, VPN, and AI chat disablement. |
| Firefox | Deny-all `WebsiteFilter` with exact-host exceptions, locked manual proxy and proxy DNS, DNS-over-HTTPS disabled, and account/private-browsing/password/autofill restrictions. |
| Safari | An explicit MDM input manifest requiring a Safari WebExtension declarative ruleset and device network filter. It is not emitted as an installable profile. |

Inspect the source catalog and compile a policy without making a network request:

```powershell
python -m oneoxygen_sandbox browser sources
python -m oneoxygen_sandbox browser policy `
  --family brave `
  --profiles sec_edgar,ofac_sanctions `
  --proxy-server http://127.0.0.1:8765 `
  --user-agent "BenchmarkName/1.0 diligence-contact@example.com"
```

The policy command accepts only a loopback HTTP proxy with an explicit port and emits JSON with
the allowed hosts, source-policy hash, browser-family policy, and complete bundle hash. It does
not install policy, package an extension, launch a browser, or verify effective policy. A
JavaScript-capable managed-browser backend still requires an authoritative egress proxy and
network namespace, ephemeral profiles, extension/enterprise-policy deployment, download
quarantine, complete network tracing, and browser-specific conformance tests.

### Browser integration file map

| File | Browser-integration responsibility |
|---|---|
| [README.md — detailed browser integration section](#browser-integration-for-us-financial-due-diligence-evaluations) | Full source research, threat model, architecture, public and licensed source rules, cross-browser deployment specification, evidence policy, and implementation status. |
| `src/oneoxygen_sandbox/browser.py` | Immutable source catalog, exact URL validation, policy hashing and prompt appendix, DNS checks, pinned TLS client, redirect enforcement, rate limiting, text decoding, HTML extraction, and same-policy link filtering. |
| `src/oneoxygen_sandbox/browser_policies.py` | Deterministic Chrome-family, Brave, Firefox, and Safari policy-bundle compilation with loopback-proxy validation and bundle hashing. |
| `src/oneoxygen_sandbox/tools/browser.py` | Canonical `browser_sources` and `browser_open` schemas, dispatcher-facing execution, normalized browser failures, and structured result-size fitting. |
| `src/oneoxygen_sandbox/models.py` | `BrowserMode`, `BrowserSourceProfile`, `BrowserConfig`, browser error codes, task cross-validation, and version 4 browser run-record fields. |
| `src/oneoxygen_sandbox/config.py` | Includes configured browser policy in canonical task hashes while preserving hashes for legacy tasks without browser configuration. |
| `src/oneoxygen_sandbox/session.py` | Records browser configuration, exact allowed hosts, and the browser policy digest when a run starts. |
| `src/oneoxygen_sandbox/agent.py` | Adds selected profiles, exact hosts, and untrusted-content guidance to direct-agent system prompts. |
| `src/oneoxygen_sandbox/coordinator.py` | Adds the same deterministic browser prompt appendix to durable/batch-coordinated agent turns. |
| `src/oneoxygen_sandbox/cli.py` | Adds `browser sources` and `browser policy` commands. |
| `src/oneoxygen_sandbox/tools/base.py` | Redacts browser query strings from persisted tool arguments and records only their size and hash. |
| `src/oneoxygen_sandbox/tools/registry.py` | Registers both browser tools and permits an injected browser client for deterministic tests or alternate backends. |
| `src/oneoxygen_sandbox/tools/__init__.py` | Exports the browser client protocol, secure client, and browser tools through the tools package. |
| `src/oneoxygen_sandbox/__init__.py` | Exports browser configuration types and the managed-policy compiler through the public package API. |
| `tests/test_browser.py` | Tests exact-host URL policy, URL normalization, private/mixed DNS rejection, redirect enforcement, HTML extraction, untrusted labeling, source hashes, and public-only task validation. |
| `tests/test_browser_policies.py` | Tests deterministic Chrome/Brave/Firefox/Safari output, exact filters, family lockdowns, loopback-proxy rejection, and browser CLI commands. |
| `tests/test_tools.py` | Tests browser schema registration, selected-source output, model-call dispatch, off-list rejection before client access, query redaction, run-record fields, and structured result truncation. |
| `tests/test_agent_runner.py` | Tests that allowed browser tools and exact hosts reach model adapters through the canonical prompt/tool contract without exposing secrets to Docker. |
| `tests/test_model_contracts.py` | Verifies legacy task hashes remain stable when the optional browser field is absent. |
| `README.md` | Provides the operator-facing overview, configuration, source catalog, security behavior, policy commands, limitations, and this file map. |

# Browser integration for U.S. financial-due-diligence evaluations

- Status: implemented provider-neutral text-browser foundation plus managed-browser hardening plan
- Research and link check: 2026-07-24
- Runtime scope: OpenAI, Api.Airforce, scripted, and future tool-calling model adapters

## 1. Decision

One Oxygen now exposes web research as provider-neutral tools executed by the host, without
enabling networking in the execution container. The implemented foundation is in
`src/oneoxygen_sandbox/browser.py` and `src/oneoxygen_sandbox/tools/browser.py`. OpenAI,
Api.Airforce, the scripted adapter, and any future adapter using `ModelTurnRequest` receive the
same canonical `browser_sources` and `browser_open` schemas through `ToolDispatcher`; browsing is
not delegated to a provider's proprietary search product.

This change does not add a missing model-provider adapter. The repository still has no direct
Anthropic/Claude adapter; that is separate provider-connectivity work. Once such an adapter
implements the existing `ModelAdapter` contract, it receives these browser tools without any
browser-specific Claude code.

The integration is deny-by-default:

- a task must include `browser.mode: live_web`, select named source profiles, and allow
  `browser_open` in its tool policy;
- the model cannot add a host, change a profile, launch an arbitrary executable, or use a general
  search engine;
- the host client permits only exact HTTPS hosts, resolves them itself, rejects any non-public or
  mixed public/private DNS answer, connects to the checked address, and verifies TLS for the
  requested hostname;
- every redirect is revalidated, only `GET` is available, and no browser cookie jar, credentials,
  arbitrary headers, request body, upload, JavaScript execution, or general HTTP tool is exposed;
- response bytes, extracted text, links, time, content hash, source profile, and truncation are
  bounded and traced; and
- public, licensed, and fee-bearing sources are never mixed implicitly.

The implemented v1 is a read-only text browser for HTML, JSON, XML, and plain text. It does not
launch Chrome, Firefox, Brave, Safari, or another GUI engine, render client-side JavaScript,
capture screenshots, or download binary files. Section 7 specifies how managed browser engines
can use the same source catalog as a later rendering backend. The model-facing tools deliberately
remain identical across browser engines and model providers.

This feature is for corroborating public facts. It is not a substitute for a target data room,
quality-of-earnings work, trial-balance testing, customer or supplier confirmations, tax review,
legal diligence, or advice from a qualified professional. Those workstreams usually require
non-public target materials that must not be sent to public websites.

## 2. Threat and fairness model

The browser sees hostile internet content. A page can contain prompt injection, misleading text,
malware, tracking code, redirects, credential prompts, oversized downloads, or instructions to
send confidential deal information elsewhere. An allowlisted publisher can also be compromised.
Accordingly, being on the source list establishes an egress destination, not truth or safety.

The implementation preserves these invariants:

1. The model receives browser tools, never a raw browser process, shell, DevTools endpoint,
   WebDriver endpoint, cookie jar, credential, proxy token, or host path.
2. The existing tool dispatcher authorizes every call and the browser broker validates every
   navigation again.
3. URL validation requires HTTPS, port 443, an exact selected hostname, no user information, and
   no IP literal; fragments are removed. DNS answers must all be globally routable.
4. The client connects to the already-checked IP address while using the original hostname for
   TLS SNI and certificate verification, closing the DNS-rebinding gap between validation and
   connection.
5. Every redirect is checked again. Page text is labeled as untrusted evidence and cannot alter
   system instructions, tools, source profiles, submission rules, or the task.
6. The selected configuration, normalized host set, and policy digest are stored in `RunRecord`.

The current host process does not yet run in a dedicated network namespace behind an independent
deny-by-default proxy. That deployment layer remains required before describing the feature as a
full managed-browser security boundary. The current boundary is the small, pinned-connection
client reachable only through the dispatcher.

Chrome explicitly describes URL allow/block lists as basic URL management and recommends a
content-filtering proxy or extension for stronger filtering. Its `URLBlocklist` documentation
also notes that a permitted page can dynamically load a blocked path with `XMLHttpRequest`.
Therefore browser policy is not the authoritative egress control
([Chrome URL filtering guidance](https://support.google.com/chrome/a/answer/7532419),
[Chrome `URLBlocklist`](https://chromeenterprise.google/policies/url-blocklist/)).

## 3. Architecture

```mermaid
flowchart LR
    M[Model adapter] --> R[AgentRunner]
    R --> D[ToolDispatcher]
    D --> B[browser_sources / browser_open]
    B --> V[Exact URL and profile validation]
    V --> N[Public DNS validation]
    N --> T[Pinned TLS GET]
    T --> S[Selected HTTPS source hosts]
    D --> C[Network-disabled execution container]
    B --> E[Bounded untrusted text and links]
    E --> R
```

The execution container remains network-disabled. The implemented host-side broker exposes only
the following canonical tools:

| Tool | Permitted behavior |
|---|---|
| `browser_sources` | Return selected profile IDs, descriptions, exact hosts, and policy hash without making a network request. |
| `browser_open` | Open one absolute `https://` URL on a selected source profile. |

`browser_click`, `browser_fill`, `browser_select`, `browser_screenshot`, `browser_download`,
`browser_back`, and `browser_close` are planned managed-engine tools, not current capabilities.
There is no arbitrary JavaScript evaluation, arbitrary HTTP client, address-bar search, clipboard,
file upload, print, external protocol handler, extension installation, cookie export, password
entry, or unrestricted DOM dump. A task without a `browser` section cannot allow either browser
tool, and a task with browser configuration must explicitly allow `browser_open`.

Implemented defaults are a 20-second request timeout, five redirects, 5 MiB of captured response
bytes, 60,000 extracted text characters, 100 same-policy links, and two requests per second per
host. The existing total and per-tool call limits remain authoritative and can be lowered by each
task.

## 4. U.S. diligence source profiles

### 4.1 Selection rules

The lists below are exact-host catalogs, not suffix wildcards. Selecting `sec_edgar`, for example,
does not permit every `.gov` host or every subdomain of `sec.gov`. A source's off-site analytics,
advertising, social media, support widget, URL shortener, translation service, and generic CDN are
blocked unless an exact dependency is separately reviewed and recorded.

Prefer an official bulk file or documented API to scraping an interactive page. Never bypass a
CAPTCHA, rate limit, paywall, access control, or publisher restriction. Store the page's own
effective/as-of date where it is available.

### 4.2 Public primary-source modules

These modules are eligible for public or synthetic tasks. A task should select only the modules it
needs.

| Profile | Exact HTTPS hosts | Diligence use and authoritative basis |
|---|---|---|
| `sec_edgar` | `sec.gov`, `www.sec.gov`, `data.sec.gov`, `efts.sec.gov` | 10-K, 10-Q, 8-K, registration statements, merger proxies, tender-offer filings, exhibits, ownership filings, filing history, and XBRL facts. EDGAR filings are freely accessible; the SEC's unauthenticated APIs expose submission history and XBRL data ([EDGAR access](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data), [EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)). |
| `us_macro` | `fred.stlouisfed.org`, `api.stlouisfed.org`, `www.bls.gov`, `download.bls.gov`, `www.bea.gov`, `apps.bea.gov`, `www.census.gov`, `api.census.gov`, `data.census.gov`, `fiscaldata.treasury.gov`, `api.fiscaldata.treasury.gov` | Interest-rate, inflation, labor, employment, GDP, industry-output, establishment, revenue, demographic, and Treasury series used to check market assumptions. See [FRED](https://fred.stlouisfed.org/docs/api/fred/fred.html), [BLS's public API](https://www.bls.gov/audience/developers.htm), [BEA's API](https://apps.bea.gov/api/signup/), and the [Economic Census API](https://www.census.gov/programs-surveys/economic-census/data/api.html). |
| `regulated_financial` | `banks.data.fdic.gov`, `www.ffiec.gov`, `www.occ.gov`, `www.consumerfinance.gov`, `files.consumerfinance.gov` | Insured-bank identity and financial trends, holding-company structure and transformations, OCC actions, and public complaint trends. The official tools include [FDIC BankFind](https://banks.data.fdic.gov/bankfind-suite/), [FFIEC NIC](https://www.ffiec.gov/npw/Institution/Index), [OCC Financial Institution Search](https://www.occ.gov/publications-and-resources/tools/occ-financial-institution-search/index-occ-financial-institution-search.html), and the [CFPB complaint database](https://www.consumerfinance.gov/data-research/consumer-complaints/). Complaint counts are not a statistical sample and must be normalized and caveated. |
| `federal_counterparty` | `sam.gov`, `api.sam.gov`, `open.gsa.gov`, `usaspending.gov`, `www.usaspending.gov`, `api.usaspending.gov` | Entity registration, exclusions, responsibility/qualification records, and federal contract or grant awards. Use only public SAM views; do not enter federal-only workspaces or CUI. See [SAM Entity Information](https://sam.gov/entity-information) and the [USAspending API](https://api.usaspending.gov/). |
| `ofac_sanctions` | `ofac.treasury.gov`, `sanctionssearch.ofac.treas.gov`, `sanctionslist.ofac.treas.gov` | Potential SDN and non-SDN sanctions matches. OFAC's search uses fuzzy logic for names; a score is a lead for review, not a final identity determination ([OFAC search tool](https://ofac.treasury.gov/sanctions-list-search-tool)). |
| `antitrust` | `www.ftc.gov`, `www.justice.gov` | FTC cases, proceedings, HSR material, merger guidance, and DOJ Antitrust case filings used to identify transaction and market-structure risk ([FTC Legal Library](https://www.ftc.gov/legal-library), [DOJ Antitrust Case Filings](https://www.justice.gov/atr/antitrust-case-filings)). |
| `workplace_environment` | `echo.epa.gov`, `www.osha.gov` | Facility permits, environmental inspections, violations, enforcement, penalties, and OSHA establishment inspections. ECHO itself warns that corporate-family matching and pending matters can be incomplete; treat name matches as candidates ([EPA ECHO](https://echo.epa.gov/), [OSHA Establishment Search help](https://www.osha.gov/help/establishment-search)). |
| `us_ip` | `www.uspto.gov`, `ppubs.uspto.gov`, `data.uspto.gov`, `tsdr.uspto.gov`, `tmsearch.uspto.gov` | Patent and trademark applications, registrations, status, documents, and public search. See the official [USPTO patent search](https://ppubs.uspto.gov/basic/) and [trademark search](https://www.uspto.gov/trademarks/search). |
| `tax_exempt` | `www.irs.gov`, `apps.irs.gov` | Form 990-series returns, determination letters, revocations, and exempt-organization status for nonprofit targets ([IRS Tax Exempt Organization Search](https://www.irs.gov/charities-non-profits/exempt-organizations-select-check)). This profile does not expose private tax accounts or general corporate tax returns. |

Public does not always mean anonymous. Some documented public-data API routes in these profiles
require a publisher API key. Such an origin is usable only with the
`brokered_public_api_key` control defined in Section 8; browser pages and bulk files that do not
require a key remain `none`. A profile must never turn a public API key into a human account login
or expose it to the model. The current runtime has no key broker, so key-required API routes are
unavailable even when their host belongs to a selected profile.

The SEC permits programmatic access subject to fair-access controls, currently documents a maximum
of 10 requests per second, and asks automated clients to declare a user agent. One Oxygen should
use a much lower default of two requests per second per SEC host, cache within a run, and identify
the benchmark operator and contact address
([SEC fair-access guidance](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)).

### 4.3 Registration and entity modules requiring extra review

These are useful, but they have site-specific terms, anti-automation rules, incomplete coverage,
or write-capable pages next to their search tools. They are disabled until the benchmark owner
records a terms review and a read-only path policy. None of the profiles in this subsection is
present in the current `BrowserSourceProfile` enum.

| Profile | Exact HTTPS hosts | Use and restriction |
|---|---|---|
| `securities_registrants` | `adviserinfo.sec.gov`, `brokercheck.finra.org`, `files.brokercheck.finra.org`, `www.finra.org`, `www.nfa.futures.org` | IAPD exposes Form ADV, registration, business, and disciplinary information; BrokerCheck covers brokers and firms; NFA BASIC covers derivatives registrants ([IAPD](https://adviserinfo.sec.gov/), [BrokerCheck description](https://www.finra.org/investors/learn-to-invest/choosing-investment-professional/about-brokercheck), [NFA BASIC description](https://www.nfa.futures.org/faqs/investors.html)). FINRA expressly prohibits bulk copying, scraping, harvesting, and database creation, so this profile must not be used for bulk extraction ([BrokerCheck Terms](https://brokercheck.finra.org/terms)). |
| `state_entity_de` | `corp.delaware.gov`, `icis.corp.delaware.gov` | Delaware entity identity and formation details. Delaware expressly prohibits data mining and automated tools on its free search, so agent automation remains disabled unless Delaware supplies an approved access method ([Delaware entity search notice](https://icis.corp.delaware.gov/Ecorp/EntitySearch/NameSearch.aspx)). |
| `state_entity_ca` | `www.sos.ca.gov`, `bizfileonline.sos.ca.gov`, `bpd.cdn.sos.ca.gov` | California corporations, LLCs, LPs, status, officers, and filed-document images. The search result is not a certified record ([California Business Search](https://bizfileonline.sos.ca.gov/search/business)). |
| `state_entity_ny` | `dos.ny.gov`, `apps.dos.ny.gov` | New York corporations and business entities ([New York Department of State](https://dos.ny.gov/)). |
| `state_entity_tx` | `comptroller.texas.gov`, `mycpa.cpa.state.tx.us` | Texas franchise-tax account status and public information reports. This is not a complete substitute for Secretary of State records ([Texas Comptroller databases](https://comptroller.texas.gov/transparency/open-data/cpa-databases/)). |
| `state_entity_fl` | `dos.fl.gov`, `search.sunbiz.org` | Florida corporations, LLCs, partnerships, fictitious names, and liens ([Florida Division of Corporations search](https://dos.fl.gov/sunbiz/search/)). |

There is no single public federal registry containing every U.S. company's formation and
good-standing record. The SBA notes that most entities register with a state Secretary of State,
business bureau, or business agency
([SBA registration guide](https://www.sba.gov/business-guide/launch-your-business/register-your-business)).
For any other state, add a separate `state_entity_<postal-code>` profile with exact official hosts,
a recorded terms review, and tests. Never solve this by allowing `*.gov`, all state portals, a
commercial registered-agent site, or a general search engine.

Under FinCEN's current interim final rule, U.S.-created domestic entities are exempt from
beneficial-ownership-information reporting. The remaining reported information is held in a
secure, non-public database and is available only to authorized recipients in limited
circumstances ([FinCEN current rule](https://www.fincen.gov/boi/ifr-qa),
[FinCEN BOI access FAQ](https://www.fincen.gov/boi-faqs)). It is not a public diligence source.
Do not represent absence of public ownership data as absence of an owner or controller.

### 4.4 Fee-bearing, authenticated, and industry modules

`pacer_federal_courts` may permit `pacer.uscourts.gov`, `pcl.uscourts.gov`, and the exact court
hosts supplied by PACER. It is off by default because searches and documents can incur fees.
PACER provides electronic access to federal court records and explains its charges on the official
site ([PACER](https://pacer.uscourts.gov/)). A future implementation enabling it requires:

- an operator-owned account and isolated credential profile;
- a per-run dollar cap of zero unless the user explicitly authorizes a positive cap;
- a pre-charge confirmation enforced by the broker, not delegated to the model;
- no password, MFA value, billing detail, session cookie, or sealed material in model context; and
- a trace of every charge and document identifier.

Industry modules should also be selected only when the task needs them:

| Profile | Exact HTTPS hosts | Typical use |
|---|---|---|
| `healthcare_public` | `www.fda.gov`, `open.fda.gov`, `api.fda.gov`, `www.cms.gov`, `data.cms.gov` | Approvals, recalls, adverse events, provider and reimbursement data. |
| `energy_public` | `www.eia.gov`, `api.eia.gov`, `www.ferc.gov`, `elibrary.ferc.gov` | Energy prices, production, infrastructure, tariffs, orders, and filings. |
| `telecom_public` | `www.fcc.gov`, `publicfiles.fcc.gov` | Licenses, ownership reports, proceedings, and public inspection files. |

Issuer investor-relations sites are task-specific. A task author may create an
`issuer_ir_<task-id>` profile containing the exact issuer-controlled hosts after reviewing every
host and redirect. The model cannot create it. Social networks, employee-review sites, generic
news, web archives, translation proxies, and search-engine caches are not default evidence
sources.

### 4.5 Licensed platforms used in investment-banking workflows

The following products are relevant to banker workflows, but no host is allowlisted merely because
the product appears in this table:

| Platform | Relevant workflow | Enablement rule |
|---|---|---|
| S&P Capital IQ Pro | Public/private company financials, transactions, comparable-company and precedent-transaction analysis, ownership, estimates, documents, and diligence. S&P describes explicit investment-banking and due-diligence workflows ([S&P Capital IQ Pro](https://www.spglobal.com/market-intelligence/en/solutions/products/sp-capital-iq-pro)). | Require an evaluator-owned license and the current vendor-provided network/FQDN manifest. Do not assume all `spglobal.com` subdomains are needed. |
| PitchBook | Private-company financials, cap tables, financing, management, investors, funds, deals, and private-market diligence ([PitchBook due-diligence workflow](https://pitchbook.com/help/how-to-prompt-library-premium-connectors)). | Require licensed credentials and written confirmation that the planned automated use and output retention are permitted. PitchBook credentials are individual and its terms restrict use ([PitchBook Terms](https://pitchbook.com/terms-of-use)). |
| FactSet | Public/private data, company screening, M&A comps, precedent transactions, models, research, and pitch materials ([FactSet banking solutions](https://www.factset.com/solutions/clients/banks)). | Require an evaluator-owned subscription and FactSet's current connectivity allowlist. Use the licensed API when the agreement permits it instead of UI extraction. |
| LSEG Workspace | Deals, league tables, estimates, markets, news, and investment-banking workflows ([LSEG Workspace for investment bankers](https://www.lseg.com/en/data-analytics/investment-banking/workspace-investment-banking)). | Require a licensed Workspace profile and LSEG's current network guide; its web product has numerous service dependencies, so a guessed wildcard is unacceptable. |
| Bloomberg | Market, company, security, transaction, news, screening, and valuation research. Bloomberg documents M&A target-screening and valuation workflows ([Bloomberg M&A workflow](https://www.bloomberg.com/professional/insights/webinar/pharma-ma-in-action-leveraging-bloomberg-for-target-screening-valuation/)). | The Terminal is not a public website. Use only a contractually permitted Bloomberg product/API and a separate licensed track; do not automate a Terminal session through this browser interface. |

Other licensed products—such as Mergermarket, Dealogic, Moody's, Fitch, D&B, AlphaSense,
LexisNexis, and Westlaw—follow the same rule. A product name is not authorization. Each requires a
separate source profile, contract review, current vendor network manifest, retention policy,
credential boundary, and experiment label. No model may accept click-through terms on the
operator's behalf.

## 5. Canonical task and source-profile contract

The following schema is implemented. Browser tools are not enabled by default:

```yaml
tool_policy:
  allowed_tool_names:
    - list_files
    - read_text_file
    - write_text_file
    - browser_sources
    - browser_open
    - submit_result
  max_total_tool_calls: 30
  per_tool_call_limits:
    browser_open: 12

browser:
  mode: live_web
  source_profiles:
    - sec_edgar
    - us_macro
  request_timeout_seconds: 20
  maximum_redirects: 5
  maximum_response_size_bytes: 5242880
  maximum_text_characters: 60000
  maximum_links: 100
  requests_per_second: 2
  user_agent: "BenchmarkName/1.0 diligence-contact@example.com"

agent:
  instruction_file: task.md
  data_classification: public
```

Operators can inspect the immutable catalog and compile a deterministic managed-browser baseline
without making a network request:

```bash
oneoxygen-sandbox browser sources
oneoxygen-sandbox browser policy \
  --family brave \
  --profiles sec_edgar,ofac_sanctions \
  --proxy-server http://127.0.0.1:8765 \
  --user-agent "BenchmarkName/1.0 diligence-contact@example.com"
```

The policy command emits JSON and a bundle hash. It does not install policy or launch a browser.

Validation rules:

- `browser` is absent by default. If a browser tool appears without it, task validation fails.
- Browser configuration requires `browser_open` in `allowed_tool_names`; the task may also expose
  `browser_sources`.
- When an `agent` section is present, its `data_classification` must be `synthetic` or `public`;
  missing, internal, confidential, or restricted classification fails validation. A non-agent
  scripted tool demo may use the same browser policy without a model.
- `source_profiles` accepts only the built-in `BrowserSourceProfile` enum. It is de-duplicated and
  must contain at least one profile.
- Read-only behavior is structural: `browser_open` accepts only a URL and the client exposes only
  `GET`. There is no task field capable of enabling writes, headers, bodies, credentials, cookies,
  or uploads.
- Extra origins cannot be supplied in model output, tool arguments, provider settings, or
  environment variables. Adding a host requires a reviewed code change to the local catalog.
- The browser configuration, sorted exact host set, and policy SHA-256 are stored in `RunRecord`
  schema version 4. Query values in model/tool traces are replaced with a size and SHA-256.
- Public API credentials and human logins are not implemented. The `user_agent` is a declared
  publisher-facing identifier, not a credential. For SEC access it should contain the benchmark
  operator's real name/contact and still comply with SEC rate guidance.

The immutable in-code catalog has this logical form:

```yaml
policy_version: 1
id: sec_edgar
origins:
  - scheme: https
    host: sec.gov
    port: 443
    include_subdomains: false
    methods: [GET]
  - scheme: https
    host: www.sec.gov
    port: 443
    include_subdomains: false
    methods: [GET]
  - scheme: https
    host: data.sec.gov
    port: 443
    include_subdomains: false
    methods: [GET]
  - scheme: https
    host: efts.sec.gov
    port: 443
    include_subdomains: false
    methods: [GET]
rate_limit:
  requests_per_second: 2
```

Host normalization lowercases ASCII, removes one terminal dot, converts Unicode through IDNA
using Python's IDNA codec, rejects invalid labels, and compares the result exactly. User
information, IP literals, non-default ports, ambiguous numeric addresses, wildcard hosts,
backslashes, and control characters are invalid. Fragments are stripped before retrieval.

## 6. Authoritative egress policy

The implemented text browser has no general browser process to escape: it validates an exact URL,
validates every DNS answer, and creates the TLS connection itself. If a JavaScript-capable managed
engine is added, an independent egress proxy and network namespace must become the authoritative
security boundary. Browser policy, DNS settings, and an extension cannot replace that future
boundary.

### 6.1 Required managed-engine network rules

- Permit TCP to the broker proxy only from the browser's network namespace.
- Permit proxy `CONNECT` only to an exact selected hostname on port 443.
- Resolve DNS at the proxy. Block direct browser DNS, DNS-over-HTTPS, DNS-over-TLS, multicast DNS,
  QUIC/UDP 443, WebRTC peer connections, WebTransport, raw sockets, and user-configured proxies.
- Reject every A and AAAA answer in loopback, private, link-local, carrier-grade NAT, multicast,
  documentation, benchmarking, reserved, or cloud-metadata ranges. Re-resolve and re-check on
  connection reuse to resist DNS rebinding.
- Require SNI and certificate host validation for the requested exact hostname. Never disable TLS
  verification or install a public-site bypass certificate.
- Revalidate every redirect before following it. An off-list redirect is a bounded
  `url_not_allowed` result, never a prompt to broaden the list.
- Permit `GET`, `HEAD`, and necessary CORS `OPTIONS` by default. A same-origin search that requires
  `POST` needs an explicit host-and-path exception, a 64 KiB body limit, no file part, no secret,
  and a proof that the action is non-mutating. Deny `PUT`, `PATCH`, `DELETE`, uploads, filing,
  purchasing, ordering, payment, complaint submission, messaging, and account changes.
- Strip proxy credentials and internal headers before forwarding. Set the reviewed identifying
  user agent required by the publisher.
- Apply a default rate of two requests per second and burst of two per origin, plus global
  navigation and byte budgets. A reviewed profile may lower this; it may increase it only within
  documented publisher limits.
- Do not auto-add a host after a blocked request. Record the candidate dependency for human
  review.

### 6.2 Required managed-engine data and browser-state rules

- Start from a new profile directory for every run and destroy it on every terminal path.
- Disable sync, browser sign-in, autofill, password storage, payment methods, telemetry where
  manageable, background mode, guest/private windows, speculative prefetch, notifications,
  geolocation, camera, microphone, USB, Bluetooth, serial, external protocols, and unapproved
  extensions.
- Do not place the task instruction, source documents, target files, model prompt, API keys, or
  prior findings into a web form. Search fields receive only the minimum public or synthetic
  query.
- Treat query strings, form bodies, cookies, authorization headers, and local storage as
  sensitive in traces. Record bounded hashes and redacted metadata rather than values.
- Render scripts when necessary for an approved site, but return only visible text and labeled
  tables. Hidden instructions, comments, accessibility-hidden overlays, and CSS-hidden text are
  excluded from the default model-facing extraction.
- Prepend every extraction with an untrusted-source envelope containing the title, final URL,
  retrieval timestamp, content type, and content hash. The system prompt must say that page
  instructions are data, not commands.
- Block downloads except PDF, CSV, TSV, TXT, JSON, XML, XLSX, and reviewed ZIP archives. Verify
  declared type, magic bytes, extension, compressed and expanded size, entry count, traversal,
  links, nested archives, and duplicate names. Quarantine first; never execute macros, scripts,
  installers, or downloaded binaries.
- A spreadsheet or PDF parser runs in a second non-networked, resource-limited process. Original
  bytes and extracted text receive separate hashes.

## 7. Cross-browser enforcement

This section is a deployment specification for the later JavaScript-rendering backend. It is not
used by the implemented `SecureBrowserClient`, whose provider-facing behavior is deliberately
browser-family independent. `src/oneoxygen_sandbox/browser_policies.py` now compiles deterministic
baseline bundles for Chrome, Chromium, Edge, Brave, Firefox, Safari, Opera, and Vivaldi from the
same exact-host catalog. Those bundles do not install policy, launch an engine, verify effective
policy, or replace the required proxy; the Safari output is explicitly an MDM input manifest, not
a directly installable Apple profile. No family may be reported as an active rendering engine
until its adapter passes Section 10.

### 7.1 Common WebExtension

Build one rules-only Manifest V3 WebExtension from the canonical origin set. It contains static
Declarative Net Request block/allow rules, a local blocked-page explanation, and no analytics,
remote code, content script, arbitrary fetch, credential access, or update path outside the
managed package. Chrome documents DNR as its declarative request-blocking API
([Chrome DNR](https://developer.chrome.com/docs/extensions/reference/api/declarativeNetRequest));
Safari supports the same DNR approach in Safari Web Extensions
([Apple DNR guidance](https://developer.apple.com/documentation/safariservices/blocking-content-with-your-safari-web-extension)).

The compiler emits:

1. one low-priority rule blocking every HTTP, HTTPS, WebSocket, and other network request type;
2. higher-priority allow rules anchored to `^https://<exact-host>(?::443)?/`; and
3. explicit blocks for `http:`, `ws:`, `file:`, `ftp:`, `data:` top-level navigation,
   `view-source:`, external protocols, and IP-literal destinations.

The extension package and ruleset are deterministic and hashed. Rules are generated per run from
reviewed local profiles; the model cannot call `updateDynamicRules`. WebExtension compatibility is
good but not identical across Chrome-family browsers, Firefox, and Safari, so each built artifact
must pass the same conformance suite
([Mozilla compatibility notes](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/Chrome_incompatibilities)).

### 7.2 Browser support matrix

| Browser | Integration | Scored-run status |
|---|---|---|
| Google Chrome | Managed `URLBlocklist`/`URLAllowlist`, fixed proxy, force-installed MV3 guard, and locked-down browser policies. | Supported after conformance tests. |
| Open-source Chromium | Same Chromium policy compiler, fixed proxy, and MV3 guard. Pin the exact build and policy directory in the image. | Reference Linux engine. |
| Microsoft Edge | Compile the same exact hosts into Edge `URLBlocklist`/`URLAllowlist`, use Edge's managed extension controls, and force the proxy. Edge documents the same URL filter model ([Edge filter format](https://learn.microsoft.com/en-us/deployedge/edge-learnmmore-url-list-filter-format)). | Supported after conformance tests. |
| Brave | Use Chromium policies plus `TorDisabled`, `BraveVPNDisabled`, rewards/wallet disablement, and AI-chat disablement. Brave states that Chromium policies are available and documents `/etc/brave/policies/managed/` on Linux ([Brave Group Policy](https://support.brave.app/hc/en-us/articles/360039248271-Group-Policy)). | Supported after conformance tests. |
| Mozilla Firefox ESR | Locked `WebsiteFilter`, locked manual proxy with proxy DNS, managed XPI guard, and enterprise restrictions. Mozilla documents both the policy and Linux `policies.json` locations ([Firefox policy templates](https://mozilla.github.io/policy-templates/)). | Supported after conformance tests. |
| Apple Safari | Managed macOS device, Safari Web Extension DNR rules, Apple content-filter payload, and the authoritative proxy/network filter. Apple supports a “Specific websites only” device policy, but some Apple hosts can remain accessible, so proxy enforcement is still required ([Apple content filtering](https://support.apple.com/guide/deployment/dep1129ff8d2/web)). | Supported only on a managed macOS worker; never silently substituted for Linux Chromium. |
| Opera and Vivaldi | Chromium-family extension plus authoritative proxy. Their support for force-install and every enterprise policy must be probed rather than assumed. | Compatibility target; scored use only after the same enforcement self-test passes. |
| Other Chromium derivatives, Arc-family browsers, mobile browsers, and embedded WebViews | May accept parts of the WebExtension, but management, policy locking, and automation differ. | Unsupported until a named adapter and conformance test are added. |

“All public-use browsers” cannot safely mean every current and future browser binary. The
portable contract covers the major desktop engine families; a browser joins the scored set only
when it proves the same controls. Mobile browsers and unmanaged personal profiles are outside this
eval sandbox.

### 7.3 Chrome, Chromium, Edge, and Brave policy template

The policy compiler must use the vendor's exact policy location and namespace. Chrome on Linux
loads managed JSON from `/etc/opt/chrome/policies/managed/`; Brave uses
`/etc/brave/policies/managed/`. Edge and Chromium use their vendor-specific managed-policy
locations. The following Chrome example shows `sec_edgar`; generated production policy includes
every selected exact host and the pinned guard extension:

```json
{
  "URLBlocklist": ["*"],
  "URLAllowlist": [
    "https://.sec.gov",
    "https://.www.sec.gov",
    "https://.data.sec.gov",
    "https://.efts.sec.gov"
  ],
  "ProxyMode": "fixed_servers",
  "ProxyServer": "http://127.0.0.1:<BROKER_PORT>",
  "ProxyBypassList": "",
  "QuicAllowed": false,
  "DnsOverHttpsMode": "off",
  "IncognitoModeAvailability": 1,
  "BrowserGuestModeEnabled": false,
  "BrowserSignin": 0,
  "SyncDisabled": true,
  "PasswordManagerEnabled": false,
  "AutofillAddressEnabled": false,
  "AutofillCreditCardEnabled": false,
  "DefaultSearchProviderEnabled": false,
  "DeveloperToolsAvailability": 2,
  "ExtensionInstallBlocklist": ["*"],
  "ExtensionInstallForcelist": [
    "<PINNED_GUARD_EXTENSION_ID>;<LOCAL_MANAGED_UPDATE_URL>"
  ]
}
```

The leading dot in a Chromium URL filter means exact-host matching; without it, `example.com`
also matches subdomains. A trailing `/*` is not valid filter syntax. Compile from the canonical
origin objects rather than hand-editing the browser pattern
([Chrome URL filter format](https://support.google.com/chrome/a/answer/9942583)).
Chrome's allowlist supports at most 1,000 entries
([Chrome `URLAllowlist`](https://chromeenterprise.google/policies/url-allowlist/)).

Use the corresponding vendor namespaces:

- Chrome on Windows: `Software\Policies\Google\Chrome`
- Edge on Windows: `Software\Policies\Microsoft\Edge`
- Brave on Windows: `Software\Policies\BraveSoftware\Brave`
- Brave stable on macOS: `com.brave.browser`

Chrome documents that `DeveloperToolsAvailability: 2` disables DevTools entirely, `QuicAllowed:
false` disables QUIC, and `DnsOverHttpsMode: "off"` disables browser DoH
([DevTools policy](https://chromeenterprise.google/policies/developer-tools-availability/),
[QUIC policy](https://chromeenterprise.google/policies/quic-allowed/),
[DoH policy](https://chromeenterprise.google/policies/dns-over-https-mode/)).
Inspect `chrome://policy`, `edge://policy`, or `brave://policy` before the first navigation and
hash the effective values.

### 7.4 Firefox policy template

Firefox's browser filter uses WebExtension match patterns, which differ from Chromium enterprise
URL filters. Compile them independently:

```json
{
  "policies": {
    "WebsiteFilter": {
      "Block": ["<all_urls>"],
      "Exceptions": [
        "https://sec.gov/*",
        "https://www.sec.gov/*",
        "https://data.sec.gov/*",
        "https://efts.sec.gov/*"
      ]
    },
    "Proxy": {
      "Mode": "manual",
      "Locked": true,
      "HTTPProxy": "127.0.0.1:<BROKER_PORT>",
      "SSLProxy": "127.0.0.1:<BROKER_PORT>",
      "UseHTTPProxyForAllProtocols": true,
      "UseProxyForDNS": true,
      "Passthrough": ""
    },
    "DNSOverHTTPS": {
      "Enabled": false,
      "Locked": true
    },
    "DisableFirefoxAccounts": true,
    "DisablePrivateBrowsing": true,
    "OfferToSaveLogins": false,
    "AutofillAddressEnabled": false,
    "BlockAboutProfiles": true
  }
}
```

Install and lock the guard XPI through Firefox enterprise policy, block other extensions, and
verify effective policy in `about:policies`. On Linux, Mozilla supports system policy in
`/etc/firefox/policies`; an image may instead place `policies.json` in the installation's
`distribution` directory. The authoritative network namespace must still prevent a direct
connection if Firefox ignores or changes a browser policy.

### 7.5 Safari

Convert the common WebExtension with Apple's supported Safari Web Extension tooling, sign it with
the benchmark operator's managed identity, and deploy it with MDM. Apply an Apple content-filter
payload in “Specific websites only” mode and a device-level proxy/network filter. The browser
broker must verify the extension is enabled and the managed profile is installed before starting.

Safari is never a drop-in Linux/headless substitute. Runs record macOS and Safari versions and
remain in a separate browser experiment cell. The broker must not ask the model to approve a new
extension permission or system dialog.

## 8. Read-only semantics, credentials, and costs

“Read-only” describes both HTTP behavior and user intent:

- Searching, filtering, and opening a public record are reads. The current client implements only
  a URL-only `GET`; binary downloads are not implemented.
- Creating an account, accepting terms, filing a form, ordering a certificate, paying, sending a
  message, submitting a complaint, saving a watchlist, changing a preference, or acknowledging on
  behalf of a user are writes and are blocked.
- `POST`, even for a search form, is not implemented. A path being named `search` would not prove
  that it is non-mutating.
- A CAPTCHA, anti-bot, robots, or rate-limit response is returned as bounded page evidence and
  must stop further automated attempts. The model cannot solve, outsource, or route around it.

Current public browser sessions receive no user credentials or API keys. A URL containing user
information is rejected, there is no request-header argument, cookies are not retained between
requests, and model-provider keys remain outside both the browser and execution container.

A future documented public-data API may use `brokered_public_api_key` only after a host-scoped
proxy injector and redaction tests exist. A future separately authorized licensed profile may use
a credential broker only after contract review and equivalent isolation. Neither capability is
present in the current task schema.

Fee-bearing sources are unavailable. There is no task field that can authorize a charge.

## 9. Evidence, citations, and run records

Every implemented `browser_open` extraction includes:

- matching source-profile ID;
- page or document title;
- requested URL, final checked URL, redirect chain, and HTTP status;
- retrieval time in UTC;
- content type, captured byte count, and SHA-256 hash;
- bounded extracted text and same-policy links;
- separate body, text, and link truncation flags; and
- an explicit `untrusted_content` marker and warning.

Final diligence findings should cite the primary page or filing, identify the filing/report date,
distinguish company-reported values from agency observations, and state material limitations.
Search-result snippets are discovery aids, not evidence. A vendor-normalized number should link
back to its source document when the license permits that audit trail.

`RunRecord` schema version 4 now contains:

- the validated `browser_configuration`;
- sorted `browser_allowed_hosts`;
- `browser_policy_sha256`; and
- the existing ordered, bounded `ToolEvent` records for `browser_sources` and `browser_open`.

A future managed engine additionally needs browser vendor/version and executable digest,
enterprise-policy/extension/proxy digests, navigation IDs, complete proxy decisions, clean-profile
lifecycle, download quarantine records, and aggregate network counters.

Do not persist unrestricted page bodies, cookies, authorization data, browser caches, DOM dumps,
response headers, crash reports, or screenshots containing credentials. Apply the existing
bounded-output, sanitization, and successful-submission artifact rules.

Live sites change independently of the model. A live-web run is not directly comparable with a
network-free scripted run or with a live run from another retrieval time. For a stable benchmark,
capture legally redistributable public fixtures with source URL, retrieval time, and hash, then
serve them from the offline fixture track. Use live web for freshness and robustness evaluations,
not as an unversioned source of ground truth.

## 10. Conformance tests

The implemented offline suite covers:

1. Browser tools without browser configuration fail task validation.
2. Browser configuration without `browser_open`, or with non-public agent data, fails validation.
3. Source selection produces a deterministic, de-duplicated exact-host set and policy hash.
4. HTTP, off-list hosts, unlisted subdomains, user information, non-443 ports, IP literals, file
   URLs, backslashes, and malformed URLs fail before client access.
5. Private or mixed public/private DNS answers fail closed.
6. Redirects among selected hosts work; an off-list redirect fails without expanding policy.
7. HTML extraction omits script content, labels returned text untrusted, and returns only
   same-policy links.
8. Tool dispatch records browser configuration/hosts/hash and hashes query values in traces.
9. An agent run receives the same browser tool schemas and exact-host prompt appendix regardless
   of model adapter.
10. Managed-policy bundles use exact Chrome-family filters, locked Firefox exceptions, Brave
    feature disablement, loopback-only proxies, and an explicitly non-installable Safari manifest.

The future managed-engine suite must additionally test subresources, service workers, WebSockets,
QUIC, WebRTC, DoH, proxy bypass, DevTools, alternate profiles, downloads, credential injection,
crash cleanup, and complete egress-log reconciliation on Chrome, Chromium, Edge, Brave, Firefox
ESR, managed Safari, Opera, and Vivaldi. Only adapters that pass may be labeled supported.

Unit tests use deterministic response and DNS fixtures and make no internet request, accept no
click-through terms, use no commercial credential, and incur no fee. A manual live smoke check
reached SEC through the implemented client; SEC correctly returned its undeclared-automated-tool
page when the initial test user agent lacked an operator contact. Production SEC tasks must
configure a truthful declared user agent and comply with SEC fair-access requirements.

## 11. Implementation status and remaining gate

Implemented now:

- immutable public-source enums and an exact-host catalog;
- browser task schema, cross-field validation, canonical configuration hashing, and
  public/synthetic data restriction;
- provider-neutral `browser_sources` and `browser_open` tools;
- exact URL validation, public-DNS validation, pinned TLS, redirect revalidation, GET-only
  retrieval, rate limiting, bounded text/link extraction, and prompt-injection labeling;
- query redaction in model/tool traces;
- browser configuration, exact hosts, and policy digest in `RunRecord` schema version 4;
- deterministic baseline managed-policy bundles for the major desktop browser families; and
- offline policy, extraction, dispatcher, and provider-neutral agent tests.

Still required before enabling a JavaScript-capable Chrome/Firefox/Brave/Safari-family backend:

- authoritative egress proxy and browser network namespace;
- extension packaging and enterprise/MDM deployment;
- ephemeral browser lifecycle and effective-policy verification;
- download quarantine and non-networked document parsers;
- complete network decision tracing and browser-version experiment cells;
- adversarial conformance and opt-in live smoke tests for every named engine; and
- current legal/terms review records for any licensed or extra-review profile.

Until that remaining gate passes, tasks may use the implemented text browser only. No task option
may select or imply a GUI browser engine, authenticated profile, binary download, or JavaScript
rendering. Container network policy remains `disabled`.

## 12. Maintenance

Review public profiles at least quarterly and whenever a redirect, certificate identity, terms
page, API version, or browser policy changes. A review records the owner, date, source links,
exact hosts, paths/methods, dependencies, rate limit, authentication class, and data license.
Catalog-semantic changes bump `BROWSER_POLICY_VERSION`; any host change produces a new policy
hash and applies immediately to new runs.

Never learn policy automatically from observed traffic. A blocked dependency is evidence for a
human review, not permission. Commercial source profiles must additionally track contract term,
authorized user class, automation rights, export/retention limits, and vendor network-document
version.

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

The version 4 `RunRecord` preserves the Phase 1/2 sandbox policy, command results, tool policy,
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

Browser-enabled runs additionally record the complete validated browser configuration, sorted
exact host set, and browser policy SHA-256. Browser tool events retain bounded results and hashes,
while URL query values in call arguments are replaced with their size and hash.

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

## Direct and Batch Inference

`InferenceTransport` separates `direct`, `provider_batch`, and `gateway_direct` execution.
`ProvenanceClassification` separately identifies `official_provider`,
`third_party_gateway_unverified`, and `scripted_test` results. Every model event and run record
also carries the logical provider, API host, requested and returned model, official-route flag,
upstream-verifiability flag, and batch identifiers when applicable. Provider identity is the API
party contacted by One Oxygen: an Airforce response remains provider `airforce` regardless of
the model-shaped identifier returned by the gateway.

Direct OpenAI calls and OpenAI Batch lines use the same Responses API compiler, output parser,
tool-call normalization, usage mapping, finish-reason mapping, prompts, tool definitions, and
generation settings. The transport changes only submission and retrieval. Batch JSONL targets
`/v1/responses`, contains unique opaque `custom_id` values, never streams, and never contains an
API key.

An entire multi-turn agent cannot be submitted as one static batch request because tool results
change its next model input. Phase 3B therefore batches one ready turn per run:

1. Persist runs in `ready_for_model`.
2. Group only matching provider, transport, model, endpoint, tool-schema hash, prompt version,
   schema mode, effective settings, and data-policy class.
3. Build and validate JSONL locally.
4. Submit the batch and persist the remote ID before reporting success.
5. Leave runs suspended in `waiting_for_model` with no live container.
6. Download output and error files, correlate by the durable custom-ID mapping, and reject
   duplicate or unknown IDs.
7. Resume each successful item, execute its tool calls in a fresh sandbox, finalize a checkpoint,
   and enqueue the next turn if needed.
8. Complete only after `submit_result` succeeds.

Successful items are never repeated. Only individually failed retryable items receive a new
attempt ID in a later batch. A timeout or connection loss that leaves remote submission state
unknown records `remote_state_unknown` and disables blind resubmission.

### Workspace checkpoints and crash recovery

Each finalized checkpoint generation contains only permitted regular workspace files plus a
versioned SHA-256 manifest. Capture and finalization are atomic. Symbolic links, traversal,
devices, sockets, named pipes, protected runtime/grader paths, provider credentials, and
oversized checkpoints fail closed. Finalized generations are immutable, older generations follow
a bounded cleanup policy, and host paths are never exposed to the model.

On resume, One Oxygen verifies every file hash, restores into an empty workspace, mounts only
that workspace into a new network-disabled container, executes tools, removes temporary runtime
files, finalizes the next generation, then stops and removes the container. A closed terminal,
process restart, machine restart, or long provider delay does not require a daemon: the SQLite
revision and checkpoint generation are sufficient to continue.

### OpenAI Batch CLI

The commands deliberately separate local construction from external state changes:

```powershell
python -m oneoxygen_sandbox eval enqueue examples/agent_demo/task.yaml `
  --provider openai --model "<MODEL_ID>" --transport provider_batch --count 5
python -m oneoxygen_sandbox batch ready --provider openai
python -m oneoxygen_sandbox batch build --provider openai
python -m oneoxygen_sandbox batch validate "<LOCAL_BATCH_ID>"
python -m oneoxygen_sandbox batch submit "<LOCAL_BATCH_ID>"
python -m oneoxygen_sandbox batch status "<LOCAL_BATCH_ID>"
python -m oneoxygen_sandbox batch collect "<LOCAL_BATCH_ID>"
python -m oneoxygen_sandbox eval resume-ready
python -m oneoxygen_sandbox batch cancel "<LOCAL_BATCH_ID>"
python -m oneoxygen_sandbox batch list --unfinished
```

The official [OpenAI Batch guide](https://developers.openai.com/api/docs/guides/batch)
documents a 50% discount versus synchronous APIs, a separate rate-limit pool, JSONL input via
the Files API, and a 24-hour completion window. Phase 3B records this as documented capability
metadata verified on 2026-07-23; it does not hard-code model prices or claim that every
provider offers the same discount. Token usage and the billing transport are retained, while any
savings estimate remains distinct from a provider invoice.

### Experimental Api.Airforce gateway

Api.Airforce is supported only as `gateway_direct` through its fixed HTTPS base URL
`https://api.airforce/v1` and OpenAI-compatible Chat Completions shape. It has no batch backend.
Its documentation describes upstream routing and failover that One Oxygen does not control, so
every result is `third_party_gateway_unverified`, uses a separate `gateway_unverified`
experiment/leaderboard track, and is never presented as validation of an official provider
identity.

Gateway tasks must explicitly declare `synthetic` or `public`; missing, `internal`,
`confidential`, and `restricted` classifications return `data_policy_violation`. Calls require
`--allow-third-party-gateway`. Only `AIRFORCE_API_KEY` is read, and no official-provider key is
sent. See [docs/provider-risk.md](docs/provider-risk.md) for the full security policy.

The free catalog is discovered live rather than hard-coded:

```powershell
python -m oneoxygen_sandbox models discover --provider airforce --free --tools `
  --operational --allow-third-party-gateway
```

`--free` requires both reported input and output prices to be exactly zero; a badge or model-name
suffix alone is not treated as proof. Live agentic testing also requires the caller-selected
`ONEOXYGEN_AIRFORCE_TEST_MODEL` and skips if zero-cost tool support cannot be verified.

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

Host keys and explicit live-test gates in PowerShell:

```powershell
$env:OPENAI_API_KEY = "<OPENAI_KEY>"
$env:AIRFORCE_API_KEY = "<AIRFORCE_KEY>"
$env:ONEOXYGEN_RUN_OPENAI_BATCH_LIVE_TESTS = "1"
$env:ONEOXYGEN_OPENAI_BATCH_TEST_MODEL = "<MODEL_ID>"
$env:ONEOXYGEN_RUN_AIRFORCE_LIVE_TESTS = "1"
$env:ONEOXYGEN_AIRFORCE_TEST_MODEL = "<ZERO_COST_TOOL_MODEL_ID>"
```

Do not put keys in YAML, CLI arguments, `.env.example`, batch files, checkpoints, run records,
or Docker. Real `.env` files remain ignored.

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

Run unit, mocked OpenAI Batch, mocked Api.Airforce, and offline coordinator tests with no Docker,
network, or paid API call:

```powershell
python -m pytest -m "not integration and not live_api and not openai_batch_live and not airforce_live"
```

Run Docker integration tests while explicitly excluding live API tests:

```powershell
python -m pytest -m "integration and not live_api and not openai_batch_live and not airforce_live"
```

Run the complete local suite without live API usage:

```powershell
python -m pytest -m "not live_api and not openai_batch_live and not airforce_live"
```

The OpenAI adapter tests inject a mocked client and verify text, function calls, local output-item
replay, usage and model mapping, error normalization, strict JSON parsing, no hosted tools,
default `store=False` plus explicit storage forwarding, disabled SDK retries, and secret exclusion.
They make no external request. The repository's default pytest options deselect `live_api`, so an
ordinary `pytest` command cannot make a paid request even when live-test environment variables are
present.

The Phase 3B tests additionally cover state transitions and recovery, checkpoint restoration and
tamper detection, grouping and custom IDs, out-of-order and partial results, missing/duplicate/
unknown IDs, retry selection, expiry/cancellation, unknown remote state, OpenAI Files and Batch
mock calls, `/v1/responses` shape, Airforce data policy, secret separation, malformed
compatibility responses, missing usage, and a multi-turn offline restart demonstration.

The browser tests additionally cover exact source selection, URL and DNS policy, pinned-routing
inputs, redirect revalidation, HTML extraction, same-policy links, result bounds, trace
redaction, model-facing tool exposure, run-record provenance, managed policy bundles, and CLI
compilation. They use injected DNS/response fixtures and make no network request.

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

OpenAI Batch live submission uses the separate `openai_batch_live` marker and requires
`OPENAI_API_KEY`, `ONEOXYGEN_RUN_OPENAI_BATCH_LIVE_TESTS=1`, and
`ONEOXYGEN_OPENAI_BATCH_TEST_MODEL`. It can incur charges and may take up to 24 hours; the normal
suite never waits for it. Api.Airforce live testing uses `airforce_live` and requires
`AIRFORCE_API_KEY`, `ONEOXYGEN_RUN_AIRFORCE_LIVE_TESTS=1`,
`ONEOXYGEN_AIRFORCE_TEST_MODEL`, explicit gateway acknowledgement in the test, and synthetic
input. Neither marker runs by default.

Format, lint, and check patch whitespace:

```powershell
python -m ruff format .
python -m ruff check .
git diff --check
```

## Current Limitations

- Only Linux containers are supported.
- Coordination is local and single-host; there is no continuously running daemon or distributed
  worker fleet.
- Only OpenAI has an official provider-batch backend. Api.Airforce is direct-only and
  experimental.
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
- There is no dollar-cost calculation, pricing table, grading, benchmark scoring, RAG,
  financial-document tooling, spreadsheet tooling, full JavaScript browser automation, or web UI.
  Tasks may explicitly enable the provider-neutral, read-only `browser_open` text browser for
  exact public source profiles while container networking remains disabled; see
  [the detailed browser integration specification](#browser-integration-for-us-financial-due-diligence-evaluations).
- Managed Chrome, Firefox, Brave, Edge, Safari, Opera, and Vivaldi policy bundles are compilation
  outputs, not active GUI browser backends. They are not installed, launched, or claimed as a
  security boundary by the current runtime.
- The repository has no direct Anthropic/Claude adapter. An external or future adapter using the
  common `ModelAdapter` contract can use the browser tools, but the current CLI cannot start a
  Claude run directly.
- SQLite state, retained run records, checkpoints, and artifacts are not encrypted or signed.
- Base-image provenance is tag-based; deployments needing bit-for-bit provenance should mirror
  and digest-pin the image.

## Deferred Provider-Batch Work

The next provider-batch phase will implement and test separate Anthropic Message Batches, Gemini
Batch, and xAI Batch backends against the common `BatchBackend` contract. Their current official
architectures differ in submission format, status model, result retrieval, and compatibility
rules, so they will not be thin aliases for OpenAI JSONL. DeepSeek batch work remains deferred
until an official documented architecture is selected. Phase 3B intentionally contains no
Anthropic, Gemini, xAI, or DeepSeek batch implementation.
