# Local llama Hermes — Agent Console implementation plan

Status: implementation-ready design; NOT IMPLEMENTED. Contract status: proposed,
not fixture-frozen or tested. Written 2026-09-14. Runtime owner: Hermes;
consumer: EterniaLauncher Agent Console. Implementation order: H0 → H1 → H2 → L1 → Q1.
The operator approved the product scope and the Hermes-owned/RPC architecture;
this document completes planning, not implementation.

## 1. Outcome and scope

An operator selects **Local llama Hermes** in the existing provider picker,
selects a saved GGUF model, then controls it from five buttons beside that picker:
**Turn on**, **Turn off**, **Load weights**, **Unload weights**, **Settings**.
No terminal window is needed. Loading uses a reviewable parameter panel.
Unloading releases model allocations and preserves all GGUF files.

Hermes owns processes, disk paths, catalog, configuration, admission, inference
routing, and observed state. Flutter owns rendering and sends JSON-RPC requests
over the existing bridge. It never starts a process, scans a model folder, or
calls a llama.cpp HTTP endpoint directly. Local means on the selected Hermes
installation. A paired operator console may manage that installation through
its existing authenticated connector; autonomous peer agents gain no new rights.

V1: operator-managed executable, installed GGUF files, one loaded model per
runtime root, manual start/load, Windows CUDA first. The service boundary stays
portable; unsupported operating systems return a capability result. No binary
downloads/upgrades, model downloads, multimodal projector support, speculative
decoding, multi-model scheduling, or arbitrary shell/CLI argument editor in V1.
Existing upstream Hermes terminal workflows are outside this feature's scope.

## 2. Evidence and integration anchors

Inspected primary baselines: Hermes `5430b7840a`, Launcher `4bcb51341`.
Rebase implementation worktrees on current main and revalidate these symbols.

| Existing anchor | Constraint / integration |
| --- | --- |
| `agent_runtime/serve_rpc.py`: `method`, `RpcContext`, `manifest`, `ok`, `err` | Extend the named JSON-RPC 2.0 lane. Method handlers run on the reader path; never load weights inline. Add methods without changing existing shapes or bumping `RPC_CONTRACT_VERSION` solely for new names. |
| `agent_runtime/call_authorization.py` | Use transport-proven callers and existing read/console tiers. Do not add methods to the peer-agent allowlist. |
| `hermes_cli/harness_parts/serve.py`; `agent_runtime/serve_socket.py` | Bind manager ownership to the runtime-root socket owner, including shutdown/restart behavior. A secondary stdio-only serve must not start a competing manager. |
| `agent_runtime/config.py`: `harness_root_config_path`; `agent_runtime/paths.py`: `store_root` | Configuration is install/root scoped, not whichever persona profile happens to be active. Reuse existing root resolution and store I/O conventions. |
| `hermes_cli/harness.py`: `build_provider_visibility`, `_provider_visibility_catalog` | Existing typed provider visibility assembly. Add the local capability block here; no credential-pool entry or fake API-key connection. |
| `agent_runtime/profile_readiness.py`; `agent_runtime/profile_runner.py`: `_resolve_request_runtime` | Route the reserved local provider through one fork-owned adapter in readiness and execution; audit prewarm/cache callers too. |
| `agent_runtime/persona_chat_actor_prewarm.py`; `agent_runtime/mission_chat_turn_context.py` | Preserve effective-model cascade and context budget. Prewarm must not start llama or load a model. |
| `hermes_cli/harness_parts/persona_commands.py`: model-setting commands | Preserve instance vs persona-default writes and the existing selection cascade; verify reserved local provider/model IDs round-trip. |
| `EterniaLauncher/lib/features/mission_control/data/mission_control_hermes_visibility.dart` | Decode the added local catalog/capability block and merge into existing provider presentation. |
| `EterniaLauncher/lib/features/mission_control/agent_chat/mission_agent_model_switcher_view_model.dart` | Separate selectability from inference readiness; saved local models remain selectable when off/unloaded. |
| `EterniaLauncher/lib/features/mission_control/agent_chat/mission_agent_chat_panel_parts/agent_model_menu.dart`; `mission_agent_chat_panel.dart` in the parent folder | Existing provider menu, per-agent selection, and toolbar insertion. Retain existing menu chrome. |
| `EterniaLauncher/lib/features/mission_control/state/mission_control_provider.dart`: `missionAimedMethodLaneCallProvider` | Mandatory active-install transport binding, including remote reconnect. Never bind directly to the local session. |
| `EterniaLauncher/lib/features/mission_control/data/mission_runtime_rpc_manifest.dart` | Feature-detect method support and caller authority from the selected install's greeting. No shell fallback on an unsupported runtime. |

The installed CUDA build identifies as b10809. Its `--help` advertises
`--models-dir`, `--models-preset`, `--models-max`, and `--no-models-autoload`.
The Qwen 27B Q4_K_M model file and RTX 4090 were found in the planning session.
No model was loaded and no lifecycle endpoint was exercised during planning.
Machine paths belong in operator configuration, never committed defaults.

Upstream documents router mode, named INI presets, `POST /models/load`,
`POST /models/unload`, and `GET /models`:
[llama.cpp server reference](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).
Those endpoints are a design dependency, not proof of the installed build.
H0 verifies the installed executable and records its version and capabilities.

## 3. Fixed product decisions

Provider ID: `local-llama-hermes`; display name: **Local llama Hermes**.
Model IDs are immutable UUIDs assigned on import; display names and GGUF paths
may change without breaking agent selections. The inference alias is
`hermes-local-<model UUID>`; a filesystem path is never an inference model ID.
Provider/model identity remains visible as local even though the internal
OpenAI-compatible client uses `provider=custom`, `api_mode=chat_completions`.

The server is a llama.cpp router with autoload disabled and `models-max=1`.
Start means an empty router; load means a model worker; unload keeps the router;
stop means router and owned workers are confirmed gone. No silent cloud fallback.
Changing provider or closing a panel does not stop or unload anything.
Closing Launcher leaves Hermes and llama running. Stopping/restarting the Hermes
service tears down its managed children; next service startup reports off.
Do not automatically restart a failed model or restore loaded weights in V1.

Loading another model is an explicit replacement: the panel names the currently
loaded model and affected agents, and its final action reads **Unload X and load Y**.
If replacement fails, X stays unloaded and Y reports failure; do not claim rollback.
No extra confirmation for ordinary start, stop, unload, or settings save.
Stop/unload/replacement is refused while any protected local-model turn is active.
The operator can use existing chat cancellation, wait for it to settle, then retry.

## 4. Runtime ownership and state machine

Add a focused `agent_runtime/local_llama/` package: `config.py`, `catalog.py`,
`manager.py`, `process.py`, `router_client.py`, `operations.py`, `provider.py`,
and `rpc.py`. Names are proposed new files, not existing APIs. Keep registry
registration thin; import/register through the existing serve initialization path.

One manager per resolved runtime root. Acquire an OS-held exclusive lock before
process mutation; scope disk state under `store_root()/local_llama/`. Config lives
under the `local_llama` key in the root config selected by
`harness_root_config_path()`. Store operations, catalog identities, generated INI,
ownership metadata, and bounded logs under the feature store. Use existing atomic
I/O and lock helpers. Do not put local absolute paths into realm synchronization.
The manager starts lazily on the owning serve and stays root-bound when a worker
enters a persona profile context. Local inference outside that owner's managed
execution lane must refuse with `manager_unavailable`, not create another manager.

Server state: `off | starting | running | stopping | failed`.
Model state: `unloaded | loading | ready | unloading | failed`.
Observed state is authoritative: a successful command acceptance is not readiness.
Track `epoch` (new manager incarnation UUID), monotonic `revision` (all visible
changes), `config_revision` (settings/catalog edits), and model preset revisions.
No synthetic percentage: `progress` is null unless llama reports measurable progress.

All transitions and inference admission share one lock. A mutating operation
reserves the manager before background dispatch; competing mutations return busy.
An admitted local turn obtains a model lease before agent construction and keeps
it through tool loops, compression, finalization, and cancellation cleanup; release
in `finally`. Stop/unload/reload sees leases under that same lock. The boundary
must cover all managed local inference consumers, not just the visible chat widget.
Prewarm and readiness probes are non-loading, non-leasing reads. Generation settings
are captured once for each admitted turn; no mid-turn preset mutation.

Launch with an argument array and hidden Windows process flags, never shell text.
Own router descendants with a kill-on-close Windows Job Object; process creation
must assign ownership before children can escape (suspended launch then resume).
Use PID plus creation identity/ownership handle, never process name, to terminate.
On explicit stop, try graceful shutdown for 10 seconds, then terminate only owned
children, and verify exit. On service crash, the job closes and children exit.
At startup reconcile persisted operation/ownership records; stale PIDs are not
evidence of ownership. An unprovable live process produces `ownership_unverified`;
never kill or adopt an unrelated server, even if its port matches.
Mark any accepted, unfinished operation from the old epoch `interrupted`. A planned
serve shutdown follows existing active-turn drain rules, then interrupts lifecycle
work and releases owned processes; it must not wait 600 seconds for a load to finish.

Use loopback binding only. Generate a runtime-owned API credential and inject it
into the internal client, keeping it out of RPC results and logs. This does not
create a cloud credential or require an operator API key. H0 verifies whether the
installed build protects management endpoints as well as inference; do not claim
management authentication beyond measured behavior. Existing bridge authentication
is the operator control boundary. Reject occupied ports instead of selecting or
killing another process. Disk model contents are never deleted by lifecycle verbs.

Timeout defaults: start 30s, load 600s, unload 60s; status router probe 2s;
individual HTTP connect 2s. RPC mutation acknowledgment targets <1s and never waits
for these deadlines. Status returns the cached manager projection without blocking
on HTTP. A watchdog performs bounded probes. Failed load cleans up that worker,
preserving the router when healthy; an unresponsive router becomes failed and
needs stop/start. Persist the terminal error and log reference.

## 5. Configuration and discovery

`local_llama` root config has `schema_version: 1`, `executable_path: string|null`,
`port: int` (default 8080, 1024–65535), `model_roots: string[]`,
and `presets: ModelPreset[]`. Missing block means unconfigured, not unsupported.
Root settings changes while running return `restart_required` without applying;
the settings UI explains Stop → Save → Turn on. V1 does not auto-start on boot.

ModelPreset fields (all required, nullable only where specified):

| Field | Type / default / validation |
| --- | --- |
| `model_id`, `display_name`, `gguf_path` | UUID; nonempty name ≤120 chars; absolute existing file on Hermes host |
| `revision` | Nonnegative integer assigned by server |
| `load.context_size` | Integer, default 32768; ≥4096, multiple of 256, ≤ metadata maximum when known |
| `load.gpu_layers` | `auto` or nonnegative integer; default `auto` |
| `load.flash_attention` | `auto | on | off`; default `auto`, validated against binary support |
| `load.cache_type_k`, `load.cache_type_v` | `f16 | q8_0 | q4_0`; default `f16`; reject unsupported combinations |
| `load.chat_template_path` | Absolute file path or null (embedded template); Jinja enabled |
| `generation.temperature` | Number 0–2, default 0.7 |
| `generation.top_p`, `generation.top_k` | Number (0,1], default 0.9; integer 0–1000, default 40 |
| `generation.max_output_tokens` | Positive integer, default 4096; strictly below effective context |
| `generation.thinking` | `auto | on | off`; default `auto`; expose on/off only after template capability proof |

These defaults are starting values, not a performance guarantee for Qwen. Hermes
must use the observed configured context, not the model's theoretical maximum.
Capability-dependent flags are hidden/disabled with a reason, not silently dropped.
V1 parameters are structured and allowlisted; no arbitrary extra argument string.

Catalog scanning is explicit (`catalog.scan`), off the reader thread, never on a
snapshot build or periodic status poll. Scan only configured roots without following
directory symlinks/junctions. Bound a scan at 10,000 candidates and 60s; report
truncation/errors rather than treating partial discovery as complete. Parse metadata
with a bounded header reader; do not read entire tensors. Group split GGUFs by their
metadata and validate all parts; expose only one model row for a complete set.
Exclude projector files and reject incomplete/unsupported models with a reason.
Unconfigured/empty catalog offers **Configure local llama** through Settings.
Scan imports new complete models with server-assigned UUIDs and default presets in
one atomic catalog/config commit. Match later scans by canonical file identity/path;
do not rename IDs on display-name edits. Missing existing files retain their IDs
and become unselectable with `missing_file`. Explicit path edits preserve the ID;
no automatic deduplication based on display name and no full-file hashing on scan.

Native file picking applies only when Launcher and selected Hermes are the same
host. For remote installs, use an explicitly host-labeled absolute-path field and
server-side validation/scan; never submit a local file-picker result as a remote path.
Settings distinguish saved parameters from parameters active in the loaded worker.
Preset edits may be saved while ready; they take effect on next load. Generation
settings passed to Load remain the worker's defaults until its next load, and each
admitted turn captures them. Saving a preset never mutates a running turn.

## 6. JSON-RPC contract (proposed v1)

Transport: existing stdio/socket/gateway JSON-RPC 2.0; no REST wrapper or argv
fallback. The selected transport supplies runtime root and caller identity.
Every result carries `schema: "hermes.local_llama/v1"` and `install_id: string`.
The caller never selects a filesystem root through RPC parameters.
Register exact methods and tiers in the existing manifest:

| Method suffix under `runtime.local_llama.` | Tier | Params beyond shared fields | Result |
| --- | --- | --- | --- |
| `status` | read | optional `operation_id: UUID` or `request_id: UUID` lookup (mutually exclusive) | State plus matching operation or null |
| `config.get` | console | none | Full config, config revision, preset revisions, capability flags |
| `config.set` | console | `config: full config` | Operation; background path validation and atomic save |
| `catalog.scan` | console | none | Operation; refresh catalog from saved roots |
| `start` | console | none | Operation |
| `stop` | console | none | Operation |
| `load` | console | `model_id: UUID`, `preset_revision: int`, `load: complete load object`, `generation: complete generation object`, `replace_model_id: UUID|null` | Operation |
| `unload` | console | `model_id: UUID` | Operation |
| `logs.get` | console | `cursor: string|null`, `limit: int` default 100, max 200 | Sanitized lines, next cursor, truncated boolean |

All mutations (`config.set`, scan, start, stop, load, unload) require
`request_id: UUID`, `expect_epoch: UUID`, `expect_revision: int`, and
`expect_config_revision: int`. Unknown top-level fields follow existing method-lane
compatibility rules; unknown keys within the versioned configuration/parameter
objects are validation errors so a typo cannot silently change inference behavior.
Do not add paths or operation progress to the parity/snapshot envelope.

State result fields, required unless marked nullable:

```json
{
  "schema": "hermes.local_llama/v1", "install_id": "install-example",
  "epoch": "00000000-0000-4000-8000-000000000001",
  "revision": 12, "config_revision": 3, "configured": true,
  "capabilities": {"supported": true, "reason": null,
    "router_load_unload": true, "parameter_schema_version": 1},
  "server": {"state": "running", "error": null},
  "models": [{"model_id": "00000000-0000-4000-8000-000000000002",
    "display_name": "Qwen 27B Q4_K_M", "preset_revision": 1, "context_length": 8192,
    "state": "unloaded", "selectable": true, "unavailable_reason": null,
    "active_parameters": null, "error": null}],
  "active_turns": [], "operation": null
}
```

`configured` reports whether this install has a saved executable; it is not a
health assertion. `models[].context_length` is the saved preset context size.
These additive read-tier fields let a remotely aimed provider menu discover the
selected host's catalog while off, without reading paths or falling back to the
Launcher's local provider probe.

`active_turns` entries contain `turn_id`, `persona_instance_id`, and `model_id`
(all strings). `active_parameters` is null when unloaded, otherwise the exact
`load` and `generation` objects plus `effective_context_size: int`. Progress and
paths are not required for read-tier viewers. Each error is null or
`{reason: string, message: string, retryable: bool, log_ref: string|null}`.

Mutation results contain `operation` and current State (under `state`). Operation:
`operation_id`, `request_id` (UUIDs), `kind` (method suffix), `state`
(`queued|running|succeeded|failed|interrupted`), `model_id: UUID|null`,
`progress: number|null` (0–1), `error: Error|null`,
`started_at: ISO8601|null`, `finished_at: ISO8601|null`.
Persist acceptance before returning. Echo the same operation for an exact repeated
request ID and normalized payload, before checking revision guards; different payload
with the same ID returns `idempotency_conflict`. Retain terminal receipts for seven
days (maximum 1,000; evict oldest terminal first, never active). Reject expired
client retries by epoch/revision; never treat an unknown old ID as safe to replay.
Every newly accepted mutation, including a terminal no-op, advances the revision,
so a receipt evicted within the same epoch cannot pass its original guard again.
Status without a lookup returns the active operation, or the most recent terminal
operation, or null when none exists. An unknown explicit lookup returns `4001`.

For example, a load request (illustrative UUID values) is:

```json
{"jsonrpc":"2.0","id":"rpc-17","method":"runtime.local_llama.load","params":{
  "request_id":"00000000-0000-4000-8000-000000000003",
  "expect_epoch":"00000000-0000-4000-8000-000000000001",
  "expect_revision":12,"expect_config_revision":3,
  "model_id":"00000000-0000-4000-8000-000000000002","preset_revision":1,
  "replace_model_id":null,
  "load":{"context_size":32768,"gpu_layers":"auto","flash_attention":"auto",
    "cache_type_k":"f16","cache_type_v":"f16","chat_template_path":null},
  "generation":{"temperature":0.7,"top_p":0.9,"top_k":40,
    "max_output_tokens":4096,"thinking":"auto"}}}
```

The reply echoes JSON-RPC `id=rpc-17` and `result={schema, install_id, operation,
state}`. A busy refusal is `error={code:4090, message:"A local model turn is active",
data:{reason:"active_turns", active_turns:[{turn_id, persona_instance_id, model_id}]}}`.
`config.get` returns `{schema, install_id, config_revision, config, capabilities}`;
its capability map includes `supported`, nullable `reason`, `router_load_unload`,
`parameter_schema_version`, and `supported_values` keyed by conditional parameter.
`logs.get` returns `{schema, install_id, lines:[{time, level, message}], next_cursor,
truncated}`; cursor null starts at the most recent bounded tail. Never accept an
arbitrary path as a log source. Scrub credentials and prompt/completion bodies.

RPC validation failures use existing codes: `-32602` for invalid values,
`4001` for missing model/operation, `4090` for guards. Branch on `error.data.reason`:
`stale_revision`, `stale_epoch`, `idempotency_conflict`, `operation_busy`,
`active_turns`, `replacement_required`, `model_not_ready`, `restart_required`.
Use `-32000` with `unsupported_binary`, `unsupported_platform`, `unconfigured`,
`port_in_use`, `missing_file`, `incomplete_model`, `ownership_unverified`,
`manager_unavailable`, `router_failed`, or `timeout` for operational failure families. Background failures
settle the accepted operation with the same reason vocabulary. Preserve existing
authorization error envelopes unchanged. Error messages are human copy, not enums.

Repeat start while running and unload of an already-unloaded known model succeed
as terminal no-ops. Load of the same model and identical active parameters succeeds
as a no-op; changed load parameters require explicit replacement of that model.
Stop while already off succeeds. Busy/active-turn guards still apply before any
operation that would actually change state. Load while off returns `model_not_ready`
with "Turn on llama first"; it does not implicitly start the server.

Launcher polls status once per second while an operation is active and every five
seconds while the local provider toolbar is visible and idle. One poller per
selected installation, shared across panels; dispose timers on detachment. On
reconnect, get current state and look up the pending request ID before any retry.
On transport timeout, label outcome unknown until reconciled; do not mint a new ID.
Switching installations discards the previous view only, not its operation; key
pending operations by install ID. No polling of hidden cloud-only panels.

## 7. Provider and inference integration

Add a failure-isolated `local_llama` block to `build_provider_visibility()`:
`{schema: "hermes.local_llama.catalog/v1", provider_id, display_name,
configured: bool, models: [{model_id, display_name, context_length: int|null,
selectable: bool, unavailable_reason: string|null}]}`. This is saved catalog
metadata, not live HTTP health; it remains present when off and does no disk scan.
The existing catalog shows this provider without inventing pooled credentials.
An absent block means unsupported/older runtime, not zero models.

The fork-owned provider adapter recognizes only the reserved provider ID. Resolve
its model UUID against the same runtime root's catalog, verify readiness, and
produce the managed alias, loopback `/v1` base URL, internal credential, configured
context, and generation defaults. Preserve nonlocal resolution byte-for-byte.
Wire the adapter into readiness and `_resolve_request_runtime`; invalidate/rekey
runtime and readiness caches by epoch/config/model revision so a stopped or replaced
worker can never be used from a stale success. Gate actual execution with the lease
regardless of cached readiness. Verify instance/default selection, copy/paste,
apply-to-agent, resumed sessions, and actor prewarm use the same provider identity.

Every auxiliary call made as part of a managed local turn (compression and other
runner-owned model calls) uses this endpoint or is explicitly unavailable. It must
not silently choose a cloud default. Tool services that intentionally use unrelated
external APIs retain their existing behavior. Apply generation options through
existing client/request configuration; do not forward llama CLI flags as HTTP JSON.
H0/H2 must demonstrate structured tool calls with the target GGUF/template, not
only a text completion. If unsupported, report it and keep agent chat unavailable
for that preset rather than claiming agent compatibility.

## 8. Launcher implementation slice

Add `data/mission_local_llama_rpc.dart` (DTOs/client),
`state/mission_local_llama_provider.dart` (install-keyed shared status/operations),
`agent_chat/mission_local_llama_controls.dart` (pure availability projection and
toolbar), and `agent_chat/mission_local_llama_settings.dart` (settings/load panels).
All paths are under `EterniaLauncher/lib/features/mission_control/`.
Wire via `missionAimedMethodLaneCallProvider`; respect the selected connection's
manifest and tier, including remote reconnect. No direct `callRpc` binding elsewhere.

Five controls appear only when the effective selection is the managed local
provider. Keep them visible and disabled with explanations in unavailable states;
cloud selections hide them. Wrap into a second toolbar row at narrow widths rather
than squeezing or overflowing. Status shows server and selected-model states,
selected install name, and affected agent names when busy. Do not show cloud price
or subscription usage as local inference cost. Unknown VRAM usage stays unknown.

| State | Enabled controls (subject to console permission) |
| --- | --- |
| Unconfigured | Settings |
| Off, configured | Turn on, Settings |
| Running, selected model unloaded | Turn off, Load weights, Settings |
| Running, selected model ready, idle | Turn off, Load weights (edit/reload), Unload weights, Settings |
| Active local turn | Settings; generation/load edits can be saved for later, no lifecycle mutation |
| Transition in progress | Settings view only; operation progress/error panel remains readable |
| Failed | Settings/logs and Turn off (cleans up owned children and clears failure even when none remain); Turn on after confirmed off |
| Disconnected / unsupported | Settings explanation only; no mutation or local fallback |

Load panel uses selected saved preset, allows parameter changes, captures matching
revision tokens, names any replacement, and submits one explicit operation. Changed
parameters apply to this load; saving as the preset is a separate labeled settings
action. Chat send is disabled with **Turn on llama** or **Load weights** reason
until ready; backend admission enforces the same rule for all clients.

## 9. Work packages and proof gates

### H0 — prove installed llama.cpp capabilities

Use an isolated runtime root and unused loopback port; never stop the operator's
live server or launcher. Record binary version/hash, supported flags, and GPU facts.
Start router without weights using generated presets and autoload disabled; verify
health, load the operator-selected existing Qwen GGUF, verify `/v1/models`, text
completion, structured tool call/result round-trip, unload, then stop. Prove whether
editing an INI preset can be applied safely while the router stays up using the
installed refresh mechanism. If it cannot, freeze an explicit empty-router restart
inside load/replacement while no model/lease exists; report `starting` during it.
No runtime/UI contract may promise in-place parameter reload without this proof.
Record management authentication behavior. Missing capability stops this dependency
gate with an actionable binary compatibility error; no fake implementation fallback.

### H1 — manager, persistence, lifecycle, and RPC

Implement sections 4–6 in fork-owned modules and minimal existing registry/serve
hooks. Integrate shutdown ownership; avoid broad upstream-core edits. Backend owns
the finalized DTO contract and fixtures before Launcher integration starts.
New tests: `tests/agent_runtime/test_local_llama_config.py`,
`test_local_llama_manager.py`, `test_local_llama_rpc.py`,
`test_local_llama_process.py` (same directory). Cover path/parameter validation,
split models, partial scan, state transitions, ownership/PID reuse, hidden windows,
job cleanup, busy races, deadlines, failures, durable idempotency, revision conflicts,
and service restart. Exercise actual JSON-RPC dispatch and read/console/peer callers.
Use controlled child processes and a local fake router for deterministic fault cases;
the real-binary H0/Q1 lane supplies actual model proof.

### H2 — provider inventory, routing, admission

Implement section 7. New `tests/agent_runtime/test_local_llama_provider.py` covers
off/unloaded selection, typed refusal before agent construction, inference alias,
parameters/context propagation, profile isolation, cache invalidation, local-only
auxiliary routing, and lease release on every terminal path. Extend
`tests/test_provider_visibility_v2.py` for additive catalog/failure isolation.
Run real imports and a temporary Hermes root/profile; mocked dictionaries alone
do not prove resolution. Freeze emitted example payloads for L1 at this gate,
including absent capability, empty catalog, loaded, busy, failed, and disconnected.
Do not assert fixed catalog counts or read source text as behavioral proof.

### L1 — provider, controls, settings, remote routing

Consume H1/H2 fixtures through DTOs and view models, then wire sections 7–8.
Extend `test/features/mission_control/mission_agent_model_switcher_view_model_test.dart`;
add `mission_local_llama_rpc_test.dart`, `mission_local_llama_controls_test.dart`,
and `mission_local_llama_settings_test.dart` in the same test directory.
Prove all availability-table rows, keyboard/tooltips/layout at narrow width,
selected-vs-loaded model, replacement, pending revisions, timeout reconciliation,
poller disposal/sharing, and cloud-provider regression. Test a remote selected install
with a live local session to prove no command reaches local; cover reconnect and
older remote runtime absence. Keep `mission_method_lane_chokepoint_test.dart` green.

### Q1 — integrated real-model and Stage C acceptance

Use the existing Stage C MCP path with the correctly built Launcher/runtime.
Capture provider selection, all five controls, parameter panel, loading, ready,
chat text and tool call, shared-agent busy refusal, unload, and off. Verify model
memory allocations are released after unload; CUDA context/driver allocations need
not return to zero. Prove duplicate clicks and disconnect/reconnect produce one
operation, invalid path and occupied port show actionable errors, a crash does not
leave uncontrolled descendants, cloud selection hides controls, and no terminal
window appears. Test commands against a second paired installation and verify only
that host changes; if no second host is available, explicitly report remote live
proof unmet and do not claim full multi-device delivery.

Commands to run during implementation (new files must exist first):

```text
# Hermes, Git Bash, from its task worktree; never invoke pytest directly:
scripts/run_tests.sh tests/agent_runtime/test_local_llama_config.py tests/agent_runtime/test_local_llama_manager.py tests/agent_runtime/test_local_llama_rpc.py tests/agent_runtime/test_local_llama_process.py tests/agent_runtime/test_local_llama_provider.py tests/test_provider_visibility_v2.py
# Launcher, from its task worktree:
flutter test test/features/mission_control/mission_local_llama_rpc_test.dart test/features/mission_control/mission_local_llama_controls_test.dart test/features/mission_control/mission_local_llama_settings_test.dart test/features/mission_control/mission_agent_model_switcher_view_model_test.dart test/features/mission_control/mission_method_lane_chokepoint_test.dart
```

Also run focused analyze on changed Dart paths and existing method-tier/dispatch,
readiness, runner, and cross-repo wire fixture tests affected by the finalized diff.
New methods change the greeting method set: regenerate only intentional fixture
changes from the producer and verify the matching Launcher fixtures. Do not add
diagnostic keys to parity to get lifecycle status on screen. Tests with deliberate
waits beyond the repository timeout must declare their own bounded timeout.

## 10. Delivery and completion

This plan is one canonical cross-repo document. Launcher queue holds one pointer.
Implementation claims that queue row before work, then uses fresh dedicated
worktrees from main in both repositories. Backend contract status must become
tested (or explicitly fixture-frozen for UI-only work) before L1 consumes it.
Each implementation commit carries its scoped paths, focused proof, and any actual
contract amendments in this plan. Land fast-forward-only, verify local/remote main
and primary-checkout synchronization, and remove landed worktrees with git.
Preserve unrelated dirty worktrees; never force a landing over conflicting changes.

Complete only when H0–Q1 evidence exists, the real model executes an agent tool
round-trip, and both repositories contain the integrated commits on origin/main.
Fold implemented runtime facts into domain docs 03/04/05 and Launcher chat/transport
canon; archive this plan with shipping SHAs/evidence and remove its queue pointer.
Until then the phase checklist is entirely open. Planning ran no product tests and
provides no claim that the local provider, toolbar, or RPC methods already exist.
