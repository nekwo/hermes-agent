"""S46 retires the INCREMENTAL projection lane; the full rebuild stays.

Ledger item 9 (docs/agent-runtime-harness/19-deferred-debt-ledger.md) asked for a
direction call: finish the read-model lane (wire the RD3 ticker chokepoint) or
retire it. The operator ruled **RETIRE** on 2026-08-01.

What made it a ruling rather than a cleanup: ``Projector.apply_pending`` and the
lease it takes were never unreachable *code* — they were reachable only from
tests. Repo-wide, the sole production entry to the projector is
``full_rebuild()`` from ``_cmd_rebuild_read_model``
(``hermes_cli/harness_parts/runtime_commands.py:478``). The RD3 design
(``docs/agent-runtime-harness/05-runtime-data-enterprise-storage.md:348``)
specified a "ticker chokepoint" that would call ``apply_pending()`` while the
lease was held. It was never wired, so the two SLO tests were timing a lane
nothing runs — ledger item 2's closed loop one level down: not a module whose
whole importer set is its test, but a METHOD whose whole caller set is its tests.

Re-verified immediately before the cut, not inherited from the audit:

=========================== ==================================================
symbol                      callers before the cut
=========================== ==================================================
Projector.apply_pending     5 test call sites, 0 production
Projector.acquire_lease     apply_pending + 1 test; full_rebuild does NOT
                            take the lease, so the lease died with its only
                            live caller
Projector._count_pending    apply_pending only
ProjectorResult             apply_pending's return type; nothing else
                            constructed or imported it
LEASE_TTL_SECONDS           acquire_lease only
meta['projector_lease']     written by acquire_lease, read by acquire_lease
=========================== ==================================================

No dynamic dispatch: the repo contains no ``getattr(projector, ...)`` and no
string literal ``"apply_pending"`` / ``"acquire_lease"`` aimed at this class, so
module-level reachability is the whole story here.

KEPT, and pinned below so a later sweep cannot mistake this cut for permission
to take the rest:

- ``Projector.full_rebuild`` — the operator-invoked cache warmer.
- ``ReadModel.projection_watermark`` — the rebuild command reads it back, and
  ``resolve_snapshot_frame`` keys freshness on it.
- ``resolve_snapshot_frame`` / ``snapshot_watermark`` / ``read_projection`` /
  ``render_snapshot`` — the serve/read path.
- ``EventLog.iter_from_offset`` — ``_count_pending`` was one of FOUR callers;
  ``stream.py``, ``checkpoint.py`` and the rotation tailers keep it load-bearing.
- ``_seed_synthetic_runtime`` / ``SLO_FULL_BUILD_MS`` — the RD0 full-build SLO
  test and ``test_read_model`` still use them.

The tests deleted with the lane are named in ``REMOVED_TESTS`` and asserted
absent, never silently dropped — the s41-s45 precedent: a witness that quietly
loses its subject hides a reversal; one that asserts absence records it.
"""

from __future__ import annotations

import ast
import inspect
import io
import tokenize
from pathlib import Path

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

#: Names cut from ``agent_runtime.projector``. Module attributes, so absence is
#: checked against the imported module, not against a grep.
REMOVED_PROJECTOR_NAMES = ("ProjectorResult", "LEASE_TTL_SECONDS")

#: Methods cut from ``Projector``.
REMOVED_PROJECTOR_METHODS = ("apply_pending", "acquire_lease", "_count_pending")

#: Test functions that existed solely to exercise the retired lane, with the
#: file that held them. Both files survive — each keeps tests that pin the live
#: full-rebuild path — so this asserts the FUNCTIONS are gone, not the files.
REMOVED_TESTS = (
    ("tests/agent_runtime/test_projector.py", "test_apply_pending_is_o_delta_on_rd0_fixture"),
    ("tests/agent_runtime/test_projector.py", "test_replay_equivalence_full_vs_incremental_goal_row"),
    (
        "tests/agent_runtime/test_projector.py",
        "test_replay_equivalence_goal_row_carries_bundle_assignment_and_lane_state",
    ),
    ("tests/agent_runtime/test_projector.py", "test_lease_excludes_second_projector"),
    ("tests/agent_runtime/test_projector.py", "test_registered_event_rebuilds_the_whole_frame"),
    ("tests/agent_runtime/test_read_model_slo.py", "test_synthetic_incremental_apply_within_rd3_slo"),
)

#: Files that must survive the cut — the lane's tests lived alongside live ones.
SURVIVING_TEST_FILES = (
    "tests/agent_runtime/test_projector.py",
    "tests/agent_runtime/test_read_model_slo.py",
    "tests/agent_runtime/test_read_model_frame_source.py",
)


@pytest.mark.parametrize("name", REMOVED_PROJECTOR_NAMES)
def test_the_lane_only_module_name_is_gone(name: str):
    from agent_runtime import projector

    assert not hasattr(projector, name)


@pytest.mark.parametrize("name", REMOVED_PROJECTOR_METHODS)
def test_the_incremental_method_is_gone(name: str):
    from agent_runtime.projector import Projector

    assert not hasattr(Projector, name)


def test_the_projector_no_longer_takes_a_lease():
    """The lease was storage, not just code: nothing may write the meta key."""

    from agent_runtime import projector

    source = _code_without_prose(inspect.getsource(projector))
    assert "projector_lease" not in source
    assert "monotonic" not in source
    assert "LEASE" not in source


def test_the_projector_no_longer_tails_the_event_log():
    """``_count_pending`` was the projector's only ``iter_from_offset`` caller
    and its only reason to hold an ``EventLog``. The constructor keyword goes
    with it — a parameter nothing reads is a caller-visible lie."""

    from agent_runtime.projector import Projector

    source = _code_without_prose(inspect.getsource(Projector))
    assert "iter_from_offset" not in source
    assert "event_log" not in inspect.signature(Projector.__init__).parameters


def test_the_full_rebuild_cache_warmer_stays_live():
    from agent_runtime.projector import Projector

    assert callable(Projector.full_rebuild)
    assert list(inspect.signature(Projector.__init__).parameters) == ["self", "read_model", "config"]


def test_the_serve_and_read_path_is_untouched():
    """Named one by one: the cut must not take anything the read path uses."""

    from agent_runtime import read_model as read_model_module

    for name in ("snapshot_watermark", "resolve_snapshot_frame"):
        assert callable(getattr(read_model_module, name))
    for name in ("projection_watermark", "read_projection", "render_snapshot", "apply_full_rebuild"):
        assert callable(getattr(read_model_module.ReadModel, name))

    # ``iter_from_offset`` had four callers; only the projector's went.
    from agent_runtime.events import EventLog

    assert callable(EventLog.iter_from_offset)


def test_the_rebuild_command_still_reaches_the_projector():
    """The one production entry point, asserted through the CLI part that owns
    it rather than by importing the module the cut touched."""

    source = (REPO_ROOT / "hermes_cli/harness_parts/runtime_commands.py").read_text(encoding="utf-8")
    assert ".full_rebuild()" in source
    assert "apply_pending" not in source


def test_no_surviving_module_re_grows_the_lane():
    """Text gate over the production packages, on CODE only.

    Docstrings and comments are stripped first: the projector's own class
    docstring records what S46 removed and by name, which is the point of a
    witness, and a gate that cannot tell that from a re-grown symbol fires on
    the record of its own cut.
    """

    forms = (
        "def apply_pending",
        ".apply_pending()",
        "ProjectorResult",
        "projector_lease",
        "LEASE_TTL_SECONDS",
    )
    offenders = []
    for package in ("agent_runtime", "hermes_cli", "agent", "tools", "scripts"):
        root = REPO_ROOT / package
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            raw = path.read_text(encoding="utf-8", errors="replace")
            if not any(form in raw for form in forms):
                continue
            try:
                source = _code_without_prose(raw)
            except (SyntaxError, tokenize.TokenError):
                source = raw
            for form in forms:
                if form in source:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{form}")
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


@pytest.mark.parametrize("relative,func", REMOVED_TESTS)
def test_the_lane_only_test_is_gone(relative: str, func: str):
    source = (REPO_ROOT / relative).read_text(encoding="utf-8")
    assert f"def {func}(" not in source


@pytest.mark.parametrize("relative", SURVIVING_TEST_FILES)
def test_the_file_that_held_them_survives(relative: str):
    assert (REPO_ROOT / relative).is_file()


def test_the_incremental_slo_constant_went_with_its_only_assertions():
    """``SLO_INCREMENTAL_APPLY_MS`` was asserted only by the deleted tests.
    ``SLO_FULL_BUILD_MS`` stays — ``test_read_model`` renders against it."""

    import test_read_model_slo

    assert not hasattr(test_read_model_slo, "SLO_INCREMENTAL_APPLY_MS")
    assert test_read_model_slo.SLO_FULL_BUILD_MS == 2000
    assert callable(test_read_model_slo._seed_synthetic_runtime)
