"""The mutation gate's CI cap is one number, and the README may not re-type it.

`tool/test_quality/README.md` tells a reader which cap each lane enforces, and
its own opening paragraph records why that is worth a gate: *"this paragraph
said 12 while the gate ran 16, then said 16 while CI ran 20."* Twice wrong, both
times silently — a prose number beside a machine number is a fact with no
reader, and the doc that explains a gate is exactly where a wrong number is
most expensive.

So this test reads BOTH sides and compares them. Neither number is written
here: hand-typing either one would make this file the third place to keep in
sync, which is the failure it exists to close. The workflow is parsed as YAML
(comments dropped, so the prose *around* the call site — which legitimately
names the older caps 12 and 16 — cannot be mistaken for the live one), and the
README is read for the bullet that claims what CI passes.

The pair this pins is deliberately narrow: **the CI lane only.** The README's
other two numbers (the per-stage default and the landing cap) are not a
workflow's to state — the default lives in the script's argparse and the
landing cap is a house convention — and inventing a source of truth for them
here would be inventing a claim.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"
README = REPO_ROOT / "tool" / "test_quality" / "README.md"

JOB_ID = "mutation-claims"
GATE_SCRIPT = "changed_line_mutation_check.py"
CAP = re.compile(r"--max-candidates[=\s]+(\d+)")


def workflow_cap() -> int:
    """The `--max-candidates` CI actually passes, read from the job that runs."""

    yaml = pytest.importorskip("yaml")
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = document["jobs"][JOB_ID]
    # The job has more than one step touching the gate (a `--list` selection step
    # that takes no cap, then the enforcing run), so the cap is looked for across
    # the steps and required to be unambiguous rather than assumed to be last.
    found = [
        int(match.group(1))
        for step in job["steps"]
        for match in [CAP.search(str(step.get("run") or ""))]
        if match is not None and GATE_SCRIPT in str(step.get("run") or "")
    ]
    assert len(found) == 1, f"expected exactly one capped {GATE_SCRIPT} run: {found}"
    return found[0]


def readme_cap() -> int:
    """The cap the README tells a reader CI passes."""

    lines = README.read_text(encoding="utf-8").splitlines()
    claims = [
        line for line in lines if "**CI**" in line and JOB_ID in line and CAP.search(line)
    ]
    assert len(claims) == 1, (
        f"expected exactly one README line claiming the {JOB_ID} cap; found "
        f"{len(claims)}. If the paragraph was rewritten, keep one line that "
        f"names **CI**, the job id and the flag — that shape is what this test "
        f"reads."
    )
    return int(CAP.search(claims[0]).group(1))


def test_the_readme_quotes_the_cap_the_workflow_actually_passes():
    """The row, and the only assertion that matters: one number, two places."""

    assert readme_cap() == workflow_cap()


def test_the_workflow_job_this_pin_reads_still_exists():
    """ANTI-VACUITY. A renamed job or a deleted step would make the comparison
    above pass over nothing, so the source side is asserted to be real before
    it is trusted — the same reason the adjacency probe is FATAL on a zero-cite
    walk."""

    yaml = pytest.importorskip("yaml")
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))

    assert JOB_ID in document["jobs"], f"{JOB_ID} job is gone; re-point this pin"
    assert any(
        GATE_SCRIPT in str(step.get("run") or "")
        for step in document["jobs"][JOB_ID]["steps"]
    ), f"no step in {JOB_ID} runs {GATE_SCRIPT}"
