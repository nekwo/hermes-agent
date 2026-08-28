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


def _provenance(shared, skill: str):
    """Read a promotion provenance record straight off disk.

    S54 deleted ``skill_promotion.promotion_provenance``, a read-back accessor
    with no production caller. These cases assert what the PRODUCTION WRITER
    put in the record, so the coverage is real and stays -- it just reads the
    file the writer names instead of going through a reader kept alive to be
    tested.
    """

    import json

    path = shared / ".provenance" / f"{skill.replace('/', '__')}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


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


# ── F1: bare slug landing on an existing category dir ────────────────────────


def test_classify_refuses_bare_slug_over_existing_category_dir(tmp_path):
    # Canonical already holds a CATEGORIZED skill, which makes the top-level
    # ``software-development`` a category dir WITHOUT its own SKILL.md. A realm
    # publishing a BARE skill of that same name must NOT classify promote_new —
    # os.replace onto the non-empty category dir would raise and abort the pull.
    shared = _shared()
    _write_package(shared, "software-development/hermes-agent", body="# Child\n")
    src = _write_package(tmp_path / "src", "software-development", body="# Bare\n")

    plan = classify_promotion("software-development", src)

    assert plan.action == "refuse_invalid"
    # The reason names the occupied canonical path.
    assert str(shared / "software-development") in plan.reason
    # Classification is pure — the child package is untouched.
    assert (shared / "software-development" / "hermes-agent" / "SKILL.md").is_file()
    assert not (shared / "software-development" / "SKILL.md").exists()


def test_classify_refuses_bare_slug_over_canonical_file(tmp_path):
    # A non-directory occupying the canonical slot is equally un-adoptable.
    shared = _shared()
    (shared / "occupied").write_text("i am a file, not a package\n", encoding="utf-8")
    src = _write_package(tmp_path / "src", "occupied", body="# Bare\n")

    plan = classify_promotion("occupied", src)

    assert plan.action == "refuse_invalid"


# ── F2: categorized child whose parent is an existing bare skill ─────────────


def test_classify_refuses_categorized_child_of_existing_bare_skill(tmp_path):
    # Canonical ``foo`` is a BARE skill package (has SKILL.md). A categorized
    # ``foo/bar`` whose leaf does not yet exist must NOT classify promote_new —
    # writing bar INSIDE foo would change foo's content hash and inject a new
    # resolvable skill with no gate (the trust-boundary defect).
    shared = _shared()
    foo = _write_package(shared, "foo", body="# Foo bare\n")
    foo_hash_before = _pkg_hash(foo)
    src = _write_package(tmp_path / "src", "foo/bar", body="# Bar child\n")

    plan = classify_promotion("foo/bar", src)

    assert plan.action == "refuse_invalid"
    assert str(shared / "foo") in plan.reason
    # Parent package is untouched by the pure classification.
    _content_hash_cache_clear()
    assert _pkg_hash(foo) == foo_hash_before
    assert not (shared / "foo" / "bar").exists()


def test_classify_categorized_child_ok_when_parent_is_pure_category(tmp_path):
    # The symmetric happy path: parent is a PURE category dir (no SKILL.md), so a
    # new categorized child is safe to adopt.
    shared = _shared()
    _write_package(shared, "software-development/existing", body="# Sibling\n")
    src = _write_package(tmp_path / "src", "software-development/newbie", body="# New\n")

    plan = classify_promotion("software-development/newbie", src)

    assert plan.action == "promote_new"
    assert plan.canonical_dir == shared / "software-development" / "newbie"


# ── F3: Windows reserved device names ────────────────────────────────────────


@pytest.mark.parametrize(
    "reserved_slug",
    [
        "con",
        "CON",
        "con.md",  # reserved check is on the STEM before the first dot
        "nul",
        "prn",
        "aux",
        "com1",
        "com9",
        "lpt1",
        "lpt9",
        "software-development/con",  # reserved child of a category
        "con/child",  # reserved category component
    ],
)
def test_classify_refuses_windows_reserved_slug(tmp_path, reserved_slug):
    _shared()
    src = _write_package(tmp_path / "src", "demo")
    plan = classify_promotion(reserved_slug, src)
    assert plan.action == "refuse_invalid"


@pytest.mark.parametrize("ok_slug", ["com0", "lpt0", "com10", "console", "aux-tool"])
def test_classify_accepts_non_reserved_lookalikes(tmp_path, ok_slug):
    # Only con/prn/aux/nul/com1-9/lpt1-9 are reserved — near-misses are fine.
    _shared()
    src = _write_package(tmp_path / "src", "demo")
    plan = classify_promotion(ok_slug, src)
    assert plan.action == "promote_new"


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

    prov = _provenance(shared, "demo")
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

    prov = _provenance(shared, "demo")
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


def test_execute_toctou_occupied_canonical_returns_refused_not_raise(tmp_path):
    # F1b: classify a clean promote_new, THEN let the world change so the
    # canonical slot is occupied by something the plan never anticipated (a
    # non-package dir holding a child skill). execute_promotion must return a
    # TYPED refusal — never raise (an os.replace onto the occupied dir would, and
    # in the pull lane that would abort the whole pull) — and touch nothing.
    shared = _shared()
    src = _write_package(tmp_path / "src", "demo", body="# Demo\n")
    plan = classify_promotion("demo", src)
    assert plan.action == "promote_new"

    occupied = shared / "demo"
    occupied.mkdir(parents=True)
    (occupied / "child").mkdir()
    (occupied / "child" / "SKILL.md").write_text(
        "---\nname: child\n---\n# c\n", encoding="utf-8"
    )
    before = _snapshot(shared)

    result = execute_promotion(plan, source={"kind": "realm", "realm_id": "r"})

    assert isinstance(result, PromotionResult)
    assert result.action == "refused"
    assert "install time" in result.reason
    # Canonical untouched, no provenance written.
    assert _snapshot(shared) == before
    assert _provenance(shared, "demo") is None


# ── F3: mirror-level reserved-name skip ─────────────────────────────────────


def test_is_windows_reserved_component_matrix():
    from agent_runtime.skill_promotion import is_windows_reserved_component

    for reserved in ("con", "CON", "Con", "con.md", "con.tar.gz", "nul", "prn",
                     "aux", "com1", "com9", "lpt1", "lpt9", "  con  "):
        assert is_windows_reserved_component(reserved), reserved
    for ok in ("con0", "com0", "com10", "lpt0", "console", "aux-tool", "nulled",
               "a.con", "", "software-development"):
        assert not is_windows_reserved_component(ok), ok


def test_mirror_skips_reserved_name_package(tmp_path, monkeypatch):
    # The pull MIRROR must skip a package whose path components include a Windows
    # reserved device name BEFORE any write (creating a ``con`` dir crashes the
    # mirror on Windows → pull DoS) and report it so the caller records it
    # refused. A real reserved dir/file can't be materialized on Windows at all
    # (the OS forbids it or silently redirects to the device), so this decouples
    # the mirror's skip-and-report machinery from OS quirks by treating a benign
    # marker as "reserved" — ``is_windows_reserved_component`` itself is proven
    # over the real device names by ``test_is_windows_reserved_component_matrix``.
    from agent_runtime import skill_promotion
    from agent_runtime.realm_sync import _mirror_realm_skill_inbox

    monkeypatch.setattr(
        skill_promotion,
        "is_windows_reserved_component",
        lambda c: str(c or "").split(".", 1)[0].strip().lower() == "blocked",
    )

    src = tmp_path / "skills"
    (src / "ok").mkdir(parents=True)
    (src / "ok" / "SKILL.md").write_text("---\nname: ok\n---\n# ok\n", encoding="utf-8")
    # A package literally named after the (marker) reserved device...
    (src / "blocked").mkdir()
    (src / "blocked" / "SKILL.md").write_text("---\nname: blocked\n---\n# b\n", encoding="utf-8")
    # ...and a healthy package that merely CONTAINS a reserved-named file — its
    # whole family must be quarantined (never half-written).
    (src / "carrier").mkdir()
    (src / "carrier" / "SKILL.md").write_text("---\nname: carrier\n---\n# c\n", encoding="utf-8")
    (src / "carrier" / "blocked.md").write_text("device\n", encoding="utf-8")

    inbox = tmp_path / "inbox"
    removed, reserved, tombstoned = _mirror_realm_skill_inbox(src, inbox)

    assert removed == []
    assert reserved == ["blocked", "carrier"]
    assert tombstoned == []
    # The healthy package IS mirrored; both reserved families are written NOWHERE.
    assert (inbox / "ok" / "SKILL.md").is_file()
    assert not (inbox / "blocked").exists()
    assert not (inbox / "carrier").exists()


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
    prov = _provenance(shared, "software-development/hermes-agent")
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


def test_plan_and_result_are_frozen_dataclasses(tmp_path):
    _shared()
    src = _write_package(tmp_path / "src", "demo")
    plan = classify_promotion("demo", src)
    assert isinstance(plan, PromotionPlan)
    with pytest.raises(Exception):
        plan.action = "mutated"  # type: ignore[misc]
