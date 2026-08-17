"""Behaviour of the generic image revision store.

The store is the piece the plan promises is reusable outside charsheets, so
everything here is asserted through its public verbs against a real directory:
propose/approve/reject semantics, what a second instance over the same root can
see, and what it does with the debris an interrupted write can leave behind.
"""

from __future__ import annotations

import json
import os

import pytest

from agent.charsheet.revisions import STATE_FILENAME, ImageRevisionStore


@pytest.fixture(autouse=True)
def _hermes_home(tmp_path, monkeypatch):
    """The store is rooted explicitly, but never let a bug reach the real home."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))


@pytest.fixture
def root(tmp_path):
    return tmp_path / "revisions"


@pytest.fixture
def store(root):
    return ImageRevisionStore(root)


@pytest.fixture
def images(tmp_path):
    """A factory for distinguishable payload files (never decoded by the store)."""
    made = {}

    def make(name: str, payload: bytes | None = None):
        path = tmp_path / "src" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload if payload is not None else f"bytes-of-{name}".encode())
        made[name] = path
        return path

    return make


# ───────────────────────────── propose / approve ─────────────────────────────


def test_propose_appends_attempts_in_order_and_records_the_note(store, images):
    assert store.propose("row@walk@e", images("a.png"), note="first") == 0
    assert store.propose("row@walk@e", images("b.png"), note="second") == 1

    history = store.history("row@walk@e")
    assert [record["note"] for record in history] == ["first", "second"]
    assert all(record["created"] for record in history)
    assert store.keys() == ["row@walk@e"]


def test_an_unknown_key_has_no_history_no_current_and_no_latest(store):
    assert store.history("turnaround@n") == []
    assert store.current("turnaround@n") is None
    assert store.latest("turnaround@n") is None
    assert store.approved_index("turnaround@n") is None
    assert store.keys() == []
    assert store.pending() == []


def test_approve_marks_the_requested_attempt_and_current_points_at_its_bytes(store, images):
    store.propose("k", images("first.png"))
    store.propose("k", images("second.png"))

    assert store.approve("k", 0) == 0
    assert store.approved_index("k") == 0
    assert store.current("k").read_bytes() == images("first.png").read_bytes()

    assert store.approve("k") == 1  # -1 == latest
    assert store.current("k").read_bytes() == images("second.png").read_bytes()


def test_a_new_proposal_clears_the_existing_approval(store, images):
    store.propose("k", images("a.png"))
    store.approve("k")
    assert store.current("k") is not None

    store.propose("k", images("b.png"))

    assert store.approved_index("k") is None
    assert store.current("k") is None
    assert store.pending() == ["k"]


def test_pending_lists_only_items_that_have_an_attempt_and_no_approval(store, images):
    store.propose("b-key", images("b.png"))
    store.propose("a-key", images("a.png"))
    store.approve("a-key")
    store.propose("c-key", images("c.png"))

    assert store.pending() == ["b-key", "c-key"]
    assert store.keys() == ["a-key", "b-key", "c-key"]


def test_latest_shows_a_pending_attempt_that_current_deliberately_hides(store, images):
    store.propose("k", images("a.png"))
    store.approve("k")
    store.propose("k", images("b.png"))

    assert store.current("k") is None
    assert store.latest("k").read_bytes() == images("b.png").read_bytes()


def test_the_stored_image_is_a_byte_for_byte_copy_of_the_source(store, images):
    payload = bytes(range(256)) * 8 + b"\x00\xff\x89PNG"
    source = images("weird.bin", payload)

    store.propose("k", source)
    store.approve("k")
    copied = store.current("k")

    assert copied.read_bytes() == payload
    assert copied != source
    assert source.read_bytes() == payload  # the source is untouched


def test_the_attempt_filename_keeps_a_plausible_suffix(store, images):
    store.propose("k", images("a.webp"))
    store.propose("k", images("b.unreasonably-long-suffix"))

    assert store.history("k")[0]["file"].endswith(".webp")
    assert store.history("k")[1]["file"].endswith(".png")


def test_history_records_are_copies_the_caller_cannot_write_back_through(store, images):
    store.propose("k", images("a.png"), note="keep")
    store.history("k")[0]["note"] = "tampered"

    assert store.history("k")[0]["note"] == "keep"


# ───────────────────────────────── reject ─────────────────────────────────


def test_a_rejected_attempt_can_never_be_approved(store, images):
    store.propose("k", images("a.png"))
    store.reject("k", 0)

    with pytest.raises(ValueError, match="rejected and cannot be approved"):
        store.approve("k", 0)
    with pytest.raises(ValueError, match="rejected and cannot be approved"):
        store.approve("k")  # latest is the rejected one
    assert store.current("k") is None


def test_rejecting_keeps_the_history_and_only_flags_the_attempt(store, images):
    store.propose("k", images("a.png"))
    store.propose("k", images("b.png"))
    store.reject("k", 0)

    history = store.history("k")
    assert len(history) == 2
    assert history[0]["rejected"] is True
    assert "rejected" not in history[1]
    assert store.approve("k", 1) == 1


def test_rejecting_the_approved_attempt_withdraws_the_approval(store, images):
    store.propose("k", images("a.png"))
    store.approve("k", 0)

    store.reject("k", 0)

    assert store.approved_index("k") is None
    assert store.current("k") is None
    assert store.pending() == ["k"]


# ───────────────────────────── argument errors ─────────────────────────────


def test_approving_or_rejecting_an_item_with_no_attempts_is_a_value_error(store):
    with pytest.raises(ValueError, match="no attempts have been proposed"):
        store.approve("k")
    with pytest.raises(ValueError, match="no attempts have been proposed"):
        store.reject("k", 0)


@pytest.mark.parametrize("attempt", [1, 5, -2])
def test_an_out_of_range_attempt_is_an_index_error(store, images, attempt):
    store.propose("k", images("a.png"))

    with pytest.raises(IndexError, match="out of range"):
        store.approve("k", attempt)


@pytest.mark.parametrize("attempt", [True, "0", 1.0, None])
def test_a_non_integer_attempt_is_refused(store, images, attempt):
    store.propose("k", images("a.png"))

    with pytest.raises(ValueError, match="attempt must be an int"):
        store.approve("k", attempt)


def test_proposing_a_file_that_does_not_exist_is_refused(store, tmp_path):
    with pytest.raises(ValueError, match="is not an existing file"):
        store.propose("k", tmp_path / "nope.png")
    with pytest.raises(ValueError, match="is not an existing file"):
        store.propose("k", tmp_path)  # a directory is not an image either


# ─────────────────────────────── key hygiene ───────────────────────────────


@pytest.mark.parametrize(
    "key",
    ["", "Upper", "has space", "a/b", "a\\b", "..", "dot.png", "unicode-é", "a:b", "a*b"],
)
def test_keys_outside_the_directory_safe_alphabet_are_refused(store, key, images):
    with pytest.raises(ValueError, match="invalid item key"):
        ImageRevisionStore.validate_key(key)
    with pytest.raises(ValueError, match="invalid item key"):
        store.propose(key, images("a.png"))


@pytest.mark.parametrize("key", ["con", "nul", "com1", "lpt9", "aux", "prn"])
def test_windows_reserved_device_names_are_refused(store, key, images):
    with pytest.raises(ValueError, match="reserved device name"):
        ImageRevisionStore.validate_key(key)
    with pytest.raises(ValueError, match="reserved device name"):
        store.propose(key, images("a.png"))


def test_a_refused_key_creates_nothing_on_disk(store, root, images):
    with pytest.raises(ValueError):
        store.propose("Bad Key", images("a.png"))

    assert list(root.iterdir()) == []


@pytest.mark.parametrize("key", ["turnaround@ne", "row@walk@e", "a-b_c", "x9"])
def test_the_charsheet_key_shapes_are_accepted(store, key, images):
    assert ImageRevisionStore.validate_key(key) == key
    store.propose(key, images("a.png"))
    assert store.keys() == [key]


# ──────────────────────────── disk is the truth ────────────────────────────


def test_two_store_instances_over_one_root_always_agree(root, images):
    writer = ImageRevisionStore(root)
    reader = ImageRevisionStore(root)

    writer.propose("k", images("a.png"))
    writer.approve("k")
    assert reader.approved_index("k") == 0
    assert reader.current("k") == writer.current("k")
    assert reader.keys() == ["k"]

    reader.propose("k", images("b.png"))
    assert writer.approved_index("k") is None
    assert writer.pending() == ["k"]

    writer.approve("k")
    assert reader.current("k").read_bytes() == images("b.png").read_bytes()


def test_a_reopened_store_continues_the_attempt_numbering(root, images):
    ImageRevisionStore(root).propose("k", images("a.png"))

    assert ImageRevisionStore(root).propose("k", images("b.png")) == 1
    assert len(ImageRevisionStore(root).history("k")) == 2


def test_leftover_temporary_files_are_ignored_and_the_slot_is_reused(store, root, images):
    store.propose("k", images("a.png"))
    item = root / "k"
    (item / f"{STATE_FILENAME}.crash.tmp").write_text("{partial", encoding="utf-8")
    (item / "attempt-2.png.crash.tmp").write_bytes(b"half-written")

    assert store.history("k") == store.history("k")
    assert store.keys() == ["k"]
    assert store.propose("k", images("b.png")) == 1
    assert store.approve("k") == 1
    assert store.current("k").read_bytes() == images("b.png").read_bytes()
    assert store.current("k").name == "attempt-2.png"


def test_unrelated_entries_in_the_root_are_not_items(store, root, images):
    store.propose("k", images("a.png"))
    (root / "not-an-item").mkdir()
    (root / "UPPER").mkdir()
    (root / "loose-file.png").write_bytes(b"x")

    assert store.keys() == ["k"]
    assert store.pending() == ["k"]


def test_a_state_file_that_is_not_json_is_reported_with_its_path(store, root, images):
    store.propose("k", images("a.png"))
    (root / "k" / STATE_FILENAME).write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError, match="corrupt state file"):
        store.history("k")


def test_a_state_file_that_is_not_an_object_is_refused(store, root, images):
    store.propose("k", images("a.png"))
    (root / "k" / STATE_FILENAME).write_text("[1, 2]", encoding="utf-8")

    with pytest.raises(ValueError, match="expected a JSON object"):
        store.history("k")


@pytest.mark.parametrize("approved", [7, -1, "0", True, None])
def test_an_approval_index_the_history_cannot_support_reads_as_unapproved(store, root, images, approved):
    store.propose("k", images("a.png"))
    state_path = root / "k" / STATE_FILENAME
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["approved"] = approved
    state_path.write_text(json.dumps(state), encoding="utf-8")

    assert store.approved_index("k") is None
    assert store.current("k") is None
    assert store.latest("k") is not None


def test_approving_an_attempt_whose_image_vanished_is_refused(store, root, images):
    store.propose("k", images("a.png"))
    os.remove(root / "k" / "attempt-1.png")

    with pytest.raises(ValueError, match="no image on disk"):
        store.approve("k")


def test_the_root_is_created_on_construction(tmp_path):
    store = ImageRevisionStore(tmp_path / "deep" / "nested" / "revisions")

    assert store.root.is_dir()
    assert store.keys() == []
