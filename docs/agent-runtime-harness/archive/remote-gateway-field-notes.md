# Field notes — remote gateway lane

A running record, newest section last. Not a plan and not canon: what a working
session actually READ, and where what it read disagreed with the brief it was
given. Rows graduate into [remote-gateway.md](remote-gateway.md), into
[authorization-chokepoint.md](../archive/authorization-chokepoint.md), or into canon — and
are left here either way, because the disagreement is the part a later session
cannot re-derive.

## 2026-08-27 — the authorization chokepoint survey

**Brief.** Prepare three operator rulings for the gateway lane: the chokepoint
placement (R11's prerequisite), R1's TLS posture, and the W2 relay's
legacy-marking. Docs only.

**Read, in this order.**

- `agent_runtime/coordinator_permissions.py` (whole file, 97 lines) — the
  decision function, its four action families, `OPERATOR_ACTORS`.
- Every call site of `authorize_coordinator_action`: eight, all in
  `hermes_cli/harness_parts/persona_commands.py` (`:688`, `:837`, `:854`,
  `:4827`, `:4864`, `:5013`, `:5088`, `:5258`). No other module calls it.
- The shared service functions the doors collapse onto:
  `agent_runtime/agent_retire.py:219` and `agent_runtime/agent_create.py:1205`.
- The RPC dispatch layer: `agent_runtime/serve_rpc.py` — `_METHODS` (`:141`),
  the `method` decorator (`:234`), `RpcContext` (`:186-215`), `handle_request`
  (`:317`), and the two agent handlers (`:1992`, `:2078`).
- Where a context is BUILT: `hermes_cli/harness_parts/serve.py:3146-3156`, and
  the identity facts the socket lane holds: `agent_runtime/serve_socket.py`
  `SocketConnection` (`:575-597`), the hello that fills it (`:1163-1165`),
  `hello_ok`'s `connection` key (`serve.py:2389`).
- The launcher's side of the same verbs:
  `lib/features/mission_control/data/harness_capability_registry.dart:1488-1530`.
- The primary plan's §5 R1/R11 and §6, launcher
  `docs/mission_control/archive/universal-remote-gateway.md:524-527`, `:568-578`,
  `:589-612`.

**What contradicted the brief.** Three things, and the first is the one that
moved the design.

1. **The brief (and canon 06) frames the gap as an ASYMMETRY — one door gated,
   one not. The measured state is that neither door is gated on any traffic
   anyone actually sends.** The gate at `persona_commands.py:4864` runs only
   when `_coordinator_actor_id` (`:1654`) returns non-`None`, and that helper
   returns `None` for every `--requested-by` spelling except `coordinator` and
   `coordinator:<id>`. `persona instance retire`'s flag DEFAULTS to `cli`
   (`hermes_cli/harness.py:998`) and the launcher hardcodes `launcher`
   (`harness_capability_registry.dart:1517`). So the "gated" door is gated
   only for a caller that volunteers it is a coordinator. Canon 06's row is
   right that something is wrong and understates it: this is not a gate in the
   wrong PLACE, it is a gate with no reachable caller.

2. **Identity and scope are both SELF-ASSERTED, by the same argv the request
   rides on.** `_coordinator_scope_from_args` (`:1663`) reads
   `--coordinator-max-spawns` / `--coordinator-may-kill-own` /
   `--coordinator-may-kill-others` (`harness.py:256-261`) off the caller's own
   command line, and `authorize_coordinator_action` returns `operator_bypass`
   for any actor naming itself `operator`/`tony`/`cli` (`:16`, `:68-69`). A
   chokepoint that keeps reading identity and scope from the request body would
   therefore relocate the hole without closing it — which is why §2 of the new
   plan splits "where the check runs" from "who supplies the fact it checks",
   and why option (b) exists at all.

3. **`RpcContext` already carries a caller identity — it just isn't an
   authorization one.** `connection_key` (`serve_rpc.py:213`) is the
   SUBSCRIPTION identity, minted per socket and swept on drop; the socket lane
   also holds `client`, `client_build`, `peer` and `authenticated`
   (`serve_socket.py:578-597`), all filled at the HMAC handshake. Nothing of
   this reaches a handler's authorization decision because handlers make none —
   both agent handlers are pure translation shims that pass `params` straight
   through (`serve_rpc.py:2068`, `:2134`). The plumbing R11 needs is one
   argument wide; what is missing is a decision, not a channel.

**Not contradicted, re-confirmed.** Both agent methods are `console`-tier as
prose only (`agent_retire.py:254-258` docstring, `serve_rpc.py:2118-2121`), the
socket listener is still loopback-pinned (`serve_socket.py:193`), and the
gateway plan's Stage 1 block is stated correctly in both repos.

**Not read, and therefore not claimed.** The `re_route` / `update_profile` /
`set_model` call sites (`:5013`, `:5088`, `:5258`) were located and counted but
their handlers were not read in full; the new plan's Stage 4 names them as an
inventory row rather than asserting their shape. Nothing was executed — no test
was run, no serve was started. Every claim in the new plan is a READ.

**Foreign working-tree state, and how it resolved.**
`docs/agent-runtime-harness/06-office-and-board.md` carried uncommitted changes
from a concurrent session for most of this survey, and that diff touched the
Open-rows region (a D6 row rewritten in place) — so the pointer canon 06 owes
the new plan was held rather than written into a file two sessions were editing.
The concurrent session committed (`13dd0c4ae7`, merged at `795cad1ee6`) before
this work was staged, the working tree went clean, and the one-line pointer went
into the authorization Open row after that. Recorded because the decision was
"wait, do not interleave", and it was only the timing that made it free.

## 2026-08-27 — Stage 0a, the install identity

**Brief.** Build Stage 0 — the one un-gated stage. Identity module, additive
`install` block on the three greeting frames, `gateway.*` config keys, CLI
verbs, docs. Mandatory first move: the install-id inventory, written down.

**The inventory, before any code.** Read both existing mechanisms end to end
(`agent/monitoring/policy.py` whole file, 57 lines; the `_install_id` method in
`hermes_cli/observability/shared_metrics.py`; both consumers in
`gateway_health_export.py:86,292` and `otlp_exporter.py:120-124`). Verdict
DISTINCT, and the argument is now in `gateway_identity.py`'s docstring and in
the plan's "Stage 0 notes". What made it easy rather than a judgement call was
the monitoring module's own docstring: it advertises **rotatability as a
feature** ("clearing `monitoring.install_id` rotates the id on the next gateway
start"). That is precisely the property a paired-device identity must not have,
so the two facts were never one fact wearing two names.

**What the brief said to reuse, and what actually got reused.** Not the id —
the *contract*. `serve_auth.py` is the same shape one field over (per store
root, mint-iff-absent, root-is-an-input, never-raises, typed state instead of an
exception), and its docstring already argues each rule. `gateway_identity.py`
restates that contract for a non-secret and says where it came from, so the two
files read as one discipline rather than two inventions.

**Where the brief was wrong, measured.**

- **The frame-vocabulary gate does not police top-level keys.**
  `mission_serve_frame_fixture_gate_test.dart` reads the launcher's own AST
  switch for frame `event` NAMES (`_frameVocabularySize = 15`) and scans test
  sources for hand-authored frames. A new `install` key on `ready` cannot trip
  it, and `_frameVocabularySize` did not move. The launcher-side reader the
  brief made conditional on that trip was therefore not forced by it.
- **The entanglement the brief warned about had cleared.** Both repos' foreign
  hunks were committed by their sessions mid-flight (hermes `a7655ccf01`,
  `6dbd789e8c`; launcher `406b7fc87`) before anything here was staged. Checked
  with `git diff -- <file>` per file rather than trusted from the brief's
  snapshot — `hermes_cli/harness.py` and the launcher's `hermes_cli_contract.json`
  were both clean, so the CLI-verb split the brief pre-authorised was not
  forced by entanglement either. It was still taken, for scope: see below.

**The determinism problem, and why seeding beat scrubbing.** A fresh uuid4 in
`ready` breaks the launcher's byte-pinned captures. The generator's existing
volatility machinery is key-based scrubbing (`pid`, `port`, `boot_id`,
`commit`), and using it here would have left the committed bytes proving only
that two keys exist. Seeding a fixed `gateway/install.json` into each sandbox
root instead makes hermes take its LOAD path — which is what a real install does
on every boot after its first — so the fixture pins the values the launcher
parses, and the mint path stays covered where it can be tested properly
(`tests/agent_runtime/test_gateway_identity.py`, 18 cases). The precedent was
already in the file: `Sandbox.make_storelike` exists for exactly this reason
("put the runtime root in the state a Launcher-spawned serve's root is in").

Secondary reason, not the main one but decisive against a hostname default
reaching a fixture: hermes' default `display_name` is `socket.gethostname()`, so
an unseeded capture would have committed the capturing operator's machine name.

**One asymmetry decided deliberately.** An EMPTY `install.json` (killed between
the `O_EXCL` create and the write) is healed; a non-empty but unparseable one is
a typed `error:malformed_record` and is never overwritten. A zero-byte file's id
is held by nobody, but a file with bytes in it may be a record whose id a Stage 1
paired device still names, and re-minting to make a boot look tidy would destroy
the only copy of that join key. Both are pinned as tests.

**Two deviations from the primary plan's `{install_id, display_name, build}`.**
`build` dropped (the frames already carry a top-level `build`; a nested copy is a
second authority), `state` added (absence cannot say "could not mint"). Recorded
in the plan doc rather than only in a commit message.

**Scope call: Stage 0b split off.** The CLI verbs were un-entangled and
buildable, but they add a second cross-repo fixture landing (hermes's argparse
tree is dumped into the launcher's `hermes_cli_contract.json` and driven through
the argv conformance suite), on top of the serve-frame refresh Stage 0a already
pays. The verbs' service half (`set_display_name`, `read_install_identity`) is
built and tested, so the remainder is registration plus a dump refresh.

**Run, not inferred.** `pytest tests/agent_runtime/test_gateway_identity.py`
18 passed; `test_serve_service_foundations.py` 27 passed;
`test_serve_socket_lane.py` 57 passed. `generate.py --check` red on exactly one
frame (`ready.json`) with the change in, green twice after the refresh. Note:
the venv at `X:/Eternia/.hermes/venvs/hermes-agent` has no `pytest` installed —
`C:\Python312\python.exe` is what runs the suite, while the serve-frame
generator still needs `--python` pointed at the venv interpreter.

---

## 2026-08-27 — the authorization chokepoint, A1–A4

Ruling A option (b) built end to end in one wave: hermes `8d69f8858b` (A1),
`4d60060dc3` (A2), `dba7ed19b6` (A3), `290f6f461b` (A4); launcher `2cf887b47`
(the A1 fixture + reader). A5 is the gateway's Stage 1. A6 was NOT built and
that is not a judgement call — the plan makes it conditional on Ruling A picking
(a) or (c), and the operator picked (b).

**The survey's central finding held, and the fix is not the one canon 06
proposed.** All three doors were unenforced on real traffic, including the one
the canon called gated: `_coordinator_actor_id` recognises only
`--requested-by coordinator[:id]`, and the two spellings anyone sends are `cli`
(the CLI's own default) and `launcher` (hardcoded in the launcher's capability
registry). So the asymmetry was the smaller half. Closing it by giving
`agent retire` the coordinator gate would have propagated a self-declaration
protocol to a second door. It was closed instead by minting ONE console identity
inside `_agent_retire_outcome` — the function whose own docstring already said
"the ONE retire the CLI performs, whichever verb the operator typed" — so the
two doors are structurally incapable of drifting apart again.

**The default value was the one real design fork, and the plan's sketch was
wrong about it.** A2 as written wanted `RpcContext.caller` to default to
"`None`/unknown", and Ruling A says an unrecognised credential is refused console
verbs. Composed, those two would have refused every bare `handle_request(req)` in
the tree — the argv lane's probes and every unit test in the repo — and broken
A3's own no-behaviour-change promise one stage early. The resolution is that the
honest default is `stdio_owner`, not unknown: an `RpcCaller` can only be
constructed by code already inside this process, so a context built with no
arguments genuinely describes an in-process caller. That is what the
`transport = "stdio"` default beside it has always said. Refuse-by-default binds
where credentials arrive from OUTSIDE — `caller_for_connection`, which mints
`unknown` for any connection that has not passed `verify_hello_proof` — and that
is the arm Stage A5 grows.

**The gate went in `handle_request`, not in a wrapper `method()` composes.** The
plan said "compose `requires_tier` into `method()`". Same guarantee either way
(it is the single point both transports pass through, so no method can be
registered around it), but a wrapper would also have gated DIRECT handler calls —
which is what every unit test in this repo does — and landing the gate would then
have meant rewriting the suites. The tier declaration still rides the decorator;
only the evaluation moved.

**`@method`'s tier argument is required, with no default.** A default is what
turns a registration into a hole: default open and a forgotten verb ships
unguarded, default closed and a forgotten read verb breaks a working client.
Requiring the word makes a tierless method unrepresentable rather than merely
untested, and an unknown tier raises at import so a typo is a boot failure with
a name in it.

**Three test pins had been RED since S5 and nobody noticed.**
`test_serve_rpc_office_upsert.py`'s two manifest literals and
`test_serve_rpc_office_subscribe.py`'s `method_names()` list never grew
`runtime.agent.retire`. Found because A1 had to touch every manifest literal;
confirmed against a clean HEAD worktree BEFORE claiming it, which also proved
that the two literals in `test_serve_rpc_office.py` were green at HEAD and were
moved by this wave alone. Standing lesson, already in the memory index and paid
again: a hand-written literal beside a live registry rots silently, and the
registry-driven test is what catches it.

**`prewarm` is the one tier row worth arguing, and it is `read`.** Its own
contract says it writes no store state, emits no event and mints no id — the same
sentence that makes `runtime.office.get` a read. It does spend CPU, but spending
CPU is a rate-limiting question and rate limiting is not a tier: a viewer device
that may not place an agent may certainly warm the cache its own reads use.

**Cross-repo.** `ready.json` was the one fixture that moved (`hello_ok` is not
captured), `--check` green twice in a row after the refresh, hermes committed
clean first so the capture records `hermes_dirty: false`. The launcher gained
`MissionRuntimeRpcManifest.tiers` as read-and-expose only, and equality includes
it — the manifest lives in a `ValueNotifier` re-read off every frame carrying an
`rpc` key, so a runtime that re-declared a tier has to wake its listeners.
Nothing branches on a tier client-side, deliberately: a manifest says what a call
WANTS, never what a connection HOLDS, and a client that refused itself on a
declaration would be inventing a policy the server never stated.

**Run, not inferred.** `C:\Python312\python.exe -m pytest` (the venv still has no
pytest): the ten serve/RPC suites 270 passed; `test_serve_rpc_authorization.py`
16 passed; `test_serve_rpc_caller_identity.py` + notification lane 17 passed;
tier/agent-verb suites 111 passed; the `tests/hermes_cli` keyword sweep
(`persona or coordinator or agent or serve or retire or create`) 576 passed / 20
skipped. Launcher: fixture gate + agent-create lane + install identity + greeting
race + prewarm, 97 passed; `mission_office_rpc_test.dart` 43 passed;
`flutter analyze` clean on both touched files.

**Honest gaps.** (1) No live serve was driven by hand — every proof is a test,
including the socket ones, which do run the real `serve_loop` on a real loopback
socket with the real HMAC. (2) The CLI mirror's refusal arm is exercised only by
patching `_console_denial`; the predicate cannot refuse `CLI_CONSOLE` by any
input, which is the design, so there is no unpatched way to see that arm until a
non-console CLI identity exists. (3) The mirror is on the three CLI doors onto
the two service functions, not on `persona instance close` / `open_chat` /
`re_route` / `update_profile` / `set_model` — those are not chokepoint doors and
keep the renamed coordinator review alone. (4) `_coordinator_scope_from_args`
still lets four argv flags widen a coordinator's self-declared budget. That is
`coordinator_permissions`' MODEL, explicitly out of scope per plan §4, and it is
now labelled rather than fixed.

## 2026-08-27 — Stage 0b, the CLI verbs

**Two subverbs where the plan wrote one verb and a flag.** Stage 0a's remainder
note (and the launcher queue row) both spell this `harness gateway id` /
`--set-name`. What landed is `harness gateway id` and `harness gateway rename
<name>`, and the reason is mechanical rather than taste:
`_add_stage42_global_args` is where every stage42 verb gets its flags, and the
writer's set and the reader's set are DIFFERENT — a mutation opts into
`--dry-run` (`roots set`, `workspace rename`, 29 call sites), a read does not.
One parser cannot carry both truthfully, and that helper's own docstring is
built around the rule that an advertised flag which does nothing is "a WRONG
ANSWER believed, not an error seen". `rename` rather than `set-name` because
`workspace rename` is the house word for exactly this operation and
`set_display_name`'s own docstring opens "Rename this install".

**No authorization gate, and it is a decision.** The A4 mirror
(`persona_commands._console_denial`) exists so two doors onto ONE service
function cannot answer differently — `perform_agent_create` /
`perform_agent_retire` each have a CLI door and an RPC door. Stage 0b adds no
RPC method, so there is one door and nothing to disagree with; `CLI_CONSOLE`
here would gate against a predicate that allows every caller that exists, with
no wire twin to keep it honest, and `_console_denial` is shaped as the two
service functions' refusal kwargs so reusing it means generalising a helper for
a caller that is always allowed. The record is also not a level and not a
secret. When a paired DEVICE may rename an install, the door is a `gateway.*`
method with a tier declaration (Stage 1 / A5) and the gate goes there — where
the caller is something the transport proved. Written into the handler block in
`harness.py` rather than left as an absent check, on A4's own reasoning that a
grandfather clause should be greppable.

**A read that mints is a side effect on a root somebody only asked about.**
`gateway id` routes at `read_install_identity`, never `ensure_install_identity`,
and the test probes the FILESYSTEM rather than the ack — the kill-mutation
(route at ensure) returns a perfectly plausible exit-0 ack. Stage 4's install
picker will run this against roots it does not own.

**One Stage 0a service wart, found by shipping the verb and fixed:**
`set_display_name` returned a constant `STATE_LOADED`, so a rename against a
fresh root — which MINTS, by its own documented contract — reported `loaded`.
That is the call reporting the opposite of what it did, on the one module whose
whole contract is "state it, never infer it from absence". It now propagates the
load-or-mint outcome it already had in hand. No test pinned the old constant; a
new one pins the new behaviour both ways.

**`clean_display_name` became public**, because `--dry-run` has to print the
string that WOULD land and the only way to get it without a second copy of the
rule is to ask the rule. A preview that echoes the raw argument shows a
500-character paste at 500 and lands it at 64 — a preview that disagrees with
its own write is worse than no preview.

**Typed states become exit families, never tracebacks.** `error:absent` → 3
(nothing to show — a root that never served genuinely has no identity);
`malformed_record` / `record_without_id` → 1 (`store_corrupt`, and deliberately
NOT a re-mint, per the asymmetry `_decode` documents: those bytes may hold the
id a paired device names); `empty_display_name` → 2; every other I/O reason → 7,
retryable in the sense that family already means. The typed reason travels
verbatim in the message, so an operator comparing a greeting frame's `install`
block against the verb reads one spelling, not two.

**Root observability is not decoration here.** The identity is per STORE ROOT,
so a `gateway id` against the wrong root returns a well-formed identity for a
runtime the operator did not mean — the 2026-08-12 incident's shape with an id
in it. Both handlers stamp the block; the ledger in
`test_harness_json_root_observability.py` gains no new row.

**Cross-repo.** A new verb changes the argparse tree the launcher pins. The
serve-frame captures should NOT move — a CLI verb touches no greeting frame —
and that was proven rather than assumed with a `generate.py --check` at the end.

**Run, not inferred.** Receipts and honest gaps are in the Stage 0b block of
`remote-gateway.md`.

## 2026-08-27 — Stage 1, the device tier and the second door

**Brief.** Build the largest hermes stage: the device credential store, pairing
verbs, the second listener, R1's TLS, A5's enforcement, docs. All rulings
settled; cite, do not re-litigate.

**What contradicted the brief, in the order it was found.**

1. **Stage 0a's config keys did not exist.** The brief says
   "`hermes_cli/config_defaults.py` (`gateway.listen`/`gateway.port`, declared
   Stage 0, read by nothing — you are their first reader)". Being the first
   reader is what found it: `"gateway"` is already a top-level key in that file's
   one ~3000-line dict literal (the messaging gateway's), and a duplicate key in
   a Python dict literal is not an error, not a warning, and not visible from the
   loaded object — the later entry wins and the earlier is discarded at parse
   time. Stage 0a's keys were never merely unread. **"Read by nothing" is the
   exact condition under which nobody could tell**, and that generalises well
   past this file: a declaration with no consumer has no observable difference
   from a declaration that failed to load. Renamed to `remote_gateway.*` (which
   Stage 0a's own comment argues for — it says the two lanes must not be
   conflated, and they cannot share a key and stay unconflated), and guarded by
   an AST walk over the module source, since the loaded dict cannot show the
   defect. The detector was checked against the exact shape that shipped rather
   than assumed to work.

2. **The pairing ceremony had no second half.** The brief scopes the listener to
   "ONLY device-tier hellos", which is correct as far as it goes and leaves
   `redeem_pairing_code` with no caller but a test — i.e. a device tier no device
   can enter and a `harness gateway pair` whose output is decoration. Built the
   redemption hello anyway and said so in the commit rather than quietly. The
   judgement: a half-ceremony is a bigger defect than the extra surface, and it
   would have to be fixed before any Stage 5 phone existed.

3. **The tier gate covers one of the two doors a device can reach.** A5 as
   designed (and as the chokepoint plan wrote it) gates the METHOD lane. The
   argv lane — `{"argv": ["harness", …]}` — reaches the CLI dispatcher, where no
   tier declaration exists and every verb is the local operator's. So a `read`
   device refused `runtime.agent.retire` on the method lane could have sent the
   same verb as argv and been obeyed: the gate would have been real and
   bypassable in one frame. Refused the lane outright rather than gating it,
   because gating means deciding a tier for every CLI verb in this repo and
   keeping that map correct forever. This is the single change in the whole
   stage most likely to be wrong if it is wrong, and it is the one to review
   first.

4. **The handshake prose was wrong in three places, and the client author found
   it.** `serve_socket.py`'s docstring, `serve.py`'s wire summary and canon 03 §1
   all described the proof as `hello_contract` 2 over `msg=<nonce>`;
   `HELLO_CONTRACT_VERSION` has been 3 and `hello_proof` has bound the dialled
   PORT since the relay defence landed. Wrong in both halves at once. It surfaced
   because the sibling session built the launcher's socket client against it
   (launcher `527940a0e`) — which is the only way this kind of drift ever
   surfaces, and the reason it is worth recording: a stale comment normally costs
   a reader a minute, and this one costs a client author an afternoon pointed at
   the wrong subsystem, because a proof over the wrong message is refused
   `bad_proof`, byte-identical to holding a bad credential. Discharges the
   primary plan's §7 bound, which named exactly this and deferred it to "Stage 2
   client authoring".

**Design calls worth re-reading before extending this.**

- **The stored verifier is HMAC-key-equivalent, and the docstring says so.** The
  brief says "hashed per-device 256-bit tokens (never store or log the
  plaintext; hash like serve_auth treats its token)" — and `serve_auth` stores
  its token in PLAINTEXT, so the instruction is about DISCIPLINE (never in a
  frame, log, or event) rather than about a digest. Both were done, but the
  digest buys exactly one thing: the bytes a phone holds are not the bytes on
  disk, so a store read cannot recover an ISSUED credential. It does not make
  the store less sensitive than `serve_auth_token`. Claiming otherwise would be
  a security note that overclaims, which is worse than none. The upgrade for
  store-read resistance is asymmetric (the R1 survey's bullet 2) and changes one
  function.
- **Revocation is checked AFTER the proof.** Checking it first lets an
  unauthenticated peer probe which device ids are revoked, and by difference
  which are live, holding no credential at all.
- **The R1 survey's bullet 3 was honoured rather than overridden.** It argues
  TLS and AUTHENTICATION are different lifts and that proof-of-key should ship
  first. Both shipped, but SEPARATE: authentication is the HMAC, TLS is
  confidentiality only, and a peer that completes a TLS handshake has proven
  nothing. The survey expected the certificate to be the expensive half; it was
  one module, because `cryptography` is already pinned in `pyproject.toml`
  (48.0.1, capped below 49 by msal) and installed. **No dependency work at all**
  — the brief's contingency ("if that's heavier than expected, propose the
  split") did not arise.
- **One implementation, three constructor arguments.** The second listener is
  `ServeSocketServer` with `port`/`ssl_context`/`authenticator` filled in, not a
  subclass and not a copy. The hardened parts of that class are precisely the
  parts a copy would get wrong, and every one of them is invisible until it
  matters (an accept loop that dies quietly, a pre-auth bound nobody counts, a
  limiter that charges the server's own state).
- **TLS is negotiated BEFORE the admission checks**, deliberately paying a
  handshake for peers about to be refused: every refusal answers with a typed
  frame, and a frame written to a socket the peer never negotiated is bytes it
  cannot read. Refusing first would turn every capacity and throttle refusal
  into an unexplained disconnect.

**Where a test had to be corrected rather than the code.** The
byte-identical-loopback assertion failed first on `auth.token_file`
minted-vs-present and `install.state` minted-vs-loaded — boot ORDER, not the
gateway lane. Warming the root and discarding that boot is the fix, and the test
says why: a comparison between a first boot and a second proves nothing about
the thing it names. Volatile fields are dropped by NAME rather than by a
heuristic, so a field that starts varying for a real reason breaks it instead of
slipping through.

**Repo hygiene, twice.** (1) Something in this checkout ran `git reset` plus a
clean twice during the session and destroyed untracked work — most likely a
sibling session's fixture generator, which requires a clean hermes tree. Recovered
from a scratchpad copy the second time and switched to write-to-scratchpad-then-
commit-atomically for the rest. Worth knowing: an untracked file in this repo is
not safe while another session is regenerating fixtures against it. (2) A
concurrent session held uncommitted work in `hermes_cli/harness.py` for the whole
of the verb commit. Only this lane's hunks were staged, by building a patch
against HEAD and `git apply --cached`-ing it — `git add <file>` would have taken
their work with it.

**Cross-repo, and how the dirty tree was worked around.** `ready.json` moved
(the additive `gateway` block) and the argparse dump grew three verbs with ZERO
removals. Both were regenerated from a clean detached WORKTREE of hermes at the
exact commit, because the working checkout carried the concurrent session's
changes and the generator's own warning is that a `+dirty` stamp is one nobody
else can reproduce. Same commit, clean tree, reproducible bytes — and
`generate.py --check` green twice consecutively after the refresh.

**Run, not inferred.** Receipts are in the Stage 1 notes in
[remote-gateway.md](remote-gateway.md); the honest gaps are there too and the
first three are the ones that matter: no second machine, no phone, and the
Windows firewall behaviour documented in canon from platform knowledge rather
than from an observation made here.

---

## Stage 3 — the write path (2026-08-27, hermes half)

**The premise of the stage was wrong, and finding that out was the stage.** The
plan says mission-chat send has no server-side dedupe, "re-verified 2026-08-27:
no `turn_request_id` anywhere". Both halves are literally true and the
conclusion is not. Mission chat has carried exactly-once since the 2026-08-24
incident under the name **`client_message_id`** plus the per-session **turn
journal**: `persona_commands._mission_chat_busy_outcome` already answers a
repeated id with `idempotent_replay: True` and the committed reply once the turn
settles, `chat_turn_duplicate_in_flight` while it is still running, and
`chat_turn_outcome_unknown` when the provider outcome cannot be proven. It is
richer than anything this stage would have built.

The lesson generalises past this row: **a grep for the NAME a plan chose is not
a survey of the CAPABILITY.** The re-verification was run twice, months apart,
and both times it asked "is the word here" rather than "does a second send of
one message run twice". The second question takes one test and cannot be
answered wrong.

So `turn_request_id` is not a second key. It is passed to
`--client-message-id` **unchanged** — no hash, no prefix, no re-mint — and the
receipt this stage adds covers only the window the journal provably cannot: the
journal's first write happens inside the chat-root lease, i.e. after a worker is
already running, and the RPC lane has to answer before that. Two layers, one
authority.

**The design collision, and why the union had a hole nobody could see.** Stage
3's sketch said "RPC where methods exist, op/argv lane otherwise — same union".
`mission.chat.*` has no methods; it lowers to argv; and Stage 1 refused the argv
lane to devices *correctly*. So the union's fallback arm was closed for exactly
the caller the gateway exists for, and the gateway shipped a device that could
place an agent and could not talk to it. Neither stage was wrong on its own —
the hole is in the SEAM, which is the kind of defect a per-stage review cannot
find and a cross-stage read can.

**The door lands one step lower than the precedent, and that is the stronger
form.** `runtime.agent.retire`'s door calls `perform_agent_retire`, the same
function the CLI calls. Mission chat has no such function — its service IS the
argparse handler, and its one existing second door (`dispatch_delivery`) reaches
it by building a namespace. This door builds ARGV, dispatched through the same
argparse tree a local send uses. A parallel Python call site would be two
implementations that agree today; this is one execution. The safety property
that makes argv-building acceptable is asserted rather than assumed
(`test_a_client_cannot_smuggle_a_flag_through_a_value`): the argv is a LIST,
flags are literals, and a value is always the element after its flag.

**The tier is `console`, and the mechanical reason is the one that would have
bitten.** The taste argument (a chat turn runs an agent with tools, so a softer
tier is a door around `console`) is the right one, but the decisive fact is that
`call_authorization.authorize_call`'s device arm is an **equality** against the
stored word, not an ordering. A new `chat` tier would have refused every
already-paired `console` device the very thing R11 says it may do, on the first
frame. Any future third tier has to make that arm an ordering FIRST; it is a
decision, not a constant.

**Written by a test, not reasoned to: where the settle goes.** The worker
records its exit onto the accept receipt in `_run`'s `finally`. The first draft
put that between the inflight pop and the exit frame, on the argument that a
client reading the exit must not then observe a receipt still saying `accepted`.
The drain monitor polls the pending set, so that placement opens a window in
which a request is out of `inflight`, has not emitted, and the drain can
complete and close the lane **under its own exit frame**. Reproduced as a lost
exit inside ten minutes. It goes FIRST in the `finally`, before the pop, where
the monitor still counts the request — both properties held, and the ordering
had a reason neither draft's comment had guessed.

**The drain refusal is an addition to the method lane's own rule.** `serve.py`
deliberately keeps answering methods during a drain, with a good argument: an
inline handler "cannot be cut off half-done". A chat turn is the counter-example
that argument itself names — it is the work the drain exists to protect — so the
spawn seam refuses, and it refuses by RAISING rather than by the seam being
absent, because "this transport has no worker lane" and "this transport is
shutting down" are different facts and a client retries only one of them. The
refusal is counted on the terminal frame exactly as an argv refusal is.

**Honest gaps, hermes side.**
1. **No real provider turn ran over the method lane.** Every serve test injects
   `dispatch`, which is the seam every other serve-loop test uses — what is
   under test is the lane (accept, dedupe, hand off, account, settle), and the
   argv it builds is pinned literally against the real verb. But "a remote
   device got a model reply" is unproven here and is the launcher acceptance's
   job against a sandbox root.
2. **`correlation_id` is accepted, fenced and echoed — and rides no further.**
   `harness mission-chat message` has no `--correlation-id` flag, so unlike the
   six office/agent writes there is nowhere for the token to join the turn's
   events. Closing it means an argv flag on the chat verb, i.e. a change to the
   LOCAL lane, which this stage's contract forbids. Filed against
   `planned/correlation-id-coverage.md`, not fixed here.
3. **The accept receipt over-claims on one crash.** A crash between
   `mark_accepted` and the submit leaves a receipt for a turn that never ran, so
   the retry is answered `idempotent_replay` for work that did not happen. That
   is the deliberate direction: the other ordering duplicates an operator's
   message, which a client cannot undo. A hung turn is visible (no journal
   record) and `turn-resolve` exists for the unprovable case.
4. **Steer rides the worker lane even though a steer is cheap.** Uniformity,
   argued rather than incidental: `_CHAT_TURN_COMMANDS` counts both verbs, and a
   steer that skipped the pool would be a chat turn the recycle protection could
   not see.
5. **No second machine, still.** Everything binds loopback — Stage 1's gap,
   unchanged and not this stage's to close.

## Stage 6 — peer pairing, install⇄install (2026-08-27)

Landed: hermes `dd8a8ad716` (the store + the shared code discipline),
`77768eea27` (37 store tests), `6775911bbc` (the peer caller kind, the
allowlist, `peer.ping`, five manifest pins), `db6bbdc899` (the wire),
`5439595880` (the four verbs), `c246b648ba` (the two-roots acceptance).
Canon: [03 §1.2](../03-transport-and-wire.md).

### The design decision that took the longest: a KIND, not a tier

The natural build is a third tier word. Add `peer` to `TIERS`, declare it on the
verbs a peer may call, compare it the way A5 compares a device's. It reads
cleanly and it is wrong, and what makes it wrong is only visible when you ask
what happens as the registry GROWS.

A tier comparison admits every future verb that declares the matching word. So
the day somebody registers a new `read` method — and this repo registers one
every few stages — every paired install on the LAN can call it, and nobody
decided that. Canon 06's exclusion (*"agents never mint or retire agents on
another install"*) would still hold for the two verbs it names, and would have
quietly stopped being a statement about the peer surface as a whole.

An allowlist inverts the default. `PEER_METHOD_ALLOWLIST = {peer.ping}` admits
nothing it was not edited to admit, so create/retire are absent not because
anyone remembered to leave them out but because everything is absent. Widening
the peer surface becomes a visible line in a diff with a reason attached, which
is the property the canon's sentence actually needs.

The test follows from that and is the one to read first:
`test_a_peer_is_refused_every_registered_method_except_the_allowlist` iterates
`serve_rpc.method_names()`. It does not name create and retire (except as a
readability assertion sitting on top of the iterated one), because a rule pinned
by two literals stops being pinned the moment a third verb arrives — the
loops-not-literals lesson, applied where it changes what the test can catch
rather than just how it reads.

**The asymmetry with the device arm is now explicable rather than incidental.**
Stage 3's note said the device arm is an equality against a stored tier
"deliberately"; that note is untouched. A device is a client of THIS install
whose operator chose how much surface to hand it, so a stored tier is the right
question to ask. A peer is another RUNTIME whose own agents drive it, and "how
much of my runtime may another runtime's agents reach" is a different question
with a different answer shape.

### The arm's POSITION is load-bearing, and no console-verb test can see it

`authorize_call`'s read arm returns ok for every caller including `unknown` —
A5 kept that deliberately and the reasoning is still right. But it means a peer
evaluated AFTER the read arm inherits this runtime's entire read surface: the
office core, the subscribe lane, every read verb not yet written. Every
console-verb test would still pass.

So the peer arm runs before the read arm, and
`test_a_peer_is_refused_read_verbs_too_which_is_the_arm_ordering` asserts it
against real read-tier methods discovered from the registry. If someone later
moves the arm for tidiness, that test is the only thing in the suite that
notices.

### `peer.ping` declares `read`, and the argument for why that is not a lie

The row an operator or a launcher reads says what a call WANTS, never what a
connection HOLDS — canon 03 §2, and the launcher's `MissionRuntimeRpcManifest`
branches on nothing. So the honest tier for a verb that reads no store, writes
nothing and mints no id is the same one `runtime.office.get` gets. A `console`
declaration would claim a level mutation's credential is needed, which is false,
and would grey out a ping any read-tier device may in fact call.

What the map does not say is who may call it BESIDES a credential of that
strength, and that asymmetry is already in the contract rather than invented
here. The allowlist NARROWS the peer lane; it does not widen this row. Inventing
a third tier word only one caller kind can hold would put a value in the map
every existing reader must be taught to ignore.

### The frame-level rule turned out to be a COUNTING rule

Four hellos now reach the listener — device credential, device pairing code,
peer credential, peer join code. Stage 1's authenticator was a chain of `if`s,
which is fine at two, and writing the third branch exposed what a chain does
with a malformed frame: it picks a winner. "A code beats an id", "a peer beats a
device" — every such rule is one refactor away from picking the MORE privileged
one, and the flip is invisible in review because the code still reads sensibly.

`_credential_kind` counts instead. Exactly one credential field named is a
credential; zero or two is a refusal. The one pair that is not two credentials
(a join frame carrying `peer_code` AND `peer_install_id`, where the code is the
credential and the id is the name being claimed under it) is spelled out as an
explicit allowance, so the counting rule stays intact for the combination an
attacker would actually try.

### One symmetric secret needed the install id in the message, or it would relay

Both ends of a peer edge store `sha256(secret)` and key the HMAC with it
directly — unlike the device lane, where the phone holds a token and digests it
per connection. That is what makes the edge symmetric: either install can dial
the other with the row it already has, which is required, because Stage 7's
cross-install chat has A dialling B while Stage 6's ceremony had B dialling A.

The consequence is a hole that does not exist on the device lane. With one key
and one nonce, A's proof to B and B's proof to A would be the same bytes, and a
relay that bounced one back would authenticate. Binding the DIALER's own install
id into the message closes it. `test_the_proof_binds_the_port_the_nonce_and_the_install_id`
pairs a second install against the same secret specifically so the refusal is
the binding and not a missing row.

The `pwv`/`gwv` prefix split is the other guard, and the store suite constructs
the case it exists for rather than assuming it impossible: a device proof
computed over the very bytes a peer holds still does not verify as a peer proof.

### Why the shared `pairing.json`, argued rather than assumed

The first sketch gave peers their own pending file. It is wrong for one reason
that outweighs the tidiness: a guesser grinding codes does not care which
ceremony a code belongs to. Same 32^8 space, same listener, same handshake
budget. Two failure counters would mean an attacker locked out of the device
ceremony simply grinds the peer one with a fresh budget — the lockout would gate
minting rather than guessing, i.e. gate nothing. That is the same defect class
`gateway/pairing.py` fixed as #10195, arriving by a different door.

So the pending map, the cap and the lockout are one, and
`test_failed_peer_redeems_lock_out_the_device_ceremony_too` is the test that
would have caught the design going the other way — it looks like an odd thing to
assert until you notice every per-ceremony test passes under the wrong design.

What is NOT shared is what a code redeems into: every entry carries a `kind` and
`match_pending` matches against it, so the plan's "never interchangeable" is
enforced at the lookup rather than by two files a refactor could merge.

### What the acceptance had to be, and why threads would have lied

The plan says "two isolated roots on one machine". The cheap build is two
`serve_loop` threads in one interpreter with `HERMES_AGENT_RUNTIME_ROOT` set
before each boot. It cannot work and the failure is silent: a runtime root is
resolved from the ENVIRONMENT, an environment is process-global, so whichever
serve boots last owns the ambient value and every later re-resolution inside the
FIRST serve answers for the SECOND root. The test would pass while demonstrating
the opposite of its own claim.

Two real `harness serve` children make the isolation a property of the operating
system rather than of nobody having called `store_root()` at the wrong moment.
29 seconds for both acceptance tests, which is cheap for what it buys.

The step worth reading is the ping. A dials B at an address it learned from
`peers.json`, because that is the only place it COULD have learned it: B's serve
registry is on a root A cannot read, and B's port is ephemeral so it exists in no
config file either. The only path from B's kernel-assigned port to A's dial is
the endpoint B asserted at join time. The plan's Stage 6 risk line — "peer
dialing needs the remote port from the pairing record, not a registry file" —
is therefore not a convention this code follows but the only mechanism available
to it, which is the strongest form that requirement could take.

### The ceremony, as it actually ran

```
A: harness gateway peers pair --note "install B"
   → peer_code W8UWTBMM, endpoint {127.0.0.1:60369, source: "live"}, TTL 599s
   → join_payload {cert_fingerprint, host, install_id, peer_code, port}
   → next_step: "run `harness gateway peers join <join_payload>` on the OTHER install"
   → A's peers list: []            ← an invitation nobody accepted leaves no row

B: harness gateway peers join <join_payload> --timeout 60
   → exit 0; row for 6f88e215-… (A), endpoints [127.0.0.1:60369],
     cert_fingerprint bed16d6b…, revoked false
   → this_install: 72e85ad1-… (B), endpoints [127.0.0.1:60374], fingerprint 8498c328…

A: harness gateway peers list → one row: 72e85ad1-… (B) @ 127.0.0.1:60374
B: harness gateway peers list → one row: 6f88e215-… (A) @ 127.0.0.1:60369

A → B  peer.ping {"echo": "two-roots"}
   → {pong: true, contract: 1, peer: "6f88e215-…", at: …, echo: "two-roots"}

A verifier == B verifier: True
verifier in any printed ack: False      'peer_secret' in any printed ack: False
```

Both approvals are visible in that transcript as two commands run against two
different roots. Neither install can perform the other's half.

### Honest gaps

1. **No second machine.** Every listener binds loopback. A config VALUE and not
   different code — the same `bind()` with a host string the operator chose —
   but "install A reached install B across a LAN" is unproven. Stage 1's gap,
   unchanged; Stage 6 does not close it and does not claim to.
2. **"Agents can never mint peers" is closed against REMOTE callers and not
   against a local agent.** The remote half is structural and complete: no
   `gateway.*` method exists for any peer verb, and the argv lane is refused
   outright to every gateway connection, so there is no lane at any tier that
   reaches them. The residual is that a local agent with shell access can run
   `harness gateway peers pair` exactly as it can read `serve_auth_token` or
   edit `peers.json` in an editor — every tool-using agent already holds the
   machine owner's authority. The accurate claim, written into
   `gateway_commands.py`'s docstring rather than left implied: *no agent on
   install A can cause install B to trust it, and no remote caller of any tier
   can mint a peer anywhere.*
3. **A revocation is one-sided and the other install is never told.** Correct —
   reaching across would be one install writing into another's credential store,
   the authority R5 says an install never has — but an operator who revokes on
   one side and assumes symmetry is wrong. The ack says so; nothing enforces
   that they read it.
4. **Both installs on this machine display the same name.** `display_name`
   defaults to the hostname, so the transcript above shows `DESKTOP-QJ7DDV2` on
   both sides of the edge. The `install_id` is the discriminator and every code
   path uses it, but an operator picking from a `peers list` on a machine with
   two roots sees two identical names. `harness gateway rename` fixes it per
   root; nothing prompts them to.
5. **The endpoint a peer records is what the other side ASSERTED.** Bounded and
   cleaned (`clean_endpoints`), and safe only because R5's second operator
   minted the code seconds earlier — but an install that later moves to a new
   address is unreachable until someone re-runs the ceremony. R8's retry posture
   is the intended answer and Stage 7 owns it.
6. **A wildcard bind advertises nothing.** `_self_endpoints` returns `[]` for
   `0.0.0.0`, because a wildcard is what an install LISTENS on and never an
   address another machine can dial. The join then records an edge with no
   address for the joining side, and the ack says so — but an operator who
   configured `listen: 0.0.0.0` gets a one-directional edge and has to notice
   the note to understand why.
7. **`icacls` narrowing is reported, not asserted**, on `peers.json` as on
   `devices.json`. Stage 1's gap, inherited.
8. **The store lock still falls THROUGH rather than refusing** when the
   filesystem will not lock. Stage 1's gap, inherited, and now covering two
   stores.

## 2026-08-27 — Stage 7, cross-install `agent_chat_send` (running record)

**Brief.** Build Stage 7 from the primary plan's §4: the `@install/target`
grammar (R4), a `peer.agent_chat.execute` method on the receiving install, a
remote-execution leg in the dispatch supervisor, R8's retry posture, and a
two-roots acceptance.

**Read, in this order.** Primary plan §4 Stage 6 receipts + Stage 7 spec
(`universal-remote-gateway.md:740-895`) and R4/R5/R8 (`:1034-1080`);
`call_authorization.py` whole; `gateway_peers.py` (`PeerRecord`, `dial_peer`,
`redeem_peer_code`, `record_peer`); `tests/agent_runtime/test_gateway_peer_two_roots_e2e.py`
whole; `dispatch_delivery.py:1-200`; `dispatch_store.py:1-600`;
`tools/agent_chat_tool.py` (`_looks_like_instance_handle`, `agent_chat_send`,
`_dispatch_detached`); `tools/agent_chat_dispatch.py` (`_run_dispatch_guarded`);
`agent_runtime/chat_turn.py` whole; `serve_rpc.py` (`method`, `handle_request`,
`peer.ping`, `runtime.chat.message`); `hermes_cli/harness_parts/serve.py:1177-1270`
(`_LineFrameProxy`), `:1900-2005` (the worker's frames + exit), `:3830-3930`
(`_spawn_chat_turn`); `serve_socket.py` `ServeSocketClient`.

**What contradicted the brief, and what it changed.**

1. **"The drain … performs the target's turn" names the wrong owner, and the
   plan's own vocabulary says so two sentences later.** The drain
   (`dispatch_delivery`) owns exactly one thing — *tell the sender* — and the
   supervisor (`tools/agent_chat_dispatch`) owns *perform the turn*. The remote
   leg is a substitution for the CHILD PROCESS, not for the delivery forge, so
   it lands in `_run_dispatch_guarded` beside the spawn it replaces. Putting it
   in the drain would have given one dispatch two owners and put a
   minutes-long network turn on the 5s loop that also forges deliveries.

2. **R8's "converges to `dropped`" cannot be taken literally without lying to
   the sender.** `dropped` is a DELIVERY state and it means *the sender was
   never told*. An unreachable peer is precisely the fact the sender most needs
   told — and a `dropped` row is indistinguishable, in the Activity panel, from
   a dispatch that vanished. So the cap converges to a terminal `error`
   completion that IS delivered, carrying `peer_unreachable` as its reason.
   R8's cap half is honoured exactly (`MAX_DELIVERY_ATTEMPTS`, one constant,
   no second number) and its naming half is honoured on the row, in the
   completion event and in the message the sender reads. Argued in
   `dispatch_store.REMOTE_UNREACHABLE_REASON` and in the S7c commit.

3. **`normalize_chat_message` hardcodes `--requested-by gateway_device`**, with
   a comment saying that field "is not a param, cannot be a param". Correct, and
   it is why `peer.agent_chat.execute` needed its own normaliser rather than an
   allowlist entry on `runtime.chat.message`: a peer's provenance must read
   `peer:<install_id>` and that id must come off the authenticated connection.

4. **The ack is an ACCEPT, so the dialling side has to read frames.** Stage 3
   established that a chat turn cannot be answered inline (`serve.py` answers
   the method lane on the reader loop). So install A does what the launcher
   does: reads `{"id": request_id, "event": "stdout", "line": …}` frames until
   the `exit` frame and parses the `--json` payload out of them —
   `parse_child_payload`, the same function the local child's stdout goes
   through, which is what makes a remote turn and a local turn one shape.

5. **Display names are not unique and Stage 6's own field note #4 says so.**
   `resolve_install_target` therefore resolves ids first and REFUSES an
   ambiguous name with both candidate ids rather than picking one. Pinned by
   `test_two_installs_with_one_name_refuse_with_both_ids`.

**S7a receipts.** `agent_runtime/gateway_targets.py` +
`tests/agent_runtime/test_gateway_targets.py` — 24 tests, all green.

**S7b found a Stage 6 red that Stage 6's manifest sweep missed.**
`tests/agent_runtime/test_serve_rpc_notification_lane.py::test_the_push_lane_itself_contributes_no_method_and_no_version_bump`
asserts `all(name.startswith("runtime."))` over the whole registry. `peer.ping`
broke that in `6775911bbc` and nobody ran the file — re-measured on a clean tree
(`git stash`) before touching it, and it reds at HEAD without any Stage 7 change.
Corrected to the two declared families (`runtime.`, `peer.`) with the reason
in place, rather than to something permissive: the line's actual subject is that
the PUSH lane contributed no name, and a family it does not know about should
still fail it. The S6b lesson ("every manifest pin, same commit") turns out to
have a second half: *find them by RUNNING the suites, not by grepping for the
verb you added* — this pin never spells a method name at all.

**S7c/S7d: the acceptance found two bugs the unit lane could not, and one of
them is a lesson about fakes.**

1. **The serve stdout event is `line`, not `stdout`.** `serve.py:1784` builds
   `_LineFrameProxy(frames, "line")` for OUT and `_LineFrameProxy(frames,
   "stderr")` for ERR — only the error stream is named after itself. The remote
   leg's frame reader guessed `"stdout"`, collected nothing, and reported every
   remote turn as "no reply payload". **The unit test was green** because its
   fake emitted `"stdout"` too: a fake that spells the wire itself can be wrong
   in the same direction as the code it tests. Fixed by naming the constant
   (`agent_chat_dispatch.SERVE_STDOUT_EVENT`), having the fake read it, and
   adding a grep fence against the one line in `serve.py` that decides it.
2. **A replayed accept carries no frames, and waiting for them is a hang.** B's
   per-request frames go to the sink of the connection that ASKED. So when the
   retry posture WORKS — the same `turn_request_id` stops B running the agent a
   second time — the retrying socket gets an ack and then silence, and the
   reader sat for the full 180s CLI timeout. The success path of the property
   was the thing that hung. The leg now branches on the ack: `settled` replays
   settle the row from the receipt (saying plainly that the answer is in the
   thread on the other install, because the connection that carried it is gone),
   and unsettled ones — the turn still running over there — count an attempt and
   retry.

**What the acceptance proves and what it does not.** B answers
`{"error_kind": "unsupported_persona", "error": "unknown persona dev"}` out of
its OWN empty roster, at exit code 2 — which is the proof that the argv reached
B's real mission-chat handler in B's runtime. It is NOT a model turn: both roots
are fresh and this suite has no provider, so "an agent on B composed a reply and
it was forged into A's chat" is proven by the unit lane and the delivery lane's
own suites, not by two serve children.

**Final sweep, measured on a clean tree after the docs landed.**

| suite | result |
|---|---|
| `tests/agent_runtime` (whole tree, minus the two e2e files) | **6800 passed, 2 skipped, 1 failed** |
| both two-roots e2e files together | **5 passed** in 98s |
| the gateway/serve/auth/dispatch group (25 files) | **686 passed, 1 skipped, 3 failed** |
| `tests/gateway` (4465 tests) | 5 failed — **all 5 pass in isolation**; ordering pollution in a suite Stage 7 does not touch |
| launcher `test/features/mission_control/` | **5291 passed, 1 skipped** |

**The reds are not Stage 7's, and here is the proof rather than the claim.** All
of them are `tests/agent_runtime/test_stream_contract_fixture.py`, failing on
`AgentCreateRefusal … placement_id 'qa_fixture' is not a deliberate-placement
id`. That fence was introduced by `d941c01db1` (office R1, "a placement id must
be classifiable by both repos"), and `git merge-base --is-ancestor d941c01db1
3d0a17922d` answers NO — it arrived AFTER Stage 7's first commit, through a
concurrent session's merge of `origin/main`. Stage 7 touched neither
`scripts/generate_agent_runtime_stream_fixtures.py` nor `agent_create.py`.

**That lane is already chasing this exact class and has not found this site.**
`012956ab67` — *"five suites build their placement ids dynamically, and only
running them found it"* — plus `fc4c4e8308` for a sixth. The stream-fixture
GENERATOR (`scripts/generate_agent_runtime_stream_fixtures.py:829`) is a
seventh: it hardcodes `placement_id="qa_fixture"`, which the new fence refuses,
so the goldens cannot be regenerated at all. Handed over here rather than fixed,
because regenerating stream goldens from inside a gateway stage would put
another lane's contract bytes in a gateway commit.

**A shared-index near-miss worth recording.** Mid-session another agent had a
CHERRY-PICK in progress with a conflict, and a plain `git add` of one Stage 7
test file put that file into THEIR staged set — one `--continue` away from
landing in their commit. Backed out with `git restore --staged <path>` (which
leaves their staged files and the conflict exactly as found), then waited for
`.git/CHERRY_PICK_HEAD` to disappear before committing. The memory entry's rule
— *stage and commit in one breath* — has a corollary: **check for an in-progress
merge/cherry-pick before staging, because "one breath" is not atomic when
somebody else's operation is already holding the index open.**

## 2026-08-27 — Stage 5's hermes half: per-subscriber promotion, and a resume that is finally asked for

**Brief.** Two rows the launcher-side plan assigned here: R10's recorded
consequence (the fold intersection demotes a whole room to its narrowest
subscriber) and Stage 2's recorded gap (watermark resume is client-side only —
hermes re-sends the full hydrate on every reattach).

### R10: the intersection was never wrong, the FRAME SHAPE was

`accepted_fold_entities` takes the intersection across every attached
subscriber, and its docstring's argument is airtight *for the wire it was
written against*: one producer feeds N subscribers, every frame is fanned to all
of them, so a batch may only be promoted when everyone can fold it. Union is the
bug, aimed at the wrong client.

What that argument actually rests on is an unstated premise — **a fan-out can
deliver exactly one shape of a frame**. Remove the premise and the conclusion
goes with it. A batch the room disagrees about now ships as ONE `fold_variants`
envelope carrying the promoted patch AND the demoted core, and each
subscription's pump resolves it against its own declaration on the way to its
own sink. The producer promotes at the union; the demotion moves from the ROOM
to the SUBSCRIBER.

**No extra build is paid, and that is the load-bearing check.** The core inside
the envelope is the one the intersection rule was already making for everybody —
a batch that any subscriber could not fold demoted the whole room, so the
snapshot build happened then too. What changes is who receives the megabyte, not
how many megabytes are made. A batch nobody can fold never reaches the envelope
(bare core), and a batch everybody can fold never reaches it either (bare patch,
no core built at all), so it exists exactly on the batches where the room
genuinely disagrees.

### The gate is a containment, and the equivalence is tested rather than asserted

`batch_required_fold_tokens` is `batch_is_patch_coverable` re-expressed as the
SET it tests rather than the boolean it returns. Every declaration-dependent
branch in the coverage classifier is a membership test against `declared` and
every other branch is a flat refusal, so
`coverable(e, d) == (req(e) is not None and req(e) <= d)` holds by construction.

It is held by a TEST over the real event vocabulary against nine declaration
shapes, not by a docstring, because an equivalence maintained in two places by
hand is one that drifts — and it would drift silently: every homogeneous test
would keep passing while the bare-patch path used one rule and the split gate
used another.

### The single-subscriber pin is a property of two lines, not a promise

With one declaration the union and the floor are the SAME set, so
`required <= accepted` takes every batch `required <= promote` could have and
the split branch has no input that reaches it. Pinned twice: at the gate (with
the snapshot builder monkeypatched to RAISE, so "no core was built" is proven
rather than inferred) and end to end against a real `serve_loop`.

### Two things the wire says differently, and one it deliberately does not

The per-connection `subscribed` ack now answers with THIS client's accepted set.
It used to be able to come back narrower than asked because a neighbour folded
less; per-subscriber promotion retired that, so the honest content of a
per-connection field is that connection's own answer. For a single subscriber it
is the same value — which is why the launcher's byte-pinned `subscribed.json`
capture does not move.

The hydrate keeps echoing the INTERSECTION, and that asymmetry is deliberate:
one hydrate is fanned to N subscribers and the floor is the only value true for
every recipient. A subscriber may then be handed patches ABOVE that floor, which
it can fold by construction — the fan-out only routes a patch to a declaration
that covers it — so the echo is a guarantee and never a ceiling.

**`test_a_subscriber_declares_what_it_can_fold_and_the_ack_says_what_was_accepted`
asserted the OPPOSITE, and the inversion is the fix rather than a relaxation.**
Read as a product sentence, the old assertion said a phone may cost a desktop its
patch lane.

### The envelope cannot reach a wire, and that is structural

It is resolved on the consumer thread at the last moment before each sink — the
only point in the fan-out that knows both the frame and WHO is about to receive
it. A subscriber that declared nothing resolves through the historical set
exactly as the coverage gate reads it, so a lane that never learned to declare
keeps folding what it always folded and takes the core for everything else. The
office push lane declares explicitly for exactly that reason: reading it as the
historical set would have handed its sink a core for the very rows it exists to
patch.

**One honest cost, stated where it is paid:** the hub's byte bound over-counts a
split frame, because `_frame_bytes` measures once on the producer thread before
anyone knows which half each subscriber takes. Measuring per subscriber would put
the serialization on the fan-out — the cost that class exists to avoid — and the
error is in the safe direction: a stalled reader is dropped slightly sooner than
its real backlog, never later.

### The resume, and the restart-free join it needed first

Stage 2 recorded the gap correctly: `subscribe` took no resume parameter, the hub
restarts its producer so a rejoin's first frame is a hydrate, and the launcher's
watermark could only feed its own `>`-gate — it paid for the megabyte and then
dropped it.

A resume is now the journal's tail from the client's own position, expressed as
ordinary v2 `patch` frames chained `base_offset`→`watermark`. **No new fold
contract, no new frame type, no second projection.** And the case a phone
actually hits is the empty span: back after ninety quiet seconds, the watermark
IS the tail, and the honest answer to "what did I miss" is nothing at all — zero
frames where it used to be a full core.

**The half that took the real thinking: a honoured resume must NOT restart the
producer.** A restart re-baselines the room, so a resume that restarted would
hand the client the very hydrate it just proved it did not need and charge every
other subscriber a fresh core for it. `restart_producer=False` already existed
for the office lane — and its own docstring names the precondition: the joiner
must not be NARROWING what the room may promote, because a producer whose floor
was frozen at build time keeps emitting BARE patches inside that floor, and a
joiner that folds less answers them with re-hydrates.

So the room is now read LIVE, once per drain pass, instead of once per producer
(`stream_frames(fold_room=…)`). The next pass sees the narrowed floor and splits
instead. This also retires a wart the old comment had to accept: a LEAVE could
not re-widen the running producer without restarting it and charging everyone a
core; re-read, a leave re-widens for free.

**The window that remains, measured and named:** one drain pass wide — a batch
already GATED when a declaration lands can still go out bare. It costs the joiner
one resync and cannot lose an event, because the client's `base_offset` gate
refuses a patch it cannot chain. Written into
`serve.py::_accepted_fold_entities` rather than left for a reader to find.

### Every refusal is named, because a silent fallback is the expensive one

`patch_lane_disabled`, `journal_unreadable`, `journal_truncated`,
`watermark_ahead_of_journal`, `backlog_exceeds_cap`, `span_not_foldable`,
`span_without_patch_rows` — each on the ack. A resume that quietly fell back is
indistinguishable from one that worked and cost a megabyte, which is the
false-all-clear class this workstream exists to retire.

`journal_truncated` needed a new primitive. `iter_from_offset` is a TAILER: it
skips a slice whose file is missing and yields what remains, which is right for a
tail and exactly wrong for a resume, where a skipped slice is a silent hole in a
span the client is about to fold as contiguous. `resume_floor_offset()` walks
BACKWARDS and answers the start of the earliest slice from which every later one
is present, so a hole raises the floor above it rather than being averaged out.

### One assertion the measurement moved

The first draft asserted that deleting the live slice makes the floor `None`. It
does not, and it cannot: once the file is gone, the bytes that would say how much
it held are the bytes that are missing, so the floor has no honest way to tell
"deleted with content" from "not created yet" — and refusing the latter would
make a brand-new install the one runtime that can never resume. What DOES catch
it is the TAIL: `log_end_offset` collapses to the live slice's start, and a client
holding a real position is refused as ahead of the journal. The test now pins
that, and names the bound it leaves (a client at or below the collapsed tail is
told there is nothing to replay, and is re-baselined by the producer's next core
— a store somebody deleted under a running serve, not a case this lane repairs).

### Numbers

* Promotion, real serve + two real socket clients + a real event log, one office
  write: **desktop patch 407 B, phone core 7,968 B — 19.6x** on a test store
  whose core is small; the field core is ~1 MB.
* Resume, resolver lane: a two-event span is **429 B** against a **7,406 B**
  hydrate on the same store. The empty span is **0 B**.
* Suites: the gateway/serve/stream group **420 passed, 0 failed**, run with
  `--timeout=300` because this machine takes 65 s on
  `test_stream_stale_first_routing`'s repo-wide AST walk and the repo's cap is
  30 s — environmental slowness, not a change of mine; the same file passes
  cleanly on its own.

### The known foreign red, unchanged

`tests/agent_runtime/test_stream_contract_fixture.py` has 3 failures owned by the
office lane's placement-id fence. Confirmed pre-existing before any change here
and untouched: this work forced no golden regeneration, so the collision the
brief warned about never arose.

### The landing, and a shared-index incident worth the space

Mid-stage, an external branch-repoint moved `main` to `origin/main`
(`refs/heads/main@{0}: branch: Reset to origin/main`), discarding ~96 files of
local commits from four lanes into the shared index. **Resolved: `main` was
restored with full history, and both halves of this stage landed on it.**

Recorded because the RECOVERY is the reusable part.

*What was done at the moment of discovery, before anything else.* The promotion
commit was reachable only from the reflog — in the object store, on no ref, and
therefore exposed to GC and to any further branch movement. So:
`git branch gateway-s5-hermes 782e87f6f9`, which creates a ref and touches
`main`, `HEAD`, the index and the worktree not at all. Then both halves out to
patches (`format-patch` for the commit, `git diff <commit> -- <paths>` for the
uncommitted slice). Preservation first, diagnosis second.

*What was deliberately NOT done, and this is the judgement worth keeping.* The
96-file staged set was not committed. Committing it would have swept four lanes
into one commit under one lane's message — the exact failure the shared-index
rule exists to prevent, arriving through the door marked "never leave work
untracked". Nor was `main` repointed back unilaterally: restoring a shared
branch is not a decision one lane makes on behalf of four.

*What the reflog could prove.* Two facts settled authorship. `git reset` writes
`reset: moving to <sha>`; this entry reads `branch: Reset to origin/main`, which
is a branch-repoint and not a reset. And the `HEAD` reflog had NO entry for the
move — `HEAD@{0}` was still this lane's commit — which a reset performed in this
working tree necessarily writes. A branch ref that moved out from under a `HEAD`
which never moved is not something a command in this worktree did.

**The lesson, as a rule rather than a story: a reflog is only forensic evidence
if you read it BEFORE doing anything that writes to it.** The first instinct on
finding a lost commit is to fix the branch, and every one of those fixes appends
an entry that makes the original question harder to answer. Read first, preserve
with operations that do not write to the ref you are reasoning about, and hand
the restore to whoever owns the branch.

## 2026-08-28 — Stage 8: what the media survey actually found

**Brief.** Build the `fetch` verb family: content-addressed handles for chat
media and Stage-C proof, size-bounded, scope-checked, so remote clients stop
needing install-local paths. The brief named a discovery step and made it part
of the stage. This is that step's record.

**Read, in this order.**

- `agent_runtime/models.py`, `agent_runtime/persona_chat_history.py:1492-1549`,
  `agent_runtime/mission_chat_turns.py` — looking for a media field on a chat
  record.
- `agent_runtime/persona_runtime.py:430-441` — the runtime's own prompt, which
  is where the `MEDIA:` protocol is actually specified.
- `gateway/platforms/base.py:1620-1760` (`extract_media`,
  `MEDIA_TAG_CLEANUP_RE`, `validate_media_delivery_path`,
  `MEDIA_DELIVERY_SAFE_ROOTS`, `_MEDIA_DELIVERY_DENIED_PREFIXES`) and
  `gateway/platforms/api_server.py:840-900` (`_MEDIA_IMG_EXT`, `_MEDIA_MIME`,
  `_MEDIA_DATA_URL_MAX_BYTES`).
- `agent_runtime/chat_live_log.py` whole file — the mirror, its root capture,
  `LIVE_LOG_TEXT_LIMIT`, `LIVE_LOG_ROTATE_BYTES`.
- `agent_runtime/paths.py` — every store-root child, looking for a blob store.
- `agent_runtime/serve_socket.py:340-360` (`MAX_LINE_BYTES`) and `:2130-2180`
  (`_LineReader`) — the frame bound, and WHICH direction it bounds.
- The launcher's render path:
  `lib/features/messaging/content/text/local_document_reference.dart`,
  `local_image_attachment.dart:960-1000`,
  `lib/features/media/fullscreen_image_source.dart:400-405`.

**Four findings, and the first two changed the design.**

1. **There is no media field on any chat record, on either side of the wire, and
   there is no blob store under the runtime root.** An image reaches an operator
   as a `MEDIA:<absolute path>` line inside the message TEXT, and that is the
   entire protocol. The launcher's model chain agrees: `AgentChatMessage` and the
   shared `ChatMessage` both carry `content`/`text` and nothing else. So the
   stage's phrase "chat media travels as install-local filesystem paths" is not
   a shorthand — the path is literally the whole record, and the client's only
   pointer to a picture is a string it parsed out of prose.

   The consequence for the design: a client CANNOT compute a content handle,
   because it holds a path and not the bytes. Some verb has to carry it from one
   to the other, which is why the family is `index` + `get` and not `get` alone.

2. **"Chat media" and "Stage-C proof" are ONE artifact family, not two.** The
   Stage C skill's contract is to reply with a `MEDIA:<absolute path>` line, so a
   proof screenshot reaches the launcher through exactly the chat-media lane. The
   live corpus on this machine confirms it in one grep: of six resolvable
   declarations, four are `X:\tmp\stagec\screenshots\*.png` and two are generated
   chat images under `.hermes/profiles/base/cache/`. So one derivation covers
   both halves of the stage's title, and a second "proof" surface would have been
   a second name for the same thing.

3. **The cap did not need to be invented — this repo had already answered it.**
   `gateway/platforms/api_server.py:851` inlines `MEDIA:` files as base64 data
   URLs for remote OpenAI-compatible frontends, capped at
   `_MEDIA_DATA_URL_MAX_BYTES = 5 MiB`. That is the same question about the same
   protocol for the same reason ("remote frontends can't read local file paths"),
   so Stage 8 reuses the number rather than minting a second policy on one lane.

   Measured before adopting it, because the brief asked for real sizes: 428
   Stage-C screenshots under `stagec-smoke-local`, 166,382,102 B total, median
   351,423 B, largest 2,146,781 B. Seven generated chat images, 1,138,544 –
   1,422,827 B. The whole 175-file image corpus under both hermes homes tops out
   at 2,722,628 B. **A 1 MiB cap — the number `MAX_LINE_BYTES` uses, and the
   obvious first guess — would refuse a real artifact, measured.** 5 MiB clears
   every image with headroom and refuses the 1.1 GB MP4 in
   `stagec-smoke-local/videos`, which is the refusal being correct.

4. **`MAX_LINE_BYTES` bounds the wrong direction to matter here, and the launcher
   has no bound at all.** `_LineReader` is the SERVER reading a CLIENT's line; a
   `runtime.media.get` request is ~200 bytes. The reply is a server→client frame,
   and the launcher reads with `Utf8Decoder` + `LineSplitter`
   (`lan_socket_connector.dart:1185`), which has no per-line ceiling. So a 5 MiB
   artifact at ~6.99 MB of base64 rides one frame and touches neither bound. That
   is why no ranging is built: not because ranging is hard, but because nothing
   this machine produces needs it.

**What contradicted the brief.** Two things.

*The brief says "prefer deriving handles server-side at read time from the same
stores that already know the artifacts."* That is exactly what shipped — but the
store that knows them is not the one a reader would guess. The transcript of
record is SessionDB (`state.db`, 15 MB on this machine), whose schema is
upstream's; the thing that is greppable, file-shaped and already a projection of
those transcripts is `chat_live_log`'s mirror, which exists because a head agent
asked for exactly this in 2026-08. The derivation reads the mirror. The honest
cost is recorded in the module: the mirror caps a line at
`LIVE_LOG_TEXT_LIMIT = 8000` characters and does not carry intermediate
assistant rows, so the scope is a SUBSET of what a transcript could offer — and
it fails CLOSED (an unknown handle), never open.

*The brief's scope rule — "artifacts reachable from data their tier already lets
them read" — is necessary and is NOT sufficient, and this is the finding worth
carrying forward.* `MEDIA:` is a line the MODEL writes. Reachability alone
therefore makes "whatever the model typed" fetchable, and a model that typed
`MEDIA:~/.ssh/id_rsa` would have written an exfiltration primitive into a chat
log. `gateway/platforms/base.py` already knows this — its denylist comment says
so outright — but its `validate_media_delivery_path` is wrong for THIS question:
a 600 s recency window and a cache-root allowlist would refuse yesterday's
Stage-C proof, which is the artifact the stage exists to deliver. So the bound
that shipped is an extension allowlist, which makes a credential
*unrepresentable* in the handle namespace rather than *rejected* by it. The
honest cost, stated rather than hidden: video and PDF are not fetchable at all.

**One measured thing that reads as a bug and is not.** Of 17 `MEDIA:`
declarations in the live corpus, only 6 resolve to a file. The other 11 point
into `characters/.drafts/<stamp>/`, which the charsheet lane sweeps. A declared
artifact that is gone is not an error and not a handle — which is also why the
index reports `scanned.declarations` separately from the artifact count.

**What a later session should not re-derive.** The peer surface was deliberately
not widened. `PEER_METHOD_ALLOWLIST` admits nothing it was not edited to admit,
so registering two verbs excluded them from the cross-install surface with no
edit at all, and Stage 6's iterated registry test covered them the moment they
existed. Cross-install media is a real question (an operator on install A
looking at a chat B ran) and it is a different one: an install's artifacts are
its operator's, and a peer is another runtime whose agents drive it. Open row,
not an oversight.
