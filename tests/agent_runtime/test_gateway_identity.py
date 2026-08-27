"""Stage 0 of the remote gateway: the per-root install identity.

The claims worth pinning are the ones a later stage will lean on and cannot
re-derive: the id is minted ONCE and never moves under a device that paired
against it, the record is per store root rather than per home, a rename keeps
the id, and every failure is a typed state on a frame block rather than an
exception on the boot path.

The root is an INPUT to every function here, so these tests need no environment
isolation — they hand it a ``tmp_path`` and read what landed on disk.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from agent_runtime.gateway_identity import (
    DISPLAY_NAME_MAX_CHARS,
    ensure_install_identity,
    install_record_path,
    read_install_identity,
    set_display_name,
)


# ── mint, then never again ──────────────────────────────────────────────────


def test_a_fresh_root_mints_a_record_and_says_it_minted(tmp_path: Path):
    identity = ensure_install_identity(tmp_path)

    assert identity.state == "minted"
    assert identity.ok is True
    # A uuid4, not "some string": Stage 1's pairing payload carries this value
    # across a network and Stage 7 qualifies chat targets with it.
    assert uuid.UUID(identity.install_id).version == 4
    assert identity.display_name
    assert identity.created_at

    record = json.loads(install_record_path(tmp_path).read_bytes().decode("utf-8"))
    assert record["install_id"] == identity.install_id
    assert record["display_name"] == identity.display_name
    assert set(record) == {"install_id", "display_name", "created_at"}


def test_the_record_lives_under_gateway_in_the_store_root(tmp_path: Path):
    """Per STORE ROOT, which is the scope the two existing ``install_id``
    mechanisms do not have: theirs are a HERMES home's config.yaml and the
    shared-metrics sqlite, and one launcher-spawned serve can share a home while
    addressing a different root."""

    ensure_install_identity(tmp_path)

    assert install_record_path(tmp_path) == tmp_path / "gateway" / "install.json"
    assert (tmp_path / "gateway" / "install.json").is_file()


def test_reloading_is_idempotent_and_reports_loaded(tmp_path: Path):
    first = ensure_install_identity(tmp_path)
    before = install_record_path(tmp_path).read_bytes()

    second = ensure_install_identity(tmp_path)
    third = ensure_install_identity(tmp_path)

    assert second.state == "loaded" and third.state == "loaded"
    assert second.install_id == first.install_id == third.install_id
    # Not merely equal values — the file is not rewritten at all. A record that
    # is re-rendered on every boot is a record whose mtime lies about when the
    # install was named.
    assert install_record_path(tmp_path).read_bytes() == before


def test_two_roots_on_one_machine_are_two_installs(tmp_path: Path):
    """The whole reason the root is an input: QA lanes and worktree roots
    coexist, and a picker that showed them as one install would send a turn to
    the wrong runtime."""

    a = ensure_install_identity(tmp_path / "root-a")
    b = ensure_install_identity(tmp_path / "root-b")

    assert a.install_id != b.install_id


# ── read without minting ────────────────────────────────────────────────────


def test_reading_an_unnamed_root_reports_absent_and_writes_nothing(tmp_path: Path):
    identity = read_install_identity(tmp_path)

    assert identity.state == "error:absent"
    assert identity.ok is False
    assert identity.install_id is None
    assert not (tmp_path / "gateway").exists(), (
        "a read must not have a side effect on a root it was only asked about"
    )


# ── rename ──────────────────────────────────────────────────────────────────


def test_a_rename_keeps_the_id_and_the_creation_stamp(tmp_path: Path):
    minted = ensure_install_identity(tmp_path)

    renamed = set_display_name(tmp_path, "  workshop  desktop ")

    assert renamed.ok is True
    assert renamed.install_id == minted.install_id
    assert renamed.created_at == minted.created_at
    # Whitespace collapsed, not preserved: this is a picker row.
    assert renamed.display_name == "workshop desktop"
    assert ensure_install_identity(tmp_path).display_name == "workshop desktop"


def test_a_rename_on_an_unnamed_root_mints_first(tmp_path: Path):
    renamed = set_display_name(tmp_path, "kitchen")

    assert renamed.ok is True
    assert renamed.display_name == "kitchen"
    assert uuid.UUID(renamed.install_id).version == 4


def test_an_empty_name_is_refused_rather_than_written(tmp_path: Path):
    minted = ensure_install_identity(tmp_path)

    refused = set_display_name(tmp_path, "   ")

    assert refused.state == "error:empty_display_name"
    assert ensure_install_identity(tmp_path).display_name == minted.display_name


def test_a_long_name_is_bounded_because_it_rides_every_greeting(tmp_path: Path):
    renamed = set_display_name(tmp_path, "n" * 500)

    assert len(renamed.display_name) == DISPLAY_NAME_MAX_CHARS


# ── the frame block ─────────────────────────────────────────────────────────


def test_the_frame_block_names_and_never_carries_the_creation_stamp(tmp_path: Path):
    payload = ensure_install_identity(tmp_path).frame_payload()

    assert set(payload) == {"install_id", "display_name", "state"}
    # Deliberately NOT `build`: the greeting frames already carry a top-level
    # build block, and a nested copy is a second authority that can disagree.
    assert "build" not in payload
    assert "created_at" not in payload


def test_an_unwritable_root_yields_a_typed_state_rather_than_an_exception(
    tmp_path: Path,
):
    """A runtime that cannot mint must still boot and SAY so — the same rule the
    ``auth`` block beside this one on ``ready`` follows."""

    blocked = tmp_path / "not-a-directory"
    blocked.write_bytes(b"")

    identity = ensure_install_identity(blocked)

    assert identity.ok is False
    assert identity.state.startswith("error:")
    payload = identity.frame_payload()
    assert payload["install_id"] is None
    assert payload["state"].startswith("error:")


# ── the two ways a record on disk can be wrong ──────────────────────────────


def test_an_empty_record_is_healed_because_nobody_can_hold_a_zero_byte_id(
    tmp_path: Path,
):
    """The kill-between-create-and-write shape. Leaving it would wedge the root
    forever: mint-iff-absent means every later boot takes the same branch."""

    path = install_record_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")

    identity = ensure_install_identity(tmp_path)

    assert identity.state == "minted"
    assert uuid.UUID(identity.install_id).version == 4


def test_a_corrupt_record_is_a_typed_error_and_is_never_overwritten(tmp_path: Path):
    """Asymmetric with the heal above, on purpose: a file with bytes in it may
    be a record whose id a paired device still names, and overwriting it to make
    a boot look tidy destroys the only copy of that join key."""

    path = install_record_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"install_id": "u1", "display_name": ')

    identity = ensure_install_identity(tmp_path)

    assert identity.state == "error:malformed_record"
    assert path.read_bytes() == b'{"install_id": "u1", "display_name": '


def test_a_record_that_lost_its_id_is_refused_not_re_minted(tmp_path: Path):
    path = install_record_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"display_name": "somebody named this"}\n')

    assert ensure_install_identity(tmp_path).state == "error:record_without_id"


def test_a_record_saved_with_crlf_still_parses(tmp_path: Path):
    """The repo's standing EOL rule: a record an operator edited by hand on
    Windows must not read as corrupt."""

    original = ensure_install_identity(tmp_path)
    path = install_record_path(tmp_path)
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))

    assert ensure_install_identity(tmp_path).install_id == original.install_id


def test_a_record_whose_name_was_emptied_still_presents_something(tmp_path: Path):
    path = install_record_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        json.dumps({"install_id": str(uuid.uuid4()), "display_name": ""}).encode("utf-8")
    )
    before = path.read_bytes()

    identity = ensure_install_identity(tmp_path)

    assert identity.ok is True
    assert identity.display_name
    # Derived for the caller, NOT written back: this is the read path.
    assert path.read_bytes() == before


def test_the_written_record_is_lf_canonical(tmp_path: Path):
    """Every record this runtime writes and reads back is LF, which is why this
    module uses ``serde.write_json_atomic`` rather than upstream's text-mode
    ``utils.atomic_json_write``."""

    set_display_name(tmp_path, "renamed")

    assert b"\r\n" not in install_record_path(tmp_path).read_bytes()


def test_no_temp_file_is_left_beside_the_record(tmp_path: Path):
    ensure_install_identity(tmp_path)
    set_display_name(tmp_path, "renamed")

    assert sorted(os.listdir(tmp_path / "gateway")) == ["install.json"]
