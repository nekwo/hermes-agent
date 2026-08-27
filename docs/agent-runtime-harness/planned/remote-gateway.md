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
- **Stage 3 (shared) — send idempotency:** mission-chat send needs a server-side
  turn-request-id dedupe hook for the remote write path (still absent there — no
  `turn_request_id` anywhere, re-verified 2026-08-27 — but the precedent now ships on
  a sibling verb; see the drift addendum).
- **Stage 6 — peer pairing (install⇄install):** `gateway/peers.json` both sides,
  distinct peer hello, operator-approval gate (R5 — agents can never mint peers),
  `harness gateway peers` verbs, `peer.ping` RPC.
- **Stage 7 — cross-install `agent_chat_send`:** install-qualified target grammar
  (R4, recommended `@install_name/target`; unqualified = local forever), dispatch row
  stays on the SENDER install with a remote-execution leg (`peer.agent_chat.execute`
  over the peer tier); claim/refund/attempt-cap semantics unchanged — peer-unreachable
  refunds like busy with a bounded dial timeout, deterministic remote refusals fail
  fast (R8 governs cap vs TTL). Target install records the turn in its own chat store.
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
