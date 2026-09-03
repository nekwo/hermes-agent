# Planned — S2 / S2b / S2c: `introduce`, the read-only directory, and the peer push lane

**Status: PLANNED — no code touched. Build plan for an Opus builder; written
2026-09-03 against hermes `55fdc99148` (`origin/main`; worktree
`X:/wt/s2-hermes`, branch `feat/s2-introduce-directory-push`), the launcher's
`docs/mission_control/planned/same-account-instant-pairing.md` at launcher
`791cc8030` (§1 rulings ALL ADOPTED, R-IP15 as AMENDED; §2 flows + parity
table; §3 S2/S2b/S2c with their "Hardening (audit 2026-09-03)" bullets; §5
ledger S0a `e94e022fb6` / S0b `b4a383a1e8` / blocker `368387ae0b` LANDED; §7
order), and the backend S1 contract packet
`eternia-backend.pair-grants/docs/architecture/planned/devices-s1-pair-grants.md`
§4 at backend `bb00ffd4`.**

Consumes **R-IP2** (the backend carries the introduction, never the traffic),
**R-IP5** (the target mints on demand when told; grants expire in 120 s),
**R-IP9** (two read-only peer methods; nothing else joins the allowlist),
**R-IP10** (every local messaging feature crosses but `wait=true`; clarify
continuity crosses as `session_id`), **R-IP11** (`@install/` chooses the
RUNTIME, never the identity; residency is VISIBLE in the HUD), **R-IP12** (one
local store, pushed never polled, filtered never copied: two files, three push
edges, one predicate), **R-IP15 as amended** (credentials expire at 30 d; any
live signed-in device may pair; the Trusted mark is not consulted), **R-IP16**
(feature-detect via `hello_ok.gateway.capabilities`; a missing capability is a
row state, never a refusal), **R-IP17** (the grant id is the correlation id and
every party writes it; reason codes are one enumerated set).

Field notes for the execution:
`s2-introduce-directory-push-field-notes-2026-09-03.md` beside this file. The
builder appends to it; nothing in this plan is closed by the plan alone.

The one-paragraph version. Today an operator carries eight characters between
two machines to pair them; an agent on install A can send to `@B/neko` but
cannot list installs, cannot see who is on B, cannot read the far thread it was
handed, and learns a revoke only as the next refusal. S2 composes the two
existing mint verbs into ONE `introduce` envelope a launcher can post as a
backend grant — with expiry, the grant id stamped through, an attested
fingerprint on `join`, and a `capabilities` list on the greeting so an old
launcher sees a row state rather than a crash. S2b adds the two read-only
peer methods, the `agent_chat_installs` tool, `@install/` on the two read
tools, and the HUD's install lines with the residency note. S2c splits what
the network TOLD us into `peers_cache.json`, emits `gateway.peer.*`
decision-contract events from every store door, adds the cache-only
`peer.announce`, and makes `usable_peers` the one predicate the resolver, the
tool and the HUD all read. Every change is additive on the wire.

---

## 0. Ground truth (surveyed read-only at `55fdc99148`; live reads under `HERMES_HOME=X:\Eternia\.hermes`)

### 0.1 This box, today

`harness gateway id --json` → install `bbdb8120-575d-4890-85e7-ecdbd650cde0`,
display name `DESKTOP-QJ7DDV2`, state `loaded`, store root
`X:\Eternia\.hermes\agent-runtime`; the row is exactly
`{install_id, display_name, state, created_at, path}`
(`hermes_cli/harness.py:2012-2027` `_gateway_install_row`) — **no
`endpoints`, no `capabilities`**. `harness gateway peers list --json` → zero
rows. `harness gateway devices list --json` → zero rows. The manual S0
ceremony has never been run here, so S2's e2e is the two-roots harness
(`tests/agent_runtime/test_gateway_peer_two_roots_e2e.py`), not this store.

### 0.2 The stores and the one place each row shape is written

| store | writer(s) | row today | note |
|---|---|---|---|
| `gateway/peers.json` | `redeem_peer_code` (A, `gateway_peers.py:676-756`), `record_peer` (B, `:759-811`), both through `_row` (`:928-963`); `revoke_peer` (`:594-626`); `note_peer_seen` (`:573-591`) | `{peer_install_id, display_name, endpoints, cert_fingerprint, secret_verifier, approved_at, last_seen, revoked, revoked_at}` | S0b: `PEER_ROW_TRUST_FIELDS` (`:910-918`) = `{peer_install_id, secret_verifier, approved_at, revoked, revoked_at}`; `PEER_ROW_CACHE_FIELDS` (`:923-925`) = `{display_name, endpoints, cert_fingerprint, last_seen}`; `tests/agent_runtime/test_gateway_peers_store.py:124` asserts they partition `_row()`'s keys exactly and `:154` that both write paths land one key set. **A new key fails until classified.** The module docstring (`:36-42`) names `last_seen` as the residue S2c moves. |
| `gateway/devices.json` | `redeem_pairing_code` (`serve_gateway_auth.py:612-689`, row literal `:671-680`); `revoke_device` (`:526-551`); `note_device_seen` (`:500-523`) | `{device_id, name, tier, verifier, created_at, last_seen, revoked, revoked_at}` | `_decode_device` (`:707-727`) reads an unknown tier as `read`. No expiry anywhere. |
| `gateway/pairing.json` | `mint_into` / `match_pending` (`gateway_pairing_codes.py:186-242`) | pending entry `{kind, salt, hash, created_at, expires_at, **extra}`; `extra` merged UNDER the fixed keys | `CODE_TTL_SECONDS = 600` (`:106`), `MAX_PENDING_CODES = 3` (`:110`) across both kinds; the device mint puts `{tier, name}` in `extra` (`serve_gateway_auth.py:594-598`), the peer mint puts `{note}` (`gateway_peers.py:661-663`). **`extra` is where `introduce` scopes a code to a requester without a schema change.** |
| `gateway/install.json` | `ensure_install_identity` / `set_display_name` (`gateway_identity.py:219-293`) | `{install_id, display_name, created_at}`; `frame_payload` (`:150-171`) = `{install_id, display_name, state}` on `ready`/`hello_ok`/`version` | rename emits nothing; frames echo the boot-time name until restart. |

Cross-process discipline every writer obeys: `store_root` is an INPUT
(`gateway_peers.py:126-135`), one lock file `devices.lock` covers the directory
(`serve_gateway_auth.py:766-779`), reads never raise, writes go through
`store_file_io.write_secure_json` (`store_file_io.py:149`).

### 0.3 The verbs and how they are wired

`hermes_cli/harness.py:333-461` declares the `gateway` subtree: `id`, `rename`,
`pair`, `devices list|revoke`, `peers pair|join|list|revoke`; each
`set_defaults(func=_cmd_gateway_*)`, thin wrappers at `:2072`, `:2163-2190`
that import `hermes_cli/harness_parts/gateway_commands.py`. Every verb takes
`_add_stage42_global_args` (`:189`) and prints through `_object_envelope` /
`_list_envelope` + `attach_root_observability` + `_print_stage42`
(`gateway_commands.py:74-79`). Refusal codes are a `StoreRefusal.reason →
harness error code` table (`_REFUSAL_CODES`, `gateway_commands.py:95-115`;
families in `hermes_cli/harness_support.py:100-228`: `invalid_payload` 2,
`not_found` 3, `pairing_locked_out` 6, `runtime_unavailable` 7,
`store_corrupt` 1).

`cmd_gateway_pair` (`:179-266`) ENSURES identity + certificate, mints, prints
`{code, expires_in_seconds, tier, device_name, install_id, cert_fingerprint,
endpoint, qr_payload, note?}` where `qr_payload` is the compact JSON string
`{host, port, install_id, cert_fingerprint, code}` and `note` says the endpoint
is `config` or unknown (`:255-265`). `cmd_gateway_peers_pair` (`:406-479`) is
the same shape with `peer_code` / `join_payload` / `next_step` /
`note_endpoint`. **Neither refuses when the listener is off — they print a
note.** `_endpoint` (`:138-176`) reads the owner sidecar's `gateway` block
(`live`), else `remote_gateway.*` (`config`), else `unknown`; `_self_endpoints`
(`:381-403`) returns `[]` for a wildcard bind — at most ONE row today.

`cmd_gateway_peers_join` (`:563-702`) parses payload-or-code
(`_parse_join_payload` `:482-560`, flags OVERRIDE the payload), dials with
`ServeSocketClient(..., tls=True, cert_fingerprint=parsed["cert_fingerprint"])`
— **the pin is the payload's own fingerprint: trust-on-first-use** — sends
`peer_join_hello` (`serve_socket.py:1949-2010`), refuses no `peered` block or
an `install_id` mismatch, then `record_peer(...)`. The ack carries
`this_install` and a `note` when this root advertised no endpoint.

`cmd_gateway_peers_revoke` (`:723-760`): `revoke_peer` local only; one-sided
by design (module docstring `gateway_peers.py:98-106`).

The CLI contract dump gate: `scripts/dump_cli_contract.py --check` against
`tests/fixtures/hermes_cli_contract.json` (`:70`); the launcher holds a byte
mirror at `test/features/mission_control/fixtures/hermes_cli_contract.json`.
**A new verb or flag moves the fixture; regenerate with `--write` and name the
launcher mirror as OWED.**

### 0.4 The wire: `hello_ok`, the `gateway` block, the `peered` block

`_hello_ok_frame` (`hermes_cli/harness_parts/serve.py:3513-3572`) sends
`{event, pid, boot_id, contract, hello_contract, schema_version, transport,
connection, runtime_root, build, install, build_mismatch, draining, rpc, ops,
gateway, **_pairing_block}`. `gateway_block` is built ONCE: `{"outcome":
"disabled"}` at `:2539`, replaced by `start_gateway_listener`'s return
(`:593-604`: `{outcome: "listening", host, port, started_at,
cert_fingerprint}` or `{outcome: "error:<T>", host, port}`), and rides
`ready` (`:2751`), `hello_ok` (`:3566`) and `version` (`:3915`). The owner
sidecar is re-published with `gateway: {host, port, cert_fingerprint}`
(`:2560-2585`) — that is what `_endpoint` reads as `live`.

**The block's key set is pinned exactly** by
`tests/agent_runtime/test_serve_gateway_lane.py:338-344`:
`{outcome, host, port, started_at, cert_fingerprint}`. Adding
`capabilities` reds that test in the same commit — named here so it is a
regen, not a surprise. The launcher reads the block by key
(`lib/features/mission_control/data/mission_runtime_gateway_address.dart:11,75`)
and ignores unknown keys; `test_gateway_peer_two_roots_e2e.py` reads
`ready["gateway"]["port"]`/`["cert_fingerprint"]` by key.

`_pairing_block` (`serve.py:394-440`) pops ONE of `paired: {device_id, tier,
device_token}` / `peered: {peer_install_id, peer_secret}` — read-and-clear,
the only frame that carries a secret. **`peered` carries no expiry today**;
`record_peer` on B therefore has nothing to store for one.

### 0.5 The peer methods and what pins them

Three handlers in `agent_runtime/serve_rpc.py`: `peer.ping` (`:3031`,
`TIER_READ`, touches no store), `peer.agent_chat.execute` (`:3114`,
`TIER_CONSOLE`), `peer.media.get` (`:3201`, `TIER_CONSOLE`). The peer-identity
refusal pattern is at `:3155-3167` / `:3238-3249`: `caller.peer_install_id`
read off `context.caller` (set only by
`call_authorization.caller_for_connection`, `call_authorization.py:325`), and
a non-peer is refused `ERR_HANDLER_FAILED` with `data.reason =
PEER_CHAT_NOT_A_PEER_REASON = "peer_identity_required"` (`:3111`).
`_correlation_id_param` (`:602`) fences an optional `correlation_id`
(`CORRELATION_ID_INVALID_REASON` `:591`). Registration is `@method(name,
tier=…)` (`:364`), tier in `_METHOD_TIERS` (`:185`), published by
`manifest()` (`:415`); the chokepoint `handle_request` (`:503`) calls
`authorize_call(method_tier(name), context.caller, method=name)` (`:554`).

`PEER_METHOD_ALLOWLIST` (`agent_runtime/call_authorization.py:200-202`) =
`{peer.ping, peer.agent_chat.execute, peer.media.get}`; the peer arm of
`authorize_call` (`:449`, arm at `~:551-570`) answers ONLY from the set and
runs before the open read-tier arm. Pins that MUST move by exactly the new
names: `tests/agent_runtime/test_peer_authorization.py:139-160`
(`test_the_allowlist_is_exactly_its_three_verbs_and_all_methods_exist`),
`:371` (`test_the_peer_prefix_is_the_declaration_that_it_touches_no_level` —
every registered `peer.*` name == the allowlist),
`tests/agent_runtime/test_peer_chat_execute.py:79`
(`test_the_peer_surface_is_exactly_three_verbs_wide`), and the e2e
`test_gateway_peer_cross_install_chat_e2e.py:290-299` ("still exactly two
verbs wide" — the comment is stale at three; the assertion is a `scope_denied`
on `runtime.chat.message`, which stays true).

### 0.6 The target grammar, the tools, the dispatch, the delivery

`agent_runtime/gateway_targets.py`: `parse_install_target` (`:196-245`),
`resolve_install_target` (`:248-336`) reads `list_peers`, id first then
casefolded name, refuses `unknown_peer_install` (with a hint of usable names,
`:293-306`), `ambiguous_peer_install` (with candidates), `peer_revoked`; the
resolver is the ONLY reader of the peer store on the send path and consults
no cache. `peer_store_root()` (`:131-158`) is the head-home store root — the
one every tool/supervisor reader must use.

`tools/agent_chat_tool.py`: five tools registered `toolset="agent_chat"`
(`:1479-1556`). Only `agent_chat_send` reads `@install/` (`:277-350`):
refuses `remote_requires_detached` for `wait=true` (`:316-333`), resolves,
refuses deterministically. `agent_chat_threads` (`:1012-1126`) = sender-scoped
`addressable_roster` + `persona_chat_history_summary` rows
`{persona_id, persona_instance_id, display_name, handle, session_id,
has_thread, title?, last_activity?, message_count?}`. `agent_chat_open`
(`:1238-1329`) = `_resolve_chat_lane_target` (`:1152-1236`) + the
`_session_belongs_to_chat_lane` guard (`:1129-1150`, refusal
`foreign_session`) + `persona_chat_session_messages(session_id, limit)`
(`agent_runtime/persona_chat_history.py:545`; `MAX_PERSONA_CHAT_MESSAGE_TAIL
= 40`, `:232`) → `{ok, target_persona, handle, session_id, has_thread, count,
redaction_status, messages: [{role, text, timestamp}]}`. A local `@mac/...`
in either read tool today falls into `_resolve_mission_chat_persona_id` and
is refused `unsupported_persona`.

`tools/agent_chat_dispatch.py`: `build_peer_execute_params` (`:578-620`)
carries `turn_request_id, target, message, max_seconds, title?, session_id?,
new_session?` — **no `clarify_token`, no `requested_by`**, by argument.
`_run_remote_dispatch` (`:665-923`) dials with `dial_peer`
(`PEER_DIAL_TIMEOUT_SECONDS = 15.0`, backoff `5.0`), settles deterministically
on a far `error` (`:748-768`), reads the far payload and records
`target_session_id = payload.session_id` (`:879-881`). `dispatch_delivery.py:912-918`
renders `Their thread: <session_id> (agent_chat_open with this session_id to
read the whole exchange)` — a pointer that is a local MISS today (§2.2 step 4
of the parent plan). Far side: `normalize_peer_chat_execute`
(`agent_runtime/chat_turn.py:346-467`) runs the turn with `--requested-by
peer:<install>` and NO `--workspace-id`, so a bare far target resolves in B's
ACTIVE workspace (`sender_scope_workspace_id`,
`persona_assignments.py:3206-3249`: no owning instance → active).

### 0.7 The HUD, its field contract, and the goldens

`agent_runtime/runtime_hud.py`: `HUD_FIELDS` (`:151-166`) declares nine keys;
`tests/agent_runtime/test_runtime_hud_field_contract.py:77` asserts every key
the resolver can emit is declared. `resolve_situational_hud` (`:686-753`)
builds `roster` from `_roster_block` (`:567-582`: entries `{display_name,
persona_instance_id, is_self?}`, capped `SITUATIONAL_HUD_ROSTER_CAP = 16`,
`:64`) and renders `- On level (n): Name (@handle), …`
(`:851-854`). Two feeders: `situational_hud_for_instance` (`:1190`, the chat
turn) and the snapshot (`prompt_observability.py:915-940`). Five committed
stream goldens carry `core.prompt_observability.chat_contexts[*].situational_hud.roster`
(`tests/fixtures/stream_frames/delta_agent_create_narrow_profile.json`) —
fixture roots have no peers, so a new `installs` block that DROPS when empty
leaves those bytes alone. **But `decision_contract_hash` rides the same
goldens** (`delta.json`, `hydrate.json` …: `"decision_contract_hash":"b6985ac4…"`),
so registering ANY event moves every golden that carries it (§5).

### 0.8 Events: registry, gates, precedent

`agent_runtime/decision_contract_registry.py`: `EventContract(event_type,
display_label, summary_fields, detail_fields, redacted_fields)` (`:21`);
`_EVENT_CONTRACTS` literal (`:108-395`; the `realm.*` rows `:255-278`, the
`dispatch.*` rows `:389-394` are the closest precedents);
`validate_event_payload` (`:53`) is enforced at `EventLog.append`
(`agent_runtime/events.py:139-158`), payload cap `EVENT_PAYLOAD_LIMIT_BYTES =
4096` (`:20`). Gates: `tests/agent_runtime/test_s15_event_contract_pruning.py:240`
pins `SURVIVING_EVENT_COUNT = 59` (→ 64);
`tests/agent_runtime/test_s55_registered_events_have_emitters.py` requires a
LITERAL emission site per registered type — a literal, a module-level string
constant, or a wrapper that forwards its parameter into `Event(...)`.
**Precedent for a CLI-process emit that a running serve's stream picks up:**
`realm_sync._append_realm_sync_event` (`agent_runtime/realm_sync.py:673-695`)
appends `realm.sync.pulled` from the `harness realm sync` process; the
serve's stream advances on the watermark. That is E1 already working for
out-of-serve writers — the "mtime check" therefore has ONE job left (§1
R-S2-8).

### 0.9 What S0a and S0b handed this lane

* `harness_core` (`toolsets.py:406-436`) includes `agent_chat` by TOOLSET
  NAME — a tool registered into `agent_chat` joins with no edit there. The
  ratchet `tests/agent_runtime/test_harness_core_ratchet.py:42-43` pins
  `DECLARED_TOOL_COUNT = 43`, `DECLARED_TOKEN_ESTIMATE = 1149`;
  `scripts/emit_harness_tool_inventory.py --check` gates SKILL.md's generated
  block (`harness-skills/harness-runtime-model/SKILL.md:154-176`),
  `references/tool-inventory.md` and `.json`; `test_harness_tool_inventory.py:158`
  asserts every `cli_only_verbs` entry has an Operate row. `agent_chat_installs`
  makes it 44 and a new token estimate; both are re-measured, not guessed.
* S0b's frozensets + partition test (§0.2) make the sidecar a MOVE.
  `PeerRecord` docstring (`gateway_peers.py:261-292`) already says which side
  each field is on.
* Launcher S0b: `MissionInstallRegistry.refreshFromGreeting` refuses a
  fingerprint CHANGE (`tls_fingerprint_mismatch`); the launcher parses the
  `gateway` block by key; Unpair revokes the LOCAL peer first and defers the
  far side to S2c's `revoked_you`
  (`s0b-add-install-data-model.md` S0b-R6).
* Backend S1 §4: `POST …/pair-grants` returns `id` = the grant id;
  `device.pair_requested` → the target runs `introduce --for-install
  <requester install> --for-device <from_device_id> --correlation <grant_id>`
  then POSTs `fulfil` with a `payload` whose RECOMMENDED keys are
  `{peer_join_payload, device_pair_payload, install_id, endpoints,
  cert_fingerprint, correlation}` (≤ 4096 bytes compact); the requester reads
  it once and runs `peers join` + the device redemption.
  `DeviceOut.gateway_cert_fingerprint` is the account-attested value `peers
  join` compares against before first contact.

### 0.10 Two facts a builder would otherwise get wrong

1. **A transcript read inside the serve process is new.** Every transcript
   read today runs in a CLI/child process (`harness persona chat history`,
   the tools). `persona_chat_session_messages` resolves its store through
   `resolve_chat_session_scope(session_id)` (`persona_chat_history.py:566-590`);
   the ambient rung is refused unless `HERMES_ALLOW_AMBIENT_CHAT_READS`
   (`chat_session_scope.py:594-601`) and a head mismatch answers
   `chat_scope_mismatch`. `peer.thread.read` must surface those as its own
   typed refusal, never set the env, and be proven on the real serve by the
   e2e (§3).
2. **The e2e roots hold no persona.** `_sandbox_env`
   (`test_gateway_peer_two_roots_e2e.py:76-100`) writes only the
   `remote_gateway` block; the chat e2e's turn on B ends `unknown persona dev`
   (`test_gateway_peer_cross_install_chat_e2e.py:266-272`) and that IS its
   proof. S2b's parity e2e needs a real addressable instance on B; §3 names
   the seeding step as the one derived detail the builder confirms.

---

## 1. Sub-rulings the builder must not decide alone

Each has a recommendation; the builder implements the recommendation unless
the orchestrator overrides before the stage that consumes it starts.

| # | Question | Recommendation | Consumed by |
|---|---|---|---|
| **R-S2-1** | The exact `hello_ok.gateway.capabilities` vocabulary, and where it rides. | `GATEWAY_CAPABILITIES: tuple[str, ...] = ("announce", "introduce", "roster", "thread_read")` — R-IP16's four words, sorted, in a NEW module `agent_runtime/gateway_capabilities.py` (no serve import; the CLI and the serve both read it). Stamped on the `gateway` block for EVERY outcome (`disabled`, `error:*`, `listening`) by one helper `with_capabilities(block)` called where `gateway_block` is initialised (`serve.py:2539`) and where `start_gateway_listener`'s block is adopted (`:2541`), so it rides `ready`/`hello_ok`/`version` unchanged elsewhere. Also printed by `harness gateway id` so the loopback argv lane can feature-detect without a socket. **Why every outcome:** S3's request loop runs `introduce` on its OWN serve over loopback argv; that serve may not be listening yet, and "does this hermes know the verb" is not the same question as "is the LAN door open". The lane test's exact key-set pin grows to `{outcome, host, port, started_at, cert_fingerprint, capabilities}` for `listening` and `{outcome, capabilities}` for `disabled` (`test_serve_gateway_lane.py:239` asserts `== {"outcome": "disabled"}` today — it moves too). | S2 |
| **R-S2-2** | What `gateway id`'s new `endpoints` list is, and how it is computed. | `_candidate_endpoints(store_root) -> list[{host, port}]` in `gateway_commands.py`, replacing the body of `_self_endpoints` (so `peers pair`/`join` payloads and hellos carry the same list — `clean_endpoints` already accepts up to `MAX_ENDPOINTS = 4`). Source = `_endpoint(root)` (`live` → `config` → `unknown`). If `unknown` → `[]`. If the host is a concrete non-loopback address → `[{host, port}]`. If the host is a wildcard (`0.0.0.0`, `::`) → enumerate this machine with the stdlib only: the default-route IPv4 via the UDP-connect trick (`socket.socket(AF_INET, SOCK_DGRAM).connect(("10.255.255.255", 1)); getsockname()` — no packet is sent) first, then `socket.getaddrinfo(socket.gethostname(), None)` IPv4s, then global/ULA IPv6; exclude loopback (`127/8`, `::1`), link-local (`169.254/16`, `fe80::/10`) and wildcards; dedupe; cap 4. `gateway id` prints `endpoints`, `endpoints_source` (`live|config|unknown`), `listener` (the live block minus nothing secret) and `capabilities`. **Not on the frame:** interface enumeration is a CLI-time computation; the frame keeps its boot-time `host`. | S2 |
| **R-S2-3** | The `introduce` verb's flags and envelope. | `harness gateway introduce [--for-install <peer_install_id>] [--for-device <account_device_id>] [--correlation <grant_id>] [--note <text>] --json`. At least one `--for-*` is required (a phone has no install: device half only, parent plan S3 "both that apply"). Refuses `runtime_unavailable` (family 7) with `peers pair`'s own sentence (`gateway_commands.py:263-265`: "remote_gateway.listen is off for this root: nothing will accept this code until an interface is configured and the runtime restarts") when `_endpoint(root)["source"] == "unknown"`; a `config`-only endpoint is allowed with `note` (the serve may simply not have booted). Envelope kind `gateway_introduction`: `{install_id, display_name, cert_fingerprint, endpoints, endpoints_source, capabilities, correlation, for_install_id, for_device_id, credential_ttl_seconds: 2592000, peer: {peer_code, join_payload, expires_in_seconds} \| null, device: {code, qr_payload, tier: "console", expires_in_seconds} \| null, grant_payload: {peer_join_payload, device_pair_payload, install_id, endpoints, cert_fingerprint, correlation}}`. **`grant_payload` is byte-for-byte what the launcher POSTs to `fulfil`** (backend §4.1's recommended keys) so there is one writer of that shape; the plaintext codes appear in `peer.peer_code`/`device.code` AND inside the two payload strings and nowhere else — never in an event, never in a log line (the codes discipline, `gateway_pairing_codes.py:70-71`). Composition, not a third ceremony: it calls `mint_peer_code` and `mint_pairing_code` under one `_store_lock` hold? **No** — two separate mints, each atomic, because the two-code cap (`MAX_PENDING_CODES = 3`) can refuse the second; the envelope then carries `peer` and `device: null` with `refusals: [{half, reason}]` and exit 0 only when BOTH requested halves minted, else the first refusal's exit family. | S2 |
| **R-S2-4** | How a minted code is scoped to the requester (R-IP5 "both scoped to A's install id / device id") when a code is a bearer. | Peer half — a real check: the pending entry's `extra` gains `for_install_id`; `redeem_peer_code` refuses (as `invalid_code`, charging one failure, `note_failed_redeem`) when the entry names an install and the hello's `peer_install_id` differs. Device half — a label, not a check: hermes never learns the account device id at redeem (the row's `device_id` is minted `dev_<hex>` at `serve_gateway_auth.py:659`), so the pending entry's `for_device_id` is COPIED onto the device row as a cache field `account_device_id` at redeem, which is the join key S3's Unpair / the sheet use to relate the far `devices.json` row to the account row (R-IP14 "one bookkeeping"). The docstring says plainly that the device code is still a bearer for its 600 s. | S2 |
| **R-S2-5** | Expiry fields, where they live, and how the verifiers refuse. | Both rows gain `expires_at` (ISO-8601 UTC or `null`), classified TRUST (this install decided it at mint; the network never moves it): `PEER_ROW_TRUST_FIELDS += {"expires_at"}`; the device row gains it beside `created_at`. `null` = never expires and is what `gateway pair` / `peers pair` keep minting, so the manual S0 ceremony's behaviour is byte-unchanged; `introduce` mints with `CREDENTIAL_TTL_SECONDS_INTRODUCED = 30 * 86400` (one constant in `serve_gateway_auth.py`, imported by `gateway_peers.py`, which already imports from it). The TTL rides the pending entry's `extra` (`credential_ttl_seconds`) so redeem computes `expires_at = stamp + ttl`. B's half: `hello_ok.peered` gains `expires_at` (additive) and `record_peer(..., expires_at=)` stores it — one edge, one expiry at both ends. Verifiers: `verify_peer_proof` / `verify_device_proof` check expiry AFTER the proof, exactly where revocation is checked and for the same probing reason (`gateway_peers.py:552-560`), answering new typed outcomes `PEER_AUTH_EXPIRED = "peer_expired"` / `AUTH_EXPIRED = "device_expired"`; **the wire stays collapsed** (one rejection frame for every credential failure — the module rule at `:517-522`), the reason is for the log, the rate limiter and the tests. The far side already holds its own `expires_at`, so it refuses ITSELF first: `usable_peers` (R-S2-16) excludes an expired row, `resolve_install_target` answers `peer_expired`, and `dial_peer` refuses an expired row before opening a socket. `PeerRecord`/`DeviceRecord` gain `expires_at` and a computed `expired` on `payload()`; `peers list`/`devices list` show both. Renewal is S3's (a fresh introduction under 7 days). | S2, S2c |
| **R-S2-6** | The attested-fingerprint check on `peers join`. | `peers join` gains `--expect-fingerprint <64 hex>` (what S3 passes from `DeviceOut.gateway_cert_fingerprint`) and `--correlation <grant_id>`. With `--expect-fingerprint`: refuse BEFORE any dial when the payload's `cert_fingerprint` ≠ expected — `emit_harness_error(RuntimeError("tls_fingerprint_mismatch"), code="invalid_payload", …)` so the R-IP17 word is the error's reason and family 2 is the exit; then dial pinned to the expected value. Without the flag: today's TOFU pin, and the ack gains `fingerprint_attested: false`. The receipt carries `correlation` when given. | S2 |
| **R-S2-7** | `peers_cache.json` row shape and its writers. | `<store_root>/gateway/peers_cache.json` = `{"contract": 1, "peers": {<peer_install_id>: row}}`; row = `{peer_install_id, announced_display_name, endpoints, cert_fingerprint, last_seen, last_hello_at, reachability, unreachable_since, roster, revoked_you, revoked_you_at, fingerprint_rotation, last_announce_at, correlation}` with `reachability ∈ {"unknown","reachable","unreachable"}`, `roster = {fetched_at, workspace_id, rows: [...]} \| null`, `fingerprint_rotation = {announced_at, new_fingerprint} \| null`; declared as `PEER_CACHE_ROW_FIELDS` with its own `_cache_row()` and partition test. Writers (all in `gateway_peers.py`, all cache-only, each emits): `note_peer_seen` MOVES here (`last_seen`, `last_hello_at`, `reachability="reachable"`, clears `unreachable_since`); `cache_peer_hello(root, id, *, display_name, endpoints, cert_fingerprint)` from the verified peer hello — the client `peer_hello` frame (`serve_socket.py:1895-1947`) gains optional `peer_display_name`/`peer_endpoints`/`peer_cert_fingerprint` (the join hello already carries them) so every hello refreshes the cache; `note_dial_result(root, id, *, ok, error)` from `dial_peer` and the tool reads; `cache_peer_roster(root, id, *, workspace_id, rows)` from `agent_chat_installs(install=)`; `apply_peer_announce(root, caller_id, payload)` from the `peer.announce` handler. **The trust file loses `last_seen`**: `_row` drops it, `PEER_ROW_CACHE_FIELDS` becomes `{display_name, endpoints, cert_fingerprint}` (pairing-time snapshots — the offline name and the dial pin), `_decode_peer` tolerates and ignores a legacy `last_seen`. A cache row can never write a trust field: the two files have two writer sets and a test asserts that no function in the cache writer set touches `peers.json`'s bytes. Dial order (R-IP14): cache `endpoints` first (freshest), then trust `endpoints` not already tried; **the pin is ALWAYS the trust row's fingerprint** — a `fingerprint_rotation` notice is shown, never applied; re-pair is the cure (S0b B2). | S2c |
| **R-S2-8** | Where the "mtime check" lives, given §0.8's precedent. | Every write door emits its `gateway.peer.*` event from ITS OWN process (the `realm.sync` precedent), so a CLI `peers join`/`revoke` beside a running serve is already E1-visible with no check. The check's one remaining job is a write that emitted nothing (an editor on `peers.json`; a pre-S2c binary). `peer_store_revision(store_root) -> (trust_mtime_ns, cache_mtime_ns)` (0 when absent); `list_peers`/`read_peer_cache` record the revision they read in a process-local `_LAST_SEEN_REVISION`, every writer records the revision it WROTE, and a read that observes a revision this process neither wrote nor seeded emits ONE `gateway.peer.updated {store: "trust"\|"cache", change: "external_write", store_revision}`. A fresh CLI process seeds on its first read and never emits; the serve reads repeatedly (every gateway hello through `_gateway_authenticator`, every `peer.*` call, every `_connections_frame`) and so is the process that notices. A stat, not a poll: nothing runs on a timer. | S2c |
| **R-S2-9** | `peer.announce` payload schema and the caller-only rule. | Params `{contract: 1, display_name?, endpoints?, cert_fingerprint?, roster_changed?: bool, revoked_you?: bool, correlation_id?}`. **No install-id field**: the row written is `rows[context.caller.peer_install_id]` — the same posture as `normalize_peer_chat_execute` (`chat_turn.py:354-366`: the field a peer could type does not exist). A payload that carries `peer_install_id`/`install_id` ≠ caller is refused `ERR_INVALID_PARAMS` `announce_names_other_install`; equal is ignored. Non-peer → `peer_identity_required`. Effects, cache only: `announced_display_name`, `endpoints` (clean_endpoints), `fingerprint_rotation` when the announced fingerprint ≠ the trust pin (never written to the pin), `roster = null` + `roster_stale: true` on `roster_changed`, `revoked_you = true` + `revoked_you_at` on `revoked_you`. **One-way:** `revoked_you` and the trust `revoked` are cleared only by a trust write (`record_peer`/`redeem_peer_code` on re-pair) — an announce cannot un-revoke either. Result `{accepted: true, contract: 1, peer: <caller>, cache_written: [field names], correlation_id?}`. Tier `console` (it writes a store; `read` is open to a caller the transport could not place — `peer.media.get`'s argument at `serve_rpc.py:3227-3231`). | S2c |
| **R-S2-10** | `peer.roster.list` projection fields and workspace scoping. | Params `{target?: str, correlation_id?}`. Scope = the workspace the caller's target would resolve in: when `target` is a resident `personainst_*` handle → `effective_workspace_id(instance, active_workspace_id=WorkspaceStore().active_id())` (`workspace_scope.py:75-84`); otherwise the active workspace — which is exactly what a bare `@B/dev` turn resolves in (§0.6). Rows = `workspace_scope.addressable_roster(...)` (`:223-248`) filtered by `_resolve_mission_chat_persona_id` reachability (so every row is sendable, as `agent_chat_threads` already does, `agent_chat_tool.py:1094-1098`), projected to `{handle, persona_id, label, is_canonical_primary, last_turn_at, workspace_id}` with `last_turn_at = persona_chat_history_summary(...)[session].updated_at` for the instance's default thread (the same two calls `agent_chat_threads` makes). Result `{contract: 1, peer, workspace_id, count, truncated, rows, at}`, cap 64. Lives in ONE function `peer_roster_projection(*, scope_workspace_id)` in a new `agent_runtime/peer_directory.py`; the handler is a thin door. Tier `read` (the same facts `runtime.office.get` hands a read-tier device). | S2b |
| **R-S2-11** | `peer.thread.read` params, clamp and refusal codes. | Params `{target: str, session_id: str, limit?: int, correlation_id?}` — `target` is REQUIRED so the far side applies the SAME lane guard the local `agent_chat_open` applies (`_session_belongs_to_chat_lane`, `agent_chat_tool.py:1129-1150`): resolve `target` as `_resolve_chat_lane_target` does with no sender scope (active workspace), then the guard. `limit` clamped `1..MAX_PERSONA_CHAT_MESSAGE_TAIL` (40) exactly as today (`:1259-1262`). Refusals, distinct and in this order: `peer_identity_required` (non-peer), `ERR_INVALID_PARAMS` (missing/oversize `target`/`session_id`, `limit` not an int), `unsupported_persona` (target unknown here), `foreign_session` (guard), `thread_unreadable` (the reader answered `ok: False` — its `error_kind` rides in `data`, incl. `chat_scope_unresolved`/`chat_scope_mismatch` from §0.10). Result = `agent_chat_open`'s dict + `{contract: 1, peer}`. `messages[].text` is already redaction-safe (`redaction_status`); nothing else is added. Tier `console` (transcript text is the operator's conversation; `peer.media.get`'s reason). The reader is shared: factor `agent_chat_open`'s post-resolution body into `read_chat_lane_tail(target, session_id, limit)` in `peer_directory.py` (or a sibling) so the local tool and the far door are one implementation. | S2b |
| **R-S2-12** | How `agent_chat_open` / `agent_chat_threads` accept `@install/`. | Both parse with `parse_install_target`; `None` → today's local path byte-for-byte. `agent_chat_open(persona_id="@mac/neko_supervisor", session_id=…, limit=…)` → `resolve_install_target(peer_store_root(), parsed)` (through `usable_peers`, S2c; until S2c lands, through `list_peers` as today) → `call_peer_method(root, install_id, "peer.thread.read", {target, session_id, limit})` → the far dict with `install: {install_id, display_name}` added and `target_persona` spelled `@<ref>/<target>`. `session_id` omitted → refuse `remote_session_required` ("name the thread: the session_id your dispatch delivery reported as 'Their thread'") — there is no "our default thread" on another install. `agent_chat_threads(persona_id="@mac/…")` → `peer.roster.list {target}` rows in the threads row shape `{persona_id, persona_instance_id, handle: "@mac/<handle>", display_name, has_thread: null, last_activity: last_turn_at, install: {…}}` filtered to the named persona/handle when one follows the `/`; `agent_chat_threads(persona_id="@mac/")` is refused by the parser today (`install_qualifier_target_empty`) and stays refused — the directory verb is `agent_chat_installs`. Transport failure → `_refusal(..., error_kind="peer_unreachable")` (`dispatch_store.REMOTE_UNREACHABLE_REASON`); a far `error` → its `data.reason` verbatim. Timeouts for in-turn reads: `PEER_READ_DIAL_TIMEOUT_SECONDS = 5.0`, `PEER_READ_REPLY_TIMEOUT_SECONDS = 10.0`, constants in a new `tools/agent_chat_remote.py` that owns `call_peer_method(root, install_id, method, params, *, dial_timeout, reply_timeout) -> dict` (dial with `dial_peer`, send one JSON-RPC, read to the reply id, close; `note_dial_result` in S2c). One helper, three tool callers, one outbound-announce caller. | S2b |
| **R-S2-13** | The HUD install lines and the "also on @mac" residency note. | New STABLE field `installs` (`HudField("installs", volatile=False, summary="paired installs, cached rosters, residency")`) — a list `{ref, install_id, display_name, reachability, unreachable_for?, roster: [{handle, persona_id}] (cap 8), roster_fetched_at?}`, cap 8 installs, present only when `usable_peers` is non-empty (drops when empty, like `board`). `ref` = the spelling the grammar accepts: the display name when unique among usable peers, else the install id (so the line never shows an address a send would refuse as ambiguous). Render, after the `On level` line: `- Installs (2): @mac reachable · @studio unreachable 12 min` then one indented line per install with a cached roster: `  - @mac: neko_supervisor (@mac/personainst_neko_supervisor_agent_2e94fab3), dev (@mac/personainst_dev_agent_2)` — each handle spelled as the FULL address so the line is actionable (the `_handle` rule, `runtime_hud.py:826-834`). Residency (R-IP11): a local roster entry whose `persona_instance_id` appears in a cached far roster gains `also_on: [{ref, last_turn_at}]` and renders `Neko (@personainst_neko…) [also on @mac, last turn there 3 min ago]`; the age is computed from `last_turn_at` at render, from the CACHED roster only — the HUD never dials. Fed by both feeders through one function `installs_hud_block(store_root)` in `peer_directory.py` that reads `usable_peers` + the cache. | S2b (lines, from `list_peers`), S2c (switch to `usable_peers`; reachability words) |
| **R-S2-14** | How `--correlation <grant_id>` is stamped. | Fenced with the SAME rule as the RPC `correlation_id` (`serve_rpc._correlation_id_param`, `:602`; reuse its regex/length via a small shared `agent_runtime/correlation.py` if it is not already importable without pulling `serve_rpc`) — the CLI refuses an unfit token as `invalid_payload` `correlation_id_invalid`. Stored on the two PENDING entries (`extra.correlation`) so the redeem-time events carry it; printed on the `introduce` envelope and on the `peers join` receipt; **never a row field** (an audit fact, not a trust or cache fact) and never a secret. Every `gateway.peer.*` event carries `grant_id` in `detail_fields` when one exists. | S2, S2c |
| **R-S2-15** | The `revoked_you`-before-local-revoke ordering. | `cmd_gateway_peers_revoke`: (1) `announce_to_peers(root, {revoked_you: true}, only=[id], attempts=2, timeout=5.0)` — best-effort, result recorded; (2) `revoke_peer` (the local trust write); (3) emit `gateway.peer.revoked {peer_install_id, announced: bool, grant_id?}`. A failed announce never blocks the revoke (the far side then learns at its next dial's refusal, as today); the ack names `announced: false` so the operator knows. `--no-announce` exists for the offline case and the tests. `dry_run` announces nothing. | S2c |
| **R-S2-16** | The `usable_peers` predicate. | `usable_peers(store_root) -> list[UsablePeer]` in `gateway_peers.py`; `UsablePeer(record: PeerRecord, cache: PeerCacheRow \| None, ref: str)`. A row is usable iff `not record.revoked and not record.expired and not (cache and cache.revoked_you)`. `ref` per R-S2-13. Readers: `resolve_install_target` matches id-then-name against `usable_peers`; when nothing usable matches it consults `list_peers` ONLY to name the reason — `peer_revoked` (existing), `peer_expired`, `peer_revoked_you` (new `REASON_PEER_EXPIRED`, `REASON_PEER_REVOKED_YOU`), else `unknown_peer_install`; the hint of paired names lists usable refs only. `agent_chat_installs` rows = `usable_peers` verbatim; the HUD reads the same; `peers list` shows every row with a computed `usable: bool` and `unusable_reason` so the sheet's REMOVED group is a filter on one list. **Byte-identical**: a test asserts `[u.record.peer_install_id for u in usable_peers(root)] == [row["install_id"] for row in json.loads(agent_chat_installs())["installs"]]` and that every listed id resolves while every unlisted paired id refuses. | S2c (S2b lands the tool on `list_peers` minus revoked, then S2c swaps the predicate in — one commit) |
| **R-S2-17** | What the new tool costs the S0a ratchet and manual. | `agent_chat_installs` registers `toolset="agent_chat"` → `harness_core` resolves 44. The builder re-measures: `DECLARED_TOOL_COUNT = 44`, `DECLARED_TOKEN_ESTIMATE = <measured>` (schema description under 400 chars so the delta stays small), regenerates the three inventory artifacts with `--write`, adds the Operate row *"see which other installs (machines) you can reach and who is on them → `agent_chat_installs` (read-only; `install=` fetches that install's roster)"* and extends the `agent_chat_threads`/`agent_chat_open` rows with "`@install/…` reaches a far install (read-only)". The Atlas artifact regenerates from `references/tool-inventory.json` (launcher-side, OWED at landing). | S2b |
| **R-S2-18** | How "introduce is unreachable from every tool registry and allowlist" is asserted. | One test walks: every name in `model_tools`' registry (`registry.list()` / the emitter's inventory), `serve_rpc.manifest()["methods"]`, `PEER_METHOD_ALLOWLIST`, and `ops_manifest(transport="gateway")["ops"]`, and asserts no entry contains `introduce`; plus the existing structural fact restated as an assertion — a gateway connection sending `{"argv": [...]}` is refused `argv_lane_unavailable` (`test_serve_gateway_peer_lane.py:345`). The docstring names the residual honestly (`gateway_peers.py:77-91`): a local agent with a shell can run the verb; that is the machine owner's authority, not a hole this test claims to close. | S2 |

---

## 2. Stages

Order: **S2 → S2b → S2c**, each its own commit group, all in this worktree,
no push. Every wire change is additive; every existing test that pins a shape
is named beside the change that moves it.

### S2 · `introduce`, expiry, attested `join`, `capabilities`, `gateway id` endpoints

**Files:** `agent_runtime/gateway_capabilities.py` (new),
`agent_runtime/serve_gateway_auth.py`, `agent_runtime/gateway_peers.py`,
`agent_runtime/gateway_pairing_codes.py` (docstring only),
`hermes_cli/harness_parts/serve.py`, `hermes_cli/harness_parts/gateway_commands.py`,
`hermes_cli/harness.py`, `agent_runtime/serve_socket.py` (docstring of
`peer_join_hello` for the new `peered.expires_at`),
`tests/fixtures/hermes_cli_contract.json` (regenerated).

1. **`gateway_capabilities.py`** — `GATEWAY_CAPABILITIES` (R-S2-1),
   `GATEWAY_CAPABILITY_CONTRACT = 1`, `with_capabilities(block: dict) ->
   dict` (returns a new dict with `capabilities: list(GATEWAY_CAPABILITIES)`).
   `serve.py:2539` → `gateway_block = with_capabilities({"outcome":
   "disabled"})`; `:2541-2551` → `gateway_server, block =
   start_gateway_listener(...)`; `gateway_block = with_capabilities(block)`.
   The `ready`/`hello_ok`/`version` sites are untouched (they read
   `gateway_block`). Moves: `test_serve_gateway_lane.py:239` and `:338-344`
   (key sets grow by `capabilities`).
2. **Expiry (R-S2-5).** `serve_gateway_auth.py`: `CREDENTIAL_TTL_SECONDS_INTRODUCED
   = 30 * 86400`; `AUTH_EXPIRED = "device_expired"`; `mint_pairing_code(...,
   credential_ttl_seconds: int | None = None, for_device_id: str | None =
   None, correlation: str | None = None)` puts them in `extra` (under the fixed
   keys, as `mint_into` guarantees); `redeem_pairing_code` writes
   `expires_at = _iso(stamp + ttl) if ttl else None` and `account_device_id`
   (R-S2-4) on the row (`:671-680`); `_decode_device` reads both;
   `DeviceRecord` gains `expires_at`, `account_device_id`, and
   `expired` (property; `payload()` carries all three); `verify_device_proof`
   adds the expiry arm after the revocation arm (`:485-487`), returning
   `AUTH_EXPIRED` with the record. `gateway_peers.py`: `PEER_AUTH_EXPIRED =
   "peer_expired"`; `mint_peer_code(..., credential_ttl_seconds=None,
   for_install_id=None, correlation=None)`; `redeem_peer_code` refuses a
   wrong `peer_install_id` against `extra.for_install_id` as `invalid_code`
   with `note_failed_redeem` (R-S2-4) and writes `expires_at`;
   `PeerCredential.expires_at`; `record_peer(..., expires_at: Any = None)`;
   `_row(..., expires_at)`; `PEER_ROW_TRUST_FIELDS += {"expires_at"}`;
   `PeerRecord.expires_at` + `expired`; `verify_peer_proof` expiry arm after
   `:552-560`; `dial_peer` refuses `record.expired` beside `record.revoked`
   (`:838-842`). `serve.py:_pairing_block` → `peered` gains
   `expires_at` (read off `connection.peer_secret_expires_at`, set by the
   authenticator from the `PeerCredential` at `:697-717`);
   `cmd_gateway_peers_join` passes `peered.get("expires_at")` to
   `record_peer`. Moves: `test_gateway_peers_store.py:124,154` (the
   partition — `expires_at` joins TRUST), `test_serve_gateway_auth.py` row
   literal pins if any (the builder greps `"revoked_at": None` there).
3. **`gateway id` endpoints (R-S2-2).** `gateway_commands.py`:
   `_candidate_endpoints(store_root)`; `_self_endpoints` becomes a call to it.
   `harness.py:_cmd_gateway_id` → `_gateway_install_row(identity)` gains
   `endpoints`, `endpoints_source`, `listener: {outcome?, host, port,
   cert_fingerprint}` (from the owner sidecar / config; no secret), and
   `capabilities`. `_cmd_gateway_rename`'s row (same helper) gains them too
   — it is the same row.
4. **`introduce` (R-S2-3, R-S2-4, R-S2-14).** `gateway_commands.py:cmd_gateway_introduce(args)`:
   `_install_and_certificate` (`:334-378`) → listener check via `_endpoint`
   → fence `--correlation` → mint the requested halves → build the envelope
   (`grant_payload` compact JSON ≤ 4096 bytes asserted; refuse
   `invalid_payload` if not, which cannot happen at 4 endpoints) →
   `_object_envelope("gateway_introduction", row)`. `harness.py`: subparser
   `gateway introduce` with `--for-install`, `--for-device`,
   `--correlation`, `--note`, `_add_stage42_global_args(gateway_introduce)`
   (reader flag set, `pair`'s reason at `:410-413`), wrapper
   `_cmd_gateway_introduce`. `gateway_commands.__all__` grows. `peers join`
   gains `--expect-fingerprint` and `--correlation` (R-S2-6). Regenerate
   `tests/fixtures/hermes_cli_contract.json` with `python
   scripts/dump_cli_contract.py --write`; **the launcher's
   `test/features/mission_control/fixtures/hermes_cli_contract.json` byte
   mirror is OWED at landing.**
5. **Docstrings:** `gateway_peers.py` module docstring "four peer verbs"
   paragraph (`:65-77`) names `introduce` as a COMPOSITION of the two mints
   with the same R5 residual; `gateway_pairing_codes.py` names the two new
   `extra` keys and that `for_install_id` is checked at match time on the
   peer kind only.

### S2b · the directory and the thread, read-only

**Files:** `agent_runtime/peer_directory.py` (new), `agent_runtime/serve_rpc.py`,
`agent_runtime/call_authorization.py`, `tools/agent_chat_remote.py` (new),
`tools/agent_chat_tool.py`, `agent_runtime/runtime_hud.py`,
`agent_runtime/prompt_observability.py`, `tests/agent_runtime/test_harness_core_ratchet.py`,
`docs/agent-runtime-harness/harness-skills/harness-runtime-model/SKILL.md` +
`references/tool-inventory.{md,json}` (regenerated), canon 03/05 rows (§5).

1. **Allowlist (R-IP9).** `PEER_METHOD_ALLOWLIST` (`call_authorization.py:200-202`)
   grows by exactly `peer.roster.list`, `peer.thread.read`, with a comment
   block in the file's existing shape (the reason: B's projection shaped by
   B's rules; A never guesses). Pins move by exactly those two names:
   `test_peer_authorization.py:139-160` (rename the test to
   `…_exactly_its_verbs…`), `test_peer_chat_execute.py:79` (`five` wide;
   S2c makes it six), and the stale "two verbs wide" comment in the chat
   e2e (`:290`).
2. **`peer_directory.py`** — `PEER_ROSTER_CONTRACT = 1`,
   `PEER_THREAD_CONTRACT = 1`, `ROSTER_ROW_CAP = 64`,
   `peer_roster_projection(*, scope_workspace_id) -> dict` (R-S2-10),
   `resolve_far_target_scope(target) -> str | None` (handle → instance
   workspace; else active), `read_chat_lane_tail(target, session_id, limit)
   -> dict | Refusal` (R-S2-11; the body lifted out of `agent_chat_open`
   `:1252-1329`, which then calls it — one implementation, two doors),
   `installs_hud_block(store_root) -> list[dict]` (R-S2-13; S2b reads
   `list_peers` minus revoked; S2c swaps in `usable_peers` and the
   reachability words).
3. **`serve_rpc.py`** — `@method("peer.roster.list", tier=TIER_READ)` and
   `@method("peer.thread.read", tier=TIER_CONSOLE)` beside `peer.media.get`
   (`:3201`), each: non-peer refusal with `PEER_CHAT_NOT_A_PEER_REASON`;
   `_correlation_id_param`; param fences; call `peer_directory`; result +
   `peer` + `contract` + `correlation_id?`. Manifest rows self-register;
   canon 03 §2's table grows (§5).
4. **`tools/agent_chat_remote.py`** — `call_peer_method(...)` (R-S2-12) and
   the two timeout constants. Returns `{"result": …}` or `{"refusal":
   {reason, message}}`, never raises past a typed transport refusal.
5. **`tools/agent_chat_tool.py`** — new `AGENT_CHAT_INSTALLS_SCHEMA`
   (`install?: str` — a display name or install id; `refresh?: bool`),
   `agent_chat_installs(*, install=None, refresh=None, requested_by_session=None)`:
   no `install` → `{ok, count, installs: [{install_id, display_name, ref,
   endpoints_count, cert_fingerprint, approved_at, expires_at, reachability
   (S2c), roster_cached_at (S2c)}]}` from the peer store, no dial; with
   `install` → resolve (`resolve_install_target` on a synthetic
   `InstallTarget(install_ref, "*")`? **no** — add
   `resolve_install_ref(store_root, ref) -> ResolvedInstall | TargetRefusal`
   to `gateway_targets.py`, the id-then-name half of `resolve_install_target`
   factored out so both share one matcher) → `peer.roster.list` → `{ok,
   install: {…}, workspace_id, count, roster: rows}` and (S2c)
   `cache_peer_roster`. Registered `name="agent_chat_installs",
   toolset="agent_chat"`, description "List the other installs (machines)
   paired with this one, or one install's agent roster (read-only, no
   mint)." `agent_chat_open`/`agent_chat_threads` gain the `@install/`
   branch first thing after `_scope_off()` (R-S2-12); their schema
   descriptions gain one sentence each. `agent_chat_send` gains the
   `clarify_token` portability refusal (`clarify_token_not_portable`) when
   the target is install-qualified — the parity row's "tool maps a far
   clarify to its thread": the refusal text names the `session_id` the
   delivery reported.
6. **HUD (R-S2-13).** `runtime_hud.py`: `HUD_FIELDS += HudField("installs",
   …)`; `resolve_situational_hud(..., installs=None)` sets `hud["installs"]`
   when non-empty and stamps `also_on` onto roster entries; render adds the
   lines after `On level`. Feeders: `situational_hud_for_instance` and
   `prompt_observability._situational_for` pass
   `installs=installs_hud_block(peer_store_root())` (best-effort; `[]` on any
   exception, the block's existing guarantee). Moves:
   `test_runtime_hud_field_contract.py:77` (declared), HUD snapshot tests
   (§3).
7. **S0a artifacts (R-S2-17).** Ratchet constants re-measured; `python
   scripts/emit_harness_tool_inventory.py --write`; SKILL.md Operate rows.

### S2c · the cache sidecar, the events, `peer.announce`, `usable_peers`

**Files:** `agent_runtime/gateway_peers.py`, `agent_runtime/gateway_announce.py`
(new), `agent_runtime/gateway_targets.py`, `agent_runtime/decision_contract_registry.py`,
`agent_runtime/serve_rpc.py`, `agent_runtime/call_authorization.py`,
`agent_runtime/serve_socket.py` (`peer_hello` optional fields),
`hermes_cli/harness_parts/serve.py`, `hermes_cli/harness_parts/gateway_commands.py`,
`hermes_cli/harness.py` (`rename` → announce), `agent_runtime/agent_create.py`
/ `agent_runtime/agent_retire.py` (roster-changed hook), `tools/agent_chat_tool.py`,
`tools/agent_chat_dispatch.py` (`note_dial_result`), `agent_runtime/peer_directory.py`,
`tests/fixtures/stream_frames/*` (regenerated), `tests/agent_runtime/test_s15_event_contract_pruning.py`.

1. **Events.** `decision_contract_registry.py` gains five rows, all with
   `detail_fields` including `grant_id` and `store_revision`, none carrying a
   secret, endpoint list or roster body (4 KB cap; ids only):
   `gateway.peer.recorded (peer_install_id, source)` with `source ∈
   {introduce, pair, join}`; `gateway.peer.revoked (peer_install_id,
   announced)`; `gateway.peer.updated (store, change)` with `change ∈
   {display_name, endpoints, fingerprint_rotation, expires_at,
   external_write}` and detail `peer_install_id`; `gateway.peer.roster
   (peer_install_id, count)` detail `workspace_id, fetched_at`;
   `gateway.peer.reachability (peer_install_id, reachability)` detail
   `unreachable_since, error`. Emitter idiom for the S55 gate: module-level
   constants `PEER_EVENT_RECORDED = "gateway.peer.recorded"` … in
   `gateway_peers.py` and one wrapper `_emit_peer_event(event_type: str,
   payload: dict)` that builds `Event(now(), event_type, None, None, None,
   payload)` and `EventLog().append(...)` best-effort (the realm precedent's
   try/except). Moves: `test_s15_event_contract_pruning.py:240` 59 → 64;
   every stream golden carrying `decision_contract_hash` → `python
   scripts/generate_agent_runtime_stream_fixtures.py` regenerates; **the
   launcher's `test/fixtures/harness_stream/` byte mirror + `MANIFEST.sha256`
   is OWED at landing** (README CROSS-STACK COPY STATUS block, the S0a
   shape).
2. **Sidecar (R-S2-7, R-S2-8).** `gateway_peers.py`: `PEER_CACHE_FILENAME =
   "peers_cache.json"`, `PEER_CACHE_CONTRACT = 1`, `PEER_CACHE_ROW_FIELDS`,
   `PeerCacheRow` dataclass, `peer_cache_path`, `read_peer_cache`,
   `_cache_row`, `_write_peer_cache` (same lock, same `write_secure_json`),
   the writers listed in R-S2-7, `peer_store_revision`, the process-local
   revision memo, `usable_peers` (R-S2-16). `note_peer_seen` re-pointed at
   the cache; `_row` drops `last_seen`; `PEER_ROW_CACHE_FIELDS` shrinks;
   `_decode_peer` tolerates a legacy `last_seen`. `dial_peer`: cache
   endpoints first, trust pin always, `note_dial_result` on both outcomes.
   Every writer emits (`recorded` from `redeem_peer_code`/`record_peer`,
   `revoked` from `revoke_peer`, `updated` from the cache writers,
   `reachability` from `note_dial_result`/`note_peer_seen` on a CHANGE of
   word only, `roster` from `cache_peer_roster`).
3. **`peer.announce` (R-S2-9).** `call_authorization.PEER_METHOD_ALLOWLIST +=
   {"peer.announce"}`; pins move by that one name. Handler in `serve_rpc.py`
   beside the S2b pair. `apply_peer_announce` in `gateway_peers.py` takes the
   caller id positionally and never reads an id from the payload.
4. **Outbound (`gateway_announce.py`).** `announce_to_peers(store_root,
   payload, *, only=None, attempts=2, timeout=5.0) -> list[AnnounceReceipt]`
   over `usable_peers`, one `call_peer_method` each, records
   `last_announce_at`/failure in the cache, never raises. Callers:
   `harness gateway rename` (`display_name`); the serve after `ready` — one
   background thread announcing `endpoints` + `cert_fingerprint` to every
   usable peer once per boot (bounded by peer count; the serve's
   `gateway_block` is the source); `perform_agent_create` /
   `perform_agent_retire` on success (`roster_changed: true`, in a thread so
   a slow peer never delays a create); `cmd_gateway_peers_revoke`
   (`revoked_you`, FIRST — R-S2-15).
5. **Hello refresh.** `serve_socket.ServeSocketClient.peer_hello(...)` gains
   optional `display_name`, `endpoints`, `cert_fingerprint` (frame keys
   `peer_display_name`/`peer_endpoints`/`peer_cert_fingerprint`, the join
   hello's names); `dial_peer` passes this root's identity, `_candidate_endpoints`
   and certificate fingerprint; `_gateway_authenticator`'s `peer_install_id`
   arm (`serve.py:718-731`) calls `cache_peer_hello` beside `note_peer_seen`.
   `_credential_kind` is untouched (these are not credential fields).
6. **The predicate everywhere (R-S2-16).** `gateway_targets.py`:
   `REASON_PEER_EXPIRED`, `REASON_PEER_REVOKED_YOU`; `resolve_install_ref` and
   `resolve_install_target` read `usable_peers`; hints list usable refs.
   `agent_chat_installs`, `installs_hud_block` read `usable_peers`.
   `peers list` rows gain `usable`, `unusable_reason`, and the cache columns
   (`reachability`, `last_seen`, `announced_display_name`, `roster_cached_at`,
   `revoked_you`) under a nested `cache` key so the trust/cache split is
   visible in the ack.
7. **`serve.py` revision reads (R-S2-8).** `_gateway_authenticator` and
   `_connections_frame` call `gateway_peers.note_peer_store_read(store_root)`
   (the memo + emit); the three peer handlers call it via `peer_directory`.

---

## 3. Tests (names are proposals; the builder owns the final ids — one row per behaviour)

### S2

| file | test | asserts |
|---|---|---|
| `tests/agent_runtime/test_gateway_capabilities.py` (new) | `test_the_capabilities_list_is_exactly_r_ip16s_four_words_sorted` | `GATEWAY_CAPABILITIES == ("announce","introduce","roster","thread_read")`; `with_capabilities` never mutates its input |
| `test_serve_gateway_lane.py` (extend `:239`, `:338`) | `test_the_gateway_block_carries_capabilities_on_every_outcome` | `ready["gateway"]["capabilities"]` present for `disabled` and `listening`; key sets are the S2 sets; `hello_ok` and `version` carry the same list |
| `test_serve_gateway_auth.py` (extend) | `test_a_pairing_code_minted_with_a_ttl_redeems_into_a_row_that_expires`; `test_a_pairing_code_minted_without_a_ttl_redeems_into_a_row_that_never_expires`; `test_an_expired_device_is_refused_with_its_own_reason_after_the_proof` (bad proof on an expired row → `bad_proof`, good proof → `device_expired`); `test_the_account_device_id_label_lands_on_the_row_and_is_not_a_check` | expiry arm ordering; label semantics |
| `test_gateway_peers_store.py` (extend) | `test_the_row_shape_is_exactly_trust_fields_plus_cache_fields` (updated sets); `test_a_peer_code_scoped_to_an_install_refuses_any_other_install_and_charges_a_failure`; `test_an_expired_peer_is_refused_after_the_proof_with_its_own_reason`; `test_record_peer_stores_the_expiry_the_far_side_minted`; `test_dial_peer_refuses_an_expired_row_before_it_opens_a_socket` | R-S2-4, R-S2-5 |
| `test_serve_gateway_peer_lane.py` (extend `:609`) | `test_the_peered_block_carries_the_expiry_when_the_code_had_a_ttl_and_null_otherwise` | additive `peered.expires_at` |
| `tests/hermes_cli/test_gateway_introduce_verb.py` (new; the in-process CLI harness `tests/hermes_cli/test_gateway_pairing_verbs.py` / `test_gateway_peer_verbs.py` already use) | `test_introduce_mints_both_halves_and_prints_one_envelope_whose_grant_payload_is_the_backend_shape` (keys exactly `{peer_join_payload, device_pair_payload, install_id, endpoints, cert_fingerprint, correlation}`; compact ≤ 4096 B); `test_introduce_refuses_when_the_listener_is_off_with_peers_pairs_sentence` (`runtime_unavailable`); `test_introduce_with_only_for_device_mints_the_device_half_only`; `test_introduce_requires_at_least_one_for_flag`; `test_introduce_stamps_the_correlation_on_the_envelope_and_refuses_an_unfit_token`; `test_the_codes_appear_in_the_envelope_and_nowhere_else` (no event, no stderr line carries them); `test_introduce_when_the_second_mint_hits_the_pending_cap_reports_the_half_that_refused` | R-S2-3, R-S2-14 |
| same file | `test_gateway_id_prints_candidate_endpoints_without_loopback_and_the_capabilities`; `test_a_wildcard_bind_enumerates_interfaces_and_a_concrete_host_is_one_row`; `test_no_listener_means_an_empty_endpoint_list_not_an_error` | R-S2-2 |
| `tests/hermes_cli/test_gateway_peers_join_attested.py` (new) | `test_join_with_expect_fingerprint_refuses_a_mismatch_before_any_dial` (a fake `ServeSocketClient` that raises if constructed; reason word `tls_fingerprint_mismatch`; family 2); `test_join_without_the_flag_pins_the_payload_and_says_fingerprint_attested_false`; `test_join_prints_the_correlation_it_was_given` | R-S2-6 |
| `tests/agent_runtime/test_introduce_is_unreachable.py` (new) | `test_no_tool_method_op_or_allowlist_entry_names_introduce`; `test_the_argv_lane_is_refused_to_a_gateway_connection` (re-asserted here by name) | R-S2-18 |
| `tests/agent_runtime/test_cli_contract_dump.py` | existing `--check` green after `--write` | fixture fresh |
| `test_gateway_peer_two_roots_e2e.py` (extend) | `test_introduce_on_b_join_on_a_and_the_device_half_redeems` — B `gateway introduce --for-install <A> --for-device dev-acct-1 --correlation grant-1`; A `peers join <envelope.peer.join_payload> --expect-fingerprint <B's> --correlation grant-1`; A's row `expires_at` ≈ +30 d and equals B's row for A; the device half is redeemed by the existing pairing client path (`ServeSocketClient.pair_hello` as `test_serve_gateway_lane.py` does) against B and lands a row with `account_device_id == "dev-acct-1"`; `peer.ping` A→B still answers; a SECOND install's join with the same code is refused (the scoping, on the wire) | S2 acceptance |

### S2b (one row per row of the parent plan §2.2 parity table, then the rest)

| parity row | test (file) | asserts |
|---|---|---|
| send, fresh thread, `title` | `test_gateway_peer_cross_install_chat_e2e.py::test_a_cross_install_send_with_a_title_opens_a_fresh_far_thread` | B's thread title == the title; a new `session_id` |
| `wait=true` refused | `test_agent_chat_tool.py::test_wait_true_to_an_install_qualified_target_stays_refused_remote_requires_detached` | unchanged word |
| `wait=false` reply delivered | existing e2e `test_a_chat_turn_crosses…` + `test_dispatch_delivery.py` delivery line carries `Their thread:` | unchanged |
| `session_id` continuation | e2e `test_a_cross_install_session_id_continuation_lands_in_the_same_far_thread` | two dispatches, second with the first's `target_session_id`; B's store shows ONE session with two turns |
| `new_session` | e2e `test_new_session_true_opens_a_second_far_thread` | distinct far ids |
| `clarify_token` | `test_agent_chat_tool.py::test_a_far_clarify_token_is_refused_as_not_portable_and_names_the_session_id_route`; e2e `test_a_far_clarify_answered_by_session_id_lands_in_the_questions_thread` | R-IP10 |
| `notify_operator` | `test_agent_chat_dispatch.py::test_notify_operator_rides_a_remote_dispatch_row` | sender side |
| `agent_chat_open`/`threads` on the far thread | e2e `test_agent_chat_open_reads_the_far_thread_the_delivery_named` (through `peer.thread.read`, on the REAL serve — proves §0.10 fact 1); `test_agent_chat_threads_lists_the_far_installs_roster_rows` | shape per R-S2-12 |
| media handles | existing media e2e unchanged | — |
| roster / discovery | e2e `test_agent_chat_installs_lists_paired_installs_and_fetches_one_roster`; `test_runtime_hud.py::test_the_hud_carries_install_lines_and_a_cached_roster_and_dials_nothing` | HUD from store only |
| A→B→A cycle | `test_agent_chat_dispatch.py::test_a_cross_install_dispatch_is_a_fresh_chain_root_on_b_recorded_as_a_known_gap` (asserts the fact, cites the row) | still ✗ by design |
| sender identity | `test_peer_chat_execute.py` existing `--requested-by peer:<id>` | unchanged |

Plus: `tests/agent_runtime/test_peer_directory.py` (new) —
`test_roster_is_scoped_to_the_active_workspace_for_a_bare_target_and_to_the_instances_workspace_for_a_handle`,
`test_roster_rows_carry_exactly_the_projection_fields_and_only_sendable_instances`,
`test_roster_is_capped_and_says_truncated`,
`test_thread_read_refuses_a_non_peer_and_an_unknown_session_with_distinct_codes`
(`peer_identity_required` vs `thread_unreadable`),
`test_thread_read_applies_the_same_lane_guard_as_agent_chat_open` (`foreign_session`),
`test_thread_read_clamps_limit_to_forty`,
`test_thread_read_surfaces_a_chat_scope_refusal_as_thread_unreadable_and_never_sets_the_env`;
`test_peer_authorization.py` — allowlist exactly five (then six), every
`peer.*` name allowlisted, a read-tier device may call `peer.roster.list`
and is refused `peer.thread.read`; `test_runtime_hud.py` —
`test_installs_block_drops_when_no_peer_is_paired`,
`test_a_locally_resident_instance_also_on_a_far_install_carries_the_residency_note_with_its_age`,
`test_hud_snapshot_with_two_installs_one_unreachable` (S2c words),
`test_runtime_hud_field_contract.py` green with the new field;
`test_harness_core_ratchet.py` at 44 / measured tokens;
`test_harness_tool_inventory.py` `--check` green; `tests/agent_runtime/test_agent_chat_remote.py`
(new) — `call_peer_method` returns a typed transport refusal on a dead port
within the dial timeout, a far `error` verbatim, and closes the connection
on every path.

### S2c

| file | test | asserts |
|---|---|---|
| `test_gateway_peers_store.py` (extend) | `test_the_cache_row_shape_is_exactly_the_cache_fields`; `test_no_cache_writer_can_change_a_trust_field` (byte-equal `peers.json` after every cache writer runs, incl. `apply_peer_announce` with hostile payloads); `test_an_announce_cannot_un_revoke_or_rename_the_trust_row`; `test_an_announce_may_write_only_the_callers_own_row`; `test_revoked_you_is_one_way_until_a_trust_write`; `test_a_fingerprint_rotation_is_recorded_and_never_applied_to_the_pin`; `test_dial_order_is_cache_endpoints_then_trust_and_the_pin_is_always_trust`; `test_usable_peers_excludes_revoked_expired_and_revoked_you`; `test_ref_is_the_name_when_unique_else_the_id` | R-S2-7, R-S2-9, R-S2-16 |
| same | `test_every_store_door_emits_its_event_with_ids_and_never_a_secret_endpoint_or_roster_body` (payload keys ⊆ contract fields; `secret_verifier`, codes, hosts absent; < 4096 B); `test_a_process_that_reads_a_revision_it_neither_wrote_nor_seeded_emits_external_write_once`; `test_a_fresh_process_seeds_on_first_read_and_emits_nothing` | R-S2-8 |
| `test_s15_event_contract_pruning.py` | count 64; `test_s55_registered_events_have_emitters.py` green (constants idiom) | registry |
| `test_stream_contract_fixture.py` | green after regeneration; README CROSS-STACK block written | goldens |
| `test_peer_authorization.py`, `test_peer_chat_execute.py` | allowlist exactly `{peer.ping, peer.agent_chat.execute, peer.media.get, peer.roster.list, peer.thread.read, peer.announce}` | R-IP9 |
| `tests/agent_runtime/test_peer_announce.py` (new) | `test_announce_refuses_a_non_peer`; `test_announce_refuses_a_payload_naming_another_install_and_ignores_its_own`; `test_announce_writes_cache_only_and_reports_the_fields_it_wrote`; `test_revoked_you_makes_the_next_send_refuse_deterministically_before_any_dial` (`resolve_install_target` → `peer_revoked_you`; a `ServeSocketClient` that raises if constructed) | R-S2-9 |
| `tests/agent_runtime/test_gateway_announce.py` (new) | `test_outbound_announce_is_best_effort_one_retry_and_records_the_receipt`; `test_revoke_announces_revoked_you_before_the_local_trust_write` (order pinned with a recording fake); `test_rename_and_agent_create_announce_and_a_slow_peer_never_delays_them` | R-S2-15 |
| `test_gateway_targets.py` (extend) | `test_an_expired_row_refuses_with_peer_expired_and_a_revoked_you_row_with_its_own_reason`; `test_the_hint_lists_usable_refs_only`; `test_the_predicate_is_byte_identical_between_the_resolver_and_the_tool` | R-S2-16 |
| `test_gateway_peer_cross_install_chat_e2e.py` (extend) | `test_a_cli_join_beside_a_running_serve_is_visible_on_the_next_read_with_no_restart` (A's serve running; `peers join` from a CLI process; A's `agent_chat_installs` / `_connections_frame` shows B without restarting A; the `gateway.peer.recorded` event is on A's EventLog tail); `test_a_revoke_on_b_reaches_a_as_revoked_you_and_as_next_send_refused` | R-IP12 |

The seeding detail (§0.10 fact 2): the S2b e2e needs one addressable instance
on B. Seed it the way the ratchet test seeds personas
(`tests/agent_runtime/test_harness_core_ratchet.py`'s `bundled_persona_profiles`
/ the `agent_runtime.config.ensure_persisted_personas` shape) by writing the
persona block into B's sandbox `config.yaml` in a `two_installs_with_persona`
fixture; the builder confirms the exact YAML key against
`agent_runtime/config.py` and records it in the field notes — the ONE detail
this plan leaves derived.

---

## 4. Acceptance — what the orchestrator runs (worktree root; never bare pytest; the runner finds `~/.venvs/hermes-test`)

```
# S2
bash scripts/run_tests.sh tests/agent_runtime/test_gateway_capabilities.py tests/agent_runtime/test_serve_gateway_lane.py tests/agent_runtime/test_serve_gateway_auth.py tests/agent_runtime/test_gateway_peers_store.py tests/agent_runtime/test_serve_gateway_peer_lane.py tests/agent_runtime/test_introduce_is_unreachable.py tests/hermes_cli/test_gateway_introduce_verb.py tests/hermes_cli/test_gateway_peers_join_attested.py tests/agent_runtime/test_cli_contract_dump.py
python scripts/dump_cli_contract.py --check
# S2b
bash scripts/run_tests.sh tests/agent_runtime/test_peer_directory.py tests/agent_runtime/test_peer_authorization.py tests/agent_runtime/test_peer_chat_execute.py tests/agent_runtime/test_agent_chat_tool.py tests/agent_runtime/test_agent_chat_dispatch.py tests/agent_runtime/test_agent_chat_remote.py tests/agent_runtime/test_runtime_hud.py tests/agent_runtime/test_runtime_hud_field_contract.py tests/agent_runtime/test_harness_core_ratchet.py tests/agent_runtime/test_harness_tool_inventory.py tests/agent_runtime/test_prompt_observability.py
python scripts/emit_harness_tool_inventory.py --check
# S2c
bash scripts/run_tests.sh tests/agent_runtime/test_gateway_peers_store.py tests/agent_runtime/test_peer_announce.py tests/agent_runtime/test_gateway_announce.py tests/agent_runtime/test_gateway_targets.py tests/agent_runtime/test_s15_event_contract_pruning.py tests/agent_runtime/test_s55_registered_events_have_emitters.py tests/agent_runtime/test_stream_contract_fixture.py tests/agent_runtime/test_s32_decision_contract_parity_retirement.py tests/hermes_cli/test_gateway_peer_verbs.py
# the live two-roots lane (real serve children; ~2-4 min)
bash scripts/run_tests.sh tests/agent_runtime/test_gateway_peer_two_roots_e2e.py tests/agent_runtime/test_gateway_peer_cross_install_chat_e2e.py tests/agent_runtime/test_gateway_peer_cross_install_media_e2e.py
# docs
python scripts/doc_cite_adjacency.py --exclude archive --exclude planned
# read-only live sanity (does not write): the new row fields on this box
HERMES_HOME=X:/Eternia/.hermes python -m hermes_cli.main harness gateway id --json      # endpoints, endpoints_source, capabilities present
HERMES_HOME=X:/Eternia/.hermes python -m hermes_cli.main harness gateway peers list --json   # items: [] still; usable/cache columns in the schema
```

Regeneration steps, in this order, each its own commit: `python
scripts/dump_cli_contract.py --write` (S2); `python
scripts/emit_harness_tool_inventory.py --write` (S2b); `python
scripts/generate_agent_runtime_stream_fixtures.py` (S2c). Acceptance is: every
command above green; the S2b parity table has one green test per row; the
two-roots e2e shows `introduce` → `join --expect-fingerprint` → device half →
`agent_chat_installs` → `peer.roster.list` → far `agent_chat_open` → CLI join
visible with no restart → revoke heard as `revoked_you` before the refusal.

---

## 5. Risks, rollback, and what is OWED at landing

* **Additive only, by construction.** New frame keys (`gateway.capabilities`,
  `peered.expires_at`), new row fields (`expires_at`, `account_device_id`),
  new file (`peers_cache.json`), new verbs/flags/methods/tool/events. A
  launcher that predates S3 reads the `gateway` block by key and sees no
  refusal; a hermes that predates S2 answers `peer.roster.list` with
  `-32601`/`scope_denied`, which the S2b tool surfaces as
  `capability_missing` (R-IP17's word) — `call_peer_method` maps a far
  method-not-found to it. Rollback per stage = revert the stage's commits;
  the stores stay readable by the previous build (`_decode_*` ignore unknown
  keys; a legacy row without `expires_at` never expires).
* **Named test moves** (each a regen in the same commit, never a baseline):
  `test_serve_gateway_lane.py:239,338` (key sets), `test_gateway_peers_store.py:124,154`
  (partition), `test_peer_authorization.py:139-160,371` and
  `test_peer_chat_execute.py:79` (allowlist), `test_runtime_hud_field_contract.py:77`
  (field), `test_harness_core_ratchet.py:42-43` (44 / measured),
  `test_s15_event_contract_pruning.py:240` (64), `test_stream_contract_fixture.py`
  (hash), `test_cli_contract_dump.py` (fixture), `test_harness_tool_inventory.py`
  (three artifacts).
* **OWED cross-repo, named for the landing commit:** (1) launcher byte
  mirror of `tests/fixtures/hermes_cli_contract.json` →
  `test/features/mission_control/fixtures/hermes_cli_contract.json`; (2)
  launcher byte mirror of every regenerated stream golden + `MANIFEST.sha256`
  → `test/fixtures/harness_stream/` (the `decision_contract_hash` moved on
  all of them); (3) the Agent Command Atlas artifact regenerated from
  `references/tool-inventory.json`. Each is recorded in the hermes README /
  field notes as OPEN until the launcher landing closes it.
* **The in-serve transcript read** (§0.10) is the one behaviour with no
  precedent. Fails closed: a scope refusal is `thread_unreadable` with the
  reader's word, never an empty page; the e2e proves the happy path on a
  real serve child. If the serve's head resolution cannot answer for
  persona-chat sessions, the fallback is spelled in the field notes, not
  invented in code: run the read through the worker lane as the argv
  `harness persona chat history --session-id … --json` (the launcher's own
  read path) — same door, one process boundary more.
* **Announce fan-out cost.** Bounded by peer count × 2 attempts × 5 s, in a
  thread, once per trigger; nothing loops. A peer that is off simply rests
  `unreachable` in the cache until its next hello.
* **Codes at rest.** `introduce` prints plaintext codes to stdout for the
  launcher to post; nothing else ever holds them (no event, no row, no
  stderr). The 600 s code TTL and the single-use match are unchanged.
* **Canon docs to update in the landing (where):**
  `docs/agent-runtime-harness/03-transport-and-wire.md` §1.1 (the `gateway`
  block gains `capabilities`; endpoints on `gateway id`), §1.2 (the allowlist
  sentence at `:182-183` and `:338` → six names; the verbs list at `:195` →
  `pair | join | list | revoke | introduce`; the trust/cache split and the
  sidecar), §1.3 (what an edge carries: roster, thread, announce; the
  `revoked_you` ordering; the far `agent_chat_open`), §2's method table
  (`:280-296`: sixteen → nineteen rows) and §2's peer paragraph, Invariants
  (a peer method writes TRUST never; announce writes the caller's cache row
  only); `05-chat-turn-lane.md` §8 "Session scope and continuity" gains the
  cross-install continuity paragraph (`session_id` crosses, `clarify_token`
  does not, the far read); `01-system-architecture.md` "The seam" gains the
  residency-note sentence (R-IP11); `08-performance-and-debt-ledger.md`
  debt register gains the row "A→B→A cross-install cycle guard — still open,
  by design (parent plan §4)"; `harness-runtime-model/SKILL.md` Operate rows
  (R-S2-17). `scripts/doc_cite_adjacency.py --exclude archive --exclude
  planned` must stay green after the cites above are re-anchored.
* **Not in this lane** (the parent plan's non-goals, restated so nobody
  reaches for them here): no relay through the backend; no write on the peer
  allowlist; no cross-install cycle guard; no `home_install` routing (R-IP11
  v2); no launcher change beyond the OWED mirrors.
