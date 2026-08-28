---
name: harness-dev-delivery
description: Shared Backend Dev and Launcher Dev working contract — repo discipline, focused self-tests, commit hygiene, backend Postgres proof, and honest reporting on a chat turn.
metadata:
  hermes:
    surfaces: [mission_chat]
    modes: [standard]
    load_policy: recommended
---

# Harness Dev Delivery

> **Scope note (2026-07-30).** This skill was written for a worker/stage lane that no
> longer exists: Mission HUD (`agent_hud`, `status_lane`, `recommended_action`,
> `evidence_stack`, `repo_bundles`, `current_assignment`), stage IDs and `proof_gate`,
> `handoff_packet` / `delivery` packets, `checklist_updates`, Harness-managed
> `request_test_run` and the final gate, `validation_repair`, and `decision_contract_mode`.
> All of it was removed with the mission lane — see
> `docs/agent-runtime-harness/16-mission-lane-removal.md`. There is no HUD to read, no
> action menu to pick from, no packet to fill in, and no Harness gate that runs after you.
> What survives is the engineering discipline below: work in one repo, patch narrowly, run
> the focused command yourself, commit exactly your slice, and report the real result in
> the chat.

Use this skill for non-trivial Backend Dev or Launcher Dev work.

## Delivery Rules

- Start from what the operator actually asked for in this chat. Stay inside the resolved
  repo and the stated scope.
- Do the work with native tools: inspect narrowly, patch files, run focused tests, then
  report. Prefer one targeted search/read pass, one patch, and one focused command over a
  broad repo audit.
- Run the narrowest self-test that proves the claim, while context is hot. Never run an
  unbounded full suite (`pytest` over a whole repo, bare `flutter test`, `manage.py test
  --noinput`) unless the operator asked for it.
- Report the command, its exit status, and a short failure excerpt. Preserve raw logs in
  artifacts; do not paste noisy logs into the chat.
- Never claim you ran something you did not run, and never claim a result you did not see.
  If a capability or path is unavailable, say so plainly.
- If a needed input is genuinely missing — an API shape, a contract, a decision only the
  operator or another agent can make — ask for that one thing by messaging the right agent
  (`agent_chat_send`) or asking the operator. Do not guess an API shape.
- For investigation/audit work with nothing to patch, read the narrow paths you need and
  report findings plus known gaps. There is nothing to "hand off"; the answer is the reply.
- After an environment change, choose exactly one bounded retry command or one narrower
  equivalent.
- Use skill search first; load one relevant skill by default and two at most.
- Never copy redacted path labels such as `<path:EterniaLauncher>` or
  `<path:eternia-backend>` into commands. Use repo-relative commands.

## Backend Commands

- For Backend product edits, read the backend `docs/testing/README.md` and use the
  `scripts/test.sh` default Postgres/Docker tier. `scripts/test.sh --sqlite` and mocked-only
  tests are not release evidence.
- For Backend Docker/PostgreSQL runs, use
  `python scripts/backend_postgres_proof.py --backend-root "X:\Unreal Engine\Engine\EterniaBackend\eternia-backend"`
  from the Hermes repo, adding focused test targets after `--` only when the work is
  explicitly focused. The helper tees the real `scripts/test.sh` default tier to backend
  `qa_artifacts` and refuses SQLite/mocked-only markers.
- For Backend smoke with no product edits, default to
  `.EterniaBackendVirtualEnv/Scripts/python.exe manage.py check --deploy` or
  `.EterniaBackendVirtualEnv/Scripts/python.exe manage.py check`. Use
  `.EterniaBackendVirtualEnv/Scripts/python.exe manage.py test <specific.app.or.test.path> --noinput`
  only against an identified target.
- If Backend evidence uses inline Python that imports Django directly, set
  `DJANGO_SETTINGS_MODULE=backend.settings` before `django.setup()` and keep `DJANGO_ENV=dev`
  or the stated environment as the selector. Do not set `DJANGO_SETTINGS_MODULE` to a
  submodule such as `backend.settings.dev`; the backend dispatcher owns that choice.
- For Backend health/client checks, prefer `python manage.py shell -c "..."` or an inline
  Python command with both `DJANGO_SETTINGS_MODULE=backend.settings` and the intended
  `DJANGO_ENV`. A command with `DJANGO_ENV` alone is not valid Django evidence.

## Launcher Commands

- For Launcher smoke with no product edits, default to a focused
  `flutter analyze <changed-or-relevant-path>` or one named widget/unit test file.
- For Launcher Windows debug rebuilds, Marionette freshness checks, or Stage C MCP
  screenshots/videos, first check for already-running `eternia_launcher.exe` and stale
  `stagec_qa_mcp_server.exe` processes and close them before capturing. This is a Windows
  build / Stage C visual preflight, not a blanket excuse for `flutter test` failures.
- See the `harness-qa-verdict` skill (which absorbed `launcher-analyze-proof` 2026-08-28)
  for choosing the narrowest analyze/test command and for the Stage C screenshot call shape.

## Deployment Is Not Local Delivery

For EterniaBackend and EterniaLauncher product edits, a passing local test is not a
production deployment. The promotion chain is, in order: local deterministic product tests
→ remote test staging k8s pod validation → production pod rollout. EterniaBackend
additionally needs the local Docker/PostgreSQL run before staging. Do not describe a local
green run as "deployed", and do not invent staging or prod evidence you did not collect.

If pushing to the protected branch triggers deployment, sync from remote before pushing:
fetch/pull, rebase when needed, resolve conflicts, rerun the relevant local test if HEAD
changed, then push. A raw `git push` with no sync step is not an acceptable rollout account.

## Commit Discipline

For product edits:

1. Commit exactly your changed paths on the current branch:
   `git add <changed_paths> && git commit -m "<bounded slice summary>"`. Never `git add -A`;
   never stage pre-existing dirty-baseline files that are not part of your slice. Do not push
   unless the operator asked for it.
2. Report the commit reference when it is relevant: `"<repo>@<branch>:<short_sha>"`
   (`git rev-parse --short HEAD`, `git rev-parse --abbrev-ref HEAD`).
3. Run the repo's deploy-check yourself when it is cheap and relevant — EterniaBackend
   `manage.py check`; EterniaLauncher focused `flutter analyze <changed paths>`; hermes-agent
   focused `pytest` for the changed modules.
4. Bound the verification tail. After a small change, one post-commit `git show --stat HEAD`
   (or `git show HEAD` when the diff itself is the proof) is the commit receipt — report it and
   stop. Do not re-run `git diff --check`, `git status`, link checks, or a second focused test
   over state a command in this same reply already proved clean. Each redundant re-check is a
   full model iteration (~12-15s) that proves nothing new.

No-edit investigation work is exempt: do not invent commits for work that changed nothing.

## Request Context

For a large file, read a bounded window instead of repeating whole-file reads — ask for
`relative/path.py#L120-L220` with a reason, rather than the whole file. If the context you
need lives with another agent or with the operator, ask for that one thing directly. Do not
use a context request as a substitute for patching when you already know the files.

## Hand Off

There is no handoff, no next owner, and no router. "Done" means: you replied in this chat
with what you changed, the exact command you ran and its result, and any remaining risk or
known gap. If another agent genuinely needs to act next, message it yourself with
`agent_chat_send` and say so in your reply.

## Report Blocker

Use after one bounded self-service attempt, or immediately when the blocker is external,
human, or environmental. Name the exact prerequisite and give redaction-safe evidence — a
command label and its failure mode, never raw logs or absolute local paths.

## Request Proof Recipe

**Retired.** There is no Harness proof runner to ask for a recipe, no
`available_proof_recipes`, and no `request_test_run`. Pick the narrowest command yourself
(see Backend Commands / Launcher Commands above) and run it in-session.

## Stage Plan

**Retired.** There are no stages, stage IDs, or stage corrections. If the work is too large
for one bounded pass, say so in the chat and propose the next minimal slice in prose.
