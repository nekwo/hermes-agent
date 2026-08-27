"""``harness gateway id`` / ``harness gateway rename`` — the operator's door onto
the per-root remote-gateway install identity (gateway plan Stage 0b).

Every test drives the REAL argparse tree and dispatches through ``args.func``,
the same rule the agent create/retire verb suites carry: a handler nothing routes
to is a verb no operator can run, and registration is precisely the half Stage 0a
deferred.

Two claims carry the suite, and neither can be read off the handler:

* **The read never mints.** ``gateway id`` against a root that has never served
  reports that it has no identity and leaves the root untouched — asserted on
  DISK, not on the ack. Stage 4's install picker runs this against roots it does
  not own, and a probe that mints is a side effect on a root somebody only asked
  about.
* **The rename never moves the id.** A device pairs against ``install_id``
  (Stage 1); a rename that rotated it would be a lockout wearing a cosmetic
  verb's name.

The service half (``agent_runtime/gateway_identity.py``) is tested at
``tests/agent_runtime/test_gateway_identity.py``. What is tested HERE is the
part that suite cannot see: that argparse routes to it, that the envelope says
which root answered, and that a typed ``error:<reason>`` becomes the right exit
family instead of a traceback.
"""

from __future__ import annotations

import argparse
import json

import pytest

from agent_runtime import paths


@pytest.fixture(autouse=True)
def hermetic_runtime_root(tmp_path, monkeypatch):
    """Pin the runtime root INSIDE this test's tmp dir, and prove it landed.

    These tests WRITE ``gateway/install.json``. A resolution regression would
    rename the operator's own install — the id a paired device names.
    """

    root = tmp_path / "agent-runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    resolved = paths.store_root().resolve()
    assert resolved == root.resolve() or root.resolve() in resolved.parents, (
        f"store_root() resolved to {resolved}, OUTSIDE {root}: this test would "
        "write into a runtime root nobody in this repo controls."
    )
    return root


def _dispatch(argv: list[str]) -> int:
    from hermes_cli import harness

    root = argparse.ArgumentParser(prog="hermes")
    harness.build_parser(root.add_subparsers(dest="command"))
    args = root.parse_args(argv)
    return args.func(args)


def _run(capsys, *argv: str) -> tuple[int, dict]:
    code = _dispatch(["harness", "gateway", *argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def _record_path():
    from agent_runtime.gateway_identity import install_record_path

    return install_record_path(paths.store_root())


# ── the read half ────────────────────────────────────────────────────────────


def test_id_on_a_root_that_has_never_served_reports_absence_and_mints_nothing(capsys):
    """ANTI-VACUITY: the probe is the FILESYSTEM, not the ack.

    The kill-mutation is routing ``gateway id`` at ``ensure_install_identity``
    — which would return a perfectly plausible ack, exit 0, and have silently
    created an identity on a root the operator only asked about.
    """

    code, data = _run(capsys, "id")

    assert code == 3
    assert data["error"]["code"] == "not_found"
    assert not _record_path().exists()
    assert not _record_path().parent.exists()


def test_id_reads_back_the_whole_record_and_says_which_root_answered(capsys):
    named_code, named = _run(capsys, "rename", "workshop")
    assert named_code == 0

    code, data = _run(capsys, "id")

    assert code == 0
    assert data["kind"] == "gateway_install"
    assert data["install_id"] == named["install_id"]
    assert data["display_name"] == "workshop"
    assert data["state"] == "loaded"
    assert data["created_at"] == named["created_at"]
    assert data["path"] == str(_record_path())
    # The identity is per STORE ROOT, so the root is half the answer: a `gateway
    # id` run against the wrong root returns a well-formed identity for a
    # runtime the operator did not mean (2026-08-12 incident shape, with an id
    # in it).
    assert data["resolution"]["store_root"] == str(paths.store_root())


# ── the write half ───────────────────────────────────────────────────────────


def test_rename_on_a_fresh_root_mints_and_says_that_is_what_it_did(capsys):
    code, data = _run(capsys, "rename", "workshop")

    assert code == 0
    assert data["state"] == "minted"
    assert data["display_name"] == "workshop"
    assert data["install_id"]
    record = json.loads(_record_path().read_bytes().decode("utf-8"))
    assert record["display_name"] == "workshop"
    assert record["install_id"] == data["install_id"]


def test_a_second_rename_keeps_the_id_a_paired_device_would_name(capsys):
    _, first = _run(capsys, "rename", "workshop")

    code, second = _run(capsys, "rename", "kitchen")

    assert code == 0
    assert second["install_id"] == first["install_id"]
    assert second["created_at"] == first["created_at"]
    assert second["display_name"] == "kitchen"
    assert second["state"] == "loaded"


def test_rename_normalises_the_name_the_way_every_greeting_frame_needs(capsys):
    """Whitespace collapsed and length bounded — the name rides ``ready`` /
    ``hello_ok`` / ``version`` on every boot, so a pasted paragraph would bloat
    every greeting on the wire."""

    from agent_runtime.gateway_identity import DISPLAY_NAME_MAX_CHARS

    _, collapsed = _run(capsys, "rename", "  workshop   desktop ")
    assert collapsed["display_name"] == "workshop desktop"

    _, bounded = _run(capsys, "rename", "n" * 500)
    assert bounded["display_name"] == "n" * DISPLAY_NAME_MAX_CHARS


# ── --dry-run ────────────────────────────────────────────────────────────────


def test_dry_run_writes_nothing_and_previews_the_name_that_would_land(capsys):
    """The preview shows the NORMALISED string, not the operator's argument.

    A dry run that echoed the raw name would preview a 500-character paste at
    500 and land it at 64 — a preview that disagrees with its own write is worse
    than no preview, which is why the CLI asks
    ``gateway_identity.clean_display_name`` rather than keeping a second copy of
    the rule.
    """

    _, named = _run(capsys, "rename", "workshop")
    before = _record_path().read_bytes()

    code, data = _run(capsys, "rename", "  kitchen   table ", "--dry-run")

    assert code == 0
    assert data["dry_run"] is True
    assert data["display_name"] == "kitchen table"
    assert data["install_id"] == named["install_id"]
    assert _record_path().read_bytes() == before


def test_dry_run_on_an_absent_record_previews_the_mint_instead_of_refusing(capsys):
    """A real run would SUCCEED here — ``set_display_name`` mints first — so the
    preview must not refuse. It reports the absence in ``state`` rather than
    inventing an id it has not minted."""

    code, data = _run(capsys, "rename", "workshop", "--dry-run")

    assert code == 0
    assert data["dry_run"] is True
    assert data["display_name"] == "workshop"
    assert data["install_id"] is None
    assert data["state"] == "error:absent"
    assert not _record_path().exists()


# ── typed service states become exit families, never tracebacks ──────────────


def test_an_empty_name_is_refused_rather_than_written(capsys):
    _, named = _run(capsys, "rename", "workshop")

    code, data = _run(capsys, "rename", "   ")

    assert code == 2
    assert data["error"]["code"] == "invalid_payload"
    record = json.loads(_record_path().read_bytes().decode("utf-8"))
    assert record["display_name"] == named["display_name"]


@pytest.mark.parametrize("verb", ["id", "rename"])
def test_an_undecodable_record_refuses_and_is_never_re_minted(capsys, verb):
    """The asymmetry ``gateway_identity._decode`` documents, held at the CLI.

    A zero-byte file's id is held by nobody, but a file with bytes in it may be
    a record whose id a paired device still names. Overwriting it to make a verb
    look tidy destroys the only copy of the join key — so both doors refuse and
    leave the bytes alone.
    """

    path = _record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{not json at all")

    argv = ["rename", "workshop"] if verb == "rename" else ["id"]
    code, data = _run(capsys, *argv)

    assert code == 1
    assert data["error"]["code"] == "store_corrupt"
    # The typed reason travels verbatim: the same word the `install` block puts
    # on a greeting frame, so an operator comparing the two reads one spelling.
    assert "error:malformed_record" in data["error"]["message"]
    assert path.read_bytes() == b"{not json at all"


# ── the tree actually routes ─────────────────────────────────────────────────


def test_the_group_refuses_a_bare_invocation_rather_than_doing_something(capsys):
    """``gateway`` is a group, not a verb — ``required=True`` on the subparser,
    the same shape every other group on this tree carries."""

    with pytest.raises(SystemExit) as exc:
        _dispatch(["harness", "gateway"])

    assert exc.value.code == 2
