"""Machine root anchor (``agent_runtime/root_anchor.py``).

The property under test end-to-end: after ``harness serve`` publishes the
anchor, a process with NO hermes environment at all resolves the operator's
real runtime root through ``resolve_runtime``'s config rung — instead of the
platform-default shadow that produced the 2026-08-12 ``ok: true, count: 0``
chat-history incident. So the first test here IS the rung verification: it
asserts the published key actually wins the ladder for an ambient env.

Isolation note: every test passes an explicit ``env`` mapping, and the POSIX
``Path.home()`` arm of ``_platform_default_hermes_home`` is fenced by
pointing ``HOME`` at a tmp dir — no test may touch the machine's real
platform-default config (the exact file production writes).
"""

from __future__ import annotations

import io
import json

import pytest

from agent_runtime.resolution import PROBE_ISOLATION_ENV, resolve_runtime
from agent_runtime.root_anchor import (
    RootAnchorOutcome,
    RootAnchorReport,
    publish_store_root_anchor,
)


@pytest.fixture
def anchor_env(tmp_path, monkeypatch):
    """(env, real_root, config_path): an explicit serve-like environment whose
    platform default home is isolated under tmp_path on every OS."""

    # POSIX `Path.home()` fence; harmless on Windows (LOCALAPPDATA is passed
    # in the mapping and wins there).
    monkeypatch.setenv("HOME", str(tmp_path / "posix-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "posix-home"))
    appdata = tmp_path / "appdata"
    real_root = tmp_path / "real" / "agent-runtime"
    (real_root / "sessions").mkdir(parents=True)  # a STORE_MARKER_DIRS member
    env = {
        "LOCALAPPDATA": str(appdata),
        "HERMES_AGENT_RUNTIME_ROOT": str(real_root),
    }
    from agent_runtime.resolution import _platform_default_hermes_home

    config_path = _platform_default_hermes_home(env) / "config.yaml"
    return env, real_root, config_path


def _ambient_env(env: dict) -> dict:
    """The environment of the incident: no hermes variables at all."""

    return {key: value for key, value in env.items() if not key.startswith("HERMES")}


def test_published_anchor_wins_the_config_rung_for_an_ambient_process(anchor_env):
    env, real_root, config_path = anchor_env

    report = publish_store_root_anchor(env)
    assert report.outcome is RootAnchorOutcome.PUBLISHED
    assert report.published
    assert report.config_path == str(config_path)

    # THE rung verification: an ambient process (no HERMES_* at all) now
    # resolves the real root via the config layer, not the shadow default.
    resolution = resolve_runtime(_ambient_env(env))
    assert resolution.layer == "config"
    assert str(resolution.store_root) == str(real_root)


def test_second_publish_is_a_no_op(anchor_env):
    env, _, config_path = anchor_env
    assert publish_store_root_anchor(env).outcome is RootAnchorOutcome.PUBLISHED
    before = config_path.read_bytes()

    report = publish_store_root_anchor(env)
    assert report.outcome is RootAnchorOutcome.ALREADY_RECORDED
    assert config_path.read_bytes() == before


def test_operator_value_is_never_overwritten(anchor_env):
    env, real_root, config_path = anchor_env
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_bytes(b"agent_runtime:\n  store_root: 'D:/operator/chose/this'\n")
    before = config_path.read_bytes()

    report = publish_store_root_anchor(env)
    assert report.outcome is RootAnchorOutcome.OPERATOR_VALUE_KEPT
    assert report.detail == "D:/operator/chose/this"
    assert config_path.read_bytes() == before
    # The operator's rung still wins for an ambient process.
    ambient = resolve_runtime(_ambient_env(env))
    assert ambient.layer == "config"
    assert str(ambient.store_root) != str(real_root)


def test_append_preserves_an_existing_config_without_the_block(anchor_env):
    import yaml

    env, real_root, config_path = anchor_env
    config_path.parent.mkdir(parents=True, exist_ok=True)
    original = "# operator note\nredaction_mode: strict\nread_model:\n  enabled: false\n"
    config_path.write_text(original, encoding="utf-8", newline="")

    report = publish_store_root_anchor(env)
    assert report.outcome is RootAnchorOutcome.PUBLISHED
    text = config_path.read_text(encoding="utf-8")
    assert text.startswith(original)  # operator bytes untouched, block appended
    parsed = yaml.safe_load(text)
    assert parsed["redaction_mode"] == "strict"
    assert parsed["read_model"] == {"enabled": False}
    assert parsed["agent_runtime"]["store_root"] == str(real_root)
    assert resolve_runtime(_ambient_env(env)).layer == "config"


def test_insertion_into_an_existing_agent_runtime_block(anchor_env):
    import yaml

    env, real_root, config_path = anchor_env
    config_path.parent.mkdir(parents=True, exist_ok=True)
    original = (
        "agent_runtime:\n"
        "    read_model:\n"
        "        enabled: true\n"
        "other_top: 1\n"
    )
    config_path.write_text(original, encoding="utf-8", newline="")

    report = publish_store_root_anchor(env)
    assert report.outcome is RootAnchorOutcome.PUBLISHED
    parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    # The merge is exactly the old document plus the anchor key — the block's
    # existing children and the file's other top-level keys survive.
    assert parsed == {
        "agent_runtime": {
            "read_model": {"enabled": True},
            "store_root": str(real_root),
        },
        "other_top": 1,
    }
    resolution = resolve_runtime(_ambient_env(env))
    assert resolution.layer == "config"
    assert str(resolution.store_root) == str(real_root)


def test_crlf_config_is_extended_without_flipping_line_endings(anchor_env):
    import yaml

    env, real_root, config_path = anchor_env
    config_path.parent.mkdir(parents=True, exist_ok=True)
    original = b"agent_runtime:\r\n  read_model:\r\n    enabled: false\r\n"
    config_path.write_bytes(original)

    report = publish_store_root_anchor(env)
    assert report.outcome is RootAnchorOutcome.PUBLISHED
    raw = config_path.read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n"), "an existing CRLF file must stay CRLF"
    parsed = yaml.safe_load(raw.decode("utf-8"))
    assert parsed["agent_runtime"]["store_root"] == str(real_root)
    assert parsed["agent_runtime"]["read_model"] == {"enabled": False}


def test_probe_isolation_refuses_to_anchor(anchor_env):
    env, _, config_path = anchor_env
    report = publish_store_root_anchor({**env, PROBE_ISOLATION_ENV: "1"})
    assert report.outcome is RootAnchorOutcome.PROBE_ISOLATED
    assert not config_path.exists()


def test_probe_prefixed_root_refuses_to_anchor(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "posix-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "posix-home"))
    probe_root = tmp_path / "agent-runtime-probe-xyz"
    (probe_root / "sessions").mkdir(parents=True)
    env = {
        "LOCALAPPDATA": str(tmp_path / "appdata"),
        "HERMES_AGENT_RUNTIME_ROOT": str(probe_root),
    }
    report = publish_store_root_anchor(env)
    assert report.outcome is RootAnchorOutcome.PROBE_ISOLATED


def test_a_root_the_harness_never_wrote_is_not_anchored(anchor_env, tmp_path):
    env, _, config_path = anchor_env
    bare = tmp_path / "bare-root"
    bare.mkdir()
    report = publish_store_root_anchor({**env, "HERMES_AGENT_RUNTIME_ROOT": str(bare)})
    assert report.outcome is RootAnchorOutcome.ROOT_NOT_STORELIKE
    assert not config_path.exists()


def test_the_ambient_default_root_is_not_anchored(anchor_env):
    env, _, config_path = anchor_env
    default_root = config_path.parent / "agent-runtime"
    (default_root / "sessions").mkdir(parents=True)
    report = publish_store_root_anchor(
        {**env, "HERMES_AGENT_RUNTIME_ROOT": str(default_root)}
    )
    assert report.outcome is RootAnchorOutcome.AMBIENT_ROOT
    assert not config_path.exists()


def test_unparseable_config_is_left_alone(anchor_env):
    env, _, config_path = anchor_env
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_bytes(b"{{{ not yaml ::\n")
    before = config_path.read_bytes()
    report = publish_store_root_anchor(env)
    assert report.outcome is RootAnchorOutcome.UNWRITABLE
    assert report.detail == "config_unparseable"
    assert config_path.read_bytes() == before


def test_scalar_agent_runtime_key_declines_the_merge(anchor_env):
    env, _, config_path = anchor_env
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_bytes(b"agent_runtime: legacy-string\n")
    before = config_path.read_bytes()
    report = publish_store_root_anchor(env)
    assert report.outcome is RootAnchorOutcome.DECLINED_UNSAFE_MERGE
    assert config_path.read_bytes() == before


def test_publish_never_raises(anchor_env, monkeypatch):
    env, _, _ = anchor_env
    import agent_runtime.root_anchor as module

    def boom(*args, **kwargs):
        raise RuntimeError("io exploded")

    monkeypatch.setattr(module, "_publish", boom)
    report = publish_store_root_anchor(env)
    assert report.outcome is RootAnchorOutcome.UNWRITABLE
    assert report.detail == "RuntimeError"


# ── the head-home declaration ────────────────────────────────────────────────
#
# Same property, one level up: after ``harness serve`` declares its head, a
# process with NO hermes environment resolves the operator's real chat head
# through the CONFIG_DECLARED rung instead of degrading to the ambient guess.
# Until this shipped, the only authority that named the head was a string
# literal in the Launcher's Dart settings — so only launcher-spawned processes
# ever learned it.


def _declared_head(config_path):
    """The declared ``agent_runtime.head_home``, PARSED — never a substring
    search: pytest's tmp dir is named after the test, so a test with
    ``head_home`` in its own name matches its own path inside the config."""

    import yaml

    if not config_path.exists():
        return None
    parsed = yaml.safe_load(config_path.read_bytes().decode("utf-8"))
    if not isinstance(parsed, dict):
        return None
    return (parsed.get("agent_runtime") or {}).get("head_home")


def _explicit_scope(head):
    """A scope naming *head* explicitly — the serve-with-HERMES_HEAD_HOME posture."""

    from agent_runtime.chat_session_scope import ChatHeadSource, ChatSessionScope

    return ChatSessionScope(head, ChatHeadSource.ENV_HEAD_HOME)


def _ambient_scope(head):
    from agent_runtime.chat_session_scope import ChatHeadSource, ChatSessionScope

    return ChatSessionScope(head, ChatHeadSource.AMBIENT_HOME)


@pytest.fixture
def head_env(anchor_env, tmp_path):
    """(env, head_home, config_path) — an existing operator head to declare."""

    env, _, config_path = anchor_env
    head_home = tmp_path / "real" / "profiles" / "base"
    head_home.mkdir(parents=True)
    return env, head_home, config_path


def test_the_declared_head_wins_the_config_rung_for_an_ambient_process(
    head_env, monkeypatch
):
    """THE rung verification, and the reason this slice exists."""

    import yaml

    from agent_runtime.chat_session_scope import (
        ChatHeadSource,
        declared_chat_head_home,
        resolve_process_chat_scope,
    )

    env, head_home, config_path = head_env
    report = publish_store_root_anchor(env, chat_scope=_explicit_scope(head_home))

    assert report.head is not None
    assert report.head.outcome is RootAnchorOutcome.PUBLISHED
    assert report.head.declared
    assert report.head.head_home == str(head_home)
    parsed = yaml.safe_load(config_path.read_bytes().decode("utf-8"))
    assert parsed["agent_runtime"]["head_home"] == str(head_home)

    # An ambient process — no head named, no pointer, and (deliberately) no
    # pinned runtime root — now READS the declaration instead of guessing.
    monkeypatch.delenv("HERMES_AGENT_RUNTIME_ROOT", raising=False)
    monkeypatch.delenv("HERMES_HEAD_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(config_path.parent))
    assert declared_chat_head_home() == head_home
    scope = resolve_process_chat_scope()
    assert scope.source is ChatHeadSource.CONFIG_DECLARED
    assert scope.head_home == head_home
    assert scope.authoritative and not scope.explicitly_named


def test_a_serve_with_no_explicit_head_declares_nothing(head_env):
    """A guess must never be laundered into a machine-wide declaration."""

    env, head_home, config_path = head_env
    report = publish_store_root_anchor(env, chat_scope=_ambient_scope(head_home))

    assert report.head.outcome is RootAnchorOutcome.NO_EXPLICIT_HEAD
    # The rung it actually resolved is named, so the skip is legible.
    assert report.head.detail == "ambient_home"
    assert _declared_head(config_path) is None


def test_an_operator_head_declaration_is_never_overwritten(head_env):
    env, head_home, config_path = head_env
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_bytes(b"agent_runtime:\n  head_home: 'D:/operator/head'\n")
    before = config_path.read_bytes()

    report = publish_store_root_anchor(env, chat_scope=_explicit_scope(head_home))

    assert report.head.outcome is RootAnchorOutcome.OPERATOR_VALUE_KEPT
    assert report.head.detail == "D:/operator/head"
    # store_root is still absent from that file, so the ROOT lane wrote — the
    # head lane leaving the operator's value alone must not have blocked it.
    assert report.outcome is RootAnchorOutcome.PUBLISHED
    assert b"D:/operator/head" in config_path.read_bytes()
    assert config_path.read_bytes() != before  # only by the store_root line


def test_a_second_boot_re_declaring_the_same_head_writes_nothing(head_env):
    env, head_home, config_path = head_env
    scope = _explicit_scope(head_home)
    assert (
        publish_store_root_anchor(env, chat_scope=scope).head.outcome
        is RootAnchorOutcome.PUBLISHED
    )
    before = config_path.read_bytes()

    report = publish_store_root_anchor(env, chat_scope=scope)

    assert report.head.outcome is RootAnchorOutcome.ALREADY_RECORDED
    assert config_path.read_bytes() == before


def test_a_head_home_that_does_not_exist_is_not_declared(head_env, tmp_path):
    env, _, config_path = head_env
    gone = tmp_path / "real" / "profiles" / "gone"

    report = publish_store_root_anchor(env, chat_scope=_explicit_scope(gone))

    assert report.head.outcome is RootAnchorOutcome.HEAD_HOME_MISSING
    assert _declared_head(config_path) is None


def test_the_platform_default_head_is_not_declared(head_env):
    """Recording what an ambient process resolves anyway says nothing."""

    env, _, config_path = head_env
    default_home = config_path.parent
    default_home.mkdir(parents=True, exist_ok=True)

    report = publish_store_root_anchor(env, chat_scope=_explicit_scope(default_home))

    assert report.head.outcome is RootAnchorOutcome.AMBIENT_HEAD


def test_probe_isolation_refuses_to_declare_a_head(head_env):
    env, head_home, config_path = head_env

    report = publish_store_root_anchor(
        {**env, PROBE_ISOLATION_ENV: "1"}, chat_scope=_explicit_scope(head_home)
    )

    assert report.head.outcome is RootAnchorOutcome.PROBE_ISOLATED
    assert not config_path.exists()


def test_both_declarations_share_one_agent_runtime_block(head_env):
    """The merge is exactly the old document plus the two anchor keys."""

    import yaml

    env, head_home, config_path = head_env
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("redaction_mode: strict\n", encoding="utf-8", newline="")

    report = publish_store_root_anchor(env, chat_scope=_explicit_scope(head_home))

    assert report.outcome is RootAnchorOutcome.PUBLISHED
    assert report.head.outcome is RootAnchorOutcome.PUBLISHED
    parsed = yaml.safe_load(config_path.read_bytes().decode("utf-8"))
    assert parsed == {
        "redaction_mode": "strict",
        "agent_runtime": {
            "store_root": str(resolve_runtime(env).store_root),
            "head_home": str(head_home),
        },
    }


def test_crlf_config_survives_both_declarations(head_env):
    env, head_home, config_path = head_env
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_bytes(b"agent_runtime:\r\n  read_model:\r\n    enabled: false\r\n")

    publish_store_root_anchor(env, chat_scope=_explicit_scope(head_home))

    raw = config_path.read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n"), "an existing CRLF file must stay CRLF"
    assert b"head_home" in raw and b"store_root" in raw


def test_the_head_lane_never_raises(head_env, monkeypatch):
    env, head_home, _ = head_env
    import agent_runtime.root_anchor as module

    def boom(*args, **kwargs):
        raise RuntimeError("io exploded")

    monkeypatch.setattr(module, "_merge_key", boom)
    report = publish_store_root_anchor(env, chat_scope=_explicit_scope(head_home))

    assert report.head.outcome is RootAnchorOutcome.UNWRITABLE
    assert report.head.detail == "RuntimeError"


# ── serve wiring ─────────────────────────────────────────────────────────────


def _serve_frames(root_anchor) -> list[dict]:
    from hermes_cli.harness_parts.serve import serve_loop

    out = io.StringIO()
    requests = iter([json.dumps({"op": "shutdown"}) + "\n"])
    assert serve_loop(requests, out, dispatch=lambda argv: 0, root_anchor=root_anchor) == 0
    return [json.loads(line) for line in out.getvalue().splitlines() if line]


def test_serve_emits_the_anchor_outcome_as_its_own_frame():
    report = RootAnchorReport(
        outcome=RootAnchorOutcome.PUBLISHED,
        store_root="X:/somewhere/agent-runtime",
        config_path="X:/appdata/hermes/config.yaml",
        detail="created",
    )
    frames = _serve_frames(lambda: report)
    anchor = [frame for frame in frames if frame.get("event") == "root_anchor"]
    assert anchor == [
        {
            "event": "root_anchor",
            "outcome": "published",
            "store_root": "X:/somewhere/agent-runtime",
            "config_path": "X:/appdata/hermes/config.yaml",
            "detail": "created",
        }
    ]


def test_the_serve_frame_carries_the_head_declaration_additively():
    """One frame line must say what was declared, what was already there, and
    what was declined — the operator's whole view of the boot's root decisions.

    The block is ADDITIVE: the assertion above pins that a report without a
    head declaration emits the pre-2026-08-13 frame byte-for-byte, so a
    consumer that predates this block is unaffected.
    """

    from agent_runtime.root_anchor import HeadAnchorReport

    report = RootAnchorReport(
        outcome=RootAnchorOutcome.ALREADY_RECORDED,
        store_root="X:/somewhere/agent-runtime",
        config_path="X:/appdata/hermes/config.yaml",
        head=HeadAnchorReport(
            outcome=RootAnchorOutcome.PUBLISHED,
            head_home="X:/somewhere/profiles/base",
            config_path="X:/appdata/hermes/config.yaml",
            detail="extended",
        ),
    )
    frames = _serve_frames(lambda: report)
    anchor = [frame for frame in frames if frame.get("event") == "root_anchor"]
    assert anchor == [
        {
            "event": "root_anchor",
            "outcome": "already_recorded",
            "store_root": "X:/somewhere/agent-runtime",
            "config_path": "X:/appdata/hermes/config.yaml",
            "detail": "",
            "head": {
                "outcome": "published",
                "head_home": "X:/somewhere/profiles/base",
                "config_path": "X:/appdata/hermes/config.yaml",
                "detail": "extended",
            },
        }
    ]


def test_serve_boot_survives_an_anchor_that_raises():
    def explode():
        raise OSError("disk on fire")

    frames = _serve_frames(explode)
    anchor = [frame for frame in frames if frame.get("event") == "root_anchor"]
    assert anchor == [
        {"event": "root_anchor", "outcome": "unwritable", "detail": "OSError"}
    ]
    assert any(frame.get("event") == "ready" for frame in frames)


def test_serve_loop_without_injection_publishes_nothing():
    """The snapshot_prewarm contract: unit-test invocations of the loop must
    never reach the machine-global platform-default config."""

    frames = _serve_frames(None)
    assert not [frame for frame in frames if frame.get("event") == "root_anchor"]


def test_cmd_serve_injects_the_real_publisher():
    """The injection contract cuts both ways: the loop's tests run with it OFF,
    so the real entry point turning it ON is otherwise uncovered — pin the
    wiring at the source so removing the kwarg cannot ship silently."""

    import ast
    from pathlib import Path

    import hermes_cli.harness_parts.serve as serve_module

    tree = ast.parse(Path(serve_module.__file__).read_text(encoding="utf-8"))
    wired = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_cmd_serve":
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == "serve_loop"
                ):
                    wired = any(
                        keyword.arg == "root_anchor"
                        and isinstance(keyword.value, ast.Name)
                        and keyword.value.id == "publish_store_root_anchor"
                        for keyword in sub.keywords
                    )
    assert wired, (
        "_cmd_serve must pass root_anchor=publish_store_root_anchor to "
        "serve_loop — without it the machine root anchor is never published "
        "and the ambient-root incident class returns"
    )
