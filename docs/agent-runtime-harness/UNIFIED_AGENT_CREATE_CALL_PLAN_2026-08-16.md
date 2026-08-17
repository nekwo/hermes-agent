# The unified agent create call — one function, every lane, and a persona that must exist (Plan F, 2026-08-16)

> **Home.** Hermes repo, beside `AGENT_CREATE_ONE_CALL_PLAN_2026-08-16.md` (Plan A, whose
> AC-1 **landed** at hermes `0439ad42bd` / launcher `2a2db7467` — this plan starts where
> that one stopped) and `OFFICE_WRITE_VERBS_RPC_PLAN_2026-08-16.md` (Plan E, whose WV-L5
> already owns the occupied-chat half of this ask; §5 UC-L5 defers to it rather than
> duplicating it). Register numbers `R#nn` resolve to
> `OFFICE_GESTURE_FOLD_PROMOTION_PLAN_2026-08-16.md` §10.4.
>
> Repos as read: hermes `b6f11b04c5` (main), launcher `3633cba4f` (main) — RAN, `git log`.

**Evidence tags** (the family's discipline): **READ** (file:line inspected this session) ·
**RAN** (command/grep executed this session) · **RELAYED** (operator/coordinator statement,
not verified on disk by me — includes today's live-root probe, which I did not and must not
re-run) · **INFERRED** (follows from READ code, not executed) · **ASSUMPTION A-n**
(unverified; UC-0 verifies before anything builds on it). Nothing in this document is
MEASURED by me: no live process was touched and nothing under `X:/Eternia/.hermes/` was read
or written this session.

**The operator's ask, verbatim (RELAYED):** *"we need a unified function command that does
everything correctly memery safe and sends the rpc."* Read as: one entry point that a CLI
verb, the RPC, scripts and any future MCP tool all share; atomic (no torn state, no orphan
artifacts); and no silently-invalid persona binding.

## 0. Verdict up front — including three corrections to the brief

The atomicity half of the ask is **already built and landed**: `runtime.agent.create`
(`agent_runtime/serve_rpc.py:1010-1276` READ) performs roster row + chat root + office
placement in one handler with a recorded-progress reservation
(`agent_runtime/agent_create_reservations.py` READ) and a compensating retire
(`serve_rpc.py:975-1007` READ). What is missing is exactly what the brief says — reachability
and validation — with three corrections:

1. **The "safe path" does not validate persona ids either.** `runtime.agent.create` accepts
   `persona_id: "qa_agent"` today. `normalize_agent_create` checks only that the id is a
   non-empty, tokenizable string (`agent_runtime/agent_create.py:200-220` READ); the roster
   lookup that exists in the same module, `resolve_persona` (`:124-150` READ), is consulted
   **only** to compute the display-name fallback (`:285`, via `honest_default_display_name`
   `:94-121`) — a miss falls through to the title-cased id and the create proceeds. There is
   no test for an unknown persona in `tests/agent_runtime/test_serve_rpc_agent_create.py`
   (RAN, `def test_` listing) because there is no refusal to test. So the defect the operator
   proved on the argv lane (RELAYED: `qa_agent` accepted silently, three artifacts bound to
   nothing) is **reproducible on the RPC lane too** (INFERRED from the code above) — the
   unified call closes the torn-state gap, not the bogus-persona gap. Both gaps are this
   plan's to close, and in ONE place.
2. **The occupied-chat flow emits no `[MissionAgentCreate]` receipt at all.** The brief says
   its receipts say `lane=twoCall`. The only `lane=twoCall` receipts in the launcher are the
   palette-drop degrade arms (`mission_control_page.dart:2462,2514` READ; RAN grep — five
   occurrences of the tag, all `gesture=drop`). The occupied-chat "add instance" flow
   (`:2263-2276` plan mint → consumed at `:1965-1974` and `:2137-2147`, both READ) runs the
   two-call dance **silently**. Plan E's WV-L5 names "`gesture=addInstance` receipts finally
   distinguishable" as something it *buys*, i.e. does not exist yet.
3. **The reproduced argv command cannot have run verbatim as quoted.** `harness persona
   instance create` requires `--title` and `--message` (argparse `required=True`,
   `hermes_cli/harness.py:909,915` READ) and refuses without `--display-name`
   (`persona_commands.py:495-508` READ) — the quoted probe carries none of the three. The
   two findings stand regardless: the `--add-instance` branch never touches `OfficeStore`
   (R#37's shape — `:417-429` READ, no placement write anywhere in the handler), and nothing
   on that path refuses an unknown persona (`_persona_by_id` returns `None` for an unknown
   bare id, `:5908-5926` READ, and the handler never checks the return). The probe's
   *conclusions* are confirmed by the code; its quoted command line is incomplete.

**The shape of the fix.** The policy layer is already shared
(`agent_runtime/agent_create.py`, born in AC-1); the **orchestration** — reserve → mint →
place → compensate/resume — is not: it lives inline in the RPC handler, welded to
JSON-RPC `rid`/`ok`/`err` envelopes. Hoist the orchestration into the same module as a typed
function, make `serve_rpc` a translation shim over it, add the roster refusal to the shared
normalizer, then hang a CLI verb (`harness agent create`) off the same function. Scripts and
cron get the CLI verb (which works serve-absent — the reservation lock and every store lock
are cross-process file locks, `agent_runtime/locks.py:22-54,107-121` READ); MCP tools get
whichever transport they already speak; the launcher's remaining two-call lane is WV-L5's.

## 1. What `runtime.agent.create` actually does today (the contract, all READ)

Registered `@method("runtime.agent.create")` at `serve_rpc.py:1010`; advertised by the
manifest like every method (decorator registry — Plan A A-R3, re-confirmed by RAN grep).

**Params** (`agent_create.py:192-289`): required `persona_id` (non-empty string; a
`profile:`-prefixed id keeps its prefix through `_normalize_instance_source_persona`,
`persona_assignments.py:2268-2273`; an id that would *collapse* to the literal token
`persona` is refused pre-store, `agent_create.py:206-219`), `workspace_id` (non-empty),
`position: [x, y]` (finite numerics; bools refused, `:167-189`), `idempotency_key`
(non-empty, ≤240 chars). Optional: `display_name`, `placement_id` (omitted → server-minted
`<token>_<hex8>`, `:153-164`; sent-but-untokenizable → refused, `:250-262`), `realm_id`,
`folder` (default `"Agents"`), `correlation_id`.

**Ordering** (`serve_rpc.py:1083-1267`):
1. Normalize/refuse (`-32602` + `{reason}`) — provably before any write.
2. `store.surface_exists(workspace_id)` — unknown workspace → `4001 workspace_not_found`,
   deliberately **before** the reservation so a typo leaves no receipt (`:1090-1101`).
3. `reserve_agent_create` under `agent_create_lock(digest)` — a keyed cross-process file
   lock nothing else takes (`locks.py:107-121`); a replayed key is scope-checked
   (same persona + workspace, else `idempotency_conflict`,
   `agent_create_reservations.py:215-226`).
4. Replay dispatch: `done` → recorded reply + `idempotent_replay: true` (`:1111-1114`);
   `rolled_back` → the recorded refusal again, never a new placement id (decision D-A3 —
   retire tombstones **burn** the placement id via `assert_bindable`,
   `persona_assignments.py:1399-1403`); `instance_minted` → skip the mint, resume the
   placement (`:1128-1150`).
5. Mint: `PersonaInstanceStore().add_instance(...)` (`:1152-1161`), stamp
   `spawned_by="operator"` (`:1185`), then `mark_instance_minted` — **durable before the
   placement is attempted** (`agent_create_reservations.py:106-121`); a crash before this
   line wrote nothing, after it is resumable.
6. Placement: `placement_actor_payload` (instance-keyed by construction,
   `agent_create.py:292-319`), class-key guard run anyway as defence (`:1201-1213`),
   `store.upsert_actor` (`:1216`).
7. Any placement failure → `_agent_create_failure` compensates: `retire` through the same
   chokepoint the delete gesture uses; success → `rolled_back: true` + `4090`; a retire that
   itself raises → state stays `instance_minted` with `rollback_error` recorded and the
   reply says `rolled_back: false` honestly (`:975-1007`;
   `agent_create_reservations.py:143-161`).
8. `mark_done(result)` then reply: `{persona_instance_id, persona_id, placement_id,
   display_name, default_chat_session_id, actor_key, revision, workspace_id,
   phases:{instance_ms, placement_ms, total_ms}, idempotent_replay}` (+`correlation_id`
   echo) (`:1249-1267`).

**What it refuses:** malformed params, unknown workspace, retired placement id
(`instance_retired`, `:1162-1171`), key reuse across persona/workspace, a still-held key
(`create_lock_unavailable`), a rolled-back key's replay. **What it does not refuse:** a
persona id that names nothing. Said plainly: **`runtime.agent.create` does not validate the
persona id against the roster.** `resolve_persona(persona_id)` returning `None` changes only
the display name.

## 2. Entry-point inventory — every way an agent is born today

RAN: greps for `add_instance(`/`create_operator_chat(` callers (hermes, non-test:
exactly `persona_commands.py:422,603`, `serve_rpc.py:1154`), for `persona.instance.create`/
`persona.profile.instantiate` submit sites (launcher), for `missionAgentCreateClientProvider`
readers (one: `mission_control_page.dart:2456`), for MCP surfaces (`**/*mcp*` — zero create
tools; `agent/transports/hermes_tools_mcp_server.py` contains no persona/harness verbs), and
the argparse tree (`harness.py:905-982,1293-1308` — no `agent create` verb; `harness agent`
has only `list`/`set-profile`).

| # | Door | Route | Unified handler? | Placement? | Persona validated? |
|---|---|---|---|---|---|
| 1 | Launcher palette drop (persona already placed) | `_addDroppedAgentInstance` → `_createDroppedAgentOverRpc` → **`runtime.agent.create`** when advertised (`mission_control_page.dart:2393-2520` READ; landed AC-2, launcher `2a2db7467` RAN) | **Yes** (rpc lane) | Yes, in-call | **No** (§0.1) |
| 2 | Same gesture, serve absent / method unadvertised | falls through to `_submitIntent(persona.instance.create)` + debounced `runtime.office.upsert` — the two-call dance, receipt `lane=twoCall gesture=drop` (`:2427-2436,2462,2514` READ) | No | Second lane, ≥600 ms later | No |
| 3 | Launcher occupied-chat "add instance" | plan minted `:2263-2276` → `persona.instance.create` argv intent (`:1965-1974`) or `persona.instance.open_chat --add-instance` (`:2137-2147`) — **always two-call, no receipt** | No | Second lane | No |
| 4 | Launcher roster-only create (template/preset browser) | `persona.profile.instantiate` intent (`:3477-3561,4798` READ) → same argv verb, no placement **by design** | No (different door, stays) | Deliberately none | No (`profile:` synthesizes, `persona_commands.py:5923-5940` READ) |
| 5 | CLI `harness persona instance create --add-instance` | `_cmd_persona_instance_create` → `add_instance` (`persona_commands.py:387-508` READ) | No | **Never** — the handler has no office write (R#37 by construction) | **No** (`_persona_by_id` → `None`, unchecked) |
| 6 | CLI `harness persona instance open-chat --add-instance` | `_cmd_persona_instance_open_chat` → `add_instance` (`:560-612` READ) | No | Never | No |
| 7 | Coordinator-driven create (relay) | doors 5/6 with `authorize_coordinator_action("persona.instance.create"/".open_chat")` gating (`:402-414,522-534` READ) | No | Never | No (auth gates the *actor*, not the persona id) |
| 8 | JSON-RPC `runtime.agent.create` direct (scripts speaking the serve socket) | `serve_rpc.py:1010` | **Yes** | Yes | **No** |
| 9 | MCP tools / cron | **no door exists** (RAN) — scripts today can only shell out to door 5/6 (unsafe) or speak raw RPC (door 8, needs a serve) | — | — | — |
| 10 | Canonical per-persona instances | `ensure_for_personas` materializes one instance per *configured* persona (`persona_assignments.py:1710+` READ) — not a gesture lane, listed for completeness | n/a | n/a | Roster-driven by construction |

Bold summary: **one gesture on one client** reaches the unified handler; every other door is
either the two-call dance or roster-only; **no door validates the persona id**.

## 3. Where the shared handler should live

**Module:** `agent_runtime/agent_create.py` — it already exists, is imported by both lanes
today, and its own docstring records why `persona_commands.py` is not importable (exec'd
into `harness.py` globals, `agent_create.py:26-37` READ; `harness.py:3667-3674` per that
note). A new sibling (`agent_create_service.py`) is acceptable if the module grows past
taste, but the seam is the same either way.

**Function (proposed):**

```python
@dataclass(frozen=True)
class AgentCreateRefusal:
    code: int                      # the ERR_* vocabulary serve_rpc already uses
    message: str
    data: dict[str, Any]           # {"reason": ..., "rolled_back": ..., ...}

@dataclass(frozen=True)
class AgentCreateOutcome:
    result: dict[str, Any] | None  # exactly today's reply dict, phases included
    refusal: AgentCreateRefusal | None

def perform_agent_create(
    params: dict[str, Any],
    *,
    updated_by: str = "operator",
    persona: Any | None = None,    # the CLI's richer pre-resolved persona, as today
) -> AgentCreateOutcome: ...
```

Everything currently between `serve_rpc.py:1083` and `:1267` moves inside; the RPC handler
becomes `outcome.result → ok(rid, …)` / `outcome.refusal → err(rid, code, message, data)`
and nothing else. The reply dict and every `data.reason` string are **byte-identical** —
the launcher's `missionAgentCreateReasonFrom` decoder (`mission_agent_create_rpc.dart:133-169`
READ) is the fielded consumer that pins them. Callers after this plan: the RPC shim, the
`harness agent create` verb (UC-H3), and any future MCP tool wrapper — one sequence, zero
copies.

## 4. Persona-id validation

**Where the roster is read.** `ensure_persisted_personas(load_agent_runtime_config())`
(`agent_runtime/config.py:570-579` READ): the persisted `AgentStore` rows
(`store.py:160-178` → `paths.agents_dir()`, i.e. `agent-runtime/agents/*.json` under the
runtime root — the file family the operator listed, RELAYED, not read live) merged over the
config-declared catalog, stored ids winning. `agent_create.resolve_persona` (`:124-150`) and
the CLI's `_persona_by_id` (`persona_commands.py:5908-5940`) both consume it; the CLI
resolver additionally **synthesizes** a persona for any `profile:<token>` id, even one whose
profile owns nothing (`profile_persona_resolution` returns `None` matches without raising,
`agent_runtime/personas.py:154-184` READ) — the browser's preset lane depends on that.

**Which layer owns the refusal:** `normalize_agent_create` — the one function that already
runs before any store write on the unified lane, whose docstring promises "a refusal here
provably wrote nothing" (`agent_create.py:195-198`). A new branch: a **bare** persona id
that `resolve_persona` cannot find → `AgentCreateInvalid("persona_not_found", …)`. A
`profile:` id keeps the CLI's synthesize semantics in the first cut (decision **D-U1**,
§8.2). The store (`add_instance`) stays permissive — it is also the restore/rebind
chokepoint and refusing there would need an audit of every historical row's persona id;
the refusal belongs to the *create* policy layer, stated as such in the module docstring.

**Is adding validation to the existing `persona instance create` path a breaking change?**
For any caller sending a roster persona id: no. For the callers that exist (checked):

- Launcher `persona.instance.create` intents send `instance.personaId`, always taken from a
  live roster instance or snapshot row (`mission_control_page.dart:4711-4740,2400-2436`
  READ) — valid by construction.
- Launcher `persona.profile.instantiate` sends `profile:` ids (`:4798` READ; bridge lowers
  both capabilities to the same argv, `mission_control_bridge.dart:3337-3388` READ) — the
  D-U1 carve-out keeps this lane byte-identical.
- Coordinator lane: same handlers, persona ids drawn from cfg personas
  (`persona_commands.py:399-414` READ) — ASSUMPTION A-2 that no coordinator composes a
  persona id from free text; UC-0 greps the relay callers.
- Hermes-internal: the only non-test `add_instance` callers are the two CLI handlers and
  the RPC handler (RAN, §2) — no internal caller depends on a bogus id being accepted.

So the only behavior that changes is the erroneous-input class: today it **fail-opens** into
three durable artifacts bound to nothing; after UC-H4 it fail-closes with a typed error.
That is a deliberate contract change, staged separately (UC-H4) so it can be reverted
independently of the unified verb, and named in the commit as the R#37-adjacent silent-mint
fix. One caller class it could bite (ASSUMPTION A-3, UC-0): an operator recreating an
instance for a persona whose config row was deleted after the instance was born. The
refusal message must name the cure (`harness agent list`, or re-add the persona).

## 5. Stages

### UC-0 — verify the assumptions (read-only, both repos)

- **A-1**: WV-L5 landed or not at execution time (RAN today: not landed — launcher log has
  AC-2 `2a2db7467` but no occupied-chat commit). If landed, UC-L5 is a checkbox.
- **A-2**: no relay/coordinator caller composes persona ids from free text (grep the
  relay/coordinator submit paths for `persona.instance.create` arguments).
- **A-3**: enumerate live callers of doors 5/6 beyond the launcher and coordinators
  (operator scripts, skills, docs that teach the verb) before UC-H4 flips them fail-closed.
- **A-4**: confirm the serve-stream producer picks up a CLI-process create's events the way
  it picks up the argv fallback lanes' today (INFERRED from the poll-drain design; confirm
  the drain has no serve-process-only filter).
- Output: this table updated; no code.

### UC-H1 — hermes: hoist the orchestration into `agent_create.perform_agent_create`

**Change surface.** `agent_runtime/agent_create.py` (or sibling): the §3 function, moved
verbatim from `serve_rpc.py:1083-1267` + `:975-1007`; `serve_rpc._runtime_agent_create`
becomes the translation shim. No reply byte moves, no reason string moves, no event moves.

**Tests.**
- The existing `test_serve_rpc_agent_create.py` suite passes **byte-unchanged** — the
  fence that the shim is faithful.
- New direct-call test: `perform_agent_create(params)` with no `serve_rpc` import in the
  test path produces both rows. **Probe:** the office actor's `revision == 1` and the
  instance file exists in `paths.persona_instances_dir()`. Why the mutation cannot also set
  it: the kill-mutation is "shim keeps its own inline copy and the extracted function is
  dead" — then the direct call writes nothing at all, and a file-existence probe cannot be
  satisfied by the mutated path because the mutated path never runs the code under test.
- Single-copy grep-gate (the doc-03 pattern): `PersonaInstanceStore().add_instance` and
  `upsert_actor` appear in `agent_runtime/serve_rpc.py` **zero** times after the hoist —
  kill: leave a second inline sequence behind.

**Rollback.** Revert. **Does NOT do.** Add validation, add a verb, touch the launcher.

### UC-H2 — hermes: the roster refusal, in the one shared normalizer

**Change surface.** `agent_create.py`: `normalize_agent_create` gains the
`persona_not_found` branch per §4 (bare ids strict against `resolve_persona`; `profile:`
ids pass-through per D-U1). `serve_rpc` needs no edit — the shim already translates
`AgentCreateInvalid` (`serve_rpc.py:1087-1088` semantics, now inside the service).

**Tests** (extend `test_serve_rpc_agent_create.py` + a direct-service case):
- `an unknown bare persona is refused persona_not_found and provably wrote nothing` —
  **probe:** after the refusal, `paths.persona_instances_dir()` contains no new file AND the
  reservation receipt path for the key's digest does not exist AND the event-log length is
  unchanged (the suite's `_event_count()` helper, `test_serve_rpc_agent_create.py:95-98`
  READ). Why the mutation cannot also set it: the kill-mutation (delete the roster check)
  makes the create *proceed*, which necessarily creates the instance file and the receipt —
  the probes are **absences**, and the mutated path's whole effect is to make those files
  exist. It cannot pass by also writing the probed state; writing it is what turns the
  probe red.
- `a seeded persona still creates` (the over-broad-guard witness, reusing the `qa_persona`
  fixture whose display name differs from its title-cased id, `:101-124` READ).
- `a profile: id keeps today's behavior` per D-U1 — kill: apply the bare-id check to
  `profile:` ids (the browser lane's regression).

**Rollback.** Revert — fail-open returns. **Does NOT do.** Touch the legacy argv handlers
(UC-H4) or the store.

### UC-H3 — hermes: `harness agent create` — the operator's unified command

**Change surface.** `hermes_cli/harness.py`: `agent_subs.add_parser("create", …)` beside
`list`/`set-profile` (`:1293-1308` — slot free, RAN). Args: `--persona` (required),
`--workspace` (required), `--pos X Y` (required), `--display-name`, `--placement-id`,
`--realm-id`, `--folder`, `--idempotency-key` (default: mint `cli-<uuid4>` — a re-run is a
new gesture, exactly the launcher's micros-stamp rule; a script that wants resume-on-retry
passes its own key), `--json`. Handler (in `persona_commands.py`, following that file's
function-local-import discipline): build the params dict, call
`perform_agent_create(params, persona=_persona_by_id(cfg, persona_id))`, print the result
dict (plus `"ok": true/false`) — the SAME dict the RPC returns, so a script cannot tell the
lanes apart. Exit 0 / 2 / 4 mapped from the refusal code family the way the office verbs map
theirs. Works serve-absent by construction: every lock in the path is a cross-process file
lock (`locks.py:22-54,107-121` READ) and the argv fallback lanes already write beside a live
serve today.

**Tests** (new `tests/hermes_cli/test_agent_create_verb.py`):
- `the verb creates both rows and echoes the RPC reply shape` — probe: `actor_key` present
  and the office store holds it; kill: route the verb to the old `add_instance`-only
  sequence — the office probe reds because that sequence contains no office write anywhere
  (§2 door 5), so the mutated path cannot set the probed field.
- `re-running with the same --idempotency-key replays and writes nothing` — **probe: the
  actor's `revision` is still 1 and the second reply carries `idempotent_replay: true`.**
  Why the mutation cannot also set it: the kill (bypass the reservation, call the stores
  directly) re-upserts, and `upsert_actor` bumps the revision monotonically — the mutated
  path cannot re-write the actor *without* moving the probed field. (This is the suite's own
  recorded lesson: ids re-derive identically, revisions do not —
  `test_serve_rpc_agent_create.py:10-13` READ.)
- `an unknown persona refuses at exit 2 with persona_not_found and no store write` — same
  absence probes as UC-H2.
- `the verb refuses a missing workspace before any write` — kill: reorder.

**Rollback.** Revert; the verb disappears, nothing else moves. **Does NOT do.** Deprecate
doors 5/6 (their non-placement, roster-only semantics are still the browser lane's
contract), touch the launcher.

### UC-H4 — hermes: the legacy argv lanes stop minting for phantom personas

**Change surface.** `persona_commands.py`: `_cmd_persona_instance_create` and
`_cmd_persona_instance_open_chat --add-instance` refuse when `_persona_by_id` returned
`None` for a bare id (one shared helper in `agent_create.py`, e.g.
`require_known_persona(persona_id, persona)`, so the refusal string and the D-U1 carve-out
have one spelling). Error payload names the cure and the roster verb.

**Breaking-change statement** (§4, restated for the commit): fail-open → fail-closed for
bogus bare ids only; launcher and coordinator callers unaffected (ids roster-sourced);
`profile:` untouched; A-3's caller sweep is the precondition.

**Tests** (`tests/hermes_cli/`, invoking the handlers the way the existing CLI tests do):
- `create --add-instance with an unknown persona exits 2 and mints nothing` — probe:
  `persona_instances_dir()` listing unchanged AND no chat session row was ensured (the
  handler's `_ensure_persona_chat_session` runs after the mint, `persona_commands.py:443-450`
  READ, so the absence of the session is a second independent witness). Same
  absence-vs-mutation argument as UC-H2.
- `create with a roster persona still mints` and `profile:x still instantiates` — the
  two non-breaking witnesses, one per caller class in §4.

**Rollback.** Revert this commit alone — the unified lane keeps its refusal (UC-H2 is
independent). **Does NOT do.** Add placements to these verbs (they stay the roster-only /
recovery doors; create-with-placement is UC-H3's and the RPC's).

### UC-L5 — launcher: occupied-chat "add instance" takes `runtime.agent.create`

**Owned by Plan E's WV-L5** (`OFFICE_WRITE_VERBS_RPC_PLAN_2026-08-16.md` §3), which
specifies the helper extraction (`_createPlacedAgentOverRpc({required String gesture, …})`),
the plan-variant carrying `createdOverRpc`, the adoption-block feed, the id-drift log, and
five tests including the double-create kill. Not restaged here — one owner, one spec. This
plan adds only a sequencing note: UC-H2 landing **before** WV-L5 is the good order, so the
first gesture the new lane carries is already persona-validated server-side (not that the
launcher can send a bogus id from a roster row — but the fence should predate the traffic).

### UC-6 — observation window and two-call retirement (recorded, not staged)

The receipts (`lane=rpc|twoCall`, now on both gestures once WV-L5 lands, plus the verb's
own JSON results) feed R#42's exit criteria. Deleting the two-call arms is
`SINGLE_TRANSPORT_COLLAPSE_PLAN` TC-3/TC-4's call and Plan A AC-3's — not this plan's, and
deliberately so: shipping fallback deletions inside a feature plan is the delete-and-see
this program keeps getting burned by.

## 6. Sequencing constraints

- UC-H1 → UC-H3 (the verb calls the extracted function; no function, no verb).
- UC-H1 → UC-H2 (the refusal lands in the service the shim already fronts; landing it
  pre-hoist would mean editing `serve_rpc` twice).
- UC-H2 → UC-H4 (one refusal spelling, shared helper first).
- UC-H2 before UC-L5/WV-L5 preferred (fence before traffic), not required — mixed pairs
  degrade honestly either way (an old runtime simply keeps accepting bogus ids, which is
  today's behavior, and the launcher never sends them).
- UC-L5 collides with nothing in this plan's hermes stages (launcher-only) but is the same
  file WV-L5 owns — whoever lands second rebases, and Plan E is the spec of record.
- Standing constraints, inherited verbatim from Plan A §8: fork-owned files only; Python
  tests 30 s cap, no `integration` marker; never write under `X:/Eternia/.hermes/`; no
  casual `harness serve`; additive params/replies only; `RPC_CONTRACT_VERSION` stays 1.

## 7. Deliberately left out, and why

- **A dedicated MCP tool for agent create.** No MCP surface exposes any harness verb today
  (RAN, §2) — inventing the first one is a policy decision (which server, which admission
  role — cf. the launcher_qa admission ruling) that dwarfs this plan. After UC-H3, an MCP
  tool is a thin wrapper over the verb or the RPC; the seam is ready, the tool is not
  scoped.
- **Retiring doors 5/6.** `persona.profile.instantiate` and roster-only creates are a
  *different door on purpose* (no placement is the feature, §2 door 4); the create verbs
  also remain the serve-absent recovery lane until R#42's window closes them deliberately.
- **Store-level persona validation** (`add_instance` refusing) — §4: the store is also the
  rebind/restore chokepoint; refusing there needs a historical-row audit no one has priced.
- **`profile:` existence validation** — D-U1 keeps synthesize semantics; tightening it is a
  browser-lane behavior change to decide out loud, not to smuggle in.
- **Prediction, the demote, naming authority, position policy** — owned elsewhere
  (UP-*, D3 landed, D-A1) and unchanged by anything here.
- **Recording the R#nn register somewhere durable** — Plan C's named work item.
- **Cleaning up the operator's live `qa_agent_probe01` artifacts** — live-root surgery,
  operator's hands only; this plan must not touch the runtime root (its three artifacts are
  also the honest regression fixture for UC-H2's refusal message, described from RELAYED
  facts, never re-read).

## 8. Adversarial pass — what I most expect to be wrong

1. **D-U1 (`profile:` pass-through) may preserve a real hole.** A script sending
   `profile:tpyo` still mints today and would still mint after UC-H2. Chosen because the
   browser preset lane provably depends on synthesis (§4) and its ids come from a picker;
   if UC-0's A-3 sweep finds free-text `profile:` senders, D-U1 flips to "profile must
   resolve to an existing hermes profile" and the browser lane gets the witness test
   instead.
2. **The retired-persona recreate (A-3's edge).** A persona removed from config whose
   operator wants its instance back hits `persona_not_found` after UC-H4. The refusal
   message carries the cure; if UC-0 finds this is a real workflow, UC-H4 gains an
   `--allow-unknown-persona` operator escape on the CLI only — never on the wire (the
   upsert's "a wire parameter is not consent" ruling, copied).
3. **Reply-shape drift between shim and verb.** Two printers of one dict can drift on JSON
   encoding (the CLI's `emit_json` vs the RPC's transport). The parity test pins dict
   equality, not bytes — if a consumer ever depends on bytes, that is its bug to state.
4. **The event-parity inheritance.** UC-H1 moves code that emits nothing itself (stores
   emit); the two-call-parity test in the existing suite re-fences it. If that test needed
   editing during the hoist, the hoist changed behavior and owes an explanation — same rule
   as Plan E's paint fences.
5. **Unverified live, all of it** — same confession as every plan in this family:
   source-read at the SHAs in the header; the live defect reproduction is RELAYED from the
   operator's session; no gesture, serve, or live-root read happened in this one.

## 9. Verification log

| # | Fact | How established |
|---|---|---|
| U-R1 | `runtime.agent.create` full contract: params, ordering, reservation states, D-A3 burn, compensation honesty, reply shape | READ serve_rpc.py:975-1276; agent_create.py:1-319; agent_create_reservations.py:1-286 |
| U-R2 | **No persona-roster refusal on the RPC lane**; `resolve_persona` feeds display-name only; no unknown-persona test exists | READ agent_create.py:94-150,200-220,285; serve_rpc.py:1086; RAN test listing of test_serve_rpc_agent_create.py |
| U-R3 | CLI create/open-chat handlers never write a placement; `_persona_by_id` → None goes unchecked; `--title`/`--message` argparse-required; display-name-less create refused | READ persona_commands.py:387-508,560-612,5908-5940; harness.py:905-946 |
| U-R4 | `add_instance`/`open_chat`/`assert_bindable` validate placement/session/retirement, never persona existence | READ persona_assignments.py:1340-1404,1432-1469,1646-1685 |
| U-R5 | Non-test `add_instance` callers: exactly two CLI handlers + the RPC handler | RAN grep |
| U-R6 | Launcher: RPC lane routes only `gesture=drop` (single provider read at :2456); occupied-chat consumers at :1965/:2137 are argv two-call; no `[MissionAgentCreate]` receipt on that flow | READ mission_control_page.dart:1965-1974,2137-2147,2263-2276,2393-2520; RAN grep (5 tag sites, all drop) |
| U-R7 | Both create capabilities lower to one argv verb; roster-only browser lane sends `profile:` ids | READ mission_control_bridge.dart:3337-3388; mission_control_page.dart:4711-4740,4798 |
| U-R8 | No `harness agent create` verb; `agent` subparser slot free; no MCP create tool anywhere | RAN greps; READ harness.py:1293-1308 |
| U-R9 | Roster authority = AgentStore rows ∪ config catalog; CLI synthesizes `profile:` personas without existence check | READ config.py:570-579; store.py:160-178; personas.py:154-184; persona_commands.py:5921-5940 |
| U-R10 | All locks in the path are cross-process file locks; `agent_create_lock` taken by nothing else | READ locks.py:22-54,107-121 |
| U-R11 | AC-1/AC-2 landed (hermes `0439ad42bd`, launcher `2a2db7467`); WV-H1 landed (`4bc61567c1`); **WV-L5 not landed** | RAN git log, both repos |
| U-R12 | WV-L5 owns the occupied-chat unification, with A-3 (adoption tolerates null previous session) fronted there | READ OFFICE_WRITE_VERBS_RPC_PLAN §3 WV-L5, §6.2 |
| U-R13 | R#10/R#37/R#42 register rows; R#37's argv shape confirmed in code (U-R3); no register row is contradicted by code, but R#10 does not record that Plan A landed | READ fold plan §10.4 (:1160-1187) |
| U-R14 | Live probe: `qa_agent` accepted, no placement, three orphan artifacts; real personas base/backend_dev/dev/neko_supervisor/qa | RELAYED (operator, 2026-08-16) — conclusions confirmed in code (U-R2/U-R3); quoted command incomplete (§0.3) |
| U-A1 | WV-L5 still unlanded at execution time | ASSUMPTION — UC-0 |
| U-A2 | No coordinator composes persona ids from free text | ASSUMPTION — UC-0 |
| U-A3 | No live caller depends on fail-open bogus-id creates; retired-persona recreate workflow does not exist | ASSUMPTION — UC-0 |
| U-A4 | Serve stream drains CLI-process creates like the argv fallback lanes' writes | ASSUMPTION (INFERRED from poll design) — UC-0 |
