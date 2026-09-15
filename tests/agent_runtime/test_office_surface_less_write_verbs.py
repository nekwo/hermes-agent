"""The three office write verbs that authored a surface from INSIDE their own lock.

``office_lock`` is not reentrant (``locks._file_lock``: a second acquisition from
the same process contends with the first and refuses ``HarnessLockUnavailable``
at the deadline rather than deadlocking). ``upsert_actor`` was split for that
reason on 2026-08-30 — the creation half is reachable on its own as
``OfficeStore._ensure_surface_locked`` — but three verbs were left calling the
PUBLIC ``ensure_surface`` while holding ``office_lock(wsid)``: ``remove_actor``,
``restore_actor`` and ``resolve_conflict``'s edit-vs-remove arm.

They worked, and that is the whole difficulty: ``ensure_surface`` returns BEFORE
acquiring when the surface already exists, so the second acquisition never
happened on any workspace whose office had been authored — which is every
workspace a test had ever built. The defect was reachable only on a SURFACE-LESS
workspace holding a live actor, and on that input each verb contended with
itself and refused at the deadline instead of doing its work.

So each case below is the same shape: an actor that exists with no
``office.json`` beside it, then the verb. Before the fix each raised
``HarnessLockUnavailable``; after it each completes AND authors the surface,
which is the assertion that cannot be satisfied by a verb that merely stopped
refusing.

The last test is the class rather than the instances: a negative source walk
saying the public door may not be called from inside the lock, anywhere. It is a
NEGATIVE guarantee ("this must never be written"), which is the one kind a
source walk is the right instrument for — over-approximation is the safe
direction, and a fourth instance is exactly what a fourth patch would not stop.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from agent_runtime import locks, paths
from agent_runtime.config import harness_root_config_path
from agent_runtime.office_store import OfficeStore
from agent_runtime.store import WorkspaceStore


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def short_lock_deadline():
    """Drop the lock deadline to its floor for this test's root config.

    A self-contending verb refuses only when the deadline expires, so the
    UNFIXED code reddens these tests after ``lock_acquire_timeout_seconds``
    each — 15s by default, three times over. This writes the real root
    ``config.yaml`` (no patching of ``locks``) and then asserts the value
    actually reached the reader, so a wiring change surfaces as a failure here
    instead of silently restoring the 45-second red.
    """

    path = harness_root_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "agent_runtime:\n  lock_acquire_timeout_seconds: 1\n", encoding="utf-8"
    )
    assert locks._lock_timeout_seconds(None) == 1.0
    return path


def _actor_payload(persona_id: str = "dev") -> dict:
    return {
        "persona_id": persona_id,
        "items": [
            {
                "item_id": persona_id,
                "persona_id": persona_id,
                "kind": "agent",
                "position": [1.5, 2.0],
                "folder": "Agents",
            },
        ],
    }


def _workspace_with_actor_and_no_surface(store: OfficeStore) -> str:
    """A live actor file whose workspace has NO ``office.json``.

    Built by authoring both through the real verb and then unlinking the
    surface, rather than by hand-writing an actor file: the point is a store
    state production can reach (a surface lost to a partial sync, an archive, a
    half-restored directory), not a shape only a fixture can make.
    """

    ws = WorkspaceStore().create(name="surface-less")
    store.upsert_actor(ws.id, _actor_payload())
    surface_path = paths.office_surface_path(ws.id)
    assert surface_path.exists()
    surface_path.unlink()
    assert not store.surface_exists(ws.id)
    assert store.actor_exists(ws.id, "dev")
    return ws.id


def test_remove_actor_completes_on_a_surface_less_workspace(short_lock_deadline):
    store = OfficeStore()
    ws = _workspace_with_actor_and_no_surface(store)

    archived = store.remove_actor(ws, "dev")

    assert archived.state == "archived"
    assert paths.office_archived_actor_path(ws, "dev").exists()
    # The verb had to AUTHOR the surface to record the archive in it — the half
    # that the self-contending call could never reach.
    assert store.surface_exists(ws)
    assert "dev" in store.get_surface(ws).archived_actor_keys


def test_restore_actor_completes_on_a_surface_less_workspace(short_lock_deadline):
    store = OfficeStore()
    ws = _workspace_with_actor_and_no_surface(store)
    store.remove_actor(ws, "dev")
    paths.office_surface_path(ws).unlink()
    assert not store.surface_exists(ws)

    restored = store.restore_actor(ws, "dev")

    assert restored.state == "active"
    assert store.actor_exists(ws, "dev")
    assert store.surface_exists(ws)
    assert "dev" not in store.get_surface(ws).archived_actor_keys


def test_resolve_conflict_edit_vs_remove_completes_on_a_surface_less_workspace(
    short_lock_deadline,
):
    store = OfficeStore()
    ws = _workspace_with_actor_and_no_surface(store)
    sidecar = paths.office_conflict_path(ws, "dev")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    # ``remote_actor: null`` with a live local actor IS the edit-vs-remove arm:
    # the remote removed the row, so ``take=remote`` archives the local one.
    sidecar.write_text(
        json.dumps({"actor_key": "dev", "kind": "both_changed", "remote_actor": None}),
        encoding="utf-8",
    )

    resolved = store.resolve_conflict(ws, "dev", take="remote")

    assert resolved is None
    assert not store.actor_exists(ws, "dev")
    assert paths.office_archived_actor_path(ws, "dev").exists()
    assert store.surface_exists(ws)
    assert "dev" in store.get_surface(ws).archived_actor_keys
    assert not sidecar.exists()


# -- the class, not the three instances ------------------------------------


def _modules_taking_the_office_lock() -> list[Path]:
    """Every module that opens an ``office_lock`` block, found by reading them.

    Enumerated from the tree rather than listed here: a fourth caller added in a
    file this test never heard of is precisely the case a hand-kept list cannot
    cover. The walk is over the two packages that own runtime state; a zero
    result fails loudly below rather than passing on an empty set.
    """

    found = []
    for package in ("agent_runtime", "hermes_cli"):
        for path in sorted((REPO_ROOT / package).rglob("*.py")):
            if "office_lock(" in path.read_text(encoding="utf-8"):
                found.append(path)
    return found


def _ensure_surface_calls_inside_office_lock(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        takes_office_lock = any(
            isinstance(item.context_expr, ast.Call)
            and (
                getattr(item.context_expr.func, "id", None) == "office_lock"
                or getattr(item.context_expr.func, "attr", None) == "office_lock"
            )
            for item in node.items
        )
        if not takes_office_lock:
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "ensure_surface"
            ):
                offenders.append(f"{path.name}:{inner.lineno}")
    return offenders


def test_no_caller_takes_the_public_ensure_surface_door_inside_the_office_lock():
    modules = _modules_taking_the_office_lock()
    # locks.py DEFINES office_lock; the others use it. If this ever walks to
    # nothing the pattern was renamed, and a green empty set would be a lie.
    assert len(modules) >= 2, modules

    offenders = sorted(
        offender
        for path in modules
        for offender in _ensure_surface_calls_inside_office_lock(path)
    )
    assert offenders == [], (
        "ensure_surface takes office_lock itself and office_lock is NOT "
        "reentrant, so calling it from inside the lock self-contends and "
        "refuses HarnessLockUnavailable at the deadline on any workspace whose "
        "surface does not already exist. Use _ensure_surface_locked. "
        f"Offenders: {offenders}"
    )
