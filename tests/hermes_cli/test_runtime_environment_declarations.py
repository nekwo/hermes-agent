"""Provider SDKs are DECLARED, so a missing one is named before token spend.

Before 2026-08-09 the Anthropic SDK was not declared anywhere: preflight said
the runtime was healthy and the first Anthropic call installed the SDK from
inside the live turn. These pins hold the declaration and the shape of the
diagnostic the launcher renders.
"""

from __future__ import annotations

import pytest

from hermes_cli import runtime_environment
from hermes_cli.runtime_environment import (
    install_command_for,
    missing_runtime_packages_for,
    required_packages_for,
    runtime_environment_status,
)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"provider": "anthropic"}, ["anthropic"]),
        ({"api_mode": "anthropic_messages"}, ["anthropic"]),
        ({"provider": "bedrock"}, ["boto3"]),
        ({"provider": "vertex"}, ["google-auth"]),
        ({"provider": "openai-codex", "api_mode": "codex_responses"}, ["openai"]),
    ],
)
def test_provider_sdks_are_declared(kwargs, expected):
    assert required_packages_for(**kwargs) == expected


def test_declared_names_are_distributions_not_import_paths():
    # `google.auth` is the import path; `google-auth` is what you pip install,
    # and it is also what the launcher turns into a reinstall target.
    assert required_packages_for(provider="vertex") == ["google-auth"]


def test_missing_anthropic_is_reported_before_token_spend(monkeypatch):
    monkeypatch.setattr(
        runtime_environment, "module_available", lambda module: module != "anthropic"
    )

    status = runtime_environment_status(["anthropic"])

    assert status.package_available == {"anthropic": False}
    assert len(status.issues) == 1
    issue = status.issues[0]
    assert issue["kind"] == "runtime_dependency_missing"
    assert issue["package"] == "anthropic"
    # The summary is what the launcher copies to the clipboard — it has to
    # carry the command, the interpreter, and the pinned version.
    assert "pip install anthropic==" in issue["summary"]
    assert status.executable in issue["summary"]


def test_missing_anthropic_reaches_readiness(monkeypatch):
    monkeypatch.setattr(
        runtime_environment, "module_available", lambda module: module != "anthropic"
    )

    assert missing_runtime_packages_for(provider="anthropic") == ["anthropic"]


def test_install_command_carries_the_provisioned_pin():
    # An operator following the diagnostic must land on the version the
    # provisioner would have installed, not on whatever PyPI serves today.
    from tools.lazy_deps import LAZY_DEPS

    expected = LAZY_DEPS["provider.anthropic"][0]
    assert install_command_for("anthropic") == f"pip install {expected}"


def test_integrity_issues_ride_the_same_health_surface(monkeypatch):
    monkeypatch.setattr(runtime_environment, "module_available", lambda module: True)
    corrupt = {
        "kind": "runtime_dependency_metadata_conflict",
        "package": "jiter",
        "summary": "three dist-info dirs",
    }
    monkeypatch.setattr(
        runtime_environment, "venv_integrity_issues", lambda dists: [corrupt]
    )

    status = runtime_environment_status(["anthropic"])

    assert corrupt in status.issues
    # Readiness must treat a corrupt-but-importable package as unusable, or the
    # turn starts and dies on its first API call instead.
    assert "jiter" in missing_runtime_packages_for(provider="anthropic")


def test_integrity_scan_covers_the_companions_a_provider_imports_through(monkeypatch):
    monkeypatch.setattr(runtime_environment, "module_available", lambda module: True)
    scanned: list[list[str]] = []

    def _capture(distributions):
        scanned.append(list(distributions))
        return []

    monkeypatch.setattr(runtime_environment, "venv_integrity_issues", _capture)

    runtime_environment_status(["anthropic"])

    # jiter is what actually broke: the Anthropic SDK decodes every response
    # through it, so scanning only the declared package would have missed it.
    assert scanned == [["anthropic", "jiter", "pydantic-core", "httpx", "certifi"]]


def test_absent_packages_are_not_also_scanned_for_corruption(monkeypatch):
    monkeypatch.setattr(runtime_environment, "module_available", lambda module: False)
    scanned: list[list[str]] = []
    monkeypatch.setattr(
        runtime_environment,
        "venv_integrity_issues",
        lambda distributions: scanned.append(list(distributions)) or [],
    )

    status = runtime_environment_status(["anthropic"])

    assert scanned == []
    assert [issue["kind"] for issue in status.issues] == ["runtime_dependency_missing"]


def test_structural_jiter_fault_suppresses_the_duplicate_symptom(monkeypatch):
    monkeypatch.setattr(runtime_environment, "module_available", lambda module: True)
    monkeypatch.setattr(
        runtime_environment,
        "venv_integrity_issues",
        lambda distributions: [
            {
                "kind": "runtime_dependency_shadowed",
                "package": "jiter",
                "summary": "shadowed",
            }
        ],
    )
    monkeypatch.setattr(
        runtime_environment,
        "_jiter_from_json_issue",
        lambda: {"kind": "runtime_dependency_corrupt", "package": "jiter.from_json", "summary": "s"},
    )

    packages = [issue["package"] for issue in runtime_environment_status(["anthropic"]).issues]

    # Two issues for one fault would make the launcher offer two fixes for it.
    assert packages == ["jiter"]


def test_jiter_symptom_now_covers_the_anthropic_sdk_too(monkeypatch):
    # Pre-fix this probe only ran when `openai` was importable, so an
    # anthropic-only runtime never checked the library that broke it.
    monkeypatch.setattr(
        runtime_environment, "module_available", lambda module: module == "anthropic"
    )
    monkeypatch.setattr(runtime_environment, "venv_integrity_issues", lambda dists: [])
    monkeypatch.setattr(
        runtime_environment,
        "_jiter_from_json_issue",
        lambda: {"kind": "runtime_dependency_corrupt", "package": "jiter.from_json", "summary": "s"},
    )

    packages = [issue["package"] for issue in runtime_environment_status(["anthropic"]).issues]

    assert packages == ["jiter.from_json"]
