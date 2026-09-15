from pathlib import Path

import pytest

from agent_runtime.external_skill_links import (
    _create_dir_link,
    _is_managed_link,
    link_shared_skills_into_external_harnesses,
    shared_skill_names,
)


def _make_skill(root: Path, name: str, *, files: dict[str, str] | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n# {name}\n", encoding="utf-8")
    for rel, body in (files or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return d


@pytest.fixture
def shared_root(tmp_path: Path) -> Path:
    root = tmp_path / "shared" / "skills"
    root.mkdir(parents=True)
    _make_skill(root, "harness-mission-lead")
    _make_skill(root, "multi", files={"references/g.md": "g"})
    # a dotdir and a dir without SKILL.md must be ignored by discovery
    (root / ".curator_backups").mkdir()
    (root / "not-a-skill").mkdir()
    return root


def _ext(tmp_path: Path, name: str) -> Path:
    # A present "tool home" with a skills dir underneath.
    home = tmp_path / name
    home.mkdir(parents=True, exist_ok=True)
    return home / "skills"


def test_discovery_lists_only_real_skills(shared_root):
    assert shared_skill_names(shared_root) == ["harness-mission-lead", "multi"]


def test_links_shared_skills_into_external_dir(shared_root, tmp_path):
    ext = _ext(tmp_path, ".claude")
    report = link_shared_skills_into_external_harnesses(
        shared_root=shared_root, external_dirs=[ext]
    )
    assert report.ok
    assert report.count("linked") == 2
    for name in ("harness-mission-lead", "multi"):
        link = ext / name
        assert _is_managed_link(link)
        assert Path((link / "SKILL.md")).read_text(encoding="utf-8").strip().endswith(f"# {name}")
    # multi-file skill is reachable through the link
    assert (ext / "multi" / "references" / "g.md").read_text(encoding="utf-8") == "g"


def test_idempotent(shared_root, tmp_path):
    ext = _ext(tmp_path, ".claude")
    link_shared_skills_into_external_harnesses(shared_root=shared_root, external_dirs=[ext])
    report = link_shared_skills_into_external_harnesses(
        shared_root=shared_root, external_dirs=[ext]
    )
    assert report.ok
    assert report.count("already") == 2
    assert report.count("linked") == 0


def test_never_clobbers_a_real_entry(shared_root, tmp_path):
    ext = _ext(tmp_path, ".claude")
    ext.mkdir(parents=True)
    real = ext / "harness-mission-lead"
    real.mkdir()
    (real / "SKILL.md").write_text("local real copy", encoding="utf-8")
    report = link_shared_skills_into_external_harnesses(
        shared_root=shared_root, external_dirs=[ext]
    )
    assert report.count("skipped_real") == 1
    assert not _is_managed_link(real)
    assert (real / "SKILL.md").read_text(encoding="utf-8") == "local real copy"


def test_prunes_managed_link_when_shared_skill_removed(shared_root, tmp_path):
    ext = _ext(tmp_path, ".claude")
    link_shared_skills_into_external_harnesses(shared_root=shared_root, external_dirs=[ext])
    # Remove one shared skill, re-run: its stale link should be pruned.
    import shutil

    shutil.rmtree(shared_root / "multi")
    report = link_shared_skills_into_external_harnesses(
        shared_root=shared_root, external_dirs=[ext]
    )
    assert report.count("pruned") == 1
    assert not (ext / "multi").exists()
    assert _is_managed_link(ext / "harness-mission-lead")


def test_leaves_foreign_links_untouched(shared_root, tmp_path):
    ext = _ext(tmp_path, ".codex")
    ext.mkdir(parents=True)
    foreign_target = tmp_path / "somewhere-else"
    foreign_target.mkdir()
    (foreign_target / "SKILL.md").write_text("foreign", encoding="utf-8")
    _create_dir_link(ext / "foreign-skill", foreign_target)
    link_shared_skills_into_external_harnesses(shared_root=shared_root, external_dirs=[ext])
    # Foreign link points outside the shared root, so it must survive.
    assert _is_managed_link(ext / "foreign-skill")
    assert (ext / "foreign-skill" / "SKILL.md").read_text(encoding="utf-8") == "foreign"


def test_skips_absent_tool_home(shared_root, tmp_path):
    # Parent (tool home) does not exist → target skipped, not created.
    ext = tmp_path / "no-such-tool" / "skills"
    report = link_shared_skills_into_external_harnesses(
        shared_root=shared_root, external_dirs=[ext]
    )
    assert report.count("skipped_absent") == 1
    assert not ext.exists()
