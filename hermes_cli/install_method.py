"""Code-scoped install provenance for downstream postinstall."""
from pathlib import Path
from typing import Optional

def _install_method_project_root(project_root: Optional[Path] = None) -> Path:
    """Resolve the directory that holds the *running code* (the install tree).

    This is the parent of ``hermes_cli/`` — i.e. the git checkout for source
    installs, ``/opt/hermes`` inside the published image. It is a property of
    the running interpreter, NOT of ``$HERMES_HOME``, which is why a
    code-scoped stamp here is immune to two installs sharing one data
    directory.
    """
    if project_root is not None:
        return project_root
    return Path(__file__).parent.parent.resolve()

def stamp_install_method(method: str, project_root: Optional[Path] = None) -> None:
    """Write the install method next to the running code (code-scoped stamp).

    The stamp lives in the install tree (``<install tree>/.install_method``),
    not in ``$HERMES_HOME``, so that two installs sharing one data directory
    do not overwrite each other's marker. See ``detect_install_method`` for
    the full rationale.

    Best-effort: if the install tree is read-only (e.g. the immutable
    ``/opt/hermes`` in the published image, which instead bakes the stamp at
    build time) the write silently no-ops and detection falls back to its
    other signals.
    """
    root = _install_method_project_root(project_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / ".install_method").write_text(method + "\n", encoding="utf-8")
    except OSError:
        pass
