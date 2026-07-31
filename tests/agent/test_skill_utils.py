"""Tests for agent/skill_utils.py."""

from unittest.mock import patch

import pytest

from agent.skill_utils import (
    extract_skill_config_vars,
    extract_skill_conditions,
    get_disabled_skill_names,
    get_external_skills_dirs,
    is_excluded_skill_path,
    is_external_skill_path,
    is_skill_support_path,
    iter_skill_index_files,
    parse_frontmatter,
    resolve_skill_config_values,
    required_preload_skill_ids,
    resolve_skill,
    skill_frontmatter_runtime_compatibility,
    skill_package_content_hash,
    skill_runtime_compatibility,
    skill_matches_platform,
    skill_matches_platform_list,
)


# Fork-owned: the canonical skill resolver (resolve_skill / resolve_skills /
# skill_runtime_compatibility / required_preload_skill_ids) and the shared
# skills root are fork surfaces with no upstream counterpart.
def _write_skill(root, name, *, modes=None, load_policy=None):
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    metadata = ""
    if modes or load_policy:
        metadata = (
            "metadata:\n  hermes:\n    surfaces: [mission_chat]\n"
            f"    modes: [{', '.join(modes or ['standard'])}]\n"
            f"    load_policy: {load_policy or 'recommended'}\n"
        )
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test\n{metadata}---\nbody\n",
        encoding="utf-8",
    )
    return skill_dir


def test_canonical_resolver_reports_collision_and_exact_hash(tmp_path):
    local = tmp_path / "local"
    shared = tmp_path / "shared"
    local.mkdir()
    shared.mkdir()
    _write_skill(local, "same")
    shared_skill = _write_skill(shared, "shared-only")

    resolved = resolve_skill("shared-only", roots=[local, shared])
    assert resolved.status == "resolved"
    assert resolved.candidate is not None
    assert skill_package_content_hash(resolved.candidate.skill_dir, resolved.candidate.skill_md)

    _write_skill(shared, "same")
    assert resolve_skill("same", roots=[local, shared]).status == "collision"


def test_runtime_compatibility_rejects_root_only_skill_in_standard_chat(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, "lead", modes=["root_node"])
    candidate = resolve_skill("lead", roots=[root]).candidate

    assert skill_runtime_compatibility(
        candidate, surface="mission_chat", root_node_mode=False
    )["reason"] == "mode_not_supported"


@pytest.mark.parametrize(
    "frontmatter",
    [
        # Claude-format skill: metadata dict present, no hermes block at all.
        {"name": "foreign", "metadata": {"short-description": "x"}},
        # Degenerate YAML: `hermes:` key present with a None value.
        {"name": "foreign", "metadata": {"hermes": None}},
        # hermes block is a non-dict scalar (malformed-YAML fallback shape).
        {"name": "foreign", "metadata": {"hermes": "nope"}},
    ],
)
def test_runtime_compatibility_tolerates_non_hermes_metadata(frontmatter):
    """A skill without a usable metadata.hermes block degrades to defaults.

    Regression pin for 2026-07-27: a Claude-format skill moved into the shared
    skills root (metadata present, no hermes key) made
    ``hermes.get("load_policy")`` raise AttributeError on None, which killed
    every mission-chat turn via available_skills_context. One foreign manifest
    must never take down the prompt-observability lane.
    """

    result = skill_frontmatter_runtime_compatibility(
        frontmatter, surface="mission_chat"
    )
    assert result["compatible"] is True
    assert result["load_policy"] == "explicit"


def test_canonical_harness_skill_refuses_non_shared_source_and_duplicates(
    tmp_path, monkeypatch
):
    import agent.skill_utils as skill_utils

    local = tmp_path / "local"
    shared = tmp_path / "shared"
    local.mkdir()
    shared.mkdir()
    monkeypatch.setattr(skill_utils, "get_shared_skills_dir", lambda: shared)
    _write_skill(local, "harness-runtime-model")

    assert resolve_skill(
        "harness-runtime-model", roots=[local, shared]
    ).status == "invalid_source"

    _write_skill(shared, "harness-runtime-model")
    assert resolve_skill(
        "harness-runtime-model", roots=[local, shared]
    ).status == "collision"


def test_required_preload_policy_uses_resolver_and_compatibility(tmp_path, monkeypatch):
    import agent.skill_utils as skill_utils

    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(skill_utils, "get_shared_skills_dir", lambda: shared)
    monkeypatch.setattr(skill_utils, "get_all_skills_dirs", lambda: [shared])
    _write_skill(
        shared,
        "harness-runtime-model",
        modes=["standard"],
        load_policy="required_preload",
    )

    assert required_preload_skill_ids(
        ["harness-runtime-model"], surface="mission_chat"
    ) == ["harness-runtime-model"]


def test_metadata_as_dict_with_hermes():
    """Normal case: metadata is a dict containing hermes keys."""
    frontmatter = {
        "metadata": {
            "hermes": {
                "fallback_for_toolsets": ["toolset_a"],
                "requires_toolsets": ["toolset_b"],
                "fallback_for_tools": ["tool_x"],
                "requires_tools": ["tool_y"],
            }
        }
    }
    result = extract_skill_conditions(frontmatter)
    assert result["fallback_for_toolsets"] == ["toolset_a"]
    assert result["requires_toolsets"] == ["toolset_b"]
    assert result["fallback_for_tools"] == ["tool_x"]
    assert result["requires_tools"] == ["tool_y"]


def test_metadata_as_string_does_not_crash():
    """Bug case: metadata is a non-dict truthy value (e.g. a YAML string)."""
    frontmatter = {"metadata": "some text"}
    result = extract_skill_conditions(frontmatter)
    assert result == {
        "fallback_for_toolsets": [],
        "requires_toolsets": [],
        "fallback_for_tools": [],
        "requires_tools": [],
    }


def test_metadata_as_none():
    """metadata key is present but set to null/None."""
    frontmatter = {"metadata": None}
    result = extract_skill_conditions(frontmatter)
    assert result == {
        "fallback_for_toolsets": [],
        "requires_toolsets": [],
        "fallback_for_tools": [],
        "requires_tools": [],
    }


def test_metadata_missing_entirely():
    """metadata key is absent from frontmatter."""
    frontmatter = {"name": "my-skill", "description": "Does stuff."}
    result = extract_skill_conditions(frontmatter)
    assert result == {
        "fallback_for_toolsets": [],
        "requires_toolsets": [],
        "fallback_for_tools": [],
        "requires_tools": [],
    }












def test_skill_config_helpers_share_raw_config_parse_cache(tmp_path, monkeypatch):
    """Repeated skill config helpers should parse config.yaml only once."""
    from agent import skill_utils

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    external = tmp_path / "external-skills"
    external.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        f"""
skills:
  disabled:
    - hidden-skill
  external_dirs:
    - {external}
  config:
    wiki:
      path: ~/wiki
""".strip(),
        encoding="utf-8",
    )
    parse_count = 0
    real_yaml_load = skill_utils.yaml_load

    def counting_yaml_load(text):
        nonlocal parse_count
        parse_count += 1
        return real_yaml_load(text)

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    skill_utils._external_dirs_cache_clear()
    getattr(skill_utils, "_raw_config_cache_clear", lambda: None)()
    monkeypatch.setattr(skill_utils, "yaml_load", counting_yaml_load)

    assert get_disabled_skill_names() == {"hidden-skill"}
    assert get_external_skills_dirs() == [external.resolve()]
    assert resolve_skill_config_values([
        {"key": "wiki.path", "description": "Wiki path"}
    ])["wiki.path"].endswith("/wiki")
    assert parse_count == 1






def test_iter_skill_index_files_prunes_skill_support_dirs(tmp_path):
    """Archived package SKILL.md files under support dirs are not active skills."""
    real = tmp_path / "umbrella"
    real.mkdir()
    (real / "SKILL.md").write_text("---\nname: umbrella\n---\n", encoding="utf-8")

    package = real / "references" / "old-skill-package"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text("---\nname: old-skill\n---\n", encoding="utf-8")
    (package / "DESCRIPTION.md").write_text(
        "---\ndescription: archived package\n---\n", encoding="utf-8"
    )

    script_package = real / "scripts" / "helper-skill"
    script_package.mkdir(parents=True)
    (script_package / "SKILL.md").write_text("---\nname: helper\n---\n", encoding="utf-8")

    found = list(iter_skill_index_files(tmp_path, "SKILL.md"))
    desc_found = list(iter_skill_index_files(tmp_path, "DESCRIPTION.md"))

    assert found == [real / "SKILL.md"]
    assert desc_found == []
    assert is_skill_support_path(package / "SKILL.md") is True
    assert is_excluded_skill_path(package / "SKILL.md") is True


def test_iter_skill_index_files_keeps_support_named_categories(tmp_path):
    """A category named scripts/templates/assets/references is still valid."""
    scripts_skill = tmp_path / "scripts" / "bash-helper"
    scripts_skill.mkdir(parents=True)
    (scripts_skill / "SKILL.md").write_text(
        "---\nname: bash-helper\n---\n", encoding="utf-8"
    )

    templates_skill = tmp_path / "templates" / "deck-template"
    templates_skill.mkdir(parents=True)
    (templates_skill / "SKILL.md").write_text(
        "---\nname: deck-template\n---\n", encoding="utf-8"
    )

    found = list(iter_skill_index_files(tmp_path, "SKILL.md"))

    assert found == [scripts_skill / "SKILL.md", templates_skill / "SKILL.md"]
    assert is_skill_support_path(scripts_skill / "SKILL.md") is False
    assert is_excluded_skill_path(scripts_skill / "SKILL.md") is False


def test_skill_support_path_uses_explicit_discovery_root_not_cwd(tmp_path, monkeypatch):
    discovery_root = tmp_path / "site-packages" / "skills"
    umbrella = discovery_root / "category" / "umbrella"
    nested = umbrella / "references" / "archived" / "SKILL.md"
    nested.parent.mkdir(parents=True)
    (umbrella / "SKILL.md").write_text("---\nname: umbrella\n---\n", encoding="utf-8")
    nested.write_text("---\nname: archived\n---\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    relative = nested.relative_to(discovery_root)
    assert is_skill_support_path(relative, root=discovery_root) is True
    assert is_excluded_skill_path(relative, root=discovery_root) is True


# ── skill_matches_platform on Termux ──────────────────────────────────────


class TestSkillMatchesPlatformTermux:
    """Termux is Linux userland on Android. Skills tagged platforms:[linux]
    must load there regardless of whether Python reports sys.platform as
    "linux" (pre-3.13) or "android" (3.13+). Reported by user @LikiusInik
    in May 2026 — only 3 built-in skills appeared on Termux because every
    github/productivity/mlops skill is tagged platforms:[linux,macos,windows]
    and sys.platform=="android" did not start with "linux".
    """

    def test_no_platforms_field_matches_everywhere(self):
        # Backward-compat default — skills without a platforms tag load
        # on any OS, Termux included.
        with patch("agent.skill_utils.sys.platform", "android"), patch(
            "agent.skill_utils.is_termux", return_value=True
        ):
            assert skill_matches_platform({}) is True
            assert skill_matches_platform({"name": "foo"}) is True







    def test_non_termux_android_does_not_widen(self):
        # If we're somehow on a plain Android Python (not Termux), don't
        # silently load Linux skills — Termux is the supported environment.
        fm = {"platforms": ["linux"]}
        with patch("agent.skill_utils.sys.platform", "android"), patch(
            "agent.skill_utils.is_termux", return_value=False
        ):
            assert skill_matches_platform(fm) is False
            assert skill_matches_platform_list(fm["platforms"]) is False

    def test_linux_skill_on_real_linux_unaffected(self):
        # The non-Termux Linux path must not change.
        fm = {"platforms": ["linux"]}
        with patch("agent.skill_utils.sys.platform", "linux"), patch(
            "agent.skill_utils.is_termux", return_value=False
        ):
            assert skill_matches_platform(fm) is True
            assert skill_matches_platform_list(fm["platforms"]) is True



class TestNormalizeSkillLookupName:
    def test_relative_path_unchanged(self, tmp_path, monkeypatch):
        from agent.skill_utils import normalize_skill_lookup_name

        # Relative identifiers early-return before any root lookup.
        assert normalize_skill_lookup_name("foo/bar") == "foo/bar"


    def test_absolute_via_symlink_uses_lexical_relative_path(self, tmp_path, monkeypatch):
        from agent.skill_utils import normalize_skill_lookup_name

        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        external = tmp_path / "external" / "my-skill"
        external.mkdir(parents=True)
        link = skills_dir / "my-skill"
        try:
            link.symlink_to(external)
        except OSError:
            pytest.skip("Symlinks not supported")
        monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", skills_dir)
        assert normalize_skill_lookup_name(str(link)) == "my-skill"


# Fork-owned: resolver/frontmatter mtime caches + batched resolve_skills.
def test_skill_package_content_hash_mtime_cache_invalidates_on_edit(tmp_path):
    """Item 4: the mtime-keyed content-hash cache returns identical hashes on a
    repeat, and a real on-disk edit (mtime/size change) invalidates the entry —
    it is never process-lifetime stale for changed content."""
    import os

    from agent.skill_utils import _content_hash_cache_clear, skill_package_content_hash

    _content_hash_cache_clear()
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    md = skill_dir / "SKILL.md"
    md.write_text("---\nname: s\n---\nv1\n", encoding="utf-8")

    h1 = skill_package_content_hash(skill_dir, md)
    assert skill_package_content_hash(skill_dir, md) == h1  # cache hit, identical

    # Same-length edit with an explicitly advanced mtime still invalidates
    # (proves the cache keys on mtime, not only size).
    md.write_text("---\nname: s\n---\nvX\n", encoding="utf-8")
    os.utime(md, ns=(1, 5_000_000_000))
    h2 = skill_package_content_hash(skill_dir, md)
    assert h2 != h1

    # The cached value equals a freshly-cleared (uncached) recompute — caching is
    # transparent, only cost differs.
    _content_hash_cache_clear()
    assert skill_package_content_hash(skill_dir, md) == h2


def test_skill_runtime_compatibility_mtime_cache_reflects_edit(tmp_path):
    """Item 3: the frontmatter parse behind skill_runtime_compatibility is
    mtime-cached; editing the manifest (new mtime/size) is reflected, so the
    cache never masks an on-disk change."""
    import os

    from agent.skill_utils import (
        SkillResolutionCandidate,
        skill_runtime_compatibility,
    )
    from agent_runtime.parse_cache import clear_parse_cache

    clear_parse_cache()
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    md = skill_dir / "SKILL.md"
    md.write_text(
        "---\nname: s\nmetadata:\n  hermes:\n    surfaces: [mission_chat]\n---\nbody\n",
        encoding="utf-8",
    )
    cand = SkillResolutionCandidate(
        root=tmp_path, skill_dir=skill_dir, skill_md=md, source_kind="external"
    )

    assert skill_runtime_compatibility(cand, surface="mission_chat")["compatible"] is True
    # Not yet allowed on mission_worker (proves the frontmatter is actually read).
    assert skill_runtime_compatibility(cand, surface="mission_worker")["compatible"] is False

    md.write_text(
        "---\nname: s\nmetadata:\n  hermes:\n    surfaces: [mission_chat, mission_worker]\n---\nbody\n",
        encoding="utf-8",
    )
    os.utime(md, ns=(1, 5_000_000_000))
    assert skill_runtime_compatibility(cand, surface="mission_worker")["compatible"] is True


def test_resolve_skills_batched_matches_per_name_resolve_skill(tmp_path):
    """Item 2: the batched resolve_skills is behavior-equivalent to per-name
    resolve_skill (same status + same candidate manifests) for present, missing,
    and collision names."""
    from agent.skill_utils import resolve_skill, resolve_skills

    local = tmp_path / "local"
    shared = tmp_path / "shared"
    local.mkdir()
    shared.mkdir()
    _write_skill(shared, "alpha")
    _write_skill(local, "collide")
    _write_skill(shared, "collide")
    roots = [local, shared]

    names = ["alpha", "collide", "missing-one"]
    batched = resolve_skills(names, roots=roots)
    for name in names:
        single = resolve_skill(name, roots=roots)
        assert batched[name].status == single.status
        assert [c.skill_md for c in batched[name].candidates] == [
            c.skill_md for c in single.candidates
        ]
    assert batched["alpha"].status == "resolved"
    assert batched["collide"].status == "collision"
    assert batched["missing-one"].status == "missing"


# ── parse_frontmatter: UTF-8 BOM tolerance ─────────────────────────────────


class TestParseFrontmatterBOM:
    """A UTF-8 BOM (U+FEFF) on a Windows-saved SKILL.md must not defeat
    frontmatter parsing.

    Notepad and PowerShell ``>`` prepend a BOM when saving UTF-8;
    ``read_text(encoding="utf-8")`` (what ``_parse_skill_file`` uses) keeps
    it, so the bytes handed to ``parse_frontmatter`` start with a BOM ahead of
    the ``---`` fence. Before the fix the ``startswith("---")`` check returned
    False and the whole frontmatter was silently dropped — the skill loaded
    nameless, platform gating fell open, and env-var/config setup never fired.
    """

    SKILL = (
        "---\n"
        "name: my-skill\n"
        "description: Does a thing.\n"
        "platforms: [macos]\n"
        "metadata:\n"
        "  hermes:\n"
        "    config:\n"
        "      - key: my.key\n"
        "        description: A configured value\n"
        "---\n\n"
        "# My Skill\n\nBody text.\n"
    )

    def test_bom_frontmatter_matches_plain(self):
        plain_fm, plain_body = parse_frontmatter(self.SKILL)
        bom_fm, bom_body = parse_frontmatter("\ufeff" + self.SKILL)
        assert bom_fm == plain_fm
        assert bom_body == plain_body
        assert bom_fm["name"] == "my-skill"
        assert bom_fm["description"] == "Does a thing."




    def test_bom_platform_gating_regression(self):
        # The concrete harm: a macOS-only skill must stay hidden on non-macOS
        # whether or not the file carries a BOM. Empty frontmatter (the bug)
        # reads as "no platform restriction" and leaks the skill everywhere.
        with patch("agent.skill_utils.sys.platform", "win32"), patch(
            "agent.skill_utils.is_termux", return_value=False
        ):
            plain_fm, _ = parse_frontmatter(self.SKILL)
            bom_fm, _ = parse_frontmatter("\ufeff" + self.SKILL)
            assert skill_matches_platform(plain_fm) is False
            assert skill_matches_platform(bom_fm) is False


    def test_real_file_read_path(self, tmp_path):
        # End-to-end: write the file the way a Windows editor does (utf-8-sig
        # emits a BOM), read it the way _parse_skill_file does (plain utf-8),
        # and confirm the frontmatter survives the round trip.
        f = tmp_path / "SKILL.md"
        f.write_text(self.SKILL, encoding="utf-8-sig")
        raw = f.read_text(encoding="utf-8")
        assert raw.startswith("\ufeff")  # BOM really is present on disk
        fm, _ = parse_frontmatter(raw)
        assert fm["name"] == "my-skill"
        assert fm["platforms"] == ["macos"]


class TestBOMToleranceSiblingSites:
    """The BOM fix must cover every independent frontmatter parser, not just
    the canonical ``parse_frontmatter`` — several modules reimplement the
    ``---`` fence check locally."""

    SKILL = "---\nname: bom-skill\ndescription: Saved by Notepad\n---\n\n# Body\n"


    def test_prompt_builder_strips_bom_frontmatter(self):
        # A BOM'd context file (AGENTS.md etc.) must not leak raw
        # frontmatter into the system prompt.
        from agent.prompt_builder import _strip_yaml_frontmatter

        out = _strip_yaml_frontmatter("\ufeff---\nfoo: bar\n---\nBody text\n")
        assert out.strip() == "Body text"

    def test_blueprints_split_frontmatter_bom(self):
        # str.lstrip() does NOT strip U+FEFF (it is not whitespace), so the
        # pre-existing lstrip() in _split_frontmatter never covered it.
        from tools.blueprints import _split_frontmatter

        fm = _split_frontmatter("\ufeff---\nname: bp\n---\nbody")
        assert fm is not None
        assert fm.get("name") == "bp"
