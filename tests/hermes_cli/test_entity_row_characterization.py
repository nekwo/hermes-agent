"""S48 — characterization of the five CLI entity-row projections, across the
deliberate output change that consolidated them onto the snapshot builders.

Ledger item 4 (`docs/agent-runtime-harness/19-deferred-debt-ledger.md`) was an
operator ruling, not a cleanup: `hermes harness` rendered entity rows through
five hand-written projections that BYPASSED `agent_runtime/snapshot.py`'s
builders, so the CLI printed values the wire masks and lists the wire bounds.
Two halves of that class had already shipped as live defects and been fixed
narrowly (`a21ab1a2a`): the workspace-show `tasks` NameError and the realm-row
`"in_sync"` lie. This is the structural retirement.

The five, as re-derived from the tree (NOT from the audit's line numbers — the
files moved under S46/S47 and `e887cdf26`):

======================= ============================== ========================
CLI projection          verbs                          builder it now re-keys
======================= ============================== ========================
harness `_workspace_row`  workspace list/show/create/     `_workspace_summary`
                          rename/bind-realm/set-active/
                          delete --dry-run/archive
harness `_realm_row`      realm list/show/create/         `_realm_summary`
                          bind-server/rename/adopt
board `_board_row`        board list/show/create/update   `board_summary_row`
board `_card_row`         board card add/edit/move/       `_board_card_row`
                          archive/restore/resolve-conflict
office `_office_actor_row` office actor upsert/remove/    `_office_actor_summary_row`
                          restore/resolve-conflict
======================= ============================== ========================

`_office_surface_row` (office show / set-folders) went with the set as the
container that held the uncapped actor list, and `_office_item_row` was deleted
outright — it re-declared, key for key, the scene-item block the actor builder
already projects.

WHAT CHANGED, exactly — everything else is byte-identical:

* card `title`, `description` and `checklist[].text` are MASKED, value-level and
  IN PLACE: `"Rotate api_key: sk-live-…"` renders `"Rotate api_key: [redacted]"`.
  No field is blanked or dropped because it contained a secret; the prose around
  the value survives verbatim. Same `_mask_board_secrets` the wire applies.
* card `description` is bounded at `BOARD_CARD_DESC_LIMIT` (2,048) — a REAL cut,
  since `BoardStore` accepts 4,000 — and the cut is ACCOUNTED by
  `description_truncated` on the full row, never silent.
* `board show --full` bounds `cards` at `MAX_BOARD_CARDS_PROJECTED` (500) and
  accounts the remainder in `cards_truncated`.
* `office show --full` bounds `actor_defs` at `MAX_OFFICE_ACTORS_PROJECTED`
  (200) and accounts the remainder in `actors_truncated`.
* workspace, realm and office-actor rows are UNCHANGED in value — those two
  projections leaked nothing; what they carried was duplication, and duplication
  is what shipped both prior defects.

Nothing was added to hide output from an operator: no field is removed, no
unmask flag was invented, and every non-secret value stays fully readable.
"""

from __future__ import annotations

import pytest

import hermes_cli.harness as harness
from agent_runtime.board_store import BoardStore
from agent_runtime.office_store import OfficeStore
from agent_runtime.store import RealmStore, WorkspaceStore


#: A secret assignment embedded in ordinary prose. The masked rendering must
#: keep BOTH sides of it — that is the difference between masking and blanking.
SECRET_TITLE = "Rotate api_key: sk-live-DEADBEEF0123456789 before Friday"
MASKED_TITLE = "Rotate api_key: [redacted] before Friday"

SECRET_DESC_HEAD = "step 1 password: hunter2hunter2hunter2 then restart; keep notes "
MASKED_DESC_HEAD = "step 1 password: [redacted] then restart; keep notes "

SECRET_CHECKLIST_TEXT = "rotate token: abc123456789 now"
MASKED_CHECKLIST_TEXT = "rotate token: [redacted] now"


@pytest.fixture
def fixture_store():
    """One realm / workspace / board / card / office actor, with secret-bearing
    card prose and a description past the projection bound."""

    realm = RealmStore().create(name="s48-realm", server_id="srv_s48")
    workspace = WorkspaceStore().create(name="s48-ws", realm_id=realm.id)

    boards = BoardStore()
    board = boards.create(workspace_id=workspace.id, title="S48 Board")
    card = boards.add_card(
        board_id=board.board_id,
        title=SECRET_TITLE,
        # 3,000 tail characters: under the store's 4,000 limit, over the
        # projection's 2,048 bound, so the cut is real and observable.
        description=SECRET_DESC_HEAD + ("x" * 3000),
        labels=["ops", "urgent"],
        assignee="operator",
        checklist=[{"text": SECRET_CHECKLIST_TEXT, "done": False}],
    )
    card = boards.get_card(card.card_id, board_id=board.board_id)

    office = OfficeStore()
    office.ensure_surface(workspace.id)
    actor = office.upsert_actor(
        workspace.id,
        {
            "persona_id": "neko",
            "backing_profile": "alice",
            "items": [
                {
                    "item_id": "it_1",
                    "kind": "agent",
                    "position": [1, 2],
                    "folder": "root",
                    "display_name": "Neko",
                }
            ],
        },
    )
    return {
        "realm": realm,
        "workspace": workspace,
        "boards": boards,
        "board": boards.get(board.board_id),
        "card": card,
        "office": office,
        "actor": actor,
    }


# ── (1) masked IN PLACE, surrounding text intact ────────────────────────────
#
# The coordinator constraint this file exists to hold: masking is value-level.
# A field must never render empty, and must never disappear, because it held a
# secret.


def test_card_title_is_masked_in_place_not_blanked(fixture_store):
    row = harness._card_row(fixture_store["card"])
    assert row["title"] == MASKED_TITLE
    assert row["title"].startswith("Rotate api_key: ")
    assert row["title"].endswith(" before Friday")
    assert "sk-live-DEADBEEF0123456789" not in row["title"]


def test_card_description_is_masked_in_place_not_blanked(fixture_store):
    row = harness._card_row(fixture_store["card"], full=True)
    assert row["description"].startswith(MASKED_DESC_HEAD)
    assert "hunter2hunter2hunter2" not in row["description"]
    # The prose AFTER the secret survives — the tail is still there, capped.
    assert row["description"].endswith("x")


def test_card_checklist_text_is_masked_in_place_not_blanked(fixture_store):
    row = harness._card_row(fixture_store["card"], full=True)
    assert row["checklist"] == [{"text": MASKED_CHECKLIST_TEXT, "done": False}]


def test_two_secrets_in_one_field_are_both_masked_and_the_prose_survives(fixture_store):
    """Value-level, per occurrence — not a whole-field decision.

    Also pins the ONE readability cost inherited from the wire's regex
    (`TEXT_SECRET_ASSIGNMENT_RE`, greedy ``\\S+``): punctuation ATTACHED to the
    secret value is swallowed with it, so ``token: abc,`` loses its comma.
    Documented rather than worked around — changing it would fork the pattern
    the whole runtime shares, which is exactly the duplication S48 retired.
    """

    card = fixture_store["boards"].add_card(
        board_id=fixture_store["board"].board_id,
        title="check api_key: one and secret: two, then ship",
        description="plain prose",
    )
    row = harness._card_row(card)
    assert row["title"] == "check api_key: [redacted] and secret: [redacted] then ship"


def test_a_card_with_no_secret_renders_byte_identically(fixture_store):
    """The mask is a no-op on ordinary prose — it never rewrites clean text."""

    clean = fixture_store["boards"].add_card(
        board_id=fixture_store["board"].board_id,
        title="Ship the release notes",
        description="No credentials here, just prose. 100% readable.",
    )
    row = harness._card_row(clean, full=True)
    assert row["title"] == "Ship the release notes"
    assert row["description"] == "No credentials here, just prose. 100% readable."
    assert row["description_truncated"] is False


def test_the_board_row_cards_are_masked_too(fixture_store):
    """`board show --full` reached cards through its own projection; the leak
    was not confined to the card verbs."""

    row = harness._board_row(fixture_store["boards"], fixture_store["board"], full=True)
    assert [card["title"] for card in row["cards"]] == [MASKED_TITLE]


# ── (2) caps are ACCOUNTED, never silent ────────────────────────────────────


def test_description_cap_is_marked(fixture_store):
    from agent_runtime.snapshot import BOARD_CARD_DESC_LIMIT

    row = harness._card_row(fixture_store["card"], full=True)
    assert row["description_truncated"] is True
    assert len(row["description"]) <= BOARD_CARD_DESC_LIMIT


def test_board_full_row_accounts_its_card_bound(fixture_store, monkeypatch):
    """A board past the projection bound reports the remainder count."""

    from agent_runtime import snapshot

    monkeypatch.setattr(snapshot, "MAX_BOARD_CARDS_PROJECTED", 1)
    boards, board = fixture_store["boards"], fixture_store["board"]
    for i in range(3):
        boards.add_card(board_id=board.board_id, title=f"filler {i}")

    row = harness._board_row(boards, boards.get(board.board_id), full=True)
    assert len(row["cards"]) == 1
    assert row["cards_truncated"] == 3
    # The count operators read is the WHOLE column population, not the
    # projected slice — a cap must never quietly shrink a count.
    assert row["active_cards"] == 4


def test_office_full_row_accounts_its_actor_bound(fixture_store, monkeypatch):
    from agent_runtime import snapshot

    monkeypatch.setattr(snapshot, "MAX_OFFICE_ACTORS_PROJECTED", 1)
    office, workspace = fixture_store["office"], fixture_store["workspace"]
    for name in ("bob", "carol"):
        office.upsert_actor(
            workspace.id,
            {"persona_id": name, "items": [{"item_id": f"it_{name}", "kind": "agent", "position": [0, 0]}]},
        )

    row = harness._office_surface_row(office, workspace.id, full=True)
    assert len(row["actor_defs"]) == 1
    assert row["actors_truncated"] == 2
    assert row["actors"] == 3


def test_uncapped_rows_still_carry_the_accounting_key(fixture_store):
    """The marker is always present, so "no cut" is stated rather than assumed
    from a missing key (the wire's own posture)."""

    board_row = harness._board_row(fixture_store["boards"], fixture_store["board"], full=True)
    office_row = harness._office_surface_row(fixture_store["office"], fixture_store["workspace"].id, full=True)
    assert board_row["cards_truncated"] == 0
    assert office_row["actors_truncated"] == 0


# ── (3) non-secret content is byte-identical to the pre-consolidation row ────


def test_board_row_key_sets_are_unchanged(fixture_store):
    boards, board = fixture_store["boards"], fixture_store["board"]
    skinny = harness._board_row(boards, board)
    full = harness._board_row(boards, board, full=True)
    assert sorted(skinny) == ["active_cards", "columns", "id", "revision", "title", "updated_at", "workspace_id"]
    assert sorted(full) == [
        "active_cards",
        "archived_card_ids",
        "cards",
        # the one added key: the cap accounting
        "cards_truncated",
        "column_defs",
        "columns",
        "id",
        "revision",
        "title",
        "updated_at",
        "workspace_id",
    ]


def test_board_row_non_secret_values_are_unchanged(fixture_store):
    boards, board = fixture_store["boards"], fixture_store["board"]
    row = harness._board_row(boards, board, full=True)
    assert row["id"] == board.board_id
    assert row["workspace_id"] == fixture_store["workspace"].id
    assert row["title"] == "S48 Board"  # board titles were never masked
    assert row["columns"] == 4
    assert row["revision"] == board.revision
    assert row["archived_card_ids"] == []
    assert row["column_defs"] == [
        {"column_id": "col_queued", "title": "Queued", "kind": "queued", "wip_limit": None},
        {"column_id": "col_active", "title": "In Progress", "kind": "active", "wip_limit": None},
        {"column_id": "col_review", "title": "Review", "kind": "review", "wip_limit": None},
        {"column_id": "col_done", "title": "Complete", "kind": "done", "wip_limit": None},
    ]


def test_card_row_key_sets_are_unchanged(fixture_store):
    card = fixture_store["card"]
    skinny = harness._card_row(card)
    full = harness._card_row(card, full=True)
    assert sorted(skinny) == ["column_id", "id", "priority", "state", "title", "updated_at"]
    assert sorted(full) == [
        "assignee",
        "board_id",
        "checklist",
        "column_id",
        "created_at",
        "created_by",
        "description",
        # the one added key: the cap accounting
        "description_truncated",
        "id",
        "labels",
        "order_key",
        "priority",
        "revision",
        "state",
        "title",
        "updated_at",
    ]


def test_card_row_non_secret_values_are_unchanged(fixture_store):
    card = fixture_store["card"]
    row = harness._card_row(card, full=True)
    assert row["id"] == card.card_id
    assert row["board_id"] == card.board_id
    assert row["column_id"] == "col_queued"
    assert row["priority"] == "p2"
    assert row["state"] == "active"
    assert row["labels"] == ["ops", "urgent"]
    assert row["assignee"] == "operator"
    assert row["created_by"] == "operator"
    assert row["order_key"] == card.order_key
    assert row["revision"] == card.revision
    # Timestamps stay ``datetime`` — the Stage-42 printer is the serialization
    # authority for this lane, so ``--output table`` renders as it always did.
    assert row["created_at"] == card.created_at
    assert row["updated_at"] == card.updated_at


def test_workspace_row_is_value_identical_to_before(fixture_store):
    """Workspace rows leaked nothing; the consolidation is pure de-duplication.
    Pinned literally so a future "while we're here" edit is a red test."""

    workspace = fixture_store["workspace"]
    skinny = harness._workspace_row(workspace)
    assert skinny == {
        "id": workspace.id,
        "name": "s48-ws",
        "realm_id": fixture_store["realm"].id,
        "agents": 0,
        "agent_ids": [],
        "live_scoped_agent_count": 0,
        "live_scoped_agent_ids": [],
        "roster_agent_count": 0,
        "roster_agent_ids": [],
        "isolation": workspace.isolation,
        "updated_at": workspace.updated_at,
    }
    full = harness._workspace_row(workspace, full=True)
    assert full == {
        **skinny,
        "kind": "workspace",
        "slug": workspace.slug,
        "default_blueprint_id": None,
        "max_concurrent_lanes": workspace.max_concurrent_lanes,
        "archived": False,
        "created_at": workspace.created_at,
    }


def test_realm_row_is_value_identical_to_before(fixture_store):
    realm = fixture_store["realm"]
    skinny = harness._realm_row(realm)
    assert skinny == {
        "id": realm.id,
        "name": "s48-realm",
        "server_id": "srv_s48",
        "default_workspace_id": realm.default_workspace_id,
        "default_workspace_version": realm.default_workspace_version,
        "workspaces": 1,
        "sync": None,
        "updated_at": realm.updated_at,
    }
    full = harness._realm_row(realm, full=True)
    assert full == {
        **skinny,
        "kind": "realm",
        "slug": realm.slug,
        "workspace_ids": [fixture_store["workspace"].id],
        "default_workspace_name": realm.default_workspace_name,
        "archived": False,
        "sync_manifest_ref": realm.sync_manifest_ref,
        "created_at": realm.created_at,
    }


def test_office_actor_row_is_value_identical_to_before(fixture_store):
    actor = fixture_store["actor"]
    skinny = harness._office_actor_row(actor)
    assert skinny == {
        "id": actor.actor_key,
        "workspace_id": fixture_store["workspace"].id,
        "persona_id": "neko",
        "persona_instance_id": actor.persona_instance_id,
        "items": 1,
        "state": "active",
        "revision": actor.revision,
        "updated_at": actor.updated_at,
    }
    full = harness._office_actor_row(actor, full=True)
    assert full == {
        **skinny,
        "backing_profile": "alice",
        "item_defs": [
            {
                "item_id": "it_1",
                "persona_id": "neko",
                "kind": "agent",
                "position": [1.0, 2.0],
                "folder": "root",
                "display_name": "Neko",
                "pet_slug": None,
                "scale": 1.0,
            }
        ],
        "updated_by": "operator",
        "created_at": actor.created_at,
    }


def test_office_surface_row_key_sets_are_unchanged(fixture_store):
    office, workspace = fixture_store["office"], fixture_store["workspace"]
    skinny = harness._office_surface_row(office, workspace.id)
    full = harness._office_surface_row(office, workspace.id, full=True)
    assert sorted(skinny) == ["actors", "conflicts", "folders", "revision", "updated_at", "workspace_id"]
    assert sorted(full) == [
        "actor_defs",
        # the one added key: the cap accounting
        "actors",
        "actors_truncated",
        "archived_actor_keys",
        "conflict_actor_keys",
        "conflicts",
        "folders",
        "revision",
        "updated_at",
        "workspace_id",
    ]
    assert skinny["folders"] == ["Agents", "Desks"]
    assert skinny["conflicts"] == 0


# ── The consolidation itself: each row is a re-key of the builder's row ──────


def test_workspace_row_delegates_to_the_snapshot_builder(fixture_store, monkeypatch):
    """A sentinel the CLI cannot have computed itself proves the delegation —
    an assertion on VALUES alone would pass against a re-grown hand-roll."""

    from agent_runtime import snapshot

    real = snapshot._workspace_summary

    def _tagged(workspace, **kwargs):
        row = real(workspace, **kwargs)
        row["name"] = "FROM-BUILDER"
        return row

    monkeypatch.setattr(snapshot, "_workspace_summary", _tagged)
    assert harness._workspace_row(fixture_store["workspace"])["name"] == "FROM-BUILDER"


def test_realm_row_delegates_to_the_snapshot_builder(fixture_store, monkeypatch):
    from agent_runtime import snapshot

    real = snapshot._realm_summary

    def _tagged(realm, **kwargs):
        row = real(realm, **kwargs)
        row["name"] = "FROM-BUILDER"
        return row

    monkeypatch.setattr(snapshot, "_realm_summary", _tagged)
    assert harness._realm_row(fixture_store["realm"])["name"] == "FROM-BUILDER"


def test_board_and_card_rows_delegate_to_the_snapshot_builders(fixture_store, monkeypatch):
    from agent_runtime import snapshot

    real_board = snapshot.board_summary_row
    real_card = snapshot._board_card_row

    def _tagged_board(board, cards, **kwargs):
        row = real_board(board, cards, **kwargs)
        row["title"] = "FROM-BUILDER"
        return row

    def _tagged_card(card, **kwargs):
        row = real_card(card, **kwargs)
        row["priority"] = "FROM-BUILDER"
        return row

    monkeypatch.setattr(snapshot, "board_summary_row", _tagged_board)
    monkeypatch.setattr(snapshot, "_board_card_row", _tagged_card)
    boards, board = fixture_store["boards"], fixture_store["board"]
    assert harness._board_row(boards, board)["title"] == "FROM-BUILDER"
    assert harness._card_row(fixture_store["card"])["priority"] == "FROM-BUILDER"


def test_office_rows_delegate_to_the_snapshot_builders(fixture_store, monkeypatch):
    from agent_runtime import snapshot

    real_surface = snapshot.office_summary_row
    real_actor = snapshot._office_actor_summary_row

    def _tagged_surface(surface, actors, **kwargs):
        row = real_surface(surface, actors, **kwargs)
        row["folders"] = ["FROM-BUILDER"]
        return row

    def _tagged_actor(actor, **kwargs):
        row = real_actor(actor, **kwargs)
        row["persona_id"] = "FROM-BUILDER"
        return row

    monkeypatch.setattr(snapshot, "office_summary_row", _tagged_surface)
    monkeypatch.setattr(snapshot, "_office_actor_summary_row", _tagged_actor)
    office, workspace = fixture_store["office"], fixture_store["workspace"]
    assert harness._office_surface_row(office, workspace.id)["folders"] == ["FROM-BUILDER"]
    assert harness._office_actor_row(fixture_store["actor"])["persona_id"] == "FROM-BUILDER"
