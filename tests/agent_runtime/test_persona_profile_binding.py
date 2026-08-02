"""The persona ⇄ Hermes-profile rebind chokepoint.

Covers the three defects that made an ad-hoc ``AgentStore.save`` rebind unsafe
(live incident 2026-07-25): silent config/store divergence, a
``persona_instance.profile_id`` projection that only self-heals for the
canonical row, and an ``agent list`` ``profile`` column that structurally could
not report the binding it was named after.
"""

from __future__ import annotations

import json

import pytest

from agent_runtime import paths
from agent_runtime.events import EventLog
from agent_runtime.models import AgentPersona, PersonaInstance
from agent_runtime.persona_assignments import PersonaInstanceStore
from agent_runtime.persona_profile_binding import (
    BINDING_SOURCE_CONFIG,
    BINDING_SOURCE_STORE,
    BINDING_SOURCE_UNBOUND,
    BUSY_ACTIVE_RUN,
    BUSY_ASSIGNMENT_IN_FLIGHT,
    BUSY_LIVE_BINDING,
    BUSY_NON_IDLE_STATE,
    BUSY_TASK_BOUND,
    REBIND_EVENT_TYPE,
    PersonaProfileRebindError,
    binding_files,
    binding_index,
    instance_busy_reason,
    rebind_persona_profile,
    resolve_effective_binding,
)
from agent_runtime.states import WorkerSessionState
from agent_runtime.store import AgentStore, RealmStore, WorkspaceStore
from hermes_time import now


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def profiles(tmp_path, monkeypatch):
    """Two real profile homes under an isolated HERMES_HOME."""

    home = tmp_path / "hermes-home"
    for name in ("alpha", "beta"):
        profile = home / "profiles" / name
        (profile / "memories").mkdir(parents=True, exist_ok=True)
        (profile / "config.yaml").write_text("model: {}\n", encoding="utf-8")
        (profile / "memories" / "MEMORY.md").write_text(f"# {name} memory\n", encoding="utf-8")
        (profile / "soul.md").write_text(f"{name} soul\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _persona(**overrides) -> AgentPersona:
    fields = {
        "id": "widget",
        "display_name": "Widget Agent",
        "role": "dev",
        "model": None,
        "provider": None,
        "api_mode": "codex_responses",
        "toolsets": ["file"],
        "system_prompt_path": "",
        "hermes_profile": "alpha",
        "soul_overlay_path": "soul.md",
        "include_profile_memory": True,
    }
    fields.update(overrides)
    return AgentPersona(**fields)


def _seed(persona: AgentPersona, *, placements: tuple[str, ...] = ("agent_aaa", "agent_bbb")) -> AgentPersona:
    """Persist the persona plus its canonical channel and placement rows."""

    saved = AgentStore().save(persona)
    store = PersonaInstanceStore()
    store.ensure_for_persona(saved)
    for suffix in placements:
        store._write(  # noqa: SLF001 — seeding a drifted projection row on purpose
            PersonaInstance(
                id=f"personainst_{saved.id}_{suffix}",
                persona_id=saved.id,
                role=saved.role,
                display_name=saved.display_name,
                profile_id=saved.hermes_profile,
                runtime_root=str(paths.store_root()),
                state=WorkerSessionState.IDLE,
                mode="chat",
                updated_at=now(),
            )
        )
    return saved


def _store_bytes() -> dict[str, bytes]:
    root = paths.store_root()
    if not root.exists():
        return {}
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


# --------------------------------------------------------------------------- #
# 1. (config says X, store says Y) -> effective binding  [pure table]
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "config_profile,store_profile,config_declared,store_row_present,expected_effective,expected_source,expected_diverged",
    [
        # The live incident: config.yaml said `neko`, the store said `base`, and
        # ensure_persisted_personas resolved {**catalog, **stored} store-wins.
        ("neko", "base", True, True, "base", BINDING_SOURCE_STORE, True),
        # Agreement is the healthy case.
        ("neko", "neko", True, True, "neko", BINDING_SOURCE_STORE, False),
        # A stored record wins WHOLESALE, not per field: an empty stored binding
        # still beats a config that names one (persona inherits the active profile).
        ("neko", None, True, True, None, BINDING_SOURCE_STORE, True),
        # No stored row at all -> the config catalog is the binding, no divergence.
        ("neko", "base", True, False, "neko", BINDING_SOURCE_CONFIG, False),
        # Store-only persona: config has no opinion, so this is not a disagreement.
        (None, "base", False, True, "base", BINDING_SOURCE_STORE, False),
        # Neither side binds anything.
        (None, None, False, False, None, BINDING_SOURCE_UNBOUND, False),
        # Whitespace is not a binding.
        ("  ", "  ", True, True, None, BINDING_SOURCE_STORE, False),
    ],
)
def test_effective_binding_table(
    config_profile,
    store_profile,
    config_declared,
    store_row_present,
    expected_effective,
    expected_source,
    expected_diverged,
):
    binding = resolve_effective_binding(
        "widget",
        config_profile=config_profile,
        store_profile=store_profile,
        config_declared=config_declared,
        store_row_present=store_row_present,
    )

    assert binding.effective_profile == expected_effective
    assert binding.source == expected_source
    assert binding.diverged is expected_diverged


def test_binding_index_reports_the_config_store_disagreement(profiles, monkeypatch):
    _seed(_persona(hermes_profile="beta"))

    class _Cfg:
        personas: dict = {}

    def _catalog(_cfg=None):
        return [_persona(hermes_profile="alpha")]

    monkeypatch.setattr("agent_runtime.config.persona_records_from_config", _catalog)
    monkeypatch.setattr("agent_runtime.config.load_agent_runtime_config", lambda *a, **k: _Cfg())

    diverged = [binding for binding in binding_index().values() if binding.diverged]

    assert [item.persona_id for item in diverged] == ["widget"]
    row = diverged[0].as_row()
    assert row["config_profile"] == "alpha"
    assert row["store_profile"] == "beta"
    assert row["effective_profile"] == "beta"
    assert row["binding_source"] == BINDING_SOURCE_STORE


# --------------------------------------------------------------------------- #
# 2. busy classifier  [pure table]
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "row,kwargs,expected",
    [
        ({"state": "idle"}, {}, None),
        ({"state": "closed"}, {}, None),
        # goal_id alone is NOT in-flight: a chat instance can carry a goal pointer
        # while nothing executes.
        ({"state": "idle", "goal_id": "goal_1"}, {}, None),
        ({"state": "idle"}, {"live_binding": True}, BUSY_LIVE_BINDING),
        ({"state": "idle", "active_run_id": "run_1"}, {}, BUSY_ACTIVE_RUN),
        # S56 deleted BUSY_ACTIVE_WORKER with the worker-session store. The row
        # still SENDS the field (a stale producer can) but it no longer makes an
        # instance busy.
        ({"state": "idle", "active_worker_session_id": "ws_1"}, {}, None),
        ({"state": "idle", "current_assignment_id": "as_1"}, {}, BUSY_ASSIGNMENT_IN_FLIGHT),
        ({"state": "idle"}, {"assignment_in_flight": True}, BUSY_ASSIGNMENT_IN_FLIGHT),
        ({"state": "idle", "current_task_id": "task_1"}, {}, BUSY_TASK_BOUND),
        ({"state": "running"}, {}, BUSY_NON_IDLE_STATE),
        ({"state": WorkerSessionState.WAITING_ON_TOOL}, {}, BUSY_NON_IDLE_STATE),
        # A stale pointer must never outrank the store-verified liveness check.
        ({"state": "idle", "current_task_id": "task_1"}, {"live_binding": True}, BUSY_LIVE_BINDING),
    ],
)
def test_instance_busy_reason_table(row, kwargs, expected):
    assert instance_busy_reason(row, **kwargs) == expected


# --------------------------------------------------------------------------- #
# 3. the cascade
# --------------------------------------------------------------------------- #
def test_rebind_cascades_every_instance_projection(profiles):
    _seed(_persona())

    result = rebind_persona_profile("widget", profile="beta")

    assert result["changed"] is True
    assert AgentStore().get("widget").hermes_profile == "beta"
    bound = {item.id: item.profile_id for item in PersonaInstanceStore().list_all()}
    # The canonical row self-heals on its own; the placement rows are the ones
    # that used to drift forever.
    assert bound == {
        "personainst_widget": "beta",
        "personainst_widget_agent_aaa": "beta",
        "personainst_widget_agent_bbb": "beta",
    }
    assert {row["persona_instance_id"] for row in result["instances_moved"]} == set(bound)


def test_rebind_leaves_other_personas_instances_alone(profiles):
    _seed(_persona())
    other = AgentStore().save(_persona(id="gadget", display_name="Gadget Agent"))
    PersonaInstanceStore().ensure_for_persona(other)

    rebind_persona_profile("widget", profile="beta")

    assert PersonaInstanceStore().get("personainst_gadget").profile_id == "alpha"
    assert AgentStore().get("gadget").hermes_profile == "alpha"


def test_rebind_repairs_a_drifted_projection_without_moving_the_persona(profiles):
    persona = _seed(_persona(hermes_profile="beta"))
    drifted = PersonaInstanceStore().get(f"personainst_{persona.id}_agent_aaa")
    drifted.profile_id = "alpha"
    PersonaInstanceStore()._write(drifted)  # noqa: SLF001

    result = rebind_persona_profile("widget", profile="beta")

    assert result["persona_changed"] is False
    assert result["changed"] is True
    assert [row["persona_instance_id"] for row in result["instances_moved"]] == [
        "personainst_widget_agent_aaa"
    ]
    assert PersonaInstanceStore().get("personainst_widget_agent_aaa").profile_id == "beta"


# --------------------------------------------------------------------------- #
# 4. busy refusal
# --------------------------------------------------------------------------- #
def test_rebind_refuses_while_an_instance_is_busy_and_writes_nothing(profiles):
    _seed(_persona())
    busy = PersonaInstanceStore().get("personainst_widget_agent_aaa")
    busy.current_task_id = "task_live"
    PersonaInstanceStore()._write(busy)  # noqa: SLF001
    before = _store_bytes()

    with pytest.raises(PersonaProfileRebindError) as excinfo:
        rebind_persona_profile("widget", profile="beta")

    assert excinfo.value.code == "instances_busy"
    named = {item["persona_instance_id"] for item in excinfo.value.details["busy_instances"]}
    assert named == {"personainst_widget_agent_aaa"}
    assert "personainst_widget_agent_aaa" in str(excinfo.value)
    # A busy row blocks the WHOLE operation — never a partial cascade.
    assert _store_bytes() == before


def test_rebind_refuses_on_a_live_run_binding(profiles, monkeypatch):
    _seed(_persona())
    monkeypatch.setattr(PersonaInstanceStore, "_has_live_binding", lambda self, instance: instance.id.endswith("bbb"))

    with pytest.raises(PersonaProfileRebindError) as excinfo:
        rebind_persona_profile("widget", profile="beta")

    assert excinfo.value.code == "instances_busy"
    assert excinfo.value.details["busy_instances"][0]["reason"] == BUSY_LIVE_BINDING


# --------------------------------------------------------------------------- #
# 5. --dry-run
# --------------------------------------------------------------------------- #
def test_dry_run_leaves_the_store_byte_identical_and_emits_nothing(profiles):
    _seed(_persona())
    before = _store_bytes()
    events_before = len(EventLog().tail(10_000))

    result = rebind_persona_profile("widget", profile="beta", dry_run=True)

    assert result["dry_run"] is True
    assert result["changed"] is True
    assert len(result["instances_moved"]) == 3
    assert _store_bytes() == before
    assert len(EventLog().tail(10_000)) == events_before
    assert AgentStore().get("widget").hermes_profile == "alpha"


def test_dry_run_still_refuses_an_invalid_target(profiles):
    _seed(_persona())

    with pytest.raises(PersonaProfileRebindError) as excinfo:
        rebind_persona_profile("widget", profile="nope", dry_run=True)

    assert excinfo.value.code == "profile_missing"


# --------------------------------------------------------------------------- #
# 6. typed event
# --------------------------------------------------------------------------- #
def test_rebind_emits_a_registered_event_naming_every_moved_row(profiles):
    from agent_runtime.decision_contract_registry import allowed_event_types, validate_event_payload

    assert REBIND_EVENT_TYPE in allowed_event_types()
    _seed(_persona())

    rebind_persona_profile("widget", profile="beta", actor="tester")

    events = [event for event in EventLog().tail(50) if event.type == REBIND_EVENT_TYPE]
    assert len(events) == 1
    payload = events[0].payload
    assert validate_event_payload(REBIND_EVENT_TYPE, payload) == ()
    assert payload["persona_id"] == "widget"
    assert payload["from_profile"] == "alpha"
    assert payload["to_profile"] == "beta"
    assert payload["actor"] == "tester"
    assert payload["instance_count"] == 3
    assert {item["persona_instance_id"] for item in payload["instances"]} == {
        "personainst_widget",
        "personainst_widget_agent_aaa",
        "personainst_widget_agent_bbb",
    }
    # The persona write still rides the sanctioned store path.
    assert any(event.type == "persona.updated" for event in EventLog().tail(50))


def test_rebind_event_stays_inside_the_payload_cap(profiles):
    from agent_runtime.events import EVENT_PAYLOAD_LIMIT_BYTES

    _seed(_persona(), placements=tuple(f"agent_{index:04d}" for index in range(120)))

    rebind_persona_profile("widget", profile="beta")

    event = next(event for event in EventLog().tail(50) if event.type == REBIND_EVENT_TYPE)
    assert event.payload["instance_count"] == 121
    assert event.payload["instances_truncated"] == 121 - len(event.payload["instances"])
    assert len(json.dumps(event.payload).encode("utf-8")) <= EVENT_PAYLOAD_LIMIT_BYTES


# --------------------------------------------------------------------------- #
# 7. preflight refusals
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "persona_id,profile,expected_code",
    [
        ("widget", "", "invalid_value"),
        ("widget", "definitely-missing", "profile_missing"),
        ("ghost", "beta", "persona_not_persisted"),
        ("profile:alpha", "beta", "persona_not_persisted"),
        ("", "beta", "persona_not_persisted"),
    ],
)
def test_preflight_refusals_are_typed(profiles, persona_id, profile, expected_code):
    _seed(_persona())

    with pytest.raises(PersonaProfileRebindError) as excinfo:
        rebind_persona_profile(persona_id, profile=profile)

    assert excinfo.value.code == expected_code
    assert AgentStore().get("widget").hermes_profile == "alpha"


def test_profile_not_ready_is_refused(profiles, monkeypatch):
    from agent_runtime import profile_context
    from agent_runtime.profile_context import PersonaProfileBinding

    _seed(_persona())

    def _not_ready(persona):
        if persona.hermes_profile == "beta":
            return PersonaProfileBinding(
                persona_id=persona.id,
                hermes_profile="beta",
                profile_home=None,
                readiness="missing_profile",
                summary="Hermes profile 'beta' does not exist",
            )
        return PersonaProfileBinding(
            persona_id=persona.id, hermes_profile=persona.hermes_profile, profile_home=None
        )

    monkeypatch.setattr(profile_context, "resolve_persona_profile", _not_ready)

    with pytest.raises(PersonaProfileRebindError) as excinfo:
        rebind_persona_profile("widget", profile="beta")

    assert excinfo.value.code == "profile_not_ready"
    assert excinfo.value.details["readiness"] == "missing_profile"


# --------------------------------------------------------------------------- #
# 8. consequence report
# --------------------------------------------------------------------------- #
def test_binding_files_resolve_against_the_new_profile_home(profiles):
    persona = _seed(_persona())

    result = rebind_persona_profile("widget", profile="beta", dry_run=True)
    files = result["binding_files"]

    assert files["hermes_profile"] == "beta"
    assert files["soul_overlay"]["path"].replace("\\", "/").endswith("profiles/beta/soul.md")
    assert files["soul_overlay"]["exists"] is True
    assert files["profile_memory"]["path"].replace("\\", "/").endswith("profiles/beta/memories/MEMORY.md")
    assert files["profile_memory"]["exists"] is True
    assert result["previous_binding_files"]["hermes_profile"] == "alpha"
    # Opt-in only: a persona that does not include core context files gets no
    # core_context block rather than a path the runtime will never load.
    assert "core_context" not in files
    assert binding_files(persona)["hermes_profile"] == "alpha"


#: ``_persona()`` carries a profile-ROOT ``soul.md`` overlay and opts into
#: profile memory, so its published profile-file set is exactly these two
#: destinations. Pinned as literal strings on purpose: this delta IS the
#: ``agent set-profile`` confirmation output, and the way it broke on 2026-07-25
#: was by silently going EMPTY when the publish grammar moved. A "something
#: appeared" assertion would not have caught that — `next(...)` raising
#: StopIteration was the only signal.
_ALPHA_PATHS = [
    "store/profile_files/alpha/memories/MEMORY.md",
    "store/profile_files/alpha/soul.md",
]
_BETA_PATHS = [
    "store/profile_files/beta/memories/MEMORY.md",
    "store/profile_files/beta/soul.md",
]


def test_realm_artifact_delta_reports_the_paths_that_move(profiles):
    _seed(_persona())
    realm = RealmStore().create(name="Test Realm")
    WorkspaceStore().create(name="Team", agent_ids=["widget"], realm_id=realm.id)

    preview = rebind_persona_profile("widget", profile="beta", dry_run=True)
    projected = preview["realm_artifact_delta"]
    assert projected["measured"] is False
    projected_realm = next(row for row in projected["realms"] if row["realm_id"] == realm.id)
    assert projected_realm["disappears"] == _ALPHA_PATHS
    assert projected_realm["appears"] == _BETA_PATHS

    applied = rebind_persona_profile("widget", profile="beta")["realm_artifact_delta"]
    assert applied["measured"] is True
    measured_realm = next(row for row in applied["realms"] if row["realm_id"] == realm.id)
    # The MEASURED apply must land on exactly what the operator confirmed.
    assert measured_realm["disappears"] == _ALPHA_PATHS
    assert measured_realm["appears"] == _BETA_PATHS


def test_projected_delta_drops_an_artifact_the_new_profile_cannot_back(profiles):
    """A memory file that does not exist under the new home does NOT reappear."""

    (profiles / "profiles" / "beta" / "memories" / "MEMORY.md").unlink()
    _seed(_persona())
    realm = RealmStore().create(name="Test Realm")
    WorkspaceStore().create(name="Team", agent_ids=["widget"], realm_id=realm.id)

    preview = rebind_persona_profile("widget", profile="beta", dry_run=True)
    row = next(item for item in preview["realm_artifact_delta"]["realms"] if item["realm_id"] == realm.id)

    assert row["disappears"] == _ALPHA_PATHS
    # Beta cannot back the memory file, so ONLY the soul overlay reappears.
    assert row["appears"] == ["store/profile_files/beta/soul.md"]
    assert "store/profile_files/beta/memories/MEMORY.md" not in row["appears"]


# --------------------------------------------------------------------------- #
# 9. the surfaces
# --------------------------------------------------------------------------- #
def test_agent_list_row_reports_the_agents_own_binding_not_the_active_profile(profiles):
    from hermes_cli.harness import _agent_definition_row

    persona = _persona(hermes_profile="beta")
    index = {
        "widget": resolve_effective_binding(
            "widget",
            config_profile="alpha",
            store_profile="beta",
            config_declared=True,
            store_row_present=True,
        )
    }

    row = _agent_definition_row(persona, source_profile="operator-active", bindings=index)

    # The bug: `profile` used to be filled with active_profile_name(), so every
    # row printed the operator's profile and never changed on a real rebind.
    assert row["profile"] == "beta"
    assert row["source_profile"] == "operator-active"
    assert row["config_profile"] == "alpha"
    assert row["store_profile"] == "beta"
    assert row["binding_diverged"] is True


def test_agent_list_row_without_a_binding_index_still_reports_the_binding(profiles):
    from hermes_cli.harness import _agent_definition_row

    row = _agent_definition_row(_persona(hermes_profile="beta"), source_profile="operator-active")

    assert row["profile"] == "beta"
    assert "binding_diverged" not in row


def test_doctor_surfaces_the_divergence_without_repairing_it(profiles, monkeypatch):
    from agent_runtime import harness_doctor

    _seed(_persona(hermes_profile="beta"))
    monkeypatch.setattr(
        "agent_runtime.config.persona_records_from_config",
        lambda cfg=None: [_persona(hermes_profile="alpha")],
    )

    report = harness_doctor._persona_binding_report()

    assert report["ok"] is True
    assert report["resolved_by"] == "store_wins"
    assert report["diverged_count"] == 1
    assert report["diverged"][0]["persona_id"] == "widget"
    assert report["diverged"][0]["config_profile"] == "alpha"
    assert report["diverged"][0]["store_profile"] == "beta"
    # Detect and label only — doctor must never silently pick a side.
    assert AgentStore().get("widget").hermes_profile == "beta"


def test_binding_index_covers_store_only_personas(profiles):
    _seed(_persona())

    index = binding_index()

    assert "widget" in index
    assert index["widget"].store_row_present is True
    assert index["widget"].effective_profile == "alpha"


def test_unreadable_assignment_store_fails_closed(profiles, monkeypatch):
    """A safety gate that cannot verify must refuse, not assume "nothing running"."""

    from agent_runtime.persona_assignments import PersonaAssignmentStore

    _seed(_persona())

    def _boom(self, **kwargs):
        raise OSError("assignment store unreadable")

    monkeypatch.setattr(PersonaAssignmentStore, "find_active", _boom)
    before = _store_bytes()

    with pytest.raises(PersonaProfileRebindError) as excinfo:
        rebind_persona_profile("widget", profile="beta")

    assert excinfo.value.code == "assignment_store_unreadable"
    assert _store_bytes() == before


def test_an_active_assignment_blocks_the_rebind(profiles, monkeypatch):
    from agent_runtime.persona_assignments import PersonaAssignmentStore

    _seed(_persona())

    class _Assignment:
        persona_instance_id = "personainst_widget_agent_bbb"

    monkeypatch.setattr(PersonaAssignmentStore, "find_active", lambda self, **kwargs: [_Assignment()])

    with pytest.raises(PersonaProfileRebindError) as excinfo:
        rebind_persona_profile("widget", profile="beta")

    assert excinfo.value.code == "instances_busy"
    assert excinfo.value.details["busy_instances"] == [
        {
            "persona_instance_id": "personainst_widget_agent_bbb",
            "reason": BUSY_ASSIGNMENT_IN_FLIGHT,
            "state": "idle",
        }
    ]


def test_a_no_op_rebind_writes_nothing_and_emits_nothing(profiles):
    """Already bound + nothing drifted: no store write, no watermark noise."""

    _seed(_persona(hermes_profile="beta"))
    before = _store_bytes()
    events_before = len(EventLog().tail(10_000))

    result = rebind_persona_profile("widget", profile="beta")

    assert result["ok"] is True
    assert result["changed"] is False
    assert result["persona_changed"] is False
    assert result["instances_moved"] == []
    assert _store_bytes() == before
    assert len(EventLog().tail(10_000)) == events_before


# --------------------------------------------------------------------------- #
# 10. partial-failure isolation
# --------------------------------------------------------------------------- #
def test_a_failing_row_is_isolated_accounted_and_still_emits_evidence(profiles, monkeypatch):
    """One unwritable row must not abort the cascade, vanish, or fake success.

    Aborting mid-loop would leave the persona authority already moved (it is
    written first) with an arbitrary suffix of rows stranded, no event, and a
    traceback instead of an envelope. The `_agent_*` placement rows have NO
    self-heal, so a stranded row stays stranded until someone retries.
    """

    from agent_runtime.persona_assignments import PersonaInstanceStore
    from agent_runtime.persona_profile_binding import STATUS_PARTIALLY_APPLIED

    _seed(_persona(), placements=("agent_aaa", "agent_bbb", "agent_ccc", "agent_ddd"))
    real = PersonaInstanceStore.set_backing_profile

    def _fail_on_bbb(self, persona_instance_id, profile_id):
        if persona_instance_id == "personainst_widget_agent_bbb":
            raise OSError("disk on fire")
        return real(self, persona_instance_id, profile_id)

    monkeypatch.setattr(PersonaInstanceStore, "set_backing_profile", _fail_on_bbb)

    result = rebind_persona_profile("widget", profile="beta")

    # The authority moved, and every row EXCEPT the broken one moved with it --
    # the failure did not abort the rows that came after it.
    assert AgentStore().get("widget").hermes_profile == "beta"
    bound = {item.id: item.profile_id for item in PersonaInstanceStore().list_all()}
    assert bound["personainst_widget"] == "beta"
    assert bound["personainst_widget_agent_aaa"] == "beta"
    assert bound["personainst_widget_agent_ccc"] == "beta"
    assert bound["personainst_widget_agent_ddd"] == "beta"
    assert bound["personainst_widget_agent_bbb"] == "alpha"

    # ...and the envelope reports a PARTIAL success, never a clean one.
    assert result["ok"] is False
    assert result["status"] == STATUS_PARTIALLY_APPLIED
    assert result["error_code"] == "cascade_partial_failure"
    assert [item["persona_instance_id"] for item in result["instances_failed"]] == [
        "personainst_widget_agent_bbb"
    ]
    assert "disk on fire" in result["instances_failed"][0]["reason"]
    assert "personainst_widget_agent_bbb" in result["error"]
    assert "STRANDED" in result["next_expected"]
    assert {row["persona_instance_id"] for row in result["instances_moved"]} == {
        "personainst_widget",
        "personainst_widget_agent_aaa",
        "personainst_widget_agent_ccc",
        "personainst_widget_agent_ddd",
    }

    # The evidence channel must survive the run that went wrong.
    event = next(item for item in EventLog().tail(50) if item.type == REBIND_EVENT_TYPE)
    assert event.payload["status"] == STATUS_PARTIALLY_APPLIED
    assert event.payload["instance_count"] == 4
    assert event.payload["failed_count"] == 1
    assert event.payload["failed"][0]["persona_instance_id"] == "personainst_widget_agent_bbb"
    assert {item["persona_instance_id"] for item in event.payload["instances"]} == {
        "personainst_widget",
        "personainst_widget_agent_aaa",
        "personainst_widget_agent_ccc",
        "personainst_widget_agent_ddd",
    }


def test_rerunning_after_a_partial_apply_repairs_only_the_stranded_row(profiles, monkeypatch):
    from agent_runtime.persona_assignments import PersonaInstanceStore

    _seed(_persona(), placements=("agent_aaa", "agent_bbb"))
    real = PersonaInstanceStore.set_backing_profile
    broken = {"on": True}

    def _fail_once(self, persona_instance_id, profile_id):
        if broken["on"] and persona_instance_id == "personainst_widget_agent_bbb":
            raise OSError("transient")
        return real(self, persona_instance_id, profile_id)

    monkeypatch.setattr(PersonaInstanceStore, "set_backing_profile", _fail_once)
    assert rebind_persona_profile("widget", profile="beta")["ok"] is False

    broken["on"] = False
    retry = rebind_persona_profile("widget", profile="beta")

    assert retry["ok"] is True
    assert retry["persona_changed"] is False
    assert [row["persona_instance_id"] for row in retry["instances_moved"]] == [
        "personainst_widget_agent_bbb"
    ]
    assert retry["instances_failed"] == []
    assert PersonaInstanceStore().get("personainst_widget_agent_bbb").profile_id == "beta"


def test_partial_failure_event_stays_inside_the_payload_cap(profiles, monkeypatch):
    from agent_runtime.events import EVENT_PAYLOAD_LIMIT_BYTES
    from agent_runtime.persona_assignments import PersonaInstanceStore

    _seed(_persona(), placements=tuple(f"agent_{index:04d}" for index in range(60)))
    monkeypatch.setattr(
        PersonaInstanceStore,
        "set_backing_profile",
        lambda self, persona_instance_id, profile_id: (_ for _ in ()).throw(OSError("x" * 500)),
    )

    result = rebind_persona_profile("widget", profile="beta")

    assert result["ok"] is False
    assert len(result["instances_failed"]) == 61
    event = next(item for item in EventLog().tail(50) if item.type == REBIND_EVENT_TYPE)
    assert event.payload["failed_count"] == 61
    assert event.payload["failed_truncated"] == 61 - len(event.payload["failed"])
    assert len(json.dumps(event.payload).encode("utf-8")) <= EVENT_PAYLOAD_LIMIT_BYTES
