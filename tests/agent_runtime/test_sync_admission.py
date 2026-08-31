"""Defect (b): the specialized pull appliers had NO admission scan.

``pull_realm_sync`` runs ``_assert_no_secret_artifacts`` over the artifacts the
GENERIC write-loop maps — and only those. Board, office, skill and profile-file
families are all EXCLUDED from that loop by design, which meant nothing scanned
them on the way in. ``sync_admission`` is the one guard they now share; these
tests pin the posture (secrets everywhere, portability over WIRING only) and the
per-entity isolation that keeps one hostile payload from aborting a pull — or,
worse, from deleting the member's own copy.
"""

from __future__ import annotations

from pathlib import Path

from agent_runtime.sync_admission import (
    PROSE_KEYS,
    content_refusal,
    path_refusal,
    payload_refusal,
    prune_prose,
    refuse_entity,
    refuse_package,
)


# ── posture ────────────────────────────────────────────────────────────────


def test_secrets_are_refused_in_content_and_payload():
    assert content_refusal(b'api_key = "sk-test-secret-value-123456"')[0] == "secret_shaped_value"
    assert content_refusal(b"just prose about api keys") is None
    # A secret embedded INSIDE a value (the persona-config lane's shape)…
    assert payload_refusal({"note": "api_key: sk-abcdefghijklmnopqrstuvwxyz"})[0] == "secret_shaped_value"
    # …including under a PROSE key: the prose exemption is a PORTABILITY
    # exemption only. Secrets are never exempt, anywhere.
    assert payload_refusal({"description": "api_key: sk-abcdefghijklmnopqrstuvwxyz"})[0] == "secret_shaped_value"
    # …and a secret carried as a FIELD, which JSON quoting hides from the raw
    # assignment regex ("token": "…" has a quote between the key and the colon).
    assert payload_refusal({"token": "abcdefghijklmnopqrstuv"})[0] == "secret_shaped_value"
    assert payload_refusal({"items": [{"password": "abcdefghijklmnopqrstuv"}]})[0] == "secret_shaped_value"
    assert payload_refusal({"assignee": "dev", "labels": ["p0"]}) is None


def test_portability_scans_wiring_but_never_prose():
    """The false-positive class this subsystem has already paid for: a card
    description or a display name that MENTIONS an absolute path is English, not
    live wiring, and refusing it would drop legitimate content."""

    wiring = {"backing_profile": "X:\\Eternia\\profiles\\dev"}
    assert payload_refusal(wiring)[0] == "nonportable_path"

    for prose_key in sorted(PROSE_KEYS):
        prose = {prose_key: "see X:\\Eternia\\notes for context"}
        assert payload_refusal(prose) is None, prose_key


def test_prose_pruning_is_recursive_and_opt_outable():
    payload = {"items": [{"display_name": "X:\\a", "persona_id": "dev"}], "title": "X:\\b"}
    assert prune_prose(payload) == {"items": [{"persona_id": "dev"}]}
    # A payload that is 100% wiring (a persona definition) opts out entirely.
    assert prune_prose(payload, frozenset()) == payload
    assert payload_refusal(payload, prose_keys=frozenset())[0] == "nonportable_path"


def test_unsafe_and_secretish_paths_are_refused():
    for rel, code in (
        ("../escape.md", "unsafe_path"),
        ("/etc/passwd", "unsafe_path"),
        ("C:/windows/system32", "unsafe_path"),
        ("pkg/con/SKILL.md", "reserved_path_component"),
        ("pkg/.env/x", "secretish_path"),
        ("pkg/credentials/x", "secretish_path"),
        ("pkg/state.db", "secretish_path"),  # both markers match; secret-ish wins
        ("pkg/runs/x", "hard_excluded_path"),
    ):
        found = path_refusal(rel)
        assert found is not None and found[0] == code, rel
    assert path_refusal("personas/dev/prompt.md") is None
    assert path_refusal("memories/MEMORY.md") is None


def test_refuse_entity_reports_the_first_reason_with_the_entity_key():
    refusal = refuse_entity("board_1:card:c1", payload={"assignee": "/home/tony/repo/x"})
    assert refusal is not None
    assert refusal.as_dict() == {
        "key": "board_1:card:c1",
        "code": "nonportable_path",
        "message": refusal.message,
    }
    assert refuse_entity("clean", payload={"assignee": "dev"}) is None


def test_refuse_package_scans_every_file_and_never_portability(tmp_path):
    """A skill's documentation legitimately names absolute paths; only secrets and
    unsafe paths close the door."""

    pkg = tmp_path / "demo"
    (pkg / "references").mkdir(parents=True)
    (pkg / "SKILL.md").write_text("Run it from X:\\Eternia\\hermes-agent\n", encoding="utf-8")
    assert refuse_package("demo", pkg) is None

    (pkg / "references" / "notes.md").write_text('token: "abcdefghijklmnopqrstuvwxyz"\n', encoding="utf-8")
    refusal = refuse_package("demo", pkg)
    assert refusal is not None and refusal.code == "secret_shaped_value"


# ── the appliers actually run it ───────────────────────────────────────────


def _write_json(path: Path, payload: dict) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_board_pull_refuses_a_secret_card_without_archiving_the_local_copy(
    isolate_agent_runtime_root, tmp_path
):
    """The isolation that matters: a refused card must be EXCLUDED from the
    reconcile, not treated as "absent remotely" — otherwise a hostile payload
    turns into a deletion of the member's own card."""

    from agent_runtime.board_sync import apply_board_pull
    from agent_runtime.board_store import BoardStore
    from agent_runtime.store import WorkspaceStore

    workspace = WorkspaceStore().create(name="W")
    store = BoardStore()
    board = store.ensure_default_board(workspace.id)
    card = store.add_card(board_id=board.board_id, title="mine")

    subtree = tmp_path / "subtree"
    board_dir = subtree / "store" / "boards" / board.board_id
    _write_json(
        board_dir / "board.json",
        {
            "board_id": board.board_id,
            "workspace_id": workspace.id,
            "title": board.title,
            "columns": [{"column_id": c.column_id, "title": c.title, "kind": c.kind} for c in board.columns],
            "archived_card_ids": [],
            "schema_version": 1,
        },
    )
    _write_json(
        board_dir / "cards" / f"{card.card_id}.json",
        {
            "card_id": card.card_id,
            "board_id": board.board_id,
            "column_id": board.columns[0].column_id,
            "title": "hostile",
            "order_key": "m",
            "assignee": "api_key: sk-abcdefghijklmnopqrstuvwxyz",
            "schema_version": 1,
        },
    )

    summary = apply_board_pull("realm_test", subtree)

    assert [row["code"] for row in summary.refused] == ["secret_shaped_value"]
    assert summary.archived == 0
    assert [item.card_id for item in store.list_cards(board.board_id)] == [card.card_id]
    assert store.get_card(card.card_id, board_id=board.board_id).title == "mine"


def test_office_pull_refuses_a_machine_shaped_actor(isolate_agent_runtime_root, tmp_path):
    from agent_runtime.office_sync import apply_office_pull
    from agent_runtime.office_store import OfficeStore
    from agent_runtime.store import WorkspaceStore

    workspace = WorkspaceStore().create(name="W")
    store = OfficeStore()
    surface = store.ensure_surface(workspace.id)

    subtree = tmp_path / "subtree"
    office_dir = subtree / "store" / "office" / workspace.id
    _write_json(
        office_dir / "office.json",
        {
            "workspace_id": workspace.id,
            "folders": list(surface.folders),
            "archived_actor_keys": [],
            "schema_version": 1,
        },
    )
    _write_json(
        office_dir / "actors" / "dev.json",
        {
            "actor_key": "dev",
            "workspace_id": workspace.id,
            "persona_id": "dev",
            "backing_profile": "X:\\Eternia\\profiles\\dev",
            "items": [],
            "state": "active",
            "schema_version": 1,
        },
    )

    summary = apply_office_pull("realm_test", subtree)

    assert [row["code"] for row in summary.refused] == ["nonportable_path"]
    assert summary.adopted == 0
    assert store.scan_actors(workspace.id).actors == []
