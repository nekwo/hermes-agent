"""``agent_runtime.build_identity`` — RS-6's code tree, and the rule that names it.

The property under test: **a landing that touched nothing the runtime executes
must not change the digest.** RL-20's build-behind restart compares the runtime's
build against the checkout's, and until this module existed it compared
``git rev-parse HEAD`` — so the 2026-09-07 16:25:09Z field restart fired on a
DOCS-ONLY landing, drained a healthy runtime, and cost the session its socket
(plan ``restart-drain-fence.md`` §0). The digest here is the same measurement
narrowed to the files a running hermes can actually load.

The second property is cross-repo: the launcher computes this digest in Dart
over the same checkout, and the two answers have to be the same string or RL-20
restarts forever. ``code_tree_parity_tree.txt`` is the shared fixture that pins
that — byte-equal in both repos, hashed to the same pinned digest on both sides.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from agent_runtime.build_identity import (
    CODE_TREE_TIMEOUT_SECONDS,
    NON_RUNTIME_PREFIXES,
    NON_RUNTIME_ROOT_SUFFIXES,
    CodeTree,
    code_tree_digest,
    code_tree_for,
    code_tree_rule,
    is_runtime_path,
    parse_ls_tree_z,
    parse_tree_list,
)

#: The shared parity fixture. The launcher's byte-equal copy is
#: ``EterniaLauncher/test/fixtures/build_identity/code_tree_parity_tree.txt``.
PARITY_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "build_identity"
    / "code_tree_parity_tree.txt"
)

#: The digest both repos must produce for :data:`PARITY_FIXTURE`.
#:
#: A pinned constant is normally circular — it is the code under test answering
#: for itself. It is not circular for the job it does here, which is CROSS-REPO:
#: the launcher's Dart pins this same string against its own copy of the fixture,
#: so a change to either implementation's filtering, sorting or framing reds on
#: one side and the two can never drift silently. Correctness of the rule itself
#: is pinned by the filtering and ordering tests below, not by this number.
PARITY_DIGEST = "10272b75505713833a1fb812e706b961e1d43a50"

#: The bytes of the fixture, so "byte-equal in both repos" is a measurement.
#: The launcher pins the same sha256 over its own copy.
PARITY_FIXTURE_SHA256 = (
    "2915319d2107307915eb6d4c9c3abf67f9428842a47e17866525b9f6a71b9a11"
)


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def real_repo(tmp_path):
    """A genuine one-commit repo — ``code_tree_for`` runs real ``git ls-tree``."""

    root = tmp_path / "repo"
    (root / "agent_runtime").mkdir(parents=True)
    (root / "docs").mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tree@test")
    _git(root, "config", "user.name", "tree")
    (root / "agent_runtime" / "runtime.py").write_bytes(b"one\n")
    (root / "docs" / "note.md").write_bytes(b"prose\n")
    (root / "README.md").write_bytes(b"root prose\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "one")
    return root


# ── The rule ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "docs/agent-runtime-harness/00-index.md",
        "tests/agent_runtime/test_build_identity.py",
        ".github/workflows/ci.yml",
        "README.md",
        "AGENTS.md",
        "CHANGELOG.md",
    ],
)
def test_the_rule_drops_prose_and_ci_and_the_repo_root_markdown(path):
    assert is_runtime_path(path) is False


@pytest.mark.parametrize(
    "path",
    [
        "agent_runtime/build_identity.py",
        "hermes_cli/harness_parts/serve.py",
        "scripts/run_tier.ps1",
        # Markdown BELOW the root is runtime data — the skills tree ships
        # ``SKILL.md`` files a live runtime reads and acts on, so a rule that
        # dropped every ``.md`` would call a skill edit a docs landing.
        "agent_runtime/skills/README.md",
        "skills/runtime-model/SKILL.md",
        # A directory whose NAME starts with a dropped prefix is not that
        # prefix: ``docsite/`` is code and ``tests_support/`` is code.
        "docsite/serve.py",
        "tests_support/helper.py",
    ],
)
def test_the_rule_keeps_everything_a_runtime_can_load(path):
    assert is_runtime_path(path) is True


def test_the_rule_is_published_so_a_reader_applies_the_same_one():
    rule = code_tree_rule()

    assert rule == {
        "prefixes": ["docs/", "tests/", ".github/"],
        "root_suffixes": [".md"],
    }
    assert rule["prefixes"] == list(NON_RUNTIME_PREFIXES)
    assert rule["root_suffixes"] == list(NON_RUNTIME_ROOT_SUFFIXES)
    # A fresh list every call: a reader that mutates what it was handed must
    # not be able to change what the next reader is told.
    rule["prefixes"].append("agent_runtime/")
    assert code_tree_rule()["prefixes"] == list(NON_RUNTIME_PREFIXES)


# ── The digest ──────────────────────────────────────────────────────────────


def test_two_trees_that_differ_only_under_docs_hash_equal():
    before = [
        ("agent_runtime/serve_socket.py", "a" * 40),
        ("docs/agent-runtime-harness/03-transport-and-wire.md", "b" * 40),
    ]
    after = [
        ("agent_runtime/serve_socket.py", "a" * 40),
        ("docs/agent-runtime-harness/03-transport-and-wire.md", "c" * 40),
        ("docs/mission_control/planned/new-plan.md", "d" * 40),
        ("CHANGELOG.md", "e" * 40),
        ("tests/agent_runtime/test_serve_socket_drain_wait.py", "f" * 40),
        (".github/workflows/ci.yml", "0" * 40),
    ]

    assert code_tree_digest(before) == code_tree_digest(after)


def test_a_tree_that_differs_under_agent_runtime_hashes_differently():
    before = [("agent_runtime/serve_socket.py", "a" * 40)]
    after = [("agent_runtime/serve_socket.py", "b" * 40)]

    assert code_tree_digest(before) != code_tree_digest(after)


def test_an_added_runtime_file_moves_the_digest_and_a_removed_one_moves_it_back():
    base = [("agent_runtime/serve_socket.py", "a" * 40)]
    grown = base + [("agent_runtime/build_identity.py", "b" * 40)]

    assert code_tree_digest(base) != code_tree_digest(grown)
    assert code_tree_digest(base) == code_tree_digest(list(base))


def test_the_digest_is_order_independent_and_sorts_by_the_paths_utf8_bytes():
    # ``Z`` (0x5A) sorts before ``a`` (0x61) in UTF-8 bytes, and ``_`` (0x5F)
    # between them — the ordering Dart's default string compare also gives for
    # ASCII, which is why the fixture parity holds across the two languages.
    entries = [
        ("agent_runtime/a.py", "1" * 40),
        ("Zulu/z.py", "2" * 40),
        ("_scratch/s.py", "3" * 40),
    ]

    assert code_tree_digest(entries) == code_tree_digest(list(reversed(entries)))
    assert code_tree_digest(entries) == code_tree_digest(sorted(entries))


def test_a_blob_is_compared_case_insensitively_because_git_hex_is_lowercase():
    assert code_tree_digest([("agent_runtime/a.py", "AB" * 20)]) == code_tree_digest(
        [("agent_runtime/a.py", "ab" * 20)]
    )


def test_an_empty_tree_is_the_empty_digest_and_not_an_error():
    assert code_tree_digest([]) == hashlib.sha1(b"").hexdigest()
    assert code_tree_digest([("docs/x.md", "a" * 40)]) == hashlib.sha1(b"").hexdigest()


# ── The parity fixture ──────────────────────────────────────────────────────


def test_the_shared_fixture_hashes_to_the_digest_the_launcher_pins():
    entries = parse_tree_list(PARITY_FIXTURE.read_bytes().decode("utf-8"))

    assert len(entries) == 10
    assert code_tree_digest(entries) == PARITY_DIGEST


def test_the_shared_fixture_is_byte_equal_to_the_launchers_copy():
    """The launcher pins this same sha256 over its own copy.

    Two files in two repos cannot be compared by a test that can only see one
    of them, so the byte-equality is carried by a digest each side pins
    independently — the same mechanism as :data:`PARITY_DIGEST`, one level down.
    The launcher's copy is
    ``EterniaLauncher/test/fixtures/build_identity/code_tree_parity_tree.txt``.
    """

    raw = PARITY_FIXTURE.read_bytes()

    assert b"\r" not in raw, "the fixture is LF in both repos or it is not byte-equal"
    assert hashlib.sha256(raw).hexdigest() == PARITY_FIXTURE_SHA256


def test_the_fixture_names_both_repos_copies_so_an_editor_finds_the_other():
    header = PARITY_FIXTURE.read_bytes().decode("utf-8")

    assert "hermes-agent/tests/fixtures/build_identity/" in header
    assert "EterniaLauncher/test/fixtures/build_identity/" in header


# ── Parsing ─────────────────────────────────────────────────────────────────


def test_the_ls_tree_reader_takes_the_blob_and_the_path_and_nothing_else():
    raw = (
        "100644 blob 1111111111111111111111111111111111111111\tagent_runtime/a.py\0"
        "100755 blob 2222222222222222222222222222222222222222\tscripts/run.sh\0"
    )

    assert parse_ls_tree_z(raw) == [
        ("agent_runtime/a.py", "1" * 40),
        ("scripts/run.sh", "2" * 40),
    ]


def test_a_path_with_a_space_survives_the_reader():
    raw = "100644 blob 3333333333333333333333333333333333333333\tagent_runtime/two words.py\0"

    assert parse_ls_tree_z(raw) == [("agent_runtime/two words.py", "3" * 40)]


def test_the_tree_list_reader_skips_comments_and_blank_lines():
    text = "# a header\n\nagent_runtime/a.py\t" + "4" * 40 + "\n"

    assert parse_tree_list(text) == [("agent_runtime/a.py", "4" * 40)]


# ── Against real git ────────────────────────────────────────────────────────


def test_a_real_checkout_answers_a_digest_that_ignores_its_prose(real_repo):
    first = code_tree_for(real_repo)

    assert isinstance(first, CodeTree)
    assert first.reason == ""
    assert first.code_tree is not None
    assert first.entry_count == 1

    (real_repo / "docs" / "note.md").write_bytes(b"more prose\n")
    (real_repo / "README.md").write_bytes(b"more root prose\n")
    _git(real_repo, "add", "-A")
    _git(real_repo, "commit", "-qm", "docs only")

    assert _git(real_repo, "rev-parse", "HEAD") != _git(
        real_repo, "rev-parse", "HEAD~1"
    )
    assert code_tree_for(real_repo).code_tree == first.code_tree

    (real_repo / "agent_runtime" / "runtime.py").write_bytes(b"two\n")
    _git(real_repo, "add", "-A")
    _git(real_repo, "commit", "-qm", "runtime")

    assert code_tree_for(real_repo).code_tree != first.code_tree


def test_a_directory_that_is_not_a_repo_is_a_typed_reason_never_a_raise(tmp_path):
    result = code_tree_for(tmp_path)

    assert result.code_tree is None
    assert result.reason == "git_failed"
    assert result.entry_count is None


def test_a_missing_git_binary_is_reported_not_raised(real_repo, monkeypatch):
    def _no_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", _no_git)

    result = code_tree_for(real_repo)

    assert result.code_tree is None
    assert result.reason == "git_missing"


def test_a_hung_git_is_a_timeout_and_the_bound_is_a_named_constant(
    real_repo, monkeypatch
):
    def _hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=CODE_TREE_TIMEOUT_SECONDS)

    monkeypatch.setattr(subprocess, "run", _hang)

    result = code_tree_for(real_repo)

    assert result.code_tree is None
    assert result.reason == "git_timeout"
    # Wider than ``build_stamp``'s two seconds ON PURPOSE: this probe reads
    # every tracked path in the tree (9,000+ on this repo) where the other
    # reads one line, and a boot-path timeout that fires on a cold file cache
    # would publish "no code tree" for a checkout that has one.
    assert CODE_TREE_TIMEOUT_SECONDS >= 8.0
