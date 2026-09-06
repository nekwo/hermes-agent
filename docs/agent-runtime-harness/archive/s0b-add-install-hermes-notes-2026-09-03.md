# S0b Add-install data model — hermes-side notes (2026-09-03)

Planning survey, read-only, for stage S0b of
`EterniaLauncher/docs/mission_control/planned/same-account-instant-pairing.md`
(rulings R-IP14 consumed; R-IP12 read). The build plan lives launcher-side at
`EterniaLauncher/docs/mission_control/planned/s0b-add-install-data-model.md`
(§1 sub-ruling S0b-R9 is the hermes scope; §2 B1 and B5 are the hermes rows).
Baseline hermes `504953f6ad` (`origin/main`), worktree `wt/s0b-hermes`, branch
`feat/s0b-add-install-hermes`. No code touched.

## What the hermes stores hold, per fact (read at the sha above)

| store | trust fields (written by a ceremony or `revoke`, never by the network) | cache fields (what the network or the operator TOLD us) |
|---|---|---|
| `gateway/peers.json` — `_row()` at `agent_runtime/gateway_peers.py:866-893`, the one place the shape is written | `peer_install_id`, `secret_verifier`, `approved_at`, `revoked`, `revoked_at` | `display_name` (name-at-pairing; from the join hello's `peer_display_name` on A, `serve.py:703-705`, or the far `install.display_name` on B, `gateway_commands.py:672-678`), `endpoints` (max 4, `:203`), `cert_fingerprint`, `last_seen` (stamped by `note_peer_seen` on every verified peer hello, `serve.py:728`) |
| `gateway/devices.json` — `serve_gateway_auth.py:666-675` | `device_id`, `tier`, `verifier`, `created_at`, `revoked`, `revoked_at` | `name`, `last_seen` (`note_device_seen`, `serve.py:742`) |
| `gateway/install.json` — `gateway_identity.py:220-224` | `install_id` (mint-iff-absent, never rewritten), `created_at` | `display_name` — what the install calls ITSELF; `set_display_name` (`:257-293`, `harness gateway rename`) rewrites the file and emits nothing; the frames echo the BOOT-time identity until restart (`serve.py:3900-3906`) |

Wire: `install.display_name` already rides `ready` / `hello_ok` / `version`
(`gateway_identity.py:140-161` `frame_payload`; `serve.py:3538`), and the
`gateway` block (`{outcome, host, port, started_at, cert_fingerprint}`,
`serve.py:593-604`) rides the same three frames (`:2751`, `:3549`, `:3915`).
**No wire change is needed for S0b**: the launcher half adds the reader it
never had for the `gateway` block and treats `install.display_name` as the
offline name (R-IP14: the account's device name is the one name).

## The S0b hermes rows (from the launcher plan, §2 B1 and S0b-R9)

1. `gateway_peers.py`: module docstring row shape (`:8-12`) gains the
   trust/cache split in prose; `PeerRecord` field docs (`:261-292`) say which
   side each field is on; add `PEER_ROW_TRUST_FIELDS` and
   `PEER_ROW_CACHE_FIELDS` frozensets beside `_row()` (`:866`), exported in
   `__all__` (`:141-169`), so S2c's `peers_cache.json` has a machine-readable
   split and a new key must be classified.
2. `tests/agent_runtime/test_gateway_peers_store.py`: add
   `test_the_row_shape_is_exactly_trust_fields_plus_cache_fields` (the union
   equals `_row()`'s keys, the intersection is empty; both `redeem_peer_code`
   and `record_peer` write that key set). The file already takes `tmp_path` as
   the store root everywhere (`:20-23`), which is the module's own rule.
3. `serve_gateway_auth.py` docstring (`:13-15`): one sentence naming `name` and
   `last_seen` as cache facts. `gateway_identity.py` docstring (`:4-19`): one
   sentence that `display_name` is what the install calls itself, published as
   a cache fact, and that a launcher shows the account's device name over it.
4. Nothing else. `note_peer_seen` / `note_device_seen` keep writing into the
   trust files until S2c moves them (R-IP12a); `peer.announce`, the sidecar and
   the `gateway.peer.*` events are S2c's and are not started here.

Verify (from the worktree root, never bare pytest; the runner probes `.venv`
then `HERMES_PYTHON`, which is a Python 3.12 that is NOT the live serve venv):

```
HERMES_PYTHON=<python3.12> scripts/run_tests.sh tests/agent_runtime/test_gateway_peers_store.py tests/agent_runtime/test_gateway_identity.py tests/agent_runtime/test_serve_gateway_auth.py
```

## The spelling question, answered from this repo

`runtime.chat.message` / `runtime.chat.steer` are registered at
`agent_runtime/serve_rpc.py:2492` and `:2572` (TIER_CONSOLE) — the names the
launcher's `mission_chat_turn_rpc.dart` spells, and the right names for a
DEVICE-credential caller. The peer lane's chat verb is `peer.agent_chat.execute`
(`serve_rpc.py:3114`; `chat_turn.py:81`), which refuses a connection that proved
no peer with `peer_identity_required` (`:3151-3167`). A peer may call exactly
`PEER_METHOD_ALLOWLIST = {peer.ping, peer.agent_chat.execute, peer.media.get}`
(`call_authorization.py:200`). There is no `peer.chat`. Both spellings are
correct for their caller kind; the launcher is never a peer.

## Two facts the parent plan's B4 assumed and this repo refutes today

- "revokes the far `devices.json` via the existing revoke": `harness gateway
  devices revoke` (`gateway_commands.py:286-321`) and `peers revoke` (`:723-758`)
  are CLI verbs with no method-lane or op-lane twin (`gateway_peers.py:35-42`
  states this as R5; grep `revoke` in `serve_rpc.py` / `serve_socket.py`: none).
  A launcher can revoke only on the install whose argv lane it holds — its own
  local serve. The far side is S2c's `peer.announce revoked_you` (peer half)
  and R-IP15 expiry / S3's backend-notified far launcher (device half).
- `revoke_peer` is one-sided by design (`gateway_peers.py:552-561`); a revoke
  that reached across the wire would be one install writing another's trust
  store, which is exactly what R5 forbids. S0b's Unpair therefore revokes the
  LOCAL peer row first (so the far install's next dial is refused
  deterministically before any turn) and defers the far row.

---

## Builder's record — the hermes half (2026-09-03)

Built on `feat/s0b-add-install-hermes` in worktree `wt/s0b-hermes`, one commit
(`556995303a`, post-rebase). Scope is exactly S0b-R9: prose plus a machine-readable split
plus tests. **No wire change, no behaviour change, no store-shape change** —
`_row()` writes the same nine keys it wrote before.

What landed:

* `agent_runtime/gateway_peers.py` — the module docstring gains a
  *Two kinds of field in one row: TRUST, and CACHE* section naming both sets
  and the honest residue (`note_peer_seen` writes a cache fact into a trust
  file on every verified hello, and stays there until S2c moves it under
  R-IP12a). `PeerRecord`'s docstring says which side each of its fields is on.
  `PEER_ROW_TRUST_FIELDS` = `{peer_install_id, secret_verifier, approved_at,
  revoked, revoked_at}` and `PEER_ROW_CACHE_FIELDS` = `{display_name,
  endpoints, cert_fingerprint, last_seen}`, declared immediately above `_row`
  and exported in `__all__`.
* `agent_runtime/serve_gateway_auth.py` — one sentence: `name` and `last_seen`
  are the device row's cache half; `device_id`, `tier`, `verifier`,
  `created_at` and the two revocation fields are trust.
* `agent_runtime/gateway_identity.py` — one paragraph: `display_name` is what
  the install calls ITSELF, published as a cache fact, and the one name a
  launcher shows is the ACCOUNT's device name joined by `install_id`, with this
  as the labelled offline fallback. The boot-time-echo caveat this survey found
  is stated beside it rather than left in a plan.
* `tests/agent_runtime/test_gateway_peers_store.py` — two tests.
  `test_the_row_shape_is_exactly_trust_fields_plus_cache_fields` asserts the
  union equals the stored row's keys and the intersection is empty, so a new
  field fails until classified. `test_record_and_redeem_write_the_same_key_set`
  asserts both write paths through `_row` land the same key set, which is the
  claim `_row`'s own docstring makes about the two halves of one edge.

Verified (from this worktree root, never bare pytest, `HERMES_PYTHON` pointed
at the canonical test venv — a Python 3.12 that is NOT the live serve venv):

* `scripts/run_tests.sh tests/agent_runtime/test_gateway_peers_store.py tests/agent_runtime/test_gateway_identity.py tests/agent_runtime/test_serve_gateway_auth.py`
  — **3 files, 87 tests passed, 0 failed**.
* `scripts/run_tests.sh tests/agent_runtime/test_serve_gateway_lane.py tests/agent_runtime/test_serve_gateway_peer_lane.py`
  — **2 files, 49 tests passed, 0 failed**.

Deviations: none. The one judgement call is the `__all__` placement — the two
names are inserted in the list's existing alphabetical order rather than
appended.

What S2c inherits: the frozensets make `peers_cache.json` a MOVE rather than a
re-derivation — the cache set is already named, and the test fails the moment a
tenth key appears without a side. The launcher half's build record (the
Unpair far-side deferral, the connector-factory answer) is in
`EterniaLauncher/docs/mission_control/planned/s0b-add-install-data-model-field-notes-2026-09-03.md`.
