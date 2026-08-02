"""S48 retires the five hand-rolled CLI entity-row projections.

Ledger item 4 (`docs/agent-runtime-harness/19-deferred-debt-ledger.md`) was an
operator ruling: `hermes harness`'s entity rows adopt the snapshot-grade
builders even though that deliberately CHANGES CLI output (card prose gains the
wire's masking; card/actor lists gain the wire's bounds).

The defect class, stated once: a second projection of the same entity, written
by hand next to a builder that already existed. It shipped twice as a live
defect before this cut — `_workspace_row` read the undefined name `tasks`
(NameError on every `workspace show`) and `_realm_row` hardcoded
`"sync": "in_sync"`, the exact fake the snapshot's realm row forbids. Both were
patched narrowly in `a21ab1a2a`; per the recurrence rule, the shared root cause
is the bug, so this removes the ability to have two answers at all.

WHAT IS REMOVED (each verified against the current tree, not the audit's stale
line numbers — S46/S47 and `e887cdf26` reshaped these files):

========================================= ==================================
symbol / derivation                        why it is gone
========================================= ==================================
`office.py::_office_item_row`              re-declared, key for key, the
                                           scene-item block
                                           `_office_actor_summary_row`
                                           already projects
`harness.py` binding                       last reader was `_realm_row`'s
`read_realm_sync_sidecar`                  duplicate sidecar read; the
                                           builder owns it now
`harness.py` binding                       last reader was `_workspace_row`'s
`exact_scoped_instance_ids`                duplicate live-scope derivation
the six rows' hand-rolled field            replaced by a re-key of the
derivations (listed per function below)    builder's row
========================================= ==================================

WHAT IS KEPT, and pinned so a later sweep cannot mistake this for permission to
take more:

* `_board_active_card_count` / `_column_kind` — the CLI's `active_cards` column
  counts cards NOT in a `done` column; the snapshot row's `active_card_count`
  counts every non-archived card. Two different questions that happen to have
  near-identical names. Consolidating them would silently change a number an
  operator reads, which is worse than the duplication; recorded as a residual
  on ledger item 4 rather than swept here.
* Per-row CLI-only fields — `created_at` (workspace / realm / card / actor),
  `sync_manifest_ref` (realm), `workspace_id` + `state` (actor). All plain model
  scalars the wire never carried, none of them re-derivations of a builder
  computation, none of them secret-bearing. No unmask flag was invented.
* Timestamps stay `datetime` on the CLI rows: `emit_json` -> `to_jsonable` is
  the Stage-42 serialization authority for this lane, so `--output table`
  renders exactly as before.

The two new snapshot seams (`board_summary_row`, `office_summary_row`) are
extractions of `_boards_summary` / `_offices_summary` loop bodies, not new
lanes; the tests below assert the loops still route through them, so the
extraction cannot fork into the very thing it retired.

-----------------------------------------------------------------------------
MIGRATED TO ``test_tombstone_registry.py`` (2026-08-01)
-----------------------------------------------------------------------------

ONE case left this file: ``test_the_retired_name_is_gone_from_the_harness_scope``
and its ``REMOVED_HARNESS_NAMES`` tuple, now three ``ATTR`` rows scoped to
``hermes_cli.harness`` (``_office_item_row``, ``read_realm_sync_sidecar``,
``exact_scoped_instance_ids``). The scope is unchanged and the mechanism is the
same ``hasattr`` on the post-exec namespace — the S41 rule — so nothing moved
except the address.

WHY EVERYTHING ELSE STAYED, and this is the important half: the registry can
express "this name is absent from production code", but NOT "this name is absent
from the body of THIS function while present elsewhere". That is what every
surviving case here asserts, and the whole point of the S48 cut:

* ``RETIRED_DERIVATIONS`` — each fragment (``card.title``, ``realm.workspace_ids``
  …) is LIVE in the snapshot builder. A repo-wide row would fail against a
  correct tree. Function-scoped is the only honest form.
* ``REQUIRED_DELEGATIONS`` — a POSITIVE assertion (the row must CALL its
  builder). A removal registry has no positive form; without it a row that
  stopped projecting anything at all would pass the removal half.
* ``test_no_cli_row_carries_its_own_masking_or_truncation`` — scoped to six
  named row functions on purpose, because other harness tiers legitimately DO
  redact. Repo-wide it would be false.
* ``_code_without_prose`` + ``test_the_gate_itself_is_not_vacuous`` — the
  reference anti-vacuity proof this file contributed to the campaign. The
  registry's ``_code_only`` IS this mechanism; the pin stays where it was
  written, and the registry carries its own copy of the same proof.
* The CLI-only field/count characterizations (``test_the_cli_only_fields_are_the_declared_set``,
  ``test_the_cli_only_count_is_deliberately_kept``) are exact-set pins and
  documented KEEPs — never absence rows.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest


def _code_without_prose(source: str) -> str:
    """Source with docstrings and comments removed — everything else intact.

    Same reason as S46's helper: a removal gate that greps raw source cannot
    tell a re-grown derivation from the comment explaining why it went, and
    these functions' docstrings name the very symbols being gated.

    DIFFERENT MECHANISM from S46's, deliberately. S46 joined the surviving
    tokens with newlines, which is fine when every gated fragment is a single
    identifier — but it silently turns ``card.title`` into ``card\\n.\\ntitle``,
    so a dotted-attribute assertion can never match and passes vacuously. This
    gate needs dotted fragments, so it strips docstrings on the AST and
    re-renders with ``ast.unparse``: canonical code, no prose, real syntax.
    """

    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            if isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


def _row_source(name: str) -> str:
    """Code-only source of one row projection, resolved through the post-load
    harness namespace — the parts are `exec`'d into it, so this is the only
    place all six are visible at once."""

    import hermes_cli.harness as harness

    return _code_without_prose(inspect.getsource(getattr(harness, name)))


def test_the_gate_itself_is_not_vacuous():
    """The helper must render dotted attributes as dotted attributes.

    Pinned because the obvious implementation (S46's token join) does not, and
    a gate that cannot match its own fragments reports success forever.
    """

    rendered = _code_without_prose("def f(card):\n    '''doc'''\n    # comment\n    return card.title\n")
    assert "card.title" in rendered
    assert "doc" not in rendered
    assert "comment" not in rendered


#: row projection -> the hand-rolled derivations that must no longer appear in
#: its body. Each is a field the builder now supplies.
RETIRED_DERIVATIONS = {
    "_workspace_row": (
        "exact_scoped_instance_ids",
        "workspace.agent_ids",
        "workspace.isolation",
        "workspace.realm_id",
        "workspace.name",
        "workspace.slug",
        "workspace.archived",
    ),
    "_realm_row": (
        "read_realm_sync_sidecar",
        "in_sync",
        "realm.workspace_ids",
        "realm.slug",
        "realm.name",
        "realm.server_id",
        "realm.archived",
    ),
    "_board_row": ("board.columns", "board.archived_card_ids", "board.title", "board.workspace_id", "board.revision"),
    "_card_row": (
        "card.title",
        "card.description",
        "card.labels",
        "card.checklist",
        "card.assignee",
        "card.priority",
        "card.state",
        "card.order_key",
        "card.column_id",
        "card.revision",
    ),
    "_office_actor_row": (
        "actor.items",
        "actor.backing_profile",
        "actor.persona_id",
        "actor.persona_instance_id",
        "actor.revision",
        "actor.updated_by",
        "_office_item_row",
    ),
    "_office_surface_row": (
        "surface.folders",
        "surface.archived_actor_keys",
        "surface.revision",
        "surface.workspace_id",
    ),
}

#: row projection -> the builder it must call. A removal gate alone would pass
#: against a row that stopped projecting anything at all.
REQUIRED_DELEGATIONS = {
    "_workspace_row": "_workspace_summary",
    "_realm_row": "_realm_summary",
    "_board_row": "board_summary_row",
    "_card_row": "_board_card_row",
    "_office_actor_row": "_office_actor_summary_row",
    "_office_surface_row": "office_summary_row",
}


@pytest.mark.parametrize("name,retired", sorted((k, v) for k, v in RETIRED_DERIVATIONS.items()))
def test_the_row_no_longer_hand_rolls_its_fields(name: str, retired: tuple[str, ...]):
    source = _row_source(name)
    for fragment in retired:
        assert fragment not in source, f"{name} still hand-rolls {fragment!r}"


@pytest.mark.parametrize("name,builder", sorted(REQUIRED_DELEGATIONS.items()))
def test_the_row_delegates_to_its_builder(name: str, builder: str):
    assert builder in _row_source(name)


def test_no_cli_row_carries_its_own_masking_or_truncation():
    """One authority. The CLI may re-key a builder row; it may not re-implement
    the protections on it. (Other harness tiers DO redact — persona chat text,
    runtime output summaries — so this gate is scoped to the six rows, not to
    the whole harness scope.)"""

    forbidden = (
        "redacted",
        "_mask_",
        "SECRET",
        "MAX_BOARD_CARDS_PROJECTED",
        "BOARD_CARD_DESC_LIMIT",
        "MAX_OFFICE_ACTORS_PROJECTED",
        "truncated =",
    )
    for name in REQUIRED_DELEGATIONS:
        source = _row_source(name)
        for fragment in forbidden:
            assert fragment not in source, f"{name} re-implements {fragment!r}"


def test_the_new_snapshot_seams_are_extractions_not_a_second_lane():
    """`board_summary_row` / `office_summary_row` were lifted out of the two
    `*_summary` loops so the CLI could share them. If a loop stopped calling its
    seam, the snapshot would be back to its own copy — the exact shape this
    stage retired, one level up."""

    from agent_runtime import snapshot

    boards = _code_without_prose(inspect.getsource(snapshot._boards_summary))
    offices = _code_without_prose(inspect.getsource(snapshot._offices_summary))
    assert "board_summary_row" in boards
    assert "office_summary_row" in offices
    # ...and the loops no longer hold a second copy of the bound.
    assert "MAX_BOARD_CARDS_PROJECTED" not in boards
    assert "MAX_OFFICE_ACTORS_PROJECTED" not in offices


def test_the_projection_bounds_live_in_exactly_one_place():
    from agent_runtime import snapshot

    board_row = _code_without_prose(inspect.getsource(snapshot.board_summary_row))
    office_row = _code_without_prose(inspect.getsource(snapshot.office_summary_row))
    assert "MAX_BOARD_CARDS_PROJECTED" in board_row
    assert "cards_truncated" in board_row
    assert "MAX_OFFICE_ACTORS_PROJECTED" in office_row
    assert "actors_truncated" in office_row


def test_the_cli_only_count_is_deliberately_kept():
    """`active_cards` answers a different question from the wire's
    `active_card_count`. Pinned KEPT so the next sweep reads the reason instead
    of re-deriving it (and, worse, "fixing" it)."""

    import hermes_cli.harness as harness

    assert callable(harness._board_active_card_count)
    assert callable(harness._column_kind)
    source = _code_without_prose(inspect.getsource(harness._board_active_card_count))
    assert "done" in source
    # The KEY, not the substring: ``_board_active_card_count`` legitimately
    # contains ``active_card_count`` in its own name, so mask the helper's
    # name out before looking for the builder's key.
    board_row = _row_source("_board_row").replace("_board_active_card_count", "")
    assert "active_card_count" not in board_row


def test_the_cli_only_fields_are_the_declared_set():
    """Every field a CLI row supplies from the MODEL rather than the builder,
    named here. A new one appearing without a ledger entry is how the two lanes
    drift apart again."""

    expected = {
        # CLI-only VALUES (the wire never carried them) plus, where marked,
        # IDENTITY reads used purely for routing — pairing a projected row back
        # to its model object, or naming the store read. Routing reads project
        # nothing, so they cannot drift from the builder.
        "_workspace_row": {"workspace.updated_at", "workspace.created_at"},
        "_realm_row": {"realm.updated_at", "realm.created_at", "realm.sync_manifest_ref"},
        "_board_row": {
            "board.updated_at",
            "board.board_id",  # routing: which board's cards to read
            "card.card_id",  # routing: re-pair each projected card row
        },
        "_card_row": {"card.updated_at", "card.created_at"},
        "_office_actor_row": {"actor.updated_at", "actor.created_at", "actor.workspace_id", "actor.state"},
        "_office_surface_row": {
            "surface.updated_at",
            "actor.actor_key",  # routing: re-pair each projected actor row
        },
    }
    import hermes_cli.harness as harness

    for name, fields in expected.items():
        # Parsed from the REAL source, not the token-joined prose-stripped
        # form: docstrings and comments produce no ``Attribute`` nodes, so
        # there is nothing to strip here.
        tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(harness, name))))
        found = {
            f"{node.value.id}.{node.attr}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        }
        # Only model reads count; store/handle calls (``store.list_cards``,
        # ``summary.get``) are not entity FIELDS.
        model_reads = {item for item in found if item.split(".")[0] in {"workspace", "realm", "board", "card", "actor", "surface"}}
        assert model_reads == fields, f"{name} model reads drifted: {sorted(model_reads)}"
