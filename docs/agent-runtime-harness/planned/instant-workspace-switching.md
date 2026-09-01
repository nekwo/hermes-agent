# Planned — Instant workspace switching (hermes half)

**Primary plan:** EterniaLauncher repo,
`docs/mission_control/planned/instant-workspace-switching.md` — architecture,
measured cost model, stages WS0–WS5, rulings R-W0..R-W3 (all RULED
2026-09-01). This pointer carries only the hermes-owned work so the harness
canon names its own lanes, per the gateway program's two-repo convention.

**Why hermes owes anything at all:** `workspace.activated` /
`realm.activated` are not in
`patch_coverage.LIVE_COVERED_DOMAIN_EVENT_TYPES`, so a two-scalar pointer
flip demotes the whole stream batch to a full O(world) core (~842 KB–2.2 MB,
`build_ms` median 3,083) — measured launcher-side at p50 **8.76 s** before the
`active` flags flip. The pointer is not load-bearing engine state (chat turns
resolve workspace from the sender's instance first), so nothing but the
stream's coverage table stands between a switch and a few-hundred-byte patch.

**hermes-owned stages:**

- **WS1 — the `scope` fold entity.** Coverage for the two activate events; a
  `state.patched` producer with payload
  `{active_workspace_id, active_realm_id}` (both pointers always); the
  derivability pin — exactly seven `active_id()` readers in `snapshot.py`
  (`:796,:800,:887,:927,:932,:964,:965`), all pure functions of the pointers,
  enumerated by a test so an eighth reader demotes honestly instead of lying.
  Version skew rides the existing fold-entities negotiation + per-subscriber
  promotion: an undeclaring subscriber gets today's demoted core. Cross-stack
  stream-golden ceremony in the SAME landing.
- **WS4 (hermes half) — `runtime.workspace.use` / `runtime.realm.use`.** Two
  RPC methods, `local_console` equality at the authorization chokepoint —
  device and peer callers refused with the typed tier reason, NOT on
  `PEER_METHOD_ALLOWLIST` (the registry-iterating exclusion test covers them
  unedited). Same accept semantics as the argv verb, one shared
  implementation. The manifest integer moves: serve-frame regenerate +
  `generate.py --check`, and the launcher's CLI-contract/manifest pins move in
  the launcher half of the same lane.
- **WS5 — GATED (R-W3).** Remove `active_workspace_path()` from
  `stream._scope_fingerprint` only, after WS1 plus one quiet field week; the
  serve poll-cache entry stays (it serves `harness snapshot` callers that have
  no event lane). Not dispatched with this wave.

**Canon graduation on landing:** WS1 → `02-runtime-data-and-shapes.md` /
`03-transport-and-wire.md`; the 8.76 s republish-cadence figure enters
`08-performance-and-debt-ledger.md` (it is documented launcher-side only — a
canon gap this lane closes in passing).
