"""Report documentation citations that no longer resolve. ADVISORY — never a gate.

Two rot classes, both measured and both older than any one wave:

* **Line cites.** 348 `file.py:N` citations across
  `docs/agent-runtime-harness`; four were measured pointing at unrelated text
  as far back as `76c6ade663`, and a ±3-line adjacency probe failed on 61 of
  the 91 machine-checkable ones. Line numbers into live modules drift on every
  refactor.
* **Sha cites.** The queue carries its own scar: a round-five entry cited
  `d01e5f7bc … 7c35e3963` for months, and those were PRE-REBASE shas —
  present only in a local reflog, not ancestors of `origin/main`, and headed
  for garbage collection. A sha that no longer exists and a sha that exists but
  was never on the mainline are different failures and are reported separately.

**Why this is not a gate, and must not quietly become one.** It lands red on
~60 pre-existing cites. A gate that is born red is turned off, and a gate that
is silenced is worse than no gate because the next reader believes it. Making
it a gate is a separate stage with a sweep budget, and it needs the adjacency
probe this does not attempt.

**What this checks, stated so nobody reads more into a green line than is
there.** Whether the cite RESOLVES: the file is tracked, the line exists, the
sha is a real commit on `origin/main`. It cannot tell whether `office_store
.py:444` is the line the sentence is about — only that there is a line 444.
An adjacency or symbol probe is the harder half and is not here.

Bare filenames (`office_store.py:113`) are resolved through the tracked-file
index and checked only when the basename is UNAMBIGUOUS; ambiguous ones are
counted, not guessed. Guessing which of three `test_office.py` a sentence meant
is how a report starts inventing findings.

A KNOWN false-positive class, named rather than filtered: this repo's docs cite
LAUNCHER shas (`e38bb108c` and friends), which are real commits in the other
repo and unknown here. They land under "not a commit in this clone" and there
is no structural way to tell them from a rotted hermes sha — a reader has to
know. Filtering them by guess would hide the rotted ones.

    python scripts/doc_cite_report.py [--root docs] [--base origin/main]
                                      [--exclude archive/]
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
import re
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]

#: A path-shaped token WITH a line number — `file.py:123` or `file.py:12-20`.
#: The line number is required, and that is the report's whole noise control.
#: A bare `office_store.py` in a sentence is prose, and `gateway/peers.json` is
#: usually a path in the runtime store rather than in this checkout; measured,
#: accepting those produced 498 "dead" rows of which almost none were cites.
#: `:N` is also exactly the class the queue row is about.
#:
#: The extension list is closed on purpose: an open one matches "Stage 4.2".
CITE = re.compile(
    r"(?<![\w/.\-])"
    r"((?:[\w.\-]+/)*[\w.\-]+\.(?:py|dart|md|json|yaml|yml|toml|txt|sh|cfg|ini))"
    r":(\d+)(?:\s*-\s*(\d+))?"
)

#: A sha cite, only inside backticks. Bare hex in prose is far more often a
#: hash of something else — a digest, an id, a colour — and this report is
#: worth nothing if its rows have to be filtered by eye.
SHA = re.compile(r"`([0-9a-f]{7,40})`")


def _git(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        input=stdin,
    )


def _classify_shas(shas: list[str], base: str) -> dict[str, str]:
    """``{sha: "ok" | "offline" | "unknown"}`` for every distinct cite, in TWO spawns.

    The per-sha shape this replaces was ``cat-file -e`` followed by
    ``merge-base --is-ancestor``, two processes per distinct sha. Measured
    2026-09-02 on the Windows dev box: 32.1 s across 356 spawns of a 26.6 /
    40.0 / 46.0 s run, ~90 ms of process creation apiece, against 0.9 s for all
    634 ``_line_count`` reads. Process creation WAS the report; the work it
    wrapped was rounding error.

    Both halves batch without moving a single verdict:

    * existence — ``cat-file --batch-check`` peels every cite through
      ``^{commit}`` on stdin and answers ``<full-oid> commit <size>`` or
      ``<input> missing``. A short sha that is ambiguous, or that names a tree
      or a blob, does not peel and lands under ``missing`` — the same
      ``unknown`` a non-zero ``cat-file -e`` produced;
    * ancestry — the FULL oids ``batch-check`` just handed back go to one
      ``rev-list --stdin`` alongside ``^<base>``, which prints exactly the
      commits reachable from a cite but not from the base. Present in that
      output is ``offline``; absent is ``ok``.

    The full oid is what makes the second call possible at all: a 7-hex cite
    cannot be compared against ``rev-list`` output, and abbreviating the output
    back down would be a second guess. Exclusions ride on stdin as ``^<base>``
    rather than as a command-line ``--not``, so hundreds of oids never approach
    the Windows command-line limit.
    """

    if not shas:
        return {}
    probe = _git("cat-file", "--batch-check", stdin="".join(f"{sha}^{{commit}}\n" for sha in shas))
    if probe.returncode != 0:
        raise RuntimeError(f"git cat-file --batch-check failed: {probe.stderr.strip()}")
    verdicts: dict[str, str] = {}
    oids: dict[str, str] = {}
    for sha, row in zip(shas, probe.stdout.splitlines()):
        if " commit " not in row:
            verdicts[sha] = "unknown"
        else:
            oids[sha] = row.split(" ", 1)[0]
    if not oids:
        return verdicts
    reach = _git(
        "rev-list", "--stdin", stdin="".join(f"{oid}\n" for oid in oids.values()) + f"^{base}\n"
    )
    # A base this clone cannot resolve used to make EVERY `--is-ancestor` probe
    # exit non-zero, which the caller read as "offline". Preserved deliberately:
    # batching is a COST change, and inventing a new failure mode for a bad
    # `--base` belongs to whoever decides this report may exit non-zero.
    not_ancestors = set(reach.stdout.split()) if reach.returncode == 0 else set(oids.values())
    for sha, oid in oids.items():
        verdicts[sha] = "offline" if oid in not_ancestors else "ok"
    return verdicts


def _tracked() -> tuple[set[str], dict[str, list[str]]]:
    completed = _git("ls-files")
    if completed.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {completed.stderr.strip()}")
    paths = {row.strip() for row in completed.stdout.splitlines() if row.strip()}
    by_name: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        by_name[path.rsplit("/", 1)[-1]].append(path)
    return paths, by_name


def _line_count(path: str) -> int:
    try:
        return len((REPO_ROOT / path).read_bytes().decode("utf-8", "replace").splitlines())
    except OSError:
        return 0


def _resolve(token: str, paths: set[str], by_name: dict[str, list[str]]) -> str | None:
    """The tracked path this cite names, or ``None`` when it cannot be settled.

    ``None`` covers both "no such file" and "three files share that basename".
    The caller separates them; this only refuses to guess.
    """

    if token in paths:
        return token
    if "/" in token:
        tail = [path for path in paths if path.endswith("/" + token)]
        return tail[0] if len(tail) == 1 else None
    candidates = by_name.get(token, [])
    return candidates[0] if len(candidates) == 1 else None


def run(root: str, base: str, exclude: list[str]) -> int:
    paths, by_name = _tracked()
    docs = sorted(
        path
        for path in paths
        if path.startswith(root + "/")
        and path.endswith(".md")
        and not any(fragment in path for fragment in exclude)
    )

    dead_path: list[str] = []
    dead_line: list[str] = []
    unknown_sha: list[str] = []
    offline_sha: list[str] = []
    ambiguous = 0
    cross_repo = 0
    checked_cites = 0
    # Sha cites are COLLECTED on the walk and classified after it, in one
    # batch. Order is the walk's, so the rows below come out doc-by-doc and
    # line-by-line exactly as the per-sha probe emitted them.
    sha_hits: list[tuple[str, str]] = []
    distinct_shas: list[str] = []

    for doc in docs:
        text = (REPO_ROOT / doc).read_bytes().decode("utf-8", "replace")
        for number, line in enumerate(text.splitlines(), start=1):
            for match in CITE.finditer(line):
                token, first, last = match.group(1), match.group(2), match.group(3)
                if token.endswith(".dart") or "..." in token:
                    # A launcher path, or one elided for width. Neither is
                    # checkable from this checkout, and reporting it as dead
                    # would be a finding about the wrong repo.
                    cross_repo += 1
                    continue
                resolved = _resolve(token, paths, by_name)
                if resolved is None:
                    if "/" not in token and len(by_name.get(token, [])) > 1:
                        # A bare basename several files share. Guessing which
                        # one the sentence meant is how a report invents rows.
                        ambiguous += 1
                    else:
                        dead_path.append(f"{doc}:{number}  {token}:{first}")
                    continue
                checked_cites += 1
                total = _line_count(resolved)
                wanted = int(last or first)
                if wanted > total:
                    dead_line.append(
                        f"{doc}:{number}  {token}:{first}"
                        f"{'-' + last if last else ''} -> {resolved} has {total} lines"
                    )
            for match in SHA.finditer(line):
                sha = match.group(1)
                if sha not in distinct_shas:
                    distinct_shas.append(sha)
                sha_hits.append((f"{doc}:{number}", sha))

    verdicts = _classify_shas(distinct_shas, base)
    checked_shas = len(sha_hits)
    for where, sha in sha_hits:
        if verdicts[sha] == "unknown":
            unknown_sha.append(f"{where}  {sha}")
        elif verdicts[sha] == "offline":
            offline_sha.append(f"{where}  {sha}")

    print(f"doc-cite report (ADVISORY — this is not a gate) over {len(docs)} docs under {root}/")
    print(f"  line cites resolved      : {checked_cites}")
    print(f"  sha cites checked        : {checked_shas} ({len(distinct_shas)} distinct)")
    print(f"  bare names left ambiguous: {ambiguous}")
    print(f"  cross-repo/elided, skipped: {cross_repo}")
    print("")
    for title, rows in (
        ("PATH DOES NOT RESOLVE", dead_path),
        ("LINE IS PAST THE END OF THE FILE", dead_line),
        ("SHA IS NOT A COMMIT IN THIS CLONE (some are launcher shas)", unknown_sha),
        (f"SHA EXISTS BUT IS NOT AN ANCESTOR OF {base}", offline_sha),
    ):
        print(f"{title}: {len(rows)}")
        for row in rows:
            print(f"  {row}")
        print("")
    # Always 0. See the module docstring: a report that can fail a lane is a
    # gate, and this one is born red.
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="docs")
    parser.add_argument("--base", default="origin/main")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="FRAGMENT",
        help="skip docs whose path contains FRAGMENT (repeatable) — `--exclude "
        "archive/` separates rot in frozen copies from rot in the live canon",
    )
    args = parser.parse_args(argv)
    try:
        return run(args.root.rstrip("/"), args.base, args.exclude)
    except RuntimeError as error:
        print(f"doc-cite report could not run: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
