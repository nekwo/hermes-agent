# Field notes — remote gateway lane

A running record, newest section last. Not a plan and not canon: what a working
session actually READ, and where what it read disagreed with the brief it was
given. Rows graduate into [remote-gateway.md](remote-gateway.md), into
[authorization-chokepoint.md](authorization-chokepoint.md), or into canon — and
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
  `docs/mission_control/planned/universal-remote-gateway.md:524-527`, `:568-578`,
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
