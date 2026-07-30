"""S3 read-model — hoist duplicated skills catalogs + evict on-demand debug.

Covers operator moves 2 (store the skills catalog once, reference by hash) and
3 (fetch the heavy per-turn ``final_model_input`` on demand, not in every
frame). The frame-shrink is proven against the S1 audit seam (a tight
prompt_observability byte budget the hoisted section passes and the un-hoisted
section blows), and the win is meaning-preserving: resolving a row's refs
reproduces the persisted skill lists byte-for-byte.

S7-B RULING-0 COMPAT STRIP (2026-07-16): the hoisted/evicted shape is the ONLY
shape — the ``inline_prompt_payloads`` kill-switch and its A/B goldens were
removed. The shrink is now measured against the raw (pre-hoist) row shape built
by the test's own ``_hoist_skills_catalogs`` primitive, not a legacy runtime flag.
"""

from __future__ import annotations

import copy
import json

from agent_runtime import prompt_observability as po
from tests.agent_runtime.snapshot_bytes import snapshot_size_budget


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
        # Non-evicted rows carry the full tool_schema block (schema summary +
        # count + wire size). The stub carries the count + size forward.
        "tool_schema": {
            "schema_version": 1,
            "kind": "actual_model_tools",
            "final_model_tools": ["terminal", "read_file"],
            "tool_count": 31,
            "json_bytes": 46216,
        },
        "cache_routing": {
            "schema_version": 1,
            "backend": "openai_codex",
            "prompt_cache_key_present": True,
            "prompt_cache_key_source": "static_prefix",
            "prompt_cache_key_fingerprint": f"sha256:{'a' * 64}",
            "cache_scope_source": "cache_scope_id",
            "session_header_present": True,
            "session_header_fingerprint": f"sha256:{'b' * 64}",
            "client_request_header_present": True,
            "client_request_header_fingerprint": f"sha256:{'b' * 64}",
            "scope_headers_match": True,
            "raw_values_omitted": True,
        },
        "system_prompt_sections": [
            {
                "kind": "stable",
                "name": "Stable Hermes foundation",
                "start_char": 0,
                "end_char": 6,
                "chars": 6,
                "truncated": False,
            }
        ],
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
    # The tool-schema size is the largest fixed slice of the prompt after the
    # system message; the stub carries the count + wire size (and ONLY those two
    # keys — never the schema bodies) so the context budget can attribute it.
    assert stub["tool_schema"] == {"tool_count": 31, "json_bytes": 46216}
    assert (
        stub["cache_routing"]["prompt_cache_key_fingerprint"]
        == f"sha256:{'a' * 64}"
    )
    assert stub["cache_routing"]["scope_headers_match"] is True
    assert stub["system_prompt_sections"] == fmi["system_prompt_sections"]


def test_final_model_input_stub_omits_absent_tool_schema():
    # A row whose final_model_input carried no tool_schema block must not grow a
    # fabricated one — the stub omits the key entirely (never a fake-empty {}).
    fmi = {
        "kind": "redaction_safe_final_model_input",
        "message_count": 1,
        "messages": [{"role": "user", "content": "hi", "bytes": 2}],
    }
    rows = [_row("ctx_b", available=[], accessible=[], fmi=fmi)]

    po._evict_final_model_input(rows)

    stub = rows[0]["final_model_input"]
    assert stub["evicted"] is True
    assert "tool_schema" not in stub


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
# Integration through snapshot_prompt_observability + ref→content parity.
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

    # Hoisted: no inline lists, two refs. S8: the ``skills_catalogs`` TABLE
    # leaves the frame entirely — rows keep only the ``*_ref`` hashes and the
    # bodies resolve on demand (content-addressed, cached forever launcher-side).
    for field in po.HOISTED_SKILL_LIST_FIELDS:
        assert field not in row
    assert "skills_catalogs" not in section
    assert section["skills_catalogs_ref"]["evicted"] is True
    assert po.skills_catalog_by_hash(row["available_skills_ref"]) is not None
    assert po.skills_catalog_by_hash(row["accessible_skills_ref"]) is not None
    # final_model_input evicted to a stub carrying its size.
    assert row["final_model_input"]["evicted"] is True
    assert row["final_model_input"]["bytes"] > 0
    # …but still fetchable on demand from disk.
    assert po.load_final_model_input_for_context("ctx_alice") is not None


def test_fresh_live_context_advertises_only_collectable_catalog_refs(
    isolate_agent_runtime_root,
):
    """Configured agents can have a prompt preview before their first chat row.

    Those fresh rows still publish hash pointers.  The detail-fetch lane must
    be able to collect the exact bodies from the same projection without
    making the ordinary snapshot path write to the catalog store.
    """

    from types import SimpleNamespace

    persona = SimpleNamespace(
        id="dev",
        hermes_profile="dev",
        display_name="Dev Agent",
        role="dev",
        skills=["alpha"],
    )
    instance = SimpleNamespace(
        id="personainst_dev",
        persona_id="dev",
        session_id="sess_fresh",
        current_task_id=None,
        goal_id=None,
    )
    catalogs = {}

    section = po.snapshot_prompt_observability(
        personas=[persona],
        persona_instances=[instance],
        tasks=[],
        catalog_sink=catalogs,
    )

    advertised = set(section["skills_catalogs_ref"]["hashes"])
    assert advertised
    assert advertised <= set(catalogs)
    assert all(po.load_skills_catalog_from_store(ref) is None for ref in advertised)


def test_hoisted_refs_resolve_to_the_persisted_lists(isolate_agent_runtime_root):
    # Meaning-preserving: the hoisted frame carries the SAME skill content the
    # persisted row held — resolving the refs reproduces the persisted lists
    # byte-for-byte. (S7-B: the hoisted shape is the only shape; parity is proven
    # against the known persisted input, not a legacy inline frame build.)
    _persist_alice_row()
    hoisted = po.snapshot_prompt_observability(
        personas=[], persona_instances=[_profile_instance("persona_chat_alice")]
    )
    hoisted_row = next(r for r in hoisted["chat_contexts"] if r["context_id"] == "ctx_alice")

    # The exact lists _persist_alice_row wrote (available catalog + accessible set).
    available = [
        _skill("a", "installed_skill_catalog"),
        _skill("b", "installed_skill_catalog"),
    ]
    accessible = [_skill("mission-lead")]
    # S8: the frame ships only ``*_ref`` hashes; the on-demand resolver
    # (`skills_catalog_by_hash`, the read behind `harness skills catalog --hash`)
    # reproduces the persisted lists byte-for-byte.
    assert po.skills_catalog_by_hash(hoisted_row["available_skills_ref"]) == available
    assert po.skills_catalog_by_hash(hoisted_row["accessible_skills_ref"]) == accessible
    # `skills_catalog`/`skills` were pure aliases of `available`/`accessible` —
    # they collapse to the same two refs (no third/fourth stored blob).
    assert hoisted_row["available_skills_ref"] != hoisted_row["accessible_skills_ref"]
    assert len(hoisted["skills_catalogs_ref"]["hashes"]) == 2


# --------------------------------------------------------------------------- #
# Size-budget ratchet (S1 seam, tightened via the budgets parameter — NOT by
# editing snapshot_audit.py).
# --------------------------------------------------------------------------- #
def _bulky_section(*, hoisted: bool) -> dict:
    """A prompt_observability section with a fat catalog repeated across many
    rows, built hoisted (refs) or un-hoisted (raw inline lists), for the
    byte-budget comparison. The un-hoisted rows are the RAW shape the production
    hoist collapses — the baseline that proves the shrink is real (not a removed
    runtime flag)."""

    catalog = [_skill(f"skill-{i}", "installed_skill_catalog") for i in range(80)]
    accessible = [_skill("mission-lead")]
    rows = [_row(f"ctx_{i}", available=catalog, accessible=accessible) for i in range(24)]
    catalogs: dict = {}
    if hoisted:
        po._hoist_skills_catalogs(rows, catalogs)
    return {"schema_version": 1, "chat_contexts": rows, "skills_catalogs": catalogs}


def test_prompt_observability_budget_ratchet():
    un_hoisted = {"prompt_observability": _bulky_section(hoisted=False)}
    hoisted = {"prompt_observability": _bulky_section(hoisted=True)}
    # A ratchet tight enough that the 24-row un-hoisted catalog blows it, but the
    # hoisted single-catalog section clears it comfortably. This is the S1 seam
    # tightened via its budgets parameter, proving the shrink is real.
    ratchet = {"prompt_observability": 40 * 1024}

    assert snapshot_size_budget(hoisted, ratchet) == []
    violations = snapshot_size_budget(un_hoisted, ratchet)
    assert violations, "the un-hoisted 24-row catalog must blow the ratchet"
    assert "prompt_observability" in violations[0]
