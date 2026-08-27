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
