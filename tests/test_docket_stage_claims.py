"""A docket's stage claims are checked against the REPOSITORY, not another doc.

The hermes port of the launcher's `no_stale_shipped_mcf_row_test.dart`
(2026-08-19, MCF-58). The row this closes: a retirement docket's header read
"stages 1, 3 and 5 remain" for eight days after `dd084605d6` executed 1, 3 and
half of 5, and the 2026-08-30 triage read the stale header and mis-scheduled a
track off it. Re-derived 2026-09-04: `scripts/` had no such check and
`.githooks/` holds only `post-merge`, so nothing struck a docket row when its
stage landed.

WHY IT IS NOT A TWO-DOCUMENT CHECK
==================================

The launcher's gate exists because **two documents agreeing proves consistency,
never correctness**: its queue and its ledger both said OPEN for a row that had
shipped hours earlier, and the two-way gate passed the whole time. So both gates
compare the docket against something that cannot be talked into agreeing — the
git history.

Here that is: a stage heading claiming EXECUTED / LANDED must cite a commit that
is an ANCESTOR OF HEAD. Ancestry, not existence, and the difference is measured:
these docs cite launcher shas constantly (56 sha-shaped tokens under
`docs/agent-runtime-harness/` resolve to nothing here, and the great majority
are the other repo's commits, cited legitimately). `git cat-file -e` would read
every one of those as a missing commit and invent 56 findings; ancestry of HEAD
answers "did this land HERE", which is the only question this gate can honestly
ask.

WHAT IT DOES NOT COVER, stated because silence about coverage reads as coverage
==============================================================================

The launcher's gate has a second arm this one CANNOT have. There, a landing
claim is `git log` finding a commit whose SUBJECT names the row's id (`MCF-39`),
so a row that stayed OPEN while its work shipped is provable. Hermes commit
subjects name a surface and a change and never a stage id -- measured over the
whole history -- so there is no way from here to find the commit that landed a
stage the docket never edited. That half needs a convention adopted first (an
operator ruling), and until it is, this gate sees only claims the docket
actually makes:

  * a stage that says it shipped, and did not ship here;
  * a doc that says stages remain, naming one its own rows record as landed.

It cannot judge COMPLETENESS: a cited commit proves a stage was worked, never
that it is finished.

MEASURED AT `3d3a33be3e`
========================

82 docket files, 42 stage sections, 11 with an in-history landing sha, 7 stage
headings claiming done -- all 7 backed -- and 1 "stages ... remain" claim,
consistent with its own rows. The gate lands green; its rule is falsified on
fabricated dockets below rather than on the live corpus, which carries no case.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKET_ROOT = "docs/agent-runtime-harness/planned"

#: A markdown heading, any of the three levels these dockets use.
HEADING = re.compile(r"^#{2,4}\s+(.*)$", re.M)

#: The heading of a stage section. ``Track`` as well as ``Stage`` because the
#: mis-scheduled unit in the incident was "Track Z0".
STAGE_HEADING = re.compile(r"^(?:Stage|Track)\s+([A-Za-z]?\d+[a-z]?)\b", re.I)

#: A stage heading declaring the stage shipped. Read from the HEADING only, not
#: from the body: a body saying "stage 4 landed" is prose about a neighbour,
#: and this docket's convention puts the verdict in the heading
#: (``### Stage 1 — ... — **EXECUTED `dd084605d6`**``).
DONE_CLAIM = re.compile(r"\b(EXECUTED|LANDED|SHIPPED|IMPLEMENTED|DONE|CLOSED)\b")

#: A backticked commit-ish token. Backticked because bare hex in prose is a
#: digest, an id, or a colour as often as it is a commit.
SHA = re.compile(r"`([0-9a-f]{7,40})`")

#: "Stages 1, 3 and 5 remain" and its neighbours — the sentence shape that was
#: wrong for eight days. Deliberately narrow: it must name stage numbers AND
#: carry an open-verb within the same sentence (no `.` between them), so prose
#: that merely mentions a stage cannot be read as a claim about it.
REMAIN_CLAIM = re.compile(
    r"[Ss]tages?\s+"
    r"((?:[A-Za-z]?\d+[a-z]?)(?:\s*(?:,|and|/|–|-)\s*(?:[A-Za-z]?\d+[a-z]?))*)"
    r"[^.]{0,60}?\b(remain|remains|are open|still open|outstanding|not started"
    r"|not built|not done)\b"
)

STAGE_TOKEN = re.compile(r"[A-Za-z]?\d+[a-z]?")


class Stage:
    def __init__(self, name: str, heading: str, body: str) -> None:
        self.name = name
        self.heading = heading
        self.body = body

    @property
    def claims_done(self) -> bool:
        return DONE_CLAIM.search(self.heading) is not None

    @property
    def cited(self) -> list[str]:
        return SHA.findall(self.body)


def stage_sections(text: str) -> list[Stage]:
    """Every ``Stage``/``Track`` heading and the text under it.

    A section runs to the next heading of ANY level, so a stage's own
    sub-headings end it — deliberately: a sha under a sub-heading belongs to
    that sub-claim, and lending it to the parent stage is how a gate starts
    accepting a neighbour's evidence.
    """

    marks = list(HEADING.finditer(text))
    sections: list[Stage] = []
    for index, mark in enumerate(marks):
        heading = mark.group(1).strip()
        named = STAGE_HEADING.match(heading.lstrip("`"))
        if named is None:
            continue
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        sections.append(Stage(named.group(1).lower(), heading, text[mark.start() : end]))
    return sections


def remain_claims(text: str) -> list[tuple[list[str], str]]:
    """``([stage names], the open-verb)`` for every "stages N remain" sentence."""

    return [
        ([token.lower() for token in STAGE_TOKEN.findall(match.group(1))], match.group(2))
        for match in REMAIN_CLAIM.finditer(text)
    ]


def _git(*args: str) -> subprocess.CompletedProcess:
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, check=False, capture_output=True, text=True
    )


def _in_this_history(sha: str, _cache: dict[str, bool] = {}) -> bool:
    """Is ``sha`` an ancestor of HEAD in THIS repo?

    Cached because a docket corpus cites the same commit from many rows and each
    answer costs a process.
    """

    if sha not in _cache:
        _cache[sha] = _git("merge-base", "--is-ancestor", sha, "HEAD").returncode == 0
    return _cache[sha]


def _dockets() -> list[str]:
    completed = _git("ls-files", "--", f"{DOCKET_ROOT}/*.md")
    if completed.returncode != 0:
        pytest.skip(f"git ls-files failed: {completed.stderr.strip()}")
    return sorted(row.strip() for row in completed.stdout.splitlines() if row.strip())


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_bytes().decode("utf-8", "replace")


# ── the rule, falsified on fabricated dockets ────────────────────────────────


FABRICATED = """# Planned — a retirement

**Status:** Stages 1 and 3 remain. **Owner domain:** 08.

## Convictions

### Stage 1 — the duplicate constant — **EXECUTED `deadbeef`**

- WHAT: two definition sites.

### Stage 2 — the second one — **OPEN**

- WHAT: not yet.
"""


def test_the_stage_walk_reads_the_heading_not_the_prose():
    stages = stage_sections(FABRICATED)

    assert [stage.name for stage in stages] == ["1", "2"]
    assert stages[0].claims_done
    assert not stages[1].claims_done
    assert stages[0].cited == ["deadbeef"]
    assert stages[1].cited == []


def test_a_status_header_naming_a_landed_stage_is_the_contradiction():
    """The incident, as a rule. `Stage 1` is recorded EXECUTED by its own
    heading, and the header still lists it as remaining."""

    claims = remain_claims(FABRICATED)
    landed = {stage.name for stage in stage_sections(FABRICATED) if stage.claims_done}

    assert claims == [(["1", "3"], "remain")]
    assert [name for names, _ in claims for name in names if name in landed] == ["1"]


def test_a_sentence_that_only_mentions_a_stage_is_not_a_claim():
    """The narrow-by-construction half: an open-verb in a LATER sentence must
    not be lent to the stage named in this one, or every docket contradicts
    itself the moment it describes its own history."""

    assert remain_claims("Stage 4 shipped first. Everything else remains.") == []


def test_a_stage_heading_with_no_verdict_is_not_read_as_done():
    assert not stage_sections("### Stage 9 — the third one\n\nbody\n")[0].claims_done


# ── the live corpus ──────────────────────────────────────────────────────────


def test_ancestry_is_the_question_asked_not_existence():
    """The decision that keeps this gate from inventing 56 findings.

    These docs cite the launcher's commits by design, and a launcher sha is a
    real commit that is not in THIS history. `git cat-file -e` cannot tell it
    from a mistyped one; ancestry of HEAD can, and answers the only question a
    hermes-side gate is entitled to ask.
    """

    head = _git("rev-parse", "HEAD").stdout.strip()

    assert _in_this_history(head)
    assert _in_this_history(head[:9])
    # A well-formed sha that is not a commit here — the shape every cross-repo
    # cite in these docs has.
    assert not _in_this_history("0" * 40)


def test_a_done_claim_whose_only_sha_is_foreign_is_unbacked():
    """The live gate's rule, run over a fabricated docket because the live
    corpus carries no case — 7 of 7 done-claims are backed at `3d3a33be3e`."""

    stage = stage_sections(FABRICATED)[0]

    assert stage.claims_done
    assert stage.cited == ["deadbeef"]
    assert not any(_in_this_history(sha) for sha in stage.cited)


def test_the_walk_finds_the_dockets_and_the_stages_it_exists_to_gate():
    """Fails loud on an empty set, three ways. A scan that resolves nothing is
    this project's most repeated defect, and it is indistinguishable from a
    passing gate."""

    dockets = _dockets()
    stages = [stage for path in dockets for stage in stage_sections(_read(path))]
    done = [stage for stage in stages if stage.claims_done]

    assert len(dockets) >= 60, len(dockets)
    assert len(stages) >= 30, len(stages)
    assert len(done) >= 5, len(done)


def test_every_stage_that_says_it_shipped_names_a_commit_that_landed_here():
    """A verdict with no landing commit in THIS history is an unbacked claim —
    the shape a triage reads as settled and reschedules around."""

    unbacked = []
    for path in _dockets():
        for stage in stage_sections(_read(path)):
            if not stage.claims_done:
                continue
            if not any(_in_this_history(sha) for sha in stage.cited):
                unbacked.append(f"{path}  Stage {stage.name}: {stage.heading[:80]}")

    assert unbacked == [], (
        "these stage headings declare the stage shipped and cite no commit that "
        "is an ancestor of HEAD in this repo. Cite the landing commit, or say "
        "what actually happened:\n  " + "\n  ".join(sorted(unbacked))
    )


def test_no_docket_lists_a_stage_as_remaining_that_its_own_rows_record_as_landed():
    """The eight-day defect. The `landed` side is validated against git, so the
    contradiction cannot be resolved by editing the stage row to agree with the
    header — which is the failure mode a two-document gate has."""

    stale = []
    for path in _dockets():
        text = _read(path)
        landed = {
            stage.name
            for stage in stage_sections(text)
            if stage.claims_done and any(_in_this_history(sha) for sha in stage.cited)
        }
        for names, verb in remain_claims(text):
            for name in names:
                if name in landed:
                    stale.append(f"{path}  says stage {name} {verb!r}, its own row landed it")

    assert stale == [], (
        "these dockets claim a stage is outstanding while their own stage row "
        "records it landed on a commit in this history:\n  " + "\n  ".join(sorted(stale))
    )
