"""No guard under ``tools/`` may decide path identity by comparing raw strings.

The ruling this enforces
------------------------

``os.path.normpath`` / ``realpath`` / ``abspath`` return *characters*. Comparing
those characters with ``==`` / ``in`` / ``startswith`` is not an answer to "is
this the same file", and on this tree it was wrong four times in one night:

* ``normpath("/etc/hosts")`` is ``"\\etc\\hosts"`` wherever ``os.sep`` is a
  backslash, so ``.startswith("/etc")`` returned False and the sensitive-path
  guard answered ALLOW — a **security bypass**, silent, on every Windows host.
* The identical defect had already been found and fixed for
  ``_BLOCKED_DEVICE_PATHS`` three hundred lines above it, and left open-coded,
  which is precisely how the second instance survived the first fix.
* ``normpath(a) == normpath(b)`` in the browser-daemon reaper folded neither
  case nor symlinks, so a daemon could not be recognised as its own.

So the technique gets one home — ``tools/path_identity.py`` — and this gate
stops the next guard from re-deriving it. It is an **AST** gate: it resolves the
producing call and follows the value, rather than grepping for prose. Source-grep
assertions are a retired class here (see ``tests/test_no_source_grep_assertions.py``
and the ``getsource`` ledger) and this file must not reintroduce one.

What is flagged
---------------

Inside ``tools/``, a value produced by ``os.path.normpath``, ``os.path.realpath``
or ``os.path.abspath`` — directly, through a local assignment, or through
``dirname`` / ``basename`` / ``str`` of one — appearing as an operand of
``==`` / ``!=`` / ``in`` / ``not in``, or as the receiver of ``.startswith`` /
``.endswith``.

Known bounds, stated rather than hidden — a gate that overclaims its reach is
the same lie it exists to stop:

1. **``Path.resolve()`` comparisons are NOT flagged.** ``resolve()`` is the
   correct primitive: it follows symlinks and recovers on-disk case, and
   :func:`tools.path_identity.denotes_same_file` is built on the same
   resolution. Roughly eight ``resolve() == resolve()`` sites under ``tools/``
   are correct as written; flagging them would bury the three real ones under an
   allowlist nobody reads. The defect class is the *string* canonicalizers.
2. **Local dataflow only.** A tainted value passed into another function, stored
   on an attribute, or routed through a container is not tracked across that
   boundary. Widening that needs real dataflow and would be its own pass.
3. **Comparison operators only.** ``sorted()``, ``set()`` de-duplication and
   dict keying over path strings are the same defect wearing different syntax
   and are not caught.
4. **Exemptions are per FUNCTION, not per line.** A registered function's other
   comparisons are exempt too, so a new defect can hide inside one. Line
   granularity was rejected because it rots on every edit above the site, which
   is how an allowlist becomes something people update without reading. Keep
   registered functions small, and re-read the reason when you edit one.

Every exemption is registered below **with a reason**, never as a bare skip, and
a registration that no longer corresponds to a real finding FAILS this gate
rather than rotting in place — the same contract ``_ENV_GAP_SKIPS`` runs under.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_ROOT = REPO_ROOT / "tools"

#: The module that is allowed — required, in fact — to compare canonicalized
#: path strings. It is the authority; the comparison has to happen somewhere.
AUTHORITY = "tools/path_identity.py"

#: The three ``os.path`` canonicalizers that return a *string*. ``Path.resolve``
#: is deliberately absent — see bound (1) in the module docstring.
_CANONICALIZERS = frozenset({"normpath", "realpath", "abspath"})

#: Calls that carry taint through: the result still denotes a path derived from
#: a canonicalized one. ``join`` is here because the shipped approval guard
#: compares an operand against ``os.path.join(realpath(tempdir), basename)`` —
#: the canonicalized value is the FIRST argument, and dropping the taint there
#: would let the whole literal-spelling family through unseen.
_TAINT_PRESERVING = frozenset({"dirname", "basename", "join", "str"})

_COMPARE_METHODS = frozenset({"startswith", "endswith"})
_COMPARE_OPS = (ast.Eq, ast.NotEq, ast.In, ast.NotIn)

#: How few files would mean the walker broke rather than the tree being clean.
#: ``tools/`` carries well over a hundred modules; a collapse to double digits is
#: the walker drifting, not a cleanup.
_MIN_SCANNED_FILES = 60


#: ``(module, function) -> reason``. A registration is a claim that the raw
#: comparison at that site is CORRECT, and the claim has to be defensible on its
#: own terms. "It has always been like that" is not a reason.
_RAW_PATH_COMPARISON_EXEMPTIONS: dict[tuple[str, str], str] = {
    ("tools/approval.py", "_is_exempt_verification_artifact_path"): (
        "Deliberate LITERAL-SPELLING guard, not an identity question. The "
        "operand must be written as the canonical temp dir joined with a bare "
        "basename; that string equality is exactly what refuses "
        "'/tmp/nested/../x', '/var/tmp/x' and every alternate spelling of "
        "temp. Replacing it with denotes_same_file would delete the traversal "
        "rejection outright. The identity half of the same function — that the "
        "operand still resolves into temp — DOES go through path_identity."
    ),
    ("tools/file_tools.py", "_is_blocked_device"): (
        "Not a path guard: 'target in seen' is the cycle breaker for the "
        "symlink-hop walk. It compares a path against paths already VISITED IN "
        "THIS WALK, all produced by the same normpath call two lines above, so "
        "spelling agreement is guaranteed by construction. Resolving here would "
        "collapse the hops the walk exists to inspect one at a time — every "
        "device check in the loop already runs through _posix_match_forms."
    ),
    ("tools/terminal_tool.py", "_get_env_config"): (
        "Classifies a cwd as host-shaped vs container-shaped by SPELLING, which "
        "is the question being asked: _HOST_CWD_PREFIXES is ('/Users/', "
        "'/home/', 'C:\\\\', 'C:/') and '/workspace'//root' are container-side "
        "roots that need not exist on this host. A resolution-based test cannot "
        "answer 'does this look like a host path' — realpath would anchor the "
        "container spellings to the host filesystem and invert the verdict."
    ),
}


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _is_canonicalizer(call: ast.Call) -> bool:
    """``os.path.normpath(...)`` and friends, under any import spelling.

    Matched on the attribute/name alone: ``normpath`` is not a word anything
    else in this tree calls, and requiring the ``os.path`` prefix would miss
    ``from os.path import realpath``.
    """
    return _called_name(call) in _CANONICALIZERS


class _FunctionScan:
    """Taint within one function body: assignments plus in-place expressions."""

    def __init__(self, fn: ast.AST) -> None:
        self.fn = fn
        self.tainted_names: set[str] = set()
        self._seed()

    def _seed(self) -> None:
        # Fixpoint: `a = realpath(x)` then `b = dirname(a)` taints both.
        changed = True
        while changed:
            changed = False
            for node in ast.walk(self.fn):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                value = node.value
                if value is None or not self._expr_is_tainted(value):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in self.tainted_names:
                        self.tainted_names.add(target.id)
                        changed = True

    def _expr_is_tainted(self, expr: ast.AST) -> bool:
        if isinstance(expr, ast.Name):
            return expr.id in self.tainted_names
        if isinstance(expr, ast.Call):
            if _is_canonicalizer(expr):
                return True
            if _called_name(expr) in _TAINT_PRESERVING:
                return any(self._expr_is_tainted(arg) for arg in expr.args)
            return False
        if isinstance(expr, ast.Subscript):
            return self._expr_is_tainted(expr.value)
        if isinstance(expr, ast.BinOp):
            return self._expr_is_tainted(expr.left) or self._expr_is_tainted(expr.right)
        return False

    def findings(self) -> list[int]:
        hits: list[int] = []
        for node in ast.walk(self.fn):
            if isinstance(node, ast.Compare):
                if not any(isinstance(op, _COMPARE_OPS) for op in node.ops):
                    continue
                operands = [node.left, *node.comparators]
                if any(self._expr_is_tainted(operand) for operand in operands):
                    hits.append(node.lineno)
            elif isinstance(node, ast.Call) and _called_name(node) in _COMPARE_METHODS:
                func = node.func
                if isinstance(func, ast.Attribute) and self._expr_is_tainted(func.value):
                    hits.append(node.lineno)
        return hits


def scan_tree(tree: ast.AST) -> dict[str, list[int]]:
    """``function name -> line numbers`` of raw canonicalized-path comparisons.

    Exposed (rather than inlined) so the vacuity pins below can drive the very
    same detector over a known-bad and a known-good sample.
    """
    found: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        hits = _FunctionScan(node).findings()
        if hits:
            found.setdefault(node.name, []).extend(hits)
    return found


def _scan_tools() -> tuple[dict[tuple[str, str], list[int]], int]:
    findings: dict[tuple[str, str], list[int]] = {}
    scanned = 0
    for py in sorted(TOOLS_ROOT.rglob("*.py")):
        rel = py.relative_to(REPO_ROOT).as_posix()
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        scanned += 1
        if rel == AUTHORITY:
            continue
        for fn_name, lines in scan_tree(tree).items():
            findings[(rel, fn_name)] = sorted(lines)
    return findings, scanned


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_no_unregistered_raw_path_comparison_under_tools():
    findings, scanned = _scan_tools()
    assert scanned >= _MIN_SCANNED_FILES, (
        f"only {scanned} modules parsed under tools/ — the walker is broken, "
        f"not the tree clean"
    )

    unregistered = sorted(k for k in findings if k not in _RAW_PATH_COMPARISON_EXEMPTIONS)
    if unregistered:
        detail = "\n".join(
            f"  {module}::{fn}  line(s) {findings[(module, fn)]}"
            for module, fn in unregistered
        )
        pytest.fail(
            "These sites decide path questions by comparing raw "
            "normpath/realpath/abspath STRINGS:\n"
            f"{detail}\n\n"
            "That is the defect class retired by tools/path_identity.py — a "
            "normpath'd POSIX root stops matching its own literal on Windows, "
            "and a string compare folds neither case nor symlinks. Route it "
            "through path_identity.denotes_same_file / posix_match_forms, or, "
            "if the raw comparison is genuinely the right question (a literal "
            "spelling guard, a cycle breaker), register it in "
            "_RAW_PATH_COMPARISON_EXEMPTIONS with the reason."
        )


def test_every_exemption_still_names_a_real_finding():
    """A registration that no longer applies must fail, not rot.

    Without this, the allowlist silently becomes a config blob: sites get fixed
    or deleted and their exemptions stay behind, ready to pre-authorise a future
    defect that happens to land in a function with the same name.
    """
    findings, _ = _scan_tools()
    stale = sorted(k for k in _RAW_PATH_COMPARISON_EXEMPTIONS if k not in findings)
    assert not stale, (
        "These _RAW_PATH_COMPARISON_EXEMPTIONS entries no longer correspond to "
        f"any raw path comparison — delete them: {stale}"
    )


def test_every_exemption_carries_a_reason():
    for key, reason in _RAW_PATH_COMPARISON_EXEMPTIONS.items():
        assert reason and len(reason) >= 80, (
            f"{key} is registered without a defensible reason. An exemption "
            f"states WHY the raw comparison is the correct question here; a "
            f"bare skip is what the registry exists to prevent."
        )


# ---------------------------------------------------------------------------
# Non-vacuity: the detector must be shown to FIRE, and to stay quiet on the
# shape it deliberately permits. A gate that finds nothing because it can see
# nothing passes just as green as a clean tree.
# ---------------------------------------------------------------------------


#: ``tools/file_tools.py::_check_sensitive_path`` as it stood at
#: ``f7fe6ce3e^`` — the shipped security bypass, copied rather than
#: paraphrased. ``normpath("/etc/hosts")`` is ``"\\etc\\hosts"`` on Windows, so
#: every ``startswith(prefix)`` returned False and the function returned None,
#: i.e. ALLOW, for the paths it exists to refuse. Parsed, never executed, so
#: the undefined helpers it calls are immaterial.
_HISTORICAL_BYPASS = '''
import os

def _check_sensitive_path(filepath, task_id="default"):
    try:
        resolved = str(_resolve_path_for_task(filepath, task_id))
    except (OSError, ValueError):
        resolved = filepath
    normalized = os.path.normpath(_expand_tilde(filepath))
    _err = "Refusing to write to sensitive system path"
    for prefix in _SENSITIVE_PATH_PREFIXES:
        if resolved.startswith(prefix) or normalized.startswith(prefix):
            return _err
    if resolved in _SENSITIVE_EXACT_PATHS or normalized in _SENSITIVE_EXACT_PATHS:
        return _err
    hermes_config = _get_hermes_config_resolved()
    if hermes_config and (resolved == hermes_config or normalized == hermes_config):
        return "Refusing to write to Hermes config file"
    return None
'''

#: ``tools/browser_tool.py::_verify_reapable_browser_daemon`` as it stood at
#: ``ad0a8e080``: two normpaths compared as strings, folding neither case nor
#: symlinks, so the reaper could not recognise its own daemon.
_HISTORICAL_REAPER = '''
import os

def _verify_reapable_browser_daemon(env_dir, socket_dir):
    bound = bool(env_dir) and os.path.normpath(env_dir) == \\
        os.path.normpath(socket_dir)
    return bound
'''

#: ``tools/file_tools.py::_is_blocked_device_path`` before the first partial
#: fix — the same normpath defect, three hundred lines above the one above, and
#: fixed there in isolation so the next guard repeated it.
_HISTORICAL_DEVICE_BLOCK = '''
import os

def _is_blocked_device_path(path):
    normalized = os.path.normpath(path)
    if normalized in _BLOCKED_DEVICE_PATHS:
        return True
    if normalized.startswith("/proc/") and normalized.endswith(
        ("/fd/0", "/fd/1", "/fd/2")
    ):
        return True
    return False
'''

_CORRECT_RESOLVE = '''
from pathlib import Path

def _same(a, b):
    return Path(a).resolve() == Path(b).resolve()
'''

_CORRECT_AUTHORITY_CALL = '''
from tools import path_identity

def _check(filepath, config):
    return path_identity.denotes_same_file(filepath, config)
'''


@pytest.mark.parametrize(
    "sample,function",
    [
        (_HISTORICAL_BYPASS, "_check_sensitive_path"),
        (_HISTORICAL_REAPER, "_verify_reapable_browser_daemon"),
        (_HISTORICAL_DEVICE_BLOCK, "_is_blocked_device_path"),
    ],
)
def test_detector_fires_on_each_historical_defect_shape(sample, function):
    """All three mechanisms that actually shipped are caught by this detector."""
    found = scan_tree(ast.parse(sample))
    assert function in found, (
        f"the detector did not flag {function} — it would not have caught the "
        f"defect it was written for"
    )


@pytest.mark.parametrize("sample", [_CORRECT_RESOLVE, _CORRECT_AUTHORITY_CALL])
def test_detector_stays_quiet_on_the_correct_shapes(sample):
    """Bound (1) and the target state are pinned, not merely described.

    If ``Path.resolve()`` comparisons started tripping this gate, ~8 correct
    sites under ``tools/`` would need exemptions and the allowlist would stop
    being readable — which is how an allowlist stops being read.
    """
    assert scan_tree(ast.parse(sample)) == {}


def test_the_authority_itself_is_excluded_and_would_otherwise_be_flagged():
    """The exclusion of ``path_identity`` is load-bearing, not decorative.

    Pinning that the module WOULD be flagged proves the exclusion is doing work,
    so a future rewrite that moves the comparison back out of the authority
    cannot hide behind a rule that never applied.
    """
    tree = ast.parse((REPO_ROOT / AUTHORITY).read_text(encoding="utf-8"))
    assert "posix_match_forms" in scan_tree(tree), (
        "path_identity.posix_match_forms no longer compares the normalized "
        "spelling against the POSIX one — either the reconciliation moved back "
        "out of the authority, or the detector stopped seeing it"
    )
    findings, _ = _scan_tools()
    assert not any(module == AUTHORITY for module, _ in findings), (
        "the authority must be excluded from the tools/ sweep"
    )
