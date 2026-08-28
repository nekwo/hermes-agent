# Operating the Mission Control Harness

The operating loop for Tony's Hermes Agent Runtime Harness and Launcher Mission Control.
The goal is not "make a patch"; the goal is to make Mission Control behave like a competent
multi-agent runtime you can actually operate — where a turn you send lands, does real work
with real tools, and answers with evidence you can check.

The model this stands on — the removed-verb list, the two messaging paths, the
persona/instance/session vocabulary, the graph-is-a-picture and board-is-planning-state
rules, the non-negotiables, and the base View/Operate tables — is in this package's
`SKILL.md` (required preload). This file does not restate it; it carries the rows and
recipes that file leaves out. Siblings: `persona-chat.md` (operator channel, relay),
`proof.md` (proof commands and Stage C hazards), `debugging.md` (parity, UI divergence).

Every one-off request — verification, MCP use, investigation, agent coordination — is chat
work: select a persona instance, open or continue a chat root, message it. Do not
manufacture a heavier route; there isn't one.

Three standing facts:

- **The roster is data — read it, never quote a count.** 19 persona-instance rows live
  2026-08-24; it was 15 on 2026-07-30. The authority is `persona list --json`, not this
  file.
- **`harness snapshot --json` is the parity anchor.** Contract **54** at
  `.parity.contract_version` (measured live 2026-08-28; it was 45 on 2026-07-30), snapshot
  envelope **schema 2** at top-level `.schema_version`. It carries no goal, stage, run,
  proof, or incident sections — if you are looking for one, it is **gone, not missing**.
- **Realms and workspaces survive** as the scoping/publishing substrate, and their list
  verbs answer under `.items` (see the table below).

In-flight architecture — check these brain notes before bridge/perf work
(`Launcher_Brain/20 — Active Initiatives/`):

- `mission-control-harness-serve.md` — the serve bridge (`harness serve --ndjson`),
  SHIPPED and live-verified 2026-07-08, plus the read-model cache slice and measured
  latency numbers. Read it before re-diagnosing chat/poll latency.
- `mission-control-snapshot-architecture.md` — snapshot / stream architecture.
- `mission-control-parity-audit.md` — dated defect/closeout log; check it before
  re-diagnosing a known divergence.
- `mcp-lane-cold-root-cause-2026-08-26.md` — why the MCP lane read cold for weeks and what
  the real discriminator is. Read it before re-diagnosing an admission that looks dead.

## Roots

- Harness repo: `X:\Eternia\hermes-agent`
- Runtime root: `X:\Eternia\.hermes\agent-runtime` — the harness **store**: persona
  instances, offices, boards, flow graphs, event log. It is shared, and it is what
  `persona list` / `snapshot` read.
- Operator-CLI profile home: `HERMES_HOME=X:\Eternia\.hermes\profiles\alice`. Use it for
  CLI inspection, and know what it does and does not decide — see the caveat below.

**`base` vs `alice`: which home answers which question (measured 2026-08-28).**
The launcher-spawned serve child does **not** run under alice. Mission Control spawns
`hermes harness serve --ndjson` with `HERMES_HOME=<root>\profiles\<hermesProfile>`, and
that setting defaults to **`base`** (`hermes_process_identity.dart`, `hermesProfile = 'base'`
→ `'HERMES_HOME': '$root\profiles\$resolvedProfile'`). A persona's own `hermes_profile`
binding then redirects `HERMES_HOME` **in-process for the duration of its turn**, so one
serve process serves N personas under N homes; there is no single home that is "the
runtime's".

What the home does **not** change: the store. Measured under both homes on 2026-08-28,
`harness snapshot --json` returned the identical `.parity.runtime_root` fingerprint,
identical contract 54, the same 19 persona-instance rows, and the same 50
`persona_chat_history` sessions. Chat transcripts resolve through the pinned head home in
`<store_root>\chat_head_home.json` (`X:\Eternia\.hermes\profiles\base`, source
`env_head_home`, recorded 2026-07-27) whichever home you set — so alice's much larger
`state.db` is history, not the live transcript authority.

What the home **does** change: `.parity.profile`, and everything read out of that
profile's `config.yaml` — the `mcp_servers:` declarations, provider/model bindings, auth.
That is exactly the layer that governs whether a chat turn admits MCP and which model it
runs. So: **roster/office/board/graph questions are home-independent; admission, model,
and profile questions are not** — and inspecting those from a CLI under alice measures a
different profile than the launcher-spawned serve turn you are trying to explain. Say
which home produced any receipt you report.

- Backend repo: `X:\Unreal Engine\Engine\EterniaBackend\eternia-backend`
- Launcher repo: `X:\Unreal Engine\Engine\Launcher\EterniaLauncher`
- Launcher brain: `X:\Unreal Engine\Engine\Launcher\EterniaLauncher\Launcher_Brain`
- Shared brain: `X:\Unreal Engine\Engine\ArcadiaLabs_Brain`

## Start Checklist

Run these first when relevant:

```powershell
$env:HERMES_HOME = 'X:\Eternia\.hermes\profiles\alice'
git -C "X:\Eternia\hermes-agent" status --short
git -C "X:\Unreal Engine\Engine\Launcher\EterniaLauncher" status --short
python -m hermes_cli.main harness status --json
python -m hermes_cli.main harness snapshot --json
```

`alice` here is the operator-CLI profile home, not the runtime's. The store these commands
read is shared, so the roster/office/board answers are the same either way; but the
launcher's serve child runs under `profiles\base` (or the persona's own bound profile), so
a profile-scoped answer — `.parity.profile`, MCP declarations, model/provider bindings —
is NOT the answer that serve turn saw. Re-run under `profiles\base` when the question is
about a launcher-spawned turn. See "base vs alice" under Roots.

Use `workdir="X:\Eternia\hermes-agent"` for Harness CLI commands. The `hermes` entry point
(e.g. `hermes harness snapshot --json`) is equivalent to `python -m hermes_cli.main`.
Always pass `--json`.

If Launcher/Stage C proof will be captured, check for stale processes first:

```powershell
Get-Process | Where-Object { $_.ProcessName -like '*eternia_launcher*' -or $_.ProcessName -like '*stagec_qa_mcp_server*' } | Select-Object Id,ProcessName,Path
```

Stop only stale proof processes that block rebuild/capture. Do not use stale-process cleanup
as an excuse for unrelated test failures.

## Inspecting the runtime

The base View table — status/doctor, persona list/show/tool-diff, chat history, flow,
board, realm/workspace, snapshot, skills, and the two Stage C probes — is in this
package's `SKILL.md`, including its ruling that `status.agents` / `agent list` show
configured agents and **never** which instances are placed on a level. These are the rows
that table does not carry, plus the payload-shape gotchas that have actually cost time:

| See | Command |
|---|---|
| the live roster (durable persona instances) | `harness persona list --json` — the loop below starts here every time |
| an instance's resolved/blocked tools, with the MCP lane | `harness persona-instance detail <instance_id> --json` · `harness persona tool-diff <persona> --explain-mcp --json` |
| realms / workspaces | `harness realm list --json` · `harness workspace list --json` — **both answer under `.items`**, not `.realms`/`.workspaces` (verified live 2026-08-24) |
| live hydrate + delta frames | `harness stream` (NDJSON; `--resync` re-baselines) |
| redaction-safe observability | `harness observe --json` |
| offices and the actors an operator actually sees | `harness snapshot --json` → `.offices[<ws_id>].actors` (`office show --workspace <id>` returns `actors` as a COUNT, not a list) |

## Operating the live chat lane

1. **Find the target.** `harness persona list --json` → the chat-mode
   `personainst_<role>_agent_<hash>` rows. Cross-check against the Stage C
   `mission_control.agent` buttons if the ask is about what the operator can see.
2. **New task ⇒ NEW session (operator ruling, 2026-08-09).** Do not default to the
   instance's `default_chat_session_id` for a fresh unit of work — a long-lived root
   bloats every subsequent turn with stale context (a reused 08-04 root was hauling
   ~200k input tokens per turn by 08-09) and buries each task's receipts in an
   unrelated transcript. `--new-session --title` on the message itself is the easy
   one-call way; no separate mint step needed:

```powershell
python -m hermes_cli.main harness mission-chat message `
  --persona <persona_id> --persona-instance-id <instance> `
  --new-session --title "<short task name>" `
  --client-message-id <unique> --message "<text>" --json
```

   The payload's `session_id`/`root_chat_session_id` is the fresh root — capture it
   for follow-ups. Reuse an EXISTING root (`--session-id <root>`) only for follow-up
   turns on the SAME task, steering, or clarify replies; `--new-session` is ignored
   when `--session-id` is given, so the two lanes cannot conflict.

   **QA tasks go further: fresh INSTANCE per task (operator ruling, 2026-08-09).**
   A new session on a shared QA instance still collides with concurrent QA work
   (chat-root leases, operator UI threads on the same instance). Place a NEW QA
   agent on the level instead — verified live end to end 2026-08-09:

```powershell
# 1. Create + place (placement id mints the instance id: personainst_<placement_id>)
#    No --message/--auto-run here — see step 2.
python -m hermes_cli.main harness persona instance create `
  --persona qa --add-instance --placement-id qa_agent_<8-hex> `
  --workspace-id <ws_id> --realm-id <realm_id> `
  --display-name "QA - <task>" --title "<task>" --json
# 2. Send the REAL ask via the canonical chat lane to the returned session_id
#    (the create verb's --message/--auto-run is the legacy free-floating queue —
#    verified live: it lands NO turn in the canonical chat root; do not rely on it)
python -m hermes_cli.main harness mission-chat message `
  --persona qa --persona-instance-id personainst_<placement_id> `
  --session-id <session_id from create payload> `
  --client-message-id <unique> --message "<the QA request>" --json
# 3. Delete on task close — archives the row, chat history preserved
python -m hermes_cli.main harness persona instance delete personainst_<placement_id> `
  --reason "<task> complete" --json
```

   The instance appears on the workspace roster within a frame and disappears on
   delete (archive lands under `agent-runtime/persona_instances_archive/`). Never
   skip step 3 for throwaway QA instances — an accumulating roster is its own
   collision surface.

   **The delete lane (2026-08-27) — three facts that change how you drive and read it:**

   - **`delete` is the operator's verb; `retire` is the same door.** `harness persona
     instance delete` is a full alias of `retire` (`hermes_cli/harness.py:1168` —
     `add_parser("retire", aliases=["delete"])`), same flags, same behaviour. The rename
     is deliberate and stops at the surface: the RPC method, the capability id
     `persona.instance.retire`, the event types, and every internal symbol keep `retire`,
     because renaming those would break launcher builds in the field to change a noun.
     **Say "delete" to an operator, expect `retire` on the wire.**
   - **The replay SWEEPS.** Deleting an already-deleted id is idempotent but **not inert**:
     the replay archives live actors still bound to the retired instance and reports
     per-actor failures, then answers with the same keys plus `already_retired: true`
     (`agent_runtime/agent_retire.py`, `_sweep_live_placements`). So if a desk is still on
     the canvas after a delete, **asking again is the correct move** — it is the gesture
     that cleans it up, not a no-op.
   - **Archived actors refuse re-upsert, and the client must drop the row.** An
     `office actor-upsert` against a deleted key answers JSON-RPC **4090** with
     `data.reason = "actor_archived"` (`agent_runtime/serve_rpc.py` ~1401). It is
     **terminal**: not refetch-and-rebase, not retry. Re-placing that agent is a NEW create
     with a freshly minted id, never a re-add of the key. The deliberate re-add doors are
     `harness office actor-restore` and `harness office actor-upsert --resurrect`
     (`hermes_cli/harness.py:883`) — both operator gestures, and there is deliberately no
     equivalent parameter on the RPC lane, because a wire parameter is not consent. A
     client that retries this spins forever; that was a live incident.
3. **Messaging flags that matter on every send:**

   `--client-message-id` is the dedup key: reuse it on retry, never on a new message.
   `--persona` accepts a persona id, `profile:<name>`, or an instance id — the harness
   canonicalizes at one chokepoint and is the single identity authority.
4. **Pre-mint a root without sending** (rarely needed now — prefer the
   `--new-session` message above):

```powershell
python -m hermes_cli.main harness persona instance open-chat `
  --persona-instance-id <instance> --persona <persona_id> `
  --new-session --idempotency-key <key> --json
```

   `--session-id` is required unless `--new-session` or `--add-instance` is given. Hermes
   mints every root; a caller may use a local draft identity only while awaiting the result.
5. **The rest of the verb surface** — `mission-chat steer`, `mission-chat queue-skill`,
   `persona instance return-summary`, `persona instance steer` / `flow set` (re-wire the
   picture, never the work — neither creates instances or dispatches anything), and
   `board card add` (planning state only, and say so when you report it) — is spelled out
   in this package's `SKILL.md` Operate table. Use it from there rather than from memory.

## Diagnosing a stalled or failed chat turn

Work the receipts, in this order. Do not code-spelunk first.

1. **Read the returned payload.** A healthy turn reports `run_ids: []`, its
   `client_message_id`/`turn_id`, `profile_timing` (including `mcp_admitted_servers` and
   `mcp_calls_spent`), and the tool trace. A prose claim with no trace row is fabrication,
   not a slow turn.
2. **`chat_turn_outcome_unknown` — do not retry it.** Resolve the exact
   `(session_id, client_message_id, turn_id)` tuple, then resend as a fresh turn:

```powershell
python -m hermes_cli.main harness mission-chat turn-resolve --session-id <root> `
  --client-message-id <id> --turn-id <turn> --action abandon --json
```

3. **`unknown_chat_session` ("unknown explicit persona chat root")** — the roster pointer
   went stale. Do not keep retrying. Mint a fresh root with `open-chat --new-session
   --idempotency-key <key>` and message that.
4. **`budget_exhausted`** — terminal, and there is no turn-resolve for it. The turn spent
   its `--max-seconds` wall budget (default 240s, or the profile's
   `agent_runtime.mission_chat.default_max_seconds`); the last max(60s, 15%) is reserved
   for a final checkpoint reply. Raise the budget deliberately or split the ask.
5. **Wrong root, or the right root and the wrong profile — two different faults.** An
   EMPTY or unfamiliar roster is a ROOT fault: check `.parity.runtime_root` (a
   `%LOCALAPPDATA%` shadow root is the classic). A roster that looks right while the turn
   behaves wrong — no MCP, unexpected model — is a PROFILE fault: check `.parity.profile`
   and remember the launcher's serve child runs under `profiles\base` or the persona's own
   bound profile, not your CLI's `alice`. Switching `HERMES_HOME` will not change the
   roster (measured 2026-08-28); it changes which `config.yaml` answered.
6. **Relay refusals** are typed and carry lineage: `relay_depth_limit`, `relay_cycle`,
   `relay_budget_exhausted`. Read the `relay_chain` on the refusal rather than guessing.
7. **Transcript truth** lives in `harness persona chat history --session-id <root> --json`
   and the snapshot's `persona_chat_history` / `persona_chat_trace` projections. If the
   UI and those disagree, go to `debugging.md` and start from `.parity`.
8. **Duplicate or vanished agent rows** — never add a dedup heuristic. Check the parity
   warning `duplicate_persona_instance`, then repair with
   `harness persona-instance reconcile [--dry-run] --json`.

Treat a stall as a Harness gap and name it: which command, which typed error, which
receipt was missing. "It hung" is not a report.

## Placing and operating a QA agent (verified live 2026-08-24)

The fresh-instance-per-QA-task ruling above works; these are the details it omits,
each re-verified live because the surrounding text had already drifted.

1. **Get the ids first — they are not guessable, and the payload key is `items`.**

```powershell
$env:HERMES_HOME = 'X:\Eternia\.hermes\profiles\alice'
hermes harness realm list --json      # .items[].id  → realm_codex-test-realm_cad6d4
hermes harness workspace list --json  # .items[].id  → ws_codex-test-workspace_28d285
```

   Either home answers these identically — realms, workspaces and the roster live in the
   shared store, not the profile (measured 2026-08-28; see "base vs alice" under Roots).
   The home matters from step 4 on, where the turn's profile decides MCP admission and
   model: the launcher's serve child runs under `profiles\base` or the persona's own bound
   profile, so quote the home with the receipt.

   A workspace row also carries `agent_ids` / `live_scoped_agent_ids` — that is how you
   check an instance is actually ON the level rather than merely existing.

2. **Create + place in one call. The payload hands you a messageable root — there is no
   separate `open-chat` step.**

```powershell
hermes harness persona instance create `
  --persona qa --add-instance --placement-id qa_agent_<8-hex> `
  --workspace-id <ws_id> --realm-id <realm_id> `
  --display-name "QA - <task>" --title "<task>" --json
# → ok, persona_instance_id = personainst_qa_agent_<8-hex>
#   session_id = persona_chat_personainst_qa_agent_<8-hex>_<12-hex>   ← message THIS
```

   `--placement-id` mints the instance id (`personainst_<placement_id>`), so the 8-hex
   suffix is yours to choose and must be unique. Do NOT pass `--message`/`--auto-run`
   here: that is the legacy free-floating queue and lands no turn in the canonical root.

3. **Place it in the Mission Office — a SECOND, SEPARATE write.** `persona instance
   create --add-instance` puts the instance on the workspace ROSTER only. The office is a
   different document (`snapshot.offices[<ws_id>].actors`, one file per actor), and nothing
   in the create path touches it. Skip this and the operator sees no agent, while every
   roster query says it is there — verified live 2026-08-24.

```powershell
# actor.json: {persona_id, persona_instance_id?, backing_profile?, items:[...]}
# items are the placed props: one `desk` and one `agent`, each with position + scale.
# {"persona_id":"qa","persona_instance_id":"personainst_qa_agent_<8-hex>","items":[
#   {"item_id":"desk-<slug>","kind":"desk","folder":"Desks","persona_id":"qa",
#    "display_name":null,"pet_slug":null,"position":[-8.2,-5.4],"scale":1.0},
#   {"item_id":"personainst_qa_agent_<8-hex>","kind":"agent","folder":"Agents",
#    "persona_id":"qa","display_name":"QA - <task>","pet_slug":null,
#    "position":[-8.05,-7.2],"scale":1.0}]}
hermes harness office actor-upsert --workspace <ws_id> `
  --actor-json <path-or-inline> --persona-instance-id personainst_qa_agent_<8-hex> `
  --updated-by operator --json
```

   Read the existing placements first so you do not stack agents on one spot:
   `hermes harness snapshot --json` → `.offices[<ws_id>].actors[].items[].position`.
   The store mints `actor_key`. **Two escape hatches, orthogonal and neither implying the
   other** (`hermes_cli/harness.py:879-883`): `--allow-class-key` consents to the KEY
   SHAPE — forcing a class-keyed write the class→instance re-key migration would refuse —
   while `--resurrect` consents to raising a DELETED key, clearing its tombstone. Reaching
   for the first when the actor was deleted is the common mix-up; the sanctioned un-archive
   verb is `harness office actor-restore`, and both flags are last resorts.
   Other verbs: `office show --workspace <id>` (note: `--workspace`,
   NOT `--workspace-id`, and it returns `actors` as a COUNT, not a list — use the snapshot
   for the actors themselves), `actor-remove` / `actor-restore` (archive-never-delete),
   `set-folders`, `resolve-conflict`.

   **`--expect-revision` is per-ACTOR, not per-surface.** Passing the surface revision for
   a NEW actor fails `stale_revision: expected 10, have None`. Omit it when creating.

   **Adding an actor does not bump the surface's `revision`/`updated_at`** (one file per
   actor). A UI that re-renders on surface revision will not notice a CLI-placed actor;
   expect to refresh, and suspect this first when a placement "did not work".

   **A persona instance on the roster and the agent the operator sees are not the same
   thing.** On 2026-08-24 the roster held three `qa` instances while the office rendered
   exactly one — a different one from the instance a chat turn had been sent to.

4. **Send the real ask on the returned root**, then **delete** on close (step 3 of the
   ruling above, and read the delete-lane notes there) — an accumulating roster is its own
   collision surface. The delete archives the instance AND sweeps the office actor you
   placed in step 3, so the operator's canvas clears with it.

## MCP admission: read the receipt, never the agent's prose

`profile_timing` on every turn carries the only trustworthy account of what the turn
could call:

| field | means |
|---|---|
| `mcp_admitted_servers` | how many servers this turn can actually call. **0 means no MCP tools, whatever else anything says.** |
| `mcp_admission_transport` | per server: `warm` = already connected **with a live session**; `cold` = everything else, including a parked/mid-reconnect server whose `session` is `None` |
| `mcp_admission_cold_servers` | count of the above |
| `mcp_admission_ms` | wall time of the admission. **Not a spawn discriminator — see below.** |
| `mcp_call_budget` | per-run tool-call ceiling |

**Do not use the clock to decide whether a server really spawned (corrected 2026-08-26).**
An earlier revision of this text — and of `mcp_admission.py`'s own docstring — recorded
"~3,200 ms = a real cold spawn", measured once against a different 60-tool server.
`launcher_qa` is a compiled Dart exe and its real cold spawn measured **~100 ms**, so that
rule of thumb reads a genuine spawn as a fast failure; it sent a whole investigation down
the wrong branch. **The discriminator is `mcp_admitted_servers` being non-zero (equivalently,
`registered_mcp_server_names()` non-empty) — never the elapsed time.** The clock only ever
separated ~0 ms from "something happened".

Typed refusals (`agent_runtime/mcp_admission.py`): `mcp_admission_lane_busy` (another
admission in flight; registration is process-global and serialized), `mcp_admission_timeout`
(registrar still running, may land for a LATER turn), `mcp_not_registered_on_lane`
(the registrar returned but the server is not in the registry). **There is no fallback
lane behind any of these** — the old `qa.request_screenshot` contract went with the
mission lane, so a denial is terminal for that route.

**An agent can report "admitted" while holding no MCP tools.** Verified live 2026-08-24:
one turn's reply said all three servers were admitted while `mcp_admitted_servers` was
`0` and no `mcp__*` tool existed in its callable schema. Believe the receipt.

**Scope note (2026-08-26) — the "chat lane admits nothing" gap is CLOSED.** Through
2026-08-25 this skill carried a standing gap saying a chat turn always got
`mcp_admitted_servers: 0`, that launching the Launcher did not fix it, and that Stage C
visual proof therefore could not be driven from a Mission Control chat turn. **That
premise was false and must not be planned against.** Root cause: hermes declares the MCP
client SDK as an *optional* pip extra (`mcp = ["mcp==1.26.0", …]`) and no provisioning
path installed it, so `tools/mcp_tool` set `_MCP_AVAILABLE = False` and
`register_mcp_servers()` returned `[]` in 0.00 ms — the runtime had no MCP client at all.
Admission, declaration, resolution and policy were all healthy the whole time. The live
venv was repaired and the launcher installer now passes the `mcp` extra
(`mission_control_hermes_installer.dart`). The lane works; drive Stage C from a chat turn.
Full evidence: `Launcher_Brain/20 — Active Initiatives/mcp-lane-cold-root-cause-2026-08-26.md`.

**The one diagnostic worth keeping from it.** If `mcp_admitted_servers: 0` with
`mcp_not_registered_on_lane` ever reappears, **check that the venv can import the SDK
first** — before re-auditing config, declarations, or roots:

```powershell
X:\Eternia\.hermes\venvs\hermes-agent\Scripts\python.exe -c "import mcp; print(mcp.__version__)"
```

That failure mode is invisible by design: `mcp_not_registered_on_lane` says "check the
server is running and its command resolves", which sends you to the two things that are
already fine, and the installer's own verify step (`from tools.mcp_tool import
_build_safe_env`) succeeds with the SDK entirely absent. Two further notes: the fix does
not reach a running process — `_MCP_AVAILABLE` is a module-level constant read at import,
so **restart the Hermes runtime from Mission Control** after any SDK install; and a
`mcp_servers:` block in `config.yaml` is still only a DECLARATION and proves nothing about
the live lane. The turn's receipt remains the authority.

## Stage C visual proof

There is exactly **one** sanctioned visual-proof path: the Stage C MCP server
(`EterniaLauncher/tool/stagec_qa_mcp_server`) driven through the
`launcher-mcp-operations` skill (renamed from `launcher-stagec-mcp-screenshot`
2026-08-28). Load that skill and follow it. Do not invent a
Python capture path inside `agent_runtime`, do not shell a PS1 acceptance-matrix script as
an agent, and do not substitute a generic desktop screenshot tool.

- "Screenshot what's on screen now" → message this task's QA persona/session with
  `mission-chat message` and have that admitted turn call `mcp_launcher_qa_screenshot_window`
  directly. It is a pure capture primitive: no launch, no attach, no login, no reap.
  (Which instance to message is the fresh-instance-per-task ruling above, not "whichever
  QA row is already on the level".)
- Driven proof (navigate, click, login, verify state) → the full MCP marionette control
  path in `launcher-mcp-operations`, under the QA `stagec-smoke` profile.
- **Never kill Tony's live Launcher session to take a screenshot.**
- Deliver each capture as a `MEDIA:<absolute path>` line, verbatim, on its own line.

Screenshots must be at least desktop-sized, redaction-clean, nonblank, and tied to the
intended Launcher debug build/profile — otherwise the visual proof is not complete. Known
hazards are listed in `proof.md`.

## Preserving runtime evidence

- Archive-never-delete, **including the verb named `delete`**. `board card archive`/`restore`,
  `persona instance archive`, and `persona instance delete` (alias of `retire`) all archive
  and preserve chat history; `office actor-remove`/`actor-restore` does the same for
  placements. There is no hard-delete path you should be reaching for — see the delete-lane
  notes under the QA ruling for what a deleted actor's 4090 `actor_archived` obliges a
  client to do.
- Orphan cleanup is explicit and bounded: `harness worktree reap --dry-run --json` then
  without `--dry-run`; `harness doctor --fix --dry-run --yes`; `harness persona instance
  sweep-orphans`. Preview first, every time.
- **There is no read-model to rebuild.** `harness rebuild-read-model` and `harness read`
  were unregistered on 2026-08-22 and `agent_runtime/read_model.py` + `projector.py` are
  deleted (tombstones at `hermes_cli/harness.py:1659` and
  `hermes_cli/harness_parts/runtime_commands.py:569`; absence pinned by
  `test_s46_incremental_projection_lane_removal.py`). The standing architecture is an
  O(world) snapshot built per call by `build_snapshot()`, cached under
  `<store_root>/serve_read_model/` by the serve process. Quoting the rebuild verb to an
  operator is a reporting error.
- **What actually preserves and re-reads evidence:** `harness snapshot --json` (the whole
  world, redaction-safe, with its `.parity` envelope) and `harness observe --json`
  (redaction-safe observability) for the frame; `harness persona chat history --session-id
  <root> --json` for the transcript; the SessionDB behind the pinned chat head home
  (`<store_root>/chat_head_home.json` → `profiles\base\state.db`) for durable turns; the
  event log and its rotated slices under the runtime root
  (`agent-runtime/events.jsonl`, `events_archive/`, `events_manifest.json`); and
  `persona_instances_archive/` for retired rows. Capture the snapshot rather than
  re-deriving it later — the world moves.
- **"What did the turn actually see?" is answered by prompt observability**, not by
  re-reading the transcript: durable per-turn context records under the runtime root at
  `prompt_observability/` (with `prompt_observability_index.json`,
  `prompt_observability_catalogs/` for content-hashed skills catalogs, and
  `prompt_observability_archive/`), projected onto the snapshot by
  `agent_runtime/prompt_observability.py`. Reach for it when a reply cites a field or skill
  the transcript cannot account for — before blaming the persona.
- Keep the raw artifacts: chat payloads, tool traces, screenshot paths, generated backend
  `qa_artifacts` logs. A summary that discards the receipt is worth less than the receipt.

## Final report shape

Keep the closeout blunt and evidence-based:

- the persona instance and chat root you used, and the `client-message-id` / `turn_id`;
- `run_ids: []` (the chat lane creates no runs — say so rather than omitting it);
- MCP admission / tool-count receipt and `mcp_calls_spent`, when MCP was involved;
- commands run and their actual results;
- artifact and screenshot paths;
  - Screenshot paths reach the operator as a MEDIA:<absolute image path> line on a line of
    its own, copied through VERBATIM — see the image-line rule in `SKILL.md`
    "Non-negotiables". It binds the closeout and every relay alike.
- commits created, if any;
- what worked smoothly;
- what required manual recovery;
- remaining Harness gaps, if any, named precisely.

Never say "works end to end" unless you ran it and read the output. If a step was blocked,
report the exact blocker and what you did verify — a partial result honestly bounded is
worth more than a confident summary that nobody can check.
