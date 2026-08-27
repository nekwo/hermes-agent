# Planned — persona-template skills (D10(ii))

**Status: DESIGN, NOT SHIPPED.** Nothing below exists in either repo. Surveyed
2026-08-27 against hermes `add7edd584` and launcher `fa39c676a` (both trees
moving — other sessions land commits continuously; re-verify anchors before
building).

**Owner surfaces:**
[06 — Office and board](../06-office-and-board.md) § Open rows, the
**D10(ii) — REOPENED 2026-08-27** entry (this plan is the design that closes
it), and launcher `EterniaLauncher/Launcher_Brain/20 — Active Initiatives/
mission-control-queue.md` § "Parked by ruling, not forgotten", second bullet
("Setting skills on the context panel does not reach the next agent placed
from that persona").

**The operator's expectation, verbatim from the ruling:** set skills on the
skills context panel, place a new agent from that persona, and it has those
skills. Broken by construction today: the panel writes the INSTANCE
(`skill_overrides`), a new placement inherits the TEMPLATE (`persona.skills`),
and no operator door writes the template.

**Every claim in §1–§2 was MEASURED during the survey** (file+line read, or a
live store read) unless it is explicitly marked ASSUMED. §7 is the honest
ledger of both.

---

## 1. The gap, re-measured — including two corrections to the filed row

### 1.1 What checks out exactly as filed

- The launcher's skills sheet submits capability
  `persona.instance.update_profile`
  (`EterniaLauncher/lib/features/mission_control/agent_chat/skills_context_controller.dart:585`),
  whose argv template renders `--skill` per entry and `--requested-by launcher`
  (`EterniaLauncher/lib/features/mission_control/data/harness_capability_registry.dart:1107-1135`,
  label "Save Agent Profile", allowedArgs `skills`/`clear_skills`). That lands
  in `PersonaInstanceStore.update_profile`
  (`agent_runtime/persona_assignments.py:1327-1329`) and writes the
  **instance's** `skill_overrides`.
- A new placement inherits the template LIVE, not as a copy:
  `runtime.agent.create` with `skills` absent leaves the new instance's
  `skill_overrides = None` (`agent_runtime/agent_create.py:1223`, ack via
  `_inherited_skills_ack()`, `:1024` — `{"assigned": [], "installed": [],
  "inherited": True}`), and every later resolution overlays through
  `apply_instance_model_overrides` (`agent_runtime/models.py:424-461`):
  `skill_overrides is None → list(persona.skills)`, read at each use.
- `AgentPersona.skills: list[str]` exists (`agent_runtime/models.py:263`).
- There is **no operator verb and no launcher capability** that writes
  `persona.skills`. Verified by grep over `hermes_cli/` + `agent_runtime/`
  (excluding `.claude/worktrees/` — a stale worktree copy shadows greps) and
  over the launcher capability registry.
- The absent-vs-empty rule is live and documented where the filed row said:
  `--skill` is `action="append", default=None` on both `persona instance
  update-profile` (`hermes_cli/harness.py:1085`) and `agent create`
  (`:1406`, with the load-bearing comment at `:1400-1406` — "absent means
  inherit the persona's skills while `[]` means override with none — two
  different agents"). The service-side translation carries a THE-BUG-THIS-
  REPLACES comment (`hermes_cli/harness_parts/persona_commands.py:5180-5203`):
  `list(... or [])` once turned every skill-less rename into a silent
  clear-all.
- `tests/agent_runtime/test_persona_skill_policy.py` (897 lines, freshest
  touches `f027b18bf5`/`b071bb23ab`) is read-side only — toolset validation,
  sample-persona skill membership, install-set derivation. No write verb, no
  collision with this plan.

### 1.2 Correction 1 — the template is NOT "populated from config only"

The filed row (and the dispatch that produced this plan) says `persona.skills`
"is populated from config only" via `personas.py` / `config.py`. That is wrong
for every persona in live use, and the difference decides §2:

- Persona records resolve through `ensure_persisted_personas`
  (`agent_runtime/config.py:578-587`): `{**catalog_from_config, **stored}` —
  **a store row wins WHOLESALE over the config record with the same id.**
  The store is `AgentStore` (`agent_runtime/store.py:152-171`), JSON rows under
  `<store_root>/agents/`, one file per persona, `save()` emitting a
  `persona.updated` event at the store chokepoint (registered in
  `agent_runtime/decision_contract_registry.py:291`).
- MEASURED on the live default root (`X:/Eternia/.hermes/agent-runtime/agents/`,
  resolution layer `default` per `agent_runtime/resolution.py`): **all five
  live personas are store rows** (`backend_dev`, `base`, `dev`,
  `neko_supervisor`, `qa`), every one carrying a populated `skills` list
  (dev has 19, qa has 3, …).
- Consequence: for these five, the config `skills:` / `skills_remove:` merge
  (`config.py:563-570`) is **dead** — shadowed by the store row. The realm-sync
  projection module already documents and ACCOUNTS this exact divergence
  (`agent_runtime/persona_config_sync.py`, module docstring +
  `config_shadowed_keys`); it is a standing property of the persona lane, not
  something this plan introduces.
- There are also two existing non-operator writers of persona rows:
  `promote_profile_to_persona` (`agent_runtime/personas.py:315`, copies
  `template.skills` into a new row) and the upstream
  `POST /api/profiles/{name}/promote` endpoint that calls it.

The gap statement itself still stands — no operator-addressable door writes
template skills — but the storage landscape is **store-first with a config
catalog underneath**, not config-only.

### 1.3 Correction 2 — the inheritance arm's address

The dispatch cites the `instance_skills = getattr(instance,
"skill_overrides", None)` fallback as living in `agent_create.py`. It lives in
`agent_runtime/models.py:441` (`apply_instance_model_overrides`).
`agent_create.py` holds the create-time absent-vs-empty split and the
inherit ack. Same mechanism, different file; anchors in this plan are the
measured ones.

### 1.4 The decisive precedent nobody named: `harness persona set-model`

A template-tier write verb **already exists for the model lane** and answers
most of this plan's shape questions by precedent
(`hermes_cli/harness.py:937-945`;
`hermes_cli/harness_parts/persona_commands.py:5411-5536`):

- help text: "Persist a persona's default provider/model (profile-default
  lane; future instances inherit it)" — the exact tier D10(ii) needs.
- persists via `AgentStore().save(target)`; ack carries
  `persistence: "agent_store"`.
- **refuses config-only personas**: `persona_not_persisted` — "agent defaults
  can only be set on store-persisted agents" (it does NOT auto-promote a
  catalog record into the store).
- resolves `profile:<name>` to the single store persona bound to that profile;
  `ambiguous_profile_persona` when several are.
- supersede clock: `--issued-at` guarded by
  `AgentPersona.model_override_issued_at` (`models.py:277`); stale writes
  answer `status: "superseded"` instead of clobbering.
- no coordinator-permission args (unlike the instance-tier verbs) — the
  template tier is an operator door.

And the launcher already carries the matching **template-tier capability**:
`persona.set_model`
(`EterniaLauncher/lib/features/mission_control/data/harness_capability_registry.dart:1259-1287`,
label "Set Profile Default Model", targetKind `persona`, exposure gateway,
`--issued-at` auto-stamped), with a write-target policy that splits instance
vs. persona-default by ROW identity
(`EterniaLauncher/lib/features/mission_control/agent_chat/mission_agent_model_switcher_view_model.dart:47-75`).

This plan is, deliberately, "do for `skills` what `set-model` already did for
`model/provider`" — plus the UX question the model lane never had to answer
(§4.2), because the model lane only offers the template door on idle
`persona:*` rows, while the operator's skills ruling starts from a live
agent's panel.

---

## 2. The central design question — where a template-skills write lands

### Option A — the store-backed persona row (`AgentStore.save`) — RECOMMENDED

Write `persona.skills` on the store row, exactly as `persona set-model`
writes `persona.model`.

For it (all measured):

- **It is the record the runtime actually reads.** Store wins in
  `ensure_persisted_personas`; the placement lane resolves personas through it
  (`agent_create.py` `_personas()` → `ensure_persisted_personas`), and the
  snapshot's roster row projects `skills: list(agent.skills)` from the same
  record (`agent_runtime/snapshot.py:2518`, `_agent_summary`). A store write
  is visible to the next placement and the next frame with zero read-side
  changes.
- **All five live personas already ARE store rows.** The write creates no new
  config/store shadowing on this machine — the shadowing already exists and is
  already accounted (`persona_config_sync.config_shadowed_keys`).
- **The event plumbing exists.** `AgentStore.save` emits `persona.updated` at
  the store chokepoint (`store.py:156-164`); the launcher's refresh path after
  a profile write is already event-driven.
- **Realm sync handles it correctly for free.** `skills` is in
  `PERSONA_DEF_ALLOWED_KEYS` (`persona_config_sync.py:101-123`) and published
  bodies are built FROM THE RESOLVED RECORD, so a template write propagates to
  realm members; the new supersede-clock field (§3.3) is excluded by the
  allowlist-by-default and lands in `dropped_keys` accounting, same as
  `model_override_issued_at` (deliberate: a write-ordering clock must not
  travel — `persona_config_sync.py:94-101`).

Against it (honest):

- A store-persisted persona is frozen against FUTURE config edits for every
  field, not just skills. True — but that die was cast when the five rows were
  seeded; this plan does not widen it for existing personas. For a
  **config-only persona** (catalog id with no row), a skills write COULD widen
  it by minting a row — which is why the recommendation is to **refuse**
  those with `persona_not_persisted`, byte-parallel with `set-model`, rather
  than silently pinning a whole persona to promote one field. (R1 below lets
  the operator choose auto-promote instead; the refusal names the fix either
  way.)

### Option B — mutate `config.yaml` (`agent_runtime.personas.<id>.skills`)

Rejected on measurement, not taste:

- For all five live personas the config value is **shadowed by the store row**
  — a config write would be a write the runtime never reads. Dead on arrival.
- The config merge semantics are additive-with-remove (`skills` extends the
  baseline, `skills_remove` subtracts — `config.py:563-570`). A panel
  replace-set cannot be expressed without also synthesizing `skills_remove`
  for every default the operator dropped; round-tripping an operator's
  hand-authored YAML (comments, anchors, `${roots.…}` machine tokens —
  `config.py:510-530`) to do it is exactly the class of hazard the config
  loader's own comments warn about.

### Option C — a new skills overlay (third tier)

A sidecar store between config and instance overrides. Rejected: the lane
already has two authorities with documented divergence accounting between
them (`persona_config_sync.py`, `persona_profile_binding.py:19` — "Silent
config/store divergence" is hazard #1 by name); a third authority multiplies
that surface and nothing needs it — the store row IS the overlay the runtime
already prefers.

**Recommendation: Option A**, with config-only ids refused (`set-model`
parity). This is R1.

---

## 3. The hermes verb — `harness persona set-skills`

Parser beside `persona set-model` (`hermes_cli/harness.py:937`), handler
beside `_cmd_persona_set_model`
(`hermes_cli/harness_parts/persona_commands.py:5411`).

### 3.1 Argv surface

```
harness persona set-skills <persona_id>
    [--skill <id>]...        # action="append", default=None — the load-bearing spelling
    [--clear-skills]
    [--issued-at <iso8601>]
    [--requested-by <who>]   # default "operator"
    [--json]
```

`<persona_id>` accepts a store persona id or `profile:<name>`, resolved
exactly as `set-model` resolves it (single bound row wins;
`ambiguous_profile_persona` / `persona_not_found` / `persona_not_persisted`
refusals reused byte-for-byte in shape).

No coordinator-permission args — parity with `persona set-model`, which has
none (the instance-tier verbs keep theirs).

### 3.2 Semantics — absent is NEVER a write

| argv | meaning at the template tier |
|---|---|
| `--skill a --skill b` | REPLACE the template set with `[a, b]` (full-set write, matching the launcher chokepoint's proven-baseline discipline) |
| `--clear-skills` | template set becomes `[]` — every future inheriting placement starts with no skills |
| neither | **typed refusal**, `error_code: "nothing_to_write"`, exit 2 |
| both | typed refusal (conflict), mirroring the `clear_model_override` conflict rule in `update_profile` (`persona_assignments.py:1270`) |

The template tier has no "inherit" state — it is the root of the cascade — so
absent cannot mean "leave alone by writing nothing" through the same code
path that writes: it must refuse. This is the same lesson the instance lane
paid for (`persona_commands.py:5180-5203`); the refusal keeps a
transport-mangled argv from ever clearing a template.

Normalization: run the submitted ids through the SAME discipline the instance
tier uses — token-safety, dedupe, cap 40 (`_safe_skill_overrides`,
`persona_assignments.py:3575-3585`). Import/rehome the one function rather
than spelling it twice; two spellings of one cap is how the create lane's
`MAX_SKILLS` comment (`agent_create.py:397-399`) says drift starts.

Resolvability: ids that no skills root can resolve are **reported in the ack
as `unresolved: [...]`, not refused** (R3). Rationale: the instance tier does
not refuse them either, placement-time strictness already lives in the create
verb's skills phase (`run_skills_phase`, `agent_create.py:854+`, which gates
only EXPLICIT create-time skills), and the readiness machinery is the honest
standing surface for a template naming a missing skill
(`missing_skills` in `_agent_summary`, `snapshot.py:2519`). A hard gate here
would also make a realm-synced persona un-editable on a machine that lacks
one of its skills.

### 3.3 Persistence, clock, event

- `store.get(persona_id)` → mutate `skills` → `AgentStore().save(target)`.
  Ack `persistence: "agent_store"`.
- New field `AgentPersona.skills_override_issued_at: datetime | None = None`
  (`models.py`, beside `model_override_issued_at:277`). Its own clock — a
  skills write must not supersede or be superseded by a model write.
  Persistence-safe both directions: `serde._coerce` ignores unknown keys on
  old readers (stated at `models.py:432-441` field-retirement note), and the
  default `None` loads on rows that predate it. Same stale-write rule as
  set-model: `issued <= applied` ⇒ `status: "superseded"`, no write, no event.
- No new event type: `AgentStore.save` already emits `persona.updated`
  (registered contract, `decision_contract_registry.py:291`). The ack's
  `next_expected` says the live-inheritance truth out loud: *instances with
  `skill_overrides = None` follow this set on their next resolution; instances
  with their own overrides keep them.*
- `persona_config_sync`: nothing to change. `skills` already publishes; the
  new clock field falls into `dropped_keys` accounting automatically
  (projection tests assert membership with `in`, not exact lists —
  `tests/agent_runtime/test_persona_config_projection.py:157-173` — so the new
  row cannot red them; still listed in S1's evidence to prove it was looked
  at).

### 3.4 Ack shape (JSON)

Mirror `set-model`'s envelope: `ok`, `status: applied|superseded`, `changed`,
`scope: "persona_template"`, `persona_id`, `applied_to_persona_id`, `skills`
(the stored set after the write), `unresolved` (warning list, §3.2),
`persistence: "agent_store"`, `next_expected` (the live-inheritance sentence
plus "refresh Harness snapshot").

---

## 4. The launcher — capability, and the UX decision the operator will care about

### 4.1 Capability `persona.set_skills`

Registry entry beside `persona.set_model`
(`harness_capability_registry.dart:1259`):

- `id: 'persona.set_skills'`, `targetKind: 'persona'`, label
  `'Set Persona Default Skills'`, group lifecycle,
  `executionSemantics: controlStateChange`, `exposure: gateway`.
- `requiredArgs: ['persona_id']`, `allowedArgs: ['skills', 'clear_skills']`.
- argv template: `harness persona set-skills` + positional
  `ArgRef.argOr('persona_id', fallback: ArgRef.target)` +
  `ArgvSegment.repeatedFlag('--skill', 'skills')` +
  `ArgvSegment.switchFlag('--clear-skills', 'clear_skills')` +
  `ArgvSegment.flag('--issued-at', ArgRef.issuedAt)` +
  `ArgvSegment.literals(['--requested-by', 'launcher'])`, `json: true`.
- Add the id to `kNowStampedIssuedAtCapabilityIds`
  (`test/features/mission_control/harness_argv_oracle_vectors.dart:58-63`) —
  its `--issued-at` is wall-clock stamped, same as both set-model lanes.

### 4.2 The UX decision — surface it, do not bury it (R2)

The operator must be able to tell "this agent" from "every future agent from
this persona" at the moment of the write. Three shapes considered:

- **U1 — scope control on the skills sheet (RECOMMENDED).** The existing
  sheet (`skills_context_controller.dart` + its editor surface) gains an
  explicit apply-to choice: **"This agent only"** (default — today's behavior,
  `persona.instance.update_profile`) vs **"<Persona> default (every future
  agent)"** (`persona.set_skills`). The template option carries honest copy:
  *"Also updates current <persona> agents that follow the persona default.
  Agents with their own skill overrides keep them."* — because template
  inheritance is LIVE (§1.1), and pretending the write only touches the
  future would be a lie the first idle agent disproves.
- **U2 — two save buttons.** Same writes, noisier surface, no default; makes
  the common case (instance edit) heavier. Not recommended.
- **U3 — mirror the model lane exactly:** live `personainst_*` rows write the
  instance, idle `persona:*` rows write the template, no in-sheet choice
  (`resolveModelWriteTarget`, view_model.dart:47-75). **Rejected as the sole
  mechanism**: the ruling's scenario starts from a LIVE agent's context panel
  ("set skills… place a new agent"), and the model lane's policy only offers
  the template door when the persona has no live instances. U3 alone cannot
  satisfy the ruling. (Adopting U1 keeps U3's row-identity behavior available
  later for idle rows at zero design cost.)

### 4.3 The template write goes through the existing chokepoint, extended

`MissionAgentSkillWrite`
(`EterniaLauncher/lib/features/mission_control/data/mission_agent_skill_write.dart`)
is THE gate every skills payload passes — proven-baseline replace, sourced
carry-forward, typed refusals. The template write must be a new constructor on
the SAME type (e.g. `MissionAgentSkillWrite.replaceTemplate(skills, {required
bool templateBaselineIsComplete})`), not a parallel hand-rolled args map:

- The template baseline is the snapshot roster row's template skills —
  `MissionPersonaRuntime.skills` with its `skillsSourced` flag
  (`EterniaLauncher/lib/features/mission_control/data/mission_control_snapshot.dart:6135-6146`,
  parsed from `_agent_summary`'s `skills` key). An unsourced baseline refuses
  with the same class of copy the instance lane uses: a whole-set write over a
  set this surface never saw is data loss, not an edit.
- The refusal/proof discipline is identical; only the capability id, target
  kind (`persona`), and args differ.

### 4.4 A stale docstring this work must fix in passing

`mission_agent_skill_write.dart`'s header states hermes resolves omitted
`--skill` to `list(args.skills or [])` so "omitting the field … is a silent
clear-everything", parenthesized "(Recorded as a hermes-side handoff; until it
lands …)". **The handoff landed** (hermes `9f697636e9`, the S4 placement
commit — `--skill` is `default=None` end-to-end and the service comment at
`persona_commands.py:5185` calls the old behavior THE BUG THIS REPLACES). The
launcher's full-set-proof discipline remains correct as defense-in-depth for
REPLACE writes, but the stated reason is now false. S4 updates the paragraph
to cite the landed fix instead of predicting it. (Same stale claim summarized
at `mission_agent_profile_editor.dart:29` and `:344` — sweep all three.)

---

## 5. Cross-repo contract — the argparse dump

The committed fixture
`EterniaLauncher/test/features/mission_control/fixtures/hermes_cli_contract.json`
(schema `hermes_cli_contract/v3`, 171 command paths at survey) pins hermes's
real argparse tree. After S1 lands in hermes, S3 regenerates it:

```
dart run tool/hermes_cli_contract/dump_hermes_cli_contract.dart --hermes-root=X:/Eternia/hermes-agent --python=C:/Python312/python.exe
```

**The diff must be PURELY ADDITIVE, verified by reading it, not by the suite
going green.** Expected shape, exactly two additions:

1. one new line `"set-skills",` inside `"harness persona"`.`subcommands` —
   it sorts between `"set-model"` and `"show"`, i.e. mid-array, so no
   trailing-comma churn on neighboring lines;
2. one new command object `"harness persona set-skills"` with options
   `--skill` (nargs 1, repeatable), `--clear-skills` (nargs 0), `--issued-at`,
   `--requested-by`, `--json`, `--help/-h`, and positional `persona_id`.

Any removed or modified line is a STOP — the dump once caught a dropped line
and that is the entire reason this step is a named verification, not a
formality. Precedent for the commit shape: launcher `bf107a882` ("the argparse
pin learns two verbs and loses none").

Launcher test-suite obligations that come WITH the new capability (all
measured conventions):

- `harness_argv_oracle_vectors.dart`: a MAXIMAL vector
  (`skills: [a, b]`) and a MINIMAL/variant vector (`clear_skills: true`) —
  every registry id gets vectors; plus the `kNowStampedIssuedAtCapabilityIds`
  entry (§4.1).
- `harness_capability_argv_test.dart`: the targetKind map gains
  `'persona.set_skills': 'persona'` (`:1060` region).
- `harness_argv_template_test.dart`: the byte-equal oracle fixture
  (`fixtures/harness_argv_oracle.json`, 71 entries) is a FROZEN capture and is
  **never regenerated** — a new id is named in the
  `registered.difference(covered)` expected set beside the six `characters.*`
  ids (`:136-160`), per that test's own instructions. Do not add fixture
  entries; do not bump the 37/71 counts except as that file's comments direct
  for a NEW id.

---

## 6. Slices

Every slice ends with its named tests green and its killing-mutation evidence
recorded. Hermes claims go in the executable registry
`tests/mutation_claims.json` (shape: `id/path/symbol/operator/find/replace/
test`; runner `python scripts/changed_line_mutation_check.py --base <sha>
[--list]`, which executes exactly the claims whose `path` intersects changed
production lines — so every claim below targets lines its own slice writes).
Launcher claims are docstring-only by that repo's convention (file-header
"killing mutations" blocks, e.g.
`test/features/mission_control/mission_stream_lane_gate_test.dart:17-24`) —
for each, the builder APPLIES the mutation, runs the named test, and QUOTES
the actual red in the docstring before reverting. A hypothetical red is not
evidence.

### S1 — hermes: the verb and the field

Build: parser (`harness.py`, beside `:937`), handler
(`persona_commands.py`, beside `:5411`), `AgentPersona.skills_override_issued_at`
(`models.py`), refusals + ack per §3. New test module
`tests/hermes_cli/test_persona_set_skills.py` (precedents:
`tests/agent_runtime/test_persona_set_model.py`,
`tests/hermes_cli/test_persona_instance_update_profile_skills.py`).

Done when:

- Against an isolated root (`HERMES_AGENT_RUNTIME_ROOT=<tmp>/agent-runtime-probe-<x>`
  + `HERMES_REQUIRE_ISOLATED_ROOT=1` — the probe gate requires the
  `agent-runtime-probe-` basename, `agent_runtime/resolution.py:96-120`):
  seed a store persona, run the verb live, and show by RE-READING THE ROW FILE
  that `skills` changed and `skills_override_issued_at` advanced. Paste the
  argv + ack into the field notes.
- Refusal matrix from §3.2 covered by tests: no-flags refusal, both-flags
  refusal, `persona_not_persisted` on a config-only id, `superseded` on a
  stale `--issued-at`, cap-40 truncation, `unresolved` warning on a fake id.
- Mutation claims registered and listed by
  `python scripts/changed_line_mutation_check.py --base <pre-slice sha> --list`:
  - `pts-s1-absent-becomes-clear` — replace the no-flags refusal with
    `requested = []`; killed by
    `test_omitted_skill_flag_is_a_refusal_not_a_clear`.
  - `pts-s1-store-write-dropped` — delete the `store.save(target)` line;
    killed by the re-read-from-disk assertion.
  - `pts-s1-stale-write-applies` — invert `issued <= applied`; killed by the
    supersede test.

Evidence: test run output, the `--list` output naming all three claims, the
live-probe ack.

### S2 — hermes: the inheritance proof, end to end

Build: one integration test (new file or a class in the S1 module) proving the
operator's sentence: `persona set-skills` → `runtime.agent.create` (or the
service function under it) with `skills` ABSENT → the new instance answers
`inherited: True` AND `apply_instance_model_overrides(persona, instance).skills`
equals the just-written template set. A second case: an instance CREATED
BEFORE the template write, with `skill_overrides = None`, resolves the new
set (live inheritance); a third: an instance with its own overrides is
untouched.

No production lines change in this slice, so no mutation claims can fire —
the changed-line runner is scoped to production diffs by design. The
integration test IS the evidence. If S2's writing reveals a production gap
(e.g. the create lane caching personas across the write), that fix lands here
WITH its own registered claim.

Done when: the three cases run green against the isolated root; the field
notes record whether the snapshot's roster row (`_agent_summary.skills`)
reflected the write on the next build (expected: yes, read live — pin it in
the test if cheap).

### S3 — launcher: capability + contract

Build: the registry spec (§4.1), oracle vectors + targetKind map +
frozen-oracle difference-set entry (§5), fixture regeneration with the
additive-diff verification (§5).

Done when:

- `flutter test test/features/mission_control/harness_capability_argv_test.dart
  test/features/mission_control/harness_argv_template_test.dart` green.
- The fixture diff is pasted (or precisely described hunk-by-hunk) in the
  field notes with the words "purely additive: verified".
- Killing-mutation docstrings on the new/changed test blocks, reds quoted.
  Minimum set: (a) mutate the template's `repeatedFlag('--skill', …)` to a
  single joined flag — the conformance gate must red naming the vector;
  (b) drop the `--issued-at` segment — the oracle/template parity or
  conformance red is quoted.
- KNOWN TREE HAZARD, restated from the dispatch: the launcher primary
  currently carries two RED `test/architecture` gates from another session's
  account-devices work (reported by the dispatching session, not re-measured
  here). They are not yours; do not fix them, do not let their red gaslight
  your run — run the FOCUSED files above, and never the push lane against a
  tree you are editing.

### S4 — launcher: the panel writes the template

Build: U1 scope control on the skills sheet;
`MissionAgentSkillWrite.replaceTemplate` (§4.3) with the sourced template
baseline from `MissionPersonaRuntime.skills/skillsSourced`; the honest
live-inheritance copy; the stale-docstring sweep (§4.4:
`mission_agent_skill_write.dart` header,
`mission_agent_profile_editor.dart:29`/`:344`). The three OTHER doors onto
`persona.instance.update_profile` (rename, goal assignment, character editor
carry-forward) stay instance-tier and untouched.

Done when:

- Controller/widget tests: default scope is instance (the existing write is
  byte-identical to before — pin it); template scope submits
  `persona.set_skills` with the full proven set; unsourced template baseline
  refuses with the named copy; the honest-copy string renders under the
  template scope.
- Killing-mutation docstrings with quoted reds. Minimum set: (a) flip the
  default scope to template — the byte-identical-instance-write pin reds;
  (b) drop the `templateBaselineIsComplete` refusal — the unsourced-baseline
  test reds; (c) point the template branch at
  `persona.instance.update_profile` — the capability-id assertion reds.

### S5 — the docs fold, both repos

- Hermes [06 — Office and board](../06-office-and-board.md): a short
  subsection under "The write verbs" (or beside the skills-phase prose the
  placement verb owns) stating the two-tier skills write — instance
  `update-profile` / template `set-skills`, live inheritance, store-first
  resolution — and the **Open row D10(ii) closes**: the REOPENED entry gains
  "SHIPPED <date>" with both repos' receipt shas, following the D10(iii) row's
  exact pattern (`06-office-and-board.md:768-771`).
- This file: the index rule (`00-index.md:31-37`) says a shipped plan's
  content moves into the owning domain doc and the planned file is DELETED in
  the same commit; the freshest live precedent
  ([authorization-chokepoint.md](authorization-chokepoint.md), landed
  2026-08-27) instead kept its file with a receipt table. Follow the index
  rule unless the operator prefers the receipt-table survival — either way
  the shipped truth's canonical home is canon 06, not here.
- Launcher `EterniaLauncher/docs/mission_control/06-board-and-aux-surfaces.md`
  § "Skills surfaces" (`:211`): the Skills Context surface entry learns the
  scope control and the second capability.
- Launcher `EterniaLauncher/Launcher_Brain/20 — Active Initiatives/
  mission-control-queue.md` § "Parked by ruling, not forgotten": the second
  bullet (`:143-152`) closes — moved/marked per that file's done convention,
  citing both shas and this plan.
- Cross-repo path discipline: repo-qualify every cross-repo mention
  (`hermes-agent/docs/…`, `EterniaLauncher/docs/…`) — both repos run dead-link
  gates that red on unqualified moved/foreign paths.

Done when: hermes pre-push link gate green; the launcher brain row and canon
row cite real shas; field notes in both repos finalized.

---

## 7. Measured vs assumed

MEASURED during this survey (file+line, live store, or fixture read): every
anchor cited in §1–§5, including: the five live store rows and their skills;
`ensure_persisted_personas` store-wins merge; `set-model`'s full handler
(refusals, clock, store write); the absent-vs-empty comments at
`harness.py:1400-1406` and `persona_commands.py:5180-5203`; the launcher
registry specs for `persona.instance.update_profile` and `persona.set_model`;
`resolveModelWriteTarget`; the skills sheet's write site and the
`MissionAgentSkillWrite` chokepoint (including its stale hermes claim); the
contract fixture (171 commands, v3, `harness persona`.subcommands list); the
dump tool's `--hermes-root`/`--python` flags; the frozen-oracle discipline
(never regenerated, difference-set naming); mutation registry shape + runner
args (`--base` required, `--list`) + CI wiring (`.github/workflows/tests.yml:39`);
`persona.updated` in the decision contract registry; projection `dropped_keys`
accounting and its `in`-not-exact tests.

ASSUMED (stated, not verified):

- The two RED launcher `test/architecture` gates (account-devices seam) —
  taken from the dispatching session's report; not re-run here.
- That no OTHER launcher door needs the template write besides the skills
  sheet (the other three `MissionAgentSkillWrite` doors are carry-forward
  writes with no skills-editing semantics — read, but their product surfaces
  not exhaustively audited).
- That serve caches no persona record across a store write in the chat lane
  (the placement lane re-resolves; S2's second case exists to catch this if
  wrong).
- `C:/Python312/python.exe` still the right interpreter for the dump (taken
  from the dispatch; the tool has fallbacks and says so when wrong).

Corrections to the dispatch framing are §1.2 and §1.3; the stale launcher
docstring is §4.4. No claim of the dispatch was found wrong in a way that
changes the gap's existence — only the storage landscape (which changes the
ANSWER, §2).

---

## 8. Rulings — answer in one message

Defaults follow the adopted-at-recommendation convention (launcher
`ffa7ea09f` pattern): each is ADOPTED at its recommendation unless overridden
before its slice lands.

- **R1 — storage tier.** Template skills are written on the store-backed
  persona row via `AgentStore.save`, `set-model` parity; config-only persona
  ids are REFUSED `persona_not_persisted` (no silent full-persona promotion).
  Alternative: (b) auto-promote the catalog record to a row on first write —
  freezes every other config field at write-time values.
  **Recommended: refuse.**
- **R2 — UX scope surface.** U1: in-sheet apply-to choice, default "This
  agent only", honest live-inheritance copy on the template option.
  Alternatives: U2 two buttons; U3 row-identity only (cannot satisfy the
  ruling's scenario). **Recommended: U1.**
- **R3 — unresolvable skill ids on a template write.** Warn in the ack
  (`unresolved: [...]`) and let readiness carry the standing truth; do not
  refuse. Alternative: hard-refuse like the create verb's skills phase —
  stricter, but breaks editing synced personas on machines missing a skill.
  **Recommended: warn.**

---

## 9. Adjacent gaps deliberately not built here

Named so their absence is a decision, not an oversight:

- **The instance re-inherit door.** Once an instance has `skill_overrides`
  set, nothing can return it to `None` — `--clear-skills` writes `[]`
  ("explicitly none"), not "follow the template again"
  (`persona_assignments.py:1327`). After the template door exists, "fix the
  template, let existing agents follow" is impossible for any agent the panel
  ever touched. Real, operator-visible, and OUT OF SCOPE: it changes an
  existing verb's semantics surface (`update-profile` would need an
  `--inherit-skills` arm and the capability an arg) and deserves its own
  ruling rather than riding this one.
- **Skill-id validation parity across the three write doors** (create:
  hard-gated skills phase; instance update: token-safety only; template:
  token-safety + warn per R3). A follow-up could unify; not needed to close
  D10(ii).
- **No Stage C / visual proof owed.** The deliverable is a write verb, a
  capability, and panel wiring whose observable truth is argv, store rows,
  and the create ack — all pinned by tests above. If S4's scope control turns
  into a visible redesign of the sheet (it should not — one control), the
  builder may add a Stage C capture at their judgment; nothing here requires
  one.
