"""C6 tests for the realm skill inbox + hash-guarded promotion core.

Covers the pinned API in ``agent_runtime/skill_promotion.py`` and the C1
resolver-invisibility change to ``agent/skill_utils.EXCLUDED_SKILL_DIRS``.

Fixture pattern mirrors ``test_realm_sync.py``: the autouse
``isolate_agent_runtime_root`` (conftest) + the global ``_hermetic_environment``
point HERMES_HOME at a per-test tempdir, so ``get_shared_skills_dir()`` resolves
to an isolated ``<home>/shared/skills``.
"""

from pathlib import Path

import pytest

from agent.skill_utils import (
    _content_hash_cache_clear,
    resolve_skills,
    skill_package_content_hash,
)
from agent_runtime.skill_promotion import (
    PromotionPlan,
    PromotionResult,
    classify_promotion,
    execute_promotion,
    list_inbox_packages,
    promotion_provenance,
    realm_inbox_dir,
    realm_inbox_root,
)
from hermes_constants import get_shared_skills_dir


@pytest.fixture(autouse=True)
def _clean_hash_cache(monkeypatch):
    # A stray HERMES_SHARED_SKILLS from the ambient env would break isolation.
    monkeypatch.delenv("HERMES_SHARED_SKILLS", raising=False)
    _content_hash_cache_clear()
    yield
    _content_hash_cache_clear()


# ── helpers ────────────────────────────────────────────────────────────────


def _shared() -> Path:
    root = get_shared_skills_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_package(
    base: Path, slug: str, *, body: str = "# Body\n", extra: dict[str, str] | None = None
) -> Path:
    """Materialize a skill package at ``base/<slug parts>`` and return its dir."""

    pkg = base.joinpath(*slug.split("/"))
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "SKILL.md").write_text(
        f"---\nname: {slug.split('/')[-1]}\n---\n{body}", encoding="utf-8"
    )
    for rel, content in (extra or {}).items():
        target = pkg / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return pkg


def _pkg_hash(pkg: Path) -> str:
    return skill_package_content_hash(pkg, pkg / "SKILL.md")


def _snapshot(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    if root.exists():
        for path in sorted(root.rglob("*")):
            if path.is_file():
                out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


# ── classify ───────────────────────────────────────────────────────────────


def test_classify_promote_new(tmp_path):
    _shared()
    src = _write_package(tmp_path / "src", "demo", body="# Demo\n")
    plan = classify_promotion("demo", src)
    assert plan.action == "promote_new"
    assert plan.source_hash == _pkg_hash(src)
    assert plan.canonical_hash is None
    assert plan.canonical_dir == get_shared_skills_dir() / "demo"


def test_classify_noop_identical(tmp_path):
    shared = _shared()
    _write_package(shared, "demo", body="# Same\n")
    src = _write_package(tmp_path / "src", "demo", body="# Same\n")
    plan = classify_promotion("demo", src)
    assert plan.action == "noop_identical"
    assert plan.source_hash == plan.canonical_hash


def test_classify_hold_divergent(tmp_path):
    shared = _shared()
    canonical = _write_package(shared, "demo", body="# Canonical\n")
    src = _write_package(tmp_path / "src", "demo", body="# Source diverged\n")
    plan = classify_promotion("demo", src)
    assert plan.action == "hold_divergent"
    assert plan.source_hash and plan.canonical_hash
    assert plan.source_hash != plan.canonical_hash
    assert plan.canonical_hash == _pkg_hash(canonical)
    # Reason is hash-bearing so drift is legible without re-reading disk.
    assert plan.source_hash[:12] in plan.reason
    assert plan.canonical_hash[:12] in plan.reason


@pytest.mark.parametrize(
    "bad_slug",
    [
        "../evil",       # traversal
        "..",            # traversal
        ".hidden",       # dot-leading component
        "a/b/c",         # two-level (>1 category)
        "/abs",          # absolute
        "C:evil",        # drive-letter
        "cat/.hidden",   # dot-leading nested component
        "",              # empty
    ],
)
def test_classify_refuse_invalid_slug(tmp_path, bad_slug):
    _shared()
    src = _write_package(tmp_path / "src", "demo")
    plan = classify_promotion(bad_slug, src)
    assert plan.action == "refuse_invalid"


def test_classify_refuse_missing_skill_md(tmp_path):
    _shared()
    empty = tmp_path / "src" / "demo"
    empty.mkdir(parents=True)
    plan = classify_promotion("demo", empty)
    assert plan.action == "refuse_invalid"
    assert "SKILL.md" in plan.reason


# ── execute ────────────────────────────────────────────────────────────────


def test_execute_promote_new_writes_canonical_and_provenance(tmp_path):
    shared = _shared()
    src = _write_package(
        tmp_path / "src", "demo", body="# Demo\n", extra={"scripts/run.py": "print(1)\n"}
    )
    plan = classify_promotion("demo", src)
    result = execute_promotion(plan, source={"kind": "path", "path": str(src)})

    assert isinstance(result, PromotionResult)
    assert result.action == "promoted"
    assert result.archived_previous_to is None

    canonical = shared / "demo"
    assert (canonical / "SKILL.md").is_file()
    # Multi-file package copied faithfully.
    assert (canonical / "scripts" / "run.py").read_text(encoding="utf-8") == "print(1)\n"
    assert _pkg_hash(canonical) == plan.source_hash

    prov = promotion_provenance("demo")
    assert prov is not None
    assert prov["skill"] == "demo"
    assert prov["content_hash"] == plan.source_hash
    assert prov["source"] == {"kind": "path", "path": str(src)}
    assert "promoted_at" in prov
    assert "previous_hash" not in prov
    # Provenance sidecar lives OUTSIDE the package (never changes its hash).
    assert result.provenance_path == shared / ".provenance" / "demo.json"
    assert not (canonical / ".provenance").exists()


def test_execute_adopt_divergent_archives_previous_and_records_previous_hash(tmp_path):
    shared = _shared()
    v1 = _write_package(tmp_path / "src1", "demo", body="# V1\n")
    first = execute_promotion(
        classify_promotion("demo", v1), source={"kind": "path", "path": str(v1)}
    )
    assert first.action == "promoted"
    v1_hash = _pkg_hash(v1)

    _content_hash_cache_clear()
    v2 = _write_package(tmp_path / "src2", "demo", body="# V2 diverged content\n")
    plan = classify_promotion("demo", v2)
    assert plan.action == "hold_divergent"

    result = execute_promotion(
        plan, source={"kind": "path", "path": str(v2)}, adopt_divergent=True
    )
    assert result.action == "promoted"

    # Previous canonical preserved in the archive (never deleted).
    assert result.archived_previous_to is not None
    assert result.archived_previous_to.exists()
    assert ".archive" in result.archived_previous_to.parts
    archived_md = (result.archived_previous_to / "SKILL.md").read_text(encoding="utf-8")
    assert "# V1" in archived_md

    # Canonical now carries V2.
    assert "# V2 diverged content" in (shared / "demo" / "SKILL.md").read_text(encoding="utf-8")

    prov = promotion_provenance("demo")
    assert prov["previous_hash"] == v1_hash
    assert prov["content_hash"] == plan.source_hash


def test_execute_hold_divergent_without_adopt_writes_nothing(tmp_path):
    shared = _shared()
    _write_package(shared, "demo", body="# Canonical\n")
    # Establish a provenance sidecar so we can assert it is untouched too.
    src_same = _write_package(tmp_path / "seed", "demo", body="# Canonical\n")
    execute_promotion(
        # noop_identical write path is inert, but provenance is only written on a
        # real promote — so seed provenance via a fresh promote into a 2nd slug.
        classify_promotion("seed-skill", src_same),
        source={"kind": "path"},
    )

    before = _snapshot(shared)
    _content_hash_cache_clear()
    diverged = _write_package(tmp_path / "src", "demo", body="# Diverged\n")
    plan = classify_promotion("demo", diverged)
    assert plan.action == "hold_divergent"

    result = execute_promotion(plan, source={"kind": "path"}, adopt_divergent=False)
    assert result.action == "held"
    assert result.archived_previous_to is None
    assert result.provenance_path is None
    # Filesystem snapshot unchanged — canonical untouched, no archive, no sidecar.
    assert _snapshot(shared) == before


def test_execute_move_source_archives_source_on_promote(tmp_path):
    shared = _shared()
    # Profile-local duplicate that should be retired on promotion.
    src = _write_package(tmp_path / "profile" / "skills", "demo", body="# Demo\n")
    plan = classify_promotion("demo", src)
    result = execute_promotion(plan, source={"kind": "profile", "profile": "alice"}, move_source=True)

    assert result.action == "promoted"
    assert (shared / "demo" / "SKILL.md").is_file()
    # Source moved (retired) — no longer present at its origin.
    assert not src.exists()
    assert result.archived_previous_to is not None
    assert result.archived_previous_to.exists()
    assert "# Demo" in (result.archived_previous_to / "SKILL.md").read_text(encoding="utf-8")


def test_execute_move_source_dedupe_lane_on_noop(tmp_path):
    shared = _shared()
    _write_package(shared, "demo", body="# Canonical\n")
    before_canonical = _snapshot(shared / "demo")
    dup = _write_package(tmp_path / "profile" / "skills", "demo", body="# Canonical\n")

    plan = classify_promotion("demo", dup)
    assert plan.action == "noop_identical"
    result = execute_promotion(plan, source={"kind": "profile", "profile": "alice"}, move_source=True)

    assert result.action == "noop"
    # Redundant source archived; canonical bytes untouched.
    assert not dup.exists()
    assert result.archived_previous_to is not None and result.archived_previous_to.exists()
    assert _snapshot(shared / "demo") == before_canonical


def test_execute_dry_run_writes_nothing(tmp_path):
    shared = _shared()
    src = _write_package(tmp_path / "src", "demo", body="# Demo\n")
    plan = classify_promotion("demo", src)

    before = _snapshot(shared)
    result = execute_promotion(plan, source={"kind": "path"}, dry_run=True)
    assert result.action == "dry_run"
    assert result.archived_previous_to is None
    assert result.provenance_path is None
    assert _snapshot(shared) == before
    assert not (shared / "demo").exists()


def test_execute_refuses_invalid_plan_without_writes(tmp_path):
    shared = _shared()
    src = _write_package(tmp_path / "src", "demo")
    plan = classify_promotion("../evil", src)
    assert plan.action == "refuse_invalid"

    before = _snapshot(shared)
    result = execute_promotion(plan, source={"kind": "path"})
    assert result.action == "refused"
    assert _snapshot(shared) == before


def test_categorized_slug_round_trip(tmp_path):
    shared = _shared()
    src = _write_package(tmp_path / "src", "software-development/hermes-agent", body="# H\n")
    plan = classify_promotion("software-development/hermes-agent", src)
    assert plan.action == "promote_new"
    assert plan.canonical_dir == shared / "software-development" / "hermes-agent"

    result = execute_promotion(plan, source={"kind": "realm", "realm_id": "realm_x"})
    assert result.action == "promoted"
    assert (shared / "software-development" / "hermes-agent" / "SKILL.md").is_file()
    # Provenance filename flattens '/' → '__' and lives outside the package.
    assert result.provenance_path == shared / ".provenance" / "software-development__hermes-agent.json"
    prov = promotion_provenance("software-development/hermes-agent")
    assert prov is not None
    assert prov["skill"] == "software-development/hermes-agent"
    assert prov["source"] == {"kind": "realm", "realm_id": "realm_x"}


# ── C1 resolver invisibility ───────────────────────────────────────────────


def test_inbox_is_resolver_invisible(tmp_path):
    shared = _shared()
    # Canonical foo + a byte-different inbox mirror of the same id.
    _write_package(shared, "foo", body="# Canonical foo\n")
    inbox = realm_inbox_dir("realm_1")
    _write_package(inbox, "foo", body="# Realm foo (different)\n")

    resolution = resolve_skills(["foo"], roots=[shared])["foo"]
    # Exactly one candidate — the inbox copy neither resolves nor collides.
    assert resolution.status == "resolved"
    assert len(resolution.candidates) == 1
    assert resolution.candidate.skill_dir == shared / "foo"


def test_inbox_only_skill_is_missing_not_resolvable(tmp_path):
    shared = _shared()
    inbox = realm_inbox_dir("realm_1")
    _write_package(inbox, "solo", body="# Only in inbox\n")
    resolution = resolve_skills(["solo"], roots=[shared])["solo"]
    assert resolution.status == "missing"


def test_provenance_dir_does_not_break_resolution(tmp_path):
    shared = _shared()
    src = _write_package(tmp_path / "src", "bar", body="# Bar\n")
    execute_promotion(classify_promotion("bar", src), source={"kind": "path"})
    # A canonical skill named after a would-be provenance file still resolves.
    resolution = resolve_skills(["bar"], roots=[shared])["bar"]
    assert resolution.status == "resolved"
    assert len(resolution.candidates) == 1


# ── inbox enumeration + provenance read ─────────────────────────────────────


def test_realm_inbox_dir_tokenizes_realm_id():
    root = realm_inbox_root()
    assert root == get_shared_skills_dir() / ".realm_inbox"
    # Unsafe characters collapse to a single safe path component (no separators,
    # not a traversal component); idempotent for already-safe tokens.
    d1 = realm_inbox_dir("realm/../weird id")
    assert d1.parent == root
    assert len(d1.relative_to(root).parts) == 1
    assert d1.name not in (".", "..")
    assert realm_inbox_dir(d1.name) == d1


def test_list_inbox_packages_classifies_and_filters(tmp_path):
    shared = _shared()
    _write_package(shared, "shared-skill", body="# Canonical\n")

    # realm_a: a brand-new package + a categorized package.
    _write_package(realm_inbox_dir("realm_a"), "newbie", body="# New\n")
    _write_package(realm_inbox_dir("realm_a"), "software-development/tooling", body="# Cat\n")
    # realm_b: a package identical to canonical (converges).
    _write_package(realm_inbox_dir("realm_b"), "shared-skill", body="# Canonical\n")

    all_rows = list_inbox_packages()
    keys = {(row["realm"], row["skill"]): row for row in all_rows}
    assert set(keys) == {
        ("realm_a", "newbie"),
        ("realm_a", "software-development/tooling"),
        ("realm_b", "shared-skill"),
    }
    for row in all_rows:
        assert set(row) >= {"skill", "realm", "action", "source_hash", "canonical_hash"}
    assert keys[("realm_a", "newbie")]["action"] == "promote_new"
    assert keys[("realm_a", "newbie")]["canonical_hash"] is None
    assert keys[("realm_b", "shared-skill")]["action"] == "noop_identical"

    only_a = list_inbox_packages("realm_a")
    assert {row["realm"] for row in only_a} == {"realm_a"}
    assert len(only_a) == 2


def test_list_inbox_packages_empty_when_no_inbox():
    _shared()
    assert list_inbox_packages() == []
    assert list_inbox_packages("nope") == []


def test_promotion_provenance_none_for_unknown_and_invalid():
    _shared()
    assert promotion_provenance("never-promoted") is None
    assert promotion_provenance("../evil") is None
    assert promotion_provenance(".hidden") is None


def test_plan_and_result_are_frozen_dataclasses(tmp_path):
    _shared()
    src = _write_package(tmp_path / "src", "demo")
    plan = classify_promotion("demo", src)
    assert isinstance(plan, PromotionPlan)
    with pytest.raises(Exception):
        plan.action = "mutated"  # type: ignore[misc]
