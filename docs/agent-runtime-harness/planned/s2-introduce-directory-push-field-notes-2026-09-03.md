# S2 / S2b / S2c — field notes (hermes side), 2026-09-03

Running record for `s2-introduce-directory-push.md` (beside this file). The
planner's survey is the first section; the builder appends below the rule,
one dated entry per commit, deviations named as deviations.

Worktree `X:/wt/s2-hermes`, branch `feat/s2-introduce-directory-push`, base
hermes `55fdc99148` (`origin/main`). Nothing outside the worktree was written;
the live store (`HERMES_HOME=X:\Eternia\.hermes`) was read with three
read-only verbs only. No push.

## Planner's survey (Fable agent, 2026-09-03)

### What was read, and the shas

* Parent plan `EterniaLauncher/docs/mission_control/planned/same-account-instant-pairing.md`
  at launcher `791cc8030` — all 17 rulings ADOPTED (R-IP15 AMENDED), §2 flows
  + parity table, §3 S2/S2b/S2c with hardening bullets, §5 ledger (S0a
  `e94e022fb6`, S0b `b4a383a1e8`, blocker `368387ae0b` LANDED), §6 audit
  table, §7 order.
* S0a plan + field notes (`planned/s0a-atlas-cleanup*.md`): `harness_core`
  has FIFTEEN members incl. `browser-cdp`; ratchet 43 / 0 / 0 / 1149 per
  persona; the inventory emitter gates three artifacts; the stream goldens
  moved in S0a and the launcher mirror was closed at launcher `791cc8030`.
* S0b hermes notes (`planned/s0b-add-install-hermes-notes-2026-09-03.md`)
  and the launcher-side S0b plan: the trust/cache frozensets and their
  partition test; the launcher reads the `gateway` block by key; Unpair
  revokes the LOCAL peer first and defers the far row to S2c.
* Backend S1 packet §4 at backend `bb00ffd4`: grant id = correlation id;
  `introduce --for-install <requester install> --for-device <from_device_id>
  --correlation <grant_id>`; the recommended `fulfil` payload keys.

### Live measurements (read-only)

```
HERMES_HOME=X:/Eternia/.hermes C:/Python312/python.exe -m hermes_cli.main harness gateway id --json
  → install bbdb8120-575d-4890-85e7-ecdbd650cde0 "DESKTOP-QJ7DDV2", state loaded,
    store root X:\Eternia\.hermes\agent-runtime; row keys {install_id, display_name, state, created_at, path}
… harness gateway peers list --json    → items: []
… harness gateway devices list --json  → items: []
```

Test runner sanity from this worktree (no `.venv` here; the runner found the
shared canonical venv `~/.venvs/hermes-test` on its own):

```
bash scripts/run_tests.sh tests/agent_runtime/test_gateway_targets.py tests/agent_runtime/test_peer_authorization.py
=== Summary: 2 files, 42 tests passed, 0 failed (100% complete) in 5.3s (8 workers) ===
```

### Findings the plan is built on (each cited in the plan's §0)

1. **The `gateway` block's key set is pinned exactly** at
   `tests/agent_runtime/test_serve_gateway_lane.py:338-344` (and `:239` for
   `disabled`). `capabilities` moves both pins — named in the plan, not
   discovered at build time.
2. **`peered` carries no expiry**, so B's `record_peer` has nothing to store;
   R-IP15's 30-day rule needs `hello_ok.peered.expires_at` (additive) or the
   two ends of one edge expire at different times.
3. **`peers join` is trust-on-first-use** (`gateway_commands.py:603-609` pins
   the payload's own fingerprint). `--expect-fingerprint` is the attested
   path; the manual S0 path keeps TOFU and says so in the ack.
4. **`introduce` has no refusal precedent for "listener off"** — both mint
   verbs print a note instead. Recommended: `runtime_unavailable` with
   `peers pair`'s sentence, because `introduce`'s consumer is a machine.
5. **A code is a bearer.** The peer half can be scoped (the join hello names
   the redeemer's install id); the device half cannot (the device id is minted
   at redeem). The plan labels the device row with `account_device_id` and
   says plainly it is a label.
6. **Cross-process event emission already works** — `realm_sync._append_realm_sync_event`
   appends from the CLI process and the serve's stream advances. So R-IP12's
   "mtime check" has one job left (hand edits / pre-S2c writers), and the
   plan sizes it to that.
7. **`decision_contract_hash` rides the stream goldens** (`delta.json`,
   `hydrate.json`: `b6985ac4…`). Registering five events regenerates every
   golden that carries it → the launcher's `test/fixtures/harness_stream/`
   byte mirror is OWED at landing. `SURVIVING_EVENT_COUNT = 59` → 64.
8. **A transcript read inside the serve process has no precedent.**
   `persona_chat_session_messages` resolves scope through
   `resolve_chat_session_scope`; the ambient rung is env-gated
   (`HERMES_ALLOW_AMBIENT_CHAT_READS`). `peer.thread.read` must fail closed
   with its own word and be proven on a real serve child by the e2e; the
   argv-lane fallback is spelled in the plan's §5.
9. **The e2e roots hold no persona** (the chat e2e's proof IS `unknown
   persona dev`). S2b's parity e2e needs a seeded instance on B — the one
   detail the plan leaves the builder to confirm against
   `agent_runtime/config.py`.
10. **The peer surface pins live in four places**: `test_peer_authorization.py:139-160`
    and `:371`, `test_peer_chat_execute.py:79`, and a stale "two verbs wide"
    comment in the chat e2e. All named.
11. **The HUD field contract** (`test_runtime_hud_field_contract.py:77`)
    asserts every emitted key is declared — `installs` needs a `HudField`
    row. The goldens' `situational_hud` blocks have no peers, so an
    `installs` block that drops when empty leaves those bytes alone.
12. **The new tool moves the S0a ratchet** (43 → 44, token estimate
    re-measured) and all three inventory artifacts; the Atlas artifact
    regenerates launcher-side (OWED).
13. **`_self_endpoints` returns at most one row** and `[]` for a wildcard
    bind; `gateway id`'s `endpoints` needs interface enumeration
    (stdlib-only recipe in R-S2-2), and reusing it for the join payload
    makes hellos carry up to four endpoints for free.

### Corrections made while writing

* Test-file names cited for the CLI verb harness were corrected to the real
  files (`tests/hermes_cli/test_gateway_pairing_verbs.py`,
  `test_gateway_peer_verbs.py`); there is no `test_decision_contract_registry.py`
  — the S32 parity test is the registry's gate.

### Sub-rulings recorded for the orchestrator (all have recommendations in §1)

R-S2-1 capabilities vocabulary/placement · R-S2-2 `gateway id` endpoints ·
R-S2-3 `introduce` flags + envelope (`grant_payload` = the backend shape) ·
R-S2-4 requester scoping (peer half checked, device half labelled) · R-S2-5
`expires_at` on both rows, TRUST-classified, verifiers refuse after the proof,
wire stays collapsed · R-S2-6 `join --expect-fingerprint` · R-S2-7
`peers_cache.json` shape + writers; `last_seen` leaves the trust file ·
R-S2-8 mtime check = revision memo, external writes only · R-S2-9
`peer.announce` schema, caller-only, one-way `revoked_you` · R-S2-10 roster
projection + scope · R-S2-11 `peer.thread.read` needs `target`, same lane
guard, five refusal codes · R-S2-12 `@install/` on open/threads,
`call_peer_method`, timeouts · R-S2-13 HUD lines + residency note · R-S2-14
correlation stamping (pending entries + events, never a row) · R-S2-15
`revoked_you` first · R-S2-16 `usable_peers` · R-S2-17 ratchet/manual cost ·
R-S2-18 the "introduce unreachable" test.

---

## Builder's record (append below; one entry per commit)

_(empty — the builder fills this)_
