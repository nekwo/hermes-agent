# Stage 22 — portable pinned register and maintenance readiness

Candidate and pushed recovery: `9caca1a1a257244c6e23542b6e034e96016dac32`.
Exact-head CI: https://github.com/nekwo/hermes-agent/actions/runs/35028303037 . Still running at this checkpoint; no full-suite pass claimed. Previous runs were superseded, not release proof.

## Portability

Python 3.12 adds empty `type_params` fields to AST dumps. Hashing now omits only those empty fields, preserving nonempty generic parameters, so the pinned upstream register has identical meaning on Python 3.11 and 3.12. All 66 hashes were regenerated directly from pinned upstream and checked against candidate functions. Windows: 33 register checks passed. Linux 3.11: 33 register plus 33 dashboard checks passed. Logs: `pinned-upstream-portable-hash.log`, `linux-pinned-register.log`.

## Isolated qualification and recovery

- `scripts/run_tests.sh X:/wt/hermes-upstream-audit-20260914/qa-artifacts/test_operator_config_qualification.py -j 1 -- -q`: 1 passed, exit 0. The candidate's real config parser and loader accepted copies of both root and Alice settings under temporary homes. Both copies and live originals retained their SHA256. No credentials or config values were printed; no gateway or platform connector was started. This proves loading only, not live provider authentication.
- `isolated-config-receipt.json` and `isolated-config-qualification.log` record that check. Root SHA256 `75638b3556997ad44ebdf19932c8d92f69c0a6be362afdb00a8fb1b10d195dc2`; Alice `d7607a2bb011eb1fcf9aabc09f6d933a2ca1f6df06aa74f6bca86af083a1e678`.
- Permanent Python 3.12.14 / SQLite 3.53.1 is staged at `X:/Eternia/.hermes/runtimes/python/cpython-3.12.14-windows-x86_64-none/python.exe`. No global PATH or registry changes. Live venv still uses old Python 3.12.5.
- Complete pre-upgrade venv copy and package inventory: `X:/Eternia/.hermes/backups/upstream-sync-20260915/`. Verification compared 7,421 non-cache files by SHA256 with zero mismatches; regenerable bytecode excluded. Receipt: `live-venv-backup-verification.json`.

## Delivery remains pending

Fresh origin fetch confirms Hermes local/origin main at original `34ad8ba33f2508ab10bb24a26f0377ddb62660cb`, divergence 0/0. Candidate descends from main. Review branch `5104732788a545579aa8498f211467a55679b9e8` and original main both resolve to tracked tree `5f5a34ad214d695f5c889a0798e220d175cc91ad`.

All registered worktrees inspected again: unrelated Launcher primary 65 dirty entries, maps-voice 9, local-llama-hermes 7 untracked directories retained. Other worktrees clean. Alice gateway remains running with zero maintenance operations performed. After CI passes, use the authorized profile-scoped stop, fast-forward delivery and live venv upgrade/dependency refresh, then restart and verify the same profile. Do not restart Launcher or rewrite main. No new permission is needed.
