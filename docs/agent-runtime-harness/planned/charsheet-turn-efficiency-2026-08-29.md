# Planned — charsheet turn efficiency: standard things cost too many calls

**Owner domain:** system architecture ([01-system-architecture.md](../01-system-architecture.md)) —
the charsheet lane and the mission-chat turn loop are both named there.
**Status:** PLANNED. Nothing below is implemented; this file is the measured
accounting and the staged fix order.
**Raised / verified:** 2026-08-29 against live turn records of 2026-08-28
(`X:\Eternia\.hermes\agent-runtime\mission_chat_turns\` +
`prompt_observability[_archive]/ctx_*.json`), the skill at
`docs/agent-runtime-harness/harness-skills/harness-charsheet-authoring/SKILL.md`
(identical bytes to the installed copy under `X:\Eternia\.hermes\shared\skills\`),
and the code at `hermes_cli/harness.py`, `agent/charsheet/draft.py`,
`tools/process_registry.py`, `agent_runtime/mission_chat_turn_context.py`.
**Origin:** operator observation — "look at inefficiencies in the skill or
conversation — seems like a lot of tool calls to do standard things" — plus two
adjacent observations (tall thumbs vs the square hero card; the skill's own
length as a per-turn token cost).

## The ask

Put numbers on where charsheet authoring turns spend their tool calls and
prompt tokens, name the buckets of waste with element-level evidence, and stage
implementation-ready fixes split by lane: SKILL (say it better), HARNESS/CLI
(give the agent a cheaper verb), PROMPT/persona (stop the agent wanting the
call at all).

## Measured accounting

All from `prompt_observability` `turn_usage` (the turn-record JSONs themselves
carry no usage; the ctx files keyed by `turn_id` do) and the `elements[]`
traces. `prompt_tokens` is CUMULATIVE across the turn's API calls — every call
re-sends the whole context, so one extra call late in a turn costs the whole
prompt again (mostly cache-read, but paid and latency-bearing).

### Specimen 1 — the fire-imp one-shot (the expensive shape)

Turn `agent-chat-send-a29a195e-de8e-4b48-b8d4-f4f3c32fc99f` in
`persona_chat_personainst_neko_supervisor_agent_f6f7a51b_eaaca35693e6_6358c1be5e0c.json`;
usage in `prompt_observability_archive/ctx_a9d41330017e2b62.json`. Persona
`neko_supervisor` (role `alice_supervisor`, profile `neko`, model
`gpt-5.6-luna`), one message: "Create a new 8-way sprite character … drive it
all the way … install it into the library."

| Measure | Value |
|---|---|
| Tool elements | 36 (+1 segment) |
| API calls | **27** |
| prompt_tokens (cumulative) | **1,556,043** |
| input_tokens (uncached) | 81,483 |
| cache_read_tokens | 1,474,560 |
| output_tokens | 3,795 |
| first_call_prompt_tokens | 17,102 |
| Wall time | 1,183 s (~19.7 min; 66% of the 1,800 s turn budget) |
| Time before the first pipeline verb (`start` at 21:27:42) | 118 s, elements 0–14 |

**Overhead: 26 of 36 tool elements (72%).** Productive elements are exactly:
[7] `image_generate` (anchor), [15] `start`, [16] `turnaround`, [18]
`approve-direction --all`, [19] `rows`, [22] `agent_chat_send` (dispatched
Chara A2 for standby QA), [32] the 10-thumb loop in ONE command (good
batching), [33] `compose`, plus one progress read and one final verification
read.

**And the deliverable still failed.** `stored_reply` carries **0 `MEDIA:`
lines and 0 `CHARSHEET-QA:` lines** — the prose-only reply the skill's
"reply is the operator's only window" section now documents as measured
2026-08-28 IS this run. It cost a remediation turn
(`charsheet-contract-verify-20260828-b`: 5 more API calls, 420,025 more
cumulative prompt tokens) to show the operator the character that was already
installed.

### Specimens 2..8 — the Chara A2 staged moss-golem turns (the lean shape)

Same day, persona `chara_a2` (role `profile`, profile `base`, model
`gpt-5.6-terra`, skill **preloaded**), in
`persona_chat_personainst_chara_a2_7b31d0e4_a238c5f9c4c2_1ffc304ec1ff.json`:

| Turn | Elements | API calls | prompt_tokens (cum.) | of which wait-polls |
|---|---|---|---|---|
| `sprite-proof-gen` (base) | 3 | 4 | 410,687 | 0 |
| `sprite-proof-turnaround` | 5 | 6 | 653,968 | 2 |
| `sprite-proof-idle` | 12 | 13 | 1,501,582 | **8** |
| `sprite-proof-walk` | 8 | 9 | 1,075,190 | 5 |
| `sprite-proof-walk` (resume) | 9 | 10 | 1,220,646 | **6** |
| `sprite-proof-compose` | 3 | 4 | 504,444 | 0 |
| `sprite-proof-recompose` | 2 | 3 | 390,295 | 0 |

The lean turns run zero rediscovery, zero environment archaeology, and go
straight to the right verb — the skill in context plus a narrow staged ask is
sufficient. **Their entire remaining waste is wait-polling**: the idle turn is
8 `process wait timeout:60` round-trips out of 13 API calls (62%), ~1.0M of
its 1.5M cumulative prompt tokens spent asking "done yet?".

### Why every API call is expensive here

Average prompt per call in the fire-imp turn is 1,556,043 / 27 ≈ 57.6k tokens
and grows through the turn; on the A2 idle turn ≈ 115k/call. A single avoided
round-trip late in a heavy turn saves ~60–120k prompt tokens. That is the
exchange rate the fixes below are priced in.

## Findings by bucket

### (a) Rediscovery of knowledge the skill already answers — 6 elements

Fire-imp elements [0][1] `skill_view` (harness-charsheet-authoring +
eternia-launcher-workflow), [5][6] `read_file` of the installed
`SKILL.md`/`FIELD-NOTES.md` in 300-line windows, [12] `characters --help`,
[14] SIX per-verb `--help`s in one command (`start base turnaround
approve-direction rows compose`). The verb table at SKILL.md:151–168 answers
every one of those probes, flags included.

Root cause is split:

1. **The preload premise is false for the persona that ran the run.** The
   fire-imp ctx shows `required_preload_skills: []`,
   `preloaded_skills_loaded: []`. `load_policy: required_preload`
   (SKILL.md:8) is resolved PER PERSONA over `persona.skills`
   (`agent_runtime/mission_chat_turn_context.py:614` →
   `agent/skill_utils.py:required_preload_skill_ids`, which filters the
   persona's OWN assignment list) — the supervisor persona does not carry the
   skill, so nothing preloaded. Chara A2's ctx shows both
   `harness-runtime-model` and `harness-charsheet-authoring` loaded. The
   `skill_view` calls were therefore *rational*; the waste upstream of them is
   a persona-assignment gap, not agent indiscipline.
2. **After `skill_view` returned the skill, the agent still re-read it from
   disk and ran 7 `--help`s.** And the two 300-line `read_file` windows
   stopped at line 600 of 794 — the reply contract ("The reply is the
   operator's only window", SKILL.md:604+; rendering contract :633+) sits in
   the never-read tail, which is exactly the contract the turn then violated.
   The length is not just a token cost; it measurably defeats consumption.

The contract-verify remediation turn repeats the pattern in miniature:
[3] `search_files` for `CHARSHEET-QA` inside SKILL.md + [4] `read_file
offset 600` — same missing preload, same persona.

### (b) Environment archaeology — 5 elements

Fire-imp [2] `read_file CLAUDE.md`, [3] `read_file Launcher_Brain/Brain
Index.md`, [8][9] `search_files` for `harness` / `main.py` under `X:\Eternia`,
[10] `pwd; ls -la; git status --short`. A mission-chat authoring turn has no
repo work in it; nothing in the ask names a file. The agent reached for
repo-orientation habits because no charsheet contract was in context at turn
start (bucket a, cause 1) and nothing in the prompt stack says "this is not a
repo task". The A2 turns, with the skill in context, never ran a single one of
these.

### (c) Redundant state reads — 5 elements

Fire-imp [4] `status --json && characters list --json` (bare env), [11] the
identical pair again, [13] `status --json` a third time under an explicit
`HERMES_HOME`, [34] `list --json` piped through a key-guess that missed,
[35] `list --json` again to dump `d.keys()`. The skill's preflight prescribes
the probe pair ONCE (SKILL.md:67–70); three runs of it are the home-anxiety
spiral of bucket (d), and [34][35] are payload-shape guessing the skill's
":status answers under `.status`" section (SKILL.md:211+) exists to prevent —
unread, per bucket (a).

### (d) Wrong entrypoint / wrong-home spellings — retry chain + per-command tax

- **Home:** the [4]→[11]→[13] chain re-ran the same probes while the agent
  worked out which home it was in, then every subsequent command paid an
  `export HERMES_HOME='X:\Eternia\.hermes\profiles\neko';` prefix. The runtime
  had already rebound `HERMES_HOME` to the persona's profile home for the turn
  (SKILL.md:58–62 says exactly this) — the exports were pure defensive tax.
  The skill invites the anxiety: the preflight spends ~70 lines (SKILL.md:41–110)
  on home-resolution war stories for a library that §13.27 made install-wide,
  i.e. for a question the same section admits "has no content" any more.
- **Entrypoint:** every terminal element in both specimens spells
  `python -m hermes_cli.main`. The skill TEACHES it at line 36
  (`` `hermes` == `python -m hermes_cli.main` ``) — agents copy the long
  spelling. It works (not a retry cost), but it is noise in every trace row
  and the operator trace truncates commands at 500 chars (SKILL.md:686), so
  the tax costs trace legibility too. Note the A2 dispatch turn
  (`agent-dispatch-dispatch-23aa318aa3d4`) used bare `hermes` — it resolves.

### (e) Wait-polling during generations — the largest bucket, and it hits every turn shape

Fire-imp: 10 `process wait`/`poll` elements ([17][20][21][23]–[26][28][29][31])
plus 2 mid-wait `status` reads ([27][30]) = 12 of 27 API calls ≈ **~690k of
the 1.556M cumulative prompt tokens**. A2 idle: 8 of 13 calls. A2 walk +
resume: 11 of 19.

Mechanism, at the code: generation verbs (`turnaround`, `rows`) block in the
CLI; the terminal tool backgrounds a long command into a process session, and
`process wait` **clamps any requested timeout to `TERMINAL_TIMEOUT`, default
180 s** (`tools/process_registry.py:1554–1565` — the fire-imp's
`timeout: 600` was silently clamped; the A2 agent asked for 60 s and got an
even worse cadence). Every clamp expiry returns "process still running" and
costs a full API round-trip at 60–120k prompt tokens. A 10-strip `rows` batch
at 1–2 min/generation is 10–20 min of wall — 4–7 forced round-trips at the
180 s clamp, 10–20 at 60 s. **There is no blocking wait long enough and no
notify affordance**: the only exit-driven wake-up in the runtime today is the
agent-chat dispatch-delivery drain, which watches persona replies, not
processes.

### (f) Everything else

- **Trace element pairing skew.** In the fire-imp record, `summary`/`args` and
  `tool_input` are crossed between adjacent elements ([0]'s summary names
  harness-charsheet-authoring while its `tool_input` names
  eternia-launcher-workflow, and [1] the reverse; same for [2]/[3] and
  [5]/[6]). Concurrent tool starts are being zipped to the wrong rows.
  Small observability bug, separate fix, filed here so it is not lost.
- **The reply-contract miss is the costliest single failure** (0 MEDIA /
  0 CHARSHEET-QA after a 19.7-min run) and its cause is bucket (a): the
  contract lives at SKILL.md:604+ and the persona had no preload and never
  read that far. Counted here because the fix lands in Stages 1–2, not as its
  own machinery.
- **The 10-thumb for-loop ([32]) is the pattern to canonize**, not a defect:
  ten `thumb` invocations, one tool call. The skill nowhere shows it.

### The two adjacent observations

- **Tall thumbs vs the square hero card.** `row_thumb` emits
  `cell × scale` at source resolution (`agent/charsheet/draft.py:1170–1204`)
  — character cells are taller than wide, and the console hero card is a fixed
  1:1 centre-cover square (SKILL.md:410–417, §13.17), so a tall crop renders
  as a torso zoom on the card. The card was ruled "never the verdict surface",
  but the confusing zoom is real and the fix is cheap on the hermes side
  (Stage 4a) without touching the launcher ruling.
- **Skill length as a per-turn cost.** SKILL.md is 51,924 chars ≈ **13k
  tokens**, injected whole by `build_preloaded_skills_prompt`
  (`agent/skill_commands.py:747–821`) into every preloaded turn and re-sent on
  every API call of that turn: ≈ 169k cumulative prompt tokens on a 13-call
  turn (mostly cache-read). The token bill alone is arguable; the
  **consumption failure is not** — the one agent that most needed the tail of
  the file never reached it (bucket a). FIELD-NOTES.md (187,676 chars ≈ 47k
  tokens) is NOT preloaded and must stay not-preloaded; its only cost today is
  that SKILL.md:18 advertises it by name, which is plausibly what invited the
  disk read of it ([5]).

## Staged fixes, by value per risk

### Stage 1 — give the authoring contract to every persona that gets authoring asks (PROMPT/persona; config-only)

The single highest-value, lowest-risk change. Two options, one owner ruling
(**R-1**):

- **1a (recommended, immediate):** add `harness-charsheet-authoring` to the
  `neko_supervisor` persona's skill assignment (and to any persona the
  operator routes authoring asks at). Zero code — `required_preload` already
  keys off `persona.skills` per surface. Kills buckets (a) and (b) for the
  supervisor case (11 elements, ~2 min, and — with Stage 2 — the
  reply-contract misses).
- **1b (posture, standing):** the supervisor treats charsheet authoring as a
  delegation: on an authoring ask it dispatches Chara A2 (the persona built
  for the lane, already preloaded, on the cheaper terra model) and relays the
  staged results. The fire-imp run half-did this ([22]) and then kept driving
  the pipeline itself on luna. 1b is a one-paragraph addition to the
  supervisor persona prompt; it also moves the 16-generation spend off the
  expensive model.

Gate: re-run the fire-imp ask verbatim at the supervisor; assert the ctx shows
`preloaded_skills_loaded` carrying the skill, zero `--help` elements, zero
CLAUDE.md/git elements, and a reply carrying ≥ 1 `CHARSHEET-QA:` and ≥ 11
`MEDIA:` lines.

### Stage 2 — restructure the skill: front-load the contract, move the narrative (SKILL)

Target: SKILL.md ≤ 20k chars (~5k tokens, from 51,924/13k) with **zero canon
loss** — moved text moves verbatim into `references/` files beside it, in the
harness-skills house shape. FIELD-NOTES.md untouched.

New SKILL.md order (the first screenful is the whole standard path):

1. **Turn-zero card (~45 lines, new, top of file):**
   - "This skill is in your context. Never re-read `SKILL.md` or
     `FIELD-NOTES.md` from disk, never run `--help` on a `characters` verb —
     the verb table below is complete, flags included."
   - "This is not a repo task: no `CLAUDE.md`, no `git status`, no file
     searches."
   - Entrypoint is `hermes` (drop the line-36 equivalence from the head; the
     `python -m` spelling moves to a reference footnote for restricted
     shells).
   - "The runtime already rebound `HERMES_HOME` to your profile home. Never
     `export` it. Preflight is ONE command pair, run ONCE:
     `hermes harness status --json` (echo `.runtime_health.hermes_home` in
     prose) `&& hermes harness characters list --json`."
   - **Wait cadence:** "generation verbs run 1–2 min per image; a full `rows`
     batch is 10–20 min. `process wait` is clamped to 180 s — always pass
     `timeout: 180`, never less; do QA work between waits (thumb the rows
     that already landed) instead of polling empty-handed."
   - **Batch patterns:** the 10-thumb for-loop from fire-imp [32], and
     `approve-direction --all && rows` in one command.
2. **The verb table** (today's :147–168, moved up, unchanged).
3. **The reply contract** (today's :604–693 — MEDIA / CHARSHEET-QA /
   clarify), compressed to the shapes plus the one-line reasons. This is the
   deliverable; it must sit in the first half of the file.
4. **Refusal reading + handedness operative rules** (what blocks, what warns,
   `--accept-handedness` token rule) — kept, tightened.
5. Pointers: `references/looking-procedure.md` (today's :301–417),
   `references/handedness.md` (:419–570 measurement narrative),
   `references/note-craft.md` (:572–602), `references/homes-and-migration.md`
   (:41–110 preflight war stories + backfill/migrate-home lore :170–190),
   `references/console-and-costs.md` (:694–793). Each pointer states when to
   open it ("open only when a refusal names handedness", etc.).

Risk: canon dilution / drift from the §13 register. Mitigation: verbatim
moves, the §13 citation rule stays in the head, and the launcher companion is
not touched. Gate: the existing skill-install verifier
(`scripts/verify_harness_skill_install.py`) still passes; a fresh A2 turn's
ctx shows the smaller preload; one live staged run shows no behavior
regression on the reply contract.

### Stage 3 — kill the wait-poll bucket (HARNESS)

- **3a (config + clamp note, cheap, first):** raise the mission-chat lane's
  `process wait` clamp from 180 s to 600 s (config: `TERMINAL_TIMEOUT` /
  lane envelope; the clamp site is `tools/process_registry.py:1554–1565`,
  which already emits a `timeout_note` — keep it). A 15-min `rows` batch drops
  from 5–7 round-trips to 2–3. The 1,800 s turn wall budget bounds the risk: a
  600 s block twice still leaves headroom, and the wall budget — not the
  clamp — is the real guard. Priced at the measured exchange rate this saves
  ~300–500k cumulative prompt tokens on every generation-bearing turn.
- **3b (machinery, owner ruling R-2):** **process-exit delivery.** When a turn
  ends while a tracked process session is still running, register a watcher;
  on exit, mint a delivery turn the way dispatch replies already do
  (`dispatch_delivery_drain` precedent — the runtime already proved the
  "background completes → new turn with the receipt" shape on this very run:
  `dispatch-delivery-dispatch-23aa318aa3d4`, 2 API calls). The agent then
  *ends its turn* after firing `rows`, and the wait bucket goes to ZERO —
  additionally freeing the operator's console mid-generation instead of
  holding a 19-minute turn open. Contract sketch: `process` tool gains
  `action: "notify"` (session_id) → records
  `{turn_id, session_id, persona_instance_id}` in a drain file; the reaper
  that already harvests exited sessions posts the delivery. Sequencing: 3a
  ships alone first; 3b supersedes most of 3a's value when ruled in.

### Stage 4 — CLI niceties (HARNESS/CLI)

- **4a `thumb --square`:** pad the finished crop onto a square flat-dark
  backdrop (side = max(out_w, out_h), cell centred) so the 1:1 centre-cover
  hero card shows the whole frame. Implementation: one pad step after
  `pipeline.upscale_on_backdrop` in `row_thumb`
  (`agent/charsheet/draft.py:1194`); filename gains `-sq`
  (`walk-n-attempt-3-frame-1-x2-sq.png`); payload gains `"square": true`;
  BOTH budget booleans computed on the padded output (padding raises the
  pixel count — the refusal text already explains the ceiling). Default stays
  non-square (the compare viewer aligns panes; a pad changes aspect and
  §13.17's compare guidance assumes today's shapes). The SKILL's reply
  contract then recommends `--square` for hero-card thumbs and bare crops for
  compare pairs. Launcher card untouched.
- **4b machine next-step hints:** each verb payload gains a `"next"` object —
  `start` → `{verb: "turnaround", cmd: "hermes harness characters turnaround --draft <id> --json"}`;
  `approve-direction` (when it advances) → the `rows` cmd; a failed `rows` →
  the `reroll-row` + resume `--only` pair (the human line half-does this
  today, per the add-state plan); `compose` refusal already hands its verb.
  Additive keys only (the payloads are ruled supersets, SKILL.md:206–208), so
  no consumer breaks. Value: zero `--help` probing even for a skill-less
  caller, and the hints double as the autopilot spine for Stage 5.

### Stage 5 — one-shot autopilot verb (GATED, owner ruling R-3)

`hermes harness characters auto --draft <id> [--through compose] [--json]`:
one CLI process that drives `turnaround → approve-direction --all → rows →
compose`, emitting a per-stage receipt line as each stage lands (the same
payloads the verbs print today, newline-delimited). With Stage 3b it makes an
explicit one-shot ask cost **~4 API calls end to end** (start, fire `auto`,
delivery turn, reply). Tension to rule on: it auto-approves the turnaround,
which the skill calls "the last moment a reference can change" — so the verb
must be documented as *only* for an operator's explicit "drive it all the
way" ask, must stop (not override) on any handedness refusal, and must write
the same per-attempt history so `reopen` repair works. If R-3 is ruled no,
Stages 1–4 already deliver most of the win: the fire-imp shape re-run with
Stages 1–3a lands at a projected ~10–12 API calls (from 27) and ~600–700k
cumulative prompt tokens (from 1.556M).

### Stage 6 — small observability fix

Fix the element `summary`/`tool_input` cross-pairing on concurrent tool starts
(bucket f; evidence: fire-imp elements [0]–[6]). Locate where the turn
recorder zips started tools to element rows and key by tool-call id, not
arrival order. Independent of everything above; land whenever.

## Non-goals

- **No launcher card geometry change.** The 1:1 centre-cover hero card and
  the opened-viewer verdict surface are ruled (§13.17); Stage 4a solves the
  zoom confusion from the hermes side.
- **No FIELD-NOTES trimming or preloading.** It is the accumulated record,
  deliberately not in context; Stage 2 removes the head-of-file advertisement
  that invites disk reads, nothing more.
- **No pass/fail art scanner** (ruled against in the skill, measured false
  positives) and **no weakening of staged QA** — Stage 5 is gated precisely
  because of it.
- **No change to `sprite`** or to the never-pipe rule.
- **No blanket `TERMINAL_TIMEOUT` raise outside the mission-chat lane** —
  other lanes' turn budgets were not measured here.

## Report-back summary (for the reviewing session)

- Fire-imp one-shot: **27 API calls / 36 tool elements, 72% overhead,
  1.556M cumulative prompt tokens, 19.7 min** — and a reply that violated the
  render contract anyway, costing a 5-call remediation turn.
- The skill was **not in context** for the persona that ran it
  (`required_preload` is per-persona; supervisor lacks the assignment) — the
  operator premise "preloaded every turn" holds only for Chara A2.
- Wait-polling is the one bucket that hits even disciplined turns: 8 of 13
  API calls on the A2 idle turn; `process wait` clamps to 180 s
  (`tools/process_registry.py:1554`), and agents ask for 60.
- Top three fixes by value/risk: **Stage 1** (assign/delegate the skill —
  config only), **Stage 3a→3b** (600 s lane clamp now, process-exit delivery
  when ruled), **Stage 2** (front-loaded ≤20k-char skill with the reply
  contract in the first half).
