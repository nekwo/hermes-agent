"""Pins for the 2026-08-09 venv-corruption detectors.

Every fixture below builds a site-packages tree on disk that is byte-different
between the healthy and the corrupt case, so a check that stopped looking at the
filesystem would go red rather than keep passing on a shared shape.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from hermes_cli.venv_integrity import (
    canonical_distribution,
    import_name_for,
    metadata_issues,
    module_available,
    shadowed_extension_issue,
    venv_integrity_issues,
)


def _dist_info(root: Path, name: str, version: str, *, header: str = "Name", record: bool = True) -> Path:
    directory = root / f"{name}-{version}.dist-info"
    directory.mkdir(parents=True)
    (directory / "METADATA").write_text(
        f"Metadata-Version: 2.1\n{header}: {name}\nVersion: {version}\n\nbody\n",
        encoding="utf-8",
    )
    if record:
        (directory / "RECORD").write_text("", encoding="utf-8")
    return directory


# ── metadata conflicts ────────────────────────────────────────────────────


def test_single_dist_info_is_clean(tmp_path):
    _dist_info(tmp_path, "jiter", "0.15.0")

    assert metadata_issues(["jiter"], directories=[tmp_path]) == []


def test_three_conflicting_dist_info_dirs_are_reported(tmp_path):
    # The exact live shape: pip resolved 0.12.0 and no-op'd every reinstall.
    _dist_info(tmp_path, "jiter", "0.12.0")
    _dist_info(tmp_path, "jiter", "0.13.0")
    _dist_info(tmp_path, "jiter", "0.15.0")

    issues = metadata_issues(["jiter"], directories=[tmp_path])

    assert len(issues) == 1
    issue = issues[0]
    assert issue["kind"] == "runtime_dependency_metadata_conflict"
    assert issue["package"] == "jiter"
    assert "3 conflicting" in issue["summary"]
    for version in ("0.12.0", "0.13.0", "0.15.0"):
        assert f"jiter-{version}.dist-info" in issue["summary"]
    # The whole point of naming the cause is that the obvious fix does not work.
    assert "silently does nothing" in issue["summary"]


def test_conflict_summary_calls_out_missing_record_files(tmp_path):
    _dist_info(tmp_path, "jiter", "0.12.0", record=False)
    _dist_info(tmp_path, "jiter", "0.15.0", record=False)

    summary = metadata_issues(["jiter"], directories=[tmp_path])[0]["summary"]

    # `pip uninstall` died with "RECORD file not found" on the live venv; an
    # operator told only to reinstall would hit that wall next.
    assert "2 of them have no RECORD" in summary


def test_conflicts_in_unrequested_distributions_are_ignored(tmp_path):
    _dist_info(tmp_path, "unrelated", "1.0")
    _dist_info(tmp_path, "unrelated", "2.0")

    assert metadata_issues(["jiter"], directories=[tmp_path]) == []


def test_same_distribution_in_two_trees_is_not_a_conflict(tmp_path):
    # purelib/platlib layering and --target stores are normal, not corruption.
    purelib = tmp_path / "purelib"
    platlib = tmp_path / "platlib"
    purelib.mkdir()
    platlib.mkdir()
    _dist_info(purelib, "jiter", "0.15.0")
    _dist_info(platlib, "jiter", "0.15.0")

    assert metadata_issues(["jiter"], directories=[purelib, platlib]) == []


def test_conflicting_names_are_matched_canonically(tmp_path):
    _dist_info(tmp_path, "pydantic_core", "2.0")
    _dist_info(tmp_path, "pydantic_core", "3.0")

    issues = metadata_issues(["pydantic-core"], directories=[tmp_path])

    assert [issue["package"] for issue in issues] == ["pydantic-core"]


# ── unreadable / invalid metadata ─────────────────────────────────────────


def test_lowercase_name_header_is_reported_as_unreadable(tmp_path):
    # pip's own words for this state were: invalid metadata entry 'name'.
    _dist_info(tmp_path, "jiter", "0.15.0", header="name")

    issues = metadata_issues(["jiter"], directories=[tmp_path])

    assert len(issues) == 1
    assert issues[0]["kind"] == "runtime_dependency_metadata_unreadable"
    assert issues[0]["package"] == "jiter"
    assert "invalid metadata entry 'name'" in issues[0]["summary"]


def test_dist_info_without_metadata_file_is_reported(tmp_path):
    (tmp_path / "jiter-0.15.0.dist-info").mkdir(parents=True)

    issues = metadata_issues(["jiter"], directories=[tmp_path])

    assert [issue["kind"] for issue in issues] == ["runtime_dependency_metadata_unreadable"]
    assert "no METADATA/PKG-INFO file" in issues[0]["summary"]


def test_metadata_without_a_name_header_is_reported(tmp_path):
    directory = tmp_path / "jiter-0.15.0.dist-info"
    directory.mkdir(parents=True)
    (directory / "METADATA").write_text("Metadata-Version: 2.1\nVersion: 0.15.0\n", encoding="utf-8")

    issues = metadata_issues(["jiter"], directories=[tmp_path])

    assert "declares no Name" in issues[0]["summary"]


# ── shadowed single-file extensions ───────────────────────────────────────


def _namespace_spec(*locations: Path):
    return SimpleNamespace(origin=None, submodule_search_locations=[str(p) for p in locations])


def test_package_dir_trapping_the_extension_is_reported(tmp_path):
    # The live shape: jiter/ with no __init__.py, holding the .pyd that belongs
    # one level up. `import jiter` succeeds; jiter.from_json does not exist.
    trapped = tmp_path / "jiter"
    trapped.mkdir()
    (trapped / "jiter.cp311-win_amd64.pyd").write_bytes(b"MZ")

    issue = shadowed_extension_issue("jiter", find_spec=lambda name: _namespace_spec(trapped))

    assert issue is not None
    assert issue["kind"] == "runtime_dependency_shadowed"
    assert issue["package"] == "jiter"
    assert "jiter.cp311-win_amd64.pyd" in issue["summary"]
    assert "no __init__.py" in issue["summary"]


def test_underscored_private_extension_counts_as_the_distributions_own(tmp_path):
    trapped = tmp_path / "pydantic_core"
    trapped.mkdir()
    (trapped / "_pydantic_core.cp312-win_amd64.pyd").write_bytes(b"MZ")

    issue = shadowed_extension_issue(
        "pydantic-core", find_spec=lambda name: _namespace_spec(trapped)
    )

    assert issue is not None
    assert issue["package"] == "pydantic-core"


def test_namespace_package_without_a_trapped_extension_is_clean(tmp_path):
    # The filenames here deliberately share the module's NAME prefix and differ
    # only in suffix — a type stub and a source shim are both legitimate
    # contents of a namespace directory. A detector that dropped the
    # compiled-extension suffix check would call this corruption.
    empty = tmp_path / "jiter"
    empty.mkdir()
    (empty / "jiter.pyi").write_text("def from_json(...): ...\n", encoding="utf-8")
    (empty / "jiter.py").write_text("from_json = None\n", encoding="utf-8")

    assert shadowed_extension_issue("jiter", find_spec=lambda name: _namespace_spec(empty)) is None


def test_real_package_holding_its_own_extension_is_clean(tmp_path):
    # A HEALTHY installed package: it has __init__.py (so ``origin`` is set)
    # and legitimately ships its compiled extension inside its own directory.
    # Only the missing __init__.py made the live jiter/ corrupt — a detector
    # that looked at directory contents alone would condemn this layout.
    package = tmp_path / "jiter"
    package.mkdir()
    (package / "__init__.py").write_text("from .jiter import from_json\n", encoding="utf-8")
    (package / "jiter.cp311-win_amd64.pyd").write_bytes(b"MZ")
    spec = SimpleNamespace(
        origin=str(package / "__init__.py"), submodule_search_locations=[str(package)]
    )

    assert shadowed_extension_issue("jiter", find_spec=lambda name: spec) is None


def test_plain_single_file_extension_at_site_packages_root_is_clean(tmp_path):
    # The correct layout the live venv had lost: the .pyd at the root, no dir.
    spec = SimpleNamespace(
        origin=str(tmp_path / "jiter.cp311-win_amd64.pyd"), submodule_search_locations=None
    )

    assert shadowed_extension_issue("jiter", find_spec=lambda name: spec) is None


def test_absent_module_is_not_corruption(tmp_path):
    assert shadowed_extension_issue("jiter", find_spec=lambda name: None) is None


# ── composition + helpers ─────────────────────────────────────────────────


def test_venv_integrity_issues_runs_shadow_checks_only_for_extension_dists(tmp_path):
    trapped = tmp_path / "anything"
    trapped.mkdir()
    (trapped / "anything.pyd").write_bytes(b"MZ")
    probes: list[str] = []

    def _probe(name):
        probes.append(name)
        return _namespace_spec(trapped)

    issues = venv_integrity_issues(["httpx", "jiter"], directories=[tmp_path], find_spec=_probe)

    # httpx is a pure-Python distribution: a directory named httpx/ is normal,
    # so probing it would manufacture a false corruption report.
    assert probes == ["jiter"]
    assert issues == []


def test_venv_integrity_issues_reports_metadata_and_shadow_together(tmp_path):
    _dist_info(tmp_path, "jiter", "0.12.0")
    _dist_info(tmp_path, "jiter", "0.15.0")
    trapped = tmp_path / "jiter"
    trapped.mkdir()
    (trapped / "jiter.cp311-win_amd64.pyd").write_bytes(b"MZ")

    kinds = [
        issue["kind"]
        for issue in venv_integrity_issues(
            ["jiter"], directories=[tmp_path], find_spec=lambda name: _namespace_spec(trapped)
        )
    ]

    assert kinds == ["runtime_dependency_metadata_conflict", "runtime_dependency_shadowed"]


def test_every_emitted_issue_matches_the_launcher_contract(tmp_path):
    _dist_info(tmp_path, "jiter", "0.12.0")
    _dist_info(tmp_path, "jiter", "0.15.0", header="name")
    trapped = tmp_path / "jiter"
    trapped.mkdir()
    (trapped / "jiter.cp311-win_amd64.pyd").write_bytes(b"MZ")

    issues = venv_integrity_issues(
        ["jiter"], directories=[tmp_path], find_spec=lambda name: _namespace_spec(trapped)
    )

    assert issues
    for issue in issues:
        assert set(issue) == {"kind", "package", "summary"}
        # parseHermesRuntimeIssues classifies on this prefix; anything else
        # renders as `unclassified` and loses the targeted-reinstall framing.
        assert issue["kind"].startswith("runtime_dependency")
        # HermesRuntimeIssue.packageDistribution is package.split('.').first —
        # a dotted package would send the launcher at the wrong distribution.
        assert "." not in issue["package"]
        assert "pip install" in issue["summary"]


def test_dotted_import_probe_survives_an_absent_parent_package():
    # find_spec("azure.identity") raises ModuleNotFoundError when `azure` is
    # absent; a health check must report False, not propagate.
    assert module_available("definitely_absent_parent_xyz.child") is False


def test_import_name_and_canonical_form():
    assert import_name_for("google-auth") == "google.auth"
    assert import_name_for("anthropic") == "anthropic"
    assert canonical_distribution("Pydantic_Core") == "pydantic-core"
