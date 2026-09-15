"""MCF-44: the round-robin cursor is a typed field, not the order of the rows.

Rotating used to mean renumbering every credential entry's ``priority`` and
re-saving the whole credential store, so advancing one integer performed a
full-store credential write. Three things followed from that, and each gate
below pins one of them:

1. a rotation wrote credential rows (gate 1: ``auth.json`` must not move);
2. the cursor had no name and no record of its own (gate 2: it lands in a typed
   sidecar record, carrying ids and never token material);
3. ``priority`` meant two things at once — operator intent AND rotation
   position — so for ``anthropic``, whose seeded rows are re-sorted BY SOURCE
   on every ``load_pool`` (``_normalize_pool_priorities``), the two meanings
   fought and normalization silently erased the rotation (gates 3 and 4).

Gate 5 pins the one new failure mode the cursor introduces: a cursor naming an
id that has left the pool.

CREDENTIAL HYGIENE: every token in this module is an inert marker string under
pytest's per-test ``HERMES_HOME`` tempdir. Nothing here is, or resembles, real
credential material, and gate 2 asserts that the rotation sidecar never
contains one.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest


PROVIDER = "deepseek"
#: Inert markers. Non-empty is the only property the pool cares about.
MARKER_A = "inert-marker-alpha-not-a-credential"
MARKER_B = "inert-marker-bravo-not-a-credential"


def _entry(idx: int, marker: str, *, source: str = "manual") -> dict:
    return {
        "id": f"cred-{idx}",
        "label": f"slot-{idx}",
        "auth_type": "api_key",
        "priority": idx,
        "source": source,
        "access_token": marker,
    }


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A sandboxed Hermes home with host credential discovery held off.

    The seeders and the stale-source pruner reach the real machine (Claude Code
    credentials, environment variables, ...). Neutralizing them keeps these
    gates measuring rotation rather than whatever this host happens to have
    authenticated.
    """
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(
        "agent.credential_pool._seed_from_singletons",
        lambda provider, entries: (False, set()),
    )
    monkeypatch.setattr(
        "agent.credential_pool._seed_from_env",
        lambda provider, entries: (False, set()),
    )
    monkeypatch.setattr(
        "agent.credential_pool._prune_stale_seeded_entries",
        lambda *args, **kwargs: False,
    )
    # HERMES_HOME does not sandbox ~/.claude/.credentials.json, and the
    # anthropic sync path in ``_available_entries`` reads it directly (the
    # non-hermetic class already catalogued in tests/agent/conftest.py). Left
    # live, gate 4 would sync REAL host tokens into a temp pool and then fail
    # or pass on whatever that host happens to hold.
    #
    # As of MCF-66 that is no longer this fixture's job to remember: the
    # autouse ``_neutralize_claude_code_credentials_file`` in tests/conftest.py
    # holds the seam for the whole suite, and
    # tests/test_claude_code_credentials_file_gate.py gates it. This stub is
    # now redundant belt-and-braces, kept because it also documents WHY this
    # particular pool must be quiescent.
    monkeypatch.setattr(
        "agent.anthropic_adapter.read_claude_code_credentials",
        lambda *args, **kwargs: None,
    )
    return hermes_home


def _seed(home: Path, monkeypatch, entries, *, provider: str = PROVIDER) -> Path:
    """A quiescent round-robin store: nothing but a rotation can move it."""
    monkeypatch.setattr(
        "agent.credential_pool.get_pool_strategy",
        lambda _provider: "round_robin",
    )
    auth_path = home / "auth.json"
    auth_path.write_text(
        json.dumps({"version": 1, "credential_pool": {provider: entries}}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return auth_path


def _rotation_path(home: Path) -> Path:
    return home / "credential_rotation.json"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _selected_id(entry) -> str:
    """Reduce a selection to its id BEFORE it can reach an assertion.

    ``PooledCredential``'s repr carries its token fields, so no assertion in
    this module ever compares entries.
    """
    assert entry is not None, "the pool returned no credential at all"
    return str(entry.id)


def _persisted_priorities(provider: str = PROVIDER) -> dict:
    from hermes_cli.auth import read_credential_pool

    return {
        row["id"]: row["priority"]
        for row in read_credential_pool(provider)
        if isinstance(row, dict)
    }


# -- Gate 1 ------------------------------------------------------------------


def test_a_persisted_rotation_writes_no_credential_row(home, monkeypatch):
    """Advancing the cursor leaves the credential store byte-identical.

    Killing mutation: restore the priority-rewriting round-robin branch in
    ``_select_unlocked`` (rebuild ``self._entries`` with new priorities and
    call ``self._persist()``) — the digest moves on the very first select.
    """
    from agent.credential_pool import load_pool

    auth_path = _seed(home, monkeypatch, [_entry(0, MARKER_A), _entry(1, MARKER_B)])
    load_pool(PROVIDER)  # let any load-time normalization settle first
    before = _digest(auth_path)

    first = _selected_id(load_pool(PROVIDER).select())
    second = _selected_id(load_pool(PROVIDER).select())

    assert _digest(auth_path) == before, (
        "a round-robin rotation rewrote the credential store: the cursor is "
        "still encoded as the order of the rows, so advancing one integer "
        "performs a full-store credential write"
    )
    # Anti-vacuity: "the store did not move" is trivially true for a pool that
    # never rotated at all.
    assert first != second, (
        "two fresh loads handed back the same credential — the cursor is not "
        "reaching disk, so this gate proves nothing"
    )


# -- Gate 2 ------------------------------------------------------------------


def test_the_cursor_lands_in_its_own_typed_record(home, monkeypatch):
    """The position is a NAMED field in a sidecar, holding ids and nothing else.

    Killing mutation: drop the ``write_pool_rotation_cursor`` call from the
    round-robin branch — the sidecar never appears.
    """
    from agent.credential_pool import load_pool

    _seed(home, monkeypatch, [_entry(0, MARKER_A), _entry(1, MARKER_B)])
    selected = _selected_id(load_pool(PROVIDER).select())

    sidecar = _rotation_path(home)
    assert sidecar.exists(), (
        "no rotation sidecar was written: the cursor still has no record of "
        "its own"
    )
    raw = sidecar.read_text(encoding="utf-8")
    assert json.loads(raw)["providers"][PROVIDER]["last_selected_id"] == selected, (
        "the sidecar does not name the entry that was actually selected"
    )
    # Hygiene, asserted rather than assumed: selection state is ids, never
    # tokens. Membership only — a failure here must not print the file.
    assert MARKER_A not in raw and MARKER_B not in raw, (
        "the rotation sidecar contains credential material; it may hold only "
        "ids the pool itself minted"
    )


# -- Gate 3 ------------------------------------------------------------------


def test_rotating_does_not_renumber_the_priority_list(home, monkeypatch):
    """``priority`` means operator intent, and rotation no longer touches it.

    Killing mutation: as gate 1 — the old branch appended the selected entry at
    ``len(entries) - 1`` and renumbered, so the two ids swap priorities.

    ONE selection, not two. Two rotations of a two-entry pool return the
    priority list to where it started, so the obvious "rotate a few times and
    compare" spelling of this gate is green against the very design it exists to
    refuse. It was written that way first and the killing mutation caught it.
    """
    from agent.credential_pool import load_pool

    _seed(home, monkeypatch, [_entry(0, MARKER_A), _entry(1, MARKER_B)])
    load_pool(PROVIDER)
    before = _persisted_priorities()

    load_pool(PROVIDER).select()

    assert _persisted_priorities() == before, (
        "rotation renumbered the credential rows: the cursor is still a side "
        f"effect of the priority list (was {before})"
    )


# -- Gate 4 ------------------------------------------------------------------


def test_rotation_survives_a_provider_whose_priorities_are_rederived(home, monkeypatch):
    """The load-time priority normalizer can no longer erase a rotation.

    ``_normalize_pool_priorities`` re-derives ``anthropic``'s priorities from
    SOURCE on every ``load_pool`` — manual rows first, then seeded rows by
    source rank. While the cursor lived in that same order, the normalizer
    overwrote it on the very next load and round-robin degraded to "always the
    first source": a defect invisible to every other gate here, because it only
    appears for the one provider that normalizes.

    Killing mutation: as gate 1 — with the priority-encoded cursor restored,
    both selections return the ``manual`` row.
    """
    from agent.credential_pool import load_pool

    # ``manual`` and ``anthropic``/``hermes_pkce`` are the two anthropic
    # sources whose tokens survive the disk boundary; every other seeded source
    # is borrowed and is fingerprinted away on write, which would empty the
    # pool on the second load for reasons unrelated to rotation.
    _seed(
        home,
        monkeypatch,
        [
            _entry(0, MARKER_A, source="manual"),
            _entry(1, MARKER_B, source="hermes_pkce"),
        ],
        provider="anthropic",
    )

    first = _selected_id(load_pool("anthropic").select())
    second = _selected_id(load_pool("anthropic").select())

    assert first != second, (
        "the load-time priority normalizer erased the rotation: for anthropic "
        "the cursor and the operator's priority order were the same field, and "
        "the normalizer owns that field"
    )


# -- Gate 5 ------------------------------------------------------------------


def test_a_cursor_naming_a_departed_entry_restarts_at_the_top(home, monkeypatch):
    """A stale cursor must not crash, and must not pin the first entry either.

    Killing mutation: drop the ``if last in order`` guard in
    ``_next_after_cursor`` (``order.index(last) + 1`` unconditionally) — the
    lookup raises ``ValueError`` out of a credential selection.
    """
    from agent.credential_pool import load_pool
    from hermes_cli.auth import write_pool_rotation_state

    _seed(home, monkeypatch, [_entry(0, MARKER_A), _entry(1, MARKER_B)])
    write_pool_rotation_state(PROVIDER, {"last_selected_id": "cred-removed"})

    first = _selected_id(load_pool(PROVIDER).select())
    second = _selected_id(load_pool(PROVIDER).select())

    assert (first, second) == ("cred-0", "cred-1"), (
        "a cursor pointing at an entry that has left the pool did not restart "
        f"cleanly at the top (got {first!r} then {second!r})"
    )
