"""Lazy dependency bootstrapper for non-Python runtime deps.

Detection and prompting live here in Python — not in install.sh — because:
  1. shutil.which() works on every platform; install.sh needs bash.
  2. Detection is instant; spawning bash for a "is node installed?" check is waste.
  3. Python controls the UX (rich prompts, non-interactive fallback, TTY detection).

install.sh is still the *installation* backend because it has 1900 lines of
battle-tested OS detection and package-manager logic (apt/brew/pacman/dnf/
zypper/Termux/…).  Reimplementing that in Python would be huge duplication.

Deps that degrade gracefully (ripgrep → grep fallback, ffmpeg → skip conversion)
don't need ensure_dependency wired in — only hard-fail sites do (TUI needs node,
browser tool needs agent-browser).
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from hermes_constants import agent_browser_runnable
from tools.environments.local import hermes_subprocess_env

_IS_WINDOWS = platform.system() == "Windows"

def _git_bash_available() -> bool:
    """True when a real Git Bash (not the WSL stub) is resolvable."""
    if _IS_WINDOWS:
        from tools.environments.local import _find_windows_git_bash
        return _find_windows_git_bash() is not None
    return shutil.which("bash") is not None


_DEP_CHECKS = {
    "node": lambda: shutil.which("node") is not None,
    "browser": lambda: (
        agent_browser_runnable(shutil.which("agent-browser"))
        or _has_system_browser()
        or _has_hermes_agent_browser()
    ),
    "ripgrep": lambda: shutil.which("rg") is not None,
    "ffmpeg": lambda: shutil.which("ffmpeg") is not None,
    "git": lambda: shutil.which("git") is not None,
    "git-bash": _git_bash_available,
}

_DEP_DESCRIPTIONS = {
    "node": "Node.js (required for browser tools and TUI)",
    "browser": "Browser engine (Chromium, for web browsing tools)",
    "ripgrep": "ripgrep (fast file search)",
    "ffmpeg": "ffmpeg (TTS voice messages)",
    "git": "Git (version control; Git for Windows also provides Git Bash)",
    "git-bash": "Git Bash (the shell Hermes runs terminal commands through on Windows)",
}


def _has_system_browser() -> bool:
    if _IS_WINDOWS:
        names = ("chrome", "msedge", "chromium")
    else:
        names = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
    for name in names:
        if shutil.which(name):
            return True
    return False


def _has_hermes_agent_browser() -> bool:
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    if _IS_WINDOWS:
        # npm -g --prefix puts .cmd shims directly in the prefix dir on Windows
        return (home / "node" / "agent-browser.cmd").is_file()
    # install.sh installs globally into $HERMES_HOME/node/bin/ via npm -g --prefix
    # Also check legacy node_modules/.bin/ path for git-clone installs.
    return (
        (home / "node" / "bin" / "agent-browser").is_file()
        or (home / "node_modules" / ".bin" / "agent-browser").is_file()
    )


def _find_install_script(
    package_dir: Path | None = None,
    repo_root: Path | None = None,
) -> tuple[Path | None, str | None]:
    """Locate the install script — bundled in wheel or in git checkout.

    On Windows, prefers install.ps1; on POSIX, prefers install.sh.
    Returns a (path, shell) tuple, or (None, None) if neither is found.
    """
    if package_dir is None:
        package_dir = Path(__file__).parent
    if repo_root is None:
        repo_root = package_dir.parent

    if _IS_WINDOWS:
        preferred = ("install.ps1", "powershell")
        fallback = ("install.sh", "bash")
    else:
        preferred = ("install.sh", "bash")
        fallback = ("install.ps1", "powershell")

    for script_name, shell in (preferred, fallback):
        bundled = package_dir / "scripts" / script_name
        if bundled.is_file():
            return bundled, shell
        repo = repo_root / "scripts" / script_name
        if repo.is_file():
            return repo, shell

    return None, None


def ensure_dependency(
    dep: str,
    interactive: bool = True,
) -> bool:
    """Ensure a non-Python dependency is available. Returns True if available."""
    check = _DEP_CHECKS.get(dep)
    if check is None:
        # Unknown dep — don't silently forward to install script.
        return False
    if check():
        return True

    script, shell = _find_install_script()
    if script is None:
        if interactive:
            desc = _DEP_DESCRIPTIONS.get(dep, dep)
            print(f"  {desc} is not installed and no install script was found.")
            print(f"  Install {dep} manually and try again.")
        return False

    if interactive and sys.stdin.isatty():
        desc = _DEP_DESCRIPTIONS.get(dep, dep)
        try:
            reply = input(f"{desc} is not installed. Install now? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if reply not in ("", "y", "yes"):
            return False

    if shell == "powershell":
        from hermes_constants import get_hermes_home
        ps_bin = shutil.which("powershell") or shutil.which("pwsh")
        if not ps_bin:
            if interactive:
                print("  PowerShell not found. Install PowerShell or run install.ps1 manually.")
            return False
        cmd = [
            ps_bin,
            # -NoProfile is not optional here, and it is what every other
            # PowerShell spawn in this repo already passes (claw, clipboard,
            # gateway, tools_config, voice_mode, tools/environments/local).
            # A user profile runs BEFORE -File does: on 2026-08-20 an operator
            # profile that upgrades 5.1 sessions to pwsh replaced this script
            # with an interactive shell, which inherited the caller's pipe
            # stdin and blocked forever — `hermes postinstall --yes --json`
            # hung for 20 minutes under the Eternia Launcher — and, because the
            # profile ended in `exit`, install.ps1 had never run at all.
            "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-File", str(script),
            "-Ensure", dep,
            "-HermesHome", str(get_hermes_home()),
        ]
    else:
        cmd = ["bash", str(script), "--ensure", dep]

    run_env = hermes_subprocess_env(inherit_credentials=False)
    run_env["IS_INTERACTIVE"] = "false"
    result = subprocess.run(
        cmd,
        env=run_env,
        # The env var above only ASKS the script to be non-interactive. This
        # makes it so: anything that reads stdin — the install script, a shell
        # profile, a package manager prompt — gets EOF instead of a pipe that
        # never closes. Applies to the bash branch too, which has the same
        # exposure through ~/.bashrc.
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return False

    # The install script may have just repaired a candidate this process
    # already probed and found non-runnable. ``agent_browser_runnable``
    # memoises its ``--version`` spawn per path, so the re-check below would
    # otherwise replay a verdict taken BEFORE the install.
    from hermes_constants import reset_agent_browser_probe_cache

    reset_agent_browser_probe_cache()

    if check:
        return check()
    return True


def ensure_git_bash(interactive: bool = True) -> "str | None":
    """Ensure the shell Hermes runs terminal commands through is provisioned.

    On POSIX bash is native, so this is a no-op that just returns the resolved
    ``bash`` path (or None if genuinely absent — extraordinarily rare).

    On Windows Hermes' terminal tool runs commands through Git Bash.  If it
    isn't found we fall back to ``install.ps1 -Ensure git`` (PortableGit) and
    re-resolve.  On success we persist the resolved path into the User-scope
    ``HERMES_GIT_BASH_PATH`` env var and the current process env so the agent
    finds bash in a fresh shell without hitting the System32 WSL stub.

    Returns the resolved bash path, or ``None`` on failure.
    """
    if not _IS_WINDOWS:
        return shutil.which("bash")

    from tools.environments.local import _find_windows_git_bash

    bash = _find_windows_git_bash()
    if bash is None:
        # No Git Bash yet — provision PortableGit via the install script.
        ensure_dependency("git", interactive=interactive)
        bash = _find_windows_git_bash()

    if bash:
        os.environ["HERMES_GIT_BASH_PATH"] = bash
        try:
            from hermes_cli import windows_env
            if windows_env.set_user_env("HERMES_GIT_BASH_PATH", bash):
                windows_env.broadcast_environment_change()
        except Exception:
            # Persistence is best-effort; the process env above still lets the
            # current session's agent find bash.
            pass

    return bash
