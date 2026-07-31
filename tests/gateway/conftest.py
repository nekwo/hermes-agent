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
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests._env_gap_fence import (
    HOST_DEPENDENCY_GAP as _HOST,
    WINDOWS_ENV_GAP as _WINDOWS,
    EnvGapRegistry,
    StaleEntryTracker,
    apply_marks,
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


# ── Pre-existing environment-gap fence (2026-07-31 upstream sync) ───────────
#
# Every row below was reproduced individually on this Windows 10 workstation,
# traced to a concrete host/platform cause, and proven PRE-EXISTING by running
# the same node on the pre-merge fork tip `1adf0404f` — where each of these
# files failed with a superset of these node ids (upstream's prune waves
# removed the rest). None is caused by the `upstream/main` merge.
#
# These tests are NOT skipped and NOT xfailed: they still run, still execute
# their real assertions, and still fail loudly on a plain
# `pytest tests/gateway`. See `tests/_env_gap_fence` for the full contract and
# the rules for adding a row.
#
#     python -m pytest tests/gateway -m "not windows_env_gap and not host_dependency_gap"
#
# Rows for files that arrived WITH the merge (no fork-side history at all) are
# marked as such: there is no fork regression intent to preserve in them, and
# "pre-existing" cannot be asked of a file that did not exist before.
#
# Three genuine defects were found in this directory during the same pass and
# were FIXED in the code, not fenced:
#   * gateway/slash_commands.py — the fork-owned `_handle_queue_status_command`
#     called the raw sync session store from loop-side async code, which
#     upstream's new AST guard (test_async_session_store.py) correctly flagged;
#   * agent/prompt_builder.py — the merge dropped the fork's guarded
#     `skill_matches_environment` import (see tests/agent/conftest.py);
#   * tests/gateway/test_completion_delivery.py — upstream's redaction test was
#     written against upstream's agent-turn delivery lane; the fork defaults to
#     a direct send, so the test was RETARGETED (redaction intent preserved and
#     now proven on the lane that actually ships), not fenced.
_ENV_GAPS: EnvGapRegistry = {
    'test_73771_media_resend_dedup.py': [
        (
            _WINDOWS,
            'arrived with the merge (upstream-only). Asserts the raw filesystem '
            'path appears in the delivered media reference; on Windows the '
            'delivery form is a percent-encoded file:// URL '
            '(file://C%3A%5CUsers%5C...), because the drive colon and '
            'backslashes are not URL-safe',
            {
                'test_streamed_explicit_media_resend_is_delivered',
            },
        ),
    ],
    'test_api_server.py': [
        (
            _HOST,
            'gateway/readiness.py flags disk usage >= 90% as degraded and this '
            "host's HERMES_HOME volume is 93.8% full, so /health/detailed "
            'correctly reports "degraded"; the test asserts an unconditional '
            '"ok" without stubbing _probe_disk',
            {
                'TestHealthDetailedEndpoint::test_health_detailed_returns_ok',
            },
        ),
    ],
    'test_complete_path_at_filter.py': [
        (
            _WINDOWS,
            'arrived with the merge (the node does not exist at 1adf0404f). The '
            'test states its own assumption — "/etc exists on any POSIX box" — '
            'so the absolute reading of "@/etc/" cannot resolve on Windows and '
            'the cwd-relative decoy legitimately wins',
            {
                'test_leading_slash_prefers_a_real_absolute_path',
            },
        ),
    ],
    'test_media_spaced_paths_and_history_dedupe.py': [
        (
            _WINDOWS,
            'arrived with the merge (upstream-only). The home-relative collapse '
            'rebuilds the path as "C:\\Users\\beast" + "/" + the remainder, so '
            'the collected form mixes separators and never equals the '
            'all-backslash path the test builds from tmp_path',
            {
                'TestHistoryMediaDedupe::test_quoted_spaced_home_path_is_collected_in_delivery_form',
            },
        ),
    ],
    'test_post_stream_media_delivery.py': [
        (
            _WINDOWS,
            'arrived with the merge (upstream-only). Same cause as '
            'test_73771_media_resend_dedup.py: the Windows path is delivered '
            'percent-encoded inside a file:// URL, not as the raw path',
            {
                'test_explicit_media_tag_still_delivers_post_stream',
            },
        ),
    ],
    'test_readiness.py': [
        (
            _HOST,
            'arrived with the merge (upstream-only). _probe_disk() degrades at '
            ">= 90% used and this host's HERMES_HOME volume is 93.8% full, so "
            'the aggregate readiness is correctly "degraded"; the test asserts '
            '"ok" without stubbing the disk probe',
            {
                'test_collect_runtime_readiness_reports_healthy_local_runtime',
            },
        ),
    ],
    'test_status_command.py': [
        (
            _WINDOWS,
            'the profile footer collapses a home-prefixed path to "~/...". On '
            'Windows pytest\'s tmp_path lives UNDER the user profile '
            '(%LOCALAPPDATA%\\Temp), so the collapse rewrites the very path the '
            'test asserts literally; on Linux /tmp is outside $HOME and nothing '
            'is collapsed',
            {
                'test_profile_command_reports_source_stamped_profile',
            },
        ),
    ],
    'test_systemd_notify.py': [
        (
            _WINDOWS,
            'arrived with the merge (upstream-only). gateway/systemd_notify.py '
            'returns False up front when socket.AF_UNIX is absent, which it is '
            'on Windows — there is no systemd notification socket to write to',
            {
                'test_notify_uses_nonblocking_datagram_send',
                'test_watchdog_sends_ready_heartbeat_and_stopping',
            },
        ),
    ],
    # ── Windows home resolution is environment-only ──────────────────────
    #
    # These tests construct their subject under
    # `@patch.dict(os.environ, {}, clear=True)`. The adapter's __init__ reaches
    # `get_hermes_home()` -> `_get_platform_default_hermes_home()` ->, with
    # LOCALAPPDATA cleared, `Path.home()`. On Windows `ntpath.expanduser` reads
    # only USERPROFILE / HOMEDRIVE+HOMEPATH, so with the env scrubbed it returns
    # "~" unchanged and pathlib raises `RuntimeError: Could not determine home
    # directory.` POSIX falls back to `pwd.getpwuid(os.getuid()).pw_dir`, which
    # needs no environment at all. Verified: all 38 feishu failures carry that
    # exact message.
    'test_feishu.py': [
        (
            _WINDOWS,
            'the adapter is constructed under patch.dict(os.environ, {}, '
            'clear=True); Windows home resolution is environment-only, so '
            'Path.home() raises "Could not determine home directory." where '
            'POSIX falls back to pwd.getpwuid()',
            {
                'TestAdapterBehavior::test_bot_origin_reactions_are_dropped_to_avoid_feedback_loops',
                'TestAdapterBehavior::test_build_event_handler_registers_reaction_and_card_processors',
                'TestAdapterBehavior::test_extract_audio_message_downloads_and_caches',
                'TestAdapterBehavior::test_extract_post_message_downloads_embedded_resources',
                'TestAdapterBehavior::test_extract_text_file_injects_content',
                'TestAdapterBehavior::test_extract_text_message_starting_with_slash_becomes_command',
                'TestAdapterBehavior::test_group_message_matches_bot_name_when_only_name_available',
                'TestAdapterBehavior::test_media_batch_merges_rapid_photo_messages',
                'TestAdapterBehavior::test_message_event_submits_to_adapter_loop',
                'TestAdapterBehavior::test_process_inbound_message_uses_event_sender_identity_only',
                'TestAdapterBehavior::test_reaction_on_peer_bot_message_is_not_routed',
                'TestAdapterBehavior::test_send_document_reply_uses_thread_flag',
                'TestAdapterBehavior::test_send_splits_fenced_code_blocks_into_separate_post_rows',
                'TestAdapterBehavior::test_send_uses_post_for_every_chunk_of_multi_chunk_markdown',
                'TestAdapterBehavior::test_text_batch_flushes_when_message_count_limit_is_hit',
                'TestAdapterBehavior::test_url_verification_requires_configured_verification_token',
                'TestAdapterBehavior::test_user_reaction_with_managed_emoji_is_still_routed',
                'TestAdapterBehavior::test_webhook_request_uses_same_message_dispatch_path',
                'TestBotNameResolution::test_fetches_and_caches_bot_name',
                'TestBotNameResolution::test_returns_cached_bot_name_without_api_call',
                'TestDedupTTL::test_duplicate_within_ttl_is_rejected',
                'TestFeishuAdapterMessaging::test_connect_websocket_sets_channel_ua_tag',
                'TestFeishuAdapterMessaging::test_edit_message_falls_back_to_text_when_post_update_is_rejected',
                'TestGroupMentionAtAll::test_at_all_still_requires_policy_gate',
                'TestHydrateBotIdentity::test_hydration_populates_open_id_from_bot_info',
                'TestHydrateBotIdentity::test_hydration_refreshes_env_values_when_bot_info_available',
                'TestPendingInboundQueue::test_drainer_replays_queued_events_when_loop_becomes_ready',
                'TestPendingInboundQueue::test_event_queued_when_loop_not_ready',
                'TestProcessingReactions::test_delete_failure_on_failure_outcome_skips_cross_mark',
                'TestProcessingReactions::test_failure_removes_typing_then_adds_cross_mark',
                'TestProcessingReactions::test_start_adds_typing_and_caches_reaction_id',
                'TestProcessingReactions::test_success_removes_typing_and_adds_nothing',
                'TestSenderNameResolution::test_fetches_and_caches_name_from_api',
                'TestSenderNameResolution::test_returns_cached_name_within_ttl',
                'TestWebhookSecurity::test_rate_limit_resets_after_window_expires',
                'TestWebhookSecurity::test_signature_valid_passes',
                'TestWebhookSecurity::test_webhook_connect_requires_inbound_auth_secret',
                'TestWebhookSecurity::test_webhook_loads_auth_secrets_from_platform_extra',
            },
        ),
    ],
    'test_setup_feishu.py': [
        (
            _WINDOWS,
            'same as test_feishu.py: FeishuAdapter(PlatformConfig()) is built '
            'under a cleared os.environ, and Windows home resolution is '
            'environment-only, so Path.home() raises "Could not determine home '
            'directory."',
            {
                'TestSetupFeishuAdapterIntegration::test_qr_env_produces_valid_adapter_settings',
            },
        ),
    ],
    # ── os.path.expanduser ignores a monkeypatched HOME on Windows ───────
    'test_platform_base.py': [
        (
            _WINDOWS,
            'the test patches HOME and relies on the "a denied prefix that IS '
            "the running user's home is exempt\" carve-out in "
            '_path_under_denied_prefix, which resolves home with '
            'os.path.expanduser("~") — and ntpath.expanduser prefers '
            'USERPROFILE, so the carve-out never fires. NOTE: the sibling '
            '_media_delivery_denied_paths() in the same file already prefers '
            '$HOME (a fork addition the merge correctly preserved); making '
            '_path_under_denied_prefix match it would retire this row and close '
            'a real alt-home divergence, but it WIDENS a denial carve-out, so '
            'it is an owner decision rather than a sync-time edit',
            {
                'TestMediaDeliveryDefaultMode::test_root_home_deliverable_is_accepted',
            },
        ),
    ],
    'test_runtime_footer.py': [
        (
            _WINDOWS,
            '_home_relative_cwd() resolves home with os.path.expanduser("~"), '
            'which ignores the monkeypatched HOME on Windows. Home becomes the '
            "real profile, pytest's tmp_path lives UNDER it "
            '(%LOCALAPPDATA%\\Temp), so the wrong prefix is collapsed and the '
            'result carries backslashes where the assertion hardcodes "/"',
            {
                'test_home_relative_cwd_collapses_home',
                'test_format_footer_all_fields',
            },
        ),
        (
            _WINDOWS,
            'no HOME patch involved: _home_relative_cwd("/tmp/wd") calls '
            'os.path.abspath, which drive-qualifies a root-relative path '
            'against the current drive on Windows ("X:\\tmp\\wd"), so the '
            'literal "/tmp/wd" cannot appear in the output',
            {
                'test_format_footer_skips_missing_context_length',
            },
        ),
    ],
    # ── missing host dependencies / undeclared packages ──────────────────
    'test_memory_monitor.py': [
        (
            _HOST,
            "declared dependency 'psutil' (psutil==7.2.2 in pyproject) is not "
            'installed on this interpreter. gateway/memory_monitor.py reads RSS '
            'via resource.getrusage (POSIX-only, absent on Windows) then falls '
            'back to psutil; with both unavailable _get_rss_mb() returns None '
            'and start_memory_monitoring() logs "monitoring unavailable" and '
            'returns False',
            {
                'test_start_logs_baseline_and_returns_true',
                'test_double_start_is_noop',
                'test_stop_logs_shutdown_snapshot',
            },
        ),
    ],
    'test_status.py': [
        (
            _HOST,
            "declared dependency 'psutil' is not installed on this interpreter, "
            'so gateway/status.py::_get_process_start_time falls off /proc (its '
            'only non-Linux source) into a failing psutil import and returns '
            'None',
            {
                'TestGetProcessStartTime::test_live_process_is_stable_int',
            },
        ),
    ],
    'test_matrix.py': [
        (
            _HOST,
            "Python-Markdown ('markdown') is not installed. "
            'MatrixAdapter._markdown_to_html imports it inside a try/except and '
            'silently drops to a regex converter with no table support, so the '
            'output carries no <table>. NOTE: `markdown` is declared in NO '
            'requirements file despite the docstring calling it "a core '
            'dependency" — a real packaging gap, not just a host gap',
            {
                'TestMatrixMarkdownToHtml::test_matrix_markdown_preserves_table_structure',
            },
        ),
    ],
    # ── test-side portability defects (fixable at the source) ────────────
    #
    # RETIRED, not fenced — both rows that lived here were fixed at the source
    # and now pass on this host:
    #   * test_config_env_bridge_authority.py — _run_gateway_import's child-env
    #     allowlist was POSIX-shaped (PATH/PYTHONPATH/VIRTUAL_ENV/HOME). It now
    #     also forwards the Windows equivalents (APPDATA/USERPROFILE/
    #     HOMEDRIVE+HOMEPATH for user-site and home resolution, SystemRoot/
    #     SystemDrive for Winsock init).
    #   * test_session_state_cleanup.py — the architectural guard read
    #     gateway/run.py without encoding=, so the cp1252 locale default killed
    #     it on Windows; it now reads encoding="utf-8" and the guard actually
    #     runs here.
    'test_update_command.py': [
        (
            _WINDOWS,
            'asserts the POSIX spawn shape (`bash -c`, setsid/nohup); the '
            'Windows branch of _handle_update_command spawns the interpreter '
            'directly, so argv[0] is the python.exe path',
            {
                'TestHandleUpdateCommand::test_fallback_when_no_setsid',
            },
        ),
        (
            _WINDOWS,
            'the fixture writes U+2713 / U+2192 through Path.write_text() with '
            "no encoding=, so Python's Windows default (cp1252) raises "
            'UnicodeEncodeError before the assertion under test is reached',
            {
                'TestSendUpdateNotification::test_sends_notification_with_output',
                'TestSendUpdateNotification::test_cleans_up_on_error',
            },
        ),
    ],
    'test_update_streaming.py': [
        (
            _WINDOWS,
            'asserts PYTHONUNBUFFERED inside the `bash -c` command STRING; on '
            'Windows the spawn is an argv list whose last element is the plain '
            '`--gateway` flag and the env goes through Popen(env=...) instead',
            {
                'TestUpdateCommandGatewayFlag::test_spawns_with_gateway_flag',
            },
        ),
    ],
    'test_wecom_callback.py': [
        (
            _HOST,
            "optional dependency 'defusedxml' is not installed, so "
            'plugins/platforms/wecom/callback_adapter.py falls back to ET=None '
            'and _build_event raises AttributeError before parsing anything',
            {
                'TestWecomCallbackEventConstruction::test_build_event_extracts_text_message',
                'TestWecomCallbackPollLoop::test_poll_loop_dispatches_handle_message',
            },
        ),
    ],
    'test_whatsapp_bridge_pidfile.py': [
        (
            _HOST,
            "dependency 'psutil' is not installed, so gateway.status."
            '_get_process_start_time() returns None off /proc (its only '
            'non-Linux source) and the pidfile is written without the '
            'start-time line the test indexes',
            {
                'TestWriteAndRoundTrip::test_pidfile_records_pid_and_start_time',
            },
        ),
    ],
}

_STALE = StaleEntryTracker(_ENV_GAPS, "tests/gateway/conftest.py")


def pytest_collection_modifyitems(items):  # noqa: D401 — pytest hook
    """Attach the environment-gap mark to every registered node id."""
    apply_marks(items, _ENV_GAPS)


def pytest_runtest_logreport(report):  # noqa: D401 — pytest hook
    """Record registered environment-gap node ids that actually passed."""
    _STALE.record(report)


def pytest_terminal_summary(terminalreporter):  # noqa: D401 — pytest hook
    """Surface registry rows that no longer describe a real failure."""
    _STALE.report(terminalreporter)

