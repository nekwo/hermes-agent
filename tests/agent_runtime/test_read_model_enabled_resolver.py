"""RD-H6 item 1 — ``read_model.enabled`` is ONE question with ONE resolver.

The flag was answered in two places. ``snapshot.write_snapshot`` gates the cache
WRITE on it; ``_cmd_snapshot`` computed the serve-side cache PREFERENCE from its
own copy of the same ``getattr`` ladder. Two copies of one question, each free to
resolve at its own scope, is the exact misplacement class that kept the
neighbouring leaf (``read_model.delta_patches``) dark for its whole life: it was
READ at the root and WRITTEN into a profile, ``harness status`` reported it
``true`` throughout because status reads profile-aware, and the lane shipped an
822 KB delta per field change where its own patch frame is 486 bytes.

WHAT MAKES THIS SEAM DIFFERENT FROM A COSMETIC DEDUPE. A disagreement between
the write gate and the serve preference does not surface as an error — it
surfaces as ``frame_source: cache_miss_rebuilt``, which is the honest name for a
COLD cache. So "the cache is never warm on this install" and "the flag is read at
two scopes" render identically to an operator, and the second one is invisible
for as long as nobody enables the lane on a fresh root. That is why the probe
below is the frame source and not a returned boolean.

ANTI-VACUITY. Both tests drive a REAL config tree in which the root and the
active profile disagree, in OPPOSITE directions, and demand a different
``frame_source`` from each. A resolver that reads the wrong scope cannot satisfy
both: it flips one case to ``built`` (preference off while the write gate ran)
and the other to ``cache_miss_rebuilt`` (preference on while the write gate did
not). A constant cannot satisfy either pair. Measured, not assumed — see the
mutation notes on each test.
"""

from __future__ import annotations

import ast
import json
from argparse import Namespace
from pathlib import Path

import pytest

from agent_runtime import paths
from agent_runtime.read_model import (
    ReadModel,
    prefer_cached_snapshot,
    read_model_enabled,
)
from agent_runtime.runtime_config import ReadModelConfig, RuntimeConfig

REPO_ROOT = Path(__file__).resolve().parents[2]


def _config_tree(tmp_path, monkeypatch, *, root_enabled: bool, profile_enabled: bool):
    """A root + active-profile config pair that DISAGREE about ``enabled``.

    ``HERMES_HOME`` points at ``<root>/profiles/<name>`` — the shape the CLI
    bootstrap installs (``hermes_cli.main._apply_profile_override``) and the shape
    ``get_default_hermes_root`` recognises, so ``harness_root_config_path()``
    still resolves ``<root>/config.yaml`` while ``get_config_path()`` resolves the
    profile's. Both files are written, so neither answer is an absence.
    """

    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "seam"
    profile.mkdir(parents=True, exist_ok=True)
    body = "agent_runtime:\n  read_model:\n    enabled: {}\n"
    (root / "config.yaml").write_text(body.format(str(root_enabled).lower()), encoding="utf-8")
    (profile / "config.yaml").write_text(
        body.format(str(profile_enabled).lower()), encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(profile))

    from agent_runtime.config import harness_root_config_path, load_agent_runtime_config

    # The fixture is only a fixture if it really installed the disagreement: a
    # test that silently pointed both loaders at one file would pass under every
    # mutation below.
    assert harness_root_config_path() == root / "config.yaml"
    assert read_model_enabled(load_agent_runtime_config()) is profile_enabled
    return root


@pytest.mark.parametrize(
    "root_enabled,profile_enabled,expected_source",
    [
        pytest.param(True, False, "cache", id="root_true_profile_false"),
        pytest.param(False, True, "built", id="root_false_profile_true"),
    ],
)
def test_the_two_snapshot_entry_points_answer_the_flag_identically(
    tmp_path, monkeypatch, capsys, root_enabled, profile_enabled, expected_source
):
    """The write gate and the serve preference agree, for the same key.

    MEASURED MUTATION (RD-H6's, adjusted for HEAD — see this file's companion
    note in the stage report): point ``read_model_enabled`` at
    ``load_agent_runtime_config`` (profile-aware) instead of
    ``load_root_runtime_config``. ``root_true_profile_false`` then answers
    ``built`` where ``cache`` is demanded, and ``root_false_profile_true``
    answers ``cache_miss_rebuilt`` where ``built`` is demanded. The two fixtures
    disagree in OPPOSITE directions, so no single scope choice other than the
    write gate's own can satisfy the pair.

    ``cache`` is the load-bearing value: it requires the write gate to have
    populated the read model AND the serve preference to have consulted it. It is
    the only one of the three sources that cannot be reached by one half of the
    seam acting alone.
    """

    import hermes_cli.harness as harness

    _config_tree(
        tmp_path, monkeypatch, root_enabled=root_enabled, profile_enabled=profile_enabled
    )

    assert harness._cmd_snapshot(Namespace(json=True)) == 0
    printed = json.loads(capsys.readouterr().out)

    assert printed["parity"]["frame_source"] == expected_source
    # Second, independent witness on the WRITE half: the read model is populated
    # exactly when the root said so. Read off the database, not off the frame, so
    # a serve path that fabricated a source string still fails.
    assert (ReadModel().render_snapshot() is not None) is root_enabled
    # And the frame is never the empty object the old branch could print.
    assert printed["schema_version"] == 2
    assert paths.snapshot_path().exists()


def test_the_resolver_answers_root_scope_and_takes_an_explicit_config(
    tmp_path, monkeypatch
):
    """The resolver's whole public shape, stated once.

    Two arms, and the second is what makes the first a CHOICE rather than the
    only thing the function can do: ``config=None`` is root scope, and an
    explicitly passed config is answered as given. A resolver with only the
    first arm would force any caller holding a resolved config to re-derive the
    ladder — which is how the second copy appeared in the first place.
    """

    from agent_runtime.config import load_agent_runtime_config

    _config_tree(tmp_path, monkeypatch, root_enabled=True, profile_enabled=False)

    assert read_model_enabled() is True
    assert read_model_enabled(load_agent_runtime_config()) is False
    # The composite the serve paths ask: both flags, one answer.
    assert prefer_cached_snapshot() is True
    assert prefer_cached_snapshot(load_agent_runtime_config()) is False


def test_serve_snapshot_from_db_off_never_prefers_the_cache():
    """``enabled`` alone must not make a serve path prefer the cache.

    The composite exists because the two flags are only meaningful together, and
    a mutant that returns ``read_model_enabled(cfg)`` and drops the second
    conjunct passes every other test in this file.
    """

    on = RuntimeConfig(read_model=ReadModelConfig(enabled=True, serve_snapshot_from_db=True))
    off = RuntimeConfig(read_model=ReadModelConfig(enabled=True, serve_snapshot_from_db=False))
    dark = RuntimeConfig(read_model=ReadModelConfig(enabled=False, serve_snapshot_from_db=True))

    assert prefer_cached_snapshot(on) is True
    assert prefer_cached_snapshot(off) is False
    assert prefer_cached_snapshot(dark) is False
    # A config object carrying no ``read_model`` block at all has told us
    # nothing, and nothing resolves to the cache.
    assert read_model_enabled(object()) is False
    assert prefer_cached_snapshot(object()) is False


# ── the fence: the question stays answered in one place ──────────────────────
#
# The consolidation is only durable if a THIRD copy is loud. This scan is the
# enumeration witness: it fails by naming any new site that reads ``enabled``
# off a ``read_model`` config block outside the sanctioned set.
#
# ``agent_runtime/snapshot.py::write_snapshot`` is listed as a PENDING row, not
# as a sanctioned one. Its inline ladder is the second copy this stage exists to
# retire; the swap was deferred because ``snapshot.py`` was held by the
# concurrent EG-3.1 branch when RD-H6 landed. Deleting that row (and the two
# lines it names) is the follow-up — and the fence goes red the moment anyone
# adds a copy that is NOT one of these two.

SANCTIONED_READERS = {
    ("agent_runtime/read_model.py", "read_model_enabled"),
}

PENDING_SWAP_READERS: set[tuple[str, str]] = set()
# The snapshot.py write_snapshot row was retired 2026-08-17: the call site now
# rides ``read_model_enabled()`` (the EG-6.3 follow-up, landed after EG-3.1).

SCANNED_PACKAGES = ("agent_runtime", "hermes_cli")


def _enclosing_functions(tree: ast.AST) -> dict[ast.AST, str]:
    owners: dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            owners.setdefault(child, node.name)
    return owners


def _reads_enabled_off_a_read_model_block(node: ast.AST) -> bool:
    """Both spellings of the ladder, read off the parser rather than grepped.

    ``getattr(<x>, "enabled", …)`` and ``<x>.enabled`` where ``<x>``'s source
    text mentions ``read_model``. Docstrings and comments cannot match, which is
    the whole reason this is an AST scan (the S48 precedent).
    """

    if isinstance(node, ast.Call):
        func = node.func
        if (
            isinstance(func, ast.Name)
            and func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "enabled"
        ):
            return "read_model" in ast.unparse(node.args[0])
        return False
    if isinstance(node, ast.Attribute) and node.attr == "enabled":
        return "read_model" in ast.unparse(node.value)
    return False


def test_read_model_enabled_is_read_in_exactly_the_declared_places():
    found: set[tuple[str, str]] = set()
    for package in SCANNED_PACKAGES:
        for path in sorted((REPO_ROOT / package).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            owners = _enclosing_functions(tree)
            for node in ast.walk(tree):
                if not _reads_enabled_off_a_read_model_block(node):
                    continue
                rel = path.relative_to(REPO_ROOT).as_posix()
                found.add((rel, owners.get(node, "<module>")))

    assert found == SANCTIONED_READERS | PENDING_SWAP_READERS, (
        "the set of places that read ``read_model.enabled`` changed.\n"
        f"  found:    {sorted(found)}\n"
        f"  declared: {sorted(SANCTIONED_READERS | PENDING_SWAP_READERS)}\n"
        "A NEW site is the RD-H6 defect re-growing: one config leaf answered at "
        "more than one resolution scope, whose disagreement renders as a cold "
        "cache rather than as an error. Call "
        "``agent_runtime.read_model.read_model_enabled`` instead. If you retired "
        "the pending snapshot.py site, delete its row from "
        "PENDING_SWAP_READERS in this file."
    )
    # The scan must be able to SEE something, or an over-narrow matcher would
    # report an empty set and pass forever.
    assert SANCTIONED_READERS <= found
