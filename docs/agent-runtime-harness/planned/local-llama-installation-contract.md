# Local llama Hermes installation and management contract

Status: **proposed, source-reviewed; not implemented or fixture-frozen**.
Reviewed 2026-09-14 against Hermes `31bf01536a` and Launcher UX plan
`d3d6e02c3`. This is the backend extension of
[the canonical local llama plan](local-llama-agent-console.md), not a replacement
for its implemented inference/lifecycle contract. Launcher owns the visual plan
at `docs/mission_control/planned/local-llama-manager-ux.md` in its repository.
No installation, runtime restart, model download, or product change was performed
to write this plan. New methods below must not be enabled before producer proof.

## 1. Review decisions and existing implementation

Approve the host-scoped management panel, explicit server/weights distinction,
manual fallback, and Location → Release → Review wizard. Installation produces
a validated, inactive installation; **Use this installation** is a separate
activation. Neither step starts llama nor loads weights. Adjust the UI diagram's
“validated and activated” edge to represent two operations. Keep the five controls
outside the scrolling section body, with Settings always reachable as a view.

Actual Hermes source observations:

| Source | Implemented fact / implication |
| --- | --- |
| `agent_runtime/local_llama/rpc.py` | Nine v1 methods. Only `status` is read-tier; config, logs, scan and lifecycle require console tier. No setup methods exist. |
| `manager.py:status` | A lookup replaces `operation` with the requested receipt, hiding another currently active operation. Separate these facts before extending reconciliation. |
| `manager.py:submit`, `lease` | One mutation slot, epoch/global/config guards, durable request fingerprints, shared inference lock. Reuse this owner rather than create an independent installer process controller. |
| `manager.py:_initialize`, `_persist`, `_save` | Receipts survive restarts, in-flight entries become interrupted; retention currently 1,000 / seven days. Config YAML and revision/receipt files are separate writes, not one transaction. Installer crash recovery needs an explicit journal. |
| `config.py:ConfigStore` | Full config replacement preserves other root YAML settings. UI must carry the draft's original config revision, not attach the latest status revision to old text. |
| `router_client.py:probe_binary` | Start probes `--help` with a ten-second timeout. No persisted version, installation inventory or hardware recommendation. Capability flags alone are not proof of a usable binary. |
| `catalog.py:scan` | Scan already creates stable UUID presets, retains existing entries, groups shards, skips links/projectors, caps candidates/time and returns errors/truncation. Extend its report; do not create a second model importer. |
| `manager.py:_execute` | Stop tears down owned router/workers; unload uses actual loaded UUID. Removing a loaded preset is refused. Executable/port/model-root edits require server off. Load failure after replacement does not restore old weights. |
| `manager.py:logs_get`, `process.py` | Current log ring is bounded process-memory operation summaries. It is not a native diagnostic log archive; cursor lacks epoch binding. Preserve this distinction in the UI. |

Current status `configured` means executable string exists, not binary detected or
compatible. Read-tier status is not authority to expose paths, hardware serials,
download locations or detailed installer receipts. Existing grant enforcement and
root selection stay in the authenticated serve dispatcher; never accept a runtime
root, endpoint, shell command or authorization tier in request parameters.

## 2. Official upstream and supported rollout

Source checks: [official release API](https://api.github.com/repos/ggml-org/llama.cpp/releases),
[official latest release](https://api.github.com/repos/ggml-org/llama.cpp/releases/latest),
[GitHub asset contract](https://docs.github.com/en/rest/releases/assets), and
[server reference](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).
These are live upstream dependencies, not fixed version recommendations.

At review, latest was `v0.4.1` with only `nightly-tag.txt`; build `b10970` was a
prerelease containing platform archives and separate CUDA runtime archives.
Therefore neither “latest is installable” nor “discard all prereleases” is valid.
Resolve a stable pointer only from the official asset, bounded to 128 bytes and
a validated tag; fetch that tag's official release and pin its release/asset IDs.
Never construct download URLs from user-entered tags. If resolution is unclear,
report unavailable; do not silently select an unrelated build.

Initial automatic-install target: Windows x64 CPU and NVIDIA CUDA ZIP packages.
Other platforms/backends remain discoverable/manual with explicit unsupported
automatic-install reasons. Add Vulkan/ARM/Linux/macOS only with their own real
binary/dependency proof. Hardware recommendations are advisory; unknown driver
compatibility is not compatible. No driver installation, elevation, compiler,
system PATH edits or pip/package-manager side effects. Select CPU only as an
explicit recommendation/choice, never as a hidden fallback after GPU failure.

Release catalog returns upstream channel and Hermes compatibility separately:
`upstream_channel=stable|prerelease`, `qualification=verified|candidate|unsupported`.
“Verified” requires a recorded Hermes qualification for that exact artifact bundle
and OS/backend, not simply a filename match. Latest stable pointer may resolve to
a prerelease-tagged build; show both honestly. Retain user version selection.

## 3. B-00–B-13 dispositions

| Requirement | Backend decision / delivery dependency |
| --- | --- |
| B-00 | Separate `hermes.local_llama.setup/v1` protocol and method manifest. Existing v1 stays decodable by old clients. New protocol is not frozen in this planning commit. |
| B-01 | Bounded detection of configured path, Hermes-managed inventory and PATH metadata; manual candidate validation. No whole-drive recursive hunt. Unknown/unreadable search remains inconclusive. |
| B-02 | Typed host path validation first. Remote folder browser deferred and advertised false; UX already permits typed paths. No generic unrestricted filesystem browser needed for initial release. |
| B-03 | Windows OS/arch/RAM/CPU features plus bounded NVIDIA inventory using a trusted system/driver executable, no shell. Unknown memory/driver facts stay null with reasons; no benchmark or serial-number collection. |
| B-04 | Hermes-only official release resolver, ETag cache, bounded pages, pinned asset IDs/digests and explicit stable-pointer support. |
| B-05 | Expiring, principal/root-bound review plan with all artifacts, compatibility, disk budget and warnings. No writes to destination during preflight. |
| B-06 | Durable staged apply, verified bytes, safe extraction, isolated binary qualification, immutable publication. Activation is separate. |
| B-07 | Cancel installer transfer/extraction only; no invented model-load cancellation. Retry after known terminal result, no automatic range resume in first release. |
| B-08 | Extend existing scan operation report with per-root results and artifact metadata; preserve preset UUIDs. No automatic model downloads. |
| B-09 | External executable adoption supported. Managed offline import first accepts an exact previously trusted official artifact bundle with digests already cached; arbitrary archive import deferred. Never delete/adopt ownership of source files. |
| B-10 | Keep complete existing load/generation DTO and actual model guards. Add structured field errors; memory estimate remains nullable/advisory. Defer separate `load.plan` RPC until a validated estimator exists. |
| B-11 | Distinct current operation and requested receipt; journal recovery and bounded durable lookup. Historical success never unlocks around another active operation. |
| B-12 | Guarded activation with server off/no turns; retained validated version supports explicit rollback. No automatic restart/reload or model rollback claim. |
| B-13 | Setup logs bound to host/epoch/operation and console authorization. Read-tier gets only sanitized activity summary. No prompt bodies, credentials, arbitrary log files or upstream raw exception dumps. |

## 4. Proposed wire contract packet

Owner: Hermes. Consumer: Launcher aimed method lane. Status: **proposed**.
Transport: existing JSON-RPC, prefix `runtime.local_llama.`; existing permission
envelopes unchanged. Every success has required `schema`, `install_id`, `epoch`.
Setup schema is `hermes.local_llama.setup/v1`. `install_id` always means the Hermes
host. A llama binary has a distinct `installation_id` UUID.

All listed fields required unless marked optional `?`; nullable values are marked
`|null`. UUIDs are canonical strings; timestamps are UTC ISO-8601; revisions and
byte counts are nonnegative integers (JSON-safe ≤ 2^53−1). Unknown response fields
are ignored; unknown state/schema means unsupported/unknown and blocks mutation.
Reject unknown input fields. Bound paths 4,096 chars, IDs 128 chars (UUID fields
stricter), page limits 1–100, user labels 120 chars and lists at documented caps.

| Suffix / tier | Request payload (besides JSON-RPC id) | Result payload (besides common envelope) |
| --- | --- | --- |
| `setup.capabilities` / read | `{}` | `features:{detect,path_validate,hardware,releases,install,cancel,activate,offline_import,host_browser:false}`, `automatic_platforms:string[]`, `unsupported_reason:string|null`, `contract_version:1` |
| `setup.status` / console | `request_id?:UUID` OR `operation_id?:UUID` | `inventory_revision:int`, `active_installation_id:UUID|null`, `active_operation:Operation|null`, `requested_operation:Operation|null`, `lookup_state:not_requested|found|not_found`, `observed_at`, `recovery:ready|recovering|blocked` |
| `installations.detect` / console | `{}` | `complete:bool`, `checked_scopes:string[]`, `issues:Issue[]`, `installations:Installation[]`, `observed_at` |
| `installations.validate` / console | `executable_path:string` | `installation:Installation`, `validation_token:UUID`, `expires_at` |
| `host_paths.validate` / console | `path:string`, `purpose:installation_parent|model_root|offline_archive` | `normalized_path:string`, `exists:bool`, `readable:bool`, `writable:bool|null`, `free_bytes:int|null`, `issues:Issue[]`, `observed_at` |
| `hardware.get` / console | `{}` | `os:string`, `architecture:string`, `cpu_features:string[]`, `ram_bytes:int|null`, `gpus:Gpu[]`, `issues:Issue[]`, `observed_at` |
| `releases.list` / console | `channel:stable|prerelease`, `cursor?:string`, `limit?:int=20` | `releases:Release[]`, `next_cursor:string|null`, `cached:bool`, `fetched_at`, `stale:bool`, `retry_after_seconds:int|null` |
| `installation.plan` / console | `release_id:string`, `variant_id:string`, `destination_parent:string`, `source:official_download|offline_cached`, `offline_paths?:string[]` | `plan:InstallPlan` |
| `installation.apply` / console | Guard + `plan_id:UUID`, `plan_revision:int`, `acknowledged_warning_ids:string[]` | `operation:Operation`, `active_operation:Operation|null` |
| `operations.cancel` / console | `request_id:UUID`, `operation_id:UUID` | `target:Operation`, `cancel_disposition:requested|already_requested|already_terminal` |
| `installations.activate` / console | Guard + `installation_id?:UUID` OR `validation_token?:UUID`, `expect_inventory_revision:int`, `expect_active_installation_id:UUID|null` | `operation:Operation`, `active_operation:Operation|null` |
| `setup.logs.get` / console | `operation_id:UUID`, `cursor?:string`, `limit?:int=100` | `lines:LogLine[]`, `next_cursor:string|null`, `truncated:bool`, `cursor_reset:bool` |

Guard = required `request_id:UUID`, `expect_epoch:UUID`, `expect_revision:int`,
`expect_config_revision:int`, using authoritative current runtime state. A plan's
bound config/inventory revisions must ALSO still match; refreshing the guard
does not make an obsolete plan acceptable. For legacy `config.set`, Launcher must
use its draft's config revision. Cancellation deliberately uses no moving progress
revision guard: target identity and authorization are sufficient, otherwise a
progress tick could make Cancel unusable. Same cancellation request ID with a
different target fails idempotency; terminal cancellation is a no-op result.

Records:

- `Issue={reason:string,message:string,field:string|null,retryable:bool}`.
- `Installation={installation_id:UUID|null,executable_path:string,ownership:managed|external,
  state:candidate|validated|invalid,version:string|null,build:string|null,sha256:string|null,
  compatibility:compatible|incompatible|unknown,capabilities:string[],issues:Issue[],
  checked_at:string|null}`. Only managed records have durable installation IDs;
  validated external candidates use short-lived validation tokens for adoption.
- `Gpu={id:string,name:string,backend:string,driver_version:string|null,
  total_bytes:int|null,available_bytes:int|null,compatibility:compatible|incompatible|unknown}`.
  IDs are host-local indexes, not hardware serials.
- `Release={release_id:string,tag:string,upstream_channel:string,qualification:string,
  published_at:string,variants:Variant[]}`. `Variant={variant_id:string,os:string,
  architecture:string,backend:string,compatibility:string,reasons:string[],
  artifacts:Artifact[],expanded_bytes:int|null}`. Enum values follow sections 2/4.
- `Artifact={asset_id:string,name:string,size_bytes:int,sha256:string|null,
  digest_source:github_api|cached_official|unavailable,role:server|runtime_dependency}`.
  Download URLs remain backend-private. Missing trusted digest makes automatic
  installation unavailable; do not label a locally computed hash publisher verification.
- `InstallPlan={plan_id:UUID,plan_revision:int,expires_at:string,release_id:string,
  variant_id:string,destination_parent:string,final_directory:string,
  artifacts:Artifact[],required_free_bytes:int,free_bytes:int,
  dependencies:Issue[],warnings:Issue[],warning_ids:string[],
  config_revision:int,inventory_revision:int,hardware_fingerprint:string,
  source:official_download|offline_cached,activation_required:true}`.
- `Operation={operation_id:UUID,request_id:UUID,kind:install|activate,
  state:queued|running|cancelling|succeeded|failed|cancelled|interrupted,
  phase:queued|downloading|verifying|extracting|validating|publishing|activating|cleaning|finished,
  sequence:int,bytes_done:int|null,bytes_total:int|null,can_cancel:bool,
  accepted_at:string,started_at:string|null,finished_at:string|null,
  result:OperationResult|null,error:Issue|null}`.
- `OperationResult={installation_id:UUID|null,activated:bool,
  cleanup:complete|pending|not_needed,retained_paths:string[],retry_allowed:bool}`.
  A failed/cancelled operation may still have cleanup results. Successful install
  returns `activated:false`. No field means model ready.
- `LogLine={time:string,level:info|warning|error,message:string}`. Cap message length
  at 2,048 chars, redact before storage and response. Opaque cursor binds root,
  operation and log generation; it cannot specify a path.

Empty detection: `complete:true, installations:[], issues:[]` means nothing found
in **the reported scopes**, never absence across the whole machine. Failed scope:
`complete:false, issues:[{reason:"access_denied",message:"Could not read configured location",
field:null,retryable:false}]`. Empty release page has `releases:[],next_cursor:null`.
Unknown hardware values are null, never zero. Missing historical lookup returns
`lookup_state:not_found` alongside fresh `active_operation`; it does not establish
whether the old request executed. A request with both lookup IDs is invalid.

Errors: preserve JSON-RPC `-32602` validation, `4001` missing identity, `4090`
conflict, `-32000` operational failure. `error.data` carries `reason`, `field|null`,
`retryable`, `retry_after_seconds|null`, and sanitized `message` where needed.
Reasons include `plan_expired`, `plan_changed`, `stale_epoch`, `stale_revision`,
`operation_busy`, `active_turns`, `server_must_be_off`, `cancel_not_available`,
`unsupported_platform`, `unsupported_binary`, `digest_unavailable`, `digest_mismatch`,
`unsafe_archive`, `disk_full`, `path_conflict`, `release_unavailable`, `rate_limited`,
`storage_unavailable`, `recovery_required`. Human messages are never dispatch keys.
Accepted failures settle the operation with the same issue vocabulary. Revoked
permissions use existing authorization errors; never downgrade to local access.

Example acceptance (illustrative, NOT a frozen runtime fixture):

```json
{"jsonrpc":"2.0","id":"rpc-7","method":"runtime.local_llama.installation.apply","params":{"request_id":"11111111-1111-4111-8111-111111111111","expect_epoch":"22222222-2222-4222-8222-222222222222","expect_revision":12,"expect_config_revision":3,"plan_id":"33333333-3333-4333-8333-333333333333","plan_revision":1,"acknowledged_warning_ids":[]}}
```

```json
{"jsonrpc":"2.0","id":"rpc-7","error":{"code":4090,"message":"Review the installation again; its facts changed.","data":{"reason":"plan_changed","field":null,"retryable":false,"retry_after_seconds":null}}}
```

## 5. Ownership, transactions and recovery

One root-bound owner and one exclusive mutation reservation spans both protocols.
Initial apply/import requires server **off**, zero leases, no active lifecycle/setup
operation. Hold reservation through publication/cleanup; do not hold a Python lock
across network waits. Legacy start/config/scan/load/unload and inference admission
must inspect this same reservation. No surprise stop, implicit queued activation,
or model eviction. Reads remain available during long operations.

Extend v1 status additively with sanitized `active_operation`, independently of
its legacy `operation` lookup. During setup, old clients see a valid v1 queued/
running busy projection; terminal cancelled maps only in that legacy projection
to interrupted. Never emit `cancelled` inside an existing v1 enum. New clients
read detailed setup states through setup.status. The lease lock, not UI policy,
enforces exclusion even for old/malicious/concurrent clients.

Store setup journal/inventory/receipts under `store_root()/local_llama/setup/`.
Use durable atomic writes, existing cross-process locks and fsync where supported.
An accepted receipt and reservation must be persisted before starting side effects;
failure to persist means no work begins. Fingerprints bind method, normalized
payload and authenticated principal/root; repeat identical request returns the
same receipt before checking changed epoch. Conflicting payload is rejected.
Console principals may inspect/control this install's shared setup work; do not
promise private receipts between console operators. Read-tier sees no detailed
setup records. Retain terminal receipts seven days / 1,000 records, never evict
active/recovery work; record retention limits in capabilities documentation.

Plan lifetime: 15 minutes, immutable after issuance, root/principal-bound and
single accepted operation (duplicate request remains idempotent). Apply rechecks
authorization, source asset ID/digest/size, hardware/dependencies, disk, config and
destination identities. Any changed approved fact returns plan_changed BEFORE
download. Cached offline metadata must carry trusted original digests. Plans can
be invalidated by restart; accepted operations and their journal cannot disappear.

Destination: user chooses a local parent; create only an owned child such as
`llama-<tag>-<variant>-<id>`. Stage in a uniquely owned sibling on the same volume;
publish by atomic rename. Reject nonempty target, reparse/link ancestors, UNC,
device paths, relative/drive-relative paths, alternate data streams and reserved
Windows names for managed installs. Revalidate opened filesystem objects, not just
string prefixes, before every publish/cleanup. External executables retain external
ownership forever; reject unsafe paths without deleting or repairing user data.

Archive extraction rejects absolute/traversal entries, symlinks/hardlinks/reparse
points, duplicate case-folded names, streams and excessive expansion. ZIP first;
at most 20,000 entries and 8 GiB aggregate expanded bytes, 1,000:1 maximum ratio.
Plan budget uses compressed size plus the bounded expanded-size allowance plus
512 MiB reserve; if expanded size is unknown, budget the full 8 GiB cap and explain
the conservative requirement. Stream limits enforce actual bytes, not just ZIP
headers. Dependencies publish inside the same immutable installation; collisions
are rejected unless their approved role mapping and identical digest allow them.
No shell scripts from archives execute. Verify each official SHA-256 BEFORE
extracting/executing, and fingerprint installed files after extraction.

Download via HTTPS from canonical GitHub API/official repository assets; validate
every redirect against explicit GitHub-owned download hosts, reject arbitrary URLs,
cross-repository assets, credentials in URLs and private-address redirects. Honor
normal trusted proxy configuration with TLS verification; no token forwarded to
asset redirects. Cache release metadata five minutes, support ETags and rate-limit
reset; cap metadata at 4 MiB/page and 100 releases per interactive traversal.
Connect timeout 10s, read-idle 30s, total operation 30 minutes; bounded retry only
for safe GETs (maximum two retries). Cancellation closes streams and cleans only
owned staging. No partial-range resume initially; a retry uses a NEW reviewed
request after a known terminal result and fully rehashes any retained complete asset.

Probe configured/previously validated binaries automatically only when trusted
identity still matches. PATH discovery is metadata-only until explicit validation;
do not execute every arbitrary PATH candidate during panel open. Probe selected
external binaries with a hidden owned child/job, absolute argv, timeout (10s per
version/help; 30s isolated empty-router smoke), bounded output (256 KiB), minimal
environment and feature/runtime checks. Process containment is not a security
sandbox; external executable validation is an explicit console action. Require
server/router flags and actual load/unload management compatibility before
automatic-install qualification; full model tool support remains a load-time test.

Publication journal phases record staged path identity, final path identity and
manifest hash. On restart: inspect journal first, adopt only a fully validated,
matching published directory, otherwise mark interrupted/cleanup_pending; block
new mutations if ownership cannot be proven. Never auto-redownload or activate.
At the publication boundary cancellation becomes unavailable. A racing cancel
gets terminal/no-op or cancel_not_available; accepted cancellation terminates only
after cleanup result is durable. Closing Launcher is never cancellation.

Activation under the same reservation revalidates candidate fingerprint and
requires off/no turns. Journal old/new root config, config revision and inventory
identity. Preserve unknown YAML fields. Atomically replace config executable path;
persist revision/receipt/inventory and mark committed. On crash reconcile hashes
to finish the known commit or restore the exact pre-image; do not overwrite a third
party edit. Ambiguous config change yields recovery_required. Keep the previous
validated installation for explicit rollback; no uninstall/GC of retained installed
versions in this first release. Temporary owned staging retention: seven days,
cleanup only when no active/recovery journal references it.

## 6. Implementation stages and proof

Each stage is a small explicit-path commit/checkpoint; record source SHA, command,
exit status, artifacts and next action. Work in a fresh task worktree. Do not let
the Launcher implementation branch's current unverified UI become backend evidence.

| Stage | Files / bounded work | Required proof before proceeding |
| --- | --- | --- |
| HS-0 contract/recovery foundation | Extend `manager.py`, `rpc.py`; extract focused `operations.py`/`setup_contract.py` as needed, no new core agent tools. Independent active/requested receipt, draft guards, shared reservation, transactional activation design and DTO fixtures. | Existing v1 decodes unchanged; delayed old receipt vs active new request; concurrent old-client start vs setup/lease; disk-failure acceptance rollback; config revision race. Freeze producer-emitted success/empty/error/null fixtures here, not hand-written UI mocks. |
| HS-1 detection/manual validation | `installation_inventory.py`, `binary_probe.py`, `host_probe.py`, setup RPC registration; reuse process job ownership. Typed paths first, no folder browser. | Real temporary executable/version/help success, timeout, malicious PATH candidate not executed, missing driver, denied scope, inconclusive discovery, selected remote host identity, manual activation under root config. |
| HS-2 official catalog/preflight | `release_catalog.py`, `installation_plan.py`; persisted cache/plan records. | Stable-pointer release, prerelease artifacts, multiple CUDA packages, stale/rate-limited/offline cache, missing digest, changed asset, disk budget and plan expiry. Check real upstream metadata read-only and save a sanitized receipt. |
| HS-3 installer/cancel/recovery | `installer.py`, `archive_install.py`, `installation_store.py`; shared journal, staged publication and offline cached bundles. | Real filesystem/process tests: traversal/case/ADS/link archive, redirect rejection, digest mismatch, disk-full simulation, cancel every phase, killed worker/restart at every journal boundary, receipt duplication, safe cleanup. Then a real official small/CPU binary install in an isolated chosen test directory. |
| HS-4 activation/integration | Same manager/config lock, setup.status/logs, additive scan metadata and structured errors. | Stop required; active-turn race; retained rollback; failed binary probe leaves old configuration; crash after YAML before receipt; other-console lookup; read/console/peer admission via actual TLS dispatcher; revoked console never sees paths. |
| HS-5 Launcher consumption and acceptance | Consume tested fixtures in its typed setup adapter/shared controller; implement remaining wizard surfaces. | Focused Dart tests/analyzer, fresh Windows build and Stage C: first run/existing/no install, chosen folder/version, download/cancel/retry, validation, activation, scan/select/load/tool turn/unload/stop, reconnect and second real host. |

Hermes tests through `scripts/run_tests.sh` ONLY, scoped to corresponding new
`tests/agent_runtime/test_local_llama_setup_*.py` files plus affected existing
manager/config/RPC/gateway/process suites. Run at implementation checkpoints, not
as evidence for this document. Use temporary root/profile and real imports. A fake
HTTP asset server supplies deterministic faults; official binary installation and
real TLS/host tests supply integration proof. Update greeting fixtures only from
producer output and preserve unrelated baseline failures.

Release gate: no unsupported platform pretending to install, no ambiguous writes
replayed, no server/weights auto-start, no other-root filesystem changes, and all
advertised capabilities proven. Second physical host and full five-control Stage C
remain outstanding from the earlier implementation; loopback is not a substitute.

## 7. Consumer unblock checklist

- HS-0–HS-4 produce a **tested** contract packet with emitted fixtures and method
  tier evidence. Until then Launcher may finish v1/manual UX but must not call new
  setup methods based on this proposal.
- Update the Launcher B-00–B-13 rows to point here; retain its visual authority.
- Use draft config revision in saves; independent current/historical operations;
  fake-clock freshness fence; selected/preview/loaded model separation.
- Gate new Install/Cancel/Activate by method admission plus capability plus grant;
  unknown states fail closed while Settings stays readable.
- Publish backend proof in the handoff; design prose and SVGs prove no executable
  compatibility, permissions, cancellation, crash recovery or visual acceptance.
