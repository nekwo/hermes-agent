"""One-shot live-home migration: retire the June codex-test scope IDs.

The active realm/workspace rows date from a June codex-test session and still
carry those IDs (`realm_codex-test-realm_cad6d4` / `ws_codex-test-workspace_28d285`)
even though both are named "default". This migrates the live scope onto the
clean IDs `realm_default` / `ws_default` (whose archived July husk rows are
retired into deleted_archive first), renames the rows to "Default", and
rewrites every live reference: active pointers, persona instances, office
scene, board, serve read model.

Run ONLY while the serve is down (no concurrent writers). A full backup of the
touched files already exists at
  X:/Eternia/.hermes/agent-runtime/migration_backups/2026-08-29-codex-id-cleanup

Historical stores (events_archive, deleted_archive, prompt_observability*,
persona_instances_archive, agent_create_reservations) are records of the past
and are deliberately NOT rewritten.
"""

import io
import json
import os
import shutil
from pathlib import Path

root = Path(r"X:/Eternia/.hermes/agent-runtime")
OLD_R, NEW_R = "realm_codex-test-realm_cad6d4", "realm_default"
OLD_W, NEW_W = "ws_codex-test-workspace_28d285", "ws_default"

# 1. Retire the archived husk rows so their IDs are free to take over.
husk_dir = root / "deleted_archive" / "20260829T000000Z_codex_id_cleanup_husks"
husk_dir.mkdir(parents=True, exist_ok=True)
for husk in (root / "realms" / f"{NEW_R}.json", root / "workspaces" / f"{NEW_W}.json"):
    if husk.exists():
        shutil.move(str(husk), str(husk_dir / husk.name))
        print("retired husk:", husk)

# 2. Rewrite every live reference.
targets = [
    root / "active_realm.json",
    root / "active_workspace.json",
    root / "realms" / f"{OLD_R}.json",
    root / "workspaces" / f"{OLD_W}.json",
]
targets += sorted((root / "persona_instances").glob("*.json"))
targets += [p for p in (root / "serve_read_model").rglob("*") if p.is_file()]
targets += [p for p in (root / "office" / OLD_W).rglob("*") if p.is_file()]
targets += [p for p in (root / "boards" / f"board_default_{OLD_W}").rglob("*") if p.is_file()]

changed = 0
for p in targets:
    try:
        s = io.open(p, encoding="utf-8").read()
    except Exception as e:  # binary/locked file — report, never guess
        print("SKIP unreadable", p, e)
        continue
    n = s.replace(OLD_R, NEW_R).replace(OLD_W, NEW_W)
    if n != s:
        io.open(p, "w", encoding="utf-8", newline="").write(n)
        changed += 1
print("files rewritten:", changed)

# 3. Proper display names on the migrated rows.
for f, fixes in (
    (root / "realms" / f"{OLD_R}.json", {"name": "Default", "default_workspace_name": "Default"}),
    (root / "workspaces" / f"{OLD_W}.json", {"name": "Default"}),
):
    d = json.load(io.open(f, encoding="utf-8"))
    d.update(fixes)
    io.open(f, "w", encoding="utf-8", newline="").write(json.dumps(d, indent=2, sort_keys=True))
    print("named:", f.name, fixes)

# 4. Rename the row files and the id-keyed dirs.
os.rename(root / "realms" / f"{OLD_R}.json", root / "realms" / f"{NEW_R}.json")
os.rename(root / "workspaces" / f"{OLD_W}.json", root / "workspaces" / f"{NEW_W}.json")
os.rename(root / "office" / OLD_W, root / "office" / NEW_W)
os.rename(root / "boards" / f"board_default_{OLD_W}", root / "boards" / f"board_default_{NEW_W}")
print("renamed rows, office dir, board dir")

# 5. Verify: nothing live still says codex-test.
leftovers = []
for d in ("realms", "workspaces", "persona_instances", "office", "boards", "serve_read_model"):
    for p in (root / d).rglob("*"):
        if p.is_file() and "codex-test" in p.read_text(encoding="utf-8", errors="ignore"):
            leftovers.append(str(p))
for p in (root / "active_realm.json", root / "active_workspace.json"):
    if "codex-test" in p.read_text(encoding="utf-8"):
        leftovers.append(str(p))
print("VERIFY leftovers:", leftovers or "none — migration clean")
