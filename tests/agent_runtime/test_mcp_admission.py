"""Selective MCP admission (R1) — policy, isolation, and bounded execution.

``mcp_lane`` (R0) made the harness lane's MCP drop honest. R1 lets a NARROW,
declared, config-flagged set of personas actually register their declared
servers for one run. Design:
``docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/mission-chat-mcp-admission.md``.

These tests pin the security floor first and the happy path second, because the
failure modes are asymmetric: denying costs a QA agent its visual proof for the
turn outright — the ``qa.request_screenshot`` fallback that once softened this
was removed with the mission lane, so a denial is now terminal for the route —
while over-admitting hands an autonomous agent process-control tools it can loop
on. Deny-by-default still wins, but on the second half of that sentence alone.
In particular they pin that

* nothing is admitted with the flag off (the default),
* the persona/profile DECLARATION is the whole admission authority — role names
  neither widen nor narrow it, and the retired root ``roles`` table is not
  parsed at all,
* nothing is admitted for a persona that declares nothing,
* ``unbounded`` can never widen the admitted set (cross-persona isolation),
* ``read_only`` subtracts the mutating tools,
* resolution NEVER spawns — a registrar that is called at resolve time fails the
  test.

No test here may spawn a real MCP server: registration is always exercised
through an injected fake registrar.
"""

from __future__ import annotations

import dataclasses
import threading
import time
import types

import pytest

from agent_runtime.mcp_admission import (
    LANE_MISSION_CHAT,
    McpAdmissionOutcome,
    MCP_ADMISSION_DISABLED,
    MCP_ADMISSION_LANE_BUSY,
    MCP_ADMISSION_TIMEOUT,
    MCP_NOT_REGISTERED_ON_LANE,
    MCP_READ_ONLY_SUBSET_UNKNOWN,
    MCP_OPERATING_SKILLS,
    MCP_SERVER_NOT_CONFIGURED,
    READ_ONLY_EXCLUDED_TOOLS,
    admission_requirement_failures,
    admitted_operating_skill_ids,
    admit_mcp_servers,
    resolve_mcp_admission,
    scope_toolsets_to_admission,
)
from agent_runtime.machine_roots import ISSUE_PLATFORM_UNSUPPORTED
from tests.agent_runtime.persona_samples import sample_persona, sample_personas
from agent_runtime.runtime_config import McpAdmissionConfig


@pytest.fixture(autouse=True)
def _persist_explicit_persona_data():
    from agent_runtime.store import AgentStore

    store = AgentStore()
    for persona in sample_personas():
        store.save(persona)


# ── fixtures / helpers ──────────────────────────────────────────────────────


def _persona(persona_id: str):
    return {persona.id: persona for persona in sample_personas()}[persona_id]


def _declaring(persona_id: str, *servers: str):
    return dataclasses.replace(_persona(persona_id), required_mcp_servers=list(servers))


def _cfg(**kwargs) -> types.SimpleNamespace:
    """A stand-in runtime config carrying only the admission block.

    Every keyword lands on the real :class:`McpAdmissionConfig`, so a retired
    field fails loudly here instead of being silently discarded.
    """

    return types.SimpleNamespace(mcp_admission=McpAdmissionConfig(**kwargs))


def _bind_profile(monkeypatch, profile_home):
    """Point BOTH declaration probes at one profile home.

    ``profile_readiness`` resolves the binding for the declaration union and
    ``mcp_admission`` resolves it again for the server configs; patching only one
    would let a test pass against a half-wired persona.
    """

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


def _profile_home(tmp_path, config_yaml: str):
    home = tmp_path / "profile"
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(config_yaml, encoding="utf-8")
    return home


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
    home = _profile_home(tmp_path, _LAUNCHER_QA_CONFIG)
    _bind_profile(monkeypatch, home)
    return home


@pytest.fixture
def fake_registered_launcher_qa():
    """Register a fake ``mcp-launcher_qa`` toolset the way MCP registration does.

    Same shape ``tools/mcp_tool._register_server_tools`` produces — a prefixed
    tool under the ``mcp-<server>`` toolset plus the bare-server-name alias — so
    the isolation tests exercise the real registry lookup rather than a mock.
    """

    from tools.registry import registry

    name = "mcp__launcher_qa__mcp_launcher_qa_get_runtime_state"
    registry.register(
        name=name,
        toolset="mcp-launcher_qa",
        schema={"name": name, "description": "fake", "parameters": {"type": "object", "properties": {}}},
        handler=lambda **_kwargs: "{}",
        check_fn=lambda: True,
        description="fake",
    )
    registry.register_toolset_alias("launcher_qa", "mcp-launcher_qa")
    try:
        yield name
    finally:
        registry.deregister(name)


# ── deny-by-default floor ───────────────────────────────────────────────────


def test_flag_off_admits_nothing(qa_profile):
    admission = resolve_mcp_admission(_persona("qa"), cfg=_cfg())

    assert admission.enabled is False
    assert admission.server_names == ()
    assert [row["code"] for row in admission.denial_rows()] == [MCP_ADMISSION_DISABLED]
    assert admission.denied[0].fix_hint


def test_flag_off_is_the_default_config():
    # The kill switch defaults closed. A deployment that never mentions
    # mcp_admission must never admit anything.
    assert McpAdmissionConfig().enabled is False


def test_persona_with_no_declaration_admits_nothing(tmp_path, monkeypatch):
    # The flag is ON and nothing is declared. There is no second grant surface
    # left for a server to arrive from — the declaration is the whole authority.
    _bind_profile(monkeypatch, _profile_home(tmp_path, "model:\n  default: gpt-5\n"))

    admission = resolve_mcp_admission(_persona("qa"), cfg=_cfg(enabled=True))

    assert admission.requested == ()
    assert admission.server_names == ()
    assert admission.denied == ()


def test_a_custom_role_persona_is_admitted_by_its_profile_declaration(qa_profile):
    """Role names are admission METADATA, never a floor.

    A persona carrying a role the harness has never heard of is admitted on the
    strength of its declaration alone, and its role rides the resolved row for
    observability only.
    """

    persona = dataclasses.replace(
        sample_persona("outsider", role="custom-reviewer"),
        required_mcp_servers=["launcher_qa"],
    )

    admission = resolve_mcp_admission(persona, cfg=_cfg(enabled=True))

    assert admission.role == "custom-reviewer"
    assert admission.server_names == ("launcher_qa",)
    assert admission.denied == ()


def test_backend_dev_is_held_out_by_its_profile_only(
    tmp_path, monkeypatch
):
    """The honest blast radius of profile-owned admission, made executable.

    Nothing in the RUNTIME holds `backend_dev` out: it carries `role=dev`
    (`personas.py`), and a role label neither grants nor refuses. What holds it
    out today is its own profile, which declares no `mcp_servers.launcher_qa` to
    spawn — a PROFILE-owned file. Pinning the typed code here means the day
    someone adds that block, this test changes and the widening is re-decided
    rather than inherited. (2026-07-29 review pass; retargeted 2026-08-02, when
    the root role table was retired and the floor it named ceased to exist.)
    """

    _bind_profile(monkeypatch, _profile_home(tmp_path, "model:\n  default: gpt-5\n"))

    admission = resolve_mcp_admission(
        _declaring("backend_dev", "launcher_qa"), cfg=_cfg(enabled=True)
    )

    assert admission.role == "dev"
    assert admission.server_names == ()
    assert [row["code"] for row in admission.denial_rows()] == [MCP_SERVER_NOT_CONFIGURED]


def test_the_lane_is_informational_and_never_gates_admission(qa_profile):
    # `lane` rides the resolved row for observability, exactly like `role`. A
    # lane the harness has never heard of must not narrow the declared set.
    admission = resolve_mcp_admission(
        _persona("qa"), lane="worker", cfg=_cfg(enabled=True)
    )

    assert admission.lane == "worker"
    assert admission.server_names == ("launcher_qa",)


def test_a_profile_declared_server_the_persona_never_required_is_admitted(
    tmp_path, monkeypatch
):
    # `launcher_qa` holds no privileged place in the runtime: an arbitrary server
    # the PROFILE declares — and that `required_mcp_servers` never mentions — is
    # requested and admitted the same way.
    _bind_profile(
        monkeypatch,
        _profile_home(
            tmp_path,
            "mcp_servers:\n  playwright:\n    command: npx\n",
        ),
    )

    admission = resolve_mcp_admission(_persona("qa"), cfg=_cfg(enabled=True))

    assert admission.requested == ("playwright",)
    assert admission.server_names == ("playwright",)
    assert admission.denied == ()


# ── resolution reuses the existing resolvers ────────────────────────────────


def test_admitted_but_unconfigured_server_is_typed(tmp_path, monkeypatch):
    _bind_profile(monkeypatch, _profile_home(tmp_path, "model:\n  default: gpt-5\n"))

    admission = resolve_mcp_admission(
        _declaring("qa", "launcher_qa"), cfg=_cfg(enabled=True)
    )

    assert admission.server_names == ()
    assert [row["code"] for row in admission.denial_rows()] == [MCP_SERVER_NOT_CONFIGURED]


def test_unresolvable_server_reuses_the_machine_roots_taxonomy(tmp_path, monkeypatch):
    # Proving REUSE, not a parallel resolver: the denial carries machine_roots'
    # own code for a server gated to another OS.
    other_os = "linux" if __import__("sys").platform.startswith("win") else "windows"
    _bind_profile(
        monkeypatch,
        _profile_home(
            tmp_path,
            f"mcp_servers:\n  launcher_qa:\n    command: noop\n    platforms: [{other_os}]\n",
        ),
    )

    admission = resolve_mcp_admission(
        _persona("qa"), cfg=_cfg(enabled=True)
    )

    assert admission.server_names == ()
    assert [row["code"] for row in admission.denial_rows()] == [ISSUE_PLATFORM_UNSUPPORTED]


def test_resolution_performs_zero_spawns(qa_profile, monkeypatch):
    import tools.mcp_tool as mcp_tool

    def _explode(*_args, **_kwargs):
        raise AssertionError("resolve_mcp_admission must never register anything")

    monkeypatch.setattr(mcp_tool, "register_mcp_servers", _explode)

    admission = resolve_mcp_admission(
        _persona("qa"), cfg=_cfg(enabled=True)
    )

    assert admission.server_names == ("launcher_qa",)


# ── happy path ──────────────────────────────────────────────────────────────


def test_declared_server_is_admitted_with_a_compiled_config(qa_profile):
    admission = resolve_mcp_admission(
        _persona("qa"), cfg=_cfg(enabled=True)
    )

    assert admission.enabled is True
    assert admission.requested == ("launcher_qa",)
    assert admission.server_names == ("launcher_qa",)
    assert admission.denied == ()
    compiled = admission.server_configs["launcher_qa"]
    assert compiled["command"] == "stagec_qa_mcp_server.exe"
    assert compiled["args"] == ["--stdio"]


def test_dev_is_admitted_from_its_profile_declaration(qa_profile):
    """The behavior the 2026-07-29 R4 widening actually bought.

    The Launcher Dev persona drives the same Stage C surface as QA, and it gets
    there through the SAME single authority — its declaration plus its profile's
    `mcp_servers` block. `role == "dev"` is recorded and consulted by nothing.
    """

    admission = resolve_mcp_admission(
        _declaring("dev", "launcher_qa"), cfg=_cfg(enabled=True)
    )

    assert admission.role == "dev"
    assert admission.requested == ("launcher_qa",)
    assert admission.server_names == ("launcher_qa",)
    assert admission.denied == ()
    assert admission.server_configs["launcher_qa"]["command"] == "stagec_qa_mcp_server.exe"


def test_the_servers_own_connect_timeout_is_clamped_to_the_admission_budget(qa_profile):
    # launcher_qa declares connect_timeout: 60. A mission-chat turn cannot spend
    # that on a capability probe, so the compiled config self-bounds.
    admission = resolve_mcp_admission(
        _persona("qa"),
        cfg=_cfg(enabled=True, connect_timeout_seconds=20.0),
    )

    assert admission.server_configs["launcher_qa"]["connect_timeout"] == 20.0


def test_required_mcp_servers_and_profile_config_both_count_as_declared(
    qa_profile,
):
    admission = resolve_mcp_admission(
        _declaring("qa", "launcher_qa"), cfg=_cfg(enabled=True)
    )

    assert admission.requested == ("launcher_qa",)
    assert admission.server_names == ("launcher_qa",)


def test_explain_is_stable_and_machine_readable(qa_profile):
    explained = resolve_mcp_admission(
        _persona("qa"), cfg=_cfg(enabled=True)
    ).explain()

    assert set(explained) == {
        "lane",
        "role",
        "permission_mode",
        "enabled",
        "requested",
        "admitted",
        "denied",
        "tool_include",
        "blocked_tool_names",
        "connect_timeout_seconds",
        "max_tool_calls_per_run",
    }
    assert explained["admitted"] == ["launcher_qa"]
    assert explained["role"] == "qa"
    # profile_default compiles no include filter: the launcher's full-capability
    # rows are a glob, and a 25-name list would deny the next tool it ships.
    assert explained["tool_include"] == {"launcher_qa": []}


# ── permission-mode composition ─────────────────────────────────────────────


def test_read_only_subtracts_the_mutating_tools(qa_profile):
    admission = resolve_mcp_admission(
        _persona("qa"), permission_mode="read_only", cfg=_cfg(enabled=True)
    )

    assert admission.server_names == ("launcher_qa",)
    excluded = set(admission.server_configs["launcher_qa"]["tools"]["exclude"])
    assert {
        "mcp_launcher_qa_kill_launcher",
        "mcp_launcher_qa_launch_or_attach",
        "mcp_launcher_qa_click_button",
    } <= excluded
    # Belt and braces for a WARM process, where a previous profile_default
    # admission already registered the full surface and register_mcp_servers
    # short-circuits: the mutators are also removed from the model's tool list.
    assert (
        "mcp__launcher_qa__mcp_launcher_qa_kill_launcher" in admission.blocked_tool_names
    )


def test_read_only_keeps_the_read_only_tools(qa_profile):
    # R2 narrowed this to the launcher YAML's `reviewer` row verbatim, so
    # screenshot_window / capture_screenshot / wait_for_state — all of which
    # drive a LIVE launcher window — are no longer kept. See
    # tests/agent_runtime/test_mcp_admission_r2.py for the parity fixture.
    admission = resolve_mcp_admission(
        _persona("qa"), permission_mode="read_only", cfg=_cfg(enabled=True)
    )

    excluded = set(admission.server_configs["launcher_qa"]["tools"]["exclude"])
    for kept in (
        "mcp_launcher_qa_get_runtime_state",
        "mcp_launcher_qa_read_trace",
        "mcp_launcher_qa_run_redaction_scan",
        "mcp_launcher_qa_read_artifact_index",
    ):
        assert kept not in excluded


def test_profile_default_admits_the_full_declared_surface(qa_profile):
    admission = resolve_mcp_admission(
        _persona("qa"), permission_mode="profile_default", cfg=_cfg(enabled=True)
    )

    assert "tools" not in admission.server_configs["launcher_qa"]
    assert admission.blocked_tool_names == ()


def test_read_only_narrows_a_profile_authored_include_list(tmp_path, monkeypatch):
    _bind_profile(
        monkeypatch,
        _profile_home(
            tmp_path,
            "mcp_servers:\n"
            "  launcher_qa:\n"
            "    command: noop\n"
            "    tools:\n"
            "      include: [mcp_launcher_qa_get_auth_state, mcp_launcher_qa_kill_launcher]\n",
        ),
    )

    admission = resolve_mcp_admission(
        _persona("qa"), permission_mode="read_only", cfg=_cfg(enabled=True)
    )

    assert admission.server_configs["launcher_qa"]["tools"]["include"] == [
        "mcp_launcher_qa_get_auth_state"
    ]


def test_read_only_refuses_a_server_it_cannot_subtract(tmp_path, monkeypatch):
    _bind_profile(
        monkeypatch,
        _profile_home(tmp_path, "mcp_servers:\n  playwright:\n    command: npx\n"),
    )

    admission = resolve_mcp_admission(
        _persona("qa"),
        permission_mode="read_only",
        cfg=_cfg(enabled=True),
    )

    assert "playwright" not in READ_ONLY_EXCLUDED_TOOLS
    assert admission.server_names == ()
    assert [row["code"] for row in admission.denial_rows()] == [
        MCP_READ_ONLY_SUBSET_UNKNOWN
    ]


# ── cross-persona isolation: `unbounded` must never widen ───────────────────


def test_unbounded_never_widens_the_admitted_set(fake_registered_launcher_qa):
    # THE security acceptance property. `unbounded` resolves
    # all_registered_toolsets(), which in a warm multi-persona process contains
    # another persona's live admission.
    from agent_runtime.personas import all_registered_toolsets

    everything = all_registered_toolsets()
    assert "mcp-launcher_qa" in everything

    scoped = scope_toolsets_to_admission(everything, admitted_servers=())

    assert "mcp-launcher_qa" not in scoped
    assert "launcher_qa" not in scoped
    assert "file" in scoped or "search" in scoped  # non-MCP toolsets untouched


def test_the_toolset_alias_is_scoped_too(fake_registered_launcher_qa):
    # tools/mcp_tool registers `mcp-<server>` AND a bare `<server>` alias. Scoping
    # only the canonical name would leak the whole surface through the alias.
    scoped = scope_toolsets_to_admission(
        ["file", "launcher_qa", "mcp-launcher_qa"], admitted_servers=()
    )

    assert scoped == ["file"]


def test_an_admitted_run_keeps_its_own_server(fake_registered_launcher_qa):
    scoped = scope_toolsets_to_admission(
        ["file", "mcp-launcher_qa"], admitted_servers=["launcher_qa"]
    )

    assert scoped == ["file", "mcp-launcher_qa"]


def test_admission_adds_its_toolset_when_the_lane_did_not_ask_for_it():
    # The persona's configured toolsets predate the server; admission is what
    # puts it on the lane.
    scoped = scope_toolsets_to_admission(["file"], admitted_servers=["launcher_qa"])

    assert scoped == ["file", "mcp-launcher_qa"]


def test_scoping_is_a_no_op_when_nothing_mcp_is_registered():
    scoped = scope_toolsets_to_admission(["file", "search", "skills"], admitted_servers=())

    assert scoped == ["file", "search", "skills"]


def test_unbounded_chat_lane_resolution_is_scoped_end_to_end(
    fake_registered_launcher_qa, monkeypatch, tmp_path
):
    # Through the real chat-lane chokepoint, with the permission store forced to
    # `unbounded`: a persona with no admission sees no MCP toolset at all.
    from agent_runtime import persona_runtime
    from agent_runtime.tool_visibility import ToolVisibilityOptions

    monkeypatch.setattr(
        persona_runtime,
        "permission_options_for_chat",
        lambda persona, session_id=None, **_kwargs: ToolVisibilityOptions(
            permission_mode="unbounded"
        ),
    )
    _bind_profile(monkeypatch, _profile_home(tmp_path, "model:\n  default: gpt-5\n"))

    resolved = persona_runtime._enabled_toolsets_for_chat(_persona("dev"), session_id="s1")

    assert "mcp-launcher_qa" not in resolved
    assert "launcher_qa" not in resolved
    assert "mission_goal" not in resolved


# ── the admitted surface's operating manual ─────────────────────────────────
#
# Live failure, 2026-07-29: QA mission-chat turns that drove the admitted
# ``launcher_qa`` surface reported ``used_skills: []`` /
# ``queued_skills_loaded: []``. The agent hit ``helper_low_information_capture``
# from ``screenshot_window`` and burned the turn rediscovering nothing — while
# ``launcher-stagec-mcp-screenshot``, granted to the persona and documenting that
# exact refusal's remedy, was never put in context. Admitting a tool surface
# without its manual is the gap these pin closed.


def _qa_with_manual(*servers: str):
    persona = _declaring("qa", *servers)
    return dataclasses.replace(
        persona, skills=[*persona.skills, "launcher-stagec-mcp-screenshot"]
    )


def test_an_admitted_server_resolves_its_operating_manual(qa_profile):
    admission = resolve_mcp_admission(
        _qa_with_manual(), cfg=_cfg(enabled=True)
    )

    assert admission.server_names == ("launcher_qa",)
    assert admitted_operating_skill_ids(
        admission, granted_skills=_qa_with_manual().skills
    ) == ["launcher-stagec-mcp-screenshot"]


def test_the_manual_is_never_invented_for_a_persona_that_was_not_granted_it(qa_profile):
    """The map turns an existing grant into an active load; it never adds one.

    An operator who revoked the grant revoked the preload, with no second place
    to look.

    The ungranted persona is built explicitly rather than taken from the seed:
    the seeded `qa` DOES grant the manual (2026-07-29), and depending on that
    absence would have made this test silently stop exercising revocation the
    moment the seed changed — which is exactly what happened."""

    revoked = dataclasses.replace(
        _persona("qa"),
        skills=[
            skill
            for skill in _persona("qa").skills
            if skill != "launcher-stagec-mcp-screenshot"
        ],
    )

    admission = resolve_mcp_admission(
        revoked, cfg=_cfg(enabled=True)
    )

    assert admission.server_names == ("launcher_qa",)
    assert "launcher-stagec-mcp-screenshot" not in revoked.skills
    assert admitted_operating_skill_ids(admission, granted_skills=revoked.skills) == []


def test_no_admission_object_resolves_no_manual():
    assert admitted_operating_skill_ids(None, granted_skills=["launcher-stagec-mcp-screenshot"]) == []


def test_every_mapped_manual_names_an_admissible_server():
    """The map is keyed by SURFACE, so every key must be a server some role can
    actually be admitted — a key nothing can admit is dead policy."""

    assert set(MCP_OPERATING_SKILLS) == {"launcher_qa"}
    for skills in MCP_OPERATING_SKILLS.values():
        assert skills and all(isinstance(name, str) and name for name in skills)


def test_the_flag_off_turn_resolves_no_manual_and_pays_no_config_load(monkeypatch):
    """Same flag-off invariant the admission LINE keeps: with the kill switch
    off nothing is ever admitted, so there is no surface to document and no
    policy resolve to pay for."""

    from agent_runtime import persona_runtime

    def _never(*_args, **_kwargs):
        raise AssertionError("the flag-off path must not resolve admission")

    monkeypatch.setattr(persona_runtime, "resolve_mcp_admission", _never)

    assert persona_runtime.mission_chat_operating_skills(
        _qa_with_manual("launcher_qa"), session_id=None
    ) == []


def test_the_live_turn_resolves_the_manual_for_an_admitted_persona(
    qa_profile, monkeypatch
):
    """End to end through the REAL kill switch and the REAL declaration path."""

    from agent_runtime import persona_runtime

    _enable_root_admission(monkeypatch)

    assert persona_runtime.mission_chat_operating_skills(
        _qa_with_manual(), session_id=None
    ) == ["launcher-stagec-mcp-screenshot"]


def test_the_seeded_dev_persona_grants_the_admitted_surfaces_manual():
    """Same pairing the seeded `qa` row keeps, for the other seeded persona that
    declares the surface.

    ``admitted_operating_skill_ids`` needs the skill GRANTED as well as admitted,
    so a fresh deployment seeded without this grant would admit `launcher_qa` to
    Launcher Dev and ship no manual — the 2026-07-29 gap, one persona over.
    """

    assert "launcher-stagec-mcp-screenshot" in _persona("dev").skills


def test_an_admitted_manual_that_is_not_installed_degrades_the_preload_quietly(
    qa_profile, monkeypatch
):
    """Hermes grants the manual; it cannot install the manual's CONTENT.

    ``launcher-stagec-mcp-screenshot`` is realm-owned — not under
    ``docs/agent-runtime-harness/harness-skills`` and never written by
    ``skill_install`` — so a realm pull that renames or removes it leaves the
    grant and the admission intact while the file is gone. This drives the REAL
    loader (``DEFAULT_RESOLVERS.build_preloaded_skills_prompt``) against a
    HERMES_HOME where the skill is absent and pins the CURRENT behavior: the
    chat lane never fails a turn over a preload, so the manual lands in the
    ``missing`` accounting the observability row reports and the turn continues
    without it. (The worker-lane analogue raises; this lane deliberately does
    not.) Only ``consume_queued_skills`` is stubbed, because consuming the queue
    is a real store mutation and this test is not about the queue.
    """

    from agent_runtime import mission_chat_turn_context as turn_context

    _enable_root_admission(monkeypatch)
    resolvers = dataclasses.replace(
        turn_context.DEFAULT_RESOLVERS, consume_queued_skills=lambda **_kwargs: []
    )

    preload = turn_context._resolve_skill_preload(
        persona=_qa_with_manual(),
        session_id="chat-admitted-manual-missing",
        native_history=[],
        resolvers=resolvers,
    )

    # Admitted + granted ⇒ runtime policy still REQUIRES it this turn ...
    assert "launcher-stagec-mcp-screenshot" in preload.required
    # ... it just is not on disk, so it is reported, not raised.
    assert "launcher-stagec-mcp-screenshot" in preload.missing
    assert "launcher-stagec-mcp-screenshot" not in preload.loaded


def test_a_non_qa_persona_resolves_the_same_surfaces_manual(qa_profile, monkeypatch):
    """The manual is keyed by SURFACE, so it follows the admitted server rather
    than the persona's role: a `dev` persona that declares `launcher_qa` gets
    exactly the manual `qa` gets."""

    from agent_runtime import persona_runtime

    _enable_root_admission(monkeypatch)
    dev = dataclasses.replace(
        _declaring("dev", "launcher_qa"),
        skills=["harness-dev-delivery", "launcher-stagec-mcp-screenshot"],
    )

    assert persona_runtime.mission_chat_operating_skills(dev, session_id=None) == ["launcher-stagec-mcp-screenshot"]


def test_manual_resolution_never_fails_a_turn(qa_profile, monkeypatch):
    from agent_runtime import persona_runtime

    _enable_root_admission(monkeypatch)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("the admission policy is wedged")

    monkeypatch.setattr(persona_runtime, "resolve_mcp_admission", _boom)

    assert persona_runtime.mission_chat_operating_skills(
        _qa_with_manual(), session_id=None
    ) == []


# ── bounded, single-flight execution ────────────────────────────────────────


def _admission(qa_profile, **cfg_kwargs):
    return resolve_mcp_admission(
        _persona("qa"), cfg=_cfg(enabled=True, **cfg_kwargs)
    )


def test_admission_registers_the_admitted_subset_only(
    qa_profile, fake_registered_launcher_qa
):
    seen: list[dict] = []

    outcome = admit_mcp_servers(
        _admission(qa_profile), register=lambda servers: seen.append(dict(servers)) or ["t"]
    )

    assert list(seen[0]) == ["launcher_qa"]
    assert outcome.admitted == ("launcher_qa",)
    assert outcome.attempted is True


def test_admission_reports_a_server_that_did_not_register(qa_profile):
    # No fake registry entry: the registrar "succeeded" but nothing landed.
    outcome = admit_mcp_servers(_admission(qa_profile), register=lambda _servers: [])

    assert outcome.admitted == ()
    assert [row["code"] for row in outcome.denial_rows()] == [MCP_NOT_REGISTERED_ON_LANE]


def test_a_registrar_that_raises_is_typed_not_propagated(qa_profile):
    def _boom(_servers):
        raise RuntimeError("spawn failed")

    outcome = admit_mcp_servers(_admission(qa_profile), register=_boom)

    assert outcome.admitted == ()
    assert [row["code"] for row in outcome.denial_rows()] == [MCP_NOT_REGISTERED_ON_LANE]


def test_a_stalled_registrar_yields_a_typed_timeout_and_the_turn_continues(qa_profile):
    release = threading.Event()

    def _stall(_servers):
        release.wait(30)
        return []

    started = time.perf_counter()
    try:
        outcome = admit_mcp_servers(
            _admission(qa_profile), register=_stall, timeout_seconds=0.3
        )
        elapsed = time.perf_counter() - started

        assert elapsed < 5
        assert outcome.admitted == ()
        assert [row["code"] for row in outcome.denial_rows()] == [MCP_ADMISSION_TIMEOUT]
        # "The turn continues" has to continue somewhere real. This hint used to
        # send the agent to `qa.request_screenshot`; that contract was deleted in
        # `5a1267ef60`, so it now says there is none and to finish without it.
        hint = outcome.denial_rows()[0]["fix_hint"]
        assert "request_screenshot" not in hint
        assert "no harness-side contract" in hint
        assert "finish without it" in hint
    finally:
        release.set()


def test_concurrent_admission_is_refused_not_interleaved(qa_profile):
    # Registration mutates the PROCESS-GLOBAL registry; two personas admitting at
    # once must serialize, never interleave.
    in_flight = threading.Event()
    release = threading.Event()

    def _hold(_servers):
        in_flight.set()
        release.wait(30)
        return []

    first: dict = {}
    worker = threading.Thread(
        target=lambda: first.update(
            outcome=admit_mcp_servers(_admission(qa_profile), register=_hold)
        )
    )
    worker.start()
    try:
        assert in_flight.wait(5)
        second = admit_mcp_servers(
            _admission(qa_profile), register=lambda _servers: ["should not run"]
        )

        assert second.admitted == ()
        assert [row["code"] for row in second.denial_rows()] == [MCP_ADMISSION_LANE_BUSY]
        # Same fence as the timeout row: retrying is a real route, the deleted
        # `qa.request_screenshot` contract is not.
        hint = second.denial_rows()[0]["fix_hint"]
        assert "request_screenshot" not in hint
        assert "no harness-side contract to take instead" in hint
        assert "retry the turn" in hint.lower()
    finally:
        release.set()
        worker.join(30)


def test_the_single_flight_mutex_is_released_after_a_completed_admission(qa_profile):
    for _ in range(3):
        outcome = admit_mcp_servers(_admission(qa_profile), register=lambda _servers: [])
        assert outcome.attempted is True


def test_nothing_admitted_means_nothing_executed(tmp_path, monkeypatch):
    _bind_profile(monkeypatch, _profile_home(tmp_path, "model:\n  default: gpt-5\n"))

    outcome = admit_mcp_servers(
        resolve_mcp_admission(_persona("qa"), cfg=_cfg(enabled=True)),
        register=lambda _servers: pytest.fail("must not register"),
    )

    assert outcome.attempted is False
    assert outcome.admitted == ()


# ── visibility is truthful BOTH ways ────────────────────────────────────────


def test_a_successful_admission_suppresses_the_r0_drop_row(qa_profile):
    admission = _admission(qa_profile)

    rows = admission_requirement_failures(
        admission,
        declared_servers=["launcher_qa"],
        lane="harness",
        registered_servers=["launcher_qa"],
    )

    assert rows == []


def test_profile_authority_suppresses_a_false_cross_persona_drop(
    tmp_path, monkeypatch
):
    # A NON-qa persona that declares launcher_qa and whose profile configures it
    # is admitted on profile authority, so the drop row must not fire for it. A
    # runtime that refused it on its role name and then reported the honest-
    # looking drop row would be telling an operator the server is unavailable
    # when the only thing missing was a policy the harness no longer has.
    _bind_profile(monkeypatch, _profile_home(tmp_path, _LAUNCHER_QA_CONFIG))
    dev = _declaring("dev", "launcher_qa")
    admission = resolve_mcp_admission(dev, cfg=_cfg(enabled=True))

    rows = admission_requirement_failures(
        admission,
        declared_servers=["launcher_qa"],
        lane="harness",
        registered_servers=["launcher_qa"],
    )

    assert rows == []


def test_an_admitted_but_unregistered_server_still_reports_the_r0_row(qa_profile):
    rows = admission_requirement_failures(
        _admission(qa_profile),
        declared_servers=["launcher_qa"],
        lane="harness",
        registered_servers=[],
    )

    assert [row["code"] for row in rows] == [MCP_NOT_REGISTERED_ON_LANE]


def test_with_admission_disabled_the_rows_are_exactly_the_r0_rows(qa_profile):
    from agent_runtime.mcp_lane import mcp_lane_requirement_failures

    disabled = resolve_mcp_admission(_persona("qa"), cfg=_cfg())

    assert admission_requirement_failures(
        disabled, declared_servers=["launcher_qa"], lane="harness", registered_servers=[]
    ) == mcp_lane_requirement_failures(
        declared_servers=["launcher_qa"], lane="harness", registered_servers=[]
    )


def test_no_admission_object_is_the_r0_answer():
    from agent_runtime.mcp_lane import mcp_lane_requirement_failures

    assert admission_requirement_failures(
        None, declared_servers=["launcher_qa"], lane="harness", registered_servers=[]
    ) == mcp_lane_requirement_failures(
        declared_servers=["launcher_qa"], lane="harness", registered_servers=[]
    )


def _enable_root_admission(monkeypatch, **kwargs):
    """Flip the REAL kill switch for one test, via the root-config loader."""

    from agent_runtime import config as agent_config

    monkeypatch.setattr(
        agent_config,
        "load_root_runtime_config",
        lambda: _cfg(enabled=True, **kwargs),
    )


def test_tool_visibility_stops_reporting_the_drop_once_admission_works(
    qa_profile, fake_registered_launcher_qa, monkeypatch
):
    # The operator-facing surface, end to end: with the flag on, the persona
    # admitted, and the server actually registered, the R0 row is gone.
    from agent_runtime.tool_visibility import ToolVisibilityOptions, resolve_tool_visibility

    _enable_root_admission(monkeypatch)

    visibility = resolve_tool_visibility(
        _persona("qa"), ToolVisibilityOptions(entry_point_lane="harness")
    )

    assert visibility["requirement_failures"] == []


def test_tool_visibility_keeps_reporting_the_drop_with_the_flag_off(qa_profile):
    # The default deployment: admission off, nothing registered, R0's answer
    # unchanged. Adding admission must not quiet the honest row it was built on.
    from agent_runtime.tool_visibility import ToolVisibilityOptions, resolve_tool_visibility

    visibility = resolve_tool_visibility(
        _persona("qa"), ToolVisibilityOptions(entry_point_lane="harness")
    )

    assert [row["code"] for row in visibility["requirement_failures"]] == [
        MCP_NOT_REGISTERED_ON_LANE
    ]


def test_with_the_flag_off_the_registry_still_answers_r0_verbatim(
    qa_profile, fake_registered_launcher_qa
):
    # With admission off there is no per-persona admitted set to intersect, so
    # the R0 rule stands verbatim: a server registered in THIS process suppresses
    # the row. That can only happen on a lane that ran discovery — which R0
    # short-circuits anyway — so it is not a hole; pinning it here keeps the
    # flag-off path provably identical to pre-admission behavior.
    from agent_runtime.tool_visibility import ToolVisibilityOptions, resolve_tool_visibility

    visibility = resolve_tool_visibility(
        _persona("qa"), ToolVisibilityOptions(entry_point_lane="harness")
    )

    assert visibility["requirement_failures"] == []


def test_tool_visibility_accepts_a_profile_declared_server_for_any_role(
    tmp_path, monkeypatch, fake_registered_launcher_qa
):
    from agent_runtime.tool_visibility import ToolVisibilityOptions, resolve_tool_visibility

    _bind_profile(monkeypatch, _profile_home(tmp_path, _LAUNCHER_QA_CONFIG))
    _enable_root_admission(monkeypatch)

    visibility = resolve_tool_visibility(
        _declaring("dev", "launcher_qa"), ToolVisibilityOptions(entry_point_lane="harness")
    )

    assert visibility["requirement_failures"] == []


def test_denial_rows_match_the_typed_issue_contract(qa_profile):
    admission = resolve_mcp_admission(_persona("qa"), cfg=_cfg())

    assert {"code", "server", "summary", "fix_hint"} == set(admission.denial_rows()[0])


# ── config parsing ──────────────────────────────────────────────────────────


def test_config_block_parses_the_documented_shape_and_ignores_retired_roles(tmp_path):
    from agent_runtime.config import load_agent_runtime_config

    path = tmp_path / "config.yaml"
    path.write_text(
        "agent_runtime:\n"
        "  mcp_admission:\n"
        "    enabled: true\n"
        "    connect_timeout_seconds: 20\n"
        "    roles:\n"
        "      qa:\n"
        "        mission_chat: [launcher_qa]\n",
        encoding="utf-8",
    )

    cfg = load_agent_runtime_config(path)

    assert cfg.mcp_admission.enabled is True
    assert cfg.mcp_admission.connect_timeout_seconds == 20.0
    assert not hasattr(cfg.mcp_admission, "roles")


def test_absent_config_block_is_disabled(tmp_path):
    from agent_runtime.config import load_agent_runtime_config

    path = tmp_path / "config.yaml"
    path.write_text("agent_runtime:\n  store_root: x\n", encoding="utf-8")

    assert load_agent_runtime_config(path).mcp_admission.enabled is False


def test_a_legacy_roles_block_is_ignored(tmp_path):
    from agent_runtime.config import load_agent_runtime_config

    path = tmp_path / "config.yaml"
    path.write_text(
        "agent_runtime:\n"
        "  mcp_admission:\n"
        "    enabled: true\n"
        "    roles: 'qa'\n",
        encoding="utf-8",
    )

    assert not hasattr(load_agent_runtime_config(path).mcp_admission, "roles")


def test_the_connect_budget_is_clamped(tmp_path):
    from agent_runtime.config import load_agent_runtime_config

    path = tmp_path / "config.yaml"
    path.write_text(
        "agent_runtime:\n  mcp_admission:\n    connect_timeout_seconds: 9000\n",
        encoding="utf-8",
    )

    assert load_agent_runtime_config(path).mcp_admission.connect_timeout_seconds == 120.0


# ── the agent-construction chokepoint ───────────────────────────────────────


class _CapturingAgent:
    """Minimal fake agent that records the kwargs it was constructed with."""

    last_kwargs: dict | None = None

    def __init__(self, **kwargs):
        _CapturingAgent.last_kwargs = kwargs
        self.session_id = kwargs.get("session_id") or "session_admission"
        self.provider = kwargs.get("provider")
        self.model = kwargs.get("model")
        self.base_url = None
        self.tools = []

    def run_conversation(self, user_message, system_message=None, task_id=None):
        return {
            "final_response": "ok",
            "session_id": self.session_id,
            "messages": [],
            "api_calls": 1,
            "total_tokens": 1,
        }


def _run_capturing(**request_kwargs):
    from agent_runtime.profile_runner import AgentRunRequest, ProfileAgentRunner

    ProfileAgentRunner(agent_factory=_CapturingAgent).run(
        AgentRunRequest(profile=None, user_message="hi", **request_kwargs)
    )
    return _CapturingAgent.last_kwargs


def test_a_run_without_admission_gets_no_mcp_toolset(fake_registered_launcher_qa):
    # Every lane today. Even with a live registration in the process, a run that
    # was not admitted cannot resolve it.
    kwargs = _run_capturing(enabled_toolsets=["file", "mcp-launcher_qa", "launcher_qa"])

    assert kwargs["enabled_toolsets"] == ["file"]


def test_an_admitted_run_carries_its_toolset_and_blocks(qa_profile, monkeypatch):
    import agent_runtime.profile_runner as profile_runner

    admission = resolve_mcp_admission(
        _persona("qa"), permission_mode="read_only", cfg=_cfg(enabled=True)
    )
    monkeypatch.setattr(
        profile_runner.ProfileAgentRunner,
        "_admit_mcp_servers",
        lambda self, request, timing, **_kwargs: McpAdmissionOutcome(
            attempted=True, admitted=tuple(request.mcp_admission.server_names)
        ),
    )

    kwargs = _run_capturing(enabled_toolsets=["file"], mcp_admission=admission)

    assert kwargs["enabled_toolsets"] == ["file", "mcp-launcher_qa"]
    assert (
        "mcp__launcher_qa__mcp_launcher_qa_kill_launcher" in kwargs["blocked_tool_names"]
    )


def test_a_failed_admission_does_not_leave_the_toolset_on_the_run(qa_profile, monkeypatch):
    # The registration timed out / never landed: the run must not advertise a
    # toolset that resolves to nothing.
    import agent_runtime.profile_runner as profile_runner

    admission = resolve_mcp_admission(
        _persona("qa"), cfg=_cfg(enabled=True)
    )
    monkeypatch.setattr(
        profile_runner.ProfileAgentRunner,
        "_admit_mcp_servers",
        lambda self, request, timing, **_kwargs: McpAdmissionOutcome(attempted=True),
    )

    kwargs = _run_capturing(enabled_toolsets=["file"], mcp_admission=admission)

    assert kwargs["enabled_toolsets"] == ["file"]


def test_registry_hygiene_still_applies_alongside_admission(qa_profile, monkeypatch):
    from agent_runtime.personas import REGISTRY_HYGIENE_BLOCKED_TOOLS
    import agent_runtime.profile_runner as profile_runner

    admission = resolve_mcp_admission(
        _persona("qa"), permission_mode="read_only", cfg=_cfg(enabled=True)
    )
    monkeypatch.setattr(
        profile_runner.ProfileAgentRunner,
        "_admit_mcp_servers",
        lambda self, request, timing, **_kwargs: McpAdmissionOutcome(
            attempted=True, admitted=tuple(request.mcp_admission.server_names)
        ),
    )

    kwargs = _run_capturing(blocked_tool_names=[], mcp_admission=admission)

    assert REGISTRY_HYGIENE_BLOCKED_TOOLS.issubset(set(kwargs["blocked_tool_names"]))


def test_explain_mcp_is_stable_machine_readable_and_registers_nothing(
    qa_profile, monkeypatch, capsys
):
    # The operator's pre-flip inspection surface. It must answer without
    # connecting to anything — a preview that spawns is not a preview.
    import argparse
    import json

    import tools.mcp_tool as mcp_tool
    from hermes_cli.harness import build_parser

    monkeypatch.setattr(
        mcp_tool,
        "register_mcp_servers",
        lambda *_a, **_k: pytest.fail("--explain-mcp must not register anything"),
    )
    _enable_root_admission(monkeypatch)

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        ["harness", "persona", "tool-diff", "qa", "--explain-mcp", "--json"]
    )

    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["mcp_admission"]["enabled"] is True
    assert payload["mcp_admission"]["requested"] == ["launcher_qa"]
    assert payload["mcp_admission"]["admitted"] == ["launcher_qa"]
    assert payload["mcp_admission"]["lane"] == LANE_MISSION_CHAT


def test_tool_diff_without_the_flag_explains_the_disabled_state(qa_profile, capsys):
    import argparse
    import json

    from hermes_cli.harness import build_parser

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        ["harness", "persona", "tool-diff", "qa", "--explain-mcp", "--json"]
    )

    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["mcp_admission"]["enabled"] is False
    assert payload["mcp_admission"]["admitted"] == []
    assert [row["code"] for row in payload["mcp_admission"]["denied"]] == [
        MCP_ADMISSION_DISABLED
    ]


def test_tool_diff_stays_silent_about_mcp_without_the_flag(qa_profile, capsys):
    # No --explain-mcp: the payload is unchanged from before this feature.
    import argparse
    import json

    from hermes_cli.harness import build_parser

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["harness", "persona", "tool-diff", "qa", "--json"])

    assert args.func(args) == 0

    assert "mcp_admission" not in json.loads(capsys.readouterr().out)


def test_enabled_toolsets_none_is_passed_through_untouched():
    # `None` means "everything the agent resolves by default"; narrowing it here
    # would change what an unscoped run gets.
    kwargs = _run_capturing(enabled_toolsets=None)

    assert kwargs["enabled_toolsets"] is None
