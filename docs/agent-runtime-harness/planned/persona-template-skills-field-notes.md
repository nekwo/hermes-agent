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
