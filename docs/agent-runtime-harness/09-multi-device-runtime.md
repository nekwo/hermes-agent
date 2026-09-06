# 09 — Multi-device runtime: one account, many machines, one runtime per machine

## What this domain is

The runtime half of the same-account multi-device program: what a hermes
runtime must be and do so that an operator with several machines on one
account — today a Windows PC and a Mac — gets ONE set of agents that exist on
every machine, can be addressed from any machine, and are driven from whichever
launcher the operator happens to be sitting at. The launcher half — the
cockpit's aim, the install picker, the pairing lane, local runtime ownership —
is `EterniaLauncher/docs/mission_control/10-multi-device-architecture.md`, and
that document carries the program's target picture, its ledger and its open
rows. This one states only what the RUNTIME holds and does, and links out for
the rest. Where 03 already states a wire fact in full (the gateway lane, the
peer hello, `@install/` targets, the `fetch` family), this doc points at the
section rather than restating it.

Added 2026-09-06. The material was scattered across 01 (replication), 03 (the
gateway lane), 04 (`--service` boot) and eleven plan files; none of them said
what the whole was for.

## The target, in one paragraph

Every machine runs its own hermes install with its own store root and its own
durable runtime. Nothing is relayed through a desktop and nothing is brokered
through the Eternia backend for frames: machines reach each other on the LAN,
directly, over the same authenticated socket lane the local launcher uses. The
account is the trust anchor — two installs on one account pair
AUTOMATICALLY, by grant, with no code typed — and the realm is the identity
anchor — an agent is the same agent on every machine because realm sync
replicates its durable identity, not a copy with a new id. Which machine RUNS a
given turn is a routing fact chosen per gesture: the operator's cockpit aims
at an install, an agent qualifies a target with `@install/`, and an
unqualified target is local forever.

## The three tiers, and where this program stops

| tier | what it means | status |
|---|---|---|
| **1 — replication** | a realm pull that delivers a desk delivers a WORKING agent with the same durable instance id on every member | LANDED and proven live across two machines 2026-09-01 (01 § The instance-replication lane) |
| **2 — cross-machine communication** | installs pair, devices and installs hold distinct credentials, agents and operators reach agents on another install, media crosses | LANDED; the live proofs are recorded in the launcher doc's ledger, the last of them the console chat proof of 2026-09-06 |
| **3 — cross-account** | pairing or messaging between different accounts | NOT DESIGNED; every mechanism below assumes same-account members, which is why admission is a single door rather than an authorization model |

Deliberately outside every tier: the backend broker connector (the gateway
plan's §8 appendix, unstaged), mDNS discovery (rejected; addresses are
published through the account), NAT traversal, a lite mobile runtime, and any
WRITE joining the peer allowlist — agents never mint or retire agents on
another install.

## The entities a runtime holds

- **Install** — `<store_root>/gateway/install.json`, `{install_id,
  display_name, created_at}`, minted once per store root at boot by
  `agent_runtime/gateway_identity.py` and echoed as the `install` block on
  `ready` / `hello_ok` / `version`. It NAMES and never authorises (03 §2).
  `display_name` is a cache fact the install publishes about itself; the name
  an operator reads is the ACCOUNT's device name joined on `install_id`
  (R-IP14), with this one as the labelled offline fallback.
- **Runtime** — the `harness serve` process that owns the root's loopback
  socket and, when configured, its LAN listener. Registered in
  `serve_instances/<pid>.json` with `boot_id`, `build`, `service` and
  `starter_pid`; ends with `<pid>.ended.json` naming why; under `--service`
  keeps `<pid>.stderr.log` (02 § serve_instances, 04 § Shutdown). A machine
  has ONE owner per root — `agent_runtime/serve_socket.py`'s OS lock decides
  it and a dead owner yields (R-L2).
- **Paired device** — `gateway/devices.json`, one row per credential a
  LAUNCHER holds: `{device_id, name, tier, verifier, …, account_device_id}`
  (`agent_runtime/serve_gateway_auth.py`). A launcher on another machine is a
  DEVICE to this runtime, at `console` tier, and that is the credential the
  cockpit's remote aim rides. One live row per account device (RL-23): a
  redeem scoped to an `account_device_id` supersedes every earlier live row
  carrying that label.
- **Paired peer** — `gateway/peers.json`, one row per INSTALL that may call
  this one as an agent's runtime rather than as an operator's cockpit
  (`agent_runtime/gateway_peers.py`). Trust fields are written by a ceremony
  only; cache fields (`display_name`, `endpoints`, `cert_fingerprint`,
  `last_seen`) refresh from the network. A peer holds an ALLOWLIST, not a tier.
- **Peer directory** — the per-install cache of what each paired install last
  said about itself and its roster, read by the launcher's namespaced roster
  and by the `agent_chat_installs` tool through one door
  (`agent_runtime/peer_directory.py`); pushed, never polled, by
  `peer.announce` (`agent_runtime/gateway_announce.py`).
- **Realm, workspace, persona instance** — the identity these machines
  SHARE, owned by 01. Placement-backed instance rows travel on publish and are
  minted through the store door on pull (`PersonaInstanceStore.replicate_instance`,
  `apply_persona_instance_pull`); canonical channel rows never travel.

## The lanes, runtime side

### Credentials: two doors on one listener

The LAN listener (03 §1.1) admits two credential kinds and refuses a frame
naming two of them. A DEVICE proves `HMAC-SHA256(key=sha256(token),
msg="gwv1|<port>|<device_id>|<nonce>")`; a PEER proves the `pwv1` shape keyed
on the shared secret verifier (03 §1.2). Each credential is refused on the
other's field, on the other lane, and in the other ceremony's verb. The tier a
device holds is fixed at pairing and compared by `call_authorization.authorize_call`
against the tier each method declares; a peer is answered from
`PEER_METHOD_ALLOWLIST` before the read-tier arm runs, so it inherits nothing.

`PEER_METHOD_ALLOWLIST` at HEAD is six methods: `peer.ping`,
`peer.agent_chat.execute`, `peer.media.get`, `peer.roster.list`,
`peer.thread.read`, `peer.announce`. The first two are gateway Stage 7, the
media verb is P4 (cross-install media), the roster and thread reads are S2b
(R-IP9, read-only by ruling), and the announce is S2c. Nothing else is
callable by a peer; a test iterates the registry against the set rather than
naming the refused verbs.

`LOCAL_CONSOLE_METHODS` is the R-B wall in the other direction — verbs only
the machine owner's own console may call, refused to a paired device of ANY
tier: `runtime.workspace.use`, `runtime.realm.use`, and the three
`runtime.gateway.peers.*` verbs (`list`, `roster`, `subscribe`). A cockpit
aimed at a remote install therefore cannot move that install's active scope
and cannot read its peer directory as a device; it reads its OWN runtime's
directory instead, which is what the launcher's namespaced roster does.

### Pairing without a code: introduce, and the account's grant

The manual ceremonies (`harness gateway pair` for a device, `harness gateway
peers pair | join` for an install) still exist and are the not-signed-in
fallback. The primary path since 2026-09-03 is same-account instant pairing:

1. Each launcher publishes its install's `install_id`, dialable endpoints and
   certificate fingerprint to its own device row on the account (backend
   `Device.harness_install_id`, `gateway_endpoints`,
   `gateway_cert_fingerprint`; the backend copy is a CACHE and the install is
   the authority).
2. The requesting launcher asks the backend for a **pair grant** addressed to
   the target device; the target's launcher runs `harness gateway introduce`
   on its own runtime, which mints BOTH a device code and a peer code for the
   named peer in one verb and returns the payload the backend hands back
   exactly once, within 120 s.
3. The requester redeems: its launcher redeems the device code as a device
   (console tier, scoped to its `account_device_id`), and its runtime runs
   `harness gateway peers join` with the peer code, dialling the ORDERED
   endpoint list from the payload (D1) — never a bind address (R-D1), never
   a registry file (03 §1.2).
4. The grant id travels as `introduce --correlation`, so one key joins the
   request on machine A, the mint on machine B and the redeem back on A across
   three processes' logs (R-IP17).

What the runtime owes this lane and holds at HEAD: the pending-entry scoping
that lets a redeem name its `account_device_id` (RL-23's supersede rides it);
a thirty-day `expires_at` on introduced edges that both ends hold because it
rides `hello_ok.peered`, against `None` for a hand-run ceremony; the routing
table's first candidate on the join ack (D1b, R-D8); and `reached_at` — the
accepting socket's own measured address — on `hello_ok`, every `connections`
row and the join ack (D12, 03 §2). Acceptance for the runtime half:
`tests/agent_runtime/test_serve_gateway_peer_lane.py`,
`test_serve_gateway_peers_rpc.py`, `test_gateway_peer_two_roots_e2e.py`,
`test_introduce_is_unreachable.py`, `test_peer_directory.py`,
`test_gateway_announce.py`.

### Addresses: what is published, what is dialled, what is measured

- **Published** — `harness gateway id --json` enumerates the interfaces behind
  a wildcard bind at CLI time and never hands out `0.0.0.0` (R-D1); the
  launcher publishes that list to the account.
- **Dialled** — `gateway_peers.dial_peer` walks the pairing record's ordered
  list; a refusal is classified, and on macOS an on-link `EHOSTUNREACH` with
  a resolved neighbour is `local_policy` (the Local Network privacy gate, D6)
  rather than `no_route`, because the two send an operator to different
  fixes.
- **Measured** — `reached_at` is the one address on the lane that
  demonstrably carried a packet; the launcher's promotion of it to the head
  of the published list is the owed launcher half of D12
  (`planned/d12-reached-at-measured-address.md`).
- **The Windows firewall** prompt names the Python interpreter; the launcher's
  host-firewall lane (S3-FW) now probes, escalates and confirms the rule on
  the operator's behalf, so the operational note in 03 §1.1 is no longer
  undriven from the product side, only from this repo's tests.

### Cross-install chat and reads

An agent addresses `@install/target`; the dispatch row lives on the sender,
`peer.agent_chat.execute` runs the turn on the far install as a DETACHED
send, the far side records who asked off the CONNECTION, and transport
retries converge to `peer_unreachable` (03 §1.3, unchanged). Reads that cross
since S2b: `peer.roster.list` (the far install's own addressable roster, shaped
by its rules) and `peer.thread.read` (a far thread by `session_id`), both
surfaced through the `agent_chat_installs` / `agent_chat_open` /
`agent_chat_threads` tools. Media: a reply's `MEDIA:` line names bytes the far
install holds, so the handle is minted THERE and fetched by `peer.media.get`
through `agent_runtime/media_proxy.py`, which re-hashes before serving (P4).
Known gap, unchanged: A→B→A across two installs is a fresh chain root on B and
is not detected as a cycle.

### The operator's remote turn

A launcher aimed at a remote install speaks to that runtime as a device at
console tier and drives chat over the method lane only — `runtime.chat.message`,
`runtime.chat.steer`, `runtime.persona.instance.open_chat` — because the argv
lane is refused to every device connection (03 §1.1). The method carries the
verb's WHOLE operator surface and the manifest advertises the carried keys in
an additive `params` block (R-C8; 18 keys on `message`, 6 on `steer`), so an
older runtime refuses a key it does not carry by name rather than losing it.
The turn's frames ride the per-request lane; `ChatTurnPresence` publishes
`persona_chat.turn_started` / `turn_ended` so a second console sees the row.
Acceptance: `tests/agent_runtime/test_serve_gateway_chat_reply_lanes.py` and
the launcher's `chat_turn_method_lane_real_serve_test.dart`.

### The runtime outlives the launcher

A launcher does not own its machine's runtime any more; it ATTACHES to the
registered owner and starts one only when none is live. The runtime side of
that (row L, 2026-09-05; 04 § Shutdown): `harness serve --ndjson --service`
treats stdin EOF as "the starter detached" (`stdio_owner_detached`), swaps the
stdio sink for a null sink, keeps both socket lanes serving and parks until a
socket `drain --force`, a SIGTERM or a stdio `shutdown` received before EOF;
a second `--service` starter that loses the owner lock exits 0 with
`serve_owner_exists` and serves nothing; `service` and `starter_pid` ride the
greeting and the registry row. What a detached runtime cannot say on stdio it
now says elsewhere: the `.ended.json` sidecar names every ending it can
observe (RL-16), the `.stderr.log` keeps its tracebacks (RL-19), the `busy`
frame separates standing subscriptions from work so an attached launcher
reading `pending=2` on an idle runtime is not misled (RL-13), and the three
argv exits are three frames so only a true parse failure is ever replayed
(RL-24; 03 §1). Acceptance: `tests/agent_runtime/test_serve_service_mode.py`,
`test_serve_socket_child_e2e.py`, `test_serve_request_silence.py`.

## Invariants

1. **Unqualified is local, forever.** A chat target with no `/` never reaches
   the peer resolver; no config, env var or roster lookup can make a bare
   target mean another machine.
2. **One credential per hello, counted rather than ranked**; neither door
   accepts the other's credential in any direction; the root token never
   travels.
3. **A peer holds an allowlist, a device holds a tier, and the local console
   holds the wall** — `PEER_METHOD_ALLOWLIST`, `authorize_call`'s tier
   compare, `LOCAL_CONSOLE_METHODS` — and every one of them is a membership
   test the manifest and the dispatcher read from the same tuple.
4. **The install id names and never authorises**; nothing secret is reachable
   from the `install` block.
5. **Addresses that are handed out are dialable ones**: no bind address in a
   payload, no registry file read across a machine boundary, and a measured
   address is marked as measured.
6. **One live device row per account device**; a hello writes cache fields
   only, never trust.
7. **The dispatch row lives on the sender; the far side records the executed
   turn as its own inbound message.** No distributed row, no two-phase state.
8. **A runtime is one owner per root, proven live at read time**, and it ends
   only on a drain, a signal or its owner's `shutdown` — never on a launcher
   closing its pipe.
9. **Same-account only.** Every admission decision above assumes the two
   parties share an account; a cross-account design is its own program.

## Ledger — runtime-side landings, by lane

Shas are the hermes tips named in the plan files that own the lanes; the
launcher doc carries the launcher and backend halves.

| lane | landed (hermes) | plan of record |
|---|---|---|
| Universal remote gateway, stages 0–8 (install identity, LAN bind + TLS, device pairing, peer ceremony, cross-install chat, media handles) | 2026-08-27/28, 24 commits, all ancestors of `main` | `planned/remote-gateway.md` → `EterniaLauncher/docs/mission_control/planned/universal-remote-gateway.md` |
| Instance replication H1–H4 | `a0c171af47` | 01 § The instance-replication lane |
| Instant workspace switching WS1/WS4 (`scope` fold entity, `runtime.workspace.use` / `runtime.realm.use`, R-B wall) | `cf9abaac4b`, `ffd540bf73` | `planned/instant-workspace-switching.md` |
| Cross-install media P4 | `ed3c6a11aa` | `EterniaLauncher/…/planned/remote-parity-and-two-machine-proof.md` |
| Same-account pairing S0a/S0b/S2 (`introduce`, peer directory, `peer.roster.list` / `peer.thread.read`, `peer.announce`, `runtime.gateway.peers.*`) | `e94e022fb6`, `b4a383a1e8`, `dcba382f0a` | `EterniaLauncher/…/planned/same-account-instant-pairing.md` §5 |
| Dialable addresses D1/D1b/D4h/D5h/D6h/D7h/D8h (ordered dial, first candidate, store writes on Windows, `local_policy`, lock deadline) | through `74bb73a124` | `EterniaLauncher/…/planned/dialable-addresses.md` |
| `reached_at` measured address (D12) | `355460290a` | `planned/d12-reached-at-measured-address.md` |
| Reachable-switch L1 (dead owner yields, greeting never silent) | `abc13e283c` | `EterniaLauncher/…/planned/reachable-switch-lifecycle.md` |
| Remote chat parity C1h/C1h-bis/C6h (`open_chat` method, turn presence, 18-key message + `params` block) | `997900010e`, `23df196e69`, `28e502e286` | `EterniaLauncher/…/planned/remote-chat-parity.md` |
| Local runtime ownership L-h/L-h-b/L-h-c/L-h-d + Q-h (`--service`, busy split, `.ended.json`, `.stderr.log`, RL-23 supersede, RL-24 three exits) | `286a29db04`, `940396b992`, `f7b89826eb`, `a12b16f287`, `6c1dfaf444` | `EterniaLauncher/…/planned/local-runtime-ownership-and-retry-safety.md` §8 |

Live proofs, both machines: replication L3 (2026-09-01), paired both ways
after the Mac allowed Local Network (2026-09-04), the cockpit switched to the
Mac with a live stream (2026-09-05), console chat to a Mac agent over the
method lane with acks under a second (2026-09-06). Each is recorded with its
receipts in the owning plan's field run section.

## Open rows

- **The Mac's device hello refused `unknown_device`** — the accumulation half
  is closed by RL-23; the refusal a REAL dial met still needs the Mac's
  `install_connection … reason=` read. Operator row.
- **The A→B→A cycle across installs** — recorded gap, own row, not staged.
- **`reached_at` promotion (D12's launcher half)** — gated on the operator's
  D3 run #7; `planned/d12-reached-at-measured-address.md`.
- **R12 / R13 of the gateway plan** (chat-bridge thinness, connector plugins)
  stay deliberately unruled — nothing staged consumes them.
- **Shipped plans still sit in `planned/`** — `remote-gateway.md`,
  `instant-workspace-switching.md` and the field notes of landed lanes carry
  corrected status headers but have not moved to `archive/` per the 00-index
  rule; a records lane owes the move once the launcher's ledger doc (10) is
  the fold-in target.

## Supersedes

Nothing archived; this doc is a new partition. It restates no wire fact that
03 owns and no replication fact that 01 owns — where a sentence here and one
there disagree, the older doc's section is the authority and this one is the
defect.
