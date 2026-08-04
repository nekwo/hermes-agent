"""Snapshot catalog TTL memos (Stage 14 slice 1).

One snapshot core measured 2026-07-09: ~963 YAML parses, 15 identical
installed-skill catalog walks (one per persona chat session) and a full
profile-template re-parse — ~1.5s of a ~4s build, re-paid on EVERY event
delta because serve rebuilds a full core per delta. These memos collapse the
catalog work to once per TTL window. They are observability rows, never
authority. Both memos key on the fetcher's IDENTITY as well as the TTL, so
a monkeypatch (tests) or hot reload invalidates instantly; the conftest
autouse reset adds cross-test hygiene on top.
"""

from agent_runtime import prompt_observability as po
from agent_runtime import snapshot as snapshot_mod
from tools import skills_tool


def test_skill_catalog_memo_collapses_repeat_walks(monkeypatch):
    calls: list[int] = []

    def fake_walk(**_kwargs):
        calls.append(1)
        return [{"name": "alpha", "description": "demo", "category": "skills"}]

    monkeypatch.setattr(skills_tool, "_find_all_skills", fake_walk)

    first = po.available_skills_context()
    second = po.available_skills_context()

    assert len(calls) == 1
    assert first == second
    assert first and first[0]["name"] == "alpha"


def test_skill_catalog_memo_invalidates_when_walker_is_swapped(monkeypatch):
    """Identity keying: replacing the walker (monkeypatch, hot reload) must
    bypass the memo instantly — a warm memo never masks the swap."""
    monkeypatch.setattr(skills_tool, "_find_all_skills", lambda **_: [
        {"name": "alpha", "description": "", "category": "skills"},
    ])
    assert [row["name"] for row in po.available_skills_context()] == ["alpha"]

    monkeypatch.setattr(skills_tool, "_find_all_skills", lambda **_: [
        {"name": "beta", "description": "", "category": "skills"},
    ])
    assert [row["name"] for row in po.available_skills_context()] == ["beta"]


def test_skill_catalog_memo_expires_after_ttl(monkeypatch):
    calls: list[int] = []

    def fake_walk(**_kwargs):
        calls.append(1)
        return []

    monkeypatch.setattr(skills_tool, "_find_all_skills", fake_walk)
    monkeypatch.setattr(po, "_SKILL_CATALOG_TTL_SECONDS", 0.0)

    po.available_skills_context()
    po.available_skills_context()

    assert len(calls) == 2


def test_skill_catalog_memo_caches_empty_catalog(monkeypatch):
    """An empty catalog is a valid answer — it must be memoized too, not
    re-walked every call (the no-skills install case)."""
    calls: list[int] = []

    def fake_walk(**_kwargs):
        calls.append(1)
        return []

    monkeypatch.setattr(skills_tool, "_find_all_skills", fake_walk)

    po.available_skills_context()
    po.available_skills_context()

    assert len(calls) == 1


def test_skill_observability_resolver_is_linear_across_production_shaped_roster(
    monkeypatch, tmp_path
):
    """Eight personas × sixty assigned skills must still perform one registry
    walk and one package hash per skill, not 480 exhaustive walks/hashes.

    This is a deterministic complexity gate for the live 2026-07-22 regression;
    it avoids a flaky wall-clock assertion while pinning the operation count that
    made a snapshot exceed the Launcher's 30-second liveness budget.
    """

    from types import SimpleNamespace

    import agent.skill_utils as skill_utils

    root = tmp_path / "shared"
    names = [f"skill-{index}" for index in range(60)]
    for name in names:
        manifest = root / name / "SKILL.md"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            f"---\nname: {name}\nmetadata:\n  hermes:\n"
            "    surfaces: [mission_chat]\n    modes: [standard]\n---\nbody\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(skill_utils, "get_all_skills_dirs", lambda: [root])
    real_resolve_skills = skill_utils.resolve_skills
    real_content_hash = skill_utils.skill_package_content_hash
    resolve_calls = 0
    hash_calls = 0

    def counting_resolve(identifiers, *, roots=None, **kwargs):
        nonlocal resolve_calls
        resolve_calls += 1
        return real_resolve_skills(identifiers, roots=roots, **kwargs)

    def counting_hash(skill_dir, skill_md):
        nonlocal hash_calls
        hash_calls += 1
        return real_content_hash(skill_dir, skill_md)

    monkeypatch.setattr(skill_utils, "resolve_skills", counting_resolve)
    monkeypatch.setattr(skill_utils, "skill_package_content_hash", counting_hash)

    resolver = po._SkillObservabilityResolver()
    for index in range(8):
        rows = po._accessible_skills_context(
            SimpleNamespace(
                id=f"persona-{index}",
                hermes_profile="base",
                skills=names,
            ),
            "base",
            skill_resolver=resolver,
        )
        assert len(rows) == 60

    assert resolve_calls == 1
    assert hash_calls == 60


def test_profile_template_memo_collapses_repeat_reads(monkeypatch):
    calls: list[int] = []

    class _Template:
        name = "alpha"
        description = "demo profile"

    def fake_templates():
        calls.append(1)
        return [_Template()]

    monkeypatch.setattr(snapshot_mod, "available_profile_templates", fake_templates)

    first = snapshot_mod._profile_templates_cached()
    second = snapshot_mod._profile_templates_cached()

    assert len(calls) == 1
    assert first == second
    assert first and first[0].name == "alpha"


def test_profile_template_memo_invalidates_when_fetcher_is_swapped(monkeypatch):
    class _Template:
        def __init__(self, name):
            self.name = name
            self.description = ""

    monkeypatch.setattr(
        snapshot_mod, "available_profile_templates", lambda: [_Template("alpha")]
    )
    assert [t.name for t in snapshot_mod._profile_templates_cached()] == ["alpha"]

    monkeypatch.setattr(
        snapshot_mod, "available_profile_templates", lambda: [_Template("beta")]
    )
    assert [t.name for t in snapshot_mod._profile_templates_cached()] == ["beta"]


def test_profile_template_memo_survives_fetch_failure(monkeypatch):
    """A failing template read memoizes [] instead of raising — the persona
    summary degrades to agents-only rows exactly as before."""

    def broken():
        raise RuntimeError("profile store unavailable")

    monkeypatch.setattr(snapshot_mod, "available_profile_templates", broken)

    assert snapshot_mod._profile_templates_cached() == []
    assert snapshot_mod._available_persona_summary([]) == []
