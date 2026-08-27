# Planned — Remote Gateway (hermes half)

**Status:** surveyed + staged 2026-08-24, **GATED on the operator rulings R1–R13 in the
primary plan — no stage builds before its ruling.**
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

- **Stage 0 — install identity:** mint-if-absent `<store_root>/gateway/install.json`
  (`install_id` + operator-set display name), additive `install` block on
  `ready`/`hello_ok`/`version`, `harness gateway id` verbs, `gateway.listen`/`gateway.port`
  config (default off). Fixture-mirror caution: additive only, run the launcher
  producer-contract check.
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
