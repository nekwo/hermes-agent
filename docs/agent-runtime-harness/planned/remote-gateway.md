# Planned — Remote Gateway (hermes half)

**Status:** surveyed + staged 2026-08-24, **GATED on the operator rulings R1–R13 in the
primary plan — no stage builds before its ruling.** 2026-08-27: R1 RULED (encrypt;
self-signed pinned baseline + launcher-reuse survey) and the authorization
chokepoint RULED (front door; tier = account auth) — see
[authorization-chokepoint.md](authorization-chokepoint.md) § the three rulings;
Stage 1's two prerequisites are now decisions, and its remaining gates are R3/R11's
vocabulary details.
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
  moved. See the Stage 0 notes below.
- **Stage 1 — device pairing + LAN bind:** `serve_gateway_auth.py` (hashed per-device
  tokens, scopes R11, revocation; `gateway/pairing.py` discipline), pairing verbs with
  QR payload `{host, port, install_id, cert_fingerprint, code}`, second listener bound
  per config accepting ONLY device/peer hellos — loopback lane byte-identical. TLS
  posture is ruling R1 (recommended: self-signed per-install cert, fingerprint pinned
  via pairing payload).
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

### Stage 0b — the CLI verbs, filed separately

`harness gateway id` / `--set-name` did **not** land with Stage 0a. Deferred for
SCOPE, not entanglement (both trees were clean by the time this was staged): the
verbs change hermes' argparse tree, which the launcher pins in
`test/features/mission_control/fixtures/hermes_cli_contract.json` and drives
through its argv conformance suite — a second cross-repo fixture landing stacked
on the serve-frame refresh Stage 0a already paid for. Filed as a row in the
launcher queue's gateway section (`Launcher_Brain/20 — Active Initiatives/mission-control-queue.md`).
Until it lands, renaming an install means editing `install.json` by hand and the
default name is the machine hostname. `set_display_name()` and `read_install_identity()` already exist in
`gateway_identity.py` as the verb's service half and are tested, so the remainder
is parser registration in `hermes_cli/harness.py` plus a dump refresh.

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
