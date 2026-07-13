# Upstream Sync Gate

Before merging an upstream sync branch back to `main`, run the fork-owned gate
from the Hermes checkout:

```powershell
python scripts/upstream_sync_gate.py --base-ref upstream/main
```

The gate prints one final `UPSTREAM_SYNC_GATE PASS` or `UPSTREAM_SYNC_GATE FAIL`
line and a JSON result block with command tails. Record that verdict in the merge
commit message. A failure blocks the merge until the fork integration issue is
fixed.

The required lanes are:

- `python -m pytest tests/agent_runtime -q`
- `python -m pytest tests/hermes_cli -q`
- `python -m hermes_cli.main harness smoke --json --temp-root --no-model`
- `flutter test test/features/mission_control --reporter=compact` when changed
  paths touch agent/tool/Harness seams, or when `--include-launcher` is supplied.

Useful operator forms:

```powershell
python scripts/upstream_sync_gate.py --dry-run --base-ref upstream/main
python scripts/upstream_sync_gate.py --include-launcher
python scripts/upstream_sync_gate.py --simulate-broken-seam
python scripts/upstream_sync_gate.py --fail-fast --base-ref upstream/main
```

`--simulate-broken-seam` is only a local red rehearsal. It prepends a deterministic
missing integration import so operators can prove the gate fails closed without
editing upstream-owned files or leaving a dirty seam behind. Rehearsals fail fast
by default; normal gate runs collect every lane unless `--fail-fast` is supplied.
