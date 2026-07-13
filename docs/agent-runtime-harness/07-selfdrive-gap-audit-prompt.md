# 07 — Self-drive gap deep-audit + fix prompt (paste into Claude)

> **CLOSED 2026-07-03 (second session).** All five gaps were reproduced, fixed, and
> live-proven — see doc-06 §6.1 for the closure record. Commits: `0170c67c2` (gap 1),
> `9fd801c39` (gap 2), `9d697ae1d` (gap 3), `8989d408c` (gap 4), `634b990ee` (gap 5),
> `4e5f52bf1` (cheap checks), plus three root-cause fixes for bugs the live acceptance
> itself surfaced: `303193e9d` (no-required-gate stage never completed),
> `3ffab38ed`/`138fd1bbc` (default-blueprint placeholder repo leak into scope/gate),
> `13b19e7c0` (dev grounding in placeholder repo). Live acceptance: task_5ed6f049
> (1m29s create-to-done, trap description scoped correctly, targeted daemon,
> goal-named gate proof, auto-archive) and task_5008f128 (chaos drill: mid-turn stop
> reaped the run immediately, done unattended after restart). Suite: 1293 → 1329.
> Known residual: Neko no-op plan-release loop after a failed gate (doc-06 §6.1).

Context: the 2026-07-03 live default-run session (doc-06 §6 + commits `2cc24a93d`,
`3420ecd11`, `c188553f4`, `acb62a2bb`) fixed the untargeted incident gate, stale
incident links, the Neko adjudication re-dispatch loop, and liveness false positives —
live-proven by an unattended 2m51s goal (task_1f9c1698). Five gaps were recorded but
NOT fixed. This prompt is the engagement to audit and close them.

---

You are deep-auditing and fixing the remaining self-drive gaps in the Hermes Agent
Runtime Harness so a default goal is reliable without operator surgery.

REPO & GROUND TRUTH
- Repo: `X:\Eternia\hermes-agent` (Windows). Check your brain first: read `AGENTS.md` /
  `CLAUDE.md` and `docs/agent-runtime-harness/00-index.md` before any code.
- Read `docs/agent-runtime-harness/06-recursive-agent-supervised-execution.md` §6
  (verification appendix) — it records how each gap below was observed live, with
  incident ids and timings. The runtime root is `X:\Eternia\.hermes\agent-runtime`;
  the editable venv serves the checkout, so harness edits are live on the next CLI
  call, but a RUNNING daemon must be restarted to pick them up.
- Reference run evidence: task_d7ec4dfc (diagnostic, cancelled-spent, archived
  2026-07-03) and task_1f9c1698 (clean 2m51s done) — both in
  `X:\Eternia\.hermes\agent-runtime\deleted_archive\`.

NON-NEGOTIABLES
- Work on main and commit + push after EVERY completed gap — the main worktree can be
  reset by live harness automation; uncommitted work is lost work.
- Before starting, run `python -m pytest tests/agent_runtime --collect-only -q` and
  record the count (expected ≥1293); after every gap the full suite must be green at
  ≥ that count + your new tests. Never weaken an existing test.
- AUDIT BEFORE FIXING: for each gap, first verify it still reproduces against current
  main with exact file:line evidence (a prior session may have fixed it). If it no
  longer reproduces, record that with proof and move on — do not fix ghosts.
- Never claim something works without running it — paste exact command output.
- New config keys: runtime_config.py → config.py → migrations.py (3-file pattern).
  New event types: register in decision_contract_registry.py. Redaction-safe text
  everywhere (safe_assignment_text-style). Behavior changes ship flag-safe where they
  alter live-goal semantics.
- The final acceptance is LIVE, not just unit tests (see ACCEPTANCE below).

THE FIVE GAPS — audit + fix in this order

1. NEKO MIS-SCOPE OF affected_repos (highest impact — burned a full Dev budget).
   Observed: a goal saying "In the hermes-agent repo …" mid-description was scoped to
   EterniaLauncher; the same goal with "Affected repo: hermes-agent (the
   agent-runtime-harness repo alias)" scoped correctly. Audit: how Neko's scope stage
   derives affected_repos (planning.py / scope decision contract / context_builder);
   the resolvable alias list lives at repo_context.py `_REPO_ALIAS_PATHS` +
   `_HARNESS_REPO_ALIASES` (~:945). Fix direction (pick the minimal one that makes the
   failure structural, not prompt-luck): (a) give the scope turn the canonical alias
   list + require affected_repos ⊆ known aliases (validation with repair feedback on
   mismatch), and/or (b) goal-create accepts an explicit `--affected-repo` that
   pre-seeds and pins scope; description-derived scope must not silently contradict a
   repo alias literally named in the title/description — mismatch → repair feedback to
   Neko, not silent acceptance. Test: a description naming `hermes-agent` cannot scope
   to EterniaLauncher without a recorded justification.

2. `task create --start-daemon` STARTS AN UNTARGETED DAEMON (target_task_id null).
   Consequences: terminal auto-archive never runs; targeted-mode invariants don't
   apply. Audit `goal_runner.py` / `mission_goal.py` daemon-start path vs
   `daemon.py` `MissionDaemon.__init__` (note `self.target_task_id = None` is
   HARDCODED at daemon.py:63 — the constructor ignores its own parameter; that line
   is almost certainly the bug). Fix: thread the created task id through to the
   spawned daemon process and verify `daemon status` shows it; terminal auto-archive
   must fire through `_settle_terminal_target_if_needed`. Test: create-with-daemon → daemon status
   carries target_task_id; on done, the archive batch appears without operator action.

3. AUTHORITATIVE GATE IGNORES GOAL-NAMED FOCUSED PROOF COMMANDS.
   Observed: a hermes-agent investigation goal naming the exact command
   `python -m pytest tests/agent_runtime/test_liveness.py -q` got gate re-runs of
   `flutter analyze` + a backend-venv command (generic recipes); the named command
   only landed via the observed-agent-proof lane. Audit: proof recipe selection
   (`proof_recipes.py`, `proof_command_policy.py`, `_build_authoritative_stage_gate_decision`
   ticker.py) and how stage test_plan / goal text feeds it. Fix: when the goal/stage
   names an exact runnable proof command for the scoped repo, the harness gate re-runs
   THAT command (through `_run_bounded_process`, workdir = the stage's repo) as the
   authoritative proof, with generic recipes as fallback — never instead-of. The
   skill/docs rule already states focused proof outranks generic recipes; make the
   code match. Test: a goal naming a focused command produces a harness-owned gate
   proof whose command matches it.

4. NEKO ADJUDICATION ANSWERS `block` INSTEAD OF CLOSING INCIDENTS.
   Now bounded (one pass per evidence signal, `acb62a2bb`) but each incident still
   burns a wasted pass and then waits on an operator. Audit: what the adjudication
   turn can actually DO — does the decision contract / capability surface expose
   incident-close (resolve) to neko_supervisor, and does its context say the incident
   is ITS to resolve? (decision_contract_registry.py, context_builder.py incident
   context, harness-mission-lead skill). Fix: give the adjudication turn a
   first-class resolve path — close incident with redaction-safe reason when the
   underlying run is already terminal (the run_hung/budget cases from this session
   were exactly that), with repair feedback when it blocks on an incident it has the
   capability to close. Update + reinstall the harness-mission-lead skill if the
   contract changes. Test: an open incident whose run is terminal is closed by a neko
   adjudication turn (stub runtime), not answered with `block`.

5. `daemon stop` ORPHANS THE MID-TURN RUN.
   Observed: restarting the daemon killed the in-flight Neko turn; the run row stayed
   `running` and the liveness watchdog reaped it ~6 min later. The watchdog is the
   backstop, not the contract. Audit `daemon.py` stop path (`_terminate`, taskkill)
   and startup. Fix both ends, cheap and deterministic: on graceful stop, cancel
   in-flight runs owned by this daemon's engine before exiting; on daemon START, reap
   active runs whose owning daemon pid is dead (`RunStore.cancel(reason="daemon_orphan_reap_restart")`
   + worker close, same pattern as liveness.py `_remediate_hung`). Test: stop-with-
   active-run cancels it; start-after-kill reaps the orphan immediately (not in 300s).

ALSO VERIFY WHILE IN THERE (cheap checks, fix only if broken)
- Daemon status `liveness` block: was observed `None` mid-session on a fresh daemon;
  confirm `_liveness_loop` writes it every poll and `_status_from_tick` preserves it.
- The mission budget-approval lane (`budget_approval.py`, `neko_extension_cap`): the
  spent goal showed `approve_budget_continuation` as next_expected with no actor able
  to approve. Confirm an approval path exists end to end or record precisely what's
  missing — do not build a new lane without auditing what's there.

ACCEPTANCE (live, unattended — run these yourself)
1. Suite green at floor + new tests.
2. Fresh default goal via `harness task create --start-daemon` with a description that
   names the repo only mid-sentence (the gap-1 trap): scopes correctly, daemon is
   TARGETED, reaches `done` unattended, gate proof includes the goal-named focused
   command, auto-archive fires. Target ≤6 min create-to-done.
3. Chaos drill: start a second goal, `daemon stop` mid-turn, `daemon start` — the
   orphan is reaped immediately and the goal still reaches `done` unattended.
4. `harness status --json` clean at the end; archive batches present; no manual tick,
   no manual incident close, no unblock (any of those = not unattended — say so).

CLOSEOUT (blunt, evidence-based)
- Per gap: reproduced? (file:line evidence) → fix summary → commit hash → test names.
- Live acceptance evidence: task ids, timings, proof ids, archive batch paths.
- Anything NOT fixed, with the exact reason and the next concrete step.
- Update `docs/agent-runtime-harness/06-recursive-agent-supervised-execution.md` §6
  residuals and this file's header with what changed, in the same commits.
