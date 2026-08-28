# 03 — Transport and Wire

How a shape leaves this process. The runtime speaks four transports — an argv
lane, a JSON-RPC method lane, an op lane carrying a push subscription, and MCP —
and one warm serve process answers all of them. This is the current,
code-verified contract for each: the frames, how a consumer negotiates what it
can fold, and which parts of the wire are pinned byte-for-byte across the repo
boundary. The goal/task mission lane was removed 2026-07-30; chat is the only
lane and nothing below carries a task frame.

## 1. The serve process model

`hermes harness serve --ndjson` is one warm process replacing the per-call CLI
spawns the Launcher bridge otherwise pays a ~3s import tax on
(`hermes_cli/harness_parts/serve.py:1-7`). Requests arrive as NDJSON, one frame
per line, and dispatch into the **existing** harness argparse tree unchanged:
`dispatch_argv` (`serve.py:937`) builds a fresh parser per request
(`_build_harness_parser`, `:925`) and calls the same `_cmd_*` handler the CLI
would, including the harness error-envelope contract — argv arrives verbatim as
the bridge already builds it, which keeps the per-call CLI fallback
byte-identical to the served path. **`ready` is a BOOT frame, not a request
frame** — classified as boot in the protocol block (`serve.py:20`) and emitted
exactly once per process, at `:1663-1664`. What rides a REQUEST is `line` × N,
`exit` and `stderr` (`:31-37`), plus a typed `error` frame when argv parsing or
dispatch fails (`:1346`, `:1355`). Handlers `print()` directly and streaming
turns emit deltas live, so stdout/stderr are swapped once for contextvar-dispatching
proxies (`_LineFrameProxy`, `:706`), one write lock keeps frames atomic, and
writes from handler-spawned threads carry `"id": null`.

**Chat turns are marked at the argv boundary, and the mark is a safety
contract.** `_CHAT_TURN_COMMANDS = (("mission-chat", "message"), ("mission-chat",
"steer"))` (`serve.py:379`); `_ArgvRequest.__init__` (`:832`) matches the argv
tail against it. The `ping` reply counts them (`_busy_frame`, `:1274`) because
the Launcher supervisor must never recycle serve while one is in flight, and a
drain deadline expiring with `held_by_chat_turns > 0` emits a NON-terminal
`drain_timeout`, keeps serving, and re-arms (`"event": "drain_timeout"` `:2418`,
`held_by_chat_turns` `:2434`, `"terminal": not chat_turn_ids` `:2436`, the
keep-serving re-arm `:2438-2445`; contract at `:76-82`).
Recording safety outranks restart latency. A running `harness stream` request is
the sole exception to "running requests cannot be cancelled" — cooperatively
cancelled, releasing its pool worker (`is_runtime_stream`, `:835`, used `:2952`).

**Two transports, one dispatcher.** `serve_loop` is transport-agnostic. One
serve per root owns a localhost socket, decided by an OS-held exclusive lock
(`agent_runtime/serve_socket.py`); the loser runs stdio-only and says so on
`ready`. The handshake is challenge-response and the SERVER speaks first —
`server_hello` carries a 64-hex nonce and `hello_contract: 3`, and the client
answers `HMAC-SHA256(key=<per-root token>, msg="v3|<the port it DIALLED>|<nonce>")`.
**The token never travels**: it is the HMAC key, never a field, so a captured
transcript is unreplayable. **The proof is bound to the port**, which is what
makes it un-RELAYABLE — a fresh nonce stops replay but not a live relay, and the
port is the one value each end takes from its own socket rather than from
anything the other one claims. `serve_socket.hello_proof` is the authority;
prose that disagrees with it has been wrong before (this file and two module
docstrings all said `msg=<nonce>` and contract 2 until 2026-08-27 — which costs a
client author an afternoon, because a proof over the wrong message is refused
`bad_proof`, byte-identical to holding a bad credential).
Only authentication failures (`bad_proof`, `hello_required`, `hello_malformed`)
charge the rate limiter; server-state reasons never do, because charging them
made a blocked window extend itself forever.

### 1.1 The gateway listener — the same lane, bound beyond loopback

Since the remote gateway's Stage 1 a serve can open a SECOND listener. It is the
same `ServeSocketServer` class with three constructor arguments filled in, not a
second implementation, because the hardened parts of that class are exactly the
parts a copy would get wrong (the accept loop that announces its own death, the
pre-auth bound that counts peers who have proven nothing, the two limiters and
the rule that server-state refusals never charge the auth one). Config:
`remote_gateway.listen` (a HOST STRING; `false` is off, and boolean `true` is
REFUSED — an operator opening a LAN port has to say which interface) and
`remote_gateway.port` (0 = ephemeral; usually pinned, because a firewall rule
and a paired phone both need a number that survives a restart). **Off by
default, forever.**

| | loopback lane | gateway lane |
|---|---|---|
| host / port | `127.0.0.1`, ephemeral | operator-chosen, usually fixed |
| link | plaintext | TLS, self-signed per-install cert, client PINS the fingerprint (R1) |
| credential | the per-root `serve_auth` token | a per-DEVICE token (`gateway/devices.json`) or a per-INSTALL secret (`gateway/peers.json`) |
| `connection.transport` | `socket` | `gateway` |
| caller kind | `local_console` | `device`, carrying its stored tier — or `peer`, carrying an allowlist |
| ops | every op minus `shutdown` | that, minus `drain` |
| argv lane | yes | **no** |

The device hello names `device_id` and proves
`HMAC-SHA256(key=sha256(device token), msg="gwv1|<port>|<device_id>|<nonce>")`
(`serve_gateway_auth.device_proof`). A hello naming `pairing_code` instead
redeems a short-TTL code minted by `harness gateway pair`, and is admitted as
the device it just created with the token riding that one `hello_ok` and never
again. Every credential failure on this lane — no id, unknown id, revoked row,
wrong proof, wrong code — collapses into ONE `bad_proof` rejection, so a peer
that has proven nothing cannot enumerate device ids by watching the reason
change.

**Neither door accepts the other's credential**, in both directions: the root
token is refused on the gateway lane, a pairing code is refused on loopback.

**The argv lane is unreachable from a device**, and that refusal is what makes
the tier gate real rather than decorative: `authorize_call` gates the METHOD
lane, while `{"argv": ["harness", …]}` reaches the CLI dispatcher where no tier
declaration exists. Without it a `read`-tier device refused
`runtime.agent.retire` sends the same verb as argv and is obeyed. It is a
REFUSAL rather than a second gate because gating argv means deciding a tier for
every CLI verb in this repo and keeping that map correct forever — the
duplicated-authority shape this stack keeps retiring.

**The loopback lane is byte-identical whether or not the gateway lane is up**,
and that is asserted directly rather than inferred: two steady-state boots
compared field by field on `ready` and on a loopback client's `hello_ok`
(`tests/agent_runtime/test_serve_gateway_lane.py`).

A `gateway` block rides `ready` / `hello_ok` / `version` on the same "states its
own outcome" rule as `socket`: `disabled`, or `listening` with `host` / `port` /
`cert_fingerprint`, or `error:<reason>`. Never absent.

**Operational note — the Windows firewall.** The port is operator-chosen, and on
Windows the first bind beyond loopback raises the Defender Firewall prompt
naming the PYTHON interpreter (not `hermes`), because the listener is a socket
in that process. Allowing it on Private networks is what a LAN pairing needs;
Public is the wrong answer and is what the dialog defaults to on some networks.
An operator who dismisses the prompt gets a listener that binds successfully and
is unreachable from every other machine — `ready` will say `listening` and be
telling the truth, which is why the firewall is named here rather than inferred
from a connection failure. A pinned `remote_gateway.port` is what makes a
`netsh advfirewall` rule writable in advance; an ephemeral port cannot have one.
**Undriven by any test in this repo**, and by nothing else either — see the
Stage 1 honest gaps in
[planned/remote-gateway.md](planned/remote-gateway.md).

### 1.2 The peer hello — the same door, a second kind of caller

Since the remote gateway's Stage 6 the gateway listener admits a second
credential: a paired INSTALL, not a paired device. `gateway/peers.json`, beside
`devices.json`, one row per edge — `{peer_install_id, display_name, endpoints,
cert_fingerprint, secret_verifier, approved_at, last_seen, revoked,
revoked_at}`.

The peer hello names `peer_install_id` — **the DIALER's own install id**, the
name it asks to be recognised under — and proves
`HMAC-SHA256(key=secret_verifier, msg="pwv1|<port>|<peer_install_id>|<nonce>")`
(`gateway_peers.peer_proof`). A hello naming `peer_code` instead redeems a
short-TTL code minted by `harness gateway peers pair` on the OTHER install, and
carries the joining install's own name, endpoints and fingerprint so the edge
can be dialled back; the minted secret rides that one `hello_ok` in a `peered`
block and never again.

**Both ends store `sha256(secret)` and key the HMAC with it directly.** That is
the difference from the device lane, where a phone holds a token and digests it
per connection, and it is what makes the edge symmetric: either install can dial
the other with the row it already holds. The install id in the message is what
that symmetry then requires — with one key and one nonce, A's proof to B and B's
proof to A would otherwise be identical bytes and a relay could bounce one back.
The `pwv` prefix (against the device lane's `gwv`) means a device proof does not
verify as a peer proof even at equal contract numbers.

**Exactly one credential per hello, counted rather than ranked.** A frame naming
two of `pairing_code` / `peer_code` / `peer_install_id` / `device_id` is refused
(`serve.py::_credential_kind`); the sole exception is a join's `peer_code` plus
`peer_install_id`, where the code is the credential and the id is the name being
claimed under it. Counting rather than a precedence, because a rule like "a peer
beats a device" is one refactor away from picking the more privileged one.
Neither door accepts the other's credential in ANY direction: the root token is
refused on the gateway lane, a pairing code on loopback, a device credential on
the peer field, a peer credential on the device field, and each code in the
other ceremony's verb.

**A peer holds an ALLOWLIST, not a tier** (`call_authorization.PEER_METHOD_ALLOWLIST`
= `{peer.ping, peer.agent_chat.execute}` since gateway Stage 7), and the arm
runs BEFORE the read-tier arm — which is open to
every caller, so a peer evaluated after it would inherit the whole read surface.
This is where canon 06's exclusion ("agents never mint or retire agents on
another install") stops being a sentence: `runtime.agent.create` and
`runtime.agent.retire` are absent from the peer surface because EVERYTHING is
absent unless it is in the set, and a test iterates the whole registry against
it rather than naming those two. A peer is refused every other method with the
same typed `data.reason: "scope_denied"`, and inherits Stage 1's two door
refusals (`drain`, the argv lane) because both key on the LANE and not on the
device stamp.

The four operator verbs are `harness gateway peers pair | join | list | revoke`.
**Every edge is approved on both sides (R5)** — not by a flag on a row but
because neither half of the ceremony can be performed by the other install: A
never learns B's address until B dials, and B cannot mint a code in A's store.
Revocation is likewise one-sided; reaching across would be one install writing
into another's credential store.

**Peer dialling reads endpoints from the PAIRING RECORD and never from a
registry file** (`gateway_peers.dial_peer`): the serve registry names ports on
the local machine, so a cross-machine read of it is not stale but impossible.
Staleness is R8's retry posture, built in Stage 7. Acceptance:
`tests/agent_runtime/test_gateway_peer_two_roots_e2e.py` — two real serve
children, two isolated roots, both CLI verbs, `peer.ping` A→B.

### 1.3 What an edge CARRIES — cross-install chat (gateway Stage 7)

An agent on install A addresses an agent on install B by qualifying the target
it already knows how to write: `@install_name/persona_or_instance`
(`agent_runtime/gateway_targets.py`, ruling R4). **Unqualified is local,
forever**, and that is a property of the parser rather than a default — a value
with no `/` never reaches the peer resolver at all. Ids outrank display names
and an ambiguous name is refused with both candidate ids, because names default
to the hostname and two roots on one machine really do collide.

**The dispatch row lives on the SENDER install.** `agent_chat_send(wait=false)`
records it exactly as it does for a local target, and the supervisor
(`tools/agent_chat_dispatch`) substitutes a peer-tier call for the child
PROCESS it would otherwise spawn — `peer.agent_chat.execute`, whose ack is an
ACCEPT and whose turn's frames ride the per-request lane the local launcher
already reads. B records the executed turn in its own chat store like any
inbound agent message. There is no distributed row and no two-phase state.
Cross-install sends are the DETACHED lane only: `wait: true` on a qualified
target is refused `remote_requires_detached`.

**Who asked comes off the CONNECTION.** B derives the calling install from
`context.caller.peer_install_id` — set only for a connection whose peer HMAC
verified — and lowers it to `--requested-by peer:<install_id>`. There is no
params key by which a peer names a different install; a correlation token
arrives by the other route precisely because a token is correlation and never
identity.

**Transport retries, an answer settles** (R8). A failed dial costs one of
`MAX_DELIVERY_ATTEMPTS` with a bounded per-attempt dial timeout; an install that
ANSWERED with a refusal settles the row immediately. The cap converges to a
terminal `error` completion carrying `peer_unreachable` — delivered, not
`dropped`, because "the other machine is not answering" is the fact the sender
most needs. Retrying is safe because `turn_request_id` is derived from the
dispatch id: B's reservation replays rather than running the agent twice.
Acceptance: `tests/agent_runtime/test_gateway_peer_cross_install_chat_e2e.py`.

**Known gap:** the relay chain does not cross an install boundary (it would be
an assertion the far side cannot check), so a cross-install dispatch is a fresh
chain root on B and **A→B→A across two installs is not detected as a cycle**.

## 2. Capability advertisement — `rpc` and `ops`

A durable service outlives the install it was started from, so "what does the
thing I am attached to carry" must be answerable at any time. Two manifests ride
`ready` (stdio), `hello_ok` (socket), and the re-askable `version` reply:
`"rpc"` from `serve_rpc.manifest()` (`agent_runtime/serve_rpc.py`) and `"ops"`
from `ops_manifest(transport=…)` (`serve.py::ops_manifest`),
`{"contract", "transport", "ops", "subscribe_lanes"}`.

`ops` answers PER TRANSPORT because the answer genuinely differs, and there are
now three answers rather than two: `shutdown` is stdio-only (it is the verb of
the process that owns the pipe), and `drain` is additionally absent from the
`gateway` answer — it ends the runtime for every other attached client, which a
paired device does not get to decide even at `console` tier. A device learns
what it may ask by MEMBERSHIP; the manifest and the dispatcher cannot disagree,
because both read one tuple (`OPS_EVERY_TRANSPORT` minus `OPS_GATEWAY_DENIED`).

**The `rpc` roster is THIRTEEN methods, and each is named by its handler** —
every one registered by a `@method("…")` decorator in `serve_rpc.py`, which is
the only registration site, so this list is
`grep -n '@method(' agent_runtime/serve_rpc.py` and nothing else:

| Method | Handler | Domain |
|---|---|---|
| `runtime.office.get` | `_runtime_office_get` | read the level |
| `runtime.office.subscribe` | `_runtime_office_subscribe` | office push lane on |
| `runtime.office.unsubscribe` | `_runtime_office_unsubscribe` | office push lane off |
| `runtime.office.upsert` | `_runtime_office_upsert` | place / move |
| `runtime.office.remove` | `_runtime_office_remove` | delete |
| `runtime.office.surface.update` | `_runtime_office_surface_update` | folder taxonomy |
| `runtime.office.resolve_conflict` | `_runtime_office_resolve_conflict` | realm-sync resolve |
| `runtime.agent.create` | `_runtime_agent_create` | roster row + chat root + actor |
| `runtime.agent.retire` | `_runtime_agent_retire` | the inverse of the above |
| `runtime.persona.prewarm` | `_runtime_persona_prewarm` | warm a persona |
| `runtime.chat.message` | `_runtime_chat_message` | send one mission-chat turn |
| `runtime.chat.steer` | `_runtime_chat_steer` | steer the running turn |
| `peer.ping` | `_peer_ping` | is the install⇄install edge alive |
| `peer.agent_chat.execute` | `_peer_agent_chat_execute` | run one chat turn for a paired install |

**Row count corrected 2026-08-27 (gateway Stage 6).** This table said TEN and
listed ten while the runtime had twelve: Stage 3's `runtime.chat.*` pair landed
in the manifest and in four literal pins but not here. The two rows above are
that correction, made while adding the thirteenth rather than filed for later.
**Fourteen since gateway Stage 7** added `peer.agent_chat.execute` (§1.3).

`peer.ping` is the first name outside the `runtime.*` family, and the prefix is
a declaration: `runtime.*` verbs act on this install's level — read it, mutate
it, or run an agent on it — while `peer.*` verbs are about the EDGE between two
installs and touch no level at all. A client can tell them apart without
consulting a table, which matters most for the one surface an operator on
another machine is asked to trust.

**Each method also declares a TIER, and the map rides `rpc` beside `methods`**
(`"tiers": {"runtime.office.get": "read", …}`, hermes `8d69f8858b`). The
classification rule is one line: a level MUTATION is `console`, everything else
is `read` — so the four office writes and both agent verbs are `console`, and
`get` / `subscribe` / `unsubscribe` / `prewarm` are `read`. `prewarm` sits with
the reads because its own contract is that it writes no store state, emits no
event and mints no id; spending CPU is a rate-limiting question, and rate
limiting is not a tier.

The tier is REQUIRED at the registration — `@method(name, tier=…)` has no
default, so a tierless method is unrepresentable rather than merely untested,
and an unknown tier raises at import. Adding the block moved no contract
integer: it is a key beside `methods`, not a shape change to any existing
method's request or result, which is exactly what the set-plus-integer rule
below permits. The launcher reads it (`MissionRuntimeRpcManifest.tiers`) and
branches on nothing — a manifest says what a call WANTS, never what a connection
HOLDS. The enforcement is `serve_rpc.handle_request`'s gate; see
[06 §authorization](06-office-and-board.md).

**The tier is now a refusal, and what it refuses is a paired device** (Stage A5,
landed with the gateway's Stage 1). `authorize_call` compares the tier a
device's record HOLDS — read off `gateway/devices.json` by the transport, fixed
at a pairing ceremony only an operator at this install's own machine can run —
against the tier the verb DECLARES, and answers a `read`-tier device asking for
a `console` verb with the typed `data.reason: "scope_denied"` the launcher's
decoders already branch on. Nothing local moved: `local_console` and
`stdio_owner` are grandfathered exactly as A3 left them, because a device's
authority was added BESIDE that set rather than folded into it — two callers
whose authority comes from different kinds of fact should not share one
membership test. Reads stay open to `unknown`, deliberately and not by
omission: nothing on the read side mutates a level.

**A PEER is refused by an allowlist rather than by a tier** (Stage 6, §1.2).
`PEER_METHOD_ALLOWLIST` is `{peer.ping, peer.agent_chat.execute}` (Stage 7
widened it by one, §1.3, with the reason in a comment beside the set) and the
arm runs before the read-tier
arm — which is open to every caller, so a peer evaluated after it would hold
this runtime's entire read surface including verbs nobody has written yet. The
choice of an allowlist over a third tier word is about what happens as the
registry GROWS: a tier comparison admits every future verb declaring that word,
where a membership test admits nothing it was not edited to admit. So canon 06's
exclusion holds by construction, and the test that pins it iterates
`method_names()` rather than naming `runtime.agent.create` and
`runtime.agent.retire` — a rule pinned by two literals stops being pinned the
moment a third verb arrives.

`peer.ping` itself declares `read`, and that is not a lie to device or console
readers. The map answers what a call WANTS, never what a connection HOLDS, so a
verb that reads no store, writes nothing and mints no id belongs with
`runtime.office.get`; a read-tier device may indeed call it. The allowlist
NARROWS the peer lane and does not widen this row. A third tier word only one
caller kind could hold would put a value in the map every existing reader must
be taught to ignore.

`peer.agent_chat.execute` declares `console` by the same rule read the other
way: a chat turn runs an agent with tools, so anything softer would be a door
around `console` — the same answer `runtime.chat.message` gives. The tier is
still not what admits a peer, and the verb refuses a non-peer caller with its
own `peer_identity_required` rather than `scope_denied`: the chokepoint DID
admit a console caller, and it is the VERB that has no provenance to run under.

This table used to be a list of ten LINE NUMBERS. Two of them (`2055`, `2115`)
had already rotted by 2026-08-27 — a slice landing above them moved both — while
the sentence around them still read as verified. A method name is what a client
sends and a handler name is what `@method` binds it to; neither moves when
somebody edits the function above.

Both follow one discipline: **a set plus an integer.** The set grows when a verb
is added — a client only ever sends a verb it FOUND — and the integer moves only
when an existing verb's shape changes incompatibly. `ops` is answered **per
transport** because the answer genuinely differs: `shutdown` is the stdio
owner's verb and is refused on the socket, so `OPS_STDIO_ONLY` (`:289`) is
unioned in only for stdio, and the block names the transport it describes so a
cached copy cannot be mis-applied.

**The push gate is `subscribe_lanes`, not `ops`.** `"subscribe" in ops` says the
op is dispatched; `"stream" in subscribe_lanes` says THIS lane is carried
(`SUBSCRIBE_LANES = ("stream",)`, `:295`). A runtime predating the advertisement
carries no `ops` key — read that as "undiscoverable, keep the existing lane",
not as a failure. The advertisement exists because a probe cannot distinguish
"too old" from "refused THIS subscribe": `unsupported_lane`, `draining` and
`already_subscribed` are all real answers a live lane gives (`:2595-2651`).

**A third block rides the same three frames, and it is not a manifest.**
`"install"` — `{install_id, display_name, state}`, resolved once per boot from
`agent_runtime/gateway_identity.py` and echoed on `ready`, `hello_ok` and every
`version` re-ask — answers *which runtime you reached*, where `rpc`/`ops` answer
*what it can do*. It rides the greeting for the same reason they do: a client
that must ask a second question has a window in which it does not know, and from
the remote gateway's Stage 2 a client on another machine has no `runtime_root`
path it can interpret. It is a pair of strings, not a set plus an integer, so
nothing negotiates on it and its arrival moved no contract integer
(`SERVE_SCHEMA_VERSION`, `RPC_CONTRACT_VERSION` and `OPS_CONTRACT_VERSION` are
all unchanged by it). Three rules hold it:

- **It names; it never authorises.** The per-root `serve_auth` token proves a
  caller may talk to this runtime, and the device/peer tiers will do so remotely.
  An id that did both is how "I know your install id" becomes "I am you".
  Nothing secret is reachable from the block.
- **It states its own outcome.** `state` is `loaded` | `minted` |
  `error:<reason>`, so a runtime that could not write its identity is
  distinguishable from one that predates the lane — which absence alone cannot
  say. Same rule as `auth.token_file` and `socket.outcome`.
- **It is resolved at boot, not per frame.** A `version` re-ask echoes the block
  the greeting carried, so an operator rename mid-session cannot make a later
  frame disagree with the handshake a client correlates against.

The record is `<store_root>/gateway/install.json`, per store root and
mint-iff-absent — deliberately NOT `monitoring.install_id` (home-scoped and
rotatable by design) nor the telemetry `install_id` (an anonymity primitive).
The argument is in the module's own docstring; the staged plan is
`planned/remote-gateway.md`.

## 3. The mission-control stream

`agent_runtime/stream.py::stream_frames` (`:841`) is the single producer body.
It yields exactly one `hydrate`, then tails the event log from that frame's
`watermark.event_offset` (`_resume_offset`, `:305`) emitting `patch`, full-core
`delta`, and `heartbeat` frames. **An unknown resume position is not byte 0**:
`_resume_offset` returns `None` rather than folding a missing watermark key and
an explicitly-unknown one into `0`, because tailing from `0` replayed the entire
event log as fresh activity at the root of every Mission Control surface
(`:883-889`).

**Freshness backstop.** Every store mutation is supposed to append an event; a
write that slips the rule would freeze watermark-gated consumers forever, since
they drop same-offset re-hydrates. At heartbeat cadence the loop fingerprints
the scope/catalog state no evented store guards, and a fingerprint change with
no offset advance appends a synthetic `state.reconciled` that flows out as an
ordinary full-core delta (`_scope_fingerprint`, `:1194`;
`_append_state_reconciled`, `:1346`). SLO: staleness ≤ 2× heartbeat interval.
Every `state.reconciled` names a producer bug to fix at source.

| Frame | Builder | `schema_version` | Carries a core? |
|---|---|---|---|
| `hydrate` | `hydrate_frame` (`:223`) | 1 | yes — the full snapshot under `core` |
| `delta` | `delta_frame` (`:376`) / `delta_batch_frame` (`:393`) | 1 | yes — ONE core per batch |
| `patch` | `patch_batch_frame` (`:456`) | 2 | **no** |
| `heartbeat` | `heartbeat_frame` (`:328`) | 1 | no |

`hydrate` carries `core`, `identity_map`, `watermark`, and the parity envelope's
`completeness` / `drops` / `parity_warnings`. With the patch lane on it also
carries `delta_patches: true` — the signal to RETAIN this frame's raw core as
the patch base — and the ACCEPTED `fold_entities`, sorted (`:291-293`). Both are
absent when the flag is off, so a flag-off hydrate stays byte-identical.

`delta_batch_frame` carries the SAME core exactly once per drained batch — the
old loop shipped one `build_snapshot()` per event, a ~9MB rebuild per append.
Its shape is strictly ADDITIVE over the single-delta shape: `watermark`/`seq` at
the FINAL offset so a `>`-only sequence gate applies the batch once,
`entity`/`op` still the LAST event for pre-batch consumers, `events` +
`coalesced_count` the additions (`:396-412`). Cap `_DELTA_BATCH_CAP = 256`.

`patch_batch_frame` carries op-based wire patches and **no core**. `base_offset`
is the watermark the batch applies FROM — the client folds only when its held
watermark equals it, and a mismatch is a sequence gap → checkpoint resync;
`watermark.event_offset` is the post-batch offset the fold advances to
(`:456-510`). **A frame that advances a watermark must carry the state that
justifies it**, which `batch_carries_patch_rows` (`:424`) guards: coverability is
decided per event, so a covered domain event whose paired `state.patched` never
arrived is coverable ON ITS OWN, and the frame that used to ship was
`{"type":"patch","patches":[],…}` — the client advanced its watermark having
folded nothing, with no downstream gate able to see it. Five producer paths
reach that case, so the guard sits at the drain, and `patch_batch_frame` refuses
to BUILD it too (`:493`).

`heartbeat` is liveness only. `offset=None` is the honest heartbeat of a stream
that could not read the log's tail and must not be stamped `0`, which every
watermark-gated reader would take as a real cursor at the head of the log
(`:337-341`). A heartbeat whose offset is AHEAD of the consumer's last applied
state-bearing frame proves a frame was missed: keep the applied watermark and
rehydrate.

`_delta_op` (`:1375`) is a four-arm table — `run.tool.*` / `run.progress` →
`chat.trace.appended`, `incident.*` → the source type verbatim,
`persona_assignment.*` → `instance.upserted`, everything else →
`event.appended`. `_redaction_safe_json` (`:1426`) makes payloads JSON-safe:
lists truncated to 200 items, secrets rewritten via the single-homed
`redaction.ENV_SECRET_ASSIGNMENT_RE`.

## 4. Patch frames and the fold negotiation

The producer half is `agent_runtime/state_patches.py`. With
`read_model.delta_patches` on, a store chokepoint mutating a keyed entity
appends a `state.patched` event carrying a **wire-level op** — `upsert` (with
`changed`), `remove`, or `refresh` (`:96-98`). The projection is hermes': at
emit time the changed entity goes through the exact per-entity projection
`snapshot.py` uses, so the launcher folds projected fields verbatim and never
re-derives. One authority, no per-field allowlist (`:1-32`).

The lane ships ON — `SHIPPED_DELTA_PATCHES = True`
(`agent_runtime/runtime_config.py:73`). A config FAULT lands on
`FALLBACK_DELTA_PATCHES = False` (`:82`), off AND loud, because "emit a new
class of event from every store chokepoint" is not what a runtime should infer
from silence it could not read. Sizing is capped by
`EVENT_PAYLOAD_LIMIT_BYTES = 4096`: an oversized `changed` value becomes an
accounted `{"oversize": true, "bytes": N}` marker, and if the payload still
overflows the patch degrades to `op: "refresh"` — accounted, never a partial
merge the client cannot vouch for (`PATCH_VALUE_BUDGET_BYTES`, `:115-116`).

**Capability negotiation.** A consumer's fold is only as wide as its own entity →
core-section table, and a `patch` for an entity it cannot fold is strictly WORSE
than the `delta` it replaced — the consumer pays the patch and then a whole core
anyway. So it declares: `hermes harness stream --fold-entities a,b`
(`hermes_cli/harness.py:1367`, parsed by
`agent_runtime/patch_coverage.py::parse_fold_entities_option`, `:404`)
or `{"op":"subscribe","lane":"stream","fold_entities":[…]}` (`:2623-2631`).
Rules, all in `agent_runtime/patch_coverage.py`:

- **Omitting the declaration means `HISTORICAL_FOLD_ENTITIES = {persona_instance,
  incident}`, not the empty set** (`:355`) — absence is what every fielded client
  sends and that set is exactly today's wire, so defaulting to empty would have
  demoted every connected client to full cores forever. An EXPLICIT empty
  declaration is honoured as empty (`normalize_fold_entities`, `:363`).
- **The shared producer accepts the INTERSECTION**, never the union
  (`accepted_fold_entities`, `:380`): one producer fans every frame to every
  subscriber, so a promotion must be safe for everyone in the room.
- **The accepted set is echoed, not assumed** — on the `subscribed` ack
  (`serve.py:2737`) and again on the hydrate (`stream.py:293`). A client can be
  honoured for strictly less than it asked for.
- **A malformed declaration is REFUSED**, not read as absent (`subscribe_denied`
  / `invalid_fold_entities`, `serve.py:2633-2639`) — a client that meant to
  narrow and was silently widened back would get patches it cannot fold.
- A batch naming any undeclared entity is demoted IN FULL to a core-bearing
  frame; there is no partial patch frame (`_batch_frames_with_liveness`,
  `stream.py:795-838`). The demotion bills `snapshot_build reason=demote`
  (`BATCH_REASON_DEMOTE`, `:64`), which makes a foldable update that paid for a
  whole snapshot greppable.

Negotiation makes a new entity POSSIBLE; it does not enable one. The producer
still emits `state.patched` only for entities its chokepoints cover.

## 5. Attachment receipts

`log_stream_attach` (`stream.py:176`) writes ONE line per attachment to the
shared producer, at subscribe time, into the serve child's own `agent.log`.
Three call sites attach a reader to the same producer, and until this line
existed the log named none of them:

| Caller | `op` / `purpose` | Site |
|---|---|---|
| socket/stdio op lane | `subscribe` / `stream_lane` | `serve.py:2712` |
| RPC office lane | `runtime.office.subscribe` / `office_patch` | `serve_office_subscriptions.py:902` |
| argv CLI | `harness_stream` / `cli_stream` | `runtime_commands.py:507` |

`op` is the call as the client made it, `purpose` is what the attachment is FOR
— neither implies the other. `pid` rides LAST here and on both build families
(`snapshot_build`, `snapshot_build_core`), so an attachment and the builds it
paid for join on one key instead of on wall clocks (`stream.py:108-124`). It
never raises — an instrument must not be why a subscribe fails.

**Who paints the boot's one stale core is a property of the ROOM**, so
`stream_frames(wants_stale_first=…)` is stated by the caller —
`serve.py::_room_wants_stale_first` (`:1954`) reads the hub's two subscriber
tables at producer-build time, `_cmd_stream` (`runtime_commands.py:544`) states
`True`, default `False`. It cannot be re-derived inside the producer: the
subscriber attaching FIRST at boot is the RPC office lane, whose sink discards
every non-`office_actor` row, and measured 2026-08-18 two boots in three handed
the stale paint to that sink (`stream.py:897-913`).

## 6. The office push lane is a re-envelope, not a second derivation

`agent_runtime/serve_office_subscriptions.py` registers with the SAME
`StreamHub` every stream client uses, under a sink re-wrapping the frames it
cares about as JSON-RPC notifications. It adds no derivation, and three
consequences followed: one producer, so a patch cannot exist on one lane and not
the other; an RPC subscriber COUNTS as a subscriber, so the hub does not stop
producing under it; backpressure and drop accounting inherited whole (`:1-24`).
What crosses is decided by `state_patches.office_patch_scope`, the ONE authority
for "is this row in this workspace" — single-homed because it was not, and the
fork cost a silent drop when `office_surface` became promotable with a BARE
workspace id that failed the private "id under `<workspace_id>/`" restatement
(`:26-45`). A `hydrate` or `delta` means the batch was not patch-coverable, so it
becomes a resync notification; an UNKNOWN frame type takes the same branch
deliberately. Drops are typed, never silent: a subscriber outrunning its bounded
buffer gets `subscription_dropped` naming which of the two bounds tripped —
frame count or bytes — then is unsubscribed (`serve.py:2676-2689`).

## 7. The PUSH-vs-RPC boundary, and the fork boundary

**The 2026-08-13 ruling stands: this fork owns the better PUSH lane; upstream
owns the better CALL lane.** The push half is live-on-by-default —
`SHIPPED_DELTA_PATCHES = True`, the hub lane advertised on every greeting, the
launcher gating on `subscribe_lanes`. The call half deliberately mirrors
`tui_gateway`'s JSON-RPC 2.0 shape and error codes rather than minting a third
convention, and sits BESIDE the argv lane: a frame is claimed by the method lane
only when it names `jsonrpc` or `method`, neither of which an argv request has
ever carried (`serve.py:110-115`).

**The fork boundary is the reverse of the natural assumption: `agent_runtime/`
is not in upstream at all.** The check that proves it is a path filter, not a
judgement call — `git diff --name-only <baseline>..HEAD` minus
`^(agent/|agent_runtime/|hermes_cli/harness)` and minus
`^tools/(agent_chat_tool|mission_goal_tool)\.py$` — and anything it surfaces
needs a ledger row. Archived doc 17 carries the rows; doc 18 carries the merge
rehearsal that priced them — 59 conflicted files in its first table, 63 on the
same-day re-rehearsal (`:151-153`), and only one named by doc 17 either way, so
the next sync is not a two-file merge. One transport-relevant row:
`scripts/generate_agent_runtime_stream_fixtures.py` is fork-created and
fork-only by construction, and sat in upstream-owned `scripts/` with no row
until it was filed.

## 8. The launcher-side consumer contract

`tests/fixtures/stream_frames/` holds fourteen manifest-pinned files. The
EterniaLauncher repo commits **byte-identical copies** under
`test/fixtures/harness_stream/` and parses them through its real decode +
read-model pipeline (`mission_stream_contract_fixture_test.dart`). Verified
2026-08-22: the two `MANIFEST.sha256` files are identical. Seven are generated
by `scripts/generate_agent_runtime_stream_fixtures.py` from a **seeded isolated
runtime root** through the current production builders, never from a live store;
the rest are hand-maintained and validated by shape + live-classifier agreement
(`tests/agent_runtime/test_stream_patch.py`) because they demonstrate fold
semantics over entities the seeded root does not contain. `patch_remove.json` is
un-emittable today — the `incident.closed` remove fold, de-registered with its
last writer, kept classifying by
`patch_coverage.HISTORICAL_COVERED_DOMAIN_EVENT_TYPES` (`:204`).

**Update rule: these fixtures change only in a cross-stack change that lands
hermes AND the launcher together** — regenerate, copy bytes, update both
manifests in the same change. Membership AND ORDER are compared before bytes by
the launcher's `tool/test_quality/check_producer_contracts.py` (that checker
lives in the LAUNCHER repo, invoked with `--hermes-root`), so a new row goes in
at the same position on both sides.

**The additive-only wire rule — why `sections_ms`-style keys must never ride the
parity envelope.** `sections_ms` rides the parity envelope, which rides the
hydrate frame, which is byte-pinned by these goldens AND by the launcher's
mirror. Two keys there for an observability nicety would be a cross-stack
fixture landing, and a hermes-only half of one is exactly the failure this repo
already paid for: hermes green, the launcher's byte-compare red on every push.
So the readiness-split attribution went to a LOG RECEIPT (`sections_top` on
`snapshot_build_core`) and `agents_readiness` kept its exact span, meaning and
key (`agent_runtime/snapshot.py:824-837`). The same discipline keeps
`correlation_id` an UNREGISTERED optional payload key — the contract hash
derives from the registry rows alone, so registering it would shift
`decision_contract_hash` inside every generated core.

## 9. Delivery directives — a name that no longer describes a transport

`agent_runtime/delivery_directive.py` is 222 lines with no wire surface. Its
transport-shaped half — a directive DECLARED on a goal-create request, stored on
the `Task`, executed at terminal settle by `ArchiveStore.archive_tasks` — went
with the mission lane; S24 swept the executors, the declaration path, and the
delivery-time patch capture that had no producer left. What remains is
`reap_orphan_worktrees` (`:38`), a capture-then-reap janitor driven by
`hermes harness worktree reap` and `harness doctor --fix`. Nothing is deleted
with an uncaptured diff: dirty candidates go to `<store_root>/wt_reaped_patches/`
under collision-proof exclusive-create names (`:162`). One registered event,
`worktree.orphans_reaped`.

## 10. MCP transport

MCP tool registration (`tools.mcp_tool.discover_mcp_tools`) is wired to the CLI
entry points that host a model turn against the operator's full MCP surface —
bare `hermes` / `chat` / `acp` / `rl`, plus `cron run|tick`, `gateway run`,
`mcp serve`. **`hermes harness …` is deliberately excluded and must stay
excluded**: the harness lane is a fast, deterministic control plane and cannot
pay an MCP connect budget on every command (`agent_runtime/mcp_lane.py:1-20`).
Admission is therefore per-RUN and per-PERSONA via
`register_mcp_servers({name: cfg})` over an explicitly resolved subset, and two
transport facts follow. Registration is **single-flight**, because
`tools.registry` and `tools/mcp_tool._servers` are process-global while a serve
process is multi-persona (`ThreadPoolExecutor(4)`) — interleaved admissions are
refused as `mcp_admission_lane_busy` rather than raced. And **the registry scope
belongs to the run while the transport belongs to the process**:
`teardown_mcp_admission` removes the admitted tools at every admitted run's end
while the connection in `tools/mcp_tool._servers` stays warm for the next
(`agent_runtime/mcp_admission.py:38-52`). Admission POLICY belongs to the
chat-lane doc. Relay hops are gated by `agent_runtime/relay_policy.py` — one
authority, so in-process tool relay, CLI and serve transport get the same depth
/ cycle / budget answer from explicit envelope data rather than inference.

## Invariants

1. **Parse per line, branch on `type`, ignore unknown fields.** Hub-vs-argv
   parity is a property of the DECODED frame, not the line — `harness stream`
   writes compact separators, `_FrameWriter` json's defaults
   (`test_serve_stream_lane_parity.py`).
2. **Every wire addition is additive**; byte-pinned frames move cross-stack.
3. **Observability never adds a key to a BYTE-PINNED surface** — an operator's
   number belongs on an `agent.log` line, not on the parity envelope the stream
   goldens and the launcher's mirror compare byte-for-byte. Additive keys on
   surfaces that are NOT byte-pinned are the deliberate exception: `phases` on
   the create RPC's result and on the turn record are both client-visible and
   both allowed (`agent_create_phases.py:14-21`).
4. **Absence and emptiness are different statements** — for `fold_entities`, for
   `watermark.event_offset`, for a heartbeat with no position. Collapsing either
   pair has cost a live incident each time.
5. **A frame that advances a watermark carries the state that justifies it.** No
   empty `patches` list, ever.
6. **A shared producer promotes on the INTERSECTION of its room's declarations**
   and echoes what it accepted; widening it may only name entities every
   fielded client already folds.
7. **The harness lane never registers MCP globally.** Admission is per-run,
   single-flight, torn down at run end.
8. **`agent_runtime/` is fork-only.** Any edit outside
   `agent/ | agent_runtime/ | hermes_cli/harness*` needs a boundary-ledger row.
9. **A listener beyond loopback is opt-in per install, forever**; it is TLS-only
   (a certificate that cannot be minted means the lane does not open — never
   that it opens in the clear); and it accepts only per-device credentials. The
   loopback lane's host is not a knob and does not become one: widening exposure
   is a different listener with a different credential story, not a different
   value in `SOCKET_HOST`.
10. **A credential appears in exactly one frame, once** — the `hello_ok` that
    redeemed the pairing code that minted it. It is absent from
    `SocketConnection.payload()`, from every log line, and from `DeviceRecord`,
    which has no field for it by construction.

## Open rows

- **Single-transport collapse is half-done** — the hub lane is primary and
  gated; the argv backstop's deletion waits on an observation window
  ([planned/single-transport-collapse.md](planned/single-transport-collapse.md)).
- **Correlation-id coverage is partial** — the office lane and
  `runtime.agent.create` carry the token, the remaining argv capability lanes do
  not, and the one-grep acceptance was never scripted
  ([planned/correlation-id-coverage.md](planned/correlation-id-coverage.md)).
- **The remote gateway is planned and unstarted** — the socket lane is the contract
  a future device/peer tier makes bindable beyond loopback (install identity,
  `gateway.listen`, pairing, cross-install `agent_chat_send`); gated on the primary
  plan's rulings R1–R13, zero answered as of 2026-08-27, with Stage 1 additionally
  blocked on the authorization chokepoint (doc 06's Open row)
  ([planned/remote-gateway.md](planned/remote-gateway.md)).
- **`serve.py:105` says the RPC registry holds "currently eight" methods; ten
  are registered.** That same docstring warns "a docstring that copies a
  register starts lying the first time the register moves" — it has.
- **Two module docstrings cite doc paths that moved into `archive/`** —
  `delivery_directive.py:8` → `delivery-directive.md`, `serve.py:9` →
  `harness-serve-design.md`. Both pointers are dangling.
- **`tests/fixtures/stream_frames/README.md` describes thirteen of its fourteen
  pinned files** — `patch_office_actor.json` has no row in the origin table.

## Unverified carry-forward

- **The persona-chat event lane's two 2026-08-09 properties** — that the
  auto-title now runs AFTER the chat-root lease releases (so
  `persona_chat.projected` and `persona_chat.metadata_updated` for one turn are
  separated by a whole auxiliary-LLM round trip, and the root accepts a send in
  between), and that `persona_chat.send_refused` omits the message text because
  the sanitising chokepoints sit inside the lease the refusal never took. All
  three types are registered (`decision_contract_registry.py:184-196`) and
  emitted from `persona_commands.py` (`:1593`, `:1640`, `:1727`), but the
  ORDERING claim and the omission RATIONALE were not re-verified against the
  lease code. Source: `…/mission-control-stream.md:361-383`.
- **The measured patch-lane saving** — 486 bytes against an 822,671-byte core,
  the ~99.96% reduction the S6/S7 acceptance names. Cited at
  `patch_coverage.py:347` and `stream.py:459-462` as 2026-07/08 measurements;
  not re-measured here, so a historical figure, not a benchmark.

## Supersedes

`planned/agent-placement-verb.md` — **deleted 2026-08-27 by the S10 fold-in
commit** (`git log --diff-filter=D --oneline -- docs/agent-runtime-harness/planned/agent-placement-verb.md`
recovers it). Its wire half is above and in 06: `runtime.agent.create` gained an
optional `position`, a `skills` param and four ack keys, and
`runtime.agent.retire` joined the `rpc` roster — all of it additive, with
`RPC_CONTRACT_VERSION` never moving, which is the set-plus-integer discipline
§2 states and the reason a set of ten could become a set of ten with one name
swapped in without a contract bump. Its D12 rollout gate — the launcher reading
`runtime.agent.retire`'s PRESENCE as "this serve accepts an absent `position`" —
is the only place in this canon where a method name is used as a capability
proxy for a PARAMETER; it is recorded as a named risk, not as a pattern to copy.

All others under `archive/2026-08-22-pre-consolidation/`:

| Archived doc | Where it went |
|---|---|
| [`mission-control-stream.md`](archive/2026-08-22-pre-consolidation/mission-control-stream.md) | primary source for §3–§5 |
| [`harness-serve-design.md`](archive/2026-08-22-pre-consolidation/harness-serve-design.md) | the 2026-07-08 settled design behind §1 |
| [`delivery-directive.md`](archive/2026-08-22-pre-consolidation/delivery-directive.md) | removed contract; §9 is the live half |
| [`17-upstream-boundary-ledger.md`](archive/2026-08-22-pre-consolidation/17-upstream-boundary-ledger.md) · [`18-upstream-merge-rehearsal-20260730.md`](archive/2026-08-22-pre-consolidation/18-upstream-merge-rehearsal-20260730.md) | summarised in §7; still the authority for their own detail |
| [`SINGLE_TRANSPORT_COLLAPSE_PLAN_2026-08-16.md`](archive/2026-08-22-pre-consolidation/SINGLE_TRANSPORT_COLLAPSE_PLAN_2026-08-16.md) | landed stages → §2/§5; open half → `planned/single-transport-collapse.md` |
| [`CORRELATION_ID_PLAN_2026-08-16.md`](archive/2026-08-22-pre-consolidation/CORRELATION_ID_PLAN_2026-08-16.md) | landed stages → §8; open half → `planned/correlation-id-coverage.md` |
