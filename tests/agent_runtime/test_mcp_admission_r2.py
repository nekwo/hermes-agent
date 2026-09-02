"""Selective MCP admission (R2) — scoped teardown, real subtraction, §D3 honesty.

R1 admitted servers and never took the scope back: in a warm serve process the
admitted tools stayed in the process registry until it recycled, and — because
``register_mcp_servers`` short-circuits on already-connected servers — a
``read_only`` admission that FOLLOWED a ``profile_default`` one re-used the
already-registered full surface, so the registration-time filter could not
subtract. R2 closes both by tearing down the registry scope per run while keeping
the transport warm, which forces the registrar to re-register warm servers off
their live session.

This file pins the three things that made R2 worth doing:

1. **Teardown really removes the scope** — tools, toolset check and the
   bare-server-name alias — and a following admission re-registers cleanly
   through it, so the lifecycle is a cycle and not a one-way door.
2. **``profile_default`` → ``read_only`` subtracts AT REGISTRATION TIME.** The
   headline defect R1 recorded as a known consequence. It is exercised through
   the REAL ``tools/mcp_tool._register_server_tools``, not a mock, because the
   whole claim is about what that upstream function does with a warm session.
3. **The agent is told when it does not get what it declared** (design §D3), on
   the same volatile envelope tail the wall-budget line rides, and never on a
   clean admission.

Plus the two guards that keep the above from rotting: a drift test on the
upstream warm-registration seam, and the launcher-allowlist parity fixture.

No test here spawns a real MCP server or connects a transport.
"""

from __future__ import annotations

import hashlib
import re
import types
from pathlib import Path

import pytest

from agent_runtime.mcp_admission import (
    LANE_MISSION_CHAT,
    MCP_ADMISSION_DISABLED,
    MCP_ADMISSION_LANE_BUSY,
    MCP_ADMISSION_TEARDOWN_FAILED,
    MCP_ADMISSION_TIMEOUT,
    MCP_NOT_REGISTERED_ON_LANE,
    MCP_SDK_UNAVAILABLE,
    MCP_SERVER_NOT_CONFIGURED,
    READ_ONLY_ALLOWLIST_PROFILE,
    READ_ONLY_EXCLUDED_TOOLS,
    READ_ONLY_INCLUDED_TOOLS,
    McpAdmission,
    McpAdmissionDenial,
    McpAdmissionOutcome,
    render_mcp_admission_line,
    resolve_mcp_admission,
    scope_toolsets_to_admission,
    teardown_mcp_admission,
)
from tests.agent_runtime.persona_samples import sample_personas
from agent_runtime.runtime_config import McpAdmissionConfig

#: sha256 of the vendored snapshot of the LAUNCHER's own per-profile allowlist.
#: Refresh instructions live in ``tests/agent_runtime/fixtures/README.md`` —
#: changing this constant without re-checking the parity assertions below is
#: exactly the silent drift the fixture exists to prevent.
_LAUNCHER_ALLOWLIST_SHA256 = (
    "4aad31d0467eaa807b2cf6295c25ec4645923d8495b88120dc4ecc63389591aa"
)
#: Tool count of the launcher's Stage C QA surface at the snapshot above
#: (`kStageCQaMcpTools`). The reviewer row must partition it exactly — that is
#: the hermes-side half of the launcher's own Stage 22 drift test.
_LAUNCHER_QA_TOOL_COUNT = 26
_LAUNCHER_ALLOWLIST_FIXTURE = (
    Path(__file__).parent / "fixtures" / "launcher_qa_profile_allowlists.yaml"
)


# ── fixtures / helpers ──────────────────────────────────────────────────────


def _persona(persona_id: str):
    return {persona.id: persona for persona in sample_personas()}[persona_id]


def _cfg(**kwargs) -> types.SimpleNamespace:
    # Every keyword lands on the real McpAdmissionConfig — a retired field fails
    # loudly here rather than being silently dropped.
    return types.SimpleNamespace(mcp_admission=McpAdmissionConfig(**kwargs))


def _bind_profile(monkeypatch, profile_home):
    from agent_runtime import profile_context, profile_readiness
    from agent_runtime.profile_context import PersonaProfileBinding

    binding = lambda persona: PersonaProfileBinding(  # noqa: E731 - one-line stub
        persona_id=persona.id,
        hermes_profile="launcher-qa",
        profile_home=profile_home,
        readiness="ready",
        summary="profile exists",
    )
    monkeypatch.setattr(profile_readiness, "resolve_persona_profile", binding)
    monkeypatch.setattr(profile_context, "resolve_persona_profile", binding)


_LAUNCHER_QA_CONFIG = """
mcp_servers:
  launcher_qa:
    command: stagec_qa_mcp_server.exe
    args: ["--stdio"]
    connect_timeout: 60
    timeout: 260
"""


@pytest.fixture
def qa_profile(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(_LAUNCHER_QA_CONFIG, encoding="utf-8")
    _bind_profile(monkeypatch, home)
    return home


@pytest.fixture
def clean_registry():
    """Leave the process registry exactly as we found it.

    ``tools.registry.registry`` is process-global and shared with every other
    test in the session, so a leaked ``mcp-*`` toolset here would silently change
    what an unrelated isolation test observes.
    """

    from tools.registry import registry

    yield registry
    for toolset in list(registry.get_registered_toolset_names() or []):
        if not str(toolset).startswith("mcp-"):
            continue
        for name in list(registry.get_tool_names_for_toolset(toolset) or []):
            registry.deregister(name)


class _FakeMcpTool:
    """The shape ``_register_server_tools`` reads off a listed MCP tool."""

    def __init__(self, name: str):
        self.name = name
        self.description = f"fake {name}"
        self.inputSchema = {"type": "object", "properties": {}}


class _FakeConnectedServer:
    """A CONNECTED server task: live session, tools already listed.

    Deliberately carries no ``initialize_result`` and a session object with none
    of the resources/prompts methods, so ``_select_utility_schemas`` selects
    nothing and the test observes exactly the tool filter under test.
    """

    def __init__(self, name: str, tool_names):
        self.name = name
        self.session = object()
        self.tool_timeout = 5.0
        self._tools = [_FakeMcpTool(tool) for tool in tool_names]


_LAUNCHER_QA_FULL_SURFACE = tuple(
    sorted(set(READ_ONLY_INCLUDED_TOOLS["launcher_qa"]) | set(READ_ONLY_EXCLUDED_TOOLS["launcher_qa"]))
)


@pytest.fixture
def warm_launcher_qa(monkeypatch, clean_registry):
    """A warm ``launcher_qa`` in ``tools/mcp_tool._servers`` — no transport, no spawn.

    This is the state R2 creates on purpose: the connection survives a run, its
    registry scope does not.
    """

    import tools.mcp_tool as mcp_tool

    server = _FakeConnectedServer("launcher_qa", _LAUNCHER_QA_FULL_SURFACE)
    monkeypatch.setitem(mcp_tool._servers, "launcher_qa", server)
    return server


def _registered_launcher_qa_tools(registry) -> set[str]:
    return set(registry.get_tool_names_for_toolset("mcp-launcher_qa") or [])


def _raw_tool_names(prefixed: set[str]) -> set[str]:
    """``mcp__launcher_qa__mcp_launcher_qa_x`` → ``mcp_launcher_qa_x``."""

    return {name.rsplit("__", 1)[-1] for name in prefixed}


# ── teardown: the scope belongs to the run ──────────────────────────────────


def test_teardown_removes_the_tools_the_toolset_and_the_alias(clean_registry, warm_launcher_qa):
    from tools.mcp_tool import _register_server_tools

    _register_server_tools("launcher_qa", warm_launcher_qa, {})
    assert _registered_launcher_qa_tools(clean_registry)
    assert clean_registry.get_toolset_alias_target("launcher_qa") == "mcp-launcher_qa"

    outcome = teardown_mcp_admission(["launcher_qa"])

    assert outcome.ok
    assert outcome.servers == ("launcher_qa",)
    assert len(outcome.removed_tool_names) == len(_LAUNCHER_QA_FULL_SURFACE)
    assert _registered_launcher_qa_tools(clean_registry) == set()
    assert "mcp-launcher_qa" not in (clean_registry.get_registered_toolset_names() or [])
    # The bare-server-name alias goes with the last tool of the toolset. Leaving
    # it would let a later run resolve `launcher_qa` in enabled_toolsets to a
    # toolset that no longer exists.
    assert clean_registry.get_toolset_alias_target("launcher_qa") is None


def test_teardown_keeps_the_transport_warm(clean_registry, warm_launcher_qa):
    """The registry scope is the RUN's; the connection is the PROCESS's.

    That is the whole performance argument for R2's shape: the next admitted turn
    re-registers off this live session instead of paying a fresh spawn +
    handshake.
    """

    import tools.mcp_tool as mcp_tool
    from tools.mcp_tool import _register_server_tools

    _register_server_tools("launcher_qa", warm_launcher_qa, {})
    teardown_mcp_admission(["launcher_qa"])

    assert mcp_tool._servers.get("launcher_qa") is warm_launcher_qa
    assert getattr(mcp_tool._servers["launcher_qa"], "session", None) is not None


def test_a_following_admission_re_registers_cleanly(clean_registry, warm_launcher_qa):
    """Teardown must be a cycle, not a one-way door.

    ``register_mcp_servers`` alone cannot do this — it short-circuits on a
    connected server — which is exactly why ``_default_registrar`` grew the warm
    path.
    """

    from agent_runtime.mcp_admission import _default_registrar

    _default_registrar({"launcher_qa": {"command": "noop"}})
    first = _registered_launcher_qa_tools(clean_registry)
    assert len(first) == len(_LAUNCHER_QA_FULL_SURFACE)

    teardown_mcp_admission(["launcher_qa"])
    assert _registered_launcher_qa_tools(clean_registry) == set()

    _default_registrar({"launcher_qa": {"command": "noop"}})

    assert _registered_launcher_qa_tools(clean_registry) == first
    assert clean_registry.get_toolset_alias_target("launcher_qa") == "mcp-launcher_qa"


def test_profile_default_then_read_only_subtracts_at_registration_time(
    qa_profile, clean_registry, warm_launcher_qa
):
    """THE R2 acceptance test — the exact sequence R1 could not serve.

    Two admissions of the SAME server in the SAME process, the second narrower.
    In R1 the second one short-circuited on the connected server and inherited
    the full surface, and only ``blocked_tool_names`` kept the mutators out of
    the model's list. Here the mutators are not REGISTERED at all.
    """

    from agent_runtime.mcp_admission import _default_registrar

    full = resolve_mcp_admission(
        _persona("qa"), permission_mode="profile_default", cfg=_cfg(enabled=True)
    )
    _default_registrar(full.server_configs)
    assert "mcp_launcher_qa_kill_launcher" in _raw_tool_names(
        _registered_launcher_qa_tools(clean_registry)
    )

    teardown_mcp_admission(full.server_names)

    reviewer = resolve_mcp_admission(
        _persona("qa"), permission_mode="read_only", cfg=_cfg(enabled=True)
    )
    _default_registrar(reviewer.server_configs)

    registered = _raw_tool_names(_registered_launcher_qa_tools(clean_registry))
    assert registered == set(READ_ONLY_INCLUDED_TOOLS["launcher_qa"])
    for mutator in READ_ONLY_EXCLUDED_TOOLS["launcher_qa"]:
        assert mutator not in registered


def test_teardown_of_nothing_is_a_no_op():
    outcome = teardown_mcp_admission([])

    assert outcome.ok
    assert outcome.servers == ()
    assert outcome.removed_tool_names == ()


def test_teardown_of_a_server_that_never_registered_is_clean(clean_registry):
    outcome = teardown_mcp_admission(["launcher_qa"])

    assert outcome.ok
    assert outcome.removed_tool_names == ()


def test_teardown_failure_is_typed_and_never_raises(clean_registry, warm_launcher_qa):
    """A finished turn must never be failed by its own cleanup.

    THE STUB IS SCOPED (EG-0.1). The eager drop below is genuinely required —
    ``clean_registry``'s own finalizer deregisters, and it runs BEFORE a
    function-scoped ``monkeypatch`` would unwind, so a wedged ``deregister``
    left in place past this block breaks teardown. What was wrong was HOW:
    ``monkeypatch.undo()`` unwinds the SHARED per-test instance, taking the
    package's ``isolate_agent_runtime_root`` pins with it and pointing the rest
    of the test at the operator's live runtime root. A scoped context drops the
    stub at exactly the same moment, on exceptions too, and touches nothing else.
    """

    from tools.mcp_tool import _register_server_tools

    _register_server_tools("launcher_qa", warm_launcher_qa, {})

    def _boom(_name):
        raise RuntimeError("registry is wedged")

    with pytest.MonkeyPatch.context() as wedged:
        wedged.setattr(clean_registry, "deregister", _boom)
        outcome = teardown_mcp_admission(["launcher_qa"])

    assert not outcome.ok
    rows = outcome.failure_rows()
    assert [row["code"] for row in rows] == [MCP_ADMISSION_TEARDOWN_FAILED]
    assert rows[0]["server"] == "launcher_qa"
    assert rows[0]["fix_hint"]


def test_a_teardown_that_cannot_take_the_admission_mutex_is_typed(
    monkeypatch, clean_registry, warm_launcher_qa
):
    """A caller-timed-out registration is still holding the mutex on its worker.

    Teardown says so in a typed row rather than pretending the scope was cleanly
    removed — the residue is bounded and the next run's toolset scope still
    refuses any MCP toolset it was not admitted.
    """

    import agent_runtime.mcp_admission as mcp_admission
    from tools.mcp_tool import _register_server_tools

    _register_server_tools("launcher_qa", warm_launcher_qa, {})
    assert mcp_admission._ADMISSION_LOCK.acquire(blocking=False)
    try:
        outcome = teardown_mcp_admission(["launcher_qa"], lock_timeout_seconds=0.01)
    finally:
        mcp_admission._ADMISSION_LOCK.release()

    assert [row["code"] for row in outcome.failure_rows()] == [MCP_ADMISSION_TEARDOWN_FAILED]
    # Removed ANYWAY: leaving the scope up is strictly worse than racing a late
    # registration, and the failure row makes the race visible.
    assert _registered_launcher_qa_tools(clean_registry) == set()


def test_unbounded_still_never_widens_after_a_teardown(clean_registry, warm_launcher_qa):
    """The security acceptance property survives the new lifecycle.

    Teardown makes the registry empty between runs, but the property must not
    START depending on that: it holds while a scope is LIVE too.
    """

    from tools.mcp_tool import _register_server_tools

    _register_server_tools("launcher_qa", warm_launcher_qa, {})
    live = ["file", "mcp-launcher_qa", "launcher_qa"]

    # Scope LIVE: both spellings the registry accepts — the canonical toolset and
    # the bare-server-name alias — are stripped from a run that was not admitted.
    assert scope_toolsets_to_admission(live, admitted_servers=()) == ["file"]
    assert scope_toolsets_to_admission(live, admitted_servers=["launcher_qa"]) == [
        "file",
        "mcp-launcher_qa",
        "launcher_qa",
    ]

    teardown_mcp_admission(["launcher_qa"])

    # Scope TORN DOWN: the canonical toolset is still stripped, and the bare
    # alias now resolves to nothing at all — it survives the scope as an inert
    # string, never as a capability. (`unbounded` resolves toolsets from the live
    # registry, which no longer contains either.)
    scoped = scope_toolsets_to_admission(live, admitted_servers=())
    assert "mcp-launcher_qa" not in scoped
    assert clean_registry.get_toolset_alias_target("launcher_qa") is None
    assert clean_registry.get_tool_names_for_toolset("mcp-launcher_qa") == []
    from agent_runtime.personas import all_registered_toolsets

    assert "mcp-launcher_qa" not in all_registered_toolsets()


# ── the upstream warm-registration seam (drift guard) ───────────────────────


def test_the_upstream_warm_registration_seam_exists():
    """Pin the ONE upstream private R2's warm path depends on.

    ``_default_registrar`` re-registers a torn-down warm server through
    ``tools.mcp_tool._register_server_tools`` because ``register_mcp_servers``
    cannot: it returns ``_existing_tool_names()`` for any already-connected
    server. If upstream renames or reshapes either, this test fails loudly here
    instead of the QA lane silently losing its tools.
    """

    import inspect

    import tools.mcp_tool as mcp_tool

    assert isinstance(mcp_tool._servers, dict)
    assert callable(mcp_tool._register_server_tools)
    params = list(inspect.signature(mcp_tool._register_server_tools).parameters)
    assert params[:3] == ["name", "server", "config"]
    # The short-circuit this whole design detail exists because of.
    source = inspect.getsource(mcp_tool.register_mcp_servers)
    assert "if not new_servers:" in source
    assert "return _existing_tool_names()" in source


def test_a_missing_warm_seam_fails_closed(monkeypatch, clean_registry, warm_launcher_qa):
    """Upstream drift must lose the capability, never widen it.

    Registering nothing surfaces as a typed ``mcp_not_registered_on_lane`` row —
    an honest "no tools this turn" — rather than falling back to whatever was
    registered before.
    """

    import tools.mcp_tool as mcp_tool
    from agent_runtime.mcp_admission import _default_registrar

    monkeypatch.delattr(mcp_tool, "_register_server_tools")

    assert _default_registrar({"launcher_qa": {"command": "noop"}}) == []
    assert _registered_launcher_qa_tools(clean_registry) == set()


def test_a_parked_server_is_not_live(monkeypatch, clean_registry):
    """A cached entry with no live session is NOT warm.

    Re-registering off a dead session would register handlers that cannot
    dispatch, so this predicate must keep excluding it — that half is unchanged.

    CORRECTED 2026-08-27, which is why the name moved. It used to say the parked
    server "belongs to ``register_mcp_servers``: it has dedicated wake handling
    for exactly that case". It has a WAKE and no REGISTRATION — it fires
    ``_signal_reconnect`` and returns on ``if not new_servers``, leaving the
    registry empty — so believing that sentence cost live turns their entire MCP
    surface, measured as a 3/0/3/0 alternation across four consecutive
    mission-chat turns to one session. ``_default_registrar`` now wakes parked
    servers itself; see ``test_mcp_admission_parked_wake.py``, the test this
    one's scope left room for.
    """

    import tools.mcp_tool as mcp_tool
    from agent_runtime.mcp_admission import _live_mcp_sessions

    parked = _FakeConnectedServer("launcher_qa", ("mcp_launcher_qa_get_auth_state",))
    parked.session = None
    monkeypatch.setitem(mcp_tool._servers, "launcher_qa", parked)

    assert "launcher_qa" not in _live_mcp_sessions()


# ── launcher-allowlist parity (design open question 6) ──────────────────────


def _parse_allowlist_profiles(text: str) -> dict[str, dict[str, list[str]]]:
    """Minimal reader for the launcher YAML's ``profiles.<name>.allowed/denied``.

    Hand-rolled rather than PyYAML on purpose: the fixture is hash-pinned, so its
    shape cannot drift underneath this, and the parity test must not gain a
    dependency that the rest of ``agent_runtime`` does not have.
    """

    profiles: dict[str, dict[str, list[str]]] = {}
    current: str | None = None
    bucket: str | None = None
    in_profiles = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("profiles:"):
            in_profiles = True
            continue
        if in_profiles and not line.startswith(" "):
            break
        if not in_profiles:
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if indent == 2 and stripped.endswith(":"):
            current = stripped[:-1]
            profiles[current] = {"allowed": [], "denied": []}
            bucket = None
            continue
        if current is None:
            continue
        match = re.fullmatch(r"(allowed|denied):\s*(\[\s*\])?", stripped)
        if indent == 4 and match:
            bucket = match.group(1)
            continue
        if indent >= 4 and stripped.startswith("- ") and bucket:
            profiles[current][bucket].append(stripped[2:].strip())
            continue
        if indent == 4:
            bucket = None
    return profiles


def _resolve_allow_set(row: dict[str, list[str]], surface) -> set[str]:
    """The YAML's own rule: denied wins, then allowed, then default-deny.

    Glob semantics per the file's header: trailing ``*`` only, no regex.
    """

    def _matches(patterns, name: str) -> bool:
        for pattern in patterns:
            if pattern.endswith("*"):
                if name.startswith(pattern[:-1]):
                    return True
            elif pattern == name:
                return True
        return False

    return {
        name
        for name in surface
        if not _matches(row["denied"], name) and _matches(row["allowed"], name)
    }


@pytest.fixture(scope="module")
def launcher_allowlist() -> dict[str, dict[str, list[str]]]:
    text = _LAUNCHER_ALLOWLIST_FIXTURE.read_text(encoding="utf-8")
    return _parse_allowlist_profiles(text)


def test_the_vendored_launcher_allowlist_matches_its_recorded_hash():
    """Pin the SNAPSHOT, so refreshing it is a reviewed act.

    The test must not depend on the launcher repo existing at runtime (hermes
    owns the policy — design open question 6), so the only thing that can keep
    the two honest is a recorded hash plus the parity assertions below.
    """

    digest = hashlib.sha256(_LAUNCHER_ALLOWLIST_FIXTURE.read_bytes()).hexdigest()

    assert digest == _LAUNCHER_ALLOWLIST_SHA256, (
        "The vendored launcher allowlist changed. Follow "
        "tests/agent_runtime/fixtures/README.md: re-check the parity assertions "
        "in this file and update READ_ONLY_INCLUDED_TOOLS / "
        "READ_ONLY_EXCLUDED_TOOLS deliberately, with a written security note."
    )


def test_the_fixture_parses_into_the_profiles_the_launcher_documents(launcher_allowlist):
    assert READ_ONLY_ALLOWLIST_PROFILE in launcher_allowlist
    assert {"launcher-qa", "launcher-qa-direct", "alice", "pm", "reviewer"} <= set(
        launcher_allowlist
    )
    # Full-capability rows are a single glob — which is why profile_default
    # compiles NO include filter rather than a 25-name list that a new launcher
    # tool would silently fall out of.
    assert launcher_allowlist["launcher-qa"]["allowed"] == ["mcp_launcher_qa_*"]
    assert launcher_allowlist["launcher-qa"]["denied"] == []


def test_read_only_include_equals_the_launcher_reviewer_allow_set(launcher_allowlist):
    row = launcher_allowlist[READ_ONLY_ALLOWLIST_PROFILE]
    surface = set(row["allowed"]) | set(row["denied"])

    assert _resolve_allow_set(row, surface) == set(READ_ONLY_INCLUDED_TOOLS["launcher_qa"])


def test_read_only_exclude_equals_the_launcher_reviewer_denied_set(launcher_allowlist):
    row = launcher_allowlist[READ_ONLY_ALLOWLIST_PROFILE]

    assert set(row["denied"]) == set(READ_ONLY_EXCLUDED_TOOLS["launcher_qa"])


def test_run_actions_the_capability_multiplexer_is_denied(launcher_allowlist):
    """The tool that proves a positive include is the right shape.

    ``run_actions`` executes an ordered list of OTHER verbs in one call, and this
    allowlist resolves per tool NAME — so admitting it to a restricted profile
    hands over every batchable verb that profile is otherwise denied. Under an
    include list it is denied by construction (it is simply not on the list);
    under R1's exclude list it would have been admitted silently the moment the
    launcher shipped it.
    """

    row = launcher_allowlist[READ_ONLY_ALLOWLIST_PROFILE]

    assert "mcp_launcher_qa_run_actions" in row["denied"]
    assert "mcp_launcher_qa_run_actions" not in READ_ONLY_INCLUDED_TOOLS["launcher_qa"]
    assert "mcp_launcher_qa_run_actions" in READ_ONLY_EXCLUDED_TOOLS["launcher_qa"]


def test_the_two_lists_partition_the_known_surface(launcher_allowlist):
    """No tool may be unclassified, and none may be in both halves.

    The launcher's Stage 22 drift test already fails ITS CI when a tool in
    ``kStageCQaMcpTools`` is unrepresented in a restricted profile; this is the
    hermes-side half of the same pin.
    """

    included = set(READ_ONLY_INCLUDED_TOOLS["launcher_qa"])
    excluded = set(READ_ONLY_EXCLUDED_TOOLS["launcher_qa"])
    row = launcher_allowlist[READ_ONLY_ALLOWLIST_PROFILE]

    assert included & excluded == set()
    assert included | excluded == set(row["allowed"]) | set(row["denied"])
    assert len(included | excluded) == _LAUNCHER_QA_TOOL_COUNT


def test_read_only_registers_the_reviewer_row_and_nothing_else(qa_profile):
    admission = resolve_mcp_admission(
        _persona("qa"), permission_mode="read_only", cfg=_cfg(enabled=True)
    )

    include = admission.server_configs["launcher_qa"]["tools"]["include"]
    assert set(include) == set(READ_ONLY_INCLUDED_TOOLS["launcher_qa"])
    assert "mcp_launcher_qa_kill_launcher" not in include
    # …and the same names stay in the model-list backstop for a resident actor
    # whose cached tool definitions predate this turn's registration.
    assert (
        "mcp__launcher_qa__mcp_launcher_qa_kill_launcher" in admission.blocked_tool_names
    )


def test_explain_shows_the_compiled_include_before_the_flag_is_flipped(qa_profile):
    """The operator's pre-flip inspection must show what will REGISTER.

    ``--explain-mcp`` previously showed only the block list, from which the
    include had to be inferred. An operator approving a read_only shape needs the
    positive list on the surface that never connects to anything.
    """

    explained = resolve_mcp_admission(
        _persona("qa"), permission_mode="read_only", cfg=_cfg(enabled=True)
    ).explain()

    assert explained["tool_include"]["launcher_qa"] == sorted(
        READ_ONLY_INCLUDED_TOOLS["launcher_qa"]
    )


def test_profile_default_compiles_no_include_filter(qa_profile):
    """The full-glob row means "everything this server advertises".

    Compiling it as a 25-name include would quietly DENY the next tool the
    launcher ships — the opposite of what the glob says.
    """

    admission = resolve_mcp_admission(
        _persona("qa"), permission_mode="profile_default", cfg=_cfg(enabled=True)
    )

    assert "tools" not in admission.server_configs["launcher_qa"]
    assert admission.blocked_tool_names == ()


# ── §D3: the agent's own turn context ───────────────────────────────────────


def _denied(code: str, server: str = "launcher_qa") -> McpAdmission:
    return McpAdmission(
        lane=LANE_MISSION_CHAT,
        role="qa",
        permission_mode="profile_default",
        enabled=True,
        requested=(server,),
        denied=(McpAdmissionDenial(server=server, code=code, summary="s", fix_hint="f"),),
    )


def test_a_clean_admission_names_what_it_admitted(qa_profile):
    """Operator ruling 2026-07-27 — replaces the old "a clean turn pays
    nothing" pin. The silence was a bet that an agent reads its tool list
    rather than its turn context; when it does not, silence and absence are
    the same thing, which is W3 from a third direction."""

    admission = resolve_mcp_admission(
        _persona("qa"), cfg=_cfg(enabled=True)
    )

    assert admission.server_names == ("launcher_qa",)
    assert admission.denied == ()

    line = render_mcp_admission_line(admission)

    assert line == (
        "- MCP tools: Admitted on this turn: launcher_qa; those servers' "
        "mcp__<server>__* tools ARE in your tool list, so call them directly."
    )
    assert "\n" not in line
    # The denial wording is a DENIAL wording: none of it leaks onto a turn that
    # lost nothing.
    assert "NOT available" not in line and "unavailable" not in line


def test_there_is_no_line_without_an_admission_object():
    assert render_mcp_admission_line(None) == ""


@pytest.mark.parametrize(
    "code",
    [
        MCP_SERVER_NOT_CONFIGURED,
        MCP_ADMISSION_DISABLED,
        MCP_ADMISSION_TIMEOUT,
        MCP_ADMISSION_LANE_BUSY,
        MCP_NOT_REGISTERED_ON_LANE,
        MCP_SDK_UNAVAILABLE,
    ],
)
def test_every_denial_code_produces_one_compact_line(code):
    line = render_mcp_admission_line(_denied(code))

    assert line.startswith("- MCP tools:")
    assert "\n" not in line
    assert "launcher_qa" in line
    assert code in line


def test_the_line_closes_the_route_and_forbids_improvising():
    """This is the whole point of §D3 — it retires W3.

    A QA agent that sees no ``mcp__launcher_qa__*`` tools and no explanation
    invents alternatives, which is why the launcher repo needs a grep gate
    against agents writing ``pwsh -File``. The line used to name
    ``qa.request_screenshot`` as the sanctioned alternative; that contract was
    removed with the mission lane, so naming it CAUSED the improvisation it was
    written to prevent. The honest replacement says the route is closed, says
    what to do instead (report and finish), and names the operator as the only
    one who can lift it.
    """

    line = render_mcp_admission_line(_denied(MCP_SERVER_NOT_CONFIGURED))

    assert "no harness-side fallback contract" in line
    assert "closed for the turn" in line
    # (c) what to do instead, and (d) who can unblock it.
    assert "finish the turn" in line
    assert "Only an operator can lift this" in line
    assert "PowerShell" in line
    assert "not a permission problem" in line


def test_the_line_never_points_at_the_deleted_screenshot_contract():
    """Regression fence for the defect this replaced.

    ``qa.request_screenshot`` / ``VisualProofRunner`` /
    ``stagec_mcp_visual_provider`` are gone from the repo. If any of them
    reappears in agent-facing text, the runtime is routing a denied agent to a
    lane that does not exist. Every denial code is checked, not just one.
    """

    for code in (
        MCP_SERVER_NOT_CONFIGURED,
        MCP_ADMISSION_DISABLED,
        MCP_ADMISSION_TIMEOUT,
        MCP_ADMISSION_LANE_BUSY,
        MCP_NOT_REGISTERED_ON_LANE,
        MCP_SDK_UNAVAILABLE,
    ):
        line = render_mcp_admission_line(_denied(code))
        assert "request_screenshot" not in line
        assert "VisualProofRunner" not in line


def _timed_out_outcome() -> McpAdmissionOutcome:
    row = McpAdmissionDenial(
        server="launcher_qa", code=MCP_ADMISSION_TIMEOUT, summary="s", fix_hint="f"
    )
    return McpAdmissionOutcome(attempted=True, denied=(row,), execution_denied=(row,))


def test_the_line_covers_execution_time_degradations():
    """Timeout / busy are only known AFTER the envelope is sealed.

    The renderer takes the execution outcome too, so the runner's in-band steer
    backstop reports exactly the same fact in the same words.
    """

    clean = McpAdmission(
        lane=LANE_MISSION_CHAT,
        role="qa",
        permission_mode="profile_default",
        enabled=True,
        requested=("launcher_qa",),
        server_names=("launcher_qa",),
    )

    # Before the outcome is known the line is the clean shape — the positive
    # half only, no denial wording (2026-07-27 ruling).
    assert "Admitted on this turn: launcher_qa" in render_mcp_admission_line(clean)
    assert MCP_ADMISSION_TIMEOUT not in render_mcp_admission_line(clean)
    assert MCP_ADMISSION_TIMEOUT in render_mcp_admission_line(
        clean, outcome=_timed_out_outcome()
    )
    # …and the execution half alone, which is what the runner's steer renders.
    assert MCP_ADMISSION_TIMEOUT in render_mcp_admission_line(
        None, outcome=_timed_out_outcome()
    )


def test_the_policy_half_is_never_repeated_by_the_execution_half():
    """``admit_mcp_servers`` carries the policy denials forward on ``denied``.

    Rendering those again for the in-band backstop would tell the agent the same
    thing twice in two voices, which is how a model learns to discount both. Only
    ``execution_denied`` rows reach the second lane.
    """

    policy = McpAdmissionDenial(
        server="other_server", code=MCP_SERVER_NOT_CONFIGURED, summary="s", fix_hint="f"
    )
    execution = McpAdmissionDenial(
        server="launcher_qa", code=MCP_ADMISSION_TIMEOUT, summary="s", fix_hint="f"
    )
    outcome = McpAdmissionOutcome(
        attempted=True, denied=(policy, execution), execution_denied=(execution,)
    )

    line = render_mcp_admission_line(None, outcome=outcome)

    assert "launcher_qa" in line
    assert "other_server" not in line
    assert outcome.degraded is True


def test_one_server_reports_once_even_with_both_a_policy_and_an_execution_row():
    admission = _denied(MCP_SERVER_NOT_CONFIGURED)

    line = render_mcp_admission_line(admission, outcome=_timed_out_outcome())

    assert line.count("launcher_qa (") == 1
    # The narrower policy reason wins over the generic execution one.
    assert MCP_SERVER_NOT_CONFIGURED in line
    assert MCP_ADMISSION_TIMEOUT not in line


def test_an_outcome_with_only_policy_rows_is_not_degraded():
    outcome = McpAdmissionOutcome(
        attempted=True,
        denied=(
            McpAdmissionDenial(
                server="other_server",
                code=MCP_SERVER_NOT_CONFIGURED,
                summary="s",
                fix_hint="f",
            ),
        ),
    )

    assert outcome.degraded is False
    assert render_mcp_admission_line(None, outcome=outcome) == ""


def test_the_line_rides_the_volatile_tail_and_never_the_hud_revision():
    """Volatile exactly like the wall-budget line.

    Folding it into the hashed HUD body would re-snapshot the whole block every
    time a capability blinked, and — worse — a cached ``unchanged`` delivery
    would show the agent a STALE capability claim.
    """

    from agent_runtime.runtime_hud import (
        render_runtime_context_envelope,
        situational_hud_revision,
    )

    hud = {"agent": "qa", "goal": "g"}
    revision = situational_hud_revision(hud)
    line = render_mcp_admission_line(_denied(MCP_ADMISSION_TIMEOUT))

    envelope = render_runtime_context_envelope(
        context_id="ctx",
        revision=revision,
        delivery="unchanged",
        situational_hud_content="body",
        volatile_content=line,
    )

    assert line in envelope
    assert situational_hud_revision(hud) == revision
    assert line not in revision


def test_the_mission_chat_line_is_empty_with_the_flag_off(qa_profile, monkeypatch):
    """Flag-off never resolves ADMISSION policy — no root-config load, no
    profile read.

    Since G5 (2026-07-26) flag-off is no longer silent: a persona that DECLARES
    a server the lane never registered gets the R0 line instead of ``""``. This
    persona declares none, so it still pays nothing and the envelope is
    byte-identical — which is the half of the invariant that had to survive.
    See ``test_mcp_lane_agent_context_line.py`` for the declaring case.
    """

    import agent_runtime.persona_runtime as persona_runtime

    def _never(*_args, **_kwargs):
        raise AssertionError("the flag-off path must not resolve admission policy")

    monkeypatch.setattr(persona_runtime, "admission_enabled", lambda: False)
    monkeypatch.setattr(persona_runtime, "resolve_mcp_admission", _never)

    assert persona_runtime.mission_chat_admission_line(_persona("qa"), session_id=None) == ""


def test_the_mission_chat_line_reports_a_denial_with_the_flag_on(qa_profile, monkeypatch):
    import agent_runtime.persona_runtime as persona_runtime

    monkeypatch.setattr(persona_runtime, "admission_enabled", lambda: True)
    monkeypatch.setattr(
        persona_runtime,
        "resolve_mcp_admission",
        lambda *_a, **_k: _denied(MCP_SERVER_NOT_CONFIGURED),
    )

    line = persona_runtime.mission_chat_admission_line(_persona("qa"), session_id=None)

    assert MCP_SERVER_NOT_CONFIGURED in line


def test_the_mission_chat_line_never_fails_a_turn(qa_profile, monkeypatch):
    import agent_runtime.persona_runtime as persona_runtime

    monkeypatch.setattr(persona_runtime, "admission_enabled", lambda: True)

    def _boom(*_a, **_k):
        raise RuntimeError("config is wedged")

    monkeypatch.setattr(persona_runtime, "resolve_mcp_admission", _boom)

    assert persona_runtime.mission_chat_admission_line(_persona("qa"), session_id=None) == ""


# ── the runner's lifecycle wiring ───────────────────────────────────────────


class _SteerRecordingAgent:
    steers: list[str] = []

    def __init__(self, **kwargs):
        self.session_id = kwargs.get("session_id") or "session_r2"
        self.provider = kwargs.get("provider")
        self.model = kwargs.get("model")
        self.base_url = None
        self.tools = []

    def steer(self, text: str) -> bool:
        _SteerRecordingAgent.steers.append(text)
        return True

    def run_conversation(self, user_message, system_message=None, task_id=None):
        return {
            "final_response": "ok",
            "session_id": self.session_id,
            "messages": [],
            "api_calls": 1,
            "total_tokens": 1,
        }


class _RaisingAgent(_SteerRecordingAgent):
    def run_conversation(self, user_message, system_message=None, task_id=None):
        raise RuntimeError("the model blew up mid-turn")


def _admitted_request(**kwargs):
    from agent_runtime.profile_runner import AgentRunRequest

    admission = McpAdmission(
        lane=LANE_MISSION_CHAT,
        role="qa",
        permission_mode="profile_default",
        enabled=True,
        requested=("launcher_qa",),
        server_names=("launcher_qa",),
        server_configs={"launcher_qa": {"command": "noop"}},
    )
    return AgentRunRequest(
        profile=None, user_message="hi", mcp_admission=admission, **kwargs
    )


@pytest.fixture
def torn_down(monkeypatch):
    """Record what the runner handed to teardown, without touching the registry."""

    import agent_runtime.mcp_admission as mcp_admission
    from agent_runtime.mcp_admission import McpTeardownOutcome

    seen: list[tuple[str, ...]] = []

    def _record(servers, **_kwargs):
        seen.append(tuple(servers))
        return McpTeardownOutcome(servers=tuple(servers))

    monkeypatch.setattr(mcp_admission, "teardown_mcp_admission", _record)
    return seen


def _stub_admission(monkeypatch, outcome: McpAdmissionOutcome):
    import agent_runtime.profile_runner as profile_runner

    monkeypatch.setattr(
        profile_runner.ProfileAgentRunner,
        "_admit_mcp_servers",
        lambda self, request, timing, **_kwargs: outcome,
    )


def test_the_runner_tears_the_scope_down_when_the_run_completes(monkeypatch, torn_down):
    from agent_runtime.profile_runner import ProfileAgentRunner

    _stub_admission(monkeypatch, McpAdmissionOutcome(attempted=True, admitted=("launcher_qa",)))

    ProfileAgentRunner(agent_factory=_SteerRecordingAgent).run(_admitted_request())

    assert torn_down == [("launcher_qa",)]


def test_the_runner_tears_the_scope_down_when_the_run_raises(monkeypatch, torn_down):
    """The raised path is the one that would leak.

    An admitted scope that survives a crashed turn is exactly the residue the
    next persona's run must not be able to observe.
    """

    from agent_runtime.profile_runner import ProfileAgentRunner

    _stub_admission(monkeypatch, McpAdmissionOutcome(attempted=True, admitted=("launcher_qa",)))

    with pytest.raises(Exception):
        ProfileAgentRunner(agent_factory=_RaisingAgent).run(_admitted_request())

    assert torn_down == [("launcher_qa",)]


def test_a_run_that_admitted_nothing_tears_nothing_down(monkeypatch, torn_down):
    from agent_runtime.profile_runner import ProfileAgentRunner

    _stub_admission(monkeypatch, McpAdmissionOutcome(attempted=True))

    ProfileAgentRunner(agent_factory=_SteerRecordingAgent).run(_admitted_request())

    assert torn_down == []


def test_the_steer_backstop_fires_only_on_a_degraded_execution(monkeypatch, torn_down):
    from agent_runtime.profile_runner import ProfileAgentRunner

    _SteerRecordingAgent.steers = []
    _stub_admission(monkeypatch, McpAdmissionOutcome(attempted=True, admitted=("launcher_qa",)))
    ProfileAgentRunner(agent_factory=_SteerRecordingAgent).run(_admitted_request())

    assert _SteerRecordingAgent.steers == []

    _SteerRecordingAgent.steers = []
    _stub_admission(monkeypatch, _timed_out_outcome())
    ProfileAgentRunner(agent_factory=_SteerRecordingAgent).run(_admitted_request())

    assert len(_SteerRecordingAgent.steers) == 1
    assert MCP_ADMISSION_TIMEOUT in _SteerRecordingAgent.steers[0]
    assert _SteerRecordingAgent.steers[0].startswith("[harness] ")


# ── §D3: a PARTIAL admission names both halves ──────────────────────────────
#
# The denial half alone is actively misleading on a mixed turn: an agent told
# only "launcher_qa is dark" reads "MCP is dark" and improvises around the
# server it actually HAS — the same W3 improvisation this line exists to stop,
# arrived at from the other direction.


def _partial(admitted: str = "other_server", dark: str = "launcher_qa") -> McpAdmission:
    return McpAdmission(
        lane=LANE_MISSION_CHAT,
        role="qa",
        permission_mode="profile_default",
        enabled=True,
        requested=(dark, admitted),
        server_names=(admitted,),
        denied=(
            McpAdmissionDenial(
                server=dark, code=MCP_ADMISSION_TIMEOUT, summary="s", fix_hint="f"
            ),
        ),
    )


def test_a_partial_admission_names_the_admitted_server_too():
    line = render_mcp_admission_line(_partial())

    assert "launcher_qa (" in line
    assert "Admitted on this turn: other_server" in line
    assert "mcp__<server>__* tools ARE" in line
    assert "\n" not in line


def test_the_admitted_half_never_contradicts_the_denied_half():
    """A server that was admitted and then degraded at execution is DARK, and
    must not also be advertised as available on the same line."""

    admission = McpAdmission(
        lane=LANE_MISSION_CHAT,
        role="qa",
        permission_mode="profile_default",
        enabled=True,
        requested=("launcher_qa",),
        server_names=("launcher_qa",),
    )

    line = render_mcp_admission_line(admission, outcome=_timed_out_outcome())

    assert MCP_ADMISSION_TIMEOUT in line
    assert "Admitted on this turn" not in line


def test_a_fully_denied_turn_still_renders_the_denial_line_verbatim():
    """Byte-stability for the overwhelmingly common shape: nothing admitted,
    nothing appended. This is the line the one-voice drift guard compares."""

    line = render_mcp_admission_line(_denied(MCP_ADMISSION_TIMEOUT))

    assert "Admitted on this turn" not in line
    assert line.endswith(
        "Only an operator can lift this, by fixing the condition the code above "
        "names in the root or persona-profile config.yaml."
    )


def test_a_clean_admission_renders_the_admitted_half_alone():
    """The 2026-07-27 flip, from the other end: the SAME sentence the partial
    line appends is the whole line here — one wording, one place to change it,
    so the two shapes can never drift into two vocabularies."""

    clean = McpAdmission(
        lane=LANE_MISSION_CHAT,
        role="qa",
        permission_mode="profile_default",
        enabled=True,
        requested=("launcher_qa",),
        server_names=("launcher_qa",),
    )

    line = render_mcp_admission_line(clean)
    partial = render_mcp_admission_line(_partial(admitted="launcher_qa", dark="other"))
    sentence = "Admitted on this turn: launcher_qa; those servers' mcp__<server>__* tools ARE in your tool list, so call them directly."

    assert line == f"- MCP tools: {sentence}"
    assert partial.endswith(f" {sentence}")


def test_a_persona_that_declares_no_server_still_pays_nothing():
    """The flip did NOT turn the line into an unconditional tax. Nothing
    requested, nothing admitted, nothing denied — nothing to say."""

    nothing = McpAdmission(
        lane=LANE_MISSION_CHAT,
        role="qa",
        permission_mode="profile_default",
        enabled=True,
        requested=(),
        server_names=(),
    )

    assert render_mcp_admission_line(nothing) == ""
    assert render_mcp_admission_line(None) == ""
