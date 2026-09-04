"""A fixture family pinned by a `MANIFEST.sha256` must be pinned by `-text` too.

The hermes half of the cross-repo byte-pin pair. The launcher shipped its half
on 2026-09-04 (`test/architecture/manifest_sha256_siblings_text_test.dart`),
which enumerates its own `MANIFEST.sha256` files and requires `git check-attr
text` to resolve `unset` for each. This is the mirror, and it is not decorative:
every family under `tests/fixtures/` that carries a manifest is committed
BYTE-IDENTICAL to a launcher copy, and the digest is what proves it.

WHY A DIGEST NEEDS AN ATTRIBUTE
==============================

`sha256` is a claim about bytes. `.gitattributes`' default `* text=auto` lets
git normalize end-of-line on check-in and convert on checkout, so a family with
a manifest and no `-text` rule has two different things called "the fixture":
what the object store holds, and what a Windows checkout with
`core.autocrlf=true` puts on disk. The digest can then be right on one machine
and wrong on the next for a file nobody edited — a red that reads as a contract
break and is a checkout setting. `-text` disables conversion in BOTH directions,
so the bytes on disk are the bytes in the object store are the bytes the other
repo committed.

`tests/test_line_endings.py` does not answer this. It reads the INDEX and asks
whether a blob carries CR; a family that is LF everywhere today passes it while
still being one contributor's `core.autocrlf` away from a mismatch, and the
attribute is what forecloses that.

MEASURED AT `3d3a33be3e`, before this gate
==========================================

Three tracked `MANIFEST.sha256` files: `tests/fixtures/office_layout/` (pinned),
`tests/fixtures/response_envelopes/` (NOT pinned) and
`tests/fixtures/stream_frames/` (NOT pinned). Both unpinned families state in
their own README that the launcher commits byte-identical copies —
`test/fixtures/hermes_responses/` and `test/fixtures/harness_stream/` — and the
launcher has now pinned both of those. So the hermes side was the half that
could still drift.

WHAT THIS GATE DOES NOT DO
==========================

It does not verify a digest — `test_response_contract_fixture.py` and the
stream-frame contract tests own that. It asserts only that the family is
DECLARED byte-exact, which is the precondition those tests rest on.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent

#: What `git check-attr text` prints for a path carrying a `-text` rule.
BYTE_PINNED = "unset"


def _git(*args: str) -> str:
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    completed = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, check=False, capture_output=True, text=True
    )
    if completed.returncode != 0:
        pytest.skip(f"git {args[0]} failed: {completed.stderr.strip()}")
    return completed.stdout


def _manifests() -> list[str]:
    return sorted(
        row.strip()
        for row in _git("ls-files", "--", "*/MANIFEST.sha256").splitlines()
        if row.strip()
    )


def _pinned_siblings(manifest: str) -> list[str]:
    """The paths a manifest names, resolved against its own directory.

    `<sha256>  <name>` per line, the format `sha256sum` writes and the format
    both repos' generators emit.
    """

    directory = manifest.rsplit("/", 1)[0]
    names: list[str] = []
    for row in (REPO_ROOT / manifest).read_text(encoding="utf-8").splitlines():
        _, _, name = row.strip().partition("  ")
        if name:
            names.append(f"{directory}/{name.strip()}")
    return names


def _text_attribute(paths: list[str]) -> dict[str, str]:
    """`path -> the value `text` resolves to`, in ONE git call.

    `git check-attr` takes every path at once; asking per file spawns a process
    per fixture and this family list only grows.
    """

    if not paths:
        return {}
    output = _git("check-attr", "text", "--", *paths)
    resolved: dict[str, str] = {}
    for row in output.splitlines():
        path, _, rest = row.rpartition(": text: ")
        if path:
            resolved[path] = rest.strip()
    return resolved


def test_the_walk_finds_the_manifests_it_exists_to_gate():
    """An enumerating gate that enumerates nothing is indistinguishable from a
    passing one — the same refusal `scripts/doc_cite_adjacency.py` makes on a
    zero-cite walk."""

    manifests = _manifests()

    assert len(manifests) >= 3, (
        "`git ls-files -- '*/MANIFEST.sha256'` found fewer families than this "
        f"repo carried when the gate was written: {manifests}"
    )


def test_every_manifest_sha256_is_itself_byte_pinned():
    """The manifest is a fixture too — a digest list whose own bytes are
    mirrored into the other repo."""

    manifests = _manifests()
    resolved = _text_attribute(manifests)
    unpinned = sorted(
        f"{path} -> text: {resolved.get(path, '<unresolved>')}"
        for path in manifests
        if resolved.get(path) != BYTE_PINNED
    )

    assert unpinned == [], (
        "these `MANIFEST.sha256` files carry a sha256 claim about bytes with no "
        "`-text` rule to keep the bytes still. Add a `<dir>/** -text` rule to "
        ".gitattributes with the reason:\n  " + "\n  ".join(unpinned)
    )


def test_every_file_a_manifest_pins_is_byte_pinned():
    """The manifest's own pin is not enough: the digest is about the SIBLINGS,
    and a directory rule is what covers a family that grows a file."""

    siblings: list[str] = []
    for manifest in _manifests():
        siblings.extend(_pinned_siblings(manifest))
    resolved = _text_attribute(siblings)
    unpinned = sorted(
        f"{path} -> text: {resolved.get(path, '<unresolved>')}"
        for path in siblings
        if resolved.get(path) != BYTE_PINNED
    )

    assert siblings, "no manifest named a single file — the parse is wrong"
    assert unpinned == [], (
        "these files are digest-pinned by a `MANIFEST.sha256` beside them but "
        "are still subject to end-of-line conversion, so their committed bytes "
        "and their checked-out bytes can differ:\n  " + "\n  ".join(unpinned)
    )
