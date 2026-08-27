# Planned — the authorization chokepoint

**Status:** not built. Surveyed 2026-08-27. **Owner surface:**
[06 — Office and board](../06-office-and-board.md) § "Authorization is a DECISION
with no enforcement point" and its Open row.
**Blocks:** [remote-gateway.md](remote-gateway.md) Stage 1, and through it the
primary plan's R11 (launcher
`docs/mission_control/planned/universal-remote-gateway.md` §5).
**Everything below is a READ.** No test was run and no serve was started while
this was written; see §5 for what that costs.

The gateway plan's Stage 1 must not bind a non-loopback listener while the write
verbs have no enforcement point, and R11's per-device scopes have nowhere to
hook. This file is the design for that hook, plus the three questions the
operator has to answer before any of it can be built. The rulings are at the
bottom, in answer-in-one-message form.

---

## 1. Inventory — the asymmetry, and what is under it

### 1.1 The decision function

`agent_runtime/coordinator_permissions.py:58` —
`authorize_coordinator_action(action, scope, target_instance, *, actor, coordinator_id)`
returns a `CoordinatorAuthorization(ok, needs_operator_confirm, reason, scope)`.
It is a **pure function over its arguments**: it reads no config, no store, no
connection. Its four action families are module constants (`:8-16`) —
`STEER_ACTIONS` (ungated by construction, `:70-71`), `CREATE_ACTIONS`,
`KILL_ACTIONS`, and everything else, which falls through to
`non_restructure_action` = allowed (`:96`).

Two properties matter more than the branch table:

- **`OPERATOR_ACTORS = {"operator", "tony", "cli"}` (`:16`) short-circuits
  everything** (`:68-69`, `reason="operator_bypass"`) before any scope is read.
- **`scope` is an argument, not a lookup.** A caller that supplies a wide
  `CoordinatorPermissionScope` is authorized by it.

### 1.2 Every call site — eight, all in one CLI module

No module outside `hermes_cli/harness_parts/persona_commands.py` calls it.

| Line | Action | Guard the call sits behind |
|---|---|---|
| `:688` | `persona.instance.create` | `coordinator_id and (display_name or add_instance)` |
| `:837` | `persona.instance.open_chat` | `coordinator_id and add_instance` |
| `:854` | `persona.instance.close` | `coordinator_id and kill_active` |
| `:4827` | `persona.instance.close` | `coordinator_id` |
| `:4864` | `persona.instance.retire` | `coordinator_id` |
| `:5013` | `re_route` | `coordinator_id` |
| `:5088` | `persona.instance.update_profile` | `coordinator_id` |
| `:5258` | `persona.instance.set_model` | `coordinator_id` |

Every row's guard begins `coordinator_id`, and `coordinator_id` comes from
`_coordinator_actor_id(args)` (`:1654`), which returns non-`None` for exactly two
`--requested-by` spellings: `coordinator` (then reading `--coordinator-id`) and
`coordinator:<id>` inline. Every other value — including the absent one —
returns `None` and the gate is skipped.

### 1.3 The asymmetry canon 06 records, and the larger fact under it

Canon 06 records that `harness persona instance retire` consults the gate while
`harness agent retire` does not, on the SAME `perform_agent_retire`. Both halves
check out:

- `_cmd_persona_instance_retire` (`persona_commands.py:4845`) has the gate at
  `:4864`, then delegates to the shared service.
- `_cmd_agent_retire` (`:617`) calls `_agent_retire_outcome` (`:578`) directly,
  which builds a four-key params dict and calls `perform_agent_retire`
  (`:605-614`). No gate, and the module docstring at `:585` names the difference
  out loud — "and in the coordinator gate, which is `persona instance`'s".

**But the gated door is not gated on any traffic anyone sends.**
`persona instance retire`'s `--requested-by` DEFAULTS to `cli`
(`hermes_cli/harness.py:998`), and the launcher's `persona.instance.retire`
capability hardcodes `--requested-by launcher`
(`EterniaLauncher/lib/features/mission_control/data/harness_capability_registry.dart:1517`).
Neither spelling reaches `_coordinator_actor_id`'s two branches, so
`coordinator_id` is `None` and the gate at `:4864` never executes. The
asymmetry is real in the source and invisible in the field: **today both retire
doors are unenforced, and the RPC method is a third unenforced door.**

This is the fact that shapes §2. A chokepoint that simply MOVES the existing
call downward inherits a predicate whose inputs the caller supplies.

### 1.4 The scope is self-asserted too

`_coordinator_scope_from_args` (`persona_commands.py:1663`) seeds from
`scope_for_persona` (persona `autonomy`, plus `runtime_config`'s
`CoordinatorPermissionConfig`) and then lets four argv flags OVERRIDE it:
`--coordinator-max-spawns`, `--coordinator-may-kill-own`,
`--coordinator-no-kill-own`, `--coordinator-may-kill-others`
(`hermes_cli/harness.py:256-261`, added to each parser by
`_add_coordinator_permission_args`). So the request carries both the identity
being checked and the grant it is checked against.

Consequence, stated plainly so no stage is designed around a fiction: **the
current mechanism is an advisory self-declaration protocol, not an authorization
system.** It is well-shaped for what it was built for — a coordinator persona
declaring its own budget so the runtime can refuse it and ask the operator to
confirm (`_coordinator_confirm_payload`, `:1684`, whose whole payload is
`needs_operator_confirm` plus a `next_expected` sentence). It is not a thing a
remote device can be held to.

### 1.5 The service functions — the chokepoint the doors share

- `agent_runtime/agent_retire.py:219` —
  `perform_agent_retire(params: dict) -> AgentRetireOutcome`. One positional
  dict. No actor, no scope, no context. Its docstring (`:252-256`) states the
  `console` scope as prose.
- `agent_runtime/agent_create.py:1205` —
  `perform_agent_create(params: dict, *, updated_by: str = "operator", persona=None)`.
  `updated_by` is an audit/stamp field, not a permission one — it flows to the
  store, and no branch reads it as an identity.

Callers of the two, outside tests and fixture scripts: the two RPC handlers
(`serve_rpc.py:2068`, `:2134`), `_agent_retire_outcome`
(`persona_commands.py:607`), `_cmd_persona_instance_retire`'s delegation
(`:4884`), and `persona_commands.py:540` for the create. That is a genuinely
small set — five production call sites across two functions — which is what
makes option (a) in §2 cheap.

### 1.6 The RPC dispatch layer, and what identity reaches a handler

`agent_runtime/serve_rpc.py`:

- `_METHODS: dict[str, Callable[[Any, dict, RpcContext], dict]]` (`:141`) — a
  flat name→function registry.
- `method(name)` (`:234`) — the decorator; its whole body is
  `_METHODS[name] = fn` (`:236`). **A wrapper is one line away here**, and every
  method on the lane is registered through it (`:455`, `:578`, `:906`, `:967`,
  `:1300`, `:1515`, `:1693`, `:1992`, `:2078`, `:2144`).
- `handle_request(req, context=None)` (`:317`) — normalizes, looks the name up
  (`:337`), calls `fn(rid, params, context)` (`:341`) inside a
  `try`/`except Exception` that turns any raise into `-32000` (`:342-349`).
  `context` defaults to an EMPTY `RpcContext()` rather than `None` (`:328`).
- `RpcContext` (`:186-215`) is `frozen=True` and carries exactly three fields:
  `connection_key: str | None` (`:213`), `transport: str = "stdio"` (`:214`),
  `emit: Callable | None` (`:215`).

**What reaches a handler today.** `connection_key` and `transport` — and the
docstring is explicit (`:206-212`) that `connection_key` is the SUBSCRIPTION
identity, the thing a teardown sweep can name, not an authorization one. It is
built in `hermes_cli/harness_parts/serve.py:3146-3156` from
`getattr(connection, "key", None)` / `getattr(connection, "transport", "stdio")`
/ `sink.emit`. On stdio there is no connection and the key is `None`.

**What the transport knows and does not pass on.** `SocketConnection`
(`agent_runtime/serve_socket.py:575-597`) holds `key`, `peer` (the address
text), `client` and `client_build` (both set from the hello frame at `:1163-1164`
— i.e. self-reported strings), `authenticated: bool` (`:1165`), `subscribed`,
and `transport` (`:597`). The connection is only ever entered into
`self._connections` AFTER `verify_hello_proof` succeeds (`:1147-1167`), so
`authenticated` is a genuine fact and not a claim — the HMAC challenge-response
in `serve_auth.py` is the one identity fact on this lane that is proven rather
than asserted. It proves ONE thing: this peer holds the install's serve token.
It does not distinguish devices, because there is one token.

`hello_ok` echoes `connection.key` back to the client (`serve.py:2389`), so the
key is already a name both sides can say — which is what makes it usable as the
join between a paired device record and a live call.

### 1.7 Summary of what each layer can see

| Layer | Identity available | Proven or asserted | Scope available |
|---|---|---|---|
| CLI handler | `--requested-by`, `--coordinator-id` | asserted (argv) | asserted (argv) + persona `autonomy` |
| RPC handler | `RpcContext.connection_key`, `.transport` | key is proven (post-HMAC); no device granularity | none |
| Socket transport | `key`, `peer`, `client`, `client_build`, `authenticated` | `authenticated` + `peer` proven; `client*` self-reported | none (one install token) |
| Service function | `params` only; create also `updated_by` | asserted | none |

The gateway's Stage 1 adds the missing column: a per-device credential
(`gateway/devices.json`, hashed, `{device_id, name, scope, …}` — primary plan
§3.4) whose `device_id` is named in the hello, which makes `connection_key` a
lookup key into a scope. **That is the whole of R11's plumbing, and it is
blocked only because no layer currently ASKS.**

---

## 2. Where the check should run

Three options. All three assume the same downstream vocabulary work is deferred
(§4) — they differ in where the predicate is evaluated and what it can see.

### Option (a) — enforcement inside the shared service functions

Add a permission argument to `perform_agent_create` / `perform_agent_retire`
(canon 06's Open row already proposes this shape: "a scope parameter on
`perform_agent_create` / `perform_agent_retire`"), evaluated at the top of each,
refusing with a typed outcome in the same `AgentCreateRefusal` /
`AgentRetireRefusal` shape both doors already render.

- **For.** Five production call sites (§1.5), so it is genuinely small. The
  refusal renders identically on argv and RPC for free, because both doors
  already print the same outcome object. It is the only option where a NEW door
  — a future verb, a script importing the service — cannot be added unguarded,
  because the guard is inside the thing being called.
- **Against.** The service functions take a `params` dict off the wire. Giving
  the permission fact the same channel makes it self-asserted exactly as §1.4
  describes; giving it a separate keyword argument means every caller must be
  taught to fill it, and a caller that passes nothing needs a default — and the
  honest default for a function with five callers and no context is "allow",
  which is the hole again. A required argument is the only safe spelling, and
  that is a breaking signature change to two functions with ~40 test call sites
  between them (`tests/agent_runtime/test_agent_create_service.py` alone has 46
  test functions).
- **R11's scopes:** no home. The service has no connection and cannot look one
  up; it can only be TOLD, and being told is what §1.4 already is.

### Option (b) — a wrapper at the RPC dispatch layer, mirrored at CLI entry

A `requires_scope("console")` decorator composed into `method()`
(`serve_rpc.py:234`), evaluating a predicate against `RpcContext` before
`fn(rid, params, context)` runs; plus a mirror at CLI-handler entry, where the
identity is the local console.

- **For.** `RpcContext` is the only place a PROVEN caller fact exists, and the
  registry is one function (`:236`). Refusals are already typed on this lane
  (`err(rid, code, message, data)` with a machine-readable `data.reason`), so a
  `-32000`/`scope_denied` needs no new decoder shape. It composes with the
  manifest: a method's declared tier can ride `manifest()` (`:246`) the same way
  its name does, so a connector learns what it may call without trying.
- **Against.** Two enforcement points that must agree, and the CLI mirror has
  nothing proven to check — the local console IS the trust boundary today, so
  its mirror is a grandfather clause, not a check. A verb reachable ONLY through
  the service (a script, a future internal caller) bypasses both.
- **R11's scopes:** **this is their home.** `connection_key` → paired-device
  record → `scope` is one lookup, and `transport` already distinguishes
  `"stdio"` from `"socket"`, which is the first cut of the device/peer listener
  split Stage 1 introduces.

### Option (c) — both: dispatch-layer as the scope-aware gate, service-layer as the backstop

The decorator in (b) is the gate that KNOWS about devices and scopes. The
service-layer check in (a) is a narrow non-bypassable assertion: not a policy
engine, just "an authorization decision was made for this call", carried as a
required argument whose only values are a decided authorization or an explicit
`LOCAL_CONSOLE` sentinel.

- **For.** The gate lives where the facts are, and no door can be added that
  silently skips it — a new caller that forgets the argument fails loudly at the
  signature rather than quietly at the policy. It also makes the grandfather
  explicit and greppable instead of implicit in an absent check.
- **Against.** Most work, two concepts to keep aligned, and the backstop's value
  depends entirely on the sentinel never becoming the ambient default.
- **R11's scopes:** same home as (b).

### Who needs a grandfathered "local console" identity

Under (b) or (c), these callers exist today, are trusted today, and must keep
working byte-identically on day one:

1. **The local CLI**, all doors — `harness agent create/retire`,
   `harness persona instance *`. The operator at the machine's own shell.
2. **The launcher's argv lane** — every `--requested-by launcher` capability
   (`harness_capability_registry.dart`, nine rows). It reaches the CLI through
   `serve.py`'s argv lane, not the RPC lane, and its trust today is "it dialled
   the loopback socket and passed the HMAC".
3. **The launcher's RPC lane** — the same trust, on the method lane. Under (b)
   this is the caller the grandfather is FOR: `transport == "socket"` with the
   install's own serve token is exactly the local console until device tiers
   exist.
4. **stdio** (`connection_key is None`) — the serve owner's own pipe. Strictly
   more trusted than any socket; must never be refused.
5. **Tests and fixture generators** that call the services directly
   (`scripts/generate_agent_runtime_stream_fixtures.py:813`, and every file in
   §3's test plan). Under (a)/(c) these are the signature churn.

The honest framing for the ruling: **on day one every existing caller is
grandfathered and nothing changes behaviour.** The value of the stage is that a
NEW caller — a paired device — arrives at a place where a decision is made,
instead of arriving at three doors that never asked.

---

## 3. Stages

Smallest first. Each lands and is testable on its own; none binds a listener.

### Stage A1 — name the tier on the methods, in the manifest, without enforcing

Add a declared tier to the `@method` registration (`serve_rpc.py:234`) — a second
registry `_METHOD_TIERS[name]`, defaulting to `console` for write verbs and
`read` for reads — and surface it on `manifest()` (`:246`) as an ADDITIVE block
beside `methods`. Nothing is refused. `RPC_CONTRACT_VERSION` does not move: a
manifest is a set plus an integer, and this adds a key, not a shape change to an
existing method (the rule 03 §2 already states and the D12 rollout gate already
proved).

- **Why first.** It converts canon 06's "a DECISION, not a check" from prose in
  two docstrings (`agent_retire.py:252`, `serve_rpc.py:2119`) into a machine-
  readable fact, with zero behaviour risk, and it gives the connector contract
  something to read before any enforcement exists.
- **Done when.** `manifest()` carries every method's tier; the two agent methods
  read `console`; no method's request or result shape changed.
- **Test plan.** Extend `tests/agent_runtime/test_serve_rpc_agent_retire.py`
  (8 tests today) and `test_serve_rpc_agent_create.py` (46) with a manifest
  assertion. Add a registry-completeness test in the same file as the manifest's
  existing coverage — every name in `_METHODS` has a tier, asserted by iterating
  the registry, not by a hand-written list (the tombstone-census lesson: loops,
  never literals). **Cross-repo caution:** the launcher's serve-frame fixtures
  (`tool/hermes_serve_frames/generate.py --check`) capture `hello_ok`/`ready`
  byte-for-byte and that check is on no CI lane — regenerate and land the
  fixture in the same wave, or Stage A1 breaks a check nobody runs.

### Stage A2 — carry a caller identity into the handler

Add one field to `RpcContext` (`serve_rpc.py:186-215`): a `caller` describing
what the transport PROVED — for the socket lane, `{kind: "local_console",
connection_key, transport}`; for stdio, `{kind: "stdio_owner"}`. Built in
`serve.py:3146-3156` beside the three fields already assembled there. Frozen
dataclass with a default, so every existing construction site (tests, the argv
lane's probes) keeps compiling and gets the honest `None`/unknown value.

- **Why second.** It is pure plumbing with no policy, and it is the argument
  Stage A3's predicate needs. It also stops `connection_key` from being
  overloaded: the docstring is explicit that it is the SUBSCRIPTION identity
  (`:206-212`), and an authorization fact riding it would quietly redefine a
  field two other systems key on.
- **Done when.** Every handler can name its caller; no handler reads it yet.
- **Test plan.** `tests/agent_runtime/test_serve_rpc_notification_lane.py`
  already builds contexts by hand — extend it for the default. Add a
  socket-lane assertion where `test_serve_auth.py` and the serve command tests
  (`tests/hermes_cli/test_serve_command.py`) already stand up a real
  handshake, asserting the caller a real authenticated connection produces.

### Stage A3 — the gate, allowing everything

Compose `requires_tier` into `method()` and evaluate it before dispatch: the
predicate takes `(tier, context.caller)` and, at this stage, returns allow for
`local_console` and `stdio_owner` — i.e. every caller that exists. Refusal is a
typed `err(rid, ERR_HANDLER_FAILED, …, {"reason": "scope_denied", "tier": …})`,
exercised only by a test that constructs a caller kind nothing yet mints.

- **Why this shape.** It lands the ENFORCEMENT POINT with an empty policy, which
  is what unblocks Stage 1 of the gateway plan: R11 then has a hook, and turning
  a device's scope into a refusal is a policy edit, not an architecture change.
  Landing the point and the policy together is how a wave ships a lane that
  cannot be reasoned about one commit at a time.
- **Done when.** Every method dispatches through the gate; every existing caller
  is allowed; one synthetic caller is refused with a typed reason.
- **Test plan.** New `tests/agent_runtime/test_serve_rpc_authorization.py`:
  registry-driven (iterate `_METHODS`, assert each name dispatches through the
  gate — the mutation that removes the decorator must kill a test). Extend
  `test_serve_rpc_agent_create.py` / `test_serve_rpc_agent_retire.py` with a
  refused-caller case each, asserting the `data.reason` string, since that is
  what the launcher's decoders branch on.

### Stage A4 — reconcile the CLI-handler gate with the chokepoint

Land the CLI mirror and resolve §1.3/§1.4 out loud. Three sub-decisions, all
inside one stage because separating them leaves the tree in a state nobody can
describe:

- (i) The eight `authorize_coordinator_action` call sites keep their COORDINATOR
  meaning — a persona declaring a self-budget so the runtime can ask the operator
  to confirm — and are renamed/documented as such, not as authorization. That is
  what they are (`:1684`'s payload is `needs_operator_confirm`, not `denied`).
- (ii) The CLI's own authorization identity becomes `local_console`, uniformly,
  on all doors including the two ungated retire doors. This is the mirror.
- (iii) The `agent retire` / `persona instance retire` asymmetry disappears —
  not by adding the coordinator gate to `agent retire`, but because both doors
  now carry the same `local_console` identity and the coordinator check is
  correctly named as the separate, advisory thing it is.
- **Test plan.** `tests/agent_runtime/test_coordinator_permissions.py` (6 tests)
  for the renamed vocabulary; `tests/hermes_cli/test_agent_retire_verb.py` (10)
  and `test_agent_create_verb.py` for the CLI identity, asserting that a plain
  operator invocation is unchanged byte-for-byte in its ack.

### Stage A5 — (gateway Stage 1's half) device scopes become a real policy

Only after the gateway plan's device credential exists. `caller.kind` gains
`device`, carrying the `device_id` and the `scope` read from
`gateway/devices.json`; the Stage A3 predicate stops returning allow for it. No
work here belongs to this file — it is named so the seam is visible.

### Optional Stage A6 — the service-layer backstop

Only if Ruling A picks (a) or (c). Required permission argument on the two
service functions, `LOCAL_CONSOLE` sentinel for every existing caller.
**Test plan:** the churn is `test_agent_create_service.py` (46 tests),
`test_agent_retire_service.py`, `test_agent_create_reservations.py`,
`test_agent_create_subphases.py`, `test_persona_skill_policy.py`,
`test_office_class_key_one_fence.py`, `test_harness_doctor.py`, and
`scripts/generate_agent_runtime_stream_fixtures.py`. Sized honestly: this is the
expensive option, and its whole value is the non-bypassability.

---

## 4. What this does NOT decide

- **The scope vocabulary itself.** `read` / `console` / `admin` is the primary
  plan's R11 sketch, and this file uses `console` only because two docstrings
  already say it. Which tiers exist, what each may call, and whether the skills
  install sub-phase carves out an `admin` tier (canon 06's compatibility table
  raises it) are R11's, not this file's.
- **TLS / link privacy** — R1. A chokepoint is orthogonal to whether the bytes
  are encrypted; both are Stage 1 prerequisites and neither substitutes.
- **The pairing ceremony**, device records, revocation, QR payload — primary plan
  §3.4, gateway Stage 1.
- **Peer-tier (install⇄install) authorization** — R5/Stage 6. Canon 06 already
  rules both agent methods off any peer allowlist; this file does not revisit it.
- **The coordinator permission MODEL** — whether a persona should be able to
  spawn or kill at all, and under what budget, is a separate question from where
  the check runs. Stage A4(i) renames it; it does not redesign it.
- **Whether the argv lane should exist.** Two lanes reach the CLI handlers, and
  collapsing them is
  [single-transport-collapse.md](single-transport-collapse.md) /
  [office-write-lane-collapse.md](office-write-lane-collapse.md).

---

## 5. Honest bounds

- **Nothing was executed.** Every claim is a read of source at
  `65258bf131`. No test was run, no serve was started, no live call was made.
  In particular, "the gate at `:4864` never executes on real traffic" is derived
  from `_coordinator_actor_id`'s two branches plus the two `--requested-by`
  spellings that are actually sent — it is a reading of the code, not an
  observed miss. A one-line log at that call site would settle it and has not
  been added.
- **Three call sites were located but not read in full** — `re_route` (`:5013`),
  `update_profile` (`:5088`), `set_model` (`:5258`). Stage A4 treats them as an
  inventory row; if any of them reaches the gate through a different identity
  path, A4 grows.
- **The launcher survey is capability-registry-deep only.** Nine
  `--requested-by launcher` rows were counted (`grep -c` on the literal pair);
  whether any launcher path
  constructs a `coordinator:` spelling at runtime was not searched exhaustively
  outside `lib/features/mission_control/`.
- **`serve_auth.py` internals were not read.** `authenticated` is treated as a
  proven fact on the strength of where it is set (`serve_socket.py:1165`, after
  `verify_hello_proof`) and the module docstring, not on a review of the HMAC.
- **Stage A1's manifest addition is asserted additive by the same rule the D12
  gate proved, not re-proved here.** The launcher's byte-pinned serve-frame
  fixture is the thing that would catch it, and that check is on no CI lane —
  which is itself a gateway-plan Stage 0 row.
- **No sizing was measured.** The test-count figures (46 / 10 / 8 / 6) are
  `grep -c "def test"` counts, which bound the churn and do not measure it.
- **This file makes no claim about the RIGHT policy.** It designs where a policy
  would be evaluated. Ruling A picks the where; R11 picks the what.

---

## The canon pointer this plan owes 06

Canon 06's Open row on authorization
(§ "Open rows" → "Authorization is not at the chokepoint") should carry a
pointer to this file, in the row's existing style — one line appended, the row
otherwise untouched:

> ```
>     The design and the placement options are
>     [planned/authorization-chokepoint.md](planned/authorization-chokepoint.md);
>     its Ruling A is R11's prerequisite.
> ```
>
> (written as source, not as a live link, because the path above is relative to
> 06's directory and not to this one)

**Written 2026-08-27, after a wait.** For most of this survey that file carried
uncommitted changes from a concurrent session whose diff touched the same
Open-rows region (the D6 row, rewritten in place), and editing it in parallel
would have made two sessions' work inseparable in one file. That diff landed
(`13dd0c4ae7`, merged at `795cad1ee6`) before this plan was committed, so the
pointer went in cleanly. The row's second half — "The fix is a scope parameter
on `perform_agent_create` / `perform_agent_retire`" — is §2's option (a), and
the pointer says out loud that this file argues that parameter is the backstop
rather than the gate.

---

## Three rulings for the operator

Answer in one message. Nothing below is built; each ruling unblocks the stage
named beside it.

### Ruling A — where the authorization check runs (R11's prerequisite)

**Question.** The three doors onto `perform_agent_create` /
`perform_agent_retire` share a service function but not an enforcement point,
and the RPC lane has none at all. Where does the check go?

- **(a)** Inside the shared service functions — a required permission argument on
  both, refused in their existing outcome shape.
- **(b)** A tier decorator at the RPC dispatch layer (`serve_rpc.method`),
  evaluated against `RpcContext`, mirrored by a `local_console` identity at CLI
  entry.
- **(c)** Both — (b) as the scope-aware gate, (a) as a narrow non-bypassable
  backstop asserting that a decision was made.

**Recommendation: (b).** It is the only option where the check can see a PROVEN
caller fact — `RpcContext` is where the socket lane's authenticated connection
lands, and the service functions see a params dict and nothing else — so it is
the only one where R11's per-device scopes become a lookup rather than another
self-declaration. (Take (c) instead if the non-bypassability is worth roughly 60
test call sites of signature churn; take (a) only if a caller that never touches
the RPC lane is expected to need enforcement, which nothing today does.)

**Unblocks.** Gateway Stage 1 (the non-loopback bind), R11, and Stages
A1–A4 above.

### Ruling B — R1, TLS posture on LAN (gateway Stage 1)

**Question.** How is the LAN link protected? Restated from the primary plan
(launcher `docs/mission_control/planned/universal-remote-gateway.md:524-527`,
elaborated at `:346-352`) — these are its three options, unchanged:

- **(a)** Self-signed per-install certificate, fingerprint pinned via the
  QR/pairing payload — the plan's recommendation.
- **(b)** Plaintext + HMAC auth only, accepting cleartext chat bodies on shared
  WiFi.
- **(c)** App-layer encryption keyed from pairing — most code, no TLS tooling
  reuse.

**Recommendation: (a), as the plan already holds it.** The existing HMAC
challenge-response already prevents impersonation, so the entire question is
confidentiality of chat bodies and media on a shared network — and (a) buys that
with `ssl` + `cryptography` self-signed minting and a fingerprint that the
pairing payload is already carrying for other reasons, while (c) rebuilds the
same guarantee by hand.

**Unblocks.** Gateway Stage 1's listener. Independent of Ruling A — both are
Stage 1 prerequisites and neither substitutes for the other.

### Ruling C — marking the W2 relay legacy

**Question.** The primary plan's §6 (`:589-612`) wants
`lib/features/mission_control/gateway/mission_gateway_relay.dart` and
`mission_gateway_state_publisher.dart` marked legacy pending a retirement
decision. But the relay took real wire-behaviour work AFTER that plan landed —
launcher `b11614268` (2026-08-25) re-bucketed a remote caller's bad argument out
of `relay_request_failed` and into `capability_rejected_by_host`, routing the
message through `FrameItem.frame`. The plan's drift audit deliberately did not
mark it. So:

- **(a)** Mark legacy now.
- **(b)** Defer until that lane's owner closes it.
- **(c)** Write the retirement plan first, and let the marking ride it.

**Recommendation: (c).** `b11614268` is not maintenance — it is a fix to the
relay's error taxonomy on its only reachable lane, which means someone is
actively reasoning about that code, and a "legacy" banner arriving mid-lane
either gets ignored (costing the banner its meaning) or stops work that is
correct to finish; writing the retirement plan answers the question the banner is
standing in for — what replaces the desktop-brokered path, and when the Django
`agent_gateway` app follows — and marking then costs nothing because it points
somewhere. (Take (b) if the lane is expected to close within days; (a) is the
one to avoid, since it marks code that is being changed without saying what
should be done instead.)

**Unblocks.** Nothing in hermes — this is launcher/backend hygiene. It is here
because the primary plan escalated it as an operator decision rather than a free
doc edit, and it should be answered in the same message as A and B.
