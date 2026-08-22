"""The skills root is resolved at CALL time — gate + behavioural proof (MCF-90).

The invariant
-------------
``tools/skills_sync.py``, ``tools/skills_tool.py`` and
``tools/skill_manager_tool.py`` each bind their skills root into a module-level
constant at IMPORT time (``SKILLS_DIR``/``HERMES_HOME``/``MANIFEST_FILE``) and
each pairs that constant with a call-time accessor
(``_skills_dir()``/``_hermes_home()``/``_manifest_file()``) plus an
``*_AT_IMPORT`` snapshot. The constant is the *override seam* — an explicit
``monkeypatch.setattr`` still wins, which is what external patchers and the web
server's profile retargeting rely on. The accessor is the *reader*: with the
constant untouched it re-resolves from the live profile-scoped
``get_hermes_home()`` on every call.

A function body that reads the constant DIRECTLY bypasses the accessor and
keeps pointing at whichever profile happened to be active when the module was
first imported. In a long-lived ``hermes harness serve`` that is a cross-profile
read — and for the deletion paths, a cross-profile ``rmtree`` (upstream #65828,
#48200).

Why this file exists, and why it is shaped this way
---------------------------------------------------
An AST sweep asserting exactly this lived in
``tests/agent_runtime/test_env_determinism_audit.py`` as
``test_no_skills_sync_function_body_reads_a_frozen_constant_directly``. It was
deleted on **2026-07-30** (``2154f05428``, the mission-lane removal S0–S12) —
not because anyone judged the invariant obsolete, but as **collateral**: that
file's module-level imports (``agent_runtime.smoke``,
``agent_runtime.terminal_envelope``, ``agent_runtime.stagec_mcp_visual_provider``)
were part of the lane being removed, so the whole 684-line module was reduced to
a five-line retirement probe and the sweep went with it. **One day later**
(2026-07-31, ``b9721809e6``, the upstream merge) eleven direct reads of
``SKILLS_DIR`` landed across six function bodies, and nothing was left to red on
them. They survived to 2026-08-20, when MCF-90 found them by replaying the
deleted sweep's own logic (0 offenders at ``2154f05428^``, 11 at
``b9721809e6``, 11 at HEAD).

So the deletion reason was **coupling**, and this restoration is built to
survive it:

* **Module scope imports nothing but stdlib and pytest.** The gate below reads
  its subjects off DISK by path and parses them with ``ast``. It does not
  ``import tools.skills_sync`` at module scope, and neither does anything else
  here — the behavioural half imports inside the fixture. A lane removal that
  deletes some other module cannot take this gate down with it.
* **The family is DISCOVERED, not hardcoded.** Any ``tools/*.py`` that grows the
  constant + accessor + ``*_AT_IMPORT`` shape is guarded automatically, so a
  fourth module joining the lineage is covered the day it lands rather than the
  day someone remembers this file.
* **The discovery cannot go quietly vacuous.** ``test_the_gate_still_has_
  subjects`` pins the three known members by name. A rename, a move or an
  accessor deletion reds HERE instead of silently reducing the sweep to zero
  files — the failure mode that made the 2026-07-30 deletion invisible.

Ours vs upstream (MCF-90's open question, answered 2026-08-21)
--------------------------------------------------------------
The ledger row assumed converting the eleven sites was a fork rewrite that would
collide on the next upstream sync. It is the opposite. Upstream fixed this same
bug itself in ``cc421cb697`` ("fix: dashboard console skills commands no longer
act on the wrong profile", #65828, 2026-08-18) and ``upstream/main`` now scores
**zero offenders**. The fork is simply behind that commit. Converting therefore
*converges* with upstream and REMOVES a future conflict rather than creating
one; the eleven conversions are byte-identical to upstream's own.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "tools"

#: constant → (call-time accessor, import-time snapshot). A module is guarded
#: for a constant only when it defines ALL THREE at module level; that trio is
#: what makes "read the accessor instead" a meaningful instruction.
GUARDED: dict[str, tuple[str, str]] = {
    "SKILLS_DIR": ("_skills_dir", "_SKILLS_DIR_AT_IMPORT"),
    "HERMES_HOME": ("_hermes_home", "_HERMES_HOME_AT_IMPORT"),
    "MANIFEST_FILE": ("_manifest_file", "_MANIFEST_FILE_AT_IMPORT"),
}

#: Offenders deliberately left in place, keyed by repo-relative path. Values are
#: ``(frozenset of "<function>:<CONSTANT>" keys, reason)``. Same contract as
#: ``tests/test_no_frozen_hermes_home.py``'s ledger: an entry that is no longer
#: an offender FAILS, so the baseline can only shrink. It is EMPTY on purpose —
#: all eleven sites were converted on 2026-08-21 and upstream has converted its
#: own — and an empty baseline is the statement that nothing here is tolerated.
BASELINE: dict[str, tuple[frozenset[str], str]] = {}


def _module_level_names(tree: ast.Module) -> set[str]:
    """Names bound at module scope: assignment targets and def/class names."""

    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _enclosing_functions(tree: ast.Module) -> dict[int, str]:
    """Map ``id(node)`` → innermost enclosing function name, for every node.

    Nested defs are handled by walking OUTWARD from each node rather than
    inward from each function, so a read inside a closure is attributed once to
    the closure — not twice, once to it and once to its parent.
    """

    owner: dict[int, str] = {}

    def visit(node: ast.AST, current: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name)
            else:
                if current is not None:
                    owner[id(child)] = current
                visit(child, current)

    visit(tree, None)
    # The function nodes themselves belong to their own body for our purposes.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner.setdefault(id(node), node.name)
    return owner


def _family() -> dict[str, tuple[ast.Module, frozenset[str]]]:
    """Discover the guarded modules under ``tools/``.

    Returns ``{repo-relative path: (parsed tree, guarded constant names)}``.
    """

    found: dict[str, tuple[ast.Module, frozenset[str]]] = {}
    for path in sorted(TOOLS_DIR.rglob("*.py")):
        # ``rglob``, not ``glob``: moving a guarded module into a ``tools/``
        # subpackage must not quietly drop it out of the sweep.
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - unreadable source
            continue
        bound = _module_level_names(tree)
        guarded = frozenset(
            constant
            for constant, (accessor, snapshot) in GUARDED.items()
            if {constant, accessor, snapshot} <= bound
        )
        if guarded:
            found[path.relative_to(REPO_ROOT).as_posix()] = (tree, guarded)
    return found


def _offenders(tree: ast.Module, guarded: frozenset[str]) -> dict[str, list[int]]:
    """``{"<function>:<CONSTANT>": [lineno, ...]}`` for in-body constant reads.

    Every offending line is listed, not just the first: a function that reads
    the frozen constant three times needs three edits, and a message that names
    one of them invites a partial fix that still passes review.

    The constant's OWN accessor is the one sanctioned reader; every other
    function body must go through it.
    """

    owner = _enclosing_functions(tree)
    hits: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name) or node.id not in guarded:
            continue
        function = owner.get(id(node))
        if function is None:  # module scope: that IS the seam
            continue
        if function == GUARDED[node.id][0]:
            continue
        hits.setdefault(f"{function}:{node.id}", []).append(node.lineno)
    return {key: sorted(lines) for key, lines in hits.items()}


def test_the_gate_still_has_subjects() -> None:
    """The sweep must never silently become a sweep over nothing.

    This is the assertion the 2026-07-30 deletion needed and did not have. If
    ``skills_sync`` is renamed, moved out of ``tools/``, or loses its accessors,
    the discovery below would quietly return fewer files and every other test in
    this module would pass over an empty set. It reds here instead.
    """

    family = _family()
    expected = {
        "tools/skills_sync.py",
        "tools/skills_tool.py",
        "tools/skill_manager_tool.py",
    }
    missing = sorted(expected - set(family))
    assert not missing, (
        "The call-time-accessor family lost a known member: "
        + ", ".join(missing)
        + ". Either the module moved (point this gate at its new home) or it "
        "dropped its accessor/`*_AT_IMPORT` pair, which retires the invariant "
        "silently. Do not 'fix' this by deleting the name from the expected "
        "set. See MCF-90."
    )
    assert "SKILLS_DIR" in family["tools/skills_sync.py"][1]


def test_no_tools_function_body_reads_an_import_time_frozen_root() -> None:
    """The accessors are only worth having if nothing bypasses them.

    A single surviving ``SKILLS_DIR`` inside a function body is the whole bug
    back: that one call site keeps pointing at whichever profile happened to be
    active at import.
    """

    surviving: list[str] = []
    for relpath, (tree, guarded) in sorted(_family().items()):
        allowed, _reason = BASELINE.get(relpath, (frozenset(), ""))
        for key, lines in sorted(_offenders(tree, guarded).items()):
            if key in allowed:
                continue
            function, constant = key.split(":", 1)
            where = ",".join(str(line) for line in lines)
            surviving.append(f"{relpath}:{where} {function}() reads {constant}")

    assert surviving == [], (
        "A function body reads an import-time-frozen skills root instead of its "
        "call-time accessor, so it keeps resolving against whichever profile was "
        "active at import. Call `_skills_dir()` / `_hermes_home()` / "
        "`_manifest_file()` instead. See MCF-90 and "
        "docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/env-determinism-audit.md §7.2. Offenders: "
        + "; ".join(surviving)
    )


def test_the_baseline_holds_no_entry_that_is_no_longer_an_offender() -> None:
    """A baseline can only shrink — a stale exemption is a silent re-opening."""

    family = _family()
    stale: list[str] = []
    for relpath, (allowed, reason) in sorted(BASELINE.items()):
        assert reason.strip(), f"{relpath}: a baseline entry must carry a reason"
        if relpath not in family:
            stale.append(f"{relpath} (file is no longer in the guarded family)")
            continue
        tree, guarded = family[relpath]
        live = set(_offenders(tree, guarded))
        for key in sorted(allowed - live):
            stale.append(f"{relpath}:{key} (converted — drop the entry)")

    assert stale == [], (
        "The exemption baseline names sites that are no longer offenders. Delete "
        "them: a stale entry re-opens the hole for the next reintroduction "
        "without anyone noticing. Stale: " + "; ".join(stale)
    )


def test_agent_runtime_never_imports_a_frozen_root_constant_from_tools() -> None:
    """``agent_runtime`` is immune by construction — keep it that way.

    ``skill_publishability`` imports only ``_dir_hash`` / ``_read_skill_name``
    (pure helpers) and derives every skills root itself, which is why no
    fork-owned call-time accessor is warranted: there is no fork-side reader for
    it to fix. A new import of the frozen names would change that silently.
    """

    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "agent_runtime").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - unreadable source
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not node.module.startswith("tools.skill"):
                continue
            for alias in node.names:
                if alias.name in GUARDED:
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno} "
                        f"imports {alias.name}"
                    )

    assert offenders == [], (
        "agent_runtime imported an import-time-frozen skills-root constant from "
        "tools/. Resolve the root at call time instead. See "
        "docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/env-determinism-audit.md §3. Offenders: "
        + ", ".join(offenders)
    )


#: constant → path suffix under the live profile home, for the drift check.
_LIVE_SUFFIX = {
    "SKILLS_DIR": ("skills",),
    "HERMES_HOME": (),
    "MANIFEST_FILE": ("skills", ".bundled_manifest"),
}

_DRIFT_CASES = sorted(
    (relpath, constant)
    for relpath, (_tree, guarded) in _family().items()
    for constant in guarded
)


@pytest.mark.parametrize("relpath,constant", _DRIFT_CASES)
def test_every_guarded_module_follows_a_live_profile_switch(
    relpath: str, constant: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both halves of the seam, against the real module rather than its source.

    Dropping the constant would break every external patcher (the reason §7.2
    kept it); ignoring a live profile switch was the bug §7.2 fixed. An
    explicit patch must still outrank the switch, or ``monkeypatch``-based test
    isolation stops working.
    """

    import importlib

    module = importlib.import_module(
        relpath[: -len(".py")].replace("/", ".")
    )
    accessor_name, snapshot_name = GUARDED[constant]
    accessor = getattr(module, accessor_name)

    frozen_home = tmp_path / "profile-a"
    frozen_value = frozen_home.joinpath(*_LIVE_SUFFIX[constant])
    monkeypatch.setattr(module, constant, frozen_value)
    monkeypatch.setattr(module, snapshot_name, frozen_value)

    live_home = tmp_path / "profile-b"
    monkeypatch.setenv("HERMES_HOME", str(live_home))

    # The constant stays exactly where it was — that IS the seam...
    assert getattr(module, constant) == frozen_value
    # ...but the module now follows the live profile.
    assert accessor() == live_home.joinpath(*_LIVE_SUFFIX[constant])

    # An explicit patch still outranks the live profile.
    patched = tmp_path / "explicitly-patched"
    monkeypatch.setattr(module, constant, patched)
    assert accessor() == patched


# ── behavioural half: the conversions actually read the CURRENT root ─────────
#
# The gate above is a source-shape assertion; on its own a `_skills_dir()` that
# returned the frozen value would satisfy it. These drive the real functions
# across a profile switch that happens AFTER import, which is the only thing
# that tells a converted site from an unconverted one. Patching ``SKILLS_DIR``
# would NOT: the accessor honors an explicit patch, so both shapes would agree.


def _write_skill(root: Path, rel: str, name: str, body: str = "hi\n") -> Path:
    skill_dir = root / rel
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\n---\n{body}", encoding="utf-8"
    )
    return skill_dir


@pytest.fixture
def switched_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Module imported under profile A; the live profile is now B.

    ``*_AT_IMPORT`` is pinned alongside the constant so the accessor sees an
    UNPATCHED module and falls through to the live home — exactly the state a
    long-lived ``serve`` is in after ``set_hermes_home_override()``.
    """

    import tools.skills_sync as skills_sync

    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    for home in (home_a, home_b):
        (home / "skills").mkdir(parents=True, exist_ok=True)

    for constant, value in (
        ("SKILLS_DIR", home_a / "skills"),
        ("_SKILLS_DIR_AT_IMPORT", home_a / "skills"),
        ("HERMES_HOME", home_a),
        ("_HERMES_HOME_AT_IMPORT", home_a),
        ("MANIFEST_FILE", home_a / "skills" / ".bundled_manifest"),
        ("_MANIFEST_FILE_AT_IMPORT", home_a / "skills" / ".bundled_manifest"),
    ):
        monkeypatch.setattr(skills_sync, constant, value)

    monkeypatch.setenv("HERMES_HOME", str(home_b))

    # Preconditions: the frozen constant still says A, the accessor says B.
    assert skills_sync.SKILLS_DIR == home_a / "skills"
    assert skills_sync._skills_dir() == home_b / "skills"

    return skills_sync, home_a / "skills", home_b / "skills"


def test_index_installed_skill_dirs_by_name_follows_the_live_profile(
    switched_profile,
) -> None:
    skills_sync, frozen_root, live_root = switched_profile
    _write_skill(frozen_root, "cat/ghost", "ghost")
    expected = _write_skill(live_root, "cat/widget", "widget")

    index = skills_sync._index_installed_skill_dirs_by_name()

    assert index == {"widget": [expected]}, (
        "the installed-dir index scanned the import-time root: it should list "
        f"{expected} from the LIVE profile, not profile A's tree"
    )


def test_find_installed_skill_dir_by_name_follows_the_live_profile(
    switched_profile,
) -> None:
    skills_sync, _frozen_root, live_root = switched_profile
    expected = _write_skill(live_root, "cat/widget", "widget")

    assert skills_sync._find_installed_skill_dir_by_name("widget") == expected


def test_read_hub_install_paths_follows_the_live_profile(switched_profile) -> None:
    import json

    skills_sync, frozen_root, live_root = switched_profile
    for root, install_path in ((frozen_root, "cat/ghost"), (live_root, "cat/widget")):
        lock = root / ".hub" / "lock.json"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(
            json.dumps({"installed": {"x": {"install_path": install_path}}}),
            encoding="utf-8",
        )

    assert skills_sync._read_hub_install_paths() == {"cat/widget"}


def test_index_active_skills_follows_the_live_profile(switched_profile) -> None:
    skills_sync, frozen_root, live_root = switched_profile
    _write_skill(frozen_root, "cat/ghost", "ghost")
    expected = _write_skill(live_root, "cat/widget", "widget")

    assert skills_sync._index_active_skills() == {"widget": [expected]}


def test_recover_renamed_skill_follows_the_live_profile(switched_profile) -> None:
    """The rename-recovery move is computed relative to the skills root.

    Against the frozen root ``candidate.relative_to(...)`` raises ``ValueError``
    and recovery silently declines every candidate — the skill is then misread
    as user-deleted and stranded forever.
    """

    skills_sync, _frozen_root, live_root = switched_profile
    candidate = _write_skill(live_root, "old/widget", "widget")
    dest = live_root / "new" / "widget"

    moved = skills_sync._recover_renamed_skill(
        "widget",
        skills_sync._dir_hash(candidate),
        dest,
        {"widget": [candidate]},
        set(),
        True,
    )

    assert moved == "old/widget"
    assert dest.is_dir() and not candidate.exists()


def test_backfill_optional_provenance_follows_the_live_profile(
    switched_profile, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The relocated-skill fallback rebases onto the skills root.

    The repo-derived ``install_path`` does not exist in the live tree, so the
    name-index fallback finds the skill under a different category and the
    install path is recomputed relative to the root. Against the frozen root
    that recompute raises and provenance repair skips the skill in silence.
    """

    skills_sync, _frozen_root, live_root = switched_profile
    optional_dir = tmp_path / "optional-skills"
    source = _write_skill(optional_dir, "mlops/widget", "widget")
    monkeypatch.setattr(skills_sync, "_get_optional_dir", lambda: optional_dir)

    installed = _write_skill(live_root, "mlops/vector-databases/widget", "widget")
    assert skills_sync._dir_hash(installed) == skills_sync._dir_hash(source)

    assert skills_sync._backfill_optional_provenance(quiet=True) == ["widget"]

    import json

    lock = json.loads((live_root / ".hub" / "lock.json").read_text(encoding="utf-8"))
    assert lock["installed"]["widget"]["install_path"] == (
        "mlops/vector-databases/widget"
    )
