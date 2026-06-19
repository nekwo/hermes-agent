from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeEnvironmentStatus:
    executable: str
    package_available: dict[str, bool]
    issues: list[dict[str, str]] = field(default_factory=list)


_PROVIDER_PACKAGE_REQUIREMENTS = {
    "openai-codex": ["openai"],
    "openai": ["openai"],
}
_API_MODE_PACKAGE_REQUIREMENTS = {
    "codex_responses": ["openai"],
    "responses": ["openai"],
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


def runtime_environment_status(packages: list[str]) -> RuntimeEnvironmentStatus:
    availability = {package: importlib.util.find_spec(package) is not None for package in packages}
    issues = [
        {"kind": "runtime_dependency_missing", "package": package, "summary": f"Missing runtime package: {package}"}
        for package, available in availability.items()
        if not available
    ]
    if availability.get("openai"):
        jiter_issue = _jiter_from_json_issue()
        if jiter_issue:
            issues.append(jiter_issue)
    return RuntimeEnvironmentStatus(executable=sys.executable, package_available=availability, issues=issues)


def missing_runtime_packages_for(*, provider: str | None = None, api_mode: str | None = None, model: str | None = None) -> list[str]:
    packages = required_packages_for(provider=provider, api_mode=api_mode, model=model)
    if not packages:
        return []
    status = runtime_environment_status(packages)
    missing = [package for package, available in status.package_available.items() if not available]
    if any(issue.get("package") == "jiter.from_json" for issue in status.issues):
        missing.append("jiter.from_json")
    return missing


def _jiter_from_json_issue() -> dict[str, str] | None:
    try:
        from jiter import from_json  # type: ignore
    except Exception:
        return {
            "kind": "runtime_dependency_corrupt",
            "package": "jiter.from_json",
            "summary": "Runtime package jiter is installed but from_json is unavailable",
        }
    if not callable(from_json):
        return {
            "kind": "runtime_dependency_corrupt",
            "package": "jiter.from_json",
            "summary": "Runtime package jiter.from_json is not callable",
        }
    return None
