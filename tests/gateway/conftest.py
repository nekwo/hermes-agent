"""Shared fixtures for gateway tests.

The ``_ensure_telegram_mock`` helper guarantees that a minimal mock of
the ``telegram`` package is registered in :data:`sys.modules` **before**
any test file triggers ``from plugins.platforms.telegram.adapter import ...``.

Without this, ``pytest-xdist`` workers that happen to collect
``test_telegram_caption_merge.py`` (bare top-level import, no per-file
mock) first will cache ``ChatType = None`` from the production
ImportError fallback, causing 30+ downstream test failures wherever
``ChatType.GROUP`` / ``ChatType.SUPERGROUP`` is accessed.

Individual test files may still call their own ``_ensure_telegram_mock``
— it short-circuits when the mock is already present.

Plugin-adapter anti-pattern guard
---------------------------------
Tests for platform plugins (``plugins/platforms/<name>/adapter.py``)
must load the adapter via
:func:`tests.gateway._plugin_adapter_loader.load_plugin_adapter`, not by
adding the plugin directory to ``sys.path`` and doing a bare
``from adapter import ...``. The guard at the bottom of this file
scans test module ASTs at collection time and fails collection with a
pointer to the helper if the anti-pattern is detected.

Rationale: every plugin ships its own ``adapter.py``, and two tests each
inserting their plugin dir on ``sys.path[0]`` race for
``sys.modules["adapter"]`` in the same xdist worker. Whichever collects
first wins; the other fails with ``ImportError``, and the polluted
``sys.path`` cascades into unrelated tests. See PR #17764 for the
incident.
"""

import ast
import importlib.util
import os
import socket
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests._env_gap_fence import (
    EnvGapRegistry,
    EnvGapSkipRegistry,
    StaleEntryTracker,
    apply_marks,
    apply_skips,
    register_marks,
)


def make_async_session_db(sync_mock=None):
    """Wrap a sync mock SessionDB in AsyncSessionDB so gateway code that awaits
    the facade works in tests. Returns (facade, sync_mock); configure return
    values and assert calls on sync_mock."""
    from hermes_state import AsyncSessionDB
    sync_mock = sync_mock if sync_mock is not None else MagicMock()
    return AsyncSessionDB(sync_mock), sync_mock


class _FakeEnumMember(str):
    """A python-telegram-bot-faithful stand-in for a ``StrEnum`` member.

    PTB constants (``ParseMode``, ``ChatType``) are ``StrEnum`` members:
    ``str(x)`` and equality give the *value* (``"supergroup"``) while
    ``repr(x)`` shows the qualified *member name*
    (``<ChatType.SUPERGROUP>``). Test stubs that pick only one of those
    shapes break the other consumer: plain strings fail assertions like
    ``"MARKDOWN_V2" in repr(parse_mode)``, while auto-generated MagicMock
    attributes fail the adapter's ``str(chat.type)`` normalization
    (``adapter.py`` ``_build_message_event``). This class satisfies both,
    so every telegram test sees the same semantics regardless of which
    file's mock installed first.
    """

    _qualname: str

    def __new__(cls, enum_name: str, member_name: str, value: str):
        obj = str.__new__(cls, value)
        obj._qualname = f"{enum_name}.{member_name}"
        return obj

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"<{self._qualname}: {str.__repr__(self)}>"


def _fake_str_enum(enum_name: str, **members: str):
    """Build a ``SimpleNamespace``-like enum of :class:`_FakeEnumMember`."""
    from types import SimpleNamespace

    return SimpleNamespace(
        **{name: _FakeEnumMember(enum_name, name, value) for name, value in members.items()}
    )


def _ensure_telegram_mock() -> None:
    """Install a comprehensive telegram mock in sys.modules.

    Idempotent — skips when the real library is already imported.
    Uses ``sys.modules[name] = mod`` (overwrite) instead of
    ``setdefault`` so it wins even if a partial/broken import
    already cached a module with ``ChatType = None``.
    """
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return  # Real library is installed — nothing to mock

    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    # One shared PTB-faithful enum namespace per constant, attached to BOTH
    # access paths: ``sys.modules["telegram.constants"]`` is registered as
    # the root mock below, so ``from telegram.constants import ParseMode``
    # resolves ``mod.ParseMode`` — while config/docs-style access reads
    # ``telegram.constants.ParseMode``. Binding the same object to both
    # keeps every consumer comparing against identical members.
    _parse_mode = _fake_str_enum(
        "ParseMode", MARKDOWN="Markdown", MARKDOWN_V2="MarkdownV2", HTML="HTML"
    )
    _chat_type = _fake_str_enum(
        "ChatType",
        PRIVATE="private",
        GROUP="group",
        SUPERGROUP="supergroup",
        CHANNEL="channel",
    )
    mod.ParseMode = _parse_mode
    mod.constants.ParseMode = _parse_mode
    mod.ChatType = _chat_type
    mod.constants.ChatType = _chat_type

    # Mirror PTB's exception hierarchy: BadRequest is a semantic API error,
    # but inherits from NetworkError in python-telegram-bot 22.x.
    mod.error.TelegramError = type("TelegramError", (Exception,), {})
    mod.error.NetworkError = type("NetworkError", (mod.error.TelegramError,), {})
    mod.error.TimedOut = type("TimedOut", (mod.error.NetworkError,), {})
    mod.error.BadRequest = type("BadRequest", (mod.error.NetworkError,), {})
    mod.error.Forbidden = type("Forbidden", (mod.error.TelegramError,), {})
    mod.error.InvalidToken = type("InvalidToken", (mod.error.TelegramError,), {})

    class RetryAfter(mod.error.TelegramError):
        def __init__(self, retry_after=1):
            self.retry_after = retry_after

    mod.error.RetryAfter = RetryAfter
    mod.error.Conflict = type("Conflict", (mod.error.TelegramError,), {})

    # Update.ALL_TYPES used in start_polling()
    mod.Update.ALL_TYPES = []

    for name in (
        "telegram",
        "telegram.ext",
        "telegram.constants",
        "telegram.request",
    ):
        sys.modules[name] = mod
    sys.modules["telegram.error"] = mod.error


def _ensure_discord_mock() -> None:
    """Install a comprehensive discord mock in sys.modules.

    Idempotent — skips when the real library is already imported.
    Uses ``sys.modules[name] = mod`` (overwrite) instead of
    ``setdefault`` so it wins even if a partial/broken import already
    cached the module.

    This mock is comprehensive — it includes **all** attributes needed by
    every gateway discord test file.  Individual test files should call
    this function (it short-circuits when already present) rather than
    maintaining their own mock setup.
    """
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return  # Real library is installed — nothing to mock

    from types import SimpleNamespace

    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.Interaction = object
    discord_mod.Message = type("Message", (), {})

    # Embed: accept the kwargs production code / tests use
    # (title, description, color). MagicMock auto-attributes work too,
    # but some tests construct and inspect .title/.description directly.
    class _FakeEmbed:
        def __init__(self, *, title=None, description=None, color=None, **_):
            self.title = title
            self.description = description
            self.color = color
            self.fields = []
            self.footer = None
        def add_field(self, *, name=None, value=None, inline=False, **_):
            self.fields.append({"name": name, "value": value, "inline": inline})
            return self
        def set_footer(self, *, text=None, icon_url=None, **_):
            self.footer = {"text": text, "icon_url": icon_url}
            return self
    discord_mod.Embed = _FakeEmbed

    # ui.View / ui.Select / ui.Button: real classes (not MagicMock) so
    # tests that subclass ModelPickerView / iterate .children / clear
    # items work.
    class _FakeView:
        def __init__(self, timeout=None):
            self.timeout = timeout
            self.children = []
        def add_item(self, item):
            self.children.append(item)
        def clear_items(self):
            self.children.clear()

    class _FakeSelect:
        def __init__(self, *, placeholder=None, options=None, custom_id=None, **_):
            self.placeholder = placeholder
            self.options = options or []
            self.custom_id = custom_id
            self.callback = None
            self.disabled = False

    class _FakeButton:
        def __init__(self, *, label=None, style=None, custom_id=None, emoji=None,
                     url=None, disabled=False, row=None, sku_id=None, **_):
            self.label = label
            self.style = style
            self.custom_id = custom_id
            self.emoji = emoji
            self.url = url
            self.disabled = disabled
            self.row = row
            self.sku_id = sku_id
            self.callback = None

    class _FakeSelectOption:
        def __init__(self, *, label=None, value=None, description=None, **_):
            self.label = label
            self.value = value
            self.description = description
    discord_mod.SelectOption = _FakeSelectOption

    # AudioSource: real class so VoiceMixer(discord.AudioSource) can subclass
    # it cleanly in tests.  MagicMock auto-attributes would make is_opus()
    # return a Mock instead of False, breaking 9 TestVoiceMixerCore tests.
    class _FakeAudioSource:
        def is_opus(self):
            return False
        def read(self):
            return b"\x00" * 3840  # one silent stereo s16 frame
        def cleanup(self):
            pass
    discord_mod.AudioSource = _FakeAudioSource

    discord_mod.ui = SimpleNamespace(
        View=_FakeView,
        Select=_FakeSelect,
        Button=_FakeButton,
        button=lambda *a, **k: (lambda fn: fn),
    )
    discord_mod.ButtonStyle = SimpleNamespace(
        success=1, primary=2, secondary=2, danger=3,
        green=1, grey=2, blurple=2, red=3,
    )
    discord_mod.Color = SimpleNamespace(
        orange=lambda: 1, green=lambda: 2, blue=lambda: 3,
        red=lambda: 4, purple=lambda: 5, greyple=lambda: 6,
        gold=lambda: 7,
    )

    # app_commands — needed by _register_slash_commands auto-registration
    class _FakeGroup:
        def __init__(self, *, name, description, parent=None):
            self.name = name
            self.description = description
            self.parent = parent
            self._children: dict = {}
            if parent is not None:
                parent.add_command(self)

        def add_command(self, cmd):
            self._children[cmd.name] = cmd

    class _FakeCommand:
        def __init__(self, *, name, description, callback, parent=None):
            self.name = name
            self.description = description
            self.callback = callback
            self.parent = parent

    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
        Group=_FakeGroup,
        Command=_FakeCommand,
    )

    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod

    for name in ("discord", "discord.ext", "discord.ext.commands"):
        sys.modules[name] = discord_mod
    sys.modules["discord.ext"] = ext_mod
    sys.modules["discord.ext.commands"] = commands_mod


# Run at collection time — before any test file's module-level imports.
_ensure_telegram_mock()
_ensure_discord_mock()


# ---------------------------------------------------------------------------
# Plugin-adapter anti-pattern guard
# ---------------------------------------------------------------------------

_GATEWAY_DIR = Path(__file__).resolve().parent
_GUARD_HINT = (
    "Plugin adapter tests must use "
    "``from tests.gateway._plugin_adapter_loader import load_plugin_adapter`` "
    "and call ``load_plugin_adapter('<plugin_name>')`` instead of inserting "
    "``plugins/platforms/<name>/`` on sys.path and doing a bare ``import "
    "adapter`` / ``from adapter import ...``. See the 'Plugin-adapter "
    "anti-pattern guard' docstring in tests/gateway/conftest.py."
)


def _scan_for_plugin_adapter_antipattern(source: str) -> list[str]:
    """Return a list of offending-line descriptions, or [] if clean.

    Flags two things:
    1. ``sys.path.insert(..., <something mentioning 'plugins/platforms'>)``
    2. ``import adapter`` or ``from adapter import ...`` at module level.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []  # Let pytest surface the real syntax error.

    offenses: list[str] = []

    for node in ast.walk(tree):
        # sys.path.insert(0, ".../plugins/platforms/...")
        if isinstance(node, ast.Call):
            func = node.func
            target_name: str | None = None
            if isinstance(func, ast.Attribute):
                # sys.path.insert / sys.path.append
                if (
                    isinstance(func.value, ast.Attribute)
                    and isinstance(func.value.value, ast.Name)
                    and func.value.value.id == "sys"
                    and func.value.attr == "path"
                    and func.attr in {"insert", "append", "extend"}
                ):
                    target_name = f"sys.path.{func.attr}"

            if target_name is not None:
                call_src = ast.unparse(node)
                # Match both the string-literal form
                # ``.../plugins/platforms/...`` and the Path-operator form
                # ``Path(...) / 'plugins' / 'platforms' / ...`` that
                # plugin tests typically use.
                _src_no_ws = "".join(call_src.split())
                if (
                    "plugins/platforms" in call_src
                    or "plugins\\platforms" in call_src
                    or "'plugins'/'platforms'" in _src_no_ws
                    or '"plugins"/"platforms"' in _src_no_ws
                ):
                    offenses.append(
                        f"line {node.lineno}: {target_name}(...) points into "
                        f"plugins/platforms/"
                    )

    # Bare `import adapter` / `from adapter import ...` anywhere (module level
    # OR inside functions — both are symptoms of the same pattern).
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "adapter":
                    offenses.append(
                        f"line {node.lineno}: ``import adapter`` "
                        f"(bare — resolves to whichever plugin's adapter.py "
                        f"is first on sys.path)"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module == "adapter" and node.level == 0:
                offenses.append(
                    f"line {node.lineno}: ``from adapter import ...`` "
                    f"(bare — resolves to whichever plugin's adapter.py "
                    f"is first on sys.path)"
                )

    return offenses


def _fingerprint_gateway_tests() -> str:
    """Return a short fingerprint that changes when any gateway test file changes.

    Uses (mtime, size) pairs instead of content hashing — fast to compute
    (stat-only, no reads) and sufficient for cache invalidation across
    per-file subprocess runs.
    """
    import hashlib

    h = hashlib.sha256()
    for path in sorted(_GATEWAY_DIR.rglob("test_*.py")):
        try:
            st = path.stat()
            h.update(f"{path.name}:{st.st_mtime_ns}:{st.st_size}".encode())
        except OSError:
            h.update(f"{path.name}:missing".encode())
    return h.hexdigest()[:16]


def _run_adapter_antipattern_scan() -> list[str]:
    """Scan gateway test files for the plugin-adapter anti-pattern.

    Returns a list of violation strings (empty if clean).
    """
    violations: list[str] = []
    for path in _GATEWAY_DIR.rglob("test_*.py"):
        if path.name in {"_plugin_adapter_loader.py", "conftest.py"}:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        # Fast string pre-filter: skip files that can't possibly violate.
        # A violating file MUST contain both (a) an adapter/plugins/platforms
        # reference AND (b) either sys.path manipulation or a bare adapter import.
        if "adapter" not in source and "plugins/platforms" not in source:
            continue
        if not (
            "sys.path" in source
            or "import adapter" in source
            or "from adapter import" in source
        ):
            continue
        offenses = _scan_for_plugin_adapter_antipattern(source)
        if offenses:
            violations.append(
                f"  {path.relative_to(_GATEWAY_DIR.parent.parent)}:\n    "
                + "\n    ".join(offenses)
            )
    return violations


def pytest_configure(config):
    """Reject plugin-adapter tests that use the sys.path anti-pattern.

    Runs once per pytest session on the controller, BEFORE any xdist
    worker is spawned. If any file under ``tests/gateway/`` matches the
    anti-pattern, we fail the whole session with a clear message —
    before a polluted ``sys.path`` can cascade across workers.

    **Performance**: in the per-file subprocess isolation model (no xdist),
    every subprocess is a "controller" — so the naive scan would run 257
    times, each costing ~1s of AST walking.  We avoid this with two
    strategies:

    1. **Tight string pre-filter**: a file can only violate if it contains
       *both* an adapter/plugins/platforms reference *and* a sys.path
       manipulation or bare ``import adapter``.  This drops ~95% of files
       from needing AST parsing.
    2. **File-locked cache**: the scan result is cached in
       ``.pytest-cache/gw-adapter-guard-<fingerprint>`` keyed on a
       fingerprint of the gateway test file mtimes/sizes.  Concurrent
       subprocesses acquire a lock; only the first performs the scan;
       the rest wait and read the cached result.
    Also registers the environment-gap marks (see the ``_ENV_GAPS`` block at
    the bottom of this file). That happens before the xdist-worker early return
    below, because every process needs the marks declared.
    """
    register_marks(config)

    # Only run on the xdist controller (or in non-xdist runs). Skip on
    # worker subprocesses so we don't scan the filesystem N times.
    if hasattr(config, "workerinput"):
        return

    fp = _fingerprint_gateway_tests()
    cache_dir = Path.cwd() / ".pytest-cache"
    cache_file = cache_dir / f"gw-adapter-guard-{fp}"
    lock_file = cache_dir / f".gw-adapter-guard-{fp}.lock"

    cache_dir.mkdir(parents=True, exist_ok=True)

    # Evict stale cache entries from previous fingerprints (best-effort).
    try:
        for old in cache_dir.glob("gw-adapter-guard-*"):
            if old.name != f"gw-adapter-guard-{fp}":
                old.unlink(missing_ok=True)
        for old in cache_dir.glob(".gw-adapter-guard-*.lock"):
            if old.name != f".gw-adapter-guard-{fp}.lock":
                old.unlink(missing_ok=True)
    except OSError:
        pass  # Non-critical; old files are harmless.

    # Use filelock to ensure only one process scans at a time.
    # Concurrent subprocesses all hit pytest_configure simultaneously;
    # without a lock they'd all find no cache and all run the scan.
    try:
        from filelock import FileLock
        lock = FileLock(str(lock_file), timeout=120)
    except ImportError:
        # Fallback: no locking (still correct, just slower under contention).

        class _NoLock:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
        lock = _NoLock()

    with lock:
        if cache_file.exists():
            cached = cache_file.read_text(encoding="utf-8")
            if cached == "clean":
                return
            raise pytest.UsageError(cached)

        # Slow path: this process is the first to acquire the lock.
        violations = _run_adapter_antipattern_scan()

        if violations:
            msg = (
                "Plugin-adapter-import anti-pattern detected in gateway tests:\n"
                + "\n".join(violations)
                + "\n\n"
                + _GUARD_HINT
            )
            cache_file.write_text(msg, encoding="utf-8")
            raise pytest.UsageError(msg)
        else:
            cache_file.write_text("clean", encoding="utf-8")


# ── Environment-gap registry (audited 2026-08-10) ──────────────────────
#
# This directory registered ~62 node ids as pre-existing Windows/host gaps.
# The audit reproduced every one and found FIFTY-FOUR were not gaps:
#
#   * 39 rows (test_feishu.py + test_setup_feishu.py), filed as "Windows home
#     resolution is environment-only", were a REAL DEFECT:
#     plugins/platforms/feishu/adapter.py.__init__ resolved get_hermes_home()
#     into an attribute and then read the dedup cache off disk. That froze the
#     path against the construction-time home AND made construction depend on
#     a resolvable home. Fixed in the code (live-resolving property + hydrate
#     on first use), not fenced; 38 rows went green on the fix alone. The 39th
#     was an over-broad patch.dict(os.environ, {}, clear=True) that only ever
#     looked hermetic because POSIX falls back to pwd.getpwuid().
#   * 2 rows (test_api_server.py, test_readiness.py) were ALREADY STALE: they
#     described a HERMES_HOME volume at 93.8% capacity, which it no longer is.
#     Nothing failed to say so — the stale detector only prints, and it printed
#     into a summary nobody reads on a permanently red suite. That is the
#     motivating case for the probe-backed form below.
#   * 3 rows monkeypatched HOME and expected `~` to follow; ntpath.expanduser
#     prefers USERPROFILE, so home stayed at the developer's real profile —
#     under which pytest's tmp_path lives on Windows, so the wrong prefix got
#     collapsed. They now use tests._home_env.point_home_at.
#   * the remainder asserted a POSIX SPELLING: a file:// envelope whose
#     percent-quoting is invisible only because a POSIX path quotes to itself
#     (they now pin the encode/decode round trip, which is the actual
#     contract), a "~/" separator, a root-relative "/tmp/wd" that
#     os.path.abspath drive-qualifies, and two fixtures writing U+2713/U+2192
#     through Path.write_text with no encoding= while production reads those
#     files with encoding="utf-8".
#
# NOTE on test_platform_base.py: the row correctly observed that
# _path_under_denied_prefix resolves home with os.path.expanduser("~") while
# its sibling _media_delivery_denied_paths() prefers $HOME, and correctly
# declined to "fix" it because reconciling them WIDENS a denial carve-out. The
# divergence is real and remains an owner decision; the test now patches what
# the platform actually reads, so it pins the carve-out on both platforms
# without anyone touching the policy.
_ENV_GAPS: EnvGapRegistry = {}


def _no_af_unix() -> bool:
    """True where there is no AF_UNIX socket, hence no systemd notify socket."""
    return not hasattr(socket, "AF_UNIX")


def _no_defusedxml() -> bool:
    """True where the optional defusedxml dependency is absent.

    DEPENDENCY-bound, not platform-bound: plugins/platforms/wecom/
    callback_adapter.py falls back to ET=None without it and _build_event
    raises before parsing. Installing defusedxml retires this row outright.
    """
    return importlib.util.find_spec("defusedxml") is None


def _posix_only_update_spawn() -> bool:
    """True where _handle_update_command does NOT take the bash/setsid branch.

    gateway/slash_commands.py:5498 branches on sys.platform: the win32 arm
    spawns the interpreter directly, so the `bash -c` / setsid fallback shape
    these tests pin is unreachable. The platform IS the mechanism here — the
    assertion targets a branch the host never selects.
    """
    return sys.platform == "win32"


def _no_posix_fhs_absolute_dir() -> bool:
    """True where "/etc" cannot name an existing filesystem-absolute directory.

    On Windows a leading slash is drive-relative, so "/etc" resolves to
    <current drive>:\etc, and there is no drive-independent absolute directory
    guaranteed to exist. The test states its own assumption ("/etc exists on
    any POSIX box"), and with no absolute candidate on disk the cwd-relative
    decoy legitimately wins.
    """
    return not os.path.isdir(os.path.abspath(os.sep + "etc"))


_ENV_GAP_SKIPS: EnvGapSkipRegistry = {
    'test_systemd_notify.py': [
        (
            _no_af_unix,
            'gateway/systemd_notify.py returns False up front when '
            'socket.AF_UNIX is absent; there is no systemd notification socket '
            'to write to on this platform',
            {
                'test_notify_uses_nonblocking_datagram_send',
                'test_watchdog_sends_ready_heartbeat_and_stopping',
            },
        ),
    ],
    'test_wecom_callback.py': [
        (
            _no_defusedxml,
            "optional dependency 'defusedxml' is not installed, so "
            'plugins/platforms/wecom/callback_adapter.py falls back to ET=None '
            'and _build_event raises AttributeError before parsing anything. '
            'DEPENDENCY-bound: installing defusedxml retires this row',
            {
                'TestWecomCallbackEventConstruction::test_build_event_extracts_text_message',
                'TestWecomCallbackPollLoop::test_poll_loop_dispatches_handle_message',
            },
        ),
    ],
    'test_update_command.py': [
        (
            _posix_only_update_spawn,
            'pins the POSIX `bash -c` / setsid spawn shape; '
            'gateway/slash_commands.py:5498 selects a direct-interpreter spawn '
            'on win32, so this fallback branch is never reached',
            {
                'TestHandleUpdateCommand::test_fallback_when_no_setsid',
            },
        ),
    ],
    'test_update_streaming.py': [
        (
            _posix_only_update_spawn,
            'asserts PYTHONUNBUFFERED inside the `bash -c` command STRING; the '
            'win32 branch of _handle_update_command spawns an argv list and '
            'passes the env through Popen(env=...) instead',
            {
                'TestUpdateCommandGatewayFlag::test_spawns_with_gateway_flag',
            },
        ),
    ],
    'test_complete_path_at_filter.py': [
        (
            _no_posix_fhs_absolute_dir,
            'the test states its own assumption — "/etc exists on any POSIX '
            'box" — and on Windows a leading slash is drive-relative, so no '
            'filesystem-absolute candidate exists and the cwd-relative decoy '
            'correctly wins',
            {
                'test_leading_slash_prefers_a_real_absolute_path',
            },
        ),
    ],
}

_STALE = StaleEntryTracker(_ENV_GAPS, "tests/gateway/conftest.py")


def pytest_collection_modifyitems(items):  # noqa: D401 — pytest hook
    """Attach the environment-gap mark to every registered node id."""
    apply_marks(items, _ENV_GAPS)
    apply_skips(items, _ENV_GAP_SKIPS)


def pytest_runtest_logreport(report):  # noqa: D401 — pytest hook
    """Record registered environment-gap node ids that actually passed."""
    _STALE.record(report)


def pytest_terminal_summary(terminalreporter):  # noqa: D401 — pytest hook
    """Surface registry rows that no longer describe a real failure."""
    _STALE.report(terminalreporter)

