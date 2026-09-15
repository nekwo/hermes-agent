from types import SimpleNamespace

from agent_runtime.models import PersonaInstance
from agent_runtime.operator_channels import _turn_identity_dropped, operator_channel_summary
from agent_runtime.parity import ProjectionAccountant
from agent_runtime.persona_chat_history import persona_chat_history_summary
from agent_runtime.states import WorkerSessionState


class _SessionDB:
    def __init__(self, sessions):
        self.sessions = list(sessions)

    def list_sessions_rich(self, **kwargs):
        return list(self.sessions)

    def get_session(self, session_id):
        return next(
            (dict(row) for row in self.sessions if row.get("id") == session_id),
            None,
        )

    def get_messages(self, session_id):
        return []


def _instance(session_id, *, persona_id="dev", mode="chat", task_id=None):
    return PersonaInstance(
        id=f"personainst_{persona_id}",
        persona_id=persona_id,
        role="dev",
        display_name=persona_id,
        profile_id=None,
        runtime_root="runtime",
        state=WorkerSessionState.IDLE,
        mode=mode,
        current_task_id=task_id,
        session_id=session_id,
        default_chat_session_id=session_id,
    )


def test_task_bound_mirrored_chat_pointer_projects_mission_without_db_drop():
    accountant = ProjectionAccountant("persona_chat_history")
    rows = persona_chat_history_summary(
        persona_instances=[
            _instance("persona_chat_personainst_dev_live", mode="task_bound", task_id="task_1")
        ],
        session_db=_SessionDB([]),
        accountant=accountant,
    )

    assert [(row["session_id"], row["kind"]) for row in rows] == [
        ("persona_chat_personainst_dev_live", "mission")
    ]
    assert "session_not_in_db" not in accountant.summary()["reasons"]


def test_history_limit_reports_exact_omitted_session_ids():
    omitted = set()
    rows = persona_chat_history_summary(
        persona_instances=[_instance("new"), _instance("old", persona_id="qa")],
        session_db=_SessionDB([
            {"id": "new", "started_at": 20},
            {"id": "old", "started_at": 10},
        ]),
        limit=1,
        omitted_session_ids=omitted,
    )

    assert [row["session_id"] for row in rows] == ["new"]
    assert omitted == {"old"}


def test_operator_channel_suppresses_only_explicit_bounded_history_warning():
    session_id = "old"
    channels = operator_channel_summary(
        persona_instances=[_instance(session_id)],
        persona_chat_history=[],
        persona_chat_trace=[{
            "session_id": session_id,
            "persona_id": "dev",
            "persona_instance_id": "personainst_dev",
            "entries": [{
                "event": "progress",
                "summary": "Working",
                "status": "running",
                "ts": "2026-07-28T12:00:00Z",
            }],
        }],
        intentionally_omitted_history_session_ids={session_id},
    )

    assert not any(
        warning["code"] == "session_without_history"
        for warning in channels[0]["warnings"]
    )

    unsuppressed = operator_channel_summary(
        persona_instances=[_instance(session_id)],
        persona_chat_history=[],
        persona_chat_trace=[{
            "session_id": session_id,
            "persona_id": "dev",
            "persona_instance_id": "personainst_dev",
            "entries": [{
                "event": "progress",
                "summary": "Working",
                "status": "running",
                "ts": "2026-07-28T12:00:00Z",
            }],
        }],
    )
    assert any(
        warning["code"] == "session_without_history"
        for warning in unsuppressed[0]["warnings"]
    )


def test_turn_identity_drop_is_matched_to_its_source_entry():
    entries = [
        {"ts": "old", "tool_name": "terminal"},
        {"ts": "new", "tool_name": "terminal", "turn_id": "turn_2"},
    ]
    messages = [
        {
            "kind": "tool_call",
            "timestamp": "old",
            "refs": {"source": "persona_chat_trace", "tool_name": "terminal"},
        },
        {
            "kind": "tool_call",
            "timestamp": "new",
            "turn_id": "turn_2",
            "refs": {"source": "persona_chat_trace", "tool_name": "terminal"},
        },
    ]

    assert _turn_identity_dropped(entries, messages) is False
    messages[1].pop("turn_id")
    assert _turn_identity_dropped(entries, messages) is True


def test_codex_readiness_peeks_pool_without_refreshing(monkeypatch):
    from agent import credential_pool
    from agent_runtime import profile_readiness

    calls = {"peek": 0, "select": 0}

    class Pool:
        def peek(self):
            calls["peek"] += 1
            return SimpleNamespace(runtime_api_key="present")

        def select(self):
            calls["select"] += 1
            raise AssertionError("readiness must not refresh credentials")

    monkeypatch.setattr(credential_pool, "load_pool", lambda provider: Pool())

    assert profile_readiness._compute_provider_issue(
        profile_readiness.resolve_runtime_provider,
        "openai-codex",
        "gpt-5.6",
    ) is None
    assert calls == {"peek": 1, "select": 0}


def test_equivalent_prompt_contexts_reuse_skill_rows(monkeypatch):
    from agent_runtime import prompt_observability as po

    calls = {"accessible": 0, "available": 0}

    def accessible(*args, **kwargs):
        calls["accessible"] += 1
        return [{"name": "shared-skill", "status": "assigned_not_loaded"}]

    def available(*args, **kwargs):
        calls["available"] += 1
        return [{"name": "shared-skill", "status": "assigned_not_loaded"}]

    monkeypatch.setattr(po, "_installed_skill_catalog", lambda: [])
    monkeypatch.setattr(po, "_accessible_skills_context", accessible)
    monkeypatch.setattr(po, "available_skills_context", available)
    monkeypatch.setattr(po, "_persona_skill_assignment_removals", lambda persona: [])
    resolver = po._SkillObservabilityResolver()
    monkeypatch.setattr(resolver, "resolve", lambda identifiers: {})
    persona = SimpleNamespace(
        id="dev",
        hermes_profile="dev",
        display_name="Launcher Dev",
        role="dev",
        skills=["shared-skill"],
    )

    po.mission_chat_prompt_observability(
        persona=persona,
        persona_instance_id="personainst_dev_a",
        session_id="chat_a",
        skill_resolver=resolver,
    )
    po.mission_chat_prompt_observability(
        persona=persona,
        persona_instance_id="personainst_dev_b",
        session_id="chat_b",
        skill_resolver=resolver,
    )

    assert calls == {"accessible": 1, "available": 1}
