"""MCF-66 — the suite must never read or WRITE the operator's Claude Code login.

``~/.claude/.credentials.json`` is Claude Code's file. It lives deliberately
outside Hermes home (``credential_sources.py`` refuses to delete it for that
reason), so ``HERMES_HOME`` never sandboxed it, and ``tests/conftest.py``
states in its own header that it does NOT redirect ``HOME`` — "that broke
subprocesses in CI". ``Path.home()`` inside a test therefore resolves to the
operator's real profile.

Two halves are proved here.

**The neutralization** (``_neutralize_claude_code_credentials_file`` in
``tests/conftest.py``) replaces both module attributes:
``agent.anthropic_adapter._read_claude_code_credentials_from_file`` returns
``None`` and ``agent.anthropic_adapter._write_claude_code_credentials`` is a
no-op. Every proof below runs against a SYNTHETIC home under ``tmp_path``;
none of them can observe the host file, by construction. Delete either patch
in the conftest fixture and the test named after it goes red.

**The gate** — an AST census over ``tests/``. Three hand-rolled copies of this
defence already existed at three seams
(``test_credential_pool_rotation_cursor.py``, ``test_credential_pool.py``,
``test_credential_pool_routing.py``): one concept, three encodings, nothing
holding the seam. The census replaces "somebody remembered" with a rule:

* a node that opts out via ``allow_claude_code_credentials_file`` must also
  redirect ``Path.home()`` at a tmpdir — the marker alone hands back the real
  file, which is the defect, not the fix; and
* a module that binds a surface function by **direct import** must opt in,
  because a direct import captured the original function object before the
  fixture patched the module attribute, so the autouse neutralization does not
  cover it.

No test is allowlisted and no real token is seeded anywhere. Production
behaviour is unchanged: nothing in ``agent/anthropic_adapter.py`` moved.
"""

from __future__ import annotations

import ast
import json
import warnings
from pathlib import Path

import pytest

# ── Synthetic values. Never a real token; nothing here is read from disk. ────
_SYNTHETIC_ACCESS = "synthetic-access-not-a-real-token"
_SYNTHETIC_REFRESH = "synthetic-refresh-not-a-real-token"
_SYNTHETIC_ROTATED = "synthetic-rotated-not-a-real-token"
_FAR_FUTURE_MS = 4_102_444_800_000  # 2100-01-01, comfortably unexpired.

_MARKER = "allow_claude_code_credentials_file"
_SURFACE = (
    "_read_claude_code_credentials_from_file",
    "_write_claude_code_credentials",
)


def _synthetic_home(monkeypatch, tmp_path, *, seed_credentials: bool = False) -> Path:
    """Point the adapter's ``Path.home()`` at ``tmp_path``.

    Every proof in this file runs here, so a regression in the neutralization
    surfaces as "the synthetic file was read/written" — never as a read of the
    operator's real login.
    """
    import agent.anthropic_adapter as aa

    monkeypatch.setattr(aa.Path, "home", lambda: tmp_path)
    if seed_credentials:
        cred_file = tmp_path / ".claude" / ".credentials.json"
        cred_file.parent.mkdir(parents=True, exist_ok=True)
        cred_file.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": _SYNTHETIC_ACCESS,
                        "refreshToken": _SYNTHETIC_REFRESH,
                        "expiresAt": _FAR_FUTURE_MS,
                    }
                }
            ),
            encoding="utf-8",
        )
    return tmp_path


def _credential_files_under(root: Path) -> list[Path]:
    return sorted(root.rglob(".credentials.json"))


# ── The reader ──────────────────────────────────────────────────────────────


def test_credentials_file_reader_is_neutralized(tmp_path, monkeypatch):
    """A readable credentials file must still yield nothing under test.

    Kill proof: drop the ``_read_claude_code_credentials_from_file`` patch from
    ``_neutralize_claude_code_credentials_file`` and this goes red — the
    synthetic token comes back, which on an unsandboxed home is the operator's.
    """
    import agent.anthropic_adapter as aa

    home = _synthetic_home(monkeypatch, tmp_path, seed_credentials=True)
    assert _credential_files_under(home), "fixture did not seed the synthetic file"

    assert aa._read_claude_code_credentials_from_file() is None
    assert aa.read_claude_code_credentials() is None


def test_credentials_file_reader_is_the_neutralized_stub():
    """The module attribute itself must be the conftest stub, not the real reader."""
    import agent.anthropic_adapter as aa

    assert getattr(
        aa._read_claude_code_credentials_from_file, "_hermes_neutralized", False
    ), "_read_claude_code_credentials_from_file is NOT neutralized in this test run"


# ── The writer ──────────────────────────────────────────────────────────────


def test_credentials_file_writer_is_neutralized(tmp_path, monkeypatch):
    """The writer must put nothing on disk.

    Kill proof: drop the ``_write_claude_code_credentials`` patch from
    ``_neutralize_claude_code_credentials_file`` and this goes red — the file
    appears, which on an unsandboxed home is the operator's live login
    overwritten with whatever a mocked refresh endpoint returned.
    """
    import agent.anthropic_adapter as aa

    home = _synthetic_home(monkeypatch, tmp_path)

    aa._write_claude_code_credentials(
        _SYNTHETIC_ACCESS, _SYNTHETIC_REFRESH, _FAR_FUTURE_MS
    )

    assert _credential_files_under(home) == []


def test_credentials_file_writer_is_the_neutralized_stub():
    import agent.anthropic_adapter as aa

    assert getattr(
        aa._write_claude_code_credentials, "_hermes_neutralized", False
    ), "_write_claude_code_credentials is NOT neutralized in this test run"


def test_token_refresh_path_cannot_write_the_credentials_file(
    tmp_path, monkeypatch, _neutralize_claude_code_credentials_file
):
    """The adapter's own refresh path reaches the writer — and lands in the stub.

    ``_refresh_oauth_token`` POSTs the refresh token and then persists the
    rotated pair. This is the specific route that could overwrite the
    operator's Claude Code login from a test whose network layer is mocked.
    """
    import agent.anthropic_adapter as aa

    home = _synthetic_home(monkeypatch, tmp_path)
    counts = _neutralize_claude_code_credentials_file

    monkeypatch.setattr(aa, "read_claude_code_credentials", lambda: None)
    monkeypatch.setattr(
        aa,
        "refresh_anthropic_oauth_pure",
        lambda *_a, **_k: {
            "access_token": _SYNTHETIC_ROTATED,
            "refresh_token": _SYNTHETIC_REFRESH,
            "expires_at_ms": _FAR_FUTURE_MS,
        },
    )

    result = aa._refresh_oauth_token(
        {"accessToken": _SYNTHETIC_ACCESS, "refreshToken": _SYNTHETIC_REFRESH}
    )

    assert result == _SYNTHETIC_ROTATED, "refresh path did not run to completion"
    assert counts["write"] == 1, "the refresh path did NOT reach the writer stub"
    assert _credential_files_under(home) == []


def test_pool_refresh_path_cannot_write_the_credentials_file(
    tmp_path, monkeypatch, _neutralize_claude_code_credentials_file
):
    """``CredentialPool._refresh_entry`` write-back also lands in the stub.

    ``credential_pool`` late-imports ``_write_claude_code_credentials`` inside
    the refresh, so the module-attribute patch covers it — but only because
    the import is late. Assert it, rather than assume it.
    """
    import agent.anthropic_adapter as aa
    from agent.credential_pool import (
        AUTH_TYPE_OAUTH,
        CredentialPool,
        PooledCredential,
    )

    home = _synthetic_home(monkeypatch, tmp_path)
    counts = _neutralize_claude_code_credentials_file

    monkeypatch.setattr(
        aa,
        "refresh_anthropic_oauth_pure",
        lambda *_a, **_k: {
            "access_token": _SYNTHETIC_ROTATED,
            "refresh_token": _SYNTHETIC_REFRESH,
            "expires_at_ms": _FAR_FUTURE_MS,
        },
    )

    entry = PooledCredential(
        provider="anthropic",
        id="cc-1",
        label="cc",
        auth_type=AUTH_TYPE_OAUTH,
        priority=0,
        source="claude_code",
        access_token=_SYNTHETIC_ACCESS,
        refresh_token=_SYNTHETIC_REFRESH,
    )
    pool = CredentialPool("anthropic", [entry])

    refreshed = pool._refresh_entry(entry, force=True)

    assert refreshed is not None and refreshed.access_token == _SYNTHETIC_ROTATED
    assert counts["write"] >= 1, "the pool refresh did NOT reach the writer stub"
    assert _credential_files_under(home) == []


# ── The two routes into the reader ──────────────────────────────────────────


def test_ungated_available_entries_route_lands_in_the_stub(
    tmp_path, monkeypatch, _neutralize_claude_code_credentials_file
):
    """Route 1: ``_available_entries`` -> ``_sync_anthropic_entry_from_credentials_file``.

    This route has NO gate. It fires whenever a hermetic pool holds an
    anthropic entry with ``source="claude_code"`` and an exhausted/dead
    ``last_status`` — a shape three test files construct today.
    """
    from agent.credential_pool import (
        AUTH_TYPE_OAUTH,
        STATUS_EXHAUSTED,
        CredentialPool,
        PooledCredential,
    )

    _synthetic_home(monkeypatch, tmp_path, seed_credentials=True)
    counts = _neutralize_claude_code_credentials_file

    entry = PooledCredential(
        provider="anthropic",
        id="cc-1",
        label="cc",
        auth_type=AUTH_TYPE_OAUTH,
        priority=0,
        source="claude_code",
        access_token=_SYNTHETIC_ACCESS,
        refresh_token=_SYNTHETIC_REFRESH,
        last_status=STATUS_EXHAUSTED,
    )
    pool = CredentialPool("anthropic", [entry])
    available = pool._available_entries()

    assert counts["read"] > 0, "route 1 did NOT reach the credentials-file reader"
    # Neutralized: the synthetic file's tokens must not have been synced in.
    assert [e.access_token for e in available] == [_SYNTHETIC_ACCESS]


def test_gated_seed_from_singletons_route_lands_in_the_stub(
    tmp_path, monkeypatch, _neutralize_claude_code_credentials_file
):
    """Route 2: ``_seed_from_singletons`` -> ``read_claude_code_credentials()``.

    Gated by ``is_provider_explicitly_configured("anthropic")``, which reads
    files ``HERMES_HOME`` *does* sandbox — so it is blocked by default. That
    is incidental, not a defence: one line of fixture (``active_provider:
    anthropic`` in the test's own hermetic ``auth.json``) opens it, and the
    suite already writes exactly that line elsewhere. Both states are asserted.
    """
    from agent import credential_pool as CP

    _synthetic_home(monkeypatch, tmp_path, seed_credentials=True)
    counts = _neutralize_claude_code_credentials_file

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    entries: list = []
    CP._seed_from_singletons("anthropic", entries)
    assert counts["read"] == 0, "the explicit-configuration gate did not hold"

    (hermes_home / "auth.json").write_text(
        json.dumps({"version": 1, "providers": {}, "active_provider": "anthropic"}),
        encoding="utf-8",
    )
    CP._seed_from_singletons("anthropic", entries)

    assert counts["read"] > 0, "route 2 did not open when anthropic is configured"
    # Neutralized: nothing was autodiscovered out of the synthetic file.
    assert [e.source for e in entries if e.source == "claude_code"] == []


# ── The gate: an AST census over tests/ ─────────────────────────────────────


def _test_modules() -> list[Path]:
    tests_root = Path(__file__).resolve().parent
    return sorted(
        p
        for p in tests_root.rglob("test_*.py")
        if "__pycache__" not in p.parts
    )


_PARSE_CACHE: dict[Path, "ast.Module | None"] = {}

# Tokens that make a module worth PARSING. Reading ~2400 files is cheap;
# compiling them all is not, and every module without one of these strings is
# provably irrelevant to both rules below — the marker rule keys on the marker
# name, the direct-import rule on a surface name. This is a pre-filter, never
# an allowlist: the tokens are derived from the rules, not from a file list,
# and ``test_gate_census_actually_sees_the_suite`` fails if it selects nothing.
_RELEVANT_TOKENS = (_MARKER, *_SURFACE)


def _parse(path: Path) -> ast.Module | None:
    """Parse once per session. ``ast.parse`` compiles, so it re-emits every
    SyntaxWarning already present in the suite — silenced here so the census
    reports its own findings and nothing else."""
    if path not in _PARSE_CACHE:
        try:
            source = path.read_text(encoding="utf-8")
            tree = (
                None
                if not any(token in source for token in _RELEVANT_TOKENS)
                else _compile(source, path)
            )
        except (OSError, UnicodeDecodeError):
            tree = None
        _PARSE_CACHE[path] = tree
    return _PARSE_CACHE[path]


def _compile(source: str, path: Path) -> ast.Module | None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return ast.parse(source, filename=str(path))
    except SyntaxError:
        return None


def _marker_names(node: ast.AST) -> set[str]:
    """Marker names attached to *node* via decorators or a module ``pytestmark``."""
    names: set[str] = set()

    def _from_expr(expr: ast.AST) -> None:
        # pytest.mark.NAME / pytest.mark.NAME(...) / a list of either
        if isinstance(expr, ast.Call):
            expr = expr.func
        if isinstance(expr, (ast.List, ast.Tuple)):
            for element in expr.elts:
                _from_expr(element)
            return
        if isinstance(expr, ast.Attribute):
            parent = expr.value
            if isinstance(parent, ast.Attribute) and parent.attr == "mark":
                names.add(expr.attr)

    if isinstance(node, ast.Module):
        for stmt in node.body:
            targets = (
                stmt.targets
                if isinstance(stmt, ast.Assign)
                else ([stmt.target] if isinstance(stmt, ast.AnnAssign) else [])
            )
            if any(
                isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets
            ) and stmt.value is not None:
                _from_expr(stmt.value)
    else:
        for decorator in getattr(node, "decorator_list", []):
            _from_expr(decorator)
    return names


def _walk_code(node: ast.AST):
    """Walk *node*, skipping bare string expressions.

    A docstring that merely *claims* the test redirects ``Path.home()`` must
    not satisfy the rule — that was a live hole in the first cut of this
    census, and the docstring that fooled it was one written for this very
    commit. Only executable syntax counts.
    """
    stack = [node]
    while stack:
        current = stack.pop()
        for child in ast.iter_child_nodes(current):
            if isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant):
                continue  # bare string / docstring — commentary, not behaviour
            stack.append(child)
            yield child


def _redirects_home_here(node: ast.AST) -> bool:
    """True when *node*'s own body points ``~`` at something the test owns.

    Accepts every spelling the suite actually uses: a patched
    ``Path.home`` attribute or string target, a ``"home"`` setattr name, or
    ``tests._home_env.point_home_at``.
    """
    for child in _walk_code(node):
        if isinstance(child, ast.Attribute) and child.attr == "home":
            return True
        if isinstance(child, ast.Name) and child.id == "point_home_at":
            return True
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            if child.value == "home" or "Path.home" in child.value:
                return True
    return False


def _function_defs(tree: ast.Module) -> dict[str, ast.AST]:
    """Every ``def`` in the module, by name — the fixture lookup table."""
    defs: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.setdefault(node.name, node)
    return defs


def _redirects_home(node: ast.AST, defs: dict[str, ast.AST], _depth: int = 3) -> bool:
    """True when *node* — or a fixture it requests — redirects ``Path.home()``.

    A test may satisfy the rule through a fixture it names as a parameter
    (``claude_code_only_env`` does exactly that), so the check follows
    parameter names into the module's own ``def``s rather than settling for
    "somewhere in this file", which would pass every module that mentions
    ``home`` anywhere.
    """
    if _redirects_home_here(node):
        return True
    if _depth <= 0:
        return False
    for child in [node, *ast.walk(node)]:
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for arg in child.args.args + child.args.kwonlyargs:
            target = defs.get(arg.arg)
            if target is not None and target is not child:
                if _redirects_home(target, defs, _depth - 1):
                    return True
    return False


def _marked_scopes(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    """Every scope carrying the opt-in marker, paired with the body that must
    redirect home. A function inside a class may satisfy the rule via a
    sibling helper, so the class body is what gets checked for class-level
    markers — and for a function-level marker, the function itself."""
    scopes: list[tuple[str, ast.AST]] = []
    if _MARKER in _marker_names(tree):
        scopes.append(("<module>", tree))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if _MARKER in _marker_names(node):
                scopes.append((node.name, node))
    return scopes


def _direct_surface_imports(tree: ast.Module) -> set[str]:
    """Surface functions bound by ``from agent.anthropic_adapter import NAME``."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "agent.anthropic_adapter":
            for alias in node.names:
                if alias.name in _SURFACE:
                    found.add(alias.name)
    return found


def test_gate_opt_in_requires_a_redirected_home():
    """Opting out of the neutralization without redirecting ``~`` IS the defect.

    Kill proof: strip the ``Path.home`` patch from any marked class and this
    names the file, the scope, and the missing redirect.
    """
    offenders: list[str] = []
    for path in _test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        defs = _function_defs(tree)
        for name, scope in _marked_scopes(tree):
            if not _redirects_home(scope, defs):
                offenders.append(f"{path.name}::{name}")

    assert not offenders, (
        f"scopes marked @pytest.mark.{_MARKER} that never redirect Path.home() "
        f"— they read/write the operator's real ~/.claude/.credentials.json: "
        f"{offenders}"
    )


def test_gate_direct_surface_import_requires_opt_in():
    """A direct import outruns the autouse patch, so it must declare itself.

    ``monkeypatch.setattr(module, name, stub)`` rebinds the module attribute.
    A module that did ``from agent.anthropic_adapter import
    _write_claude_code_credentials`` at import time holds the ORIGINAL
    function and calls the real writer regardless. Such a module must carry
    the opt-in marker, which the rule above then forces to redirect home.
    """
    offenders: list[str] = []
    for path in _test_modules():
        tree = _parse(path)
        if tree is None:
            continue
        imported = _direct_surface_imports(tree)
        if not imported:
            continue
        marked = {name for name, _ in _marked_scopes(tree)}
        if not marked:
            offenders.append(f"{path.name} (direct-imports {sorted(imported)})")

    assert not offenders, (
        f"test modules that bind {list(_SURFACE)} by direct import — the "
        f"autouse neutralization patches the module attribute and cannot "
        f"reach them — without declaring @pytest.mark.{_MARKER}: {offenders}"
    )


def test_gate_census_actually_sees_the_suite():
    """A census over zero files is a green light that proves nothing.

    Three ways this file could go quietly vacuous, all checked: the glob
    finding nothing, the relevance pre-filter selecting nothing, and the
    marker resolving against nothing.
    """
    modules = _test_modules()
    assert len(modules) > 100, f"census only found {len(modules)} test modules"

    parsed = [path for path in modules if _parse(path) is not None]
    assert parsed, (
        "the relevance pre-filter selected ZERO modules — no file under "
        f"tests/ contains any of {list(_RELEVANT_TOKENS)}, so both rules "
        "below would pass by looking at nothing"
    )

    marked = [
        path.name
        for path in parsed
        if _marked_scopes(_parse(path))  # type: ignore[arg-type]
    ]
    assert marked, (
        "no scope anywhere declares the opt-in marker — either the marker was "
        "renamed or the census is looking in the wrong place"
    )


def test_gate_marker_is_registered():
    """An unregistered marker silently does nothing under ``--strict-markers``."""
    conftest = Path(__file__).resolve().parent / "conftest.py"
    source = conftest.read_text(encoding="utf-8")
    assert f'"{_MARKER}"' in source, f"{_MARKER} is not defined in tests/conftest.py"
    for surface in _SURFACE:
        assert f'"{surface}"' in source, (
            f"tests/conftest.py no longer patches {surface} — the "
            f"neutralization has a hole"
        )


@pytest.mark.allow_claude_code_credentials_file
class TestOptInStillNeverTouchesTheHostFile:
    """The opt-in half, exercised: real code path, synthetic home, no leak."""

    def test_real_reader_runs_against_the_tmpdir_only(self, tmp_path, monkeypatch):
        import agent.anthropic_adapter as aa

        monkeypatch.setattr(aa.Path, "home", lambda: tmp_path)
        assert not getattr(
            aa._read_claude_code_credentials_from_file, "_hermes_neutralized", False
        ), "the marker did not disengage the neutralization"

        # Empty synthetic home → the REAL reader must report nothing.
        assert aa._read_claude_code_credentials_from_file() is None

        _synthetic_home(monkeypatch, tmp_path, seed_credentials=True)
        creds = aa._read_claude_code_credentials_from_file()
        assert creds is not None
        assert creds["accessToken"] == _SYNTHETIC_ACCESS
        assert creds["source"] == "claude_code_credentials_file"

    def test_real_writer_lands_inside_the_tmpdir_only(self, tmp_path, monkeypatch):
        import agent.anthropic_adapter as aa

        monkeypatch.setattr(aa.Path, "home", lambda: tmp_path)
        aa._write_claude_code_credentials(
            _SYNTHETIC_ACCESS, _SYNTHETIC_REFRESH, _FAR_FUTURE_MS
        )

        written = _credential_files_under(tmp_path)
        assert written == [tmp_path / ".claude" / ".credentials.json"]
        payload = json.loads(written[0].read_text(encoding="utf-8"))
        assert payload["claudeAiOauth"]["accessToken"] == _SYNTHETIC_ACCESS
