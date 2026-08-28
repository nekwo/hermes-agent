# Planned — Remote Gateway (hermes half)

**Status:** surveyed + staged 2026-08-24, **GATED on the operator rulings R1–R13 in the
primary plan — no stage builds before its ruling.** 2026-08-27: R1 RULED (encrypt;
self-signed pinned baseline + launcher-reuse survey) and the authorization
chokepoint RULED (front door; tier = account auth) — see
[authorization-chokepoint.md](authorization-chokepoint.md) § the three rulings;
Stage 1's two prerequisites are now decisions, and its remaining gates are R3/R11's
vocabulary details. **Evening 2026-08-27: R3 RULED (QR + typed-code fallback;
CLI-first pairing surface), and R2/R4–R10 ADOPTED at their recommended options
under the operator's "implement it all" directive (overridable until the
consuming stage lands) — the primary plan's §5 carries the status block. Stage 1
is fully unblocked.**
**Primary plan (full architecture, stages, rulings):** EterniaLauncher repo,
`docs/mission_control/planned/universal-remote-gateway.md`. This pointer carries only
the hermes-owned half so the harness canon names its own work.

**Operator rulings already binding:** LAN-direct only (no Eternia backend broker in any
buildable stage — the broker is a future full-console connector appendix); the gateway
is a connector abstraction (full-console vs chat-bridge tiers); multi-install device
switching and cross-install `agent_chat_send` are first-class requirements.

**The load-bearing find:** the serve socket lane (`agent_runtime/serve_socket.py`,
`SOCKET_HOST = "127.0.0.1"` :193) is a nearly-complete gateway server with no client —
hardened HMAC challenge-response (token never travels, `serve_auth.py`), versioned
hello + capability manifests, JSON-RPC lane (`serve_rpc.py`), hydrate/patch stream with
fold negotiation, registry advertising the live port (`serve_registry.py`). The gateway
contract is NOT to be invented: it is this lane's existing contract made bindable
beyond loopback with a device-credential tier. The chat-bridge tier and the pairing
ceremony both have upstream reuse material in `gateway/` (`platform_registry.py`,
`pairing.py` — 8-char short-TTL codes, never logged).

## hermes-owned stages (numbering matches the primary plan)

- **Stage 0 — install identity: SHIPPED 2026-08-27** (hermes `b5bc9761a3`,
  launcher `7444119ce`). `agent_runtime/gateway_identity.py` load-or-mints
  `<store_root>/gateway/install.json` = `{install_id, display_name, created_at}`;
  the additive `install` block rides `ready`/`hello_ok`/`version`;
  `gateway.listen` (False) / `gateway.port` (0) are declared in
  `config_defaults.py` and **read by nothing** — no network behaviour changed and
  `serve_socket.SOCKET_HOST` is still `127.0.0.1`. Neither contract integer
  moved. **Stage 0b (the CLI half) followed the same day** — hermes
  `29ba464d9c`, launcher `bf107a882`: `harness gateway id` and `harness gateway
  rename <name>`, so Stage 0 is complete. See the Stage 0 notes below.
- **Stage 1 — device pairing + LAN bind: SHIPPED 2026-08-27.** hermes
  `add7edd584` (S1a, the device credential store), `9e266d7871` (S1b, R1's
  certificate), `cc6ece232d` (A5, the tier becomes a refusal), `b37cb8331f`
  (S1c, the second listener + the config-key defect), `3f0c29592d` (S1d, the
  pairing ceremony's second half), `28ec9e3180` (S1e, the operator verbs),
  `afd0667df7` (the handshake prose truth-up); launcher `8891351ed` (fixtures
  only). Canon: [03 §1.1](../03-transport-and-wire.md). Full notes, deviations
  and honest gaps in "Stage 1 notes" below.
- **Stage 3 (shared) — the remote write path: SHIPPED 2026-08-27.** hermes
  `3debed5977` (the method lane + the accept receipt), `296a983340` (20 tests +
  four manifest-literal pins), `704151cb22` (field notes), `cf69a0d842` (a claim
  the launcher acceptance disproved); launcher `2632d9ce0`, `9e9bd7aa8`,
  `c01b665a5`. Two new methods — `runtime.chat.message` / `runtime.chat.steer`,
  both `console` — plus `agent_runtime/chat_turn.py` and
  `agent_runtime/chat_turn_reservations.py`. Full notes in "Stage 3 notes" below.
  **The row above was WRONG and is kept here corrected rather than deleted:**
  mission-chat send has had server-side exactly-once since the 2026-08-24
  incident, under the name `client_message_id` plus the per-session turn journal.
  The grep for `turn_request_id` was accurate twice and answered the wrong
  question both times.
- **Stage 6 — peer pairing (install⇄install): SHIPPED 2026-08-27.** hermes
  `dd8a8ad716` (S6a, the peer store + the pairing-code discipline both
  ceremonies now share), `77768eea27` (37 store tests), `6775911bbc` (S6b, the
  `peer` caller kind, `PEER_METHOD_ALLOWLIST`, `peer.ping`, five manifest
  pins), `db6bbdc899` (S6c, the peer hello), `5439595880` (S6d, the four
  operator verbs), `c246b648ba` (S6e, the two-roots acceptance). Canon:
  [03 §1.2](../03-transport-and-wire.md). `gateway/peers.json` on both sides, a
  distinct peer hello (`peer_install_id` / `peer_code`, proof prefix `pwv`), R5
  honoured by a ceremony neither install can complete alone, `harness gateway
  peers pair | join | list | revoke`, and `peer.ping` as the whole peer
  surface. Full notes, deviations and honest gaps in "Stage 6 notes" below.
- **Stage 7 — cross-install `agent_chat_send`: SHIPPED 2026-08-27.** hermes
  `3d0a17922d` (S7a, `agent_runtime/gateway_targets.py` — the `@install/target`
  grammar, 24 tests), `8e2a97bf29` (S7b, `peer.agent_chat.execute`, the
  allowlist's second name, ten manifest pins in the same commit), `8e1dd74c91`
  (S7c, the supervisor's remote leg + `remote_install_id` + R8's convergence),
  `a8b7c6fe90` (S7d, the two-roots acceptance); launcher `614edc546` (fixtures
  only). R4 and R8 both consumed and cited in the primary plan's §5. The
  dispatch row stays on the SENDER install and the remote leg substitutes for
  the CHILD PROCESS, not for the delivery forge; the target install records the
  turn in its own chat store. Full notes, deviations and honest gaps in
  "Stage 7 notes" below.
- **Stage 8 — media fetch:** content-addressed `runtime.media.get` (size-bounded,
  scope-checked) so remote clients stop needing install-local paths.

**Stale-marked by this plan (launcher/backend side):** the W2 "Agent Gateway" backend
relay (2026-07-16/19, `gateway_state/v1` desktop→Django fan-out, no phone consumer ever
built) — retirement is its own small plan. `mobile_core/` is orthogonal (on-device
provider runtime, no agent loop by contract) and NOT superseded.

## Stage 0 notes — landed 2026-08-27

### The install-id inventory decision: DISTINCT

The drift addendum below ordered an inventory before minting a third
`install_id`. It was done, both existing mechanisms were read, and the verdict is
**distinct** — the argument lives in `agent_runtime/gateway_identity.py`'s
module docstring (the file that would be deleted if the verdict were ever
reversed), and is summarised here so
[duplicate-implementation-retirement.md](duplicate-implementation-retirement.md)
reads a decision rather than an accident:

| | scope | lifetime | audience |
|---|---|---|---|
| `monitoring.install_id` (`agent/monitoring/policy.py`) | a HERMES **home**'s `config.yaml` | **rotatable by design** — clearing the key mints a new one next start | OTel `service.instance.id`; "carries no account identity" |
| telemetry `install_id` (`hermes_cli/observability/shared_metrics.py`) | the shared-metrics sqlite `telemetry_state` | per metrics db | anonymous counter aggregation, never shown |
| **`gateway/install.json`** (new) | a **store root** | **never rotates** | an operator, in a picker, with a name they chose |

Three independent disqualifiers, any one of them sufficient. **Scope:** a gateway
addresses a store root, and homes and roots are provably not the same scope on
this machine — the launcher's serve spawns with `HERMES_HOME=profiles/base`
against the shared `agent-runtime` root, so one monitoring id would span several
roots while several roots shared one id. **Lifetime:** rotatability is the
feature there and a lockout here — Stage 1 pairs a device *against* this id, and
`serve_auth.py` already states the rule ("rotating it under them is a lockout,
not a hardening"). **Audience:** a telemetry id put on a wire frame stops being
an anonymity primitive and becomes a network address.

What is deliberately NOT duplicated is the *mechanism*: mint-iff-absent,
root-is-an-input, never-raises, typed `state` instead of an exception — all of it
is `serve_auth.py`'s contract restated for a non-secret, and the docstring says
so rather than re-deriving it.

### Two deviations from the primary plan's §3.3 `install` shape

The plan specified `{install_id, display_name, build}`. What shipped is
`{install_id, display_name, state}`:

- **`build` dropped.** All three frames already carry a top-level `build` block.
  A nested second copy is a second authority that can disagree with the first —
  the shape the build stamp itself exists to retire.
- **`state` added** (`loaded` | `minted` | `error:<reason>`). Absence cannot
  distinguish "this runtime predates the lane" from "this runtime could not write
  its identity", and the greeting's standing rule is that a block states its own
  outcome rather than vanishing (`auth.token_file`, `socket.outcome`). The block
  is therefore always present once the runtime has the lane.

The block **names, and never authorises.** `serve_auth` (today) and the device /
peer tiers (Stage 1/6) are what prove a caller may talk to this runtime; an id
that did both is how "I know your install id" becomes "I am you". Pinned by
`test_the_install_block_carries_nothing_secret`.

### How the byte-pinned capture stayed deterministic

A freshly minted uuid4 in `ready` would have made the launcher's
`test/fixtures/hermes_serve_frames/` captures unreproducible across regens, on
the CI lane that closed days earlier. Fixed by **seeding**, not scrubbing: the
generator writes a fixed `gateway/install.json` into each sandbox root before
boot (`tool/hermes_serve_frames/generate.py`, `Sandbox.seed_gateway_identity`),
so hermes takes its **load** path — the path a real install takes on every boot
after its first — and the committed bytes pin the real field values instead of
two sentinels. It is the same argument `Sandbox.make_storelike` already makes
about the store marker dirs. The mint path is covered hermes-side in
`tests/agent_runtime/test_gateway_identity.py`, where it belongs.

Receipt: `generate.py --check` went red on exactly one frame (`ready.json`) with
the change in place and green twice consecutively after the refresh.

### Stage 0b — the CLI verbs: SHIPPED 2026-08-27

~~`harness gateway id` / `--set-name` did **not** land with Stage 0a.~~ Landed
hermes `29ba464d9c`, launcher `bf107a882`. It had been deferred for SCOPE,
not entanglement: the verbs change hermes' argparse tree, which the launcher
pins in `test/features/mission_control/fixtures/hermes_cli_contract.json` — a
second cross-repo fixture landing stacked on the serve-frame refresh Stage 0a
already paid for.

**What shipped is two subverbs, where this note wrote one verb and a flag:**
`harness gateway id` (read) and `harness gateway rename <name>` (write). The
reason is mechanical rather than taste. `_add_stage42_global_args` is where every
stage42 verb gets its flags, and the writer's set and the reader's set differ — a
mutation opts into `--dry-run` (`roots set`, `workspace rename`, 29 call sites), a
read does not — so one parser cannot carry both truthfully, and that helper's own
docstring is built on the rule that an advertised flag which does nothing is "a
WRONG ANSWER believed, not an error seen". `rename` rather than `set-name`
because `workspace rename` is the house word for this operation and
`set_display_name`'s own first line is "Rename this install".

**No authorization gate, written down rather than left absent.** The A4 mirror
(`persona_commands._console_denial`) exists so two doors onto ONE service
function cannot answer differently. Stage 0b adds no RPC method, so there is one
door and nothing to disagree with; `CLI_CONSOLE` here would gate against a
predicate that allows every caller that exists, with no wire twin to keep it
honest, and the record is neither a level nor a secret. When a paired DEVICE may
rename an install, the door is a `gateway.*` method with a tier declaration
(Stage 1 / A5) and the gate goes there — where the caller is something the
transport proved rather than the machine owner's own shell. The argument sits in
the handler block in `hermes_cli/harness.py`, on A4's own reasoning that a
grandfather clause should be greppable.

**The read never mints.** `gateway id` routes at `read_install_identity`, never
`ensure_install_identity`, and its test probes the FILESYSTEM rather than the
ack — the kill-mutation returns a perfectly plausible exit-0 ack having created
an identity on a root the operator only asked about. Stage 4's install picker
runs this against roots it does not own.

**Typed states became exit families**, and the typed reason travels verbatim in
the message so a greeting frame's `install` block and the verb read as one
spelling: `error:absent` → 3 (`not_found`), `malformed_record` /
`record_without_id` → 1 (`store_corrupt`, and deliberately never a re-mint — per
`_decode`'s documented asymmetry those bytes may hold the id a paired device
names), `empty_display_name` → 2, every other I/O reason → 7. Both handlers stamp
the root-observability block: the identity is per store root, so a `gateway id`
against the wrong root returns a well-formed identity for a runtime the operator
did not mean.

**One Stage 0a wart, found by shipping the verb and fixed.**
`set_display_name` returned a constant `loaded`, so a rename against a fresh root
— which MINTS by its own documented contract — reported the opposite of what it
did, on the module whose whole contract is "state it, never infer it from
absence". It now propagates the load-or-mint outcome it already held. Nothing
pinned the old constant; a new test pins both directions.
`gateway_identity.clean_display_name` also became public, because `--dry-run` has
to print the string that WOULD land and the only way to get it without a second
copy of the rule is to ask the rule.

**Receipts.** `C:\Python312\python.exe -m pytest` (the venv still has no pytest):
`tests/hermes_cli/test_gateway_verbs.py` + `tests/agent_runtime/test_gateway_identity.py`
30 passed; the neighbouring CLI suites (root-observability gate, harness CLI,
argparse flag propagation, completion, agent create/retire verbs) 113 passed / 1
skipped. Launcher: `dump_hermes_cli_contract.dart` regenerated (+214 lines,
**zero removals** — a new verb, no button lost), `--check` green after;
`harness_capability_argv_test.dart` + `harness_argv_template_test.dart` 268
passed, no oracle vector moved (no launcher capability lowers to these verbs
yet). `tool/hermes_serve_frames/generate.py --check` green — a CLI verb touches
no greeting frame, proven rather than assumed.

**Honest gap, not caused here:**
`tests/agent_runtime/test_serve_stream_lane_parity.py::test_the_advertisement_grew_and_no_contract_integer_moved`
is RED at HEAD and was red before this stage (confirmed on a stashed tree). A1
grew `serve_rpc.manifest()` by a `tiers` key and this parity pin still asserts
`{"contract", "methods"}`. It belongs to the chokepoint wave, not to Stage 0b.


## Stage 1 notes — landed 2026-08-27

### What shipped, in the order it landed

| sha | what |
|---|---|
| `add7edd584` | `agent_runtime/serve_gateway_auth.py` — per-device tokens (stored as `sha256`), pairing codes with `gateway/pairing.py`'s discipline, revocation. 29 tests. |
| `9e266d7871` | `agent_runtime/gateway_tls.py` — the self-signed per-install EC P-256 certificate R1 ruled for, and the fingerprint a client pins. 10 passed / 1 skipped. |
| `cc6ece232d` | A5 — `RpcCaller` grows a `device` kind carrying a tier; `authorize_call` refuses on it. 43 + 100 passed. |
| `b37cb8331f` | The second listener: three default-off seams on `ServeSocketServer`, the lane in `serve_loop`, the `gateway` greeting block, the argv/`drain` refusals. 17 + 16 + 2 passed, loopback 59 unchanged. |
| `3f0c29592d` | The pairing ceremony's second half — a `pairing_code` hello redeems and is admitted, token riding one `hello_ok`. 23 passed. |
| `28ec9e3180` | `harness gateway pair` / `devices list` / `devices revoke`. 15 passed. |
| `afd0667df7` | Prose: the handshake was documented as contract 2 over `msg=<nonce>` in three places. |

### The finding that mattered most: Stage 0a's config keys never existed

Stage 0a's receipts say `gateway.listen` / `gateway.port` were "declared, read by
nothing". The first half was false, and the second half is exactly what hid it.
`"gateway"` is ALREADY a top-level key in `config_defaults.py`'s single
~3000-line dict literal (the messaging gateway's), and a duplicate key in a
Python dict literal does not merge and does not warn — the later entry wins and
the earlier one is discarded at parse time. So the keys were not merely unread;
they were not there. **A key nobody reads and a key that is not there are
indistinguishable from every angle except a reader's**, which is why becoming
the first reader is the only thing that could have found it.

They are `remote_gateway.*` now. That is also the honest spelling rather than a
workaround: Stage 0a's own comment said the word `gateway` is overloaded in this
codebase and that the two lanes must not be conflated — and two lanes cannot
share one key and stay unconflated. `tests/hermes_cli/test_config_defaults_no_duplicate_keys.py`
walks the module's AST, because the loaded dict cannot show the defect (by then
the evidence is gone), and it was checked non-vacuous against the exact shape
that shipped.

### Deviations from the plan, and the argument for each

- **`listen` is a HOST STRING, and boolean `true` is refused.** §4 wrote
  "bound per `gateway.listen` (e.g. `0.0.0.0` or an explicit interface)", which
  is what shipped — but the refusal of `true` is an addition. An operator
  opening a port onto a LAN should have to say which interface; "guessed one for
  you" is not a sentence a runtime that executes agents with tools should be
  able to say about a listener.
- **The pairing ceremony's SECOND half was built, and the brief scoped the
  listener to "ONLY device-tier hellos".** A hello naming `pairing_code` is a
  second, narrower hello. The argument for exceeding the letter: without it
  `redeem_pairing_code` has no caller but a test, `harness gateway pair`'s output
  is decoration, and Stage 1 ships a device tier no device can ever enter — a
  bigger defect than the extra surface, and one that would have to be fixed
  before any Stage 5 phone could be built.
- **The argv lane is REFUSED to devices rather than gated.** Not in the brief,
  and load-bearing: `authorize_call` gates the method lane, and
  `{"argv": ["harness", …]}` reaches the CLI dispatcher where no tier
  declaration exists — so a `read` device refused `runtime.agent.retire` on the
  method lane could send the same verb as argv and be obeyed. Gating argv
  instead would mean deciding a tier for every CLI verb in this repo and keeping
  that map correct forever, which is the duplicated-authority shape this stack
  keeps retiring.
- **`drain` is refused to a device even at `console` tier**, because `drain` is
  not a level mutation the tier speaks about: it ends the process and
  disconnects every other attached client.
- **Two exit codes were added** to `harness_support.ERROR_EXIT_CODES`
  (`pairing_codes_pending`, `pairing_locked_out`, both family 6), documented in
  the table the way `duplicate_desk` and `cancel_unsupported` document theirs.
- **The R1 survey's bullet 3 was honoured, not overridden.** It argues TLS and
  AUTHENTICATION are separate lifts and that mutual proof-of-key should ship
  first. What shipped keeps them separate — a device is authenticated by the HMAC
  proof and TLS is confidentiality only, so a peer that completes a TLS
  handshake has proven nothing — while still shipping both, because R1 was
  ruled `encrypt` and the certificate turned out to cost one module with
  `cryptography` already pinned in `pyproject.toml` (48.0.1, no dependency work
  at all). The survey's bullet 4 (one key, two encodings) is the upgrade path if
  the ES256 device key ever becomes the TLS identity; nothing here forecloses it.

### The honest limit of hashing the device token

`devices.json` stores `sha256(token)` and the proof is an HMAC keyed by that
digest, so **the stored verifier is HMAC-key-equivalent**: anyone who can read
the file can impersonate every device in it, exactly as anyone who can read
`serve_auth_token` can impersonate the machine owner. Digesting buys one real
thing and not two — the bytes a phone holds are not the bytes on this disk, so a
store read cannot recover an ISSUED credential. Store-read resistance needs an
asymmetric scheme (the R1 survey's bullet 2), and that changes only
`device_proof` and one module, never the wire, which says "proof" and nothing
about how it was computed. Written into `serve_gateway_auth.py`'s docstring
rather than left implied.

`serve_auth.py` recorded the Windows-ACL gap and declined to fix it, on the
grounds that the real control belongs with the transport slice that introduces
the exposure. **This is that slice**, so an `icacls` narrowing is attempted on
the device store and the private key, and its outcome is returned rather than
assumed — a permission posture this runtime cannot enforce is still never
claimed as enforced.

### Honest gaps

1. **No second machine.** Every integration test binds loopback. That is a
   config VALUE and not different code — the same `bind()` on the same class
   with a host string the operator chose — but "a phone on the LAN reached this
   install" is unproven, and Stage 5 is where it gets proven.
2. **No phone, and no QR was ever scanned.** `harness gateway pair` emits the
   payload R3 specifies and a test asserts the scanned bytes equal the printed
   fields; whether a phone camera reads them is untested by construction.
3. **Windows firewall behaviour is undriven.** The prompt, its Private/Public
   default, and what an operator sees when they dismiss it are documented in
   canon 03 §1.1 from knowledge of the platform, not from an observation made
   here. Nobody bound a non-loopback interface on this machine during this work.
4. **No live serve was driven by hand.** Every proof is a test — though the
   socket ones run the real `serve_loop`, over real sockets, with a real TLS
   handshake, a real certificate pin and a real HMAC.
5. **`icacls` narrowing is reported but not asserted.** No test checks that the
   resulting DACL is what was asked for; the function returns its outcome and
   nothing reads it yet.
6. **The primary plan (launcher `docs/mission_control/planned/universal-remote-gateway.md`)
   still describes Stage 1 as unbuilt.** Its §4 block and §5's R1/R3/R11 records
   are owed a receipt. Not written here: a sibling session was landing Stage 2
   receipts into that same file, and this lane's launcher writes were scoped to
   fixture regenerations.
7. **The device store's cross-process lock falls THROUGH on a filesystem that
   will not lock**, rather than refusing. The writes are atomic-replace either
   way, so the loss is a lost update and not a corrupt store — but a pairing
   minted during that window can be dropped.

## Stage 3 notes — landed 2026-08-27

### The finding: the stage's premise was false

This plan and the primary plan both recorded that mission-chat send has no
server-side dedupe, "no `turn_request_id` anywhere, re-verified 2026-08-27". The
grep was right and the conclusion was wrong. Mission chat has carried
exactly-once for months under a different name — **`client_message_id`** plus the
per-session **turn journal** (`persona_commands._mission_chat_busy_outcome`,
`mission_chat_turn_record`) — and it answers a repeated presentation with
`idempotent_replay: True` and the committed reply, `chat_turn_duplicate_in_flight`
while the turn is still running, and `chat_turn_outcome_unknown` when the provider
outcome cannot be proven. That machinery came out of the 2026-08-24 incident and
is richer than anything this stage would have re-derived.

**A grep for the NAME a plan chose is not a survey of the CAPABILITY.** The
re-verification ran twice, months apart, and both times asked "is the word here"
rather than "does a second send of one message run twice". The second question
takes one test and cannot be answered wrong.

So `turn_request_id` is **not a second key**: the RPC door passes the bytes to
`--client-message-id` unchanged, and `chat_turn_reservations.py` covers only the
ACCEPT window the journal provably cannot — between the accept and the journal's
first write (which happens inside the chat-root lease, after a worker is already
running) a duplicate accept would otherwise spawn a second worker. That worker is
not a second TURN (it loses the lease), so correctness was never at risk; what was
missing is the `idempotent_replay` marker the remote outbox has to branch on.

### The design collision, and why it was invisible per-stage

Stage 3's sketch said "RPC where methods exist, op/argv lane otherwise — same
union". `mission.chat.*` has no methods and lowers to argv; Stage 1 refuses the
argv lane to devices, correctly, because the tier gate lives on the method lane
and an argv lane open to devices makes every tier declaration bypassable in one
frame. So the union's fallback arm was closed for exactly the caller the gateway
exists for, and the gateway had shipped a device that could place an agent and
could not talk to it. Neither stage is wrong on its own — the hole is in the
SEAM, which a per-stage review cannot find and a cross-stage read can.

### The door lands one step LOWER than the precedent, and that is stronger

`runtime.agent.retire`'s door calls `perform_agent_retire`, the same function the
CLI calls. Mission chat has no such function: its service IS
`_cmd_mission_chat_message`, an argparse handler, and its one existing second
door (`dispatch_delivery.deliver_via_mission_chat`) reaches it by BUILDING a
namespace. So this door builds ARGV, which the worker dispatches through the same
argparse tree a local send uses. A parallel Python call site would be two
implementations that agree today; this is one execution — same handler, same
lease, same journal, same frames — and no future edit to the chat handler can
move one without moving the other. The hazard that makes argv-building
acceptable is pinned rather than asserted
(`test_a_client_cannot_smuggle_a_flag_through_a_value`): the argv is a LIST,
flags are literals, and a value is always the element after its flag.

### The tier is `console`, and the decisive reason is mechanical

The taste argument is right — a chat turn runs an agent with tools, so anything
below `console` is a door around `console`, and a `read` device refused
`runtime.agent.retire` could ask an agent to retire one. But the fact that
settles it is that `call_authorization.authorize_call`'s device arm is an
**equality** against the stored word, not an ordering. A new `chat` tier would
have refused every already-paired `console` device the very thing R11 says it may
do, on the first frame. Declaring chat at `console` satisfies R11 exactly, keeps
the vocabulary at two words and changes no predicate. **Any future third tier has
to make that arm an ordering first; that is a decision, not a constant.**

### The ack is an ACCEPT, and the lane forces it

`serve.py` answers the method lane INLINE on the reader loop — its own comment
names chat turns as the reason the worker pool exists — so a method that ran a
turn would stall every other client on this serve for its length. The chat
methods therefore validate, dedupe, hand the turn to the pool through the new
`RpcContext.spawn_chat_turn` seam, and return
`{turn_request_id, request_id, accepted, state, idempotent_replay, settled,
exit_code?}`. The turn's frames ride the EXISTING per-request frame lane under
`request_id`; no second streaming transport was invented.

The spawn builds the same `_ArgvRequest` an argv send does, so `is_chat_turn`
comes off the same `_CHAT_TURN_COMMANDS` shapes and **`held_by_chat_turns` counts
a remote turn exactly as it counts a local one**. A serve recycling mid-turn
because the turn arrived on the other lane is the exact defect that ledger exists
to prevent, and it is the load-bearing test of the stage.

A draining serve REFUSES a new chat turn, which is an addition to the method
lane's own rule. That lane deliberately keeps answering during a drain, on the
argument that an inline handler "cannot be cut off half-done" — and a chat turn is
the counter-example that argument itself names. The seam RAISES to decline rather
than being absent, because "this transport has no worker lane" and "this transport
is shutting down" are different facts and a client retries only one of them. The
refusal is counted on the terminal drain frame exactly as an argv refusal is, and
the accept receipt is REMOVED, so the retry against the replacement runtime is a
fresh accept rather than a replay of a turn that never ran.

### Two things a test found that neither draft's comment had guessed

1. **Where the settle goes.** The worker records its exit onto the accept receipt
   in `_run`'s `finally`. The first draft put it between the inflight pop and the
   exit frame, on the argument that a client reading the exit must not then see a
   receipt still saying `accepted`. The drain monitor polls the pending set, so
   that placement opens a window in which a request is out of `inflight`, has not
   emitted, and the drain can complete and close the lane **under its own exit
   frame**. Reproduced as a lost exit within ten minutes. It goes FIRST in the
   `finally`, before the pop, where the monitor still counts the request — both
   properties held.
2. **The receipt does carry the id, and had to.** `_write`'s comment said "the
   DIGEST, never the id", and the launcher acceptance asserted it against a real
   serve and failed. It cannot be true: the ack is recorded verbatim so a replay
   is byte-identical to the accept the client saw, and an ack echoes the
   `turn_request_id` the client sent. Two questions had been conflated — the KEY
   is digested (a client-chosen string must never become a path component) and the
   ACK is echoed. The hermes test passed only because it stored an ack shape this
   lane never writes; it now uses a hostile id and asserts the file landed under
   its digest and nowhere else.

### Honest gaps, hermes side

1. **No real provider turn ran over the method lane.** Every serve test injects
   `dispatch`, which is the seam every other serve-loop test uses — what is under
   test is the lane (accept, dedupe, hand off, account, settle), and the argv it
   builds is pinned literally against the real verb. "A remote device got a model
   reply" is unproven here; the launcher acceptance's sandbox has no provider
   either, and asserting on a reply would be asserting on the provider.
2. **`correlation_id` is accepted, fenced and echoed — and rides no further.**
   `harness mission-chat message` has no `--correlation-id` flag, so unlike the six
   office/agent write verbs there is nowhere for the token to join the turn's
   events. Closing it means an argv flag on the chat verb, i.e. a change to the
   LOCAL lane, which this stage's contract forbids. Filed against
   `planned/correlation-id-coverage.md`.
3. **The accept receipt over-claims on one crash.** Between `mark_accepted` and
   the submit, a crash leaves a receipt for a turn that never ran, so the retry is
   answered `idempotent_replay` for work that did not happen. Deliberate: the other
   ordering duplicates an operator's message, which a client cannot undo. A hung
   turn is visible (no journal record) and `turn-resolve` exists for the unprovable
   case.
4. **Steer rides the worker lane even though a steer is cheap.** Uniformity,
   argued: `_CHAT_TURN_COMMANDS` counts both verbs, and a steer that skipped the
   pool would be a chat turn the recycle protection could not see.
5. **No second machine.** Everything binds loopback — Stage 1's gap, unchanged.
6. **Inherited red, not this stage's:**
   `tests/agent_runtime/test_duplicate_helper_bodies.py` fails on
   `gateway_identity` / `gateway_tls` / `serve_auth` / `serve_gateway_auth`
   helper bodies from the Stage 0/1 wave. `git diff eb29bf248b HEAD` over those
   four files and that test is empty — none of them moved in this stage.

### Receipts

`C:\Python312\python.exe -m pytest` (the venv still has no pytest):
`tests/agent_runtime/test_serve_rpc_chat_turn.py` 20 passed; the four
manifest-literal pins across `test_serve_rpc_office.py` /
`test_serve_rpc_office_subscribe.py` / `test_serve_rpc_office_upsert.py` green
(125 in those three plus the parity suite); the whole `tests/agent_runtime/`
tree **6654 passed, 2 skipped, 1 failed** (gap 6 above). Launcher-side, the
serve-frame fixtures were regenerated from a clean detached worktree at
`cf69a0d842` and `generate.py --check` is green twice consecutively; `ready.json`
grew the two method names and their tier rows, with zero removals and no contract
integer moved.

## Stage 6 notes — landed 2026-08-27

### What shipped, in the order it landed

| sha | what |
|---|---|
| `dd8a8ad716` | `agent_runtime/gateway_peers.py` (the peer store) + `gateway_pairing_codes.py` (the code discipline both ceremonies share) + four more helpers hoisted into `store_file_io`. |
| `77768eea27` | 37 store tests. |
| `6775911bbc` | `RpcCaller` grows a `peer` kind; `PEER_METHOD_ALLOWLIST`; `peer.ping`; five manifest-literal pins in the same commit. 18 + 122 passed. |
| `db6bbdc899` | The peer hello on the gateway listener, `_credential_kind`, two client hellos, `_is_device` → `_is_gateway`. 49 passed across both lane suites. |
| `5439595880` | `harness gateway peers pair / join / list / revoke`. 23 + 58 passed. |
| `c246b648ba` | Two isolated roots, two real serve children, both verbs, `peer.ping` A→B. 2 passed. |

### Deviations from the plan, and the argument for each

- **A peer holds an ALLOWLIST, not a tier.** §4 says "peer tier" throughout and
  the brief inherited the word. What shipped is a caller KIND answered from
  `PEER_METHOD_ALLOWLIST = {peer.ping}`, and the argument is about registry
  growth rather than about this stage: a tier comparison admits every future
  verb declaring that word, so the next `read` method somebody registers would
  silently join the peer surface. Canon 06's exclusion needs the opposite
  default. The word "tier" survives in the plan; the mechanism is a membership
  test, and the test that pins it iterates the registry.
- **`peer.ping` declares `read` rather than a peer-only tier.** `TIERS` has two
  members and a third that only one caller kind can hold would put a value in
  the manifest every existing reader must learn to ignore. The map says what a
  call WANTS; the allowlist says who may call it. Argued at length on the
  handler.
- **The two ceremonies SHARE `pairing.json`** — one pending map, one cap, one
  lockout, with a `kind` on each entry. Not in the brief and load-bearing: a
  guesser grinds one code space through one listener, so two failure counters
  would mean a lockout on one ceremony left the other's budget intact. The
  credentials stay disjoint because `match_pending` matches on the kind.
- **Four more helpers moved into `store_file_io`.** The brief said reuse it and
  do not restate bodies; making that true for a second credential store meant
  hoisting the JSON read, the atomic write, the cross-process lock and the UTC
  stamp out of `serve_gateway_auth` rather than importing its privates. Nothing
  was rewritten and the private names stayed as aliases, so Stage 1's call sites
  and tests read unchanged.
- **`ServeSocketClient` lost three copies of the challenge read.** Adding the
  fourth and fifth hello made the duplication a decision rather than an
  accident; `_challenge` is that half once, and the three existing hellos were
  folded onto it rather than left beside it.
- **`_is_device` → `_is_gateway`.** A rename, no behaviour change. The refusals
  it guards were always keyed on the door rather than the device stamp, which is
  why a peer inherits both — and the old name had started to say otherwise.
- **No `peers ping` verb.** `peer.ping` is the wire proof, not an operator
  surface, and inventing a verb to make a test convenient would ship a door
  nobody asked for. The acceptance dials through `gateway_peers.dial_peer`.

### The acceptance had to be two PROCESSES, and threads would have lied

A runtime root resolves from the environment and an environment is
process-global. Two `serve_loop` threads in one interpreter race over
`HERMES_AGENT_RUNTIME_ROOT`: whichever boots last owns the ambient value, and
every later re-resolution inside the FIRST serve answers for the SECOND root.
Such a test passes while demonstrating the opposite of its claim. Two real
`harness serve` children make the isolation the operating system's property
instead. 29s for both tests.

The step that carries the stage is the ping: A dials B at an address it learned
from `peers.json`, because that is the only place it could have. B's serve
registry is on a root A cannot read, and B's port is ephemeral so it is in no
config file either. §4's risk line ("peer dialing needs the remote port from the
pairing record, not a registry file") is therefore not a convention this code
follows but the only mechanism available to it.

### Honest gaps

1. **No second machine.** Every listener binds loopback — Stage 1's gap,
   unchanged, and not closed by two roots on one box.
2. **"Agents can never mint peers" is closed against REMOTE callers only.** No
   `gateway.*` method exists for any peer verb and the argv lane is refused
   outright to every gateway connection, so no caller on that listener at any
   tier can reach them. A LOCAL agent with shell access can run them, exactly as
   it can read `serve_auth_token` — every tool-using agent already holds the
   machine owner's authority. Written into `gateway_commands.py`'s docstring
   rather than left implied.
3. **Revocation is one-sided** and the far install is never told. Correct (the
   alternative is writing into another install's credential store) but an
   operator assuming symmetry is wrong; the ack says so and nothing enforces
   reading it.
4. **Two roots on one machine share a `display_name`** — it defaults to the
   hostname, so a `peers list` shows two identical names and only the
   `install_id` discriminates. `harness gateway rename` fixes it per root.
5. **A recorded endpoint is what the far side ASSERTED**, bounded and cleaned,
   safe because R5's second operator minted the code seconds earlier. An install
   that moves is unreachable until the ceremony is re-run; R8 owns the retry
   posture.
6. **A wildcard bind advertises no endpoint** (`0.0.0.0` is what an install
   listens on, never an address to dial), so that operator gets a
   one-directional edge and a note explaining it.
7. **`icacls` narrowing reported not asserted**, and **the store lock still
   falls through** on a filesystem that will not lock. Stage 1's gaps, now
   covering two stores.


## Stage 7 notes — landed 2026-08-27

### What shipped, in the order it landed

| sha | what |
|---|---|
| `3d0a17922d` | `agent_runtime/gateway_targets.py` — parse `@install/target`, resolve it against `peers.json`, refuse deterministically. 24 tests. |
| `8e2a97bf29` | `chat_turn.normalize_peer_chat_execute` + the `peer.agent_chat.execute` method + `PEER_METHOD_ALLOWLIST`'s second name + **ten** manifest pins. 38 + 99 passed. |
| `8e1dd74c91` | The supervisor's remote leg, `dispatch_store`'s `remote_install_id` column and `REMOTE_UNREACHABLE_REASON`, `peer_store_root()`, `ServeSocketClient.set_timeout`. 20 + 336 passed. |
| `a8b7c6fe90` | Two isolated roots, a turn between them, R8's convergence against a genuinely stopped install. 3 passed. |

### Deviations from the plan, and the argument for each

- **The leg lands in the SUPERVISOR, not the drain.** §4 says "the
  drain/executor … performs the target's turn". `dispatch_delivery` owns exactly
  one thing — *tell the sender* — and `tools/agent_chat_dispatch` owns *perform
  the turn*; the remote leg substitutes for the child PROCESS, so it belongs
  beside the spawn it replaces. In the drain it would have given one dispatch two
  owners and put a minutes-long network turn on the 5s loop that also forges
  deliveries.
- **R8 converges to a delivered `error`, not to `dropped`.** `dropped` means
  *the sender was never told*, and "the other machine is not answering" is the
  one fact the sender most needs. The cap is R8's, read from
  `MAX_DELIVERY_ATTEMPTS` rather than given a second number; `peer_unreachable`
  lands on the row, in `dispatch.completed` and in the sentence the sender
  reads. Argued in full on `dispatch_store.REMOTE_UNREACHABLE_REASON`.
- **The attempts are spent inside ONE supervised run**, not by re-queueing on
  the drain's cadence. That holds a pool slot for as long as the retries take,
  which is the honest cost and a small one — a local dispatch holds its slot for
  a whole thirty-minute turn. The alternative wanted a durable copy of the spec,
  a second claim protocol and a second attempt counter on a row that already has
  one, to buy surviving a serve restart mid-dial, which the local lane does not
  have either.
- **A cross-install send is the DETACHED lane only.** `wait: true` on an
  `@install/…` target is a typed `remote_requires_detached` refusal. The
  synchronous relay's safety story is made of facts about one process (the
  workdir lock, the shared chain deadline) and none of them cross a machine.
- **`peer.agent_chat.execute` is a new method rather than
  `runtime.chat.message` added to the allowlist.** That verb hardcodes
  `--requested-by gateway_device` with a comment saying the field "is not a
  param, cannot be a param" — correct, and exactly why a peer needs its own
  normaliser: its provenance is `peer:<install_id>` and the id must come off the
  authenticated connection. The peer install id is a keyword-only ARGUMENT, not
  a params key, which is the security posture expressed as a signature.
- **The reservation scope carries the peer install id.** `turn_request_id` is
  minted on the OTHER install, so two paired installs can legitimately present
  the same one; without the scope a replay could hand install C the ack for
  install A's turn.
- **A non-peer caller gets `peer_identity_required`, not `scope_denied`.** The
  chokepoint DID admit a console caller (it holds the console tier); it is the
  verb that has no provenance to run under, and a console client already has
  `runtime.chat.message`.
- **`peer_store_root()` is not `paths.store_root()`.** The runtime root resolves
  through `HERMES_HOME`'s config when the env var is unset, and
  `persona_profile_context` flips that process-globally for the length of every
  persona turn — which is exactly when a cross-install send is made and where the
  supervisor thread runs. A peer edge belongs to the INSTALL, so it reads under
  the head home, the precedence `get_hermes_background_work_home` already owns.

### The acceptance found two bugs, and one of them is a lesson about fakes

1. **The serve stdout event is `line`, not `stdout`** — `serve.py:1784` builds
   `_LineFrameProxy(frames, "line")` for OUT and `…, "stderr")` for ERR, so only
   the error stream is named after itself. The frame reader guessed, collected
   nothing, and reported every remote turn as "no reply payload". **The unit test
   was green**, because its fake emitted `"stdout"` too: *a fake that spells the
   wire itself can be wrong in the same direction as the code it tests.* Fixed
   with a named constant that the fake now reads, plus a grep fence against the
   one line in `serve.py` that decides it.
2. **A replayed accept carries no frames, and waiting for them is a hang.** B's
   per-request frames go to the sink of the connection that ASKED, so when the
   retry posture WORKS — the same `turn_request_id` stops B running the agent
   twice — the retrying socket gets an ack and then silence. The success path of
   the property was the thing that hung, for the full CLI timeout. The leg now
   settles a `settled` replay from the receipt (saying plainly that the answer is
   in the thread on the other install) and retries an unsettled one.

Also found and corrected in passing: `test_serve_rpc_notification_lane.py`'s
`all(name.startswith("runtime."))` assertion had been RED since Stage 6's
`peer.ping` and nobody ran the file. Re-measured on a stashed tree before
touching it. **The S6b lesson has a second half: find the manifest pins by
RUNNING the suites, not by grepping for the verb you added** — this one never
spells a method name at all.

### Honest gaps

1. **No second machine.** Every listener binds loopback. Stage 1's gap,
   inherited unchanged through Stages 6 and 7.
2. **No model turn on B in the acceptance.** Both roots are fresh and the suite
   has no provider, so what is proven is that the turn reached B's real
   mission-chat handler and was answered by B's own admission
   (`unsupported_persona`, exit code 2) — not that an agent composed a reply.
   The forge back into A's chat is the unit lane's proof and the delivery lane's
   own suites', not this file's.
3. **The relay chain does not cross an install boundary**, so **A→B→A across two
   installs is not detected as a cycle** by either side's guard. Forwarding it
   would be forwarding an assertion the far side cannot check, and a chain a
   sender can understate is a guard that can be talked past; a cross-install
   dispatch is therefore a fresh chain root on B, which is what a detached
   dispatch already is locally. Named rather than hidden; it is the next thing
   this lane owes.
4. **A retry that lands after B accepted loses the reply TEXT.** The frames went
   to a connection that no longer exists, so the row settles from the receipt and
   points at the thread on the other install rather than carrying the answer.
   Rare (it needs the socket to die between accept and exit) and honest, but the
   sender gets a pointer instead of a reply.
5. **The remote leg's retries hold a dispatch-pool slot** for up to
   `MAX_DELIVERY_ATTEMPTS × PEER_RETRY_BACKOFF_SECONDS` (~40s) while converging.
   Inside what the cap already admits for local turns, and stated because it is a
   real occupancy cost that did not exist before.
6. **Nothing on the operator surface names a cross-install dispatch yet beyond
   `remote_install_id` on the row and in `agent_chat_dispatches`.** The Activity
   HUD renders `target_persona`, which now reads `@workstation/dev` — legible,
   but not a designed surface.
7. **The CLI contract fixture in the launcher is red for an unrelated reason.**
   `harness characters backfill-home` (the charsheet lane) has never been
   captured. Stage 7 adds no CLI verb, so that refresh was deliberately left out
   of the gateway commit and recorded in the launcher's field notes instead.

### Receipts

`C:\Python312\python.exe -m pytest` (the venv still has no pytest):
`test_gateway_targets.py` 24; `test_peer_chat_execute.py` + `test_peer_authorization.py`
38; the chat-turn / peer-lane / stream-parity / notification / snapshot-contract
group 99+13; `test_remote_dispatch_leg.py` 20; the dispatch + agent-chat + event
group 336; `test_gateway_peer_cross_install_chat_e2e.py` **3 passed in 48s**.
Launcher-side: `ready.json` grew `peer.agent_chat.execute` and its `console`
tier row with zero removals and `contract` still 1;
`generate.py --check` green twice consecutively;
`flutter test test/features/mission_control/` **5291 passed, 1 skipped**.


## Drift addendum — audited 2026-08-27

Architecture re-verified at HEAD (`1295212f2e`) after the S0–S10 placement wave: no
design decision invalidated, the socket lane (`serve_socket.py`, `serve_auth.py`,
`serve_registry.py`, `harness_parts/serve.py`) has ZERO commits since this plan
landed, every stage is still unbuilt, and neither contract integer moved. But the
wave built three of this plan's hard parts on the very lane it calls the contract —
ride them, don't re-derive:

- **Manifest membership is the proven rollout mechanism.** `serve_rpc.manifest()`
  (`serve_rpc.py:246` — a set plus an integer, `RPC_CONTRACT_VERSION = 1`, methods
  derived from the `@method` registration site) and `ops_manifest(transport=…)`
  (`harness_parts/serve.py:307` — answers PER TRANSPORT, `shutdown` stdio-only) ride
  `ready`/`hello_ok`/`version` — the exact frames Stage 0 extends with `install`. The
  launcher's D12 gate keys its placement lane off `runtime.agent.retire`'s presence
  in `manifest.rpc.methods`, test-pinned against a byte-pinned capture. Stages
  0/1/6/8 advertise themselves the same way; connector tiers check membership, never
  mint a version negotiation. The per-transport `ops` shape is what a device/peer
  listener split reuses. Caution: the launcher's serve-frame fixture check
  (`tool/hermes_serve_frames/generate.py --check`) is on no CI lane — that hole is on
  Stage 0's critical path.
- **Stage 3's dedupe hook has a shipped precedent.** `runtime.agent.create` carries
  `idempotency_key` reservations replaying the ack as `idempotent_replay: true`
  (`agent_create.py:522`, `agent_create_reservations.py:248`); `already_retired: true`
  is the retire analogue. Copy this to mission-chat send.
  **ANSWERED 2026-08-27, and the premise underneath it was false.** Mission-chat
  send did NOT need this hook: `client_message_id` plus the per-session turn
  journal has answered a repeated send with `idempotent_replay: True` since the
  2026-08-24 incident, and it is richer than the create's (it distinguishes
  still-running from committed from unprovable). What Stage 3 built is the
  create's SHAPE over a strictly narrower claim — the ACCEPT window the journal
  cannot cover, because the journal's first write happens inside the chat-root
  lease, after a worker is already running. `turn_request_id` is passed to
  `--client-message-id` unchanged, so there is one key and not two.
- **Stage 0 must not mint another install id.** `monitoring.install_id`
  (`agent/monitoring/policy.py::ensure_install_id`, consumed as OTel
  `service.instance.id`) and the telemetry `install_id`
  (`hermes_cli/observability/shared_metrics.py:259`) already ship. Inventory and
  reuse-or-distinguish, per `planned/duplicate-implementation-retirement.md`.
  **ANSWERED 2026-08-27 — DISTINCT, on scope + lifetime + audience. The
  three-row table and the full argument are in "Stage 0 notes" above; the
  mechanism is `serve_auth.py`'s, deliberately not re-derived.**
- **Stage 1 is blocked on an authorization chokepoint that does not exist.** Canon 06
  ("What a remote connector inherits" + its Open row): `authorize_coordinator_action`
  is called from CLI handlers only — `runtime.agent.create`/`runtime.agent.retire`
  are ungated on RPC, `console`-tier is a decision with no check. `serve_gateway_auth.py`
  has nowhere to hook scopes (R11) until authorization moves to the chokepoint the
  three doors share; rule that with R11 before any non-loopback bind. **The design
  for that chokepoint — inventory, three placement options, staged — is
  [authorization-chokepoint.md](authorization-chokepoint.md) (2026-08-27); its
  Ruling A is R11's prerequisite, and it measured the gap to be wider than an
  asymmetry: the "gated" door's gate is unreachable on the spellings the launcher
  and the CLI actually send.**
- **Correlation tokens are Stage 7's join primitive — and not an identity.** Six
  write verbs carry optional `correlation_id` (charset + 64-cap, refused out loud at
  the RPC boundary, `serve_rpc.py:379`); the launcher mints per-process-origin tokens
  that already solve the N-clients collision this plan multiplies. Device attribution
  must come from the connection identity the socket lane tracks, never from the
  token. The cross-process one-grep acceptance (CI-3,
  `planned/correlation-id-coverage.md`) is still unscripted; a remote lane raises its
  price.
- **The placement verb's compatibility table lives in canon now.** What a remote
  connector may and may not assume from `runtime.agent.create`/`runtime.agent.retire`
  — including that both are `console`-tier and belong on NO peer allowlist, and that
  `placement_census` is a CLI/ops report, not a method — is
  `06-office-and-board.md` § "What a remote connector inherits". Cite that, not the
  deleted placement plan. The fold-set-intersection hazard (a narrow phone fold
  demotes every subscriber's patches to full cores; fix = per-subscriber promotion at
  the hub, owned by Stage 5) is filed in the primary plan's §5 R10.
