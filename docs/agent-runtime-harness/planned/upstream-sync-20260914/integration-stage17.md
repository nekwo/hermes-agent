# Checkpoint 17: native Windows verification passed

Candidate and recovery branch `codex/updater-history-safety-20260915` are pushed at `a3d33e95a904c7e981627a0d044a4ac6356a98b4`.

GitHub CI run `35012872230`, Windows job `104529881835`: **212 passed, 1 skipped, 1,684 deselected**, exit success, 247.57 seconds. Raw log: `qa-artifacts/windows-ci-a3d-pass.log`. The deselected set is outside the Windows-only marker; it is not omitted Windows evidence. This includes the real dead-relaunch refusal and desktop handoff probes.

Additional canonical local proof:

- `windows-handoff-budgets-final.log`: **7 passed**, zero failed, four files. Real progress probe took 52.9 seconds; UI delivery, cwd, and retry-policy probes also passed.
- `windows-installer-provenance-final.log`: **6 passed**, zero failed, 41.8 seconds. The test exercises the real 30-second Python-discovery timeout and checks the failure message. Its pytest limit now permits compilation and cleanup.

The added test timeout markers preserve each probe's existing subprocess/diagnostic budget. The global pytest timeout and every production deadline are unchanged. No assertion was removed. The first push of the installer-test marker received a transient HTTP 403; the remote ref was checked and a single retry succeeded.

The preceding partial Python run on `f40acc96efc623c70e705a62a0353681701ef1a2` reached **12,477 passing tests, zero failures** (21.3% of its estimated collection) before the installer-test-only revision replaced it. This is explicitly partial evidence, not a full-suite pass. Current JavaScript/TypeScript and full Python jobs are still running. No further candidate edits are planned absent a failure.

The managed Python environment's pre-maintenance `pip check` reports no broken requirements (exit 0), and package names/versions are recorded locally without credential-bearing URLs. No runtime stop/start, editable reinstall, or Hermes main update has occurred. Launcher local/origin main remains `241391dd66a55d4375fee81609c9195d8240c1b7`.
