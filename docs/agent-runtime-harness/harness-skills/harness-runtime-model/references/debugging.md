# Debugging: Snapshot Parity, Mission Control UI, Payload Gaps

## Snapshot Parity / Observability (debug UI ⟷ harness divergence)

The snapshot is a lossy projection — built fresh per call over the whole world
(`build_snapshot()`), not read out of a projected database. When the UI doesn't match
harness truth, **start with the parity envelope — do not code-spelunk**:
`hermes harness snapshot --json`, read the top-level `.parity` object (`contract_version`,
`build_ms`, `runtime_root`, `profile`, watermark, per-projection
`completeness {considered, included, dropped, reasons{}, truncated}`, bounded `drops[]`,
`warnings[]` self-checks). It answers "what did the harness drop, and why."

Two version numbers, measured live **2026-08-28** — read them, do not quote them:

- `.parity.contract_version` = **54** (`SNAPSHOT_CONTRACT_VERSION`,
  `agent_runtime/snapshot.py:95`). It is a cross-repo lockstep with the Launcher's
  `kSupportedMissionContractVersion`, stated in exactly one test
  (`test_snapshot_contract_version_authority.py`) with an AST gate failing any other test
  that states it — so it moves only by deliberate review, and it moves.
- top-level `.schema_version` = **2** — the version of the **snapshot envelope** itself.
  It is *not* a "read-model version": there is no read model. `harness rebuild-read-model`
  / `harness read` were unregistered 2026-08-22 and `agent_runtime/read_model.py` +
  `projector.py` are deleted; the serve process caches built cores under
  `<store_root>/serve_read_model/` instead. If a note, a consumer, or your own memory
  talks about rebuilding or reading a projection, it is describing a retired lane.

Primitives: `agent_runtime/parity.py` + `agent_runtime/snapshot.py`.

**Sections that no longer exist:** `goals`, `stage_verification`, `runs`, `proofs`,
`incidents`, `mission_plan`, `agent_topology`, and `agent_instances.task_id` were removed
on 2026-07-30. If a consumer is looking for one, that is a stale consumer, not a dropped
projection — do not "restore" it.

Divergence classes that have actually bitten (history + closeouts in
`Launcher_Brain/20 — Active Initiatives/mission-control-parity-audit.md`):

- **Identity mismatch** — canonicalize persona identity with `_canonical_persona_id`
  (preserves `profile:alice`), NEVER `safe_assignment_token` (mangles →
  `profile_alice`). Mismatched keys silently orphan trace/history rows.
- **Missing chat tool calls** — confirm `ChatProgressSink` emitted `run.tool.*` for the
  session (grep the event log) AND `persona_chat_trace` projects a row for the instance.
- **Slow** — `build_ms` is the truth. If it regresses, profile `build_snapshot` by
  cumulative time; the hog has historically been repeated file parses (fixed 54s → ~10s
  via `parse_cache.py` + `CachedEventLog`), not the work itself.
- **Stale UI** — the bridge hydrates from `harness stream` (hydrate frame then deltas,
  folded into `MissionReadModel`) and falls back to one-shot `harness snapshot --json`.
  `stream` has no frame-count flag; `--resync` forces the first post-hydrate batch to a
  full core for a reconnecting client. Since the serve bridge shipped (2026-07-08) polls
  run through one warm `hermes harness serve --ndjson` child with a 20s read-model cache —
  a repeat poll is ~0.19s and byte-identical, stamped `served_from_cache` +
  `cache_age_ms`. Post-turn lag until the forced refresh (`forceFresh`) lands is the
  remaining class. Harness edits are live on the next snapshot (editable venv
  `X:\Eternia\.hermes\venvs\hermes-agent` serves the CHECKOUT — verify the branch);
  launcher Dart edits need a rebuild.
- **Displayed chat switched with no operator action** — bug by contract: every implicit
  console-selection transition flows through pure `MissionAgentSelectionPolicy` with
  typed, debug-logged reasons (`[MissionSelection] <reason>`). A silent switch means a
  write path bypassed the chokepoint; fix the chokepoint, don't patch the symptom.
- **Duplicate agent cards / rows (or a card that vanished)** — retired class
  (2026-07-10, launcher `11a151a6f` + hermes `77410af53`). NEVER add a dedup
  heuristic. Read two diagnostics first: (1) snapshot parity warning
  `duplicate_persona_instance` — 2+ live persona-instance rows alias to one
  canonical id (store drift) → repair with
  `hermes harness persona-instance reconcile [--dry-run] [--json]` (archives
  legacy rows, records `persona_instance_aliases.json`, idempotent);
  (2) launcher `[MissionRoster]` debug log — every removed roster row is a
  typed `MissionRosterDrop` with reason + kept id. Identity authorities:
  hermes `canonical_persona_instance_id()` (persona_assignments.py) and
  launcher `MissionAgentRosterPolicy` / `canonicalMissionInstanceId`
  (data/mission_agent_roster_policy.dart, mission_agent_identity.dart) —
  the ONLY places instance identity/dedup may be derived.
- **Agent graph looks wrong** — the graph is a stored document, not inferred state. Read
  `hermes harness flow list --json` / `flow show <graph_id> --json` and the instances'
  `steered_by` fields. `flow set` reconciles steering for the instances a document
  references and never creates instances; if a node is missing, the instance is missing.

## Mission Control/UI Debugging checklist

Start from the parity envelope above, then verify the bridge and UI with both code and
runtime proof:

- The CLI bridge maps the requested UI action to the correct Harness CLI command — and to
  a verb that still exists. A UI control wired to a removed verb fails silently-ish; check
  the parser (`harness <verb> --help`) before believing a "backend bug".
- UI feedback is truthful for success, failure, and Harness-side refusal (typed refusals
  like `unknown_chat_session`, `relay_cycle`, `budget_exhausted` must surface as
  themselves, not as a generic error).
- State refreshes after mutations.
- Archived instances, chats, and board cards remain visible in archive/history —
  archive-never-delete is the contract, and the operator-facing `persona instance delete`
  verb (alias of `retire`, 2026-08-27) does not change it.
- **An agent that keeps coming back after a delete is a client bug, not a store bug.**
  The serve refuses an `office actor-upsert` against a deleted key with JSON-RPC `4090`
  / `data.reason = "actor_archived"` (`agent_runtime/serve_rpc.py` ~1401), which is
  terminal: the client must DROP its local row, and re-placing is a new create with a new
  id. A client that treats it as retryable re-pushes forever — the live incident was a
  launcher re-pushing archived actors nineteen seconds after boot. Conversely a desk still
  on the canvas after a delete is cured by **repeating the delete**: the replay sweeps live
  actors bound to a retired instance (`agent_runtime/agent_retire.py`,
  `_sweep_live_placements`) and still answers `already_retired: true`.
- Chat transcript and trace render for every chat-mode instance in the roster, whatever
  its role. Roles are data; a role name is never a rendering condition.
- Thinking summaries shown in UI must be redaction-safe summaries, not hidden provider
  chain-of-thought.
- Operator chat renders the conversational reply (not raw decision JSON); the operator's
  own message survives a snapshot refresh.
- The Agent Console `Inspect ▾` menu is Context Inspector · Turn tool context ·
  Permissions · Skills Context. `Run detail` and `Assign Work` were removed with the
  mission lane — their absence is correct, not a regression.

## Payload and field gaps

Treat repeated malformed payloads, dropped fields, or missing cross-turn visibility as
Harness protocol bugs, not just agent mistakes. Chat turn payloads are first-class
communication: an agent must cite the concrete receipt (tool trace row, artifact path,
`mcp_calls_spent`, `client_message_id`/`turn_id`) rather than describing it.

When a reply claims something the transcript cannot show, or a persona invented a field:
inspect the preserved event and context records before blaming the persona; check whether
the field exists in the decision contract, the context projection, and the Mission Control
renderer; prefer a first-class field / visibility rule / repair-feedback fix over a
prompt-only wording change; preserve raw + normalized evidence; add tests
(unsupported-field normalization, stale ordering, cross-turn carry); and update the
affected shared skills (`harness-runtime-model`, `harness-dev-delivery`,
`harness-qa-verdict`) in the same change that alters the contract.
A skill describing a retired contract makes agents behave wrong even when the code is right.
