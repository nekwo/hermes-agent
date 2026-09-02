"""The adjacency probe judges a cite by its SUBJECT, and is a capped gate.

The other half of the doc-cite pair. ``test_doc_cite_report`` pins the half that
asks whether a cite RESOLVES; this pins the half that asks whether the cited
line is about the thing the sentence says it is — the failure four hermes cites
had been committing since `76c6ade663` while resolving perfectly.

What is pinned here is the JUDGEMENT, at the seam the judgement lives at
(:func:`verdict`), not through the file-walking loop around it: every case below
hands the rule a fabricated doc and a fabricated module and asks for its
verdict. Two integration tests then pin the two things only the whole program
can answer — that a zero-cite walk is FATAL, and that the live canon is capped
by its baseline in both directions.
"""

from __future__ import annotations

import json

import pytest

from scripts import doc_cite_adjacency as probe


LIVE_ROOT = "docs/agent-runtime-harness"
LIVE_EXCLUDE = ["archive/", "planned/"]


MODULE = '''"""A module whose docstring names publish_chat_head_home and nothing else."""


def unrelated_helper(value):
    return value + 1


CONSTANT_ONE = "one"
CONSTANT_TWO = "two"


def publish_chat_head_home(store_root):
    """The one writer of the shared chat-head pointer."""

    pointer = store_root / "head.json"
    pointer.write_text("{}")
    return pointer
'''


def target() -> probe.Target:
    return probe.Target("agent_runtime/fake_module.py", MODULE)


def judge(doc: str, line_index: int, radius: int = 3):
    """Run the rule over a fabricated doc, returning (verdict, subjects)."""

    lines = doc.splitlines()
    match = probe.CITE.search(lines[line_index])
    assert match is not None, f"no cite on line {line_index}: {lines[line_index]!r}"
    return probe.verdict(lines, line_index, match, target(), radius)


def test_a_cite_at_the_wrong_line_is_reported_and_names_its_subject():
    """The rowed defect, reproduced: the file is alive, the line exists, the
    resolution half is green — and the line is nowhere near the function."""

    doc = (
        "The ONE writer of the shared chat-head pointer is\n"
        "`publish_chat_head_home` (`fake_module.py:5`), and without it a later\n"
        "plain CLI turn degrades to its own profile database.\n"
    )

    outcome, present = judge(doc, 1)

    assert outcome == probe.FAILED
    assert present == ["publish_chat_head_home"]


def test_the_same_cite_at_the_right_line_passes():
    """ANTI-VACUITY for the case above: same doc, same subject, same rule —
    only the number changes, and the verdict flips. Without this the failing
    test is satisfied by a probe that fails everything."""

    doc = (
        "The ONE writer of the shared chat-head pointer is\n"
        "`publish_chat_head_home` (`fake_module.py:12`), and without it a later\n"
        "plain CLI turn degrades to its own profile database.\n"
    )

    outcome, present = judge(doc, 1)

    assert outcome == probe.ADJACENT
    assert present == ["publish_chat_head_home"]


def test_a_line_inside_the_named_function_passes_as_in_symbol():
    """A cite to a line deep inside the function the sentence names is not rot.

    Calling it rot would push the canon toward citing the ``def`` line of
    everything, so it passes — but on its OWN verdict, so the headline count can
    still be read strictly.
    """

    doc = (
        "The ONE writer of the shared chat-head pointer is\n"
        "`publish_chat_head_home` (`fake_module.py:16`), and without it a later\n"
        "plain CLI turn degrades to its own profile database.\n"
    )

    outcome, _ = judge(doc, 1)

    assert outcome == probe.IN_SYMBOL


def test_a_neighbouring_sentence_does_not_speak_for_this_cite():
    """The measured false-positive class, pinned.

    `realm_membership.py:1-12` was reported rotted because the sentence BEFORE
    it named two classes that live in that file twenty lines below the cited
    docstring. A subject belongs to its own sentence; borrowing one from the
    neighbour invents a finding.
    """

    doc = (
        "`publish_chat_head_home` is the one writer of the chat-head pointer.\n"
        "Server-bound realms authorize every sync action against the backend\n"
        "and **fail closed** (`fake_module.py:1`).\n"
    )

    outcome, present = judge(doc, 2)

    assert outcome == probe.NO_SUBJECT
    assert present == []


def test_a_sentence_naming_nothing_in_the_file_is_unchecked_never_failed():
    """A paragraph the probe cannot read is not evidence of rot. Counting it as
    a failure is how a report starts inventing findings."""

    doc = "The store keeps one file per card (`fake_module.py:5`).\n"

    outcome, present = judge(doc, 0)

    assert outcome == probe.NO_SUBJECT
    assert present == []


def test_the_cited_path_is_never_its_own_subject():
    """`models` matching inside `models.py` would pass every cite in the canon,
    so the cite's own token is subtracted from the subjects."""

    doc = "See `fake_module.py:5` for the shape.\n"

    outcome, _ = judge(doc, 0)

    assert outcome == probe.NO_SUBJECT


def test_a_range_cite_is_judged_from_both_ends():
    """`file.py:9-12` asks about a REGION: the window runs from three before the
    first line to three after the last, so a subject anywhere in it answers."""

    doc = (
        "`publish_chat_head_home` writes the pointer\n"
        "(`fake_module.py:8-9`).\n"
    )

    outcome, _ = judge(doc, 1)

    assert outcome == probe.ADJACENT


def test_a_range_cite_still_fails_when_the_whole_region_is_elsewhere():
    """ANTI-VACUITY for the range case: widening the window is not a free pass."""

    doc = (
        "`publish_chat_head_home` writes the pointer\n"
        "(`fake_module.py:6-7`).\n"
    )

    outcome, _ = judge(doc, 1)

    assert outcome == probe.FAILED


def test_a_line_past_the_end_of_the_file_is_refused_not_judged():
    """There is no window to probe, and the resolution report already owns the
    class. Folding it in here would double-count one defect."""

    doc = "`publish_chat_head_home` (`fake_module.py:9000`).\n"

    outcome, _ = judge(doc, 0)

    assert outcome == probe.PAST_END


def test_short_identifiers_are_not_subjects():
    """`id` / `ok` / `db` in a backtick match half of every Python file and
    would turn the probe green by coincidence."""

    found = probe.subjects("`ok` and `db` and `value` and `store_root`")

    assert found == {"value", "store_root"}


def test_the_probe_fails_loud_on_a_zero_cite_walk(capsys):
    """An unrun gate is indistinguishable from a passing one.

    Run against a REAL doc root that carries no Python cites — not a stubbed
    walk — because the thing being pinned is that the program refuses to print
    a clean report over nothing.
    """

    code = probe.main(["--root", "docs/agent-runtime-harness/upstream-prs"])
    err = capsys.readouterr().err

    assert code == 2
    assert "zero cites" in err


def test_the_live_canon_is_capped_by_its_baseline(capsys):
    """The gate itself, over the real canon: every remaining failure is waived
    with a written reason, and nothing new has appeared."""

    code = probe.main(
        ["--root", LIVE_ROOT, *sum(([f"--exclude={f}"] for f in LIVE_EXCLUDE), [])]
    )
    out = capsys.readouterr().out

    assert code == 0, out
    assert "UNWAIVED FAILURES: 0" in out
    assert "STALE WAIVERS (no longer failing - delete the entry): 0" in out


def test_every_baseline_entry_carries_a_written_reason():
    """A waiver with no reason is a silenced gate one commit later."""

    waived = probe.load_baseline(probe.REPO_ROOT / probe.DEFAULT_BASELINE)

    assert waived, "the baseline is empty — has the sweep budget been closed?"
    for key, reason in waived.items():
        assert len(reason) > 40 and "TODO" not in reason, key


def test_a_waiver_that_has_stopped_failing_turns_the_gate_red(tmp_path, capsys):
    """The ratchet, and the half a plain baseline never has.

    A waiver that outlives its rot is how a capped gate silently stops
    shrinking. Burn-down is enforced by making the stale entry itself red.
    """

    stale = tmp_path / "baseline.json"
    real = probe.load_baseline(probe.REPO_ROOT / probe.DEFAULT_BASELINE)
    stale.write_text(
        json.dumps({"waived": {**real, "docs/nowhere.md|gone.py:1": "long gone"}}),
        encoding="utf-8",
    )

    code = probe.run(LIVE_ROOT, LIVE_EXCLUDE, 3, stale, write_baseline=False)
    out = capsys.readouterr().out

    assert code == 1
    assert "docs/nowhere.md|gone.py:1" in out
    assert "STALE WAIVERS (no longer failing - delete the entry): 1" in out


def test_an_unwaived_failure_turns_the_gate_red(tmp_path, capsys):
    """The other direction: an empty baseline over a canon that still carries
    waived rot is red, so the cap cannot be dropped by deleting the file."""

    empty = tmp_path / "baseline.json"
    empty.write_text(json.dumps({"waived": {}}), encoding="utf-8")

    code = probe.run(LIVE_ROOT, LIVE_EXCLUDE, 3, empty, write_baseline=False)

    assert code == 1
    assert "UNWAIVED FAILURES: 0" not in capsys.readouterr().out


@pytest.mark.parametrize("radius", [0, 3, 10])
def test_the_window_widens_with_the_radius(radius):
    """The +/-3 is a parameter, not a constant welded into the rule — and the
    verdict really does depend on it."""

    doc = "`publish_chat_head_home` (`fake_module.py:9`).\n"

    outcome, _ = judge(doc, 0, radius=radius)

    assert outcome == (probe.ADJACENT if radius >= 3 else probe.FAILED)


# --------------------------------------------------------------------------
# Bare `:N` continuation cites (2026-09-02). The canon writes `harness.py:1873`
# and then `at :4776`, and the first cut of this probe could not see the second
# half at all: 299 of them against 326 path cites in the gated canon.
# --------------------------------------------------------------------------


def judge_continuation(doc: str, line_index: int, radius: int = 3):
    """Resolve the bare `:N` on a line and run the SAME rule over it."""

    lines = doc.splitlines()
    match = probe.CONTINUATION.search(lines[line_index])
    assert match is not None, f"no `:N` on line {line_index}: {lines[line_index]!r}"
    token = probe.continued_path(lines, line_index, match)
    if token is None:
        return None, []
    return probe.verdict(
        lines, line_index, probe.ContinuedCite(token, match), target(), radius
    )


def test_a_bare_continuation_inherits_the_preceding_path_and_is_judged_the_same():
    """The rowed residual: `fake_module.py:5`, `publish_chat_head_home` at `:5`
    carries no path on its second half, and the line is still wrong."""

    doc = (
        "The pointer is written in one place (`fake_module.py:5`,\n"
        "`publish_chat_head_home` at `:5`), and nowhere else.\n"
    )

    outcome, present = judge_continuation(doc, 1)

    assert outcome == probe.FAILED
    assert present == ["publish_chat_head_home"]


def test_the_same_continuation_at_the_right_line_passes():
    """ANTI-VACUITY: same sentence, same inherited path, only the number moves."""

    doc = (
        "The pointer is written in one place (`fake_module.py:5`,\n"
        "`publish_chat_head_home` at `:12`), and nowhere else.\n"
    )

    outcome, present = judge_continuation(doc, 1)

    assert outcome == probe.ADJACENT
    assert present == ["publish_chat_head_home"]


def test_a_continuation_inherits_a_path_that_carries_no_line_number():
    """Measured on 03-transport-and-wire.md, and the reason the rule reads a path
    MENTION rather than a path CITE.

    The canon writes ``module.py::symbol`` with no line, and a rule that only
    looked at ``CITE`` skipped it and handed the `:N` to the file cited BEFORE
    it — a fabricated finding, not a missed one.
    """

    doc = (
        "Parsed by `other/module.py:900`, then by\n"
        "`agent_runtime/fake_module.py::publish_chat_head_home`, `:12`.\n"
    )
    lines = doc.splitlines()
    match = probe.CONTINUATION.search(lines[1])

    assert probe.continued_path(lines, 1, match) == "agent_runtime/fake_module.py"


def test_a_continuation_with_no_path_in_its_sentence_is_refused_not_guessed():
    """Same refusal as an ambiguous bare name. Lending a continuation the path
    of a cite from a neighbouring sentence is how a probe invents a finding —
    measured: 76 of the canon's continuations would inherit a paragraph-scoped
    path, and the ones checked by hand inherit the WRONG file.
    """

    doc = (
        "`agent_runtime/fake_module.py:12` is the writer. A different claim\n"
        "entirely mentions `publish_chat_head_home` at `:5`.\n"
    )
    lines = doc.splitlines()
    match = probe.CONTINUATION.search(lines[1])

    assert probe.continued_path(lines, 1, match) is None


def test_the_gate_counts_continuations_it_could_not_resolve():
    """An unresolved continuation must be VISIBLE, not silently dropped: this is
    the number that says how much of the canon the rule still cannot read."""

    result = probe.walk(LIVE_ROOT, LIVE_EXCLUDE, 3)

    assert result.continuations_seen > 0
    assert result.continuations_unresolved > 0
    assert result.continuations_unresolved < result.continuations_seen


# --------------------------------------------------------------------------
# The subject occurrence ceiling (2026-09-02).
# --------------------------------------------------------------------------


COMMON = (
    '"""A module in which `pointer` is everywhere and pins nothing."""\n\n\n'
    + "".join(f"pointer_{n} = 'pointer'\n" for n in range(30))
    + "\n\ndef publish_chat_head_home(store_root):\n    return store_root\n"
    # Called as well as defined, so the exemption below is doing real work: a
    # name used exactly once would clear any ceiling on its own.
    + "\n\nHEAD = publish_chat_head_home\n"
)


def common_target() -> probe.Target:
    return probe.Target("agent_runtime/common_module.py", COMMON)


def test_an_identifier_that_occurs_everywhere_is_not_a_locator():
    """The measured coincidence, reproduced.

    `07-observability.md|persona_commands.py:3522` passed because its sentence
    also backticks `show`, `final` and `chat`, one of which landed in the +/-3
    window — while its real subject sits at 127/4222/4495. A word the file uses
    everywhere answers "is this file about that" and never "is this LINE".
    """

    doc = "The `pointer` table (`common_module.py:8`) is built once.\n"
    lines = doc.splitlines()
    match = probe.CITE.search(lines[0])

    outcome, present = probe.verdict(lines, 0, match, common_target(), 3)

    assert outcome == probe.NO_SUBJECT
    assert present == []


def test_the_same_identifier_under_the_ceiling_is_still_a_locator():
    """ANTI-VACUITY: the ceiling is a frequency rule, not a word ban. Raise it
    past the identifier's own count and the very same cite passes."""

    doc = "The `pointer` table (`common_module.py:8`) is built once.\n"
    lines = doc.splitlines()
    match = probe.CITE.search(lines[0])

    outcome, present = probe.verdict(lines, 0, match, common_target(), 3, ceiling=100)

    assert outcome == probe.ADJACENT
    assert present == ["pointer"]


def test_a_name_the_file_defines_is_exempt_from_the_ceiling():
    """A ``def``/``class`` name pins a line by construction, however often the
    file then calls it. Without the exemption the ceiling refuses a file's own
    workhorses — `store_root` in `paths.py`, `StoreDriftItem` in `realm_sync.py`
    — and reports cites landing exactly on their ``def`` as rot, which is
    inventing a finding in order to stop inventing findings.
    """

    doc = "`publish_chat_head_home` (`common_module.py:34`) writes it.\n"
    lines = doc.splitlines()
    match = probe.CITE.search(lines[0])

    outcome, present = probe.verdict(lines, 0, match, common_target(), 3, ceiling=1)

    assert common_target().occurrences("publish_chat_head_home") > 1
    assert outcome == probe.ADJACENT
    assert present == ["publish_chat_head_home"]


def test_the_ceiling_can_never_turn_a_red_cite_green():
    """The direction that makes the rule safe: dropping a subject can only make
    a cite FAILED or UNCHECKED, never ADJACENT. A gate whose new rule could
    silence an existing failure would need its own audit; this one cannot."""

    doc = "The `pointer` table (`common_module.py:8`) is built once.\n"
    lines = doc.splitlines()
    match = probe.CITE.search(lines[0])
    loose, _ = probe.verdict(lines, 0, match, common_target(), 3, ceiling=100)
    tight, _ = probe.verdict(lines, 0, match, common_target(), 3, ceiling=4)

    assert loose == probe.ADJACENT
    assert tight in (probe.NO_SUBJECT, probe.FAILED)
