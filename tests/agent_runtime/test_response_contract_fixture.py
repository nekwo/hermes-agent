"""The response-envelope fixture gate — re-derived from the producer.

=============================================================================
WHY THIS FILE WAS REWRITTEN (2026-08-09)
=============================================================================

This gate used to hash each fixture against ``MANIFEST.sha256`` and check four
structurally-invariant scalars. Both halves are self-referential: the manifest
digest is generated FROM the committed bytes and lives beside them, so the two
move together or neither moves, and the four scalars (``kind``, ``item_kind``,
``items``, ``exit_code``) are precisely the fields that do NOT change when the
response contract evolves.

The consequence was measured, not theorised. Contract 53 removed ``mcp_server``
from ``RUNNING_WORK_SOURCES``. ``work_list_empty.json`` went on advertising it
in ``stdout.sources`` — the Launcher's byte-identical mirror advertised it
too — and this gate stayed GREEN through the whole removal. Its sibling
``test_stream_contract_fixture`` went red in the same window, for the one
structural reason that matters: **the stream gate re-runs the producer and
compares; this one never did.**

A gate that cannot detect producer drift is not coverage. It is worse than no
gate, because the fixture carries an implicit "verified" claim that nothing
verifies, and a consumer repo mirrors those bytes as a contract.

=============================================================================
WHAT IT DOES NOW
=============================================================================

:func:`test_every_fixture_is_re_derivable_from_the_producer` re-runs the real
CLI producer — ``hermes_cli.harness.build_parser`` and the ``_cmd_work_*``
handlers, reached through the generator's own ``_parser`` / ``_run`` helpers so
the test and the generator can never disagree about how a fixture is made — and
requires the result to equal the committed bytes exactly.

Byte equality is achievable here, and that was verified rather than assumed.
The whole nondeterminism surface of these two envelopes is:

* ``error_id``, minted as ``f"err_{uuid4().hex[:8]}"`` — the generator's
  ``_normalize`` rewrites it to ``err_fixture`` and so does ``_run``, which is
  why the comparison can be exact;
* the isolated ``HERMES_HOME`` / ``HERMES_HEAD_HOME`` path, which reaches the
  bytes ONLY as ``detail: "head_home=hermes"`` — ``running_work`` emits the
  basename, never the path. The temp home is therefore named literally
  ``hermes``, exactly as the generator names it. **This coupling is load-bearing
  and easy to break by "tidying" the fixture name below.**

No timestamps, elapsed times, pids, or machine-root probes reach either
envelope: the empty list has no rows to carry per-row time fields, and neither
``work list`` nor ``work peek`` reads ``machine_roots.json`` (the probe that
forced the stream generator to pin ``repo_scopes[*].resolved``).

The manifest hash check SURVIVES, demoted to what it always actually was: a
tripwire for a hand-edited fixture. It is no longer the gate.

RED-PROOF (run before this file landed):

1. **Real producer drift, the original defect reproduced.** Adding a source row
   to ``RUNNING_WORK_SOURCES`` in ``agent_runtime/running_work.py`` — the exact
   shape of the ``mcp_server`` removal, run in reverse — makes
   :func:`test_every_fixture_is_re_derivable_from_the_producer` fail, naming
   ``stdout.sources`` and the added key. The OLD gate passes unchanged against
   that same sabotage, because the bytes and their digest never moved. Reverted.
2. **Contract-scalar drift.** Changing ``item_kind`` at the producer fails here
   and also failed before — kept so the rewrite is a strict superset.
3. :func:`test_the_comparison_would_notice_a_drifted_fixture` runs permanently:
   it plants each drift class into a COPY of the committed envelope and requires
   the comparator to reject it. A comparator that had degenerated into
   ``assert True`` would pass this file forever otherwise.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from hermes_cli.harness_support import ERROR_EXIT_CODES


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "response_envelopes"

#: The generator is the single owner of "how a response fixture is produced".
#: Importing it (rather than re-implementing the parser/dispatch here) is what
#: keeps the gate and the regeneration path from drifting apart — the failure
#: this file exists to prevent, one level up.
_GENERATOR = "scripts.generate_agent_runtime_response_fixtures"

#: Environment-state suffixes that ``running_work`` appends to a source's
#: ``detail`` prose (``running_work.py`` builds ``f"{provenance}={head.name}"``
#: and then appends these).
#:
#: THEY ARE NOT CONTRACT, AND THEY ARE NOT DETERMINISTIC. Whether the isolated
#: home has a ``state.db`` yet depends on whether any module that opens one was
#: imported BEFORE the home was isolated — so the same producer emits
#: ``"head_home=hermes"`` or ``"head_home=hermes; no state.db"`` for the same
#: contract, decided by test import order. The committed fixture happens to
#: encode the first.
#:
#: So the comparison below normalises the suffix away and compares the stable
#: ``provenance=basename`` prefix exactly. The vocabulary is pinned rather than
#: pattern-stripped: an UNKNOWN suffix is a real change in what the producer
#: says and fails, instead of being silently swallowed by a permissive regex.
#: (That this field mixes contract with ambient filesystem state at all is a
#: weakness in ``running_work``, not in the fixture — filed, not fixed here.)
_ENVIRONMENT_DETAIL_SUFFIXES = (
    "no state.db",
    "no checkpoint file",
    "no async_delegations table",
)


def _read(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _normalise_detail(envelope: dict) -> dict:
    """Strip the ambient-state suffixes from every source ``detail``.

    Returns a copy; the caller's envelope is untouched.
    """

    clone = json.loads(json.dumps(envelope))
    for source in (clone.get("stdout", {}).get("sources") or {}).values():
        detail = source.get("detail")
        if not isinstance(detail, str):
            continue
        head, _, tail = detail.partition("; ")
        while tail:
            piece, _, rest = tail.partition("; ")
            assert piece in _ENVIRONMENT_DETAIL_SUFFIXES, (
                f"unrecognised `detail` suffix {piece!r}. If the producer now "
                "says something new here, decide whether it is contract (assert "
                "it) or ambient state (add it to _ENVIRONMENT_DETAIL_SUFFIXES) — "
                "do not let it pass unread."
            )
            tail = rest
        source["detail"] = head
    return clone


@pytest.fixture
def producer(tmp_path, monkeypatch):
    """The real CLI producer, against an isolated home.

    Mirrors the generator's ``main()`` env setup exactly. The home directory is
    named ``hermes`` on purpose: ``running_work`` renders provenance as
    ``head_home=<basename>``, so the name reaches the fixture bytes.
    """

    home = tmp_path / "hermes"
    home.mkdir()
    (tmp_path / "runtime").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_HEAD_HOME", str(home))
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))

    generator = importlib.import_module(_GENERATOR)
    parser = generator._parser()

    def run(argv: list[str]) -> dict[str, Any]:
        return generator._run(parser, argv)

    run.cases = dict(generator.FIXTURE_CASES)  # type: ignore[attr-defined]
    return run


# --------------------------------------------------------------------------- #
# THE GATE
# --------------------------------------------------------------------------- #
def test_every_fixture_is_re_derivable_from_the_producer(producer):
    """Re-run the producer; the committed envelope must be what it emits.

    This is the check whose absence let ``work_list_empty.json`` advertise a
    running-work source that contract 53 had removed.
    """

    for name, argv in producer.cases.items():
        live = _normalise_detail(producer(argv))
        committed = _normalise_detail(_read(name))

        # Report the structural difference first — a raw dict inequality on a
        # 1 KB envelope is unreadable, and the drift that actually happens is a
        # key appearing or leaving.
        assert set(live["stdout"]) == set(committed["stdout"]), (
            f"{name}: top-level response keys drifted from the producer.\n"
            f"  producer only: {sorted(set(live['stdout']) - set(committed['stdout']))}\n"
            f"  fixture only : {sorted(set(committed['stdout']) - set(live['stdout']))}\n"
            f"Regenerate with `python {_GENERATOR.replace('.', '/')}.py` and mirror "
            "the bytes into the Launcher's test/fixtures/hermes_responses/."
        )
        for section in ("sources", "counts", "completeness"):
            if section not in live["stdout"]:
                continue
            live_section, fixture_section = live["stdout"][section], committed["stdout"][section]
            if isinstance(live_section, dict):
                assert set(live_section) == set(fixture_section), (
                    f"{name}: `{section}` keys drifted from the producer.\n"
                    f"  producer only: {sorted(set(live_section) - set(fixture_section))}\n"
                    f"  fixture only : {sorted(set(fixture_section) - set(live_section))}"
                )

        assert live == committed, (
            f"{name}: the producer no longer emits these bytes. Regenerate and "
            "mirror; do not hand-edit the fixture."
        )


def test_the_fixture_set_is_exactly_what_the_generator_produces(producer):
    """A fixture nobody generates is unverifiable by construction.

    The old manifest test pinned the file set against a hardcoded pair, which
    would have accepted a hand-authored third fixture as long as someone also
    appended its digest.
    """

    on_disk = {p.name for p in FIXTURES.glob("*.json")}
    assert on_disk == set(producer.cases), (
        f"fixture files on disk {sorted(on_disk)} != generator cases "
        f"{sorted(producer.cases)}"
    )


def test_the_manifest_is_a_hand_edit_tripwire_not_the_gate():
    """Kept, demoted, and labelled.

    Hashing committed bytes against a digest committed beside them proves only
    that nobody edited one without the other. That is worth having and it is not
    drift detection — the gate above is.
    """

    rows = (FIXTURES / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines()
    manifest = {name: digest for digest, name in (row.split("  ", 1) for row in rows)}
    assert set(manifest) == {p.name for p in FIXTURES.glob("*.json")}
    for name, expected in manifest.items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == expected


# --------------------------------------------------------------------------- #
# Anti-vacuity
# --------------------------------------------------------------------------- #
def _rejects(live: dict, committed: dict) -> bool:
    """The comparator's decision, extracted so it can be driven on planted data."""

    if set(live["stdout"]) != set(committed["stdout"]):
        return True
    for section in ("sources", "counts", "completeness"):
        a, b = live["stdout"].get(section), committed["stdout"].get(section)
        if isinstance(a, dict) and isinstance(b, dict) and set(a) != set(b):
            return True
    return live != committed


@pytest.mark.parametrize(
    "drift",
    [
        pytest.param(
            lambda e: e["stdout"]["sources"].update(
                {"mcp_server": {"lane": "live", "status": "ok"}}
            ),
            id="a-removed-source-still-advertised",
        ),
        pytest.param(
            lambda e: e["stdout"].__setitem__("item_kind", "running_work_v2"),
            id="contract-scalar-renamed",
        ),
        pytest.param(
            lambda e: e["stdout"]["sources"].pop("terminal"),
            id="a-source-silently-dropped",
        ),
        pytest.param(
            lambda e: e["stdout"]["sources"]["cron_job"].__setitem__("status", "ok"),
            id="a-source-status-flipped",
        ),
        pytest.param(
            lambda e: e["stdout"].__setitem__("schema_version", 2),
            id="schema-version-bumped",
        ),
        pytest.param(
            lambda e: e["stdout"]["counts"].__setitem__("total", 7),
            id="a-count-value-changed",
        ),
    ],
)
def test_the_comparison_would_notice_a_drifted_fixture(producer, drift):
    """Each drift class, planted into a copy of the real envelope.

    The old gate is the cautionary tale: it hashed a fixture against its own
    manifest and therefore could not have failed on ANY of these. Driving the
    comparator on planted data is what stops this rewrite from decaying the same
    way — if it ever stops discriminating, this fails while the tree is clean.
    """

    live = _normalise_detail(producer(["harness", "work", "list", "--json"]))
    committed = _normalise_detail(_read("work_list_empty.json"))
    assert not _rejects(live, committed), "precondition: the clean fixture must be accepted"

    drifted = json.loads(json.dumps(committed))
    drift(drifted)
    assert _rejects(live, drifted), "the comparator accepted a drifted fixture"


def test_the_detail_prose_carries_a_basename_never_a_path(producer):
    """``detail`` is provenance prose, and the ONE thing about it that IS
    contract is that it never leaks an absolute path into bytes the Launcher
    mirrors. ``running_work`` emits ``head.name``; this pins that it stays a
    basename."""

    live = producer(["harness", "work", "list", "--json"])
    for source_name, source in (live["stdout"].get("sources") or {}).items():
        detail = source.get("detail")
        if not isinstance(detail, str):
            continue
        assert detail.startswith("head_home=hermes"), (source_name, detail)
        for leak in ("C:\\", "X:\\", "/mnt/", "AppData"):
            assert leak not in detail, f"{source_name} leaks a path: {detail!r}"


# --------------------------------------------------------------------------- #
# Contract-shape assertions (retained from the previous gate)
# --------------------------------------------------------------------------- #
def test_work_peek_fixture_is_the_real_typed_not_found_response():
    fixture = _read("work_peek_not_found.json")
    assert fixture["argv"] == [
        "harness",
        "work",
        "peek",
        "terminal:fixture-missing",
        "--json",
    ]
    assert fixture["exit_code"] == ERROR_EXIT_CODES["not_found"]
    assert fixture["stdout"]["kind"] == "error"
    assert fixture["stdout"]["error"]["code"] == "not_found"


def test_work_list_fixture_is_an_accepted_empty_response():
    fixture = _read("work_list_empty.json")
    assert fixture["exit_code"] == 0
    assert fixture["stdout"]["kind"] == "list"
    assert fixture["stdout"]["item_kind"] == "running_work"
    assert fixture["stdout"]["items"] == []
