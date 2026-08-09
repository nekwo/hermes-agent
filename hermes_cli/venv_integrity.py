"""Detects the venv-corruption shapes that a plain import probe reports only as
a symptom.

WHY THIS EXISTS (2026-08-09 incident). Mission Control refused to open behind a
destructive-repair gate. The runtime venv's ``jiter`` was corrupt in two ways at
once, and neither had a name:

1. **A package directory shadowing a single-file extension.** The compiled
   ``jiter.cp311-win_amd64.pyd`` sat *inside* a ``jiter/`` directory that had no
   ``__init__.py``, instead of at the site-packages root. Python resolved
   ``jiter`` as an empty **namespace package**: ``jiter.__file__`` was ``None``
   and ``jiter.from_json`` did not exist.
2. **Conflicting distribution metadata.** THREE dist-info dirs coexisted —
   ``jiter-0.12.0.dist-info``, ``jiter-0.13.0.dist-info``,
   ``jiter-0.15.0.dist-info`` — all rejected by pip with
   ``invalid metadata entry 'name'``. pip then believed 0.12.0 was installed and
   **silently no-op'd every** ``--force-reinstall``; ``pip uninstall`` failed
   with "RECORD file not found". Only deleting the directories by hand fixed it.

``runtime_environment._jiter_from_json_issue`` caught the *symptom* ("from_json
is unavailable") — true, but it points an operator at a reinstall that pip will
silently refuse to perform. The checks here name the *cause*, so the emitted
remediation is one the operator can actually execute.

Emitted issue shape is the established one — ``kind`` / ``package`` /
``summary`` — and every ``kind`` stays under the ``runtime_dependency_`` prefix
the launcher classifies as a dependency fault (see ``HermesRuntimeIssue`` /
``parseHermesRuntimeIssues`` in the launcher's
``mission_control_hermes_installer.dart``). ``package`` is always a bare
distribution name with no dots, because the launcher derives the reinstall
target as ``package.split('.').first``.

SCOPE. The metadata scan is deliberately name-scoped rather than whole-venv:
these issues are fatal to a turn (``assert_provider_health_for_persona`` raises
on any issue), so a false positive on an unrelated distribution would wedge
Mission Control. Callers pass the distributions that actually carry the runtime
— the provider SDKs plus the fragile compiled-extension core they depend on —
which is exactly the set tonight's fault lived in. :func:`venv_integrity_issues`
takes the name set as an argument, so widening it later is a call-site change,
not a rewrite.
"""

from __future__ import annotations

import importlib.util
import re
import sysconfig
from pathlib import Path
from typing import Callable, Iterable, Optional

# Distribution name -> the module name you actually import. Identical for most
# packages; the exceptions are the ones that would otherwise make us probe a
# module that does not exist (and report a permanently-missing dependency) or
# emit a dotted ``package`` the launcher would truncate to the wrong reinstall
# target.
_IMPORT_NAME: dict[str, str] = {
    "google-auth": "google.auth",
    "azure-identity": "azure.identity",
    "pydantic-core": "pydantic_core",
    "huggingface-hub": "huggingface_hub",
}

# Distributions whose importable form is a SINGLE-FILE compiled extension at the
# site-packages root (``jiter.cp311-win_amd64.pyd``, ``_pydantic_core...pyd``).
# Those are the ones an interrupted install can leave as an empty directory that
# silently resolves as a namespace package — the shape that produced tonight's
# "installed but ``from_json`` is missing".
_SINGLE_FILE_EXTENSIONS: frozenset[str] = frozenset({"jiter", "pydantic-core"})

_EXTENSION_SUFFIXES: frozenset[str] = frozenset({".pyd", ".so", ".dylib"})


def canonical_distribution(name: str) -> str:
    """PEP 503 canonical form: ``Zope_Interface`` and ``zope-interface`` agree."""
    return re.sub(r"[-_.]+", "-", str(name).strip()).lower()


def import_name_for(distribution: str) -> str:
    """The module name to probe for ``distribution``."""
    return _IMPORT_NAME.get(canonical_distribution(distribution), str(distribution))


def module_available(module: str) -> bool:
    """``find_spec`` that cannot raise.

    A dotted probe (``google.auth``, ``azure.identity``) imports its parent and
    raises ``ModuleNotFoundError`` when the parent is absent — the bare
    ``find_spec`` call the old code used would have propagated that out of a
    health check.
    """
    try:
        return importlib.util.find_spec(module) is not None
    except Exception:
        return False


def site_package_dirs() -> list[Path]:
    """Directories that hold this interpreter's installed distributions.

    ``purelib``/``platlib`` are the same directory on Windows and on most venvs;
    deduplicated so a distribution is not reported as conflicting with itself.
    """
    seen: list[Path] = []
    try:
        paths = sysconfig.get_paths()
    except Exception:  # pragma: no cover - defensive
        return seen
    for key in ("purelib", "platlib"):
        raw = paths.get(key)
        if not raw:
            continue
        try:
            resolved = Path(raw).resolve()
        except OSError:  # pragma: no cover - defensive
            continue
        if resolved not in seen:
            seen.append(resolved)
    return seen


def _metadata_dirs_by_distribution(directory: Path) -> dict[str, list[Path]]:
    """Map canonical distribution name -> its metadata dirs inside ``directory``."""
    grouped: dict[str, list[Path]] = {}
    try:
        children = sorted(directory.iterdir())
    except OSError:
        return grouped
    for child in children:
        if not child.is_dir():
            continue
        if child.suffix not in (".dist-info", ".egg-info"):
            continue
        # ``{escaped_name}-{version}.dist-info``; the escaped name never
        # contains ``-`` (PEP 427 replaces it with ``_``), so the first dash
        # ends the name. ``.egg-info`` may carry no version at all.
        raw_name = child.stem.split("-", 1)[0]
        grouped.setdefault(canonical_distribution(raw_name), []).append(child)
    return grouped


def _declared_name(metadata_dir: Path) -> tuple[Optional[str], Optional[str]]:
    """Read the distribution name a metadata dir declares.

    Returns ``(name, fault)``. ``fault`` is a short phrase describing why the
    metadata is unusable, or ``None`` when it parsed cleanly. Reproduces the
    strictness that made pip reject tonight's dirs: the header key must be
    exactly ``Name`` — a lowercase ``name:`` is what pip reports as
    ``invalid metadata entry 'name'``.
    """
    candidates = (metadata_dir / "METADATA", metadata_dir / "PKG-INFO")
    source = next((p for p in candidates if p.is_file()), None)
    if source is None:
        return None, "no METADATA/PKG-INFO file"
    try:
        text = source.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"metadata unreadable ({exc.__class__.__name__})"
    for line in text.splitlines():
        if not line.strip():
            break  # end of headers; body follows
        key, sep, value = line.partition(":")
        if not sep or key.lower().strip() != "name":
            continue
        if key.strip() != "Name":
            return None, f"invalid metadata entry {key.strip()!r} (must be 'Name')"
        return value.strip(), None
    return None, "metadata declares no Name"


def metadata_issues(
    distributions: Iterable[str],
    *,
    directories: Optional[Iterable[Path]] = None,
) -> list[dict[str, str]]:
    """Report conflicting or unreadable distribution metadata.

    Only the named ``distributions`` are inspected — see the module docstring on
    scope. Two faults are distinguished because they need different fixes:

    ``runtime_dependency_metadata_conflict``
        More than one metadata directory for one distribution in one
        site-packages tree. pip resolves the FIRST one and treats the package as
        already installed, so ``--force-reinstall`` succeeds while changing
        nothing. The only fix is to delete the directories first, which is what
        the summary says.

    ``runtime_dependency_metadata_unreadable``
        The metadata exists but does not declare a usable ``Name``. pip refuses
        it and reports the distribution as not installed, so ``pip uninstall``
        fails too.
    """
    dirs = list(directories) if directories is not None else site_package_dirs()
    wanted = {canonical_distribution(d) for d in distributions}
    issues: list[dict[str, str]] = []
    reported_conflict: set[str] = set()
    reported_unreadable: set[str] = set()

    for directory in dirs:
        grouped = _metadata_dirs_by_distribution(Path(directory))
        for canonical, metadata_dirs in sorted(grouped.items()):
            if canonical not in wanted:
                continue
            if len(metadata_dirs) > 1 and canonical not in reported_conflict:
                reported_conflict.add(canonical)
                names = ", ".join(sorted(p.name for p in metadata_dirs))
                uninstallable = [p.name for p in metadata_dirs if not (p / "RECORD").is_file()]
                record_note = (
                    f" {len(uninstallable)} of them have no RECORD file, so "
                    f"`pip uninstall` cannot remove them either."
                    if uninstallable
                    else ""
                )
                issues.append(
                    {
                        "kind": "runtime_dependency_metadata_conflict",
                        "package": canonical,
                        "summary": (
                            f"Runtime package {canonical} has {len(metadata_dirs)} conflicting "
                            f"metadata directories in {directory} ({names}). pip resolves the "
                            f"first one and reports {canonical} as already installed, so "
                            f"`pip install --force-reinstall {canonical}` silently does "
                            f"nothing.{record_note} Delete those directories, then run: "
                            f"pip install --force-reinstall --no-cache-dir {canonical}"
                        ),
                    }
                )
                continue
            for metadata_dir in metadata_dirs:
                _name, fault = _declared_name(metadata_dir)
                if fault is None or canonical in reported_unreadable:
                    continue
                reported_unreadable.add(canonical)
                issues.append(
                    {
                        "kind": "runtime_dependency_metadata_unreadable",
                        "package": canonical,
                        "summary": (
                            f"Runtime package {canonical} has unusable metadata in "
                            f"{metadata_dir.name}: {fault}. pip cannot read this "
                            f"distribution, so it neither reinstalls nor uninstalls it. "
                            f"Delete {metadata_dir}, then run: "
                            f"pip install --force-reinstall --no-cache-dir {canonical}"
                        ),
                    }
                )
    return issues


def shadowed_extension_issue(
    distribution: str,
    *,
    find_spec: Callable[[str], object] | None = None,
) -> Optional[dict[str, str]]:
    """Report a package directory shadowing a same-named extension module.

    The exact 2026-08-09 shape: ``jiter/`` exists with no ``__init__.py`` but
    contains ``jiter.cp311-win_amd64.pyd``. Python resolves ``jiter`` as an
    empty namespace package (``spec.origin is None``), every attribute lookup
    fails, and no amount of reinstalling fixes it while the stale directory and
    its metadata remain.

    Returns ``None`` when the module resolves normally, is genuinely absent
    (that is ``runtime_dependency_missing``, not corruption), or is a real
    namespace package with no trapped extension inside it.
    """
    module = import_name_for(distribution)
    probe = find_spec or importlib.util.find_spec
    try:
        spec = probe(module)
    except Exception:
        return None
    if spec is None:
        return None
    if getattr(spec, "origin", None) is not None:
        return None  # a real module or a package with __init__
    locations = list(getattr(spec, "submodule_search_locations", None) or [])
    leaf = module.rsplit(".", 1)[-1]
    for location in locations:
        directory = Path(location)
        try:
            children = sorted(directory.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_file():
                continue
            if child.suffix not in _EXTENSION_SUFFIXES:
                continue
            # ``jiter.cp311-win_amd64.pyd`` and the underscored private form
            # ``_pydantic_core.cp313-....pyd`` both belong to the distribution.
            if not (
                child.name.startswith(f"{leaf}.")
                or child.name.startswith(f"_{leaf}.")
            ):
                continue
            return {
                "kind": "runtime_dependency_shadowed",
                "package": canonical_distribution(distribution),
                "summary": (
                    f"Runtime package {module} resolves to an empty namespace package: "
                    f"{directory} has no __init__.py but contains the compiled extension "
                    f"{child.name}, which belongs at the site-packages root instead. "
                    f"Every attribute of {module} is missing while the import itself "
                    f"succeeds. Delete {directory}, then run: "
                    f"pip install --force-reinstall --no-cache-dir "
                    f"{canonical_distribution(distribution)}"
                ),
            }
    return None


def venv_integrity_issues(
    distributions: Iterable[str],
    *,
    directories: Optional[Iterable[Path]] = None,
    find_spec: Callable[[str], object] | None = None,
) -> list[dict[str, str]]:
    """All integrity faults for ``distributions``, in a stable order.

    Shadow checks run only for distributions that ship as a single-file
    compiled extension — the only shape where an empty directory can silently
    win the import — so a normal package directory is never mistaken for one.
    """
    names = [str(d) for d in distributions]
    issues = list(metadata_issues(names, directories=directories))
    for name in names:
        if canonical_distribution(name) not in _SINGLE_FILE_EXTENSIONS:
            continue
        issue = shadowed_extension_issue(name, find_spec=find_spec)
        if issue is not None:
            issues.append(issue)
    return issues
