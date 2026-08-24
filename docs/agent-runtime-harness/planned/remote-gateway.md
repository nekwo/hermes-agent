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
  turn-request-id dedupe hook for the remote write path (survey found none).
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
