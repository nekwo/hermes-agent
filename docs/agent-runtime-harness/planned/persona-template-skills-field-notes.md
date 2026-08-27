# Field notes — persona-template skills survey (hermes half)

Running record of the 2026-08-27 planning survey behind
[persona-template-skills.md](persona-template-skills.md). Survey only — no
production line was changed in this repo by this session. Hermes HEAD moved
under the survey from `5f33b5add0` to `add7edd584` (gateway S1a,
`serve_gateway_auth.py` — no overlap with this lane).

## What was measured, in the order it changed the plan

- `agent_runtime/config.py:578-587` — `ensure_persisted_personas` returns
  `{**catalog, **stored}`: a store row shadows the config record WHOLESALE.
  This single line reframed the whole storage question.
- Live store read (`X:/Eternia/.hermes/agent-runtime/agents/*.json`, default
  resolution layer confirmed via `agent_runtime.resolution.resolve_runtime()`):
  five rows — `backend_dev`, `base`, `dev`, `neko_supervisor`, `qa` — every
  one with a populated `skills` list. So the dispatch's "populated from config
  only" is false for every persona in live use; the runtime reads the store.
- `hermes_cli/harness.py:937-945` + `harness_parts/persona_commands.py:5411-5536`
  — `harness persona set-model` is a full template-tier write precedent:
  `AgentStore().save`, `persona_not_persisted` refusal for config-only ids,
  `profile:<name>` resolution, `--issued-at` supersede clock
  (`model_override_issued_at`), `persistence: "agent_store"` ack, no
  coordinator args. The plan's verb is this handler with `skills` instead of
  `model/provider`.
- Absent-vs-empty: `--skill` is `action="append", default=None` on
  `update-profile` (`harness.py:1085`) and `agent create` (`:1406`, comment at
  `:1400-1406`); the service-side THE-BUG-THIS-REPLACES comment is at
  `persona_commands.py:5180-5203`. The rule is real and load-bearing, as
  claimed.
- Inheritance arm actually lives in `agent_runtime/models.py:424-461`
  (`apply_instance_model_overrides`), not `agent_create.py` as dispatched;
  `agent_create.py:1024` holds `_inherited_skills_ack` and `:1223` the
  create-time split.
- `agent_runtime/persona_config_sync.py` — `skills` is in
  `PERSONA_DEF_ALLOWED_KEYS`; `model_override_issued_at` is deliberately
  excluded (write-ordering clocks must not travel); `_record_to_def` accounts
  non-allowlisted record fields in `dropped_keys`, and the projection tests
  assert membership (`in`), not exact lists — so the planned
  `skills_override_issued_at` field cannot red them.
- `tests/mutation_claims.json` shape (`id/path/symbol/operator/find/replace/
  test`) and `scripts/changed_line_mutation_check.py` args (`--base` required,
  `--list`); CI runs it in `.github/workflows/tests.yml:39`. Claims fire only
  on changed production lines — which is why the plan's S2 (tests-only) carries
  no claims.
- `tests/agent_runtime/test_persona_skill_policy.py` (897 lines) — read-side
  only, no write verb; no collision.
- `decision_contract_registry.py:291` — `persona.updated` already registered;
  the store save emits it; no new event type needed.

## Surprises

- The strongest fact in the whole survey was not in the dispatch at all:
  `persona set-model` + launcher `persona.set_model` already built the exact
  two-tier write pattern this gap needs, down to the launcher's
  instance-vs-persona-default write-target policy. The plan is mostly
  "do it again for skills".
- `--clear-skills` at the instance tier writes `[]`, not `None` — there is NO
  re-inherit door. Filed in the plan as a named adjacent gap, not built.
- `.claude/worktrees/h1-recorded-home/` shadows naive `grep -r` over the repo
  with a stale copy of `agent_runtime/`. Cost a few minutes; use `git grep`.
- `agent_runtime/capabilities.py` (named as drifted-but-extant by the
  launcher's contract README) no longer exists at all. Nothing in this lane
  needs it; the README sentence is stale.

## Not verified

- No test was run and no serve was started in this repo during the survey —
  every claim is a read or a store-file read. S1's done-when includes the live
  isolated-root probe precisely because of this bound.
- Whether any serve-resident cache holds a persona record across an
  `AgentStore.save` (S2's second test case exists to answer it).

---

# Build pass — S1 (hermes: the verb and the field)

Running record of the 2026-08-27 build session that implemented S1. Hermes HEAD
moved under this pass three times (`fe5ed08a0f` → `3f0c29592d` → `28ec9e3180`,
all gateway S1c–S1e work in `serve_*` / `gateway_commands.py`); every suite
below was re-run at `28ec9e3180` immediately before the commit.

## What shipped

- `AgentPersona.skills_override_issued_at: datetime | None = None`
  (`agent_runtime/models.py`, beside `model_override_issued_at`).
- `harness persona set-skills <persona_id> [--skill]... [--clear-skills]
  [--issued-at] [--requested-by] [--json]` — parser beside `set-model`
  (`hermes_cli/harness.py`), handler beside `_cmd_persona_set_model`
  (`hermes_cli/harness_parts/persona_commands.py`).
- `tests/hermes_cli/test_persona_set_skills.py` — 14 tests, every one driving
  the REAL argparse tree through `args.func` and probing the row FILE on disk
  rather than the command's own reply.
- Three registered killing mutations in `tests/mutation_claims.json`, all three
  KILLED (below).

## Two shared helpers extracted rather than re-spelled

The plan says the refusals are "reused byte-for-byte in shape"; the build reused
the CODE, which is stronger and is what "do not invent a second pattern for the
same shape" actually asks for:

- `_template_write_store_target(store, persona_id, *, what=...)` — the
  `profile:<name>` resolution and the `persona_not_persisted` /
  `ambiguous_profile_persona` refusals, now shared by BOTH template-tier verbs.
  `_cmd_persona_set_model` was rewritten onto it; its own tests (all of
  `tests/agent_runtime/test_persona_set_model.py`) stayed green, because they
  assert `error_code`, not the sentence.
- `_parse_issued_at_arg(raw)` — the `Z`-suffix workaround `--issued-at` needs,
  previously inline in `_validated_set_model_request`. One spelling for all
  three supersede-clock verbs.

`_safe_skill_overrides` is IMPORTED from `agent_runtime.persona_assignments`
into the persona-commands part (the plan's "import/rehome, don't re-spell").
Checked against `tests/hermes_cli/test_harness_parts_namespace.py` first —
harness.py binds no name of that spelling, so the import shadows nothing.

## One refusal the plan does not name, added deliberately

`--skill "   "` is a PRESENT flag whose only value token safety drops. The
plan's §3.2 table covers absent (refuse) and `--clear-skills` (write `[]`), but
not this. Writing the survivors would be an empty set — i.e. the exact clear the
absent-flag branch just refused to infer — so it gets the same answer:
`invalid_value`, exit 2, row untouched
(`test_a_skill_flag_whose_every_value_is_rejected_is_not_a_clear`). This serves
§3.2's own stated intent ("the refusal keeps a transport-mangled argv from ever
clearing a template") rather than diverging from it, but it IS an addition to
the specified surface and is flagged here as one.

## Killing mutations — registered, run, KILLED

Runner: `python scripts/changed_line_mutation_check.py --base HEAD [--list]`
(3 candidates selected, cap 12; every other registered claim UNSELECTED).

| id | operator | observed red |
|---|---|---|
| `pts-s1-absent-becomes-clear` | the `nothing_to_write` raise becomes `clear = True` | `assert 0 == 2` — and, with that assertion removed, `assert [] == ['seeded-skill']` on the row re-read from disk |
| `pts-s1-store-write-dropped` | `store.save(target)` deleted | `assert ['seeded-skill'] == ['alpha', 'beta']` |
| `pts-s1-stale-write-applies` | `issued <= applied_at` inverted | `assert 'applied' == 'superseded'` |

Every red quoted in a docstring was produced by APPLYING the mutation and
reading pytest's output, not predicted.

## The live probe — isolated root, real CLI, row re-read off disk

`HERMES_AGENT_RUNTIME_ROOT=<scratch>/agent-runtime-probe-pts`,
`HERMES_REQUIRE_ISOLATED_ROOT=1`, `HERMES_HOME=<scratch>/hermes-home`. The
operator's real root at `X:/Eternia/.hermes` was never opened.

Seeded: persona `qa` with `skills: ["harness-qa-verdict"]` and
`skills_override_issued_at: null`; workspace + office surface `ws_probe`; two
real skill packages (`probe-alpha`, `probe-beta`) in the probe skills root.

```
$ harness persona set-skills qa --skill probe-alpha --skill not-a-real-skill --requested-by operator --json
{
  "applied": true,
  "applied_to_persona_id": "qa",
  "changed": true,
  "cleared": false,
  "next_expected": "refresh Harness snapshot; instances whose skill_overrides is null follow this set live on their next resolution, and instances carrying their own overrides keep them",
  "ok": true,
  "persistence": "agent_store",
  "persona_id": "qa",
  "scope": "persona_template",
  "skills": ["probe-alpha", "not-a-real-skill"],
  "status": "applied",
  "unresolved": ["not-a-real-skill"]
}

--- RE-READ THE ROW FILE ON DISK ---
skills= ['probe-alpha', 'not-a-real-skill']
skills_override_issued_at= 2026-08-27T20:20:42.965390Z
```

Also exercised live on the same root: `--clear-skills` (writes `[]`,
`cleared: true`); a stale `--issued-at 2020-01-01T00:00:00Z`
(`status=superseded changed=False`, set unchanged); the no-flags refusal
(`nothing_to_write`, exit 2); the both-flags refusal (`conflicting_args`,
exit 2); an unknown id (`persona_not_found`, exit 2).

R3 confirmed by construction on that root: `probe-alpha` resolved,
`not-a-real-skill` did not, the ack warned about exactly one of them, and the
write landed anyway.

## Surprises

- The `persona_not_persisted` refusal is HARDER to reach from a CLI than the
  plan implies. On a fresh runtime root the config catalog is empty, so a
  catalog-only id answers `persona_not_found` (from `_persona_by_id`) long
  before the store lookup runs. Reaching `persona_not_persisted` needs a config
  that DECLARES the persona and a store that lacks it — the test builds exactly
  that (`AgentRuntimeConfig(personas={"catalog_only": {...}})` patched over
  `harness.load_agent_runtime_config`) and additionally asserts the refusal
  minted no row. On the live default root all five personas are store rows, so
  no operator will meet this refusal there; it guards the catalog case only.
- `Path.write_text` on Windows silently rewrote every line of
  `tests/mutation_claims.json` to CRLF, turning a 51-line append into a
  619-deletion diff. Reverted and re-appended textually with
  `open(..., newline="\n")`. Anything editing a tracked file from Python in
  this repo needs the explicit newline.

## Not mine, measured, left alone

`tests/hermes_cli/test_harness_cli.py::test_every_stage42_global_flag_is_honored`
is RED and it is not this lane's. It names 14 unhonored registrations, every one
of them `_cmd_gateway_pair` / `_cmd_gateway_devices_list` /
`_cmd_gateway_devices_revoke`: the gateway session's S1e commit `28ec9e3180` put
those verbs' readers in `hermes_cli/harness_parts/gateway_commands.py`, which is
not in that test's `_stage42_lane_sources()` tuple (and is still untracked on
the primary). Proven pre-existing by running that one test in the clean HEAD
worktree `X:/Eternia/.gwc` (`git status` empty, same sha `28ec9e3180`) — same
failure, none of my working tree involved. Not fixed here.

## Suites run (at `28ec9e3180`, `-q -p no:cacheprovider`)

| suite group | result |
|---|---|
| `test_persona_set_skills` + `test_persona_instance_update_profile_skills` + `test_persona_set_model` | 61 passed |
| `test_persona_skill_policy` + `test_persona_config_projection` + `test_models_serde` + `test_persona_assignments` | 203 passed |
| `test_agent_create_verb` + `test_agent_retire_verb` + `test_agent_create_{service,subphases,reservations}` + `test_agent_retire_service` | 105 passed |
| `test_office_{store,layout_policy,class_key_one_fence,sync}` + `test_harness_parts_namespace` + `test_mission_chat_turn_context` | 158 passed |
| `test_harness_cli` + `test_commands` + `test_completion` + `test_s22_vestigial_parser_flags` + `test_persona_profile_binding` + `test_realm_sync_skill_inbox` | 186 passed, 1 skipped, 1 xfailed, 1 failed (the gateway red above) |

## Where the plan was right and it mattered

Corrections §1.2 and §1.3 both held. The store row IS the write target the
runtime reads (`ensure_persisted_personas` store-wins is pinned by
`test_the_placement_lane_and_the_roster_row_both_see_the_write`), and the
inheritance arm is in `models.py`, not `agent_create.py`. `set-model` answered
every shape question the verb had — the handler is its twin, field for field.
Nothing in §1–§5 was found wrong.
