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
  bytes ONLY as ``ambient.home_name: "hermes"`` — ``running_work`` emits the
  basename, never the path. The temp home is therefore named literally
  ``hermes``, exactly as the generator names it. **This coupling is load-bearing
  and easy to break by "tidying" the fixture name below.**

The comparison used to need a NORMALISER here, and its removal is the point of
this paragraph. ``running_work`` folded machine-local filesystem state into the
same ``detail`` prose that carried its contract, and one half of that state —
"does ``state.db`` exist yet" — was perturbed by the projection's own lazy
import, so the producer emitted ``"head_home=hermes"`` or
``"head_home=hermes; no state.db"`` for identical work depending on test import
order. This file worked around it by pinning the suffix vocabulary and comparing
only the stable prefix. The producer now separates the two — contract on the
source entry, machine-local context in a sibling ``ambient`` block, store
presence off the wire entirely — so the envelopes are byte-stable as emitted and
the comparison is raw.

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


def _read(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def producer(tmp_path, monkeypatch):
    """The real CLI producer, against an isolated home.

    Mirrors the generator's ``main()`` env setup exactly. The home directory is
    named ``hermes`` on purpose: ``running_work`` publishes the resolved home
    as ``ambient.home_name = <basename>``, so the name reaches the fixture bytes.
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
        live = producer(argv)
        committed = _read(name)

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

    live = producer(["harness", "work", "list", "--json"])
    committed = _read("work_list_empty.json")
    assert not _rejects(live, committed), "precondition: the clean fixture must be accepted"

    drifted = json.loads(json.dumps(committed))
    drift(drifted)
    assert _rejects(live, drifted), "the comparator accepted a drifted fixture"


def test_the_ambient_home_is_a_basename_never_a_path(producer):
    """The machine-local block names the home; it must never leak the PATH.

    These bytes are mirrored verbatim into the Launcher repo, and the error
    contract forbids absolute paths in operator-visible messages, so the one
    thing that IS guaranteed about this diagnostic is that it stays a basename.
    The guarantee used to sit on ``detail``; it moved here with the fact.
    """

    live = producer(["harness", "work", "list", "--json"])
    ambient = live["stdout"]["ambient"]

    assert ambient == {"home_provenance": "head_home", "home_name": "hermes"}
    for leak in ("C:\\", "X:\\", "/mnt/", "AppData", "Temp", "/"):
        assert leak not in ambient["home_name"], f"leaks a path: {ambient!r}"


def test_no_source_health_entry_carries_machine_local_prose(producer):
    """The split itself, pinned on the bytes a consumer repo mirrors.

    A source entry is CONTRACT. Before this split it also carried the resolved
    home and the presence of files under it, concatenated into one ``detail``
    string — which is how a wire field became a function of test import order.
    An ``ok`` lane now attaches nothing but typed keys, and ``detail`` survives
    only as the bare machine token an ``unavailable`` lane reports.
    """

    live = producer(["harness", "work", "list", "--json"])
    home_name = live["stdout"]["ambient"]["home_name"]

    for source_name, source in (live["stdout"]["sources"]).items():
        assert set(source) <= {
            "status",
            "lane",
            "reason",
            "detail",
            "live_enrichment_error",
        }, (source_name, source)
        detail = source.get("detail", "")
        assert home_name not in detail, (
            f"{source_name}: the resolved home is back on a contract field "
            f"({detail!r}). It belongs in `ambient`."
        )
        assert "; " not in detail, (
            f"{source_name}: `detail` is concatenating facts again ({detail!r}). "
            "Model them as separate typed keys."
        )


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
