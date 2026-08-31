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

from scripts import doc_cite_report as report


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
