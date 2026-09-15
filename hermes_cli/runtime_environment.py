"""Typed runtime-dependency health for the interpreter Hermes is running on.

Consumed by ``agent_runtime.provider_health`` (which raises before a turn spends
tokens), ``agent_runtime.profile_readiness``, and ``hermes harness status
--json`` — whose ``runtime_health.issues`` the Eternia launcher renders with a
copy button (``HermesRuntimeIssue`` / ``resolveHermesStartupHealth``). Every
emitted issue is ``{"kind", "package", "summary"}``; ``kind`` always starts with
``runtime_dependency`` so the launcher classifies it as a dependency fault, and
``package`` is a dotless distribution name so the launcher's
``package.split('.').first`` reinstall target is correct.

Because the summary is what the operator copies, it names the exact command to
run — never just the symptom.

DECLARE, DON'T LAZY-INSTALL. Provider SDKs are declared here so a missing one is
reported *before* the turn starts, with a command the operator can run. They
used to be installed on demand from inside the running turn instead
(``tools.lazy_deps.ensure("provider.anthropic")`` at the first Anthropic call),
which is what corrupted the runtime venv on 2026-08-09. That lane is now barred
at the chokepoint by ``tools.lazy_deps.deny_venv_installs``, armed for the whole
turn by ``agent_runtime.profile_runner.ProfileAgentRunner.run`` — this module is
the half that makes the refusal actionable instead of merely correct.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

from hermes_cli.venv_integrity import (
    canonical_distribution,
    import_name_for,
    module_available,
    venv_integrity_issues,
)


@dataclass(frozen=True, slots=True)
class RuntimeEnvironmentStatus:
    executable: str
    package_available: dict[str, bool]
    issues: list[dict[str, str]] = field(default_factory=list)


# Provider / api-mode -> the DISTRIBUTIONS (pip-installable names, not import
# names) that provider cannot run without. Anything listed here is checked
# before token spend; anything omitted falls back to being discovered mid-turn,
# which is precisely the failure mode this table exists to retire.
_PROVIDER_PACKAGE_REQUIREMENTS = {
    "openai-codex": ["openai"],
    "openai": ["openai"],
    "anthropic": ["anthropic"],
    "bedrock": ["boto3"],
    "vertex": ["google-auth"],
}
_API_MODE_PACKAGE_REQUIREMENTS = {
    "codex_responses": ["openai"],
    "responses": ["openai"],
    "anthropic_messages": ["anthropic"],
}

# Distribution -> the ``tools.lazy_deps`` feature carrying its pinned spec.
# Rendered into the remediation command so an operator following the diagnostic
# lands on the version provisioning would have installed, not on whatever is
# newest on PyPI today.
_LAZY_FEATURE_FOR_DISTRIBUTION = {
    "anthropic": "provider.anthropic",
    "boto3": "provider.bedrock",
    "google-auth": "provider.vertex",
    "azure-identity": "provider.azure_identity",
}

# Distributions that the declared ones import through, whose corruption
# presents as a fault in the declared package rather than in themselves. jiter
# is the 2026-08-09 case: both the OpenAI and the Anthropic SDK decode JSON
# through it, and a corrupt jiter breaks them while looking installed.
_COMPANION_DISTRIBUTIONS = {
    "openai": ("jiter", "pydantic-core", "httpx", "certifi"),
    "anthropic": ("jiter", "pydantic-core", "httpx", "certifi"),
    "boto3": ("botocore",),
}


def required_packages_for(*, provider: str | None = None, api_mode: str | None = None, model: str | None = None) -> list[str]:
    packages: list[str] = []
    for key in (str(provider or "").lower(), str(api_mode or "").lower()):
        for package in _PROVIDER_PACKAGE_REQUIREMENTS.get(key, []) + _API_MODE_PACKAGE_REQUIREMENTS.get(key, []):
            if package not in packages:
                packages.append(package)
    if not packages and model and "codex" in str(model).lower():
        packages.append("openai")
    return packages


def install_command_for(distribution: str) -> str:
    """The exact command an operator should run to install ``distribution``.

    Uses the ``tools.lazy_deps`` pin when the distribution has one. Falls back
    to the bare name if lazy_deps is unavailable (mobile core and stripped
    installs drop it).
    """
    canonical = canonical_distribution(distribution)
    spec = canonical
    feature = _LAZY_FEATURE_FOR_DISTRIBUTION.get(canonical)
    if feature:
        try:
            from tools.lazy_deps import LAZY_DEPS

            specs = LAZY_DEPS.get(feature) or ()
            if specs:
                spec = " ".join(specs)
        except Exception:
            pass
    return f"pip install {spec}"


def _integrity_scan_distributions(packages: list[str]) -> list[str]:
    """The declared distributions plus the companions they import through."""
    scanned: list[str] = []
    for package in packages:
        canonical = canonical_distribution(package)
        if canonical not in scanned:
            scanned.append(canonical)
        for companion in _COMPANION_DISTRIBUTIONS.get(canonical, ()):
            if companion not in scanned:
                scanned.append(companion)
    return scanned


def runtime_environment_status(packages: list[str]) -> RuntimeEnvironmentStatus:
    availability = {package: module_available(import_name_for(package)) for package in packages}
    issues = [
        {
            "kind": "runtime_dependency_missing",
            "package": canonical_distribution(package),
            "summary": (
                f"Missing runtime package: {package}. This interpreter "
                f"({sys.executable}) cannot import {import_name_for(package)}. "
                f"Install it: {install_command_for(package)}"
            ),
        }
        for package, available in availability.items()
        if not available
    ]
    # Corruption checks only make sense for packages that are present — an
    # absent one is already reported above, and probing it would emit a second
    # issue naming the same fix.
    present = [package for package, available in availability.items() if available]
    if present:
        issues.extend(venv_integrity_issues(_integrity_scan_distributions(present)))
    # jiter's from_json is the symptom that first surfaced the 2026-08-09
    # corruption. Kept alongside the structural checks above (which name the
    # cause) because it also catches shapes those cannot see — a truncated
    # .pyd, an ABI mismatch, a partially-written file.
    if availability.get("openai") or availability.get("anthropic"):
        jiter_issue = _jiter_from_json_issue()
        if jiter_issue and not any(issue["package"] == "jiter" for issue in issues):
            issues.append(jiter_issue)
    return RuntimeEnvironmentStatus(executable=sys.executable, package_available=availability, issues=issues)


def missing_runtime_packages_for(*, provider: str | None = None, api_mode: str | None = None, model: str | None = None) -> list[str]:
    packages = required_packages_for(provider=provider, api_mode=api_mode, model=model)
    if not packages:
        return []
    status = runtime_environment_status(packages)
    missing = [package for package, available in status.package_available.items() if not available]
    # A package that imports but is CORRUPT is not usable either — readiness has
    # to treat it as missing or the turn starts and dies on the first API call.
    # Structural faults name a bare distribution (``jiter``); the from_json
    # symptom names ``jiter.from_json``; both belong in this list.
    for issue in status.issues:
        package = issue.get("package")
        if not package or issue.get("kind") == "runtime_dependency_missing":
            continue
        if package not in missing:
            missing.append(package)
    return missing


def _jiter_from_json_issue() -> dict[str, str] | None:
    try:
        from jiter import from_json  # type: ignore
    except Exception:
        return _jiter_issue("Runtime package jiter is installed but from_json is unavailable")
    if not callable(from_json):
        return _jiter_issue("Runtime package jiter.from_json is not callable")
    return None


def _jiter_issue(summary: str) -> dict[str, str]:
    return {
        "kind": "runtime_dependency_corrupt",
        # NOTE: dotted on purpose and load-bearing. The launcher derives the
        # reinstall target as ``package.split('.').first`` -> ``jiter``, and
        # ``missing_runtime_packages_for`` matches on this exact string.
        "package": "jiter.from_json",
        "summary": (
            f"{summary}. The OpenAI and Anthropic SDKs decode every response "
            f"through it, so this breaks all provider traffic on this "
            f"interpreter ({sys.executable}). Reinstall it: "
            f"pip install --force-reinstall --no-cache-dir jiter"
        ),
    }
