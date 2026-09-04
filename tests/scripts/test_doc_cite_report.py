"""The doc-cite report finds rot, refuses to guess, and never fails a lane.

W1-H3 slice 6. Advisory by ruling: it lands red on ~60 pre-existing cites, and
a gate that is born red gets silenced, after which the next reader believes it.

What this file pins is the part that would make the report worthless if it
drifted — not the counts, which change with every doc edit, but the three
judgements underneath them:

* it reports only cites carrying a LINE NUMBER. Measured while building it:
  accepting bare path-shaped tokens produced 498 "dead path" rows over the
  harness canon, of which nearly none were citations — `models.py` in a
  sentence, `gateway/peers.json` naming a file in the runtime store;
* an ambiguous bare name is COUNTED, never resolved to one of its candidates;
* the exit code is 0 whatever it found.
"""

from __future__ import annotations

import subprocess

import pytest

from scripts import doc_cite_report as report


class _SpawnCounter:
    """Counts real ``git`` processes without faking any of them.

    What the assertions below are about is the process COUNT, so the calls have
    to stay real: a stub would pin the shape of a mock instead of the cost of
    the report.
    """

    def __init__(self, monkeypatch):
        self.count = 0
        real = subprocess.run

        def counted(*args, **kwargs):
            self.count += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(report.subprocess, "run", counted)


#: The live-canon test's cost is its CORPUS and its GIT calls, and the repo-wide
#: per-test cap does not know that. Measured 2026-09-02 on the Windows dev box:
#: importing ``scripts.doc_cite_report`` is 0.54 s and there is no module fixture
#: at all, so nothing here is slow before collection; ``report.main`` over the
#: gated canon is 26.6 / 40.0 / 46.0 s across three runs, of which 32.1 s is 356
#: ``git`` subprocesses (``cat-file -e`` then ``merge-base --is-ancestor``, two
#: per distinct sha, ~90 ms each to spawn on Windows) and 0.9 s is 634
#: ``_line_count`` reads. Against ``pyproject.toml``'s ``--timeout=30`` that is
#: green on an idle box and dead under an 8-worker suite — which is how it was
#: reported as "times out before collection": that is the wording of
#: ``run_tests_parallel``'s bucket ("collection/import error, timeout before
#: collection, etc."), not a diagnosis.
#:
#: So the cap is DECLARED here with the number beside it rather than left to a
#: hand-passed ``--timeout=600``, the same repair
#: ``tests/test_coverage_claims_resolve.py`` carries. It stays far under
#: ``run_tests_parallel``'s 300 s per-FILE wall, which remains the real bound on
#: a hang: this buys headroom for a slow corpus walk, not for a loop.
#:
#: 2026-09-04: the 32.1 s half is GONE — ``_classify_shas`` batches every sha
#: into two ``git`` processes, so the same gated canon costs 3 spawns and 2.5 s
#: against 454 and 46.2 s, with byte-identical report output. The cap stays at
#: 180 rather than dropping to the new measurement: what it bounds is a corpus
#: that grows, and the reason it exists — that the repo-wide 30 s knows nothing
#: about a walk over the whole canon — did not change with the spawn count.
_LIVE_CANON_TIMEOUT_SECONDS = 180


def test_only_a_cite_with_a_line_number_is_a_cite():
    """The noise control, stated as a property of the pattern."""

    matched = [match.group(0) for match in report.CITE.finditer(
        "see office_store.py:444 and models.py for the shape, plus paths.py:12-18"
    )]

    assert matched == ["office_store.py:444", "paths.py:12-18"]


def test_a_sha_is_only_read_inside_backticks():
    """Bare hex in prose is a digest, an id, a colour far more often than a
    commit. A report whose rows have to be filtered by eye is not read."""

    found = report.SHA.findall("landed `0c744aa586`, digest deadbeef1234, `af1b3944b9`")

    assert found == ["0c744aa586", "af1b3944b9"]


def test_an_exact_tracked_path_resolves_to_itself():
    paths = {"agent_runtime/office_store.py", "tests/agent_runtime/test_office_store.py"}
    by_name = {
        "office_store.py": ["agent_runtime/office_store.py"],
        "test_office_store.py": ["tests/agent_runtime/test_office_store.py"],
    }

    assert (
        report._resolve("agent_runtime/office_store.py", paths, by_name)
        == "agent_runtime/office_store.py"
    )


def test_a_unique_bare_name_resolves_and_an_ambiguous_one_does_not():
    """The refusal that keeps the report honest.

    ANTI-VACUITY: the same call shape answers for both names in one index, so
    the `None` is the ambiguity and not a lookup that never works.
    """

    paths = {
        "agent_runtime/office_store.py",
        "agent_runtime/models.py",
        "gateway/models.py",
    }
    by_name = {
        "office_store.py": ["agent_runtime/office_store.py"],
        "models.py": ["agent_runtime/models.py", "gateway/models.py"],
    }

    assert report._resolve("office_store.py", paths, by_name) == "agent_runtime/office_store.py"
    assert report._resolve("models.py", paths, by_name) is None


def test_a_suffix_path_resolves_only_when_it_is_unique():
    """`blueprints/resolve.py` for `agent_runtime/blueprints/resolve.py` is how
    the canon actually spells half its cites, so the tail is followed — but
    only where one file can answer for it."""

    paths = {"agent_runtime/blueprints/resolve.py", "web/src/blueprints/resolve.py"}
    by_name = {"resolve.py": sorted(paths)}

    assert report._resolve("blueprints/resolve.py", paths, by_name) is None
    assert (
        report._resolve("agent_runtime/blueprints/resolve.py", paths, by_name)
        == "agent_runtime/blueprints/resolve.py"
    )


@pytest.mark.timeout(_LIVE_CANON_TIMEOUT_SECONDS)
def test_the_report_exits_zero_over_the_live_canon(capsys):
    """Advisory means advisory. Run over the real docs — which really do carry
    dead cites — and the exit code is still 0, because the moment this can fail
    a lane it becomes a gate that is born red.
    """

    code = report.main(
        ["--root", "docs/agent-runtime-harness", "--exclude", "archive/", "--base", "HEAD"]
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "ADVISORY — this is not a gate" in out
    assert "PATH DOES NOT RESOLVE:" in out


def test_every_sha_verdict_costs_two_git_processes_however_many_shas(monkeypatch):
    """The row's whole finding: process creation was the report.

    Per sha this used to be `cat-file -e` then `merge-base --is-ancestor` — two
    spawns each, ~90 ms apiece on Windows, 32.1 s across 356 of them. The three
    verdicts are unchanged and are asserted here beside the count, because a
    batch that is cheap and wrong is the only way this change could hurt.

    ANTI-VACUITY: all three verdicts come out of ONE call, so `unknown` is the
    classification and not a probe that never resolves anything.
    """

    head, ancestor, base = (
        subprocess.run(
            ["git", "rev-parse", rev],
            cwd=report.REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()[:10]
        for rev in ("HEAD", "HEAD~3", "HEAD~2")
    )
    # AFTER the revs are resolved: `report.subprocess` IS the `subprocess`
    # module, so the patch is global and would count this fixture's own spawns.
    counter = _SpawnCounter(monkeypatch)

    verdicts = report._classify_shas([ancestor, head, "deadbee", ancestor], base)

    assert verdicts == {ancestor: "ok", head: "offline", "deadbee": "unknown"}
    assert counter.count == 2


def test_no_shas_at_all_spawns_nothing(monkeypatch):
    """The empty batch is a return, not a `git` invocation with no input."""

    counter = _SpawnCounter(monkeypatch)

    assert report._classify_shas([], "HEAD") == {}
    assert counter.count == 0
