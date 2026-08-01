import os

from agent_runtime.models import AgentPersona
from agent_runtime.profile_readiness import profile_readiness_for_persona
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.states import TaskState
from hermes_time import now


def test_profile_readiness_reports_missing_profile_without_secret_paths():
    persona = AgentPersona(
        id="qa",
        display_name="QA",
        role="qa",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=["file"],
        system_prompt_path="personas/qa/system.md",
        hermes_profile="definitely-missing-stage9-profile",
        skills=["definitely-missing-stage9-skill"],
    )

    readiness = profile_readiness_for_persona(persona)

    assert readiness["readiness"] == "missing_profile"
    assert readiness["hermes_profile"] == "definitely-missing-stage9-profile"
    assert readiness["missing_skills"] == []
    assert "access_token" not in str(readiness).lower()
    assert "api_key" not in str(readiness).lower()


def test_profile_readiness_checks_skills_inside_bound_profile(tmp_path, monkeypatch):
    from agent_runtime import profile_context

    profile_home = tmp_path / "profiles" / "qa"
    profile_home.mkdir(parents=True)
    # A server with NO canonical template: this test is about skills resolving
    # inside the bound profile, and since 2026-08-01 a stub ``launcher_qa`` block
    # is (correctly) blocking template drift, which would answer a different
    # question than the one asked here.
    (profile_home / "config.yaml").write_text(
        "mcp:\n  servers:\n    stagec_probe:\n      command: stagec-probe\n",
        encoding="utf-8",
    )
    skill = profile_home / "skills" / "profile-only-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Profile Only Skill\n", encoding="utf-8")
    monkeypatch.setattr(profile_context, "profile_exists", lambda name: name == "qa")
    monkeypatch.setattr(profile_context, "get_profile_dir", lambda name: profile_home)

    persona = AgentPersona(
        id="qa",
        display_name="QA",
        role="qa",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=["file"],
        system_prompt_path="personas/qa/system.md",
        hermes_profile="qa",
        skills=["profile-only-skill"],
        required_mcp_servers=["stagec_probe"],
    )

    readiness = profile_readiness_for_persona(persona)

    assert readiness["readiness"] == "ready"
    assert readiness["missing_skills"] == []
    assert readiness["missing_mcp_servers"] == []


def test_profile_readiness_finds_nested_profile_skills_by_frontmatter_name(tmp_path, monkeypatch):
    from agent_runtime import profile_context

    profile_home = tmp_path / "profiles" / "dev"
    skill = profile_home / "skills" / "github" / "github-code-review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: github-code-review\n---\n# GitHub Code Review\n", encoding="utf-8")
    monkeypatch.setattr(profile_context, "profile_exists", lambda name: name == "dev")
    monkeypatch.setattr(profile_context, "get_profile_dir", lambda name: profile_home)

    persona = AgentPersona(
        id="dev",
        display_name="Dev",
        role="dev",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=["file"],
        system_prompt_path="personas/dev/system.md",
        hermes_profile="dev",
        skills=["github-code-review"],
    )

    readiness = profile_readiness_for_persona(persona)

    assert readiness["readiness"] == "ready"
    assert readiness["missing_skills"] == []


def test_readiness_receipt_reports_effective_hash_and_loadability(
    tmp_path, monkeypatch
):
    import agent.skill_utils as skill_utils
    from agent_runtime.profile_readiness import _resolve_skill_names

    shared = tmp_path / "shared"
    manifest = shared / "harness-runtime-model" / "SKILL.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        "---\nname: harness-runtime-model\nmetadata:\n  hermes:\n"
        "    surfaces: [mission_chat]\n    modes: [standard]\n"
        "    load_policy: required_preload\n---\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(skill_utils, "get_shared_skills_dir", lambda: shared)
    monkeypatch.setattr(skill_utils, "get_all_skills_dirs", lambda: [shared])

    row = _resolve_skill_names(["harness-runtime-model"])[0]

    assert row["status"] == "resolved"
    assert row["source_kind"] == "shared_core"
    assert row["installed_hash"] == row["content_hash"]
    assert row["expected_hash"]
    assert isinstance(row["hash_matches_expected"], bool)
    assert row["loadable"] is True
    assert row["loadability"]["mission_chat"]["load_policy"] == "required_preload"


def test_profile_readiness_reports_provider_auth_attention(monkeypatch):
    from hermes_cli.auth import AuthError
    from agent_runtime import profile_readiness

    def fake_resolve_runtime_provider(*, requested=None, target_model=None):
        raise AuthError("missing provider credential", provider=requested, code="missing")

    monkeypatch.setattr(profile_readiness, "resolve_runtime_provider", fake_resolve_runtime_provider)
    monkeypatch.setattr(profile_readiness, "_runtime_dependency_issue", lambda _persona: None)

    persona = AgentPersona(
        id="dev",
        display_name="Dev",
        role="dev",
        model="gpt-5.1-codex-max",
        provider="openai-codex",
        api_mode="codex_responses",
        toolsets=["file"],
        system_prompt_path="personas/dev/system.md",
    )

    readiness = profile_readiness_for_persona(persona)
    assert readiness["readiness"] == "auth_attention"
    assert "missing provider credential" in readiness["summary"]
    assert "api_key" not in str(readiness).lower()


def test_profile_readiness_reports_runtime_dependency_missing_before_auth(monkeypatch):
    from hermes_cli.auth import AuthError
    from agent_runtime import profile_readiness

    def fake_runtime_dependency_issue(_persona):
        return ("runtime_dependency_missing", "Missing runtime packages: openai")

    def fake_resolve_runtime_provider(*, requested=None, target_model=None):
        raise AuthError("missing provider credential", provider=requested, code="missing")

    monkeypatch.setattr(profile_readiness, "_runtime_dependency_issue", fake_runtime_dependency_issue)
    monkeypatch.setattr(profile_readiness, "resolve_runtime_provider", fake_resolve_runtime_provider)

    persona = AgentPersona(
        id="dev",
        display_name="Dev",
        role="dev",
        model="gpt-5.1-codex-max",
        provider="openai-codex",
        api_mode="codex_responses",
        toolsets=["file"],
        system_prompt_path="personas/dev/system.md",
    )

    readiness = profile_readiness_for_persona(persona)
    assert readiness["readiness"] == "runtime_dependency_missing"
    assert readiness["summary"] == "Missing runtime packages: openai"


def test_profile_readiness_injects_launcher_qa_only_for_visual_scope(monkeypatch):
    from agent_runtime import profile_readiness

    monkeypatch.setattr(profile_readiness, "_missing_skill_names", lambda _skills: [])
    monkeypatch.setattr(profile_readiness, "harness_skill_hash_mismatches", lambda _skills, hermes_home=None: [])
    monkeypatch.setattr(profile_readiness, "_runtime_dependency_issue", lambda _persona: None)
    monkeypatch.setattr(profile_readiness, "_provider_issue", lambda _persona: None)

    persona = AgentPersona(
        id="qa",
        display_name="QA",
        role="qa",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=["file"],
        system_prompt_path="personas/qa/system.md",
    )
    plain_task = Task(id="task_plain", title="Plain", description="Plain", state=TaskState.CREATED, created_at=now(), updated_at=now(), requested_by="test")
    visual_task = Task(id="task_visual", title="Mission Control", description="needs screenshot", state=TaskState.CREATED, created_at=now(), updated_at=now(), requested_by="test", requires_visual_proof=True)

    plain = profile_readiness_for_persona(persona, task=plain_task)
    visual = profile_readiness_for_persona(persona, task=visual_task)

    assert plain["effective_required_mcp_servers"] == []
    assert visual["effective_required_mcp_servers"] == ["launcher_qa"]
    assert visual["missing_mcp_servers"] == ["launcher_qa"]


def test_readiness_missing_set_is_single_source_derivation(tmp_path, monkeypatch):
    """Item 1: profile_readiness derives the missing-skill set from the ONE
    resolution it already computed (no redundant second resolver walk). The
    derived set must equal both the rows' ``status == 'missing'`` ids and the
    re-resolving compatibility wrapper — proving the removal is behavior-identical
    including duplicate occurrences and order."""
    import agent.skill_utils as skill_utils
    from agent_runtime.profile_readiness import (
        _missing_skill_ids,
        _missing_skill_names,
        _resolve_skill_names,
    )

    shared = tmp_path / "shared"
    (shared / "present-skill").mkdir(parents=True)
    (shared / "present-skill" / "SKILL.md").write_text(
        "---\nname: present-skill\n---\nbody\n", encoding="utf-8"
    )
    monkeypatch.setattr(skill_utils, "get_shared_skills_dir", lambda: shared)
    monkeypatch.setattr(skill_utils, "get_all_skills_dirs", lambda: [shared])

    names = ["present-skill", "absent-one", "absent-two", "present-skill"]
    rows = _resolve_skill_names(names)
    # One row per occurrence, original order preserved (dedup would drop the dup).
    assert [row["skill_id"] for row in rows] == names
    derived = _missing_skill_ids(rows)
    assert derived == ["absent-one", "absent-two"]
    # The compat wrapper (which re-resolves) agrees with the single-source
    # derivation from the already-computed rows.
    assert derived == _missing_skill_names(names)


def test_provider_issue_memo_is_scoped_per_profile_home(monkeypatch):
    # The memo runs inside persona_profile_context, which diverts HERMES_HOME /
    # HERMES_AUTH_HOME so the resolver reads PER-PROFILE config and secrets.
    # Keyed on (provider, model) alone, profile A's verdict would leak to
    # profile B within the TTL (adversarial review of 7f6ac5208, finding 1).
    from agent_runtime import profile_readiness

    profile_readiness._provider_issue_cache_clear()
    calls: list[str] = []

    def _resolver(requested=None, target_model=None):
        calls.append(os.environ.get("HERMES_HOME") or "")
        if (os.environ.get("HERMES_HOME") or "").endswith("bob"):
            raise profile_readiness.AuthError("no credential for bob")

    monkeypatch.setattr(profile_readiness, "resolve_runtime_provider", _resolver)
    persona = AgentPersona(
        id="dev",
        display_name="Dev",
        role="dev",
        model="gpt-5",
        provider="openai",
        api_mode=None,
        toolsets=[],
        system_prompt_path="personas/dev/system.md",
        hermes_profile=None,
        skills=[],
    )

    monkeypatch.setenv("HERMES_HOME", r"X:\fake\profiles\alice")
    assert profile_readiness._provider_issue(persona) is None
    # Same (provider, model) from a DIFFERENT profile home must re-resolve and
    # surface ITS OWN verdict, never alice's cached "ready".
    monkeypatch.setenv("HERMES_HOME", r"X:\fake\profiles\bob")
    issue = profile_readiness._provider_issue(persona)
    assert issue is not None and issue[0] == "auth_attention"
    assert len(calls) == 2
    # Within the same profile home the TTL memo still deduplicates.
    assert profile_readiness._provider_issue(persona)[0] == "auth_attention"
    assert len(calls) == 2
    profile_readiness._provider_issue_cache_clear()
