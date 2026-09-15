"""The CLI's pre-argparse profile bootstrap — and the gate that says who may run it.

TWO THINGS LIVE HERE, and they are one mechanism.

``apply_profile_override()`` is the pre-parse that has to run before any hermes
module is importable: many modules cache ``HERMES_HOME`` at import time
(module-level constants), so ``--profile``/``-p`` is intercepted out of
``sys.argv`` and turned into an env var here, and the flag is stripped so
argparse never sees it. A sticky ``<root>/active_profile`` marker is the
fallback. None of that is new; it moved out of ``hermes_cli/main.py`` unchanged.

``is_hermes_cli_entrypoint()`` is why it moved. The pre-parse used to run from
``main.py``'s MODULE SCOPE, which made two facts true that nobody chose:

* **it parsed whatever argv the process happened to have.** Under pytest that
  argv is pytest's, so ``pytest … -p markdump`` was read as ``--profile
  markdump``, resolved nothing, and ``sys.exit(1)`` out of a collection import —
  reported as a bare ``INTERNALERROR> SystemExit: 1`` with no line naming the
  cause. Reproduced three times before it was understood.
* **it mutated the whole process's env.** No fixture is active during
  collection, so a collection-time import of ``hermes_cli.main`` read the
  OPERATOR's live ``<root>/active_profile`` and pointed ``HERMES_HOME`` at their
  live profile for the rest of the session. Every hermetic fixture in the tree
  runs one layer BELOW that window and cannot close it.

So importing a module must not do either, and the gate is what makes that
structural: the pre-parse now runs only when this process was STARTED as a
hermes CLI entrypoint. The gate is deliberately **entrypoint-based, not
env-var-based** — an env toggle would be a silent fallback that any process
could set, including the ones this exists to keep out, and "the tests set the
opt-out" is exactly how the window would grow back.

``is_hermes_cli_entrypoint`` answers POSITIVELY or not at all: it names the
console scripts hermes installs (pinned against ``pyproject.toml``'s
``[project.scripts]`` by ``tests/hermes_cli/test_cli_entrypoint_gate.py``, so a
new script cannot be added silently) and it recognises ``python -m
hermes_cli.main`` through the caller's own ``__name__``. Nothing else answers
true, and pytest can satisfy neither arm: its argv[0] is pytest's and it imports
``main`` under its real dotted name.

Import-safe and stdlib-only by contract, exactly as the module-scope block it
replaced had to be — every hermes import inside ``apply_profile_override`` is
deferred into the function for that reason.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: The console scripts ``pyproject.toml`` installs. Every one of them reaches
#: ``hermes_cli.main`` (``hermes`` IS it; ``hermes-acp`` and ``hermes-agent``
#: import it), so all three keep the pre-parse they have always had — this gate
#: was built to change nothing about a real invocation.
#:
#: Typed here rather than read from ``pyproject.toml`` because a wheel install
#: does not ship one; the equality against that table is asserted in the test
#: named in the module docstring, which is where a fourth script gets noticed.
HERMES_CONSOLE_SCRIPTS = frozenset({"hermes", "hermes-agent", "hermes-acp"})

#: Suffixes a launcher may hang on the script name. ``.exe`` is pip on Windows;
#: ``-script.py`` is older pip's Windows shim; ``.hermes-wrapped`` is what
#: ``makeWrapper`` leaves behind in a nix store, and the nix path is a shipped
#: deployment here (see ``pyproject.toml``'s uv2nix note), so dropping it is not
#: hypothetical tidiness.
_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".pyw", ".pyc", ".py")


def _argv0_basename(text: str) -> str:
    """The last path component of ``argv0`` under EITHER separator.

    ``argv[0]`` is a path in the syntax of the launcher that produced it, and
    the Windows launcher shapes this module exists to recognise
    (``C:\\venv\\Scripts\\hermes.exe``, ``…\\hermes-script.py``) are
    backslash-separated. ``os.path.basename`` only knows the RUNNING host's
    separator: on POSIX it hands the whole Windows path back, so
    :func:`entrypoint_name` returned ``c:\\venv\\scripts\\hermes`` and the gate
    compared THAT against the console-script names. Splitting on both is right
    on both hosts — a POSIX console script's path never carries a backslash,
    and a Windows one may carry either — and it is what lets the Windows
    shapes be asserted from the Linux CI runners instead of only on a
    developer's box.
    """

    tail = str(text or "").strip().strip('"')
    for separator in ("\\", "/"):
        tail = tail.rpartition(separator)[2]
    # A drive-relative argv[0] ("C:hermes.exe") has no separator at all;
    # ntpath.basename drops the drive, so this keeps parity with it.
    if len(tail) > 1 and tail[1] == ":" and tail[0].isalpha():
        tail = tail[2:]
    return tail


def entrypoint_name(argv0: str) -> str:
    """The bare program name behind ``argv[0]``, launcher decoration removed."""

    name = _argv0_basename(argv0)
    lowered = name.lower()
    for suffix in _EXECUTABLE_SUFFIXES:
        if lowered.endswith(suffix):
            name = name[: -len(suffix)]
            break
    if name.startswith("."):
        name = name[1:]
    for shim in ("-script", "-wrapped"):
        if name.lower().endswith(shim):
            name = name[: -len(shim)]
    return name.lower()


def is_hermes_cli_entrypoint(
    caller_module_name: str,
    *,
    argv0: str | None = None,
) -> bool:
    """Was THIS process started as a hermes CLI entrypoint?

    ``caller_module_name`` is the importing module's ``__name__``. It is the
    whole of the ``python -m hermes_cli.main`` / ``python path/to/main.py`` arm:
    runpy executes the module AS ``__main__`` in that case, and only in that
    case, so the caller reporting ``"__main__"`` is the process saying it IS the
    program being run. A test importing the module gets its real dotted name and
    cannot reach this arm even by accident.

    Otherwise the process entrypoint has to BE one of hermes' console scripts.
    ``argv[0]`` is what carries that: pip/uv/nix all put the script's own path
    there. Under pytest it is pytest's own path, which is not in the set and
    cannot be made to be by anything a test does short of rewriting argv[0] —
    at which point the process is lying about what it is, not accidentally
    tripping a check.
    """

    if caller_module_name == "__main__":
        return True
    if argv0 is None:
        argv0 = sys.argv[0] if sys.argv else ""
    return entrypoint_name(argv0) in HERMES_CONSOLE_SCRIPTS


import re

_PROFILE_NAME_RE = r"^[a-z0-9][a-z0-9_-]{0,63}$"

def _inside_mcp_add_args(argv: list, index: int) -> bool:
    """True once argv reaches `hermes mcp add ... --args <command argv>`.

    ``mcp add --args`` is command-argv passthrough. Flags after that point
    belong to the child MCP command (for example Docker MCP Toolkit's
    ``--profile``), not to Hermes' own profile selector.
    """
    try:
        mcp_index = argv.index("mcp", 0, index)
        argv.index("add", mcp_index + 1, index)
    except ValueError:
        return False
    return True

def _resolve_sudo_user_profile_env(name: str) -> str | None:
    """Resolve `sudo hermes -p <name>` against the invoking user's home.

    This runs before argparse, so `--run-as-user` is not available yet. For
    sudo invocations the best signal is SUDO_USER: root is only doing the
    privileged install/start action; the profile store belongs to the user.
    """
    if name == "default":
        return None
    from hermes_constants import sudo_invoker_default_home

    sudo_home = sudo_invoker_default_home()
    if sudo_home is None:
        return None
    candidate = sudo_home / "profiles" / name
    return str(candidate) if candidate.is_dir() else None

def _scan_profile_flag(argv: list) -> tuple:
    """Find -p/--profile/--profile= in argv -> (name, tokens_consumed, index).

    Historically the flag worked even after the subcommand (`hermes chat -p
    coder`), so scan broadly; stop at ``--`` and at the `mcp add --args`
    passthrough region. Values that can't be profile names (pytest's
    ``-p no:xdist``) are rejected so resolve_profile_env never sys.exits on them.
    """
    from hermes_cli._parser import top_level_value_flag_sets

    value_flags, optional_value_flags = top_level_value_flag_sets()
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--" or (arg == "--args" and _inside_mcp_add_args(argv, i)):
            break
        # The downstream rebind verb owns its target --profile argument.
        if arg in {"--profile", "-p"} or arg.startswith("--profile="):
            if any(argv[j:j + 3] == ["harness", "agent", "set-profile"] for j in range(i)):
                break
        if arg in {"--profile", "-p"} and i + 1 < len(argv):
            if re.match(_PROFILE_NAME_RE, argv[i + 1]):
                return argv[i + 1], 2, i
            break
        if arg.startswith("--profile="):
            return arg.split("=", 1)[1], 1, i
        takes_value = "=" not in arg and i + 1 < len(argv) and (
            arg in value_flags
            or (arg in optional_value_flags and not argv[i + 1].startswith("-"))
        )
        i += 2 if takes_value else 1
    return None, 0, None

def _under_gateway_supervisor(argv: list) -> bool:
    """A supervisor-launched gateway child must NOT follow the sticky active_profile.

    Each supervised slot has a fixed profile identity: named slots pass
    ``-p <name>`` or pin HERMES_HOME to the profile dir; a bare invocation
    means "the root HERMES_HOME profile". If a supervised default-profile
    child read active_profile, switching the active profile (dashboard,
    ``hermes profile use``) would silently redirect the default gateway into
    that profile — adopting its credentials and double-polling a Telegram
    token already owned by that profile's own gateway (#74872).

    Markers (see gateway/restart.py ``is_gateway_supervisor_process``):
    HERMES_SUPERVISED_CHILD (systemd unit / launchd plist / Windows task),
    HERMES_S6_SUPERVISED_CHILD (legacy s6 container), INVOCATION_ID (systemd
    service children only — consulted ONLY for gateway commands because it is
    inherited by every descendant of a systemd-launched process, e.g.
    self-hosted CI runners), HERMES_GATEWAY_EXTERNAL_SUPERVISOR (explicit
    opt-in). XPC_SERVICE_NAME is deliberately NOT consulted: interactive macOS
    terminals set it too.
    """
    if os.environ.get("HERMES_SUPERVISED_CHILD") or os.environ.get("HERMES_S6_SUPERVISED_CHILD"):
        return True
    is_gateway_cmd = next((a for a in argv if not a.startswith("-")), None) == "gateway"
    if is_gateway_cmd and os.environ.get("INVOCATION_ID"):
        return True
    return os.environ.get(
        "HERMES_GATEWAY_EXTERNAL_SUPERVISOR", ""
    ).strip().lower() in {"1", "true", "yes", "on"}

def _desktop_ssh_backend(argv: list) -> bool:
    """A Desktop-owned ``serve --ssh-session-token-file`` child has a fixed identity too.

    The Desktop client names the remote profile explicitly (``--profile <name>``, or none for
    the root home). Following the remote host's sticky ``active_profile`` instead silently
    re-homes the backend into a profile the UI never asked for, so Settings read one
    ``config.yaml`` and the user edits another (KC's "nothing sticks over SSH").
    """
    return "--ssh-session-token-file" in argv

def apply_profile_override() -> None:
    """Pre-parse --profile/-p and set HERMES_HOME before imports."""
    argv = sys.argv[1:]
    profile_name, consume, profile_index = _scan_profile_flag(argv)

    # HERMES_HOME already set with no explicit flag: trust it only when it
    # points at a specific profile dir ("profiles" as immediate parent). If it
    # points at the hermes root (systemd hardcodes HERMES_HOME=/root/.hermes)
    # we must still read active_profile — the user may have run
    # `hermes profile use` and the gateway should honour it (#22502).
    # Reset inherited receipts: this value describes this process only.
    os.environ["HERMES_PROFILE_RESOLUTION"] = "default"
    resolution = "flag" if profile_name is not None else "default"
    hermes_home_env = os.environ.get("HERMES_HOME", "")
    if profile_name is None and hermes_home_env and Path(hermes_home_env).parent.name == "profiles":
        os.environ["HERMES_PROFILE_RESOLUTION"] = "env_profile_dir"
        return

    if profile_name is None and not _under_gateway_supervisor(argv) and not _desktop_ssh_backend(argv):
        try:
            from hermes_constants import get_default_hermes_root

            active_path = get_default_hermes_root() / "active_profile"
            if active_path.exists():
                name = active_path.read_text(encoding="utf-8").strip()
                if name and name != "default":
                    profile_name = name  # consume stays 0: nothing to strip
                    resolution = "active_profile_marker"
        except (UnicodeDecodeError, OSError):
            pass  # corrupted file, skip

    if profile_name is None:
        return
    try:
        from hermes_cli.profiles import resolve_profile_env

        hermes_home = resolve_profile_env(profile_name)
    except FileNotFoundError as exc:
        hermes_home = _resolve_sudo_user_profile_env(profile_name)
        if not hermes_home:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        # A bug in profiles.py must NEVER prevent hermes from starting
        print(f"Warning: profile override failed ({exc}), using default", file=sys.stderr)
        return
    os.environ["HERMES_HOME"] = hermes_home
    os.environ["HERMES_PROFILE_RESOLUTION"] = resolution
    # Strip the flag from argv so argparse doesn't choke
    if consume > 0 and profile_index is not None:
        start = profile_index + 1  # +1 because argv is sys.argv[1:]
        sys.argv = sys.argv[:start] + sys.argv[start + consume :]
