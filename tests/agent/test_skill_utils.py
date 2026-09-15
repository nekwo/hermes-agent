"""Tests for agent/skill_utils.py."""

from unittest.mock import patch

from agent import skill_utils

import pytest
from pathlib import Path
from agent_runtime import skill_resolution

from agent.skill_utils import (
    extract_skill_config_vars,
    extract_skill_conditions,
    get_disabled_skill_names,
    get_external_skills_dirs,
    is_excluded_skill_path,
    is_external_skill_path,
    is_skill_support_path,
    iter_skill_index_files,
    parse_config_string_list,
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
    monkeypatch.setattr(skill_resolution, "get_shared_skills_dir", lambda: shared)
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
    monkeypatch.setattr(skill_resolution, "get_shared_skills_dir", lambda: shared)
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
        "session_platforms": [],
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
        "session_platforms": [],
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
        "session_platforms": [],
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


class TestParseConfigStringList:
    """#86661: `hermes config set` and JSON-mode editor saves store lists as
    quoted strings (e.g. '["a","b"]'). Treating such a string as a single name
    made curated disabled lists silently filter nothing."""

    def test_json_array_string_parses(self):
        assert parse_config_string_list('["skill-a","skill-b"]') == [
            "skill-a",
            "skill-b",
        ]

    def test_python_literal_array_string_parses(self):
        # `hermes config set` can persist single-quoted Python-literal forms.
        assert parse_config_string_list("['skill-a']") == ["skill-a"]

    def test_scalar_string_means_one_name(self):
        # #13026: a scalar string still names a single entry.
        assert parse_config_string_list("skill-a") == ["skill-a"]

    def test_real_list_passes_through(self):
        assert parse_config_string_list(["skill-a", "skill-b"]) == [
            "skill-a",
            "skill-b",
        ]
        assert parse_config_string_list(("skill-a",)) == ["skill-a"]

    def test_none_returns_empty(self):
        assert parse_config_string_list(None) == []

    def test_malformed_json_falls_back_to_single_name(self):
        assert parse_config_string_list('["skill-a"') == ['["skill-a"']

    def test_empty_array_string_returns_empty(self):
        assert parse_config_string_list("[]") == []


class TestDisabledSkillsJsonArrayString:
    """The skills.disabled setting must honor a JSON-array string form, not
    treat the whole string as one dead skill name (#86661)."""

    def test_get_disabled_skill_names_parses_json_array_string(
        self, tmp_path, monkeypatch
    ):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "skills:\n  disabled: '[\"skill-a\",\"skill-b\"]'\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        from agent import skill_utils

        getattr(skill_utils, "_raw_config_cache_clear", lambda: None)()

        assert get_disabled_skill_names() == {"skill-a", "skill-b"}

    def test_get_disabled_skill_names_scalar_string_still_single_name(
        self, tmp_path, monkeypatch
    ):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "skills:\n  disabled: 'hidden-skill'\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        from agent import skill_utils

        getattr(skill_utils, "_raw_config_cache_clear", lambda: None)()

        assert get_disabled_skill_names() == {"hidden-skill"}


def test_skill_config_home_vars_use_subprocess_home(tmp_path, monkeypatch):
    """``~`` / ``$HOME`` / ``${HOME}`` defaults resolve against the HOME tools receive, not the
    control process HOME; other variables keep normal expansion (#12260)."""
    from agent import skill_utils

    # A backslash in the home path must not be read as a regex-replacement escape.
    hermes_home = tmp_path / "da\\ta"
    subprocess_home = hermes_home / "home"
    subprocess_home.mkdir(parents=True)
    (hermes_home / "config.yaml").write_text("", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HOME", str(hermes_home))
    monkeypatch.setenv("TERMINAL_HOME_MODE", "profile")
    monkeypatch.setenv("PROJECT_ROOT", "/proj")
    monkeypatch.setenv("LEAF", "leaf")
    getattr(skill_utils, "_raw_config_cache_clear", lambda: None)()

    resolved = resolve_skill_config_values([
        {"key": "wiki.home_var", "default": "$HOME/wiki"},
        {"key": "wiki.braced_home", "default": "${HOME}/notes"},
        {"key": "wiki.tilde", "default": "~/scratch"},
        {"key": "wiki.other_var", "default": "${PROJECT_ROOT}/cache"},
        {"key": "wiki.tilde_var", "default": "~/$LEAF"},
    ])

    assert Path(resolved["wiki.home_var"]) == subprocess_home / "wiki"
    assert Path(resolved["wiki.braced_home"]) == subprocess_home / "notes"
    assert Path(resolved["wiki.tilde"]) == subprocess_home / "scratch"
    assert resolved["wiki.other_var"] == "/proj/cache"
    assert Path(resolved["wiki.tilde_var"]) == subprocess_home / "leaf"


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


def test_skill_root_registry_reuses_unchanged_roots_and_invalidates_only_changed_root(
    tmp_path,
):
    import os

    from agent.skill_utils import (
        _skill_root_registry,
        _skill_root_registry_cache_clear,
    )

    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    _write_skill(root_a, "alpha")
    _write_skill(root_b, "beta")
    _skill_root_registry_cache_clear()

    first_a = _skill_root_registry(root_a)
    first_b = _skill_root_registry(root_b)
    assert _skill_root_registry(root_a) is first_a
    assert _skill_root_registry(root_b) is first_b

    changed = root_a / "alpha" / "SKILL.md"
    changed.write_text("---\nname: renamed-alpha\n---\nchanged\n", encoding="utf-8")
    os.utime(changed, ns=(1, 6_000_000_000))

    assert _skill_root_registry(root_a) is not first_a
    assert _skill_root_registry(root_b) is first_b


def test_cached_skill_registry_preserves_root_precedence_and_profile_classification(
    tmp_path, monkeypatch
):
    from agent import skill_utils

    local = tmp_path / "local"
    shared = tmp_path / "shared"
    local.mkdir()
    shared.mkdir()
    _write_skill(local, "same")
    _write_skill(shared, "same")
    skill_utils._skill_root_registry_cache_clear()

    monkeypatch.setattr(skill_resolution, "get_skills_dir", lambda: local)
    monkeypatch.setattr(skill_resolution, "get_shared_skills_dir", lambda: shared)
    first = skill_utils.resolve_skills(["same"], roots=[local, shared])["same"]
    assert first.status == "collision"
    assert [candidate.root for candidate in first.candidates] == [local, shared]
    assert [candidate.source_kind for candidate in first.candidates] == [
        "profile_local",
        "shared_core",
    ]

    # Reuse the same physical registries under another profile classification;
    # source metadata is projected per call, never cached into the root entry.
    other_profile = tmp_path / "other-profile"
    monkeypatch.setattr(skill_resolution, "get_skills_dir", lambda: other_profile)
    second = skill_utils.resolve_skills(["same"], roots=[shared, local])["same"]
    assert [candidate.root for candidate in second.candidates] == [shared, local]
    assert [candidate.source_kind for candidate in second.candidates] == [
        "shared_core",
        "external",
    ]


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
        # The concrete harm: a macOS-only skill must be gated identically
        # whether or not the file carries a BOM. Empty frontmatter (the bug)
        # reads as "no platform restriction" and leaks the skill everywhere,
        # i.e. it would answer True on every host. Compare against the real
        # host's verdict instead of faking Windows — the fake only stood in
        # for "some non-macOS host", which the CI host already is.
        import sys

        expected = sys.platform == "darwin"
        with patch("agent.skill_utils.is_termux", return_value=False):
            plain_fm, _ = parse_frontmatter(self.SKILL)
            bom_fm, _ = parse_frontmatter("\ufeff" + self.SKILL)
            assert skill_matches_platform(plain_fm) is expected
            assert skill_matches_platform(bom_fm) is expected


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


# ── chat-turn-prep Stage 8: one registry walk per root per turn (CP-4, CP-5) ──
#
# The number these defend, from the 2026-09-08 CP-9 read of nine live Windows
# turns: skill work was 74–89 % of the pre-admit span (median 88.2 %), with
# `observability_skill_rows_ms` at 157–547 ms against a 30 ms target.
#
# What makes the walk expensive is NOT a cache miss. `_skill_root_registry`'s
# process cache is validated BY fingerprint, so reaching it at all re-runs
# `iter_skill_index_files`, a whole-root `rglob("*.md")` and a `stat` per path;
# only the frontmatter parse is skipped on a hit. So these count WALKS, through
# `skill_root_walks_this_thread`, and never wall-clock.


def test_one_registry_fingerprint_walk_per_root_per_turn(tmp_path, monkeypatch):
    """A turn's four resolution sites walk each root ONCE between them.

    The four, as the audit found them: the preload policy's
    ``required_preload_skill_ids``; the observability resolver's batch
    ``resolve_skills``; and — the one the plan's §0.3 did not name — the per-NAME
    ``resolve_skill`` behind every ``used_skills`` receipt, which is unbatched
    and therefore walks once per name.

    Sharing one map across all of them is the stage. The assertion is exact
    (``== len(roots)``) rather than "fewer", because "fewer" would pass a change
    that merely batched two of the four.
    """

    local = tmp_path / "local"
    shared = tmp_path / "shared"
    _write_skill(local, "alpha", load_policy="required_preload")
    _write_skill(shared, "beta")
    roots = [local, shared]
    monkeypatch.setattr(skill_utils, "get_all_skills_dirs", lambda: list(roots))
    skill_utils._skill_root_registry_cache_clear()

    # One turn: one map, handed to every site.
    registries: dict = {}
    skill_utils.reset_skill_root_walks_for_tests()

    skill_utils.required_preload_skill_ids(
        ["alpha"], surface="mission_chat", _root_registries=registries
    )
    skill_utils.resolve_skills(["alpha", "beta"], _root_registries=registries)
    for name in ("alpha", "beta", "alpha", "beta"):
        skill_utils.resolve_skill(name, _root_registries=registries)

    walks = skill_utils.skill_root_walks_this_thread()
    assert walks == len(roots), (
        f"one turn must walk each root exactly once; walked {walks} times for "
        f"{len(roots)} roots"
    )

    # A SECOND turn is a second map, and must re-validate the filesystem — the
    # freshness half of CP-4a. "Zero additional walks" is within a turn, never
    # forever.
    skill_utils.reset_skill_root_walks_for_tests()
    skill_utils.resolve_skills(["alpha"], _root_registries={})
    assert skill_utils.skill_root_walks_this_thread() == len(roots), (
        "a later turn must re-stat its roots; a memo that outlived the turn "
        "would be a staleness window, which CP-4 refuses"
    )


def test_unshared_resolution_still_walks_per_site(tmp_path, monkeypatch):
    """The control that gives the test above its meaning.

    Without a shared map every site walks for itself. This is the pre-Stage-8
    behaviour, and it is pinned so the counter cannot silently start counting
    something cheaper.
    """

    local = tmp_path / "local"
    _write_skill(local, "alpha")
    monkeypatch.setattr(skill_utils, "get_all_skills_dirs", lambda: [local])
    skill_utils._skill_root_registry_cache_clear()

    skill_utils.reset_skill_root_walks_for_tests()
    skill_utils.resolve_skills(["alpha"])
    skill_utils.resolve_skill("alpha")
    skill_utils.resolve_skill("alpha")
    assert skill_utils.skill_root_walks_this_thread() == 3, (
        "each unshared site pays its own walk — if this drops, the counter is "
        "measuring calls rather than filesystem work"
    )


def test_next_turn_manifest_add_edit_delete_and_org_flip_invalidate(
    tmp_path, monkeypatch
):
    """Sharing must not outlive the turn: the next turn sees real changes.

    Add, edit and delete, each read on a FRESH map the way a new turn would,
    with an unrelated root left alone throughout. Alias/collision behaviour is
    asserted across the change so sharing cannot quietly flatten two candidates
    into one.
    """

    local = tmp_path / "local"
    shared = tmp_path / "shared"
    _write_skill(local, "alpha")
    _write_skill(shared, "beta")
    roots = [local, shared]
    monkeypatch.setattr(skill_utils, "get_all_skills_dirs", lambda: list(roots))
    skill_utils._skill_root_registry_cache_clear()

    first = skill_utils.resolve_skills(["alpha", "beta", "gamma"], _root_registries={})
    assert first["alpha"].status == "resolved"
    assert first["gamma"].status == "missing"

    # ADD, on the next turn's own map.
    _write_skill(local, "gamma")
    after_add = skill_utils.resolve_skills(["gamma"], _root_registries={})
    assert after_add["gamma"].status == "resolved", "an added skill must appear"

    # COLLISION: the same name in a second root is still two candidates.
    _write_skill(shared, "gamma")
    after_collision = skill_utils.resolve_skills(["gamma"], _root_registries={})
    assert after_collision["gamma"].status == "collision", (
        "a shared walk must not flatten a collision into a silent winner"
    )

    # DELETE one side; the collision resolves back to a single candidate.
    (shared / "gamma" / "SKILL.md").unlink()
    (shared / "gamma").rmdir()
    after_delete = skill_utils.resolve_skills(["gamma"], _root_registries={})
    assert after_delete["gamma"].status == "resolved"

    # The unrelated root was never disturbed by any of it.
    assert skill_utils.resolve_skills(["beta"], _root_registries={})["beta"].status == (
        "resolved"
    )


def test_a_shared_map_is_keyed_by_root_so_lanes_with_different_roots_are_safe(
    tmp_path, monkeypatch
):
    """CP-5a: the two lanes may enumerate DIFFERENT root lists.

    ``mission_chat_prompt_observability`` runs inside ``persona_profile_scope``
    and the context builder does not, so for a persona whose profile is not the
    ambient one the lists can differ. The map is keyed by RESOLVED ROOT PATH,
    which is what makes the hand-off safe without the lanes having to agree: a
    root both lanes see is walked once, a root only one lane sees is walked by
    that lane, and no lane is served a root it did not ask for.
    """

    a = tmp_path / "a"
    b = tmp_path / "b"
    _write_skill(a, "alpha")
    _write_skill(b, "beta")
    skill_utils._skill_root_registry_cache_clear()

    registries: dict = {}
    skill_utils.reset_skill_root_walks_for_tests()

    # Lane one sees only `a`.
    first = skill_utils.resolve_skills(["alpha"], roots=[a], _root_registries=registries)
    assert first["alpha"].status == "resolved"
    assert skill_utils.skill_root_walks_this_thread() == 1

    # Lane two sees `a` and `b`: it reuses `a` and walks only the new root.
    second = skill_utils.resolve_skills(
        ["alpha", "beta"], roots=[a, b], _root_registries=registries
    )
    assert second["alpha"].status == "resolved"
    assert second["beta"].status == "resolved"
    assert skill_utils.skill_root_walks_this_thread() == 2, (
        "the second lane must walk only the root the first had not already taken"
    )

    # And a lane restricted to `b` is not handed `a`'s skills.
    third = skill_utils.resolve_skills(["alpha"], roots=[b], _root_registries=registries)
    assert third["alpha"].status == "missing", (
        "a shared map must never widen a lane's root list"
    )
