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
