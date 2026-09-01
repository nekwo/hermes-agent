"""S46 retired the INCREMENTAL projection lane. STAGE 6 retired the rest of it.

S46 (operator-ruled RETIRE 2026-08-01) cut ``Projector.apply_pending``, the
``meta.projector_lease`` it took, the watermark diff it did, the pending-event
count it made, and the ``ProjectorResult`` offsets/timings only it produced —
five test call sites, zero production ones. What it deliberately KEPT was the
operator-invoked cache warmer: ``Projector.full_rebuild`` behind
``hermes harness rebuild-read-model``, plus the whole ``read_model.py``
serve/read path. This file pinned that keep-set name by name, so a later sweep
could not mistake S46's cut for permission to take the rest.

**STAGE 6 (duplicate-implementation retirement, 2026-08-22) took the rest, and
it took it by a ruling rather than by a sweep.** The keep-set argument was that
``full_rebuild`` had a production entry point. It did — but the entry point was a
CLI verb an operator had to run by hand, writing a database that nothing on the
serve path ever read:

* ``write_snapshot()`` (``snapshot.py``) had exactly ONE non-test caller,
  ``read_model.resolve_snapshot_frame``, reached only from ``harness snapshot``.
* ``Projector.full_rebuild()`` and ``write_snapshot()``'s gated
  ``ReadModel().apply_full_rebuild(snapshot)`` were TWO production writers of the
  same database over the same ``build_snapshot()`` output — one gated on
  ``read_model_enabled()``, one not.
* The lane could not save work as shaped: ``resolve_snapshot_frame`` built the
  full core FIRST and only then decided whether to serve the cached frame, so
  ``FrameSource.CACHE`` cost one full build plus a database read.
* The launcher's ``snapshot.json`` cold-paint consumer was retired at MC-7 / P11
  (``mission_control_snapshot.dart:187``), so the boot cache served no boot.

So this file INVERTS. Where it asserted the keep-set present, it now asserts the
same names absent, in the tombstone style it already used for the S46 half. The
prose above is preserved rather than rewritten because the keep-set argument is
the interesting part: it was correct on its own terms and still lost, which is
what "a production caller" is worth when the caller is a verb nobody runs.

=========================================================================
WHAT IS PINNED HERE AND NOT IN THE REGISTRY
=========================================================================

``tests/agent_runtime/test_tombstone_registry.py`` carries the pure-absence
rows: the ``agent_runtime.read_model`` / ``agent_runtime.projector`` MODULE rows
(s74), the S46 ``Form.ATTR`` / ``Form.CLASS_ATTR`` rows those MODULE rows now
supersede, and the four repo-wide ``Form.CODE`` bans (``ProjectorResult``,
``projector_lease``, ``LEASE_TTL_SECONDS``, ``SLO_INCREMENTAL_APPLY_MS``).

Everything below is a claim the registry cannot make:

* ``apply_pending`` cannot be a repo-wide CODE row, because
  ``agent.agent_runtime_helpers.apply_pending_steer_to_tool_results`` is live and
  a bare-name row would be red against a correct tree. This file states the
  narrow DEF/CALL form structurally instead.
* The two retired CLI VERBS are argv strings in a parser registration, not
  importable names. ``rebuild-read-model`` and ``read`` are pinned by scanning
  the registration site.
* ``SLO_FULL_BUILD_MS`` surviving in ``test_read_model_slo`` is a claim about a
  TEST module, and the registry scans production source only.
* The lookalike keep set (``CredentialPool.acquire_lease``,
  ``apply_pending_steer_to_tool_results``) is asserted rather than left to the
  accident of a gate not matching it.

``REMOVED_TESTS`` is gone as a table: it asserted ``def <name>(`` absent from
``tests/agent_runtime/test_projector.py``, and that FILE is now deleted whole.
Absence of the file is the stronger claim and is asserted directly.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import io
import textwrap
import tokenize
from pathlib import Path

from tests.agent_runtime import _tree_index

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _code_without_prose(source: str) -> str:
    """Source with docstrings and comments removed — everything else intact.

    A removal gate that greps raw source cannot tell a re-grown symbol from the
    comment explaining why it was removed, so it fires on the witness that
    records the cut (s45 stated the rule; this makes it mechanical). Ordinary
    string literals are KEPT: ``meta['projector_lease']`` only ever existed as a
    literal, so dropping all strings would gate on nothing.
    """

    docstrings = set()
    for node in ast.walk(ast.parse(source)):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            if isinstance(body[0].value.value, str):
                docstrings.add((body[0].value.lineno, body[0].value.col_offset))

    kept = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            continue
        if token.type == tokenize.STRING and token.start in docstrings:
            continue
        kept.append(token.string)
    return "\n".join(kept)


#: The two modules Stage 6 deleted whole. Asserted unimportable here as well as
#: in the registry, because the two gates answer different questions: the
#: registry proves the NAME cannot resolve, this file proves it in the company of
#: the argument for why — and a reader who lands on this file from the s46 wave
#: is exactly the reader who needs both.
_RETIRED_MODULES = ("agent_runtime.read_model", "agent_runtime.projector")

#: Files deleted with the lane. Each held ONLY lane tests, which is why the whole
#: file goes rather than named functions inside it (the s46 shape).
_REMOVED_TEST_FILES = (
    "tests/agent_runtime/test_projector.py",
    "tests/agent_runtime/test_read_model.py",
    "tests/agent_runtime/test_read_model_enabled_resolver.py",
    "tests/agent_runtime/test_read_model_frame_source.py",
)

#: The one test file from the S46 keep-set that SURVIVES. It measures
#: ``build_snapshot`` directly and never touched the retired modules, which is
#: why the RD0 full-build SLO outlived two retirements of the lane around it.
_SURVIVING_TEST_FILE = "tests/agent_runtime/test_read_model_slo.py"

#: The two CLI verbs, as the operator typed them. Scanned against the file that
#: registers every harness subcommand.
_RETIRED_VERBS = ("rebuild-read-model",)

_PARSER_FILE = "hermes_cli/harness.py"
_RUNTIME_COMMANDS_FILE = "hermes_cli/harness_parts/runtime_commands.py"


@pytest.mark.parametrize("dotted", _RETIRED_MODULES)
def test_the_read_model_lane_module_is_gone(dotted: str):
    """``find_spec``, not ``import`` — a module deleted from a tree whose
    ``__pycache__`` survives still imports for one process otherwise."""

    assert importlib.util.find_spec(dotted) is None, (
        f"{dotted} is importable again. Stage 6 retired the whole read_model.db "
        "lane: two production writers of a database with no production reader, "
        "reachable only from two hand-run CLI verbs."
    )


@pytest.mark.parametrize("relative", _REMOVED_TEST_FILES)
def test_the_lane_only_test_file_is_gone(relative: str):
    assert not (REPO_ROOT / relative).exists(), (
        f"{relative} is back. Every test in it exercised a deleted module; a "
        "file that returns has brought the module with it."
    )


def test_the_full_build_slo_file_survives_the_cut():
    """The keep that is easiest to take by accident.

    ``test_read_model_slo.py`` matches the ``test_read_model*`` glob that names
    the deleted files, and it does NOT belong to the lane: it seeds 10,000
    synthetic events and times ``build_snapshot`` against ``SLO_FULL_BUILD_MS``.
    The name is the whole hazard, so the survival is asserted rather than
    assumed.
    """

    assert (REPO_ROOT / _SURVIVING_TEST_FILE).is_file()

    import test_read_model_slo

    assert test_read_model_slo.SLO_FULL_BUILD_MS == 2000
    assert test_read_model_slo.SLO_CONSUMER_VISIBLE_LAG_MS == 1500
    assert callable(test_read_model_slo._seed_synthetic_runtime)
    # S46's own scar: the third constant went with the two assertions that were
    # its only readers, and this is where that stays recorded.
    assert not hasattr(test_read_model_slo, "SLO_INCREMENTAL_APPLY_MS")


@pytest.mark.parametrize("verb", _RETIRED_VERBS)
def test_the_retired_cli_verb_is_not_registered(verb: str):
    """The verbs are ARGV STRINGS, so no importable-name gate can hold them.

    ``rebuild-read-model`` is scanned as a literal because the spelling is
    unique in the tree. Its sibling ``read`` deliberately is NOT scanned as a
    bare word — ``"read"`` appears in dozens of legitimate contexts and a
    repo-wide ban on it would be a false-positive machine (the S52 rule). The
    ``read`` verb is instead pinned by its HANDLER's absence below, which is the
    exact form the registration needed to dispatch.
    """

    source = (REPO_ROOT / _PARSER_FILE).read_text(encoding="utf-8")
    rendered = _code_without_prose(source)
    assert verb not in rendered, (
        f"{_PARSER_FILE} registers `harness {verb}` again. Its handler imported "
        "agent_runtime.projector and agent_runtime.read_model, both deleted at "
        "Stage 6, so the verb can only fail at dispatch."
    )


def test_neither_retired_handler_survives_in_the_runtime_commands_part():
    """The dispatch targets, by name, in the part file that held them.

    Both handlers were the whole production caller set of the deleted modules.
    A registration without a handler is a NameError at dispatch time; a handler
    without a registration is dead code that reads as live. Banning the handler
    names closes the second half and, with the verb scan above, the first.
    """

    rendered = _code_without_prose(
        (REPO_ROOT / _RUNTIME_COMMANDS_FILE).read_text(encoding="utf-8")
    )
    for handler in ("_cmd_rebuild_read_model", "_cmd_read_projection"):
        assert handler not in rendered, (
            f"{_RUNTIME_COMMANDS_FILE} defines or references {handler} again"
        )


def test_the_snapshot_verb_builds_directly_and_still_stamps_its_source():
    """``_cmd_snapshot`` was the lane's ONLY remaining production reader.

    It went through ``prefer_cached_snapshot`` / ``resolve_snapshot_frame``,
    which built the full core and then decided whether to throw it away for a
    cached frame. Collapsed to ``build_snapshot()``.

    ``frame_source`` STAYS on the envelope. Removing a key from the wire is a
    contract change and the additive rule cuts one way only; the field now
    carries the one value it can, ``built``. Asserted here rather than left to
    the CLI suite because the temptation to delete a constant-valued field is
    exactly what this stage had to refuse.
    """

    rendered = _code_without_prose(
        (REPO_ROOT / _RUNTIME_COMMANDS_FILE).read_text(encoding="utf-8")
    )
    for gone in ("resolve_snapshot_frame", "prefer_cached_snapshot", "FrameSource"):
        assert gone not in rendered, f"{gone} is back in {_RUNTIME_COMMANDS_FILE}"
    assert "frame_source" in rendered, (
        "the snapshot envelope lost its frame_source stamp. The lane it named is "
        "retired, but the KEY is wire: dropping it is a contract change nobody "
        "ruled and no consumer was told about."
    )


def test_the_boot_cache_writer_went_with_the_lane():
    """``write_snapshot`` had ONE production caller and it was inside the lane.

    Its ``snapshot.json`` write served the launcher's cold-paint reader, retired
    at MC-7 / P11, and its gated ``apply_full_rebuild`` was the second of the two
    duplicate writers. ``build_snapshot`` is asserted live in the same breath so
    a broken import cannot make this pass by accident.
    """

    from agent_runtime import snapshot

    assert callable(snapshot.build_snapshot)
    for gone in ("write_snapshot", "_sweep_stale_snapshot_tmp_files"):
        assert not hasattr(snapshot, gone), f"snapshot.{gone} is back"

    # ``paths.snapshot_path`` is the deliberate SURVIVOR of that cut — one
    # authority for where a legacy copy of the file lives, so a migration or an
    # operator can still find and remove one. Pinned so the next sweep does not
    # read its callerlessness as the same verdict.
    from agent_runtime import paths

    assert callable(paths.snapshot_path)


#: The three IDENTIFIER forms. Each survives the token-join in
#: :func:`_code_without_prose` intact, because each is a single token.
_IDENTIFIER_FORMS = ("ProjectorResult", "projector_lease", "LEASE_TTL_SECONDS")

#: The retired entry point, by NAME. Read structurally below — never as text.
_RETIRED_MEMBER = "apply_pending"

_SCANNED_PACKAGES = ("agent_runtime", "hermes_cli", "agent", "tools", "scripts")


def _structural_regrowth(tree: ast.AST) -> list[str]:
    """The retired member re-grown as a DEFINITION or as a CALL.

    Both were text forms — ``"def apply_pending"`` and ``".apply_pending()"`` —
    and both were VACUOUS, which is this file's own diagnosis arriving one wave
    late. :func:`_code_without_prose` joins surviving tokens with newlines, so
    ``def apply_pending`` renders as two tokens on two lines and
    ``.apply_pending()`` as four. Neither substring can ever appear in the
    stripped text. S48's file docstring already records this
    exact failure of the token-join helper — "so a dotted assertion can never
    match and passes vacuously" — and S46 was never re-aimed.

    They are also this file's UNIQUE contribution: the module header explains
    that the bare name cannot be a registry CODE row, because
    ``agent.agent_runtime_helpers.apply_pending_steer_to_tool_results`` is live
    and a repo-wide row would be red against a correct tree. So the two vacuous
    forms were the only thing this gate held that nothing else does.

    AST answers both without the ambiguity that forced the text form in the
    first place: ``node.name == "apply_pending"`` is EXACT, so the live
    ``apply_pending_steer_to_tool_results`` — which the old raw prefilter
    matched as a substring — is structurally not the same name.
    """

    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == _RETIRED_MEMBER:
            offenders.append(f"def {_RETIRED_MEMBER}@{node.lineno}")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == _RETIRED_MEMBER
        ):
            offenders.append(f".{_RETIRED_MEMBER}()@{node.lineno}")
    return offenders


def test_the_structural_detector_fires_and_stays_quiet():
    """ANTI-VACUITY for :func:`_structural_regrowth`, on synthetic source.

    The gate below asserts an EMPTY list — the shape that keeps passing once the
    machinery under it stops working, which is exactly what happened to the two
    text forms this replaces. It must name a planted definition and a planted
    call, and it must NOT name the live lookalike.
    """

    regrown = ast.parse(
        textwrap.dedent(
            """
            class P:
                def apply_pending(self, events):
                    return events

            def run(p):
                return p.apply_pending([])
            """
        )
    )
    assert [entry.split("@")[0] for entry in _structural_regrowth(regrown)] == [
        "def apply_pending",
        ".apply_pending()",
    ]

    lookalike = ast.parse(
        textwrap.dedent(
            """
            def apply_pending_steer_to_tool_results(results):
                return results

            def run(h):
                return h.apply_pending_steer_to_tool_results([])
            """
        )
    )
    assert _structural_regrowth(lookalike) == [], (
        "the live steering helper is not the retired projector member; an exact "
        "name match is what lets this gate exist at all"
    )


def test_no_surviving_module_re_grows_the_lane():
    """Gate over the production packages, on CODE only.

    Docstrings and comments are stripped first: the modules' own docstrings
    recorded what S46 removed and by name, which is the point of a witness, and
    a gate that cannot tell that from a re-grown symbol fires on the record of
    its own cut. Those modules are deleted now, but the rule outlives them —
    every surviving file that explains this cut names the same symbols.

    Two lanes, because the forms are two different kinds of claim. The three
    IDENTIFIER forms are single tokens and survive the strip, so they stay text.
    The retired member is read STRUCTURALLY — see :func:`_structural_regrowth`
    for why its two text forms could never match.
    """

    scanned = 0
    offenders = []
    for package in _SCANNED_PACKAGES:
        root = REPO_ROOT / package
        assert root.is_dir(), f"scan package {package} has moved; the scope is silently smaller"
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            scanned += 1
            raw = _tree_index.text(str(path), errors="replace")
            try:
                tree = _tree_index.parsed(str(path), errors="replace")
            except SyntaxError:
                tree = None
            if tree is not None:
                for entry in _structural_regrowth(tree):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{entry}")
            if not any(form in raw for form in _IDENTIFIER_FORMS):
                continue
            try:
                source = _code_without_prose(raw)
            except (SyntaxError, tokenize.TokenError):
                source = raw
            for form in _IDENTIFIER_FORMS:
                if form in source:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{form}")
    assert scanned > 500, scanned
    assert offenders == []


def test_the_lookalike_keep_set_survives():
    """Names live code one bare-word grep away from the cut, so a later sweep
    reading only the s46 summary does not take them too."""

    # A DIFFERENT class's lease, on the credential pool, with a real caller in
    # ``tools/delegate_tool.py``. Its signature takes a credential id, which is
    # why the ``def apply_pending``/``projector_lease`` gate above never sees it
    # — but the survival is asserted, not left to that accident.
    from agent.credential_pool import CredentialPool

    assert callable(CredentialPool.acquire_lease)

    # ``agent.agent_runtime_helpers.apply_pending_steer_to_tool_results`` is the
    # steering lane, unrelated to projection and very much live.
    from agent import agent_runtime_helpers

    assert callable(agent_runtime_helpers.apply_pending_steer_to_tool_results)

    # ``iter_from_offset`` had FOUR callers; only the projector's went at S46 and
    # nothing at Stage 6 touched it. ``stream.py``, ``checkpoint.py`` and the
    # rotation tailers keep it load-bearing.
    from agent_runtime.events import EventLog

    assert callable(EventLog.iter_from_offset)


def test_the_core_cache_keeps_the_colliding_directory_name():
    """Stage 7 was CANCELLED by the same ruling that executed Stage 6.

    ``CORE_CACHE_DIRNAME = "serve_read_model"`` is the LIVE core cache's on-disk
    home and has never had anything to do with the retired read model. The
    contingent rename existed only for a world where two live "read model"s
    coexisted; with the other one deleted, renaming would orphan the directory
    and buy one demote-priced rebuild to fix a name that no longer collides.
    Pinned so the next reader of this file does not finish the job it looks like
    half of.
    """

    from agent_runtime.core_cache import CORE_CACHE_DIRNAME

    assert CORE_CACHE_DIRNAME == "serve_read_model"
