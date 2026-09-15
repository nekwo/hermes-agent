"""C1+C2 record-once store (console-chat plan 2026-07-17, stages C1/C2).

C1 — the persisted per-turn RECORD carries one copy of each fact: the alias
key-pairs (``skills_catalog`` ≡ ``available_skills``, ``skills`` ≡
``accessible_skills``, 57.8% of every pre-C1 file) are gone; the canonical
lists leave the row as content-hash refs into a persist-time catalog store;
``final_model_input`` STAYS in the row, compact (operator ruling §7.2); the
row is built once per turn (post-turn PATCHES the pre-turn object).

C2 — the live dir is bounded (newest K per (instance, session) lane, older
rows MOVED to the archive — never deleted, always accounted) and frame reads
are roster-keyed through a latest-pointer index (a cache, never authority:
misses/corruption fall back to the glob path with typed accounting; the READ
path never writes — heal happens at the persist chokepoint, the one owner).

The byte-parity golden pins the load-bearing invariant: the emitted frame
``chat_contexts`` section is IDENTICAL whether the persisted store holds
old-shape (inline + alias) rows or new-shape (ref) rows.
"""

from __future__ import annotations

import copy
import json
import os

from agent_runtime import paths
from agent_runtime import prompt_observability as po


# --------------------------------------------------------------------------- #
# Fixture material.
# --------------------------------------------------------------------------- #
def _skill(name: str, source: str = "persona_definition", **extra) -> dict:
    row = {
        "name": name,
        "kind": "skill",
        "status": "accessible",
        "hash_tracked": False,
        "source": source,
    }
    row.update(extra)
    return row


def _catalog(count: int = 24) -> list[dict]:
    return [
        _skill(
            f"skill-{index:03d}",
            source="installed_skill_catalog",
            category="harness",
            description=f"Installed skill number {index} with a realistic frontmatter description.",
            loadable=True,
        )
        for index in range(count)
    ]


def _accessible(count: int = 6) -> list[dict]:
    return [_skill(f"skill-{index:03d}") for index in range(count)]


def _final_model_input(message_bytes: int = 4000, messages: int = 6) -> dict:
    return {
        "schema_version": 1,
        "kind": "redaction_safe_final_model_input",
        "message_count": messages,
        "messages": [
            {
                "role": "system" if index == 0 else "user",
                "source": "model_input",
                "content": "x" * message_bytes,
                "truncated": False,
                "bytes": message_bytes,
                "sha256": None,
            }
            for index in range(messages)
        ],
        "tool_schema": {
            "schema_version": 1,
            "kind": "actual_model_tools",
            "final_model_tools": ["file_read", "skill_view"],
            "tool_count": 2,
            "json_bytes": 48_000,
        },
    }


def _canonical_row(
    context_id: str,
    *,
    instance_id: str,
    session_id: str,
    persona_id: str = "dev",
    catalog: list | None = None,
    accessible: list | None = None,
    fmi: dict | None = None,
) -> dict:
    """A realistic post-C1 BUILT row (canonical two lists, no aliases)."""

    return {
        "context_id": context_id,
        "prompt_mode": "normal_hermes_profile_chat",
        "persona_id": persona_id,
        "persona_instance_id": instance_id,
        "profile": "dev",
        "session_id": session_id,
        "accessible_skills": copy.deepcopy(accessible if accessible is not None else _accessible()),
        "available_skills": copy.deepcopy(catalog if catalog is not None else _catalog()),
        "used_skills": [],
        "chat_history_context": [
            {"role": "operator", "text": "hello", "timestamp": "", "source": "persona_chat_history"}
        ],
        "final_model_input": copy.deepcopy(fmi if fmi is not None else _final_model_input()),
        "model_selection": {"effective_provider": "openai-codex", "effective_model": "gpt-5.5"},
        "turn_usage": {
            "api_calls": 2,
            "prompt_tokens": 9000,
            "input_tokens": 1000,
            "output_tokens": 400,
            "cache_read_tokens": 8000,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "first_call_prompt_tokens": 4200,
        },
    }


def _legacy_row(canonical: dict) -> dict:
    """The pre-C1 persisted shape: canonical lists PLUS the two alias copies."""

    row = copy.deepcopy(canonical)
    if isinstance(row.get("available_skills"), list):
        row["skills_catalog"] = copy.deepcopy(row["available_skills"])
    if isinstance(row.get("accessible_skills"), list):
        row["skills"] = copy.deepcopy(row["accessible_skills"])
    return row


def _write_legacy_file(row: dict, mtime: float) -> None:
    """Persist a row the way the PRE-C1 chokepoint did: inline lists, alias
    keys, ``indent=2`` — bypassing the new chokepoint entirely."""

    root = paths.prompt_observability_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{row['context_id']}.json"
    path.write_text(
        json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.utime(path, (mtime, mtime))


def _persist_new(row: dict, mtime: float) -> None:
    po.persist_prompt_observability_context(row)
    path = paths.prompt_observability_dir() / f"{row['context_id']}.json"
    os.utime(path, (mtime, mtime))


def _instance(instance_id: str, session_id: str, persona_id: str = "dev"):
    from types import SimpleNamespace

    return SimpleNamespace(
        id=instance_id,
        persona_id=persona_id,
        session_id=session_id,
        current_task_id=None,
        goal_id=None,
    )


def _persona(persona_id: str = "dev"):
    from types import SimpleNamespace

    return SimpleNamespace(
        id=persona_id,
        hermes_profile="dev",
        display_name="Dev Agent",
        role="dev",
        skills=["skill-000"],
    )


# --------------------------------------------------------------------------- #
# C1 — the persisted row: refs, no aliases, compact, final_model_input inline.
# --------------------------------------------------------------------------- #
def test_persist_writes_ref_shaped_compact_row(isolate_agent_runtime_root):
    row = _canonical_row("ctx_refshape", instance_id="personainst_a", session_id="sess_a")
    po.persist_prompt_observability_context(row)

    raw = (paths.prompt_observability_dir() / "ctx_refshape.json").read_text(encoding="utf-8")
    persisted = json.loads(raw)

    # One copy of each fact: refs instead of inline lists, aliases gone.
    assert persisted["available_skills_ref"] == po._skills_list_content_hash(row["available_skills"])
    assert persisted["accessible_skills_ref"] == po._skills_list_content_hash(row["accessible_skills"])
    for field in po.HOISTED_SKILL_LIST_FIELDS:
        assert field not in persisted, field
    # Ruling §7.2: final_model_input STAYS in the row (compact, not evicted).
    assert persisted["final_model_input"]["message_count"] == 6
    assert "evicted" not in persisted["final_model_input"]
    # Compact write: no pretty-print indentation.
    assert "\n" not in raw.strip()
    # The catalog store holds the bodies, content-addressed and compact.
    catalog_path = (
        paths.prompt_observability_catalogs_dir()
        / f"{persisted['available_skills_ref']}.json"
    )
    catalog_raw = catalog_path.read_text(encoding="utf-8")
    assert json.loads(catalog_raw) == row["available_skills"]
    assert "\n" not in catalog_raw.strip()


def test_persist_never_mutates_the_callers_row(isolate_agent_runtime_root):
    # The chat.final wire echo embeds the very dict the turn built (C3's lane
    # slims it, not this one) — the persist chokepoint must transform a COPY.
    row = _canonical_row("ctx_nomutate", instance_id="personainst_a", session_id="sess_a")
    before = copy.deepcopy(row)
    po.persist_prompt_observability_context(row)
    assert row == before


def test_persist_normalizes_alias_only_legacy_input(isolate_agent_runtime_root):
    # A legacy-shaped input carrying ONLY the alias keys loses no data: the
    # aliases fold into the canonical refs (one shape on disk, ruling 0).
    accessible = _accessible(3)
    catalog = _catalog(5)
    po.persist_prompt_observability_context(
        {
            "context_id": "ctx_alias_only",
            "persona_id": "dev",
            "persona_instance_id": "personainst_alias",
            "session_id": "sess_alias",
            "skills": copy.deepcopy(accessible),
            "skills_catalog": copy.deepcopy(catalog),
        }
    )
    persisted = json.loads(
        (paths.prompt_observability_dir() / "ctx_alias_only.json").read_text(encoding="utf-8")
    )
    assert persisted["accessible_skills_ref"] == po._skills_list_content_hash(accessible)
    assert persisted["available_skills_ref"] == po._skills_list_content_hash(catalog)
    assert "skills" not in persisted and "skills_catalog" not in persisted
    assert po.skills_catalog_by_hash(persisted["accessible_skills_ref"]) == accessible


def test_skills_catalog_by_hash_is_an_o1_store_read(isolate_agent_runtime_root):
    row = _canonical_row("ctx_store_hit", instance_id="personainst_a", session_id="sess_a")
    po.persist_prompt_observability_context(row)
    ref = po._skills_list_content_hash(row["available_skills"])

    # Remove every persisted ctx row: the legacy walk has NOTHING to find, so a
    # successful resolve proves the content-addressed store lane, not the walk.
    for path in paths.prompt_observability_dir().glob("*.json"):
        path.unlink()
    assert po.skills_catalog_by_hash(ref) == row["available_skills"]


def test_skills_catalog_by_hash_materializes_a_live_projection_miss(
    isolate_agent_runtime_root, monkeypatch
):
    """A hash advertised for a configured agent may have no chat-time row yet.

    The explicit fetch verb must rebuild that live projection once, cache its
    immutable bodies, and make the next lookup an O(1) store hit.  Ordinary
    snapshot reads remain write-free; only the operator-requested detail fetch
    is allowed to materialize this derived cache.
    """

    catalog = _catalog(3)
    ref = po._skills_list_content_hash(catalog)
    calls = []

    def fake_build_snapshot(*, prompt_skills_catalogs=None, **_kwargs):
        calls.append(True)
        assert prompt_skills_catalogs is not None
        prompt_skills_catalogs[ref] = copy.deepcopy(catalog)
        return {}

    monkeypatch.setattr("agent_runtime.snapshot.build_snapshot", fake_build_snapshot)

    assert po.load_skills_catalog_from_store(ref) is None
    assert po.skills_catalog_by_hash(ref) == catalog
    assert calls == [True]
    assert po.load_skills_catalog_from_store(ref) == catalog

    # The immutable body is now a direct store hit.  A second lookup must not
    # rebuild the live projection.
    assert po.skills_catalog_by_hash(ref) == catalog
    assert calls == [True]


def test_corrupt_catalog_store_file_is_a_typed_miss(isolate_agent_runtime_root):
    # Sabotage (C1 acceptance): a corrupt/tampered store file must be a MISS —
    # never fake content served under a hash it doesn't match.
    row = _canonical_row("ctx_corrupt", instance_id="personainst_a", session_id="sess_a")
    po.persist_prompt_observability_context(row)
    ref = po._skills_list_content_hash(row["available_skills"])
    catalog_path = paths.prompt_observability_catalogs_dir() / f"{ref}.json"

    # Tamper 1: valid JSON, wrong content — the integrity re-hash catches it.
    catalog_path.write_text(json.dumps([{"name": "tampered"}]), encoding="utf-8")
    assert po.load_skills_catalog_from_store(ref) is None
    # The persisted ROW still resolves nothing through the walk (it carries a
    # ref, not an inline list) — an honest miss, never the tampered payload.
    resolved = po.skills_catalog_by_hash(ref)
    assert resolved is None or resolved == row["available_skills"]
    assert resolved != [{"name": "tampered"}]

    # Tamper 2: invalid JSON — same typed miss.
    catalog_path.write_text("{not json", encoding="utf-8")
    assert po.load_skills_catalog_from_store(ref) is None


def test_legacy_inline_rows_still_resolve_via_walk(isolate_agent_runtime_root):
    # Rows persisted BEFORE the store existed carry inline lists and no store
    # entry; the pre-C1 walk remains their resolve lane (intent preserved from
    # the landed S8 hit/miss coverage).
    catalog = _catalog(4)
    legacy = _legacy_row(
        _canonical_row(
            "ctx_legacy_walk",
            instance_id="personainst_legacy",
            session_id="sess_legacy",
            catalog=catalog,
        )
    )
    _write_legacy_file(legacy, mtime=1_700_000_100.0)
    ref = po._skills_list_content_hash(catalog)
    assert (paths.prompt_observability_catalogs_dir() / f"{ref}.json").exists() is False
    assert po.skills_catalog_by_hash(ref) == catalog


# --------------------------------------------------------------------------- #
# C1 — build once per turn: the post-turn PATCH equals the pre-C1 full rebuild.
# --------------------------------------------------------------------------- #
def test_attach_turn_results_matches_the_full_rebuild(isolate_agent_runtime_root):
    persona = _persona()
    build_kwargs = dict(
        persona=persona,
        persona_instance_id="personainst_dev",
        session_id="persona_chat_dev",
        turn_id="turn-1",
        surface_prompt="",
        limiting_wrapper_active=False,
        session_db=None,
        current_message="hello there",
        model_selection={"effective_provider": "openai-codex", "effective_model": "gpt-5.5"},
    )
    fmi = _final_model_input(message_bytes=800, messages=3)
    trace = [
        {
            "tool_name": "skill_view",
            "step": "tool_finished",
            "status": "passed",
            "skill_name": "systematic-debugging",
        }
    ]
    usage = {
        "api_calls": 1,
        "prompt_tokens": 5200,
        "input_tokens": 200,
        "output_tokens": 90,
        "cache_read_tokens": 5000,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "first_call_prompt_tokens": 5200,
    }

    pre_turn = po.mission_chat_prompt_observability(**build_kwargs)
    record_at_injection = {
        key: copy.deepcopy(pre_turn[key])
        for key in ("chat_history_context", "context_files", "prompt_layers", "situational_hud")
    }
    rebuilt = po.mission_chat_prompt_observability(
        **build_kwargs,
        final_model_input=fmi,
        turn_usage=usage,
        trace_events=trace,
    )
    patched = po.attach_prompt_observability_turn_results(
        pre_turn,
        final_model_input=fmi,
        model_selection=build_kwargs["model_selection"],
        turn_usage=usage,
        trace_events=trace,
    )

    assert patched is pre_turn, "the patch mutates the pre-turn object in place"
    for key in (
        "context_id",
        "final_model_input",
        "turn_usage",
        "used_skills",
        "model_selection",
        "context_budget",
    ):
        assert patched[key] == rebuilt[key], key
    # Record-at-injection fields are untouched: the peek shows what was FED.
    for key, value in record_at_injection.items():
        assert patched[key] == value, key


# --------------------------------------------------------------------------- #
# C1 — measured row shrink (reported bytes; the assertion is structural).
# --------------------------------------------------------------------------- #
def test_persisted_row_size_shrinks(isolate_agent_runtime_root):
    catalog = _catalog(160)
    accessible = _accessible(40)
    fmi = _final_model_input(message_bytes=3500, messages=7)
    row = _canonical_row(
        "ctx_size_new",
        instance_id="personainst_size",
        session_id="sess_size",
        catalog=catalog,
        accessible=accessible,
        fmi=fmi,
    )
    legacy = _legacy_row({**copy.deepcopy(row), "context_id": "ctx_size_old"})
    _write_legacy_file(legacy, mtime=1_700_000_000.0)
    po.persist_prompt_observability_context(row)

    old_bytes = (paths.prompt_observability_dir() / "ctx_size_old.json").stat().st_size
    new_bytes = (paths.prompt_observability_dir() / "ctx_size_new.json").stat().st_size
    catalog_store_bytes = sum(
        path.stat().st_size for path in paths.prompt_observability_catalogs_dir().glob("*.json")
    )
    # Printed for the stage report (pytest -s); the assertion is the ratio.
    print(
        f"\nC1 row size: old(indent=2 + aliases)={old_bytes}B "
        f"new(compact refs)={new_bytes}B catalog_store={catalog_store_bytes}B"
    )
    # The alias duplication alone was ~57.8% of the old row; with compaction the
    # new row must come in well under half — anything above that means the
    # dedup regressed.
    assert new_bytes < old_bytes * 0.5, (old_bytes, new_bytes)


# --------------------------------------------------------------------------- #
# THE byte-parity golden: frame chat_contexts identical over old/new stores.
# --------------------------------------------------------------------------- #
def _seed_store(*, legacy_shape: bool) -> None:
    """Three lanes: (a) live instance's CURRENT session (built-row overlap),
    (b) same live instance's OLDER console session (no built match — the
    historical-session eviction lane), (c) a departed instance (evicted)."""

    rows = [
        (
            _canonical_row(
                "ctx_live_current",
                instance_id="personainst_live",
                session_id="sess_current",
            ),
            1_700_000_300.0,
        ),
        (
            _canonical_row(
                "ctx_live_older",
                instance_id="personainst_live",
                session_id="sess_older",
                accessible=_accessible(2),
            ),
            1_700_000_200.0,
        ),
        (
            _canonical_row(
                "ctx_departed",
                instance_id="personainst_departed",
                session_id="sess_departed",
            ),
            1_700_000_100.0,
        ),
    ]
    for row, mtime in rows:
        if legacy_shape:
            _write_legacy_file(_legacy_row(row), mtime)
        else:
            _persist_new(row, mtime)


def _build_frame_section() -> dict:
    return po.snapshot_prompt_observability(
        personas=[_persona()],
        persona_instances=[_instance("personainst_live", "sess_current")],
    )


def test_frame_chat_contexts_byte_parity_old_vs_new_store(tmp_path, monkeypatch):
    # Old-shape store (pre-C1 files, no index → legacy glob read mode).
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "old-root"))
    _seed_store(legacy_shape=True)
    old_section = _build_frame_section()

    # New-shape store (C1 refs + C2 index → roster-keyed read mode).
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "new-root"))
    _seed_store(legacy_shape=False)
    new_section = _build_frame_section()

    old_bytes = json.dumps(old_section["chat_contexts"], ensure_ascii=False, sort_keys=True)
    new_bytes = json.dumps(new_section["chat_contexts"], ensure_ascii=False, sort_keys=True)
    assert old_bytes == new_bytes, "frame chat_contexts must be byte-identical across store shapes"
    # The catalog pointer accounting holds across shapes too.
    assert old_section["skills_catalogs_ref"]["hashes"] == new_section["skills_catalogs_ref"]["hashes"]
    # Both modes evict the departed lane and the live instance's stale session.
    assert old_section["chat_contexts_ref"]["count"] == new_section["chat_contexts_ref"]["count"] == 2
    # And the new store actually took the index lane, not the fallback.
    assert new_section["chat_contexts_ref"]["read"]["source"] == "index"
    assert old_section["chat_contexts_ref"]["read"]["source"] == "glob_fallback"


# --------------------------------------------------------------------------- #
# C2 — retention at the persist chokepoint.
# --------------------------------------------------------------------------- #
def test_retention_keeps_newest_k_live_and_archives_older(isolate_agent_runtime_root):
    for index in range(4):
        _persist_new(
            _canonical_row(
                f"ctx_lane_{index}",
                instance_id="personainst_ret",
                session_id="sess_ret",
                catalog=_catalog(3),
            ),
            mtime=1_700_000_000.0 + index,
        )

    live = {path.name for path in paths.prompt_observability_dir().glob("*.json")}
    archived = {path.name for path in paths.prompt_observability_archive_dir().glob("*.json")}
    assert live == {"ctx_lane_3.json", "ctx_lane_2.json"}, live
    assert archived == {"ctx_lane_0.json", "ctx_lane_1.json"}, archived

    # Archive-never-delete AND accounted: the archived row stays fetchable via
    # the same id lane the frame's fetch verb uses.
    archived_row = po.load_persisted_context_row("ctx_lane_0")
    assert archived_row is not None
    assert archived_row["context_id"] == "ctx_lane_0"
    index = po._load_prompt_observability_index()
    assert index["archived_count"] == 2


def test_first_indexed_persist_sweeps_legacy_lanes(isolate_agent_runtime_root):
    # 218 legacy files, no retention — the measured live state. The FIRST
    # persist through the C2 chokepoint rebuilds the index from the dir and
    # performs the one-time bounded-store sweep across every lane.
    for index in range(3):
        _write_legacy_file(
            _legacy_row(
                _canonical_row(
                    f"ctx_old_a{index}",
                    instance_id="personainst_a",
                    session_id="sess_a",
                    catalog=_catalog(2),
                )
            ),
            mtime=1_700_000_000.0 + index,
        )
    _write_legacy_file(
        _legacy_row(
            _canonical_row(
                "ctx_old_b0",
                instance_id="personainst_b",
                session_id="sess_b",
                catalog=_catalog(2),
            )
        ),
        mtime=1_700_000_050.0,
    )

    _persist_new(
        _canonical_row(
            "ctx_new_c0", instance_id="personainst_c", session_id="sess_c", catalog=_catalog(2)
        ),
        mtime=1_700_000_100.0,
    )

    live = {path.stem for path in paths.prompt_observability_dir().glob("*.json")}
    archived = {path.stem for path in paths.prompt_observability_archive_dir().glob("*.json")}
    # Lane a keeps its 2 newest; the oldest moved. Lanes b/c are under K.
    assert live == {"ctx_old_a1", "ctx_old_a2", "ctx_old_b0", "ctx_new_c0"}, live
    assert archived == {"ctx_old_a0"}, archived


# --------------------------------------------------------------------------- #
# C2 — roster-keyed reads: I/O is O(live roster), typed fallbacks, heal.
# --------------------------------------------------------------------------- #
def _seed_many_lanes(lane_count: int = 6, rows_per_lane: int = 2) -> None:
    for lane in range(lane_count):
        for row_index in range(rows_per_lane):
            _persist_new(
                _canonical_row(
                    f"ctx_l{lane}_r{row_index}",
                    instance_id=f"personainst_l{lane}",
                    session_id=f"sess_l{lane}",
                    catalog=_catalog(3),
                ),
                mtime=1_700_000_000.0 + lane * 10 + row_index,
            )


def test_frame_read_is_roster_sized(isolate_agent_runtime_root):
    _seed_many_lanes(lane_count=6, rows_per_lane=2)
    total_live_files = len(list(paths.prompt_observability_dir().glob("*.json")))
    assert total_live_files == 12

    section = po.snapshot_prompt_observability(
        personas=[_persona()],
        persona_instances=[
            _instance("personainst_l0", "sess_l0"),
            _instance("personainst_l1", "sess_l1"),
        ],
    )
    read = section["chat_contexts_ref"]["read"]
    assert read["source"] == "index"
    assert read["index_misses"] == 0
    # The measured before/after: the legacy path parsed min(dir, 50) files per
    # full core (12 here, 50/4.51MB live); the roster-keyed path reads exactly
    # one file per live lane. Ratchet: never above the roster size.
    assert read["files_read"] <= 2, read
    print(f"\nC2 full-core ctx I/O: legacy path would read {total_live_files} files; "
          f"index path read {read['files_read']}")
    # The four stale lanes are accounted, never silently skipped.
    assert section["chat_contexts_ref"]["count"] == 4
    live_ids = {row["persona_instance_id"] for row in section["chat_contexts"]}
    assert live_ids == {"personainst_l0", "personainst_l1"}


def test_frame_read_excludes_old_sessions_for_live_instance(isolate_agent_runtime_root):
    _persist_new(
        _canonical_row(
            "ctx_old_session",
            instance_id="personainst_live",
            session_id="sess_old",
            catalog=_catalog(3),
        ),
        mtime=1_700_000_000.0,
    )
    _persist_new(
        _canonical_row(
            "ctx_current_session",
            instance_id="personainst_live",
            session_id="sess_current",
            catalog=_catalog(3),
        ),
        mtime=1_700_000_100.0,
    )

    section = po.snapshot_prompt_observability(
        personas=[_persona()],
        persona_instances=[_instance("personainst_live", "sess_current")],
    )

    read = section["chat_contexts_ref"]["read"]
    assert read["source"] == "index"
    assert read["files_read"] == 1
    assert {row["context_id"] for row in section["chat_contexts"]} <= {
        "ctx_current_session"
    }
    assert section["chat_contexts_ref"]["count"] == 1


def test_deleted_indexed_file_typed_miss_fallback_then_heal(isolate_agent_runtime_root):
    _seed_many_lanes(lane_count=2, rows_per_lane=2)
    # Sabotage: delete lane 0's newest row OUTSIDE the chokepoint.
    (paths.prompt_observability_dir() / "ctx_l0_r1.json").unlink()

    # Phase 1 — typed miss, correct output via the glob fallback (read-only).
    section = po.snapshot_prompt_observability(
        personas=[_persona()],
        persona_instances=[_instance("personainst_l0", "sess_l0")],
    )
    read = section["chat_contexts_ref"]["read"]
    assert read["index_misses"] == 1
    assert read["source"] == "glob_fallback"
    assert read["index_status"] == "miss"
    # The fallback even recovers the lane's PREVIOUS row — correct, not empty.
    ids = {row["context_id"] for row in section["chat_contexts"]}
    assert "ctx_l0_r0" in ids

    # Phase 2 — the heal happens at the NEXT persist (the index's one owner;
    # the read path never writes).
    _persist_new(
        _canonical_row(
            "ctx_l1_r2", instance_id="personainst_l1", session_id="sess_l1", catalog=_catalog(3)
        ),
        mtime=1_700_000_900.0,
    )
    healed = po._load_prompt_observability_index()
    lane0 = next(
        entry for entry in healed["entries"] if entry["instance_id"] == "personainst_l0"
    )
    assert "ctx_l0_r1" not in lane0["context_ids"], "the dangling pointer must be healed away"

    # Phase 3 — the read path is back on the index lane, no misses.
    section = po.snapshot_prompt_observability(
        personas=[_persona()],
        persona_instances=[_instance("personainst_l0", "sess_l0")],
    )
    read = section["chat_contexts_ref"]["read"]
    assert read["source"] == "index"
    assert read["index_misses"] == 0


def test_corrupt_index_falls_back_then_heals_at_next_persist(isolate_agent_runtime_root):
    _seed_many_lanes(lane_count=1, rows_per_lane=1)
    paths.prompt_observability_index_path().write_text("{corrupt", encoding="utf-8")

    # Read: typed fallback, correct output, NO write (the read path never heals).
    section = po.snapshot_prompt_observability(
        personas=[_persona()],
        persona_instances=[_instance("personainst_l0", "sess_l0")],
    )
    read = section["chat_contexts_ref"]["read"]
    assert read["source"] == "glob_fallback"
    assert read["index_status"] == "absent_or_corrupt"
    assert {row["context_id"] for row in section["chat_contexts"]} >= {"ctx_l0_r0"}
    assert paths.prompt_observability_index_path().read_text(encoding="utf-8") == "{corrupt"

    # Persist: the chokepoint rebuilds (heals) the index from the live dir.
    _persist_new(
        _canonical_row(
            "ctx_l0_r1", instance_id="personainst_l0", session_id="sess_l0", catalog=_catalog(3)
        ),
        mtime=1_700_000_950.0,
    )
    healed = po._load_prompt_observability_index()
    assert healed is not None
    assert healed["schema_version"] == 1
    lane = next(entry for entry in healed["entries"] if entry["instance_id"] == "personainst_l0")
    assert lane["context_ids"][0] == "ctx_l0_r1"


def test_archived_rows_resolve_through_prompt_context_fetch_lane(isolate_agent_runtime_root):
    # The chat_contexts_ref fetch verb (`harness prompt-context show`) resolves
    # archived rows too — retention MOVES, the operator's fetch never breaks.
    for index in range(3):
        _persist_new(
            _canonical_row(
                f"ctx_fetch_{index}",
                instance_id="personainst_f",
                session_id="sess_f",
                catalog=_catalog(2),
            ),
            mtime=1_700_000_000.0 + index,
        )
    assert not (paths.prompt_observability_dir() / "ctx_fetch_0.json").exists()
    row = po.load_persisted_context_row("ctx_fetch_0")
    assert row is not None and row["context_id"] == "ctx_fetch_0"
    # final_model_input survives on the archived row as well (asserted off the
    # row itself since S54 removed the callerless on-demand accessor).
    assert row["final_model_input"].get("evicted") is not True
    # Honest miss for an unknown id — never a fabricated row.
    assert po.load_persisted_context_row("ctx_never_existed") is None
