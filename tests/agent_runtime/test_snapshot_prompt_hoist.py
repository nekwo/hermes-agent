"""S3 read-model — hoist duplicated skills catalogs + evict on-demand debug.

Covers operator moves 2 (store the skills catalog once, reference by hash) and
3 (fetch the heavy per-turn ``final_model_input`` on demand, not in every
frame). The frame-shrink is proven against the S1 audit seam (a tight
prompt_observability byte budget the hoisted section passes and the inline
section blows), and the win is meaning-preserving: resolving a row's refs
reproduces the inline lists byte-for-byte.
"""

from __future__ import annotations

import copy
import json

from agent_runtime import prompt_observability as po
from agent_runtime.snapshot_audit import snapshot_size_budget


def _skill(name: str, source: str = "persona_definition") -> dict:
    return {
        "name": name,
        "kind": "skill",
        "status": "accessible",
        "hash_tracked": False,
        "source": source,
    }


def _row(context_id: str, *, available: list, accessible: list, fmi: dict | None = None) -> dict:
    """A minimal chat_contexts row with the four inline skill fields (and the
    two byte-identical alias pairs) the pre-S3 frame carried."""

    return {
        "context_id": context_id,
        "available_skills": copy.deepcopy(available),
        "skills_catalog": copy.deepcopy(available),
        "accessible_skills": copy.deepcopy(accessible),
        "skills": copy.deepcopy(accessible),
        "final_model_input": copy.deepcopy(fmi) if fmi is not None else None,
    }


# --------------------------------------------------------------------------- #
# Skills catalog hoist (operator move 2).
# --------------------------------------------------------------------------- #
def test_hoist_replaces_inline_lists_with_refs():
    catalog = [_skill(n, source="installed_skill_catalog") for n in ("alpha", "beta", "gamma")]
    accessible = [_skill("alpha")]
    rows = [_row("ctx_a", available=catalog, accessible=accessible)]
    catalogs: dict = {}

    po._hoist_skills_catalogs(rows, catalogs)

    row = rows[0]
    # The four inline fields leave the row entirely.
    for field in po.HOISTED_SKILL_LIST_FIELDS:
        assert field not in row
    # Two refs replace them; each resolves through the table.
    assert row["available_skills_ref"] in catalogs
    assert row["accessible_skills_ref"] in catalogs
    assert catalogs[row["available_skills_ref"]] == catalog
    assert catalogs[row["accessible_skills_ref"]] == accessible


def test_skills_stored_once_across_rows():
    # The installed catalog is a GLOBAL — byte-identical on every row. Three rows
    # share it and share one accessible set → the table holds exactly two blobs,
    # not six.
    catalog = [_skill(n, source="installed_skill_catalog") for n in ("alpha", "beta")]
    accessible = [_skill("alpha")]
    rows = [_row(f"ctx_{i}", available=catalog, accessible=accessible) for i in range(3)]
    catalogs: dict = {}

    po._hoist_skills_catalogs(rows, catalogs)

    assert len(catalogs) == 2, "identical catalog + accessible set collapse to two entries"
    refs = {row["available_skills_ref"] for row in rows}
    assert len(refs) == 1, "the global catalog resolves to ONE ref on every row"
    # S1 census meaning: the catalog appears exactly once in the stored table.
    catalog_ref = next(iter(refs))
    assert list(catalogs).count(catalog_ref) == 1


def test_distinct_accessible_sets_get_distinct_refs():
    catalog = [_skill("alpha", source="installed_skill_catalog")]
    rows = [
        _row("ctx_a", available=catalog, accessible=[_skill("alpha")]),
        _row("ctx_b", available=catalog, accessible=[_skill("beta")]),
    ]
    catalogs: dict = {}

    po._hoist_skills_catalogs(rows, catalogs)

    assert rows[0]["accessible_skills_ref"] != rows[1]["accessible_skills_ref"]
    # available catalog shared; two distinct accessible sets → 3 entries.
    assert len(catalogs) == 3


def test_hoist_missing_list_carries_no_fake_ref():
    row = {"context_id": "ctx_x"}  # no skill lists at all
    catalogs: dict = {}
    po._hoist_skills_catalogs([row], catalogs)
    assert "available_skills_ref" not in row
    assert "accessible_skills_ref" not in row
    assert catalogs == {}


# --------------------------------------------------------------------------- #
# final_model_input eviction (operator move 3).
# --------------------------------------------------------------------------- #
def test_final_model_input_evicted_to_typed_stub():
    fmi = {
        "kind": "redaction_safe_final_model_input",
        "message_count": 3,
        "messages": [{"role": "user", "content": "hello", "bytes": 5}],
    }
    rows = [_row("ctx_a", available=[], accessible=[], fmi=fmi)]

    po._evict_final_model_input(rows)

    stub = rows[0]["final_model_input"]
    assert stub["evicted"] is True
    assert stub["message_count"] == 3
    assert stub["context_id"] == "ctx_a"
    assert stub["bytes"] == len(
        json.dumps(fmi, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    assert "final-model-input" in stub["fetch"]


def test_evict_is_idempotent_and_skips_absent():
    rows = [
        {"context_id": "ctx_none", "final_model_input": None},
        {"context_id": "ctx_stub", "final_model_input": {"evicted": True, "bytes": 10}},
    ]
    po._evict_final_model_input(rows)
    assert rows[0]["final_model_input"] is None  # nothing to evict
    assert rows[1]["final_model_input"] == {"evicted": True, "bytes": 10}  # already a stub


def test_final_model_input_fetchable_by_context(isolate_agent_runtime_root):
    # Archive-never-delete: the full payload stays on disk in the persisted row;
    # only the FRAME is stubbed. The on-demand read returns the full payload.
    fmi = {
        "kind": "redaction_safe_final_model_input",
        "message_count": 1,
        "messages": [{"role": "user", "content": "on disk", "bytes": 7}],
    }
    po.persist_prompt_observability_context(
        {"context_id": "ctx_disk", "final_model_input": fmi, "persona_id": "dev"}
    )

    fetched = po.load_final_model_input_for_context("ctx_disk")
    assert fetched == fmi
    # A stub on disk (should never happen) or a missing id resolves to None, not
    # a fake-empty payload.
    assert po.load_final_model_input_for_context("ctx_missing") is None


# --------------------------------------------------------------------------- #
# Integration through snapshot_prompt_observability + kill-switch parity.
# --------------------------------------------------------------------------- #
def _profile_instance(session_id: str):
    from agent_runtime.models import PersonaInstance, WorkerSessionState

    return PersonaInstance(
        id="personainst_profile_alice",
        persona_id="profile:alice",
        role="profile",
        display_name="Alice Agent",
        profile_id="alice",
        runtime_root=".",
        state=WorkerSessionState.IDLE,
        mode="chat",
        session_id=session_id,
    )


def _persist_alice_row(context_id: str = "ctx_alice"):
    po.persist_prompt_observability_context(
        {
            "context_id": context_id,
            "persona_id": "profile:alice",
            "persona_instance_id": "personainst_profile_alice",
            "profile": "alice",
            "session_id": "persona_chat_alice",
            "accessible_skills": [_skill("mission-lead")],
            "skills": [_skill("mission-lead")],
            "available_skills": [_skill("a", "installed_skill_catalog"), _skill("b", "installed_skill_catalog")],
            "skills_catalog": [_skill("a", "installed_skill_catalog"), _skill("b", "installed_skill_catalog")],
            "final_model_input": {
                "kind": "redaction_safe_final_model_input",
                "message_count": 2,
                "messages": [{"role": "system", "content": "x" * 400, "bytes": 400}],
            },
        }
    )


def test_snapshot_prompt_observability_hoists_by_default(isolate_agent_runtime_root):
    _persist_alice_row()
    section = po.snapshot_prompt_observability(
        personas=[],
        persona_instances=[_profile_instance("persona_chat_alice")],
    )
    row = next(r for r in section["chat_contexts"] if r["context_id"] == "ctx_alice")

    # Hoisted: no inline lists, two refs, one shared catalog table.
    for field in po.HOISTED_SKILL_LIST_FIELDS:
        assert field not in row
    assert row["available_skills_ref"] in section["skills_catalogs"]
    assert row["accessible_skills_ref"] in section["skills_catalogs"]
    # final_model_input evicted to a stub carrying its size.
    assert row["final_model_input"]["evicted"] is True
    assert row["final_model_input"]["bytes"] > 0
    # …but still fetchable on demand from disk.
    assert po.load_final_model_input_for_context("ctx_alice") is not None


def test_inline_kill_switch_restores_full_payloads(isolate_agent_runtime_root):
    _persist_alice_row()
    section = po.snapshot_prompt_observability(
        personas=[],
        persona_instances=[_profile_instance("persona_chat_alice")],
        inline_payloads=True,
    )
    row = next(r for r in section["chat_contexts"] if r["context_id"] == "ctx_alice")

    assert section["skills_catalogs"] == {}
    assert "available_skills_ref" not in row
    assert isinstance(row["available_skills"], list)
    assert isinstance(row["accessible_skills"], list)
    # final_model_input is the full payload, not a stub.
    assert row["final_model_input"].get("evicted") is not True
    assert row["final_model_input"]["message_count"] == 2


def test_hoisted_refs_resolve_to_the_inline_lists(isolate_agent_runtime_root):
    # A/B parity: the hoisted frame carries the SAME skill content the inline
    # frame did — resolving the refs reproduces the inline lists byte-for-byte.
    _persist_alice_row()
    instances = [_profile_instance("persona_chat_alice")]
    inline = po.snapshot_prompt_observability(
        personas=[], persona_instances=instances, inline_payloads=True
    )
    hoisted = po.snapshot_prompt_observability(
        personas=[], persona_instances=instances, inline_payloads=False
    )
    inline_row = next(r for r in inline["chat_contexts"] if r["context_id"] == "ctx_alice")
    hoisted_row = next(r for r in hoisted["chat_contexts"] if r["context_id"] == "ctx_alice")
    catalogs = hoisted["skills_catalogs"]

    assert catalogs[hoisted_row["available_skills_ref"]] == inline_row["available_skills"]
    assert catalogs[hoisted_row["accessible_skills_ref"]] == inline_row["accessible_skills"]
    # The alias fields were pure aliases — recoverable from the same two refs.
    assert catalogs[hoisted_row["available_skills_ref"]] == inline_row["skills_catalog"]
    assert catalogs[hoisted_row["accessible_skills_ref"]] == inline_row["skills"]


# --------------------------------------------------------------------------- #
# Size-budget ratchet (S1 seam, tightened via the budgets parameter — NOT by
# editing snapshot_audit.py).
# --------------------------------------------------------------------------- #
def _bulky_section(inline_payloads: bool) -> dict:
    """A prompt_observability section with a fat catalog repeated across many
    rows, built inline or hoisted, for the byte-budget comparison."""

    catalog = [_skill(f"skill-{i}", "installed_skill_catalog") for i in range(80)]
    accessible = [_skill("mission-lead")]
    rows = [_row(f"ctx_{i}", available=catalog, accessible=accessible) for i in range(24)]
    catalogs: dict = {}
    if not inline_payloads:
        po._hoist_skills_catalogs(rows, catalogs)
    return {"schema_version": 1, "chat_contexts": rows, "skills_catalogs": catalogs}


def test_prompt_observability_budget_ratchet():
    inline = {"prompt_observability": _bulky_section(inline_payloads=True)}
    hoisted = {"prompt_observability": _bulky_section(inline_payloads=False)}
    # A ratchet tight enough that the 24-row inline catalog blows it, but the
    # hoisted single-catalog section clears it comfortably. This is the S1 seam
    # tightened via its budgets parameter, proving the shrink is real.
    ratchet = {"prompt_observability": 40 * 1024}

    assert snapshot_size_budget(hoisted, ratchet) == []
    inline_violations = snapshot_size_budget(inline, ratchet)
    assert inline_violations, "the inline 24-row catalog must blow the ratchet"
    assert "prompt_observability" in inline_violations[0]
