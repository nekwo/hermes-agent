from types import SimpleNamespace

from agent_runtime.prompt_observability import (
    MAX_WORKSPACE_AGENTS_BYTES,
    _attach_context_file_prompt_contributions,
    _attach_skills_prompt_contribution,
    _backfill_derived_fields,
    _context_file_summary,
    _layer_text_size,
    _mission_chat_identity_prompt_chars,
    _mission_chat_operative_rules_chars,
    _set_row_prompt_contribution,
    _workspace_agents_prompt_chars,
    attach_prompt_observability_turn_results,
    load_workspace_agents_context,
    mission_chat_prompt_observability,
    snapshot_prompt_observability,
)


def test_prompt_observability_preserves_profile_persona_identity():
    context = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="profile:alice",
            hermes_profile="alice",
            display_name="Alice Agent",
            role="profile",
        ),
        persona_instance_id="personainst_profile_alice",
        session_id="persona_chat_alice",
    )

    assert context["persona_id"] == "profile:alice"
    assert context["profile"] == "alice"


def test_prompt_observability_reports_typed_persona_envelope_and_memory_flag():
    # Runtime identity, the optional profile SOUL, and operator-channel rules
    # are distinct layers. Memory independently tracks include_profile_memory.
    no_memory = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="neko_supervisor",
            hermes_profile="neko",
            display_name="Neko Mission Lead",
            role="alice_supervisor",
            include_profile_memory=False,
        ),
        session_id="persona_chat_neko",
    )
    layers = {layer["kind"]: layer for layer in no_memory["prompt_layers"]}
    assert no_memory["prompt_stack_schema_version"] == 2
    assert "persona_identity" not in layers
    assert "Neko Mission Lead" in layers["runtime_identity"]["summary"]
    assert "You are Neko Mission Lead" in layers["runtime_identity"]["content"]
    assert layers["runtime_identity"]["owner"] == "mission_control"
    assert layers["runtime_identity"]["group"] == "mission_control_persona"
    assert layers["profile_soul"]["owner"] == "profile"
    assert layers["operator_channel_rules"]["owner"] == "mission_control"
    assert "Mission Control operator-chat rules" in layers["operator_channel_rules"]["content"]
    assert layers["profile_context"]["status"] == "skipped"
    assert no_memory["prompt_flags"]["skip_memory"] is True
    assert no_memory["prompt_flags"]["load_soul_identity"] is False

    with_memory = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="neko_supervisor",
            hermes_profile="neko",
            display_name="Neko Mission Lead",
            role="alice_supervisor",
            include_profile_memory=True,
        ),
        session_id="persona_chat_neko",
    )
    memory_layer = {layer["kind"]: layer for layer in with_memory["prompt_layers"]}[
        "profile_context"
    ]
    assert memory_layer["status"] == "loaded"
    assert memory_layer["included"] is True
    assert with_memory["prompt_flags"]["skip_memory"] is False


def test_safe_final_model_input_preserves_prompt_lines_and_section_receipts():
    from agent_runtime.prompt_observability import _safe_final_model_input

    safe = _safe_final_model_input(
        {
            "messages": [
                {
                    "role": "system",
                    "content": "stable line\ncontext line\npassword: should-not-leak",
                }
            ],
            "system_prompt_sections": [
                {
                    "kind": "stable",
                    "name": "Stable Hermes foundation",
                    "start_char": 0,
                    "end_char": 11,
                    "chars": 11,
                    "truncated": False,
                }
            ],
        }
    )

    assert safe is not None
    assert "stable line\ncontext line" in safe["messages"][0]["content"]
    assert "should-not-leak" not in safe["messages"][0]["content"]
    assert safe["system_prompt_sections"] == [
        {
            "kind": "stable",
            "name": "Stable Hermes foundation",
            "start_char": 0,
            "end_char": 11,
            "chars": 11,
            "truncated": False,
        }
    ]

def test_prompt_layers_situational_hud_reports_operator_turn_placement():
    # T5 observability coherence: after the HUD moved out of the system prompt,
    # the frame carries a `situational_hud` prompt layer whose status/summary
    # reflect the NEW placement — injected on the operator's user turn, not the
    # system prompt. The top-level `situational_hud` block still rides for the
    # launcher's HUD peek. The layer carries no per-layer token estimate (its
    # bytes are counted via the user-turn message; a per-layer estimate would
    # double-count).
    persona = SimpleNamespace(
        id="neko_supervisor",
        hermes_profile="neko",
        display_name="Neko Mission Lead",
        role="alice_supervisor",
    )
    hud = {"preview": True, "scope": {"realm": "default", "workspace": "alpha"}}
    with_hud = mission_chat_prompt_observability(
        persona=persona, session_id="persona_chat_neko", situational_hud=hud
    )
    layers = {layer["kind"]: layer for layer in with_hud["prompt_layers"]}
    assert "situational_hud" in layers
    hud_layer = layers["situational_hud"]
    assert hud_layer["status"] == "loaded"
    assert "user turn" in hud_layer["summary"]
    assert hud_layer["injection_location"] == "user_turn"
    assert hud_layer["included"] is True
    assert "token_estimate" not in hud_layer
    # The block still carries the full dict for the launcher's HUD peek.
    assert with_hud["situational_hud"] == hud

    without_hud = mission_chat_prompt_observability(
        persona=persona, session_id="persona_chat_neko"
    )
    empty_layer = {layer["kind"]: layer for layer in without_hud["prompt_layers"]}["situational_hud"]
    assert empty_layer["status"] == "empty"
    assert empty_layer["included"] is False


def test_prompt_observability_names_live_task_bound_chat_without_session_row():
    context = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="dev",
            hermes_profile="dev",
            display_name="Launcher Dev",
            role="dev",
        ),
        persona_instance_id="personainst_dev",
        session_id="persona_chat_personainst_dev_live",
        task_id="task_live",
        session_db=None,
    )

    assert context["chat_id"] == "persona_chat_personainst_dev_live"
    assert context["chat_title"] == "Mission run"
    assert context["chat"]["source"] == "task_bound"


def test_workspace_agents_context_is_loaded_and_reported_from_selected_file(tmp_path):
    agents_file = tmp_path / "AGENTS.md"
    agents_file.write_text("# Workspace rules\nKeep this workspace isolated.\n", encoding="utf-8")

    workspace_agents = load_workspace_agents_context(str(agents_file))
    assert workspace_agents is not None
    assert workspace_agents.content.startswith("# Workspace rules")
    assert workspace_agents.receipt["included"] is True
    assert workspace_agents.receipt["status"] == "loaded"

    context = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="dev",
            hermes_profile="dev",
            display_name="Launcher Dev",
            role="dev",
        ),
        workspace_id="ws_launcher",
        workspace_name="Launcher",
        workspace_agents=workspace_agents,
    )

    receipt = next(item for item in context["context_files"] if item["name"] == "AGENTS.md")
    assert context["workspace_id"] == "ws_launcher"
    assert context["workspace_name"] == "Launcher"
    assert receipt["path"] == str(agents_file.resolve())
    assert receipt["sha256"]
    workspace_layer = next(
        layer for layer in context["prompt_layers"] if layer["kind"] == "workspace_context"
    )
    assert workspace_layer["source_path"] == str(agents_file.resolve())
    assert workspace_layer["source_sha256"] == receipt["sha256"]
    # Per-item token attribution: the workspace receipt carries bytes // 4.
    assert receipt["token_estimate"] == receipt["bytes"] // 4
    layer_kinds = [layer["kind"] for layer in context["prompt_layers"]]
    assert layer_kinds.index("workspace_context") < layer_kinds.index("surface")


def test_context_file_summary_carries_bytes_over_four_token_estimate(tmp_path):
    soul = tmp_path / "SOUL.md"
    soul.write_bytes(b"x" * 2867)  # today's live SOUL.md size

    summary = _context_file_summary(soul, included=True)

    assert summary["bytes"] == 2867
    # bytes // 4 — an integer-floor estimate, presented as ~N by the launcher.
    assert summary["token_estimate"] == 2867 // 4 == 716
    assert summary["token_estimate"] == summary["bytes"] // 4


def test_context_file_summary_absent_file_has_no_token_estimate(tmp_path):
    missing = tmp_path / "USER.md"  # never created

    summary = _context_file_summary(missing, included=False)

    # Absent file → no bytes, no fabricated token estimate (never a crash).
    assert summary["included"] is False
    assert "bytes" not in summary
    assert "token_estimate" not in summary


def test_layer_text_size_helper_relationship():
    # A present layer text → chars + chars // 4; an empty (blank) layer is a real
    # 0, distinct from an absent layer (no text seam) which degrades to {}.
    assert _layer_text_size("abcdefgh") == {"chars": 8, "token_estimate": 2}
    assert _layer_text_size("") == {"chars": 0, "token_estimate": 0}
    assert _layer_text_size(None) == {}


def test_prompt_layers_surface_carries_chars_and_token_estimate():
    surface_text = "Keep answers terse and cite the runbook."
    context = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="dev",
            hermes_profile="dev",
            display_name="Launcher Dev",
            role="dev",
        ),
        surface_prompt=surface_text,
    )
    layers = {layer["kind"]: layer for layer in context["prompt_layers"]}
    surface = layers["surface"]
    assert surface["chars"] == len(surface_text)
    assert surface["token_estimate"] == len(surface_text) // 4


def test_prompt_layers_blank_surface_is_zero_not_absent():
    context = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="dev",
            hermes_profile="dev",
            display_name="Launcher Dev",
            role="dev",
        ),
        surface_prompt="",
    )
    surface = {layer["kind"]: layer for layer in context["prompt_layers"]}["surface"]
    # Blank surface is present-but-empty: an explicit 0, never a missing key.
    assert surface["chars"] == 0
    assert surface["token_estimate"] == 0


def test_prompt_layers_without_available_text_degrade_to_no_estimate():
    context = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="dev",
            hermes_profile="dev",
            display_name="Launcher Dev",
            role="dev",
        ),
    )
    layers = {layer["kind"]: layer for layer in context["prompt_layers"]}
    # system_core / profile_context text is assembled later in the turn (or
    # attributed via context files) — no fabricated per-layer number.
    for kind in ("system_core", "profile_context"):
        assert kind in layers
        assert "token_estimate" not in layers[kind]
        assert "chars" not in layers[kind]
    # Runtime identity and channel rules are independently measurable at the
    # surface-message assembly seam.
    for kind in ("runtime_identity", "operator_channel_rules"):
        assert layers[kind]["token_estimate"] is not None
        assert layers[kind]["chars"] > 0


def test_prompt_layer_order_reflects_actual_assembly_order():
    context = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="neko_supervisor",
            hermes_profile="neko",
            display_name="Neko Mission Lead",
            role="alice_supervisor",
        ),
    )
    layers = context["prompt_layers"]
    assert [layer["order"] for layer in layers] == sorted(
        layer["order"] for layer in layers
    )
    assert [layer["kind"] for layer in layers] == [
        "system_core",
        "runtime_identity",
        "profile_soul",
        "operator_channel_rules",
        "surface",
        "profile_context",
        "conversation",
        "situational_hud",
    ]
    assert layers[0]["injection_location"] == "system_stable"
    assert layers[-1]["injection_location"] == "user_turn"


def test_workspace_agents_context_refuses_oversized_file_without_blocking_receipt(tmp_path):
    agents_file = tmp_path / "AGENTS.md"
    agents_file.write_bytes(b"x" * (MAX_WORKSPACE_AGENTS_BYTES + 1))

    workspace_agents = load_workspace_agents_context(str(agents_file))

    assert workspace_agents is not None
    assert workspace_agents.content is None
    assert workspace_agents.receipt["included"] is False
    assert workspace_agents.receipt["status"] == "too_large"


def test_accessible_skills_hash_check_uses_persona_profile_home(monkeypatch, tmp_path):
    # Regression: skill hash/missing checks in the HUD must run against the persona's
    # OWN profile home (mirroring profile_readiness), not the active HERMES_HOME.
    # Without the home an isolated persona (e.g. base) shows a false hash_mismatch in
    # Mission Control while `harness status` reports the skill clean.
    from agent_runtime import prompt_observability as po
    import agent_runtime.skill_install as skill_install
    import agent_runtime.profile_context as profile_context

    home = tmp_path / "base_home"
    captured = {}

    monkeypatch.setattr(
        profile_context,
        "resolve_persona_profile",
        lambda _persona: SimpleNamespace(profile_home=home, hermes_profile="base", readiness="ready"),
    )

    def fake_mismatches(_names, *, hermes_home=None):
        captured["hermes_home"] = hermes_home
        return []

    monkeypatch.setattr(skill_install, "harness_skill_hash_mismatches", fake_mismatches)

    po._accessible_skills_context(
        SimpleNamespace(id="base", hermes_profile="base", skills=["harness-runtime-model"]),
        "base",
    )

    assert captured["hermes_home"] == home


def test_mission_chat_prompt_observability_omits_retired_mission_hud_key():
    context = mission_chat_prompt_observability(
        persona=SimpleNamespace(id="dev", hermes_profile="dev", display_name="Dev", role="dev"),
        persona_instance_id="personainst_dev",
        task_id="task_live",
    )
    assert "mission_hud" not in context


def test_mission_chat_prompt_observability_has_no_empty_mission_hud_placeholder():
    context = mission_chat_prompt_observability(
        persona=SimpleNamespace(id="dev", hermes_profile="dev", display_name="Dev", role="dev"),
    )
    # Fresh rows no longer manufacture an always-empty compatibility field.
    # Historical persisted rows remain readable by context id in the Launcher.
    assert "mission_hud" not in context
    from agent_runtime.decision_contract_registry import contract_hash

    assert context["prompt_contract_hash"] == contract_hash()
    assert context["skill_manifest_hash"]


def test_accessible_skill_receipt_preserves_instance_policy_and_load_state(
    monkeypatch, tmp_path
):
    import agent.skill_utils as skill_utils
    from agent_runtime import prompt_observability as po

    shared = tmp_path / "shared"
    manifest = shared / "instance-skill" / "SKILL.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        "---\nname: instance-skill\nmetadata:\n  hermes:\n"
        "    surfaces: [mission_chat]\n    modes: [standard]\n"
        "    load_policy: recommended\n---\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(skill_utils, "get_shared_skills_dir", lambda: shared)
    monkeypatch.setattr(skill_utils, "get_all_skills_dirs", lambda: [shared])

    rows = po._accessible_skills_context(
        SimpleNamespace(id="dev", hermes_profile="dev", skills=["instance-skill"]),
        "dev",
        loaded_skill_names={"instance-skill"},
        instance_override_names={"instance-skill"},
    )

    assert rows[0]["assignment_policy"] == "instance_override"
    assert rows[0]["assignment_source"] == "persona_instance"
    assert rows[0]["load_state"] == "loaded_this_turn"
    assert rows[0]["hash_tracked"] is True


def test_unresolved_used_skill_never_claims_hash_tracking():
    from agent_runtime.prompt_observability import _resolved_skill_receipt

    receipt = _resolved_skill_receipt("definitely-not-installed-skill")
    assert receipt["hash_tracked"] is False
    assert receipt["content_hash"] is None


def test_snapshot_omits_mission_hud_even_for_a_bound_task(monkeypatch):
    """Retargeted in S19 (was ``..._previews_the_mission_hud_for_a_bound_task``).

    The preview producer (``context_builder.mission_hud_preview``) is removed,
    and S39 retires its writerless always-empty compatibility field. Historical
    persisted rows remain fetchable by context id through the Launcher reader.
    """

    from agent_runtime import prompt_observability as po

    monkeypatch.setattr(po, "load_latest_prompt_observability_contexts", lambda: [])
    persona = SimpleNamespace(id="dev", hermes_profile="dev", display_name="Dev", role="dev")
    instance = SimpleNamespace(
        id="personainst_dev",
        persona_id="dev",
        session_id="persona_chat_dev",
        current_task_id="task_live",
        goal_id="goal_1",
    )
    snapshot = snapshot_prompt_observability(
        personas=[persona],
        persona_instances=[instance],
    )

    contexts = snapshot["chat_contexts"]
    assert len(contexts) == 1
    assert "mission_hud" not in contexts[0]


def test_snapshot_omits_mission_hud_for_unbound_instance(monkeypatch):
    from agent_runtime import prompt_observability as po

    monkeypatch.setattr(po, "load_latest_prompt_observability_contexts", lambda: [])
    persona = SimpleNamespace(id="dev", hermes_profile="dev", display_name="Dev", role="dev")
    instance = SimpleNamespace(id="personainst_dev", persona_id="dev", session_id="s")
    snapshot = snapshot_prompt_observability(
        personas=[persona],
        persona_instances=[instance],
    )
    assert "mission_hud" not in snapshot["chat_contexts"][0]


def test_snapshot_empty_roster_omits_compiled_flow_and_persona_context(monkeypatch):
    from agent_runtime import prompt_observability as po

    monkeypatch.setattr(po, "load_latest_prompt_observability_contexts", lambda: [])
    snapshot = snapshot_prompt_observability(
        personas=[
            SimpleNamespace(
                id="custom_research_lead",
                hermes_profile="research",
                display_name="Research Lead",
                role="research_coordinator",
            )
        ],
        persona_instances=[],
    )

    assert snapshot["chat_contexts"] == []
    assert "default_flow" not in snapshot


def test_snapshot_includes_situational_hud_for_instance(monkeypatch):
    from agent_runtime import prompt_observability as po

    monkeypatch.setattr(po, "load_latest_prompt_observability_contexts", lambda: [])
    persona = SimpleNamespace(
        id="neko_supervisor", hermes_profile="neko", display_name="Neko Mission Lead", role="supervisor"
    )
    instance = SimpleNamespace(
        id="personainst_neko",
        persona_id="neko_supervisor",
        session_id="s",
        current_task_id=None,
        goal_id=None,
        role="supervisor",
        display_name="Neko Mission Lead",
        state="idle",
        mode="configured",
    )
    snapshot = snapshot_prompt_observability(
        personas=[persona],
        persona_instances=[instance],
        daemon=None,
        realm="default",
        workspace="default",
    )
    situational = snapshot["chat_contexts"][0]["situational_hud"]
    assert situational["scope"] == {"realm": "default", "workspace": "default"}
    assert situational["lane"]["persona_instance_id"] == "personainst_neko"


def test_snapshot_situational_hud_without_daemon_scope_still_carries_lane(monkeypatch):
    from agent_runtime import prompt_observability as po

    monkeypatch.setattr(po, "load_latest_prompt_observability_contexts", lambda: [])
    persona = SimpleNamespace(id="dev", hermes_profile="dev", display_name="Dev", role="dev")
    instance = SimpleNamespace(
        id="personainst_dev",
        persona_id="dev",
        session_id="s",
        current_task_id=None,
        goal_id=None,
        role="dev",
        display_name="Dev",
        state="idle",
        mode="configured",
    )
    snapshot = snapshot_prompt_observability(
        personas=[persona], persona_instances=[instance]
    )
    situational = snapshot["chat_contexts"][0]["situational_hud"]
    # No daemon/scope threaded → those sub-blocks are absent, but the lane
    # identity (and the always-present key) still hold a stable shape.
    assert "runtime" not in situational
    assert situational["lane"]["persona_instance_id"] == "personainst_dev"


def test_backfill_copies_situational_hud_onto_persisted_row():
    persisted = {"persona_instance_id": "x", "session_id": "s", "persona_id": "p"}
    built = {"situational_hud": {"preview": True, "lane": {"persona_instance_id": "x"}}}
    _backfill_derived_fields(persisted, built)
    assert persisted["situational_hud"] == {"preview": True, "lane": {"persona_instance_id": "x"}}


def test_chat_time_row_carries_situational_hud_verbatim():
    # Record-at-injection: the chat turn passes the very dict it rendered into
    # the fed block, so the persisted row IS what the model received — the
    # CONTEXT peek must never depend on the snapshot backfill's key match
    # (persona_instance_id, session_id, persona_id), which misses whenever the
    # console chat session differs from the instance's session field.
    injected = {
        "preview": True,
        "lane": {"persona_instance_id": "personainst_neko"},
        "steering": {"steered_by": [], "steers": [{"persona_instance_id": "personainst_dev", "display_name": "Dev"}]},
    }
    row = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="neko_supervisor",
            hermes_profile="neko",
            display_name="Neko Mission Lead",
            role="supervisor",
        ),
        persona_instance_id="personainst_neko",
        session_id="console_chat_session",
        situational_hud=injected,
    )
    assert row["situational_hud"] == injected


def test_backfill_does_not_add_fresh_mission_hud_to_persisted_row():
    persisted = {"persona_instance_id": "x", "session_id": "s", "persona_id": "p"}
    built = {"mission_hud": {"preview": True, "phase": "in_progress"}}
    _backfill_derived_fields(persisted, built)
    assert "mission_hud" not in persisted


def test_backfill_does_not_overwrite_an_existing_persisted_hud():
    persisted = {"mission_hud": {"real": True}}
    built = {"mission_hud": {"preview": True}}
    _backfill_derived_fields(persisted, built)
    assert persisted["mission_hud"] == {"real": True}


# --------------------------------------------------------------------------- #
# T8 (2026-07-18): per-file IN-PROMPT contribution + persona-identity template
# layer estimate. The loaded-file `token_estimate` stays; these additive
# `prompt_chars` / `prompt_token_estimate` fields answer "how much of the file
# actually landed in the prompt", omitted (never guessed) where unreachable.
# --------------------------------------------------------------------------- #


def test_set_row_prompt_contribution_semantics():
    # A real count sets both fields; a deliberate 0 is present-and-zero; None /
    # negative leave the row untouched (absent -> launcher omits the chip).
    row = {}
    _set_row_prompt_contribution(row, 2823)
    assert row == {"prompt_chars": 2823, "prompt_token_estimate": 705}
    zero = {}
    _set_row_prompt_contribution(zero, 0)
    assert zero == {"prompt_chars": 0, "prompt_token_estimate": 0}
    absent = {}
    _set_row_prompt_contribution(absent, None)
    _set_row_prompt_contribution(absent, -5)
    assert absent == {}


def test_attach_context_file_prompt_contributions_reachable_only():
    # SOUL.md gets the soul chars; the workspace row gets the workspace part
    # chars; config.yaml is a deliberate 0; MEMORY.md / USER.md /
    # .skills_prompt_snapshot.json are left untouched (unreachable at this seam).
    files = [
        {"name": "SOUL.md", "kind": "soul", "included": True, "token_estimate": 710},
        {"name": "MEMORY.md", "kind": "memory", "included": True},
        {"name": "USER.md", "kind": "user_memory", "included": True},
        {"name": ".skills_prompt_snapshot.json", "kind": "skills", "included": True},
        {"name": "config.yaml", "kind": "profile_config", "included": True},
        {"name": "AGENTS.md", "kind": "workspace_context", "included": True},
    ]
    _attach_context_file_prompt_contributions(files, soul_chars=2823, workspace_chars=1694)
    by_name = {f["name"]: f for f in files}
    # SOUL.md: soul overlay chars (distinct from the loaded-file estimate 710).
    assert by_name["SOUL.md"]["prompt_chars"] == 2823
    assert by_name["SOUL.md"]["prompt_token_estimate"] == 705
    assert by_name["SOUL.md"]["token_estimate"] == 710  # loaded-file field kept
    assert by_name["SOUL.md"]["prompt_included"] is True
    assert by_name["SOUL.md"]["prompt_status"] == "injected"
    # Workspace part (preamble + body).
    assert by_name["AGENTS.md"]["prompt_chars"] == 1694
    assert by_name["AGENTS.md"]["prompt_token_estimate"] == 423
    assert by_name["AGENTS.md"]["prompt_status"] == "injected"
    # config.yaml: consumed as configuration, never pasted -> a deliberate 0.
    assert by_name["config.yaml"]["prompt_token_estimate"] == 0
    assert by_name["config.yaml"]["prompt_included"] is False
    assert by_name["config.yaml"]["prompt_status"] == "observed_only"
    # Unreachable rows are untouched -- the launcher keeps their loaded-file chip.
    for name in ("MEMORY.md", "USER.md", ".skills_prompt_snapshot.json"):
        assert "prompt_chars" not in by_name[name]
        assert "prompt_token_estimate" not in by_name[name]


def test_attach_context_file_contributions_none_soul_omits_field():
    files = [{"name": "SOUL.md", "kind": "soul", "included": True}]
    _attach_context_file_prompt_contributions(files, soul_chars=None, workspace_chars=None)
    assert "prompt_chars" not in files[0]
    assert "prompt_token_estimate" not in files[0]
    assert files[0]["prompt_included"] is False
    assert files[0]["prompt_status"] == "not_injected"


def test_workspace_agents_prompt_chars_is_preamble_plus_body(tmp_path):
    from agent_runtime.persona_runtime import MISSION_CHAT_WORKSPACE_AGENTS_PREAMBLE

    agents_file = tmp_path / "AGENTS.md"
    body = "# Workspace rules\nKeep this workspace isolated.\n"
    agents_file.write_text(body, encoding="utf-8")
    workspace_agents = load_workspace_agents_context(str(agents_file))
    chars = _workspace_agents_prompt_chars(workspace_agents)
    # The pasted part is the fixed preamble + the STRIPPED loaded body (matching
    # _mission_chat_surface_message, which strips the content), never the raw
    # file bytes. Reconcile against the LOADED content (CRLF-safe) rather than
    # the source string.
    assert chars == len(MISSION_CHAT_WORKSPACE_AGENTS_PREAMBLE) + len(
        workspace_agents.content.strip()
    )
    assert _workspace_agents_prompt_chars(None) is None


def test_config_yaml_row_carries_deliberate_zero_in_prompt():
    context = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="dev", hermes_profile="dev", display_name="Launcher Dev", role="dev"
        ),
    )
    config = next(f for f in context["context_files"] if f["name"] == "config.yaml")
    # A deliberate zero -- config.yaml is consumed as configuration, never pasted.
    assert config["prompt_token_estimate"] == 0
    assert config["prompt_chars"] == 0


def test_persona_envelope_layers_carry_separate_estimates():
    persona = SimpleNamespace(
        id="dev", hermes_profile="dev", display_name="Launcher Dev", role="dev"
    )
    context = mission_chat_prompt_observability(persona=persona)
    layers = {layer["kind"]: layer for layer in context["prompt_layers"]}
    identity_chars = _mission_chat_identity_prompt_chars(persona)
    rules_chars = _mission_chat_operative_rules_chars()
    assert identity_chars is not None and identity_chars > 0
    assert rules_chars is not None and rules_chars > 0
    assert layers["runtime_identity"]["chars"] == identity_chars
    assert layers["runtime_identity"]["token_estimate"] == identity_chars // 4
    assert layers["operator_channel_rules"]["chars"] == rules_chars
    assert layers["operator_channel_rules"]["token_estimate"] == rules_chars // 4


def test_persona_section_reconciles_identity_rules_and_soul_no_overlap():
    # Runtime identity + SOUL context-file attribution + operator rules must
    # reconcile to the surface-message persona section with no overlap.
    from agent_runtime import persona_runtime as PR
    from agent_runtime.models import AgentPersona

    persona = AgentPersona(
        id="neko_supervisor",
        display_name="Neko Mission Lead",
        role="alice_supervisor",
        model="",
        provider="",
        api_mode="",
        toolsets=[],
        system_prompt_path=None,
    )
    identity = PR._mission_chat_identity_prompt(persona)
    rules = PR._mission_chat_operative_rules()
    template_chars = len(identity) + len(rules)

    # With a soul overlay pasted, the surface message == template + soul + the
    # two joiners (identity . soul . rules). No memory in this lane's surface
    # message; a persona with populated MEMORY.md/USER.md attributes that via
    # those rows (Hermes-core stack), never this template layer.
    soul_text = "SOUL LINE\n" * 40
    surface = f"{identity}\n\n{soul_text.strip()}\n\n{rules}"
    assert len(surface) == template_chars + len(soul_text.strip()) + 4


def test_skills_row_attached_from_final_model_input_post_turn():
    context = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="dev", hermes_profile="dev", display_name="Launcher Dev", role="dev"
        ),
    )
    skills = next(
        f for f in context["context_files"] if f["name"] == ".skills_prompt_snapshot.json"
    )
    # Pre-turn: the render needs the constructed agent, so no in-prompt number.
    assert "prompt_chars" not in skills
    # Post-turn: profile_runner recorded the rendered index chars (measured
    # against the agent's real tool set) -- attach them to the snapshot row.
    _attach_skills_prompt_contribution(
        context, {"messages": [], "skills_prompt_chars": 8989}
    )
    skills = next(
        f for f in context["context_files"] if f["name"] == ".skills_prompt_snapshot.json"
    )
    assert skills["prompt_chars"] == 8989
    assert skills["prompt_token_estimate"] == 2247  # ~4.4x under the loaded 41 KB
    assert skills["prompt_included"] is True
    assert skills["prompt_status"] == "injected"


def test_skills_row_untouched_when_final_model_input_lacks_chars():
    context = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="dev", hermes_profile="dev", display_name="Launcher Dev", role="dev"
        ),
    )
    # No skills_prompt_chars (old build / failure lane) -> row stays loaded-file.
    _attach_skills_prompt_contribution(context, {"messages": []})
    _attach_skills_prompt_contribution(context, None)
    skills = next(
        f for f in context["context_files"] if f["name"] == ".skills_prompt_snapshot.json"
    )
    assert "prompt_chars" not in skills
    assert "prompt_token_estimate" not in skills


def test_attach_turn_results_patches_skills_row_end_to_end():
    context = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="dev", hermes_profile="dev", display_name="Launcher Dev", role="dev"
        ),
    )
    attach_prompt_observability_turn_results(
        context,
        final_model_input={
            "messages": [{"role": "system", "content": "x"}],
            "skills_prompt_chars": 9000,
        },
    )
    skills = next(
        f for f in context["context_files"] if f["name"] == ".skills_prompt_snapshot.json"
    )
    assert skills["prompt_token_estimate"] == 2250
