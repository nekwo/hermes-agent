"""Which CODE — not which COMMIT — is this checkout? RS-6's code tree.

Why this exists
---------------

``agent_runtime/build_stamp.py`` answers *"what commit did this interpreter
load"*, and the launcher's RL-20 build-behind restart used to compare that
commit against the checkout's ``git rev-parse HEAD``. On 2026-09-07 at
16:25:09Z that comparison drained a perfectly healthy runtime because the
hermes head had moved — by a **docs-only landing**. The replacement started
while the first was still leaving, lost the socket lock for good, and the
session spent the rest of its life on the argv lane with `bridge stopped` on
the sheet (plan ``EterniaLauncher/docs/mission_control/planned/restart-drain-fence.md``
§0, ruling RS-6).

A commit is the wrong question. The right one is *"does this checkout contain
code the running runtime is not running"*, and that is answerable: hash the
tracked files a runtime can actually load and compare the hashes. A landing
that touched only prose, tests or CI does not move the digest, so RL-20 does
not restart, so a docs push cannot cost the operator their socket.

The rule, exactly
-----------------

``code_tree_for`` runs ``git ls-tree -r -z HEAD``, drops every entry the rule
below rejects, sorts what is left by the path's UTF-8 bytes, and takes a sha1
over ``<path>\\0<blob>\\n`` per entry. Dropped:

* anything under :data:`NON_RUNTIME_PREFIXES` — ``docs/``, ``tests/``,
  ``.github/`` — matched as a literal path prefix, so ``docsite/serve.py`` and
  ``tests_support/helper.py`` are KEPT (they are directories whose names begin
  with those letters, not those directories);
* **repo-root markdown**: a path containing no ``/`` at all whose name ends,
  case-insensitively, in a suffix from :data:`NON_RUNTIME_ROOT_SUFFIXES`
  (``.md``). ``README.md`` and ``AGENTS.md`` go; ``agent_runtime/skills/README.md``
  and ``skills/runtime-model/SKILL.md`` stay, because a live runtime reads and
  acts on the skills tree and a rule that dropped every ``.md`` would report a
  skill edit as a docs landing.

Both halves ride the register row as ``build.code_tree_rule`` (see
:func:`code_tree_rule`), so the launcher applies the rule it was HANDED rather
than a second copy of it that can drift. Two implementations still exist — one
Python, one Dart — and the shared fixture
``tests/fixtures/build_identity/code_tree_parity_tree.txt`` (byte-equal in
``EterniaLauncher/test/fixtures/build_identity/``) is what pins them to one
answer.

What the digest deliberately does NOT cover
-------------------------------------------

* **File modes.** ``ls-tree`` reports them and this hash ignores them: a
  ``chmod +x`` is not code a runtime loads differently.
* **Untracked and uncommitted work.** The digest is of ``HEAD``, exactly as the
  commit comparison was — RL-20 must not restart the runtime on every keystroke
  in the checkout.
* **A non-git source.** No repo, no ``git``, a Docker image built from a baked
  sha: the digest is ``None`` with a typed reason, never fabricated. A
  fabricated digest is a well-formed wrong answer, which is the class
  ``build_stamp``'s whole contract exists to refuse.

Contract
--------

**Never raises.** Every failure yields a :class:`CodeTree` whose ``code_tree``
is ``None`` and whose ``reason`` is a typed token. This runs on the boot path;
an instrument may not be why a boot did not happen.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "CODE_TREE_TIMEOUT_SECONDS",
    "NON_RUNTIME_PREFIXES",
    "NON_RUNTIME_ROOT_SUFFIXES",
    "CodeTree",
    "code_tree_digest",
    "code_tree_for",
    "code_tree_rule",
    "is_runtime_path",
    "parse_ls_tree_z",
    "parse_tree_list",
]

#: Path prefixes whose contents no running hermes loads. Literal prefixes, each
#: ending in ``/`` so a directory NAME that merely starts with these letters is
#: not swept up with it. Defined once, here, and published on the register row.
NON_RUNTIME_PREFIXES: tuple[str, ...] = ("docs/", "tests/", ".github/")

#: Suffixes that make a REPO-ROOT file non-runtime — a path with no ``/`` in it
#: whose name ends in one of these, matched case-insensitively. The root of this
#: repo carries ``README.md``, ``AGENTS.md`` and their siblings, which are the
#: docs that do not live under ``docs/``; markdown deeper in the tree is data a
#: runtime reads (the skills tree) and stays.
NON_RUNTIME_ROOT_SUFFIXES: tuple[str, ...] = (".md",)

#: Hard bound on the ``ls-tree`` probe. Four times ``build_stamp``'s
#: ``GIT_TIMEOUT_SECONDS`` because this reads every tracked path in the tree
#: (9,000+ here) where that one reads a single line, and a bound that fires on
#: a cold file cache would publish "this checkout has no code tree" for a
#: checkout that has one.
CODE_TREE_TIMEOUT_SECONDS = 8.0


@dataclass(frozen=True, slots=True)
class CodeTree:
    """The digest of a checkout's runtime files, and how confidently we know it."""

    #: 40-char sha1 hex, or None when it could not be measured.
    code_tree: str | None
    #: Empty when resolved; otherwise a typed token (``git_missing``,
    #: ``git_timeout``, ``git_failed``, ``git_error``).
    reason: str
    #: How many entries survived the rule. None when nothing was measured — a
    #: zero would read as "this checkout has no code", which is a different
    #: (and alarming) fact.
    entry_count: int | None


def code_tree_rule() -> dict[str, Any]:
    """The rule itself, in the shape that rides the register row.

    A fresh dict of fresh lists every call: this value is handed to a reader
    (the launcher, over the wire) and a reader that mutated what it was given
    must not be able to change what the next one is told.
    """

    return {
        "prefixes": list(NON_RUNTIME_PREFIXES),
        "root_suffixes": list(NON_RUNTIME_ROOT_SUFFIXES),
    }


def is_runtime_path(path: str) -> bool:
    """Does *path* name a file a running hermes can load? See the module doc."""

    if not path:
        return False
    for prefix in NON_RUNTIME_PREFIXES:
        if path.startswith(prefix):
            return False
    if "/" not in path:
        lowered = path.lower()
        for suffix in NON_RUNTIME_ROOT_SUFFIXES:
            if lowered.endswith(suffix):
                return False
    return True


def code_tree_digest(entries: Iterable[tuple[str, str]]) -> str:
    """sha1 over the sorted ``(path, blob)`` pairs that survive the rule.

    The framing is ``<path>\\0<blob>\\n`` per entry, in ascending order of the
    path's UTF-8 bytes. Both halves matter and both are mirrored in Dart: the
    NUL keeps a path from running into a blob, and sorting by bytes rather than
    by the host language's collation is what makes two languages agree.

    An empty result is the sha1 of the empty string, not an error: a tree of
    nothing but prose is a real (if improbable) answer, and refusing to name it
    would make "no runtime files" indistinguishable from "git did not answer".
    """

    kept = [
        (path, blob.strip().lower())
        for path, blob in entries
        if is_runtime_path(path)
    ]
    kept.sort(key=lambda pair: pair[0].encode("utf-8"))
    digest = hashlib.sha1()
    for path, blob in kept:
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(blob.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def parse_ls_tree_z(raw: str) -> list[tuple[str, str]]:
    """Read ``git ls-tree -r -z`` output into ``(path, blob)`` pairs.

    ``-z`` rather than the default: without it git C-quotes any path with a
    space, a quote or a non-ASCII byte in it, and a digest computed over quoted
    paths on one machine and unquoted ones on another is two different digests
    for one tree.

    Every record ``-r`` returns participates, gitlinks included: a submodule's
    commit id IS code identity, and a rule that skipped it would call a
    submodule bump a docs landing.
    """

    entries: list[tuple[str, str]] = []
    for record in raw.split("\0"):
        if not record:
            continue
        head, tab, path = record.partition("\t")
        if not tab or not path:
            continue
        fields = head.split()
        if len(fields) < 3:
            continue
        entries.append((path, fields[2]))
    return entries


def parse_tree_list(text: str) -> list[tuple[str, str]]:
    """Read the ``<path>\\t<blob>`` fixture format. Blank and ``#`` lines skipped."""

    entries: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        path, tab, blob = line.partition("\t")
        if not tab:
            continue
        entries.append((path, blob.strip()))
    return entries


def code_tree_for(root: Path | str, head: str = "HEAD") -> CodeTree:
    """The code tree of *head* in the checkout at *root*. Never raises.

    RS-6 spells this ``code_tree_for(head)``; the checkout has to be named too,
    because ``build_stamp`` already resolved one and this module refuses to
    resolve a second (see ``repo_root_for`` there — one walker, one answer).
    """

    try:
        completed = subprocess.run(
            ["git", "ls-tree", "-r", "-z", head],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CODE_TREE_TIMEOUT_SECONDS,
            # stdin pinned to the null device for the reason ``build_stamp``
            # states at length: a child inheriting serve's stdin pipe blocks
            # forever against the Launcher's open pipe.
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"},
        )
    except FileNotFoundError:
        return CodeTree(code_tree=None, reason="git_missing", entry_count=None)
    except subprocess.TimeoutExpired:
        return CodeTree(code_tree=None, reason="git_timeout", entry_count=None)
    except Exception:  # pragma: no cover - defensive; must never raise
        return CodeTree(code_tree=None, reason="git_error", entry_count=None)
    if completed.returncode != 0:
        return CodeTree(code_tree=None, reason="git_failed", entry_count=None)

    entries = parse_ls_tree_z(completed.stdout or "")
    kept = [pair for pair in entries if is_runtime_path(pair[0])]
    return CodeTree(
        code_tree=code_tree_digest(entries),
        reason="",
        entry_count=len(kept),
    )
