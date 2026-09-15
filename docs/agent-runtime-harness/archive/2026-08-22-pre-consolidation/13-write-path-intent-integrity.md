# Stage 13 — Write-path intent integrity (scope mutations)

**Diagnosed live 2026-07-09 (~4pm ET), hours after Stage 12 merged.** Mission
Control "flipped scope randomly": `realm.activated` ping-ponged between the
user's realm and a stale one every 30s–4min. Stage 12 was NOT regressed — the
read path rendered every write faithfully; the disease had moved to the write
path.

## Root cause (all confirmed in code + live timeline)

The launcher's serve-first runner (`runMissionControlCommandPreferServe`,
40s timeout) treats every argv as safely re-runnable, but only chat actually
was (`client_message_id` dedup):

1. **No cancel**: a timed-out request was abandoned CALLER-side only — the
   serve child still executed it whenever a worker freed (observed live:
   an abandoned `realm use` draining 3 minutes later, 19:55:54Z).
2. **Blind re-run**: the caller re-ran the SAME argv via one-shot CLI
   (6.6s warm, measured) — one click, two executions (observed live:
   click + exactly 40s + CLI spawn = the 19:53:23Z flip).
3. **No ordering**: serve dispatches to `ThreadPoolExecutor(max_workers=4)`;
   `RealmStore/WorkspaceStore.set_active` was a bare last-writer-wins
   pointer write, so late landers clobbered newer selections.

## Fix — make replays safe instead of forbidding them

- **A. Supersede guard at the store chokepoint**
  (`agent_runtime/store.py::_resolve_activation_write`): active-pointer
  writes carry an intent basis (`issued_at`, persisted as
  `intent_issued_at`). A pointer owned by a strictly newer intent rejects a
  late lander (`superseded`, no write, NO event — the launcher never sees a
  flicker); the exact same intent applies once (`duplicate`, one event);
  no-basis callers (humans, legacy) are stamped `now()` and always win.
  Unparseable bases fail open. Guard lives in the STORE so every transport
  is covered: serve lane, CLI fallback, orphan drains, and the future
  mobile-gateway second writer.
- **B. CLI verbs carry the basis**: `harness realm use` / `workspace use`
  accept `--issued-at`; `realm use`'s workspace reconcile rides the SAME
  basis so a late realm switch can't drag the workspace pointer past a newer
  explicit selection. Envelopes gain `applied` / `superseded` / `reason`
  (exit code stays 0 — both outcomes are protocol, not errors).
- **C. Serve cancel op** (`harness_parts/serve.py`):
  `{"op":"cancel","id":…}` drops a still-QUEUED request
  (`exit code=130, cancelled=true`, argv never dispatches); a RUNNING
  request answers `cancel_denied` — its side effect may land, which is safe
  because of A.
- **D. Launcher**: `mission_control_bridge.dart` stamps `--issued-at` ONCE
  at argv build (≈ the click) so every replay presents the original instant;
  `abandonRequest` best-effort writes the cancel op before the turn-aware
  recycle; a `superseded` envelope maps to rejected (optimistic scope
  reverts), `duplicate` maps to success.

## Validation

- Hermes: `tests/agent_runtime/test_stage13_write_path_integrity.py`
  (guard semantics + the live flip scenario as a CLI integration test) +
  cancel tests in `test_harness_serve.py`. Full `tests/agent_runtime` gate:
  **1636 passed** (2026-07-09).
- Launcher: bridge argv/mapping tests + serve-session cancel test;
  mission_control suite green (2026-07-09).

## Recorded debt / non-goals

- `workspace.create` (and other non-pointer mutations) still lack a
  request-id dedup — a timeout replay could create a duplicate workspace.
  Same treatment (client request id, store-side dedup table) when it bites.
- No mutation serialization lane in the serve pool: correctness now comes
  from the store guard; a single-lane executor remains an efficiency option.
- Wall-clock basis: same-machine clock steps (NTP) can misorder one click —
  worst case a rejected switch the user repeats. Revisit only if it bites.
