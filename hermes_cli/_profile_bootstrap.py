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


def apply_profile_override() -> None:
    """Pre-parse --profile/-p and set HERMES_HOME before imports."""
    argv = sys.argv[1:]
    profile_name = None
    consume = 0
    profile_index = None

    def _inside_mcp_add_args(index: int) -> bool:
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

    def _inside_harness_agent_set_profile(index: int) -> bool:
        """True when ``--profile`` belongs to the Harness rebind verb.

        ``harness agent set-profile`` deliberately owns a required
        ``--profile`` argument naming the target persona profile.  Consuming
        that argument here turns a valid rebind into an argparse error before
        the Harness operator can validate or apply it.
        """
        return any(
            argv[harness_index : harness_index + 3]
            == ["harness", "agent", "set-profile"]
            for harness_index in range(index)
        )

    def _resolve_sudo_user_profile_env(name: str) -> str | None:
        """Resolve `sudo hermes -p <name>` against the invoking user's home.

        `apply_profile_override()` runs before argparse, so `--run-as-user`
        is not available yet. For sudo invocations, the best available signal
        is SUDO_USER: root is only doing the privileged install/start action,
        while the profile store normally belongs to the user who invoked sudo.
        """
        if name == "default":
            return None
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            return None
        sudo_user = os.environ.get("SUDO_USER", "").strip()
        if not sudo_user or sudo_user == "root":
            return None

        try:
            import pwd

            home = Path(pwd.getpwnam(sudo_user).pw_dir)
        except Exception:
            return None

        candidate = home / ".hermes" / "profiles" / name
        try:
            if candidate.is_dir():
                return str(candidate)
        except OSError:
            return None
        return None

    # 1. Check for explicit -p / --profile flag. Historically this worked even
    # after the subcommand (`hermes chat -p coder`), so keep scanning broadly.
    # The exception is command-argv passthrough regions such as `mcp add --args`.
    value_flags = {
        "-z", "--oneshot",
        "-m", "--model",
        "--provider",
        "-t", "--toolsets",
        "-r", "--resume",
        "-s", "--skills",
        "--usage-file",
    }
    optional_value_flags = {"-c", "--continue"}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            break
        if arg == "--args" and _inside_mcp_add_args(i):
            break
        if (
            arg == "--profile" or arg.startswith("--profile=")
        ) and _inside_harness_agent_set_profile(i):
            break
        if arg in {"--profile", "-p"} and i + 1 < len(argv):
            profile_name = argv[i + 1]
            consume = 2
            profile_index = i
            break
        if arg.startswith("--profile="):
            profile_name = arg.split("=", 1)[1]
            consume = 1
            profile_index = i
            break
        if "=" not in arg and arg in value_flags and i + 1 < len(argv):
            i += 2
        elif (
            "=" not in arg
            and arg in optional_value_flags
            and i + 1 < len(argv)
            and not argv[i + 1].startswith("-")
        ):
            i += 2
        else:
            i += 1

    # 1b. Reject values that can't be valid profile names (e.g. pytest's
    # "-p no:xdist" would be misread as profile "no:xdist" otherwise).
    # Mirrors hermes_cli.profiles._PROFILE_ID_RE so we never call
    # resolve_profile_env() with a value it must reject + sys.exit on.
    if profile_name is not None and consume == 2:
        import re as _re

        if not _re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", profile_name):
            profile_name = None
            consume = 0
            profile_index = None

    # 1.5 If HERMES_HOME is already set and no explicit flag was given, trust it
    # only when it already points to a specific profile directory.  The
    # distinguishing heuristic: a profile path has "profiles" as its immediate
    # parent directory name (e.g. ~/.hermes/profiles/coder or
    # /opt/data/profiles/coder).  If HERMES_HOME points to the hermes root
    # instead (e.g. systemd hardcodes HERMES_HOME=/root/.hermes), we must
    # still read active_profile — the user may have switched profiles via
    # `hermes profile use` and the gateway should honour that choice.
    # See issue #22502.
    # Stamped unconditionally BEFORE any rung can answer, so the value always
    # describes THIS process. Without the reset, a child inherits its parent's
    # answer and the receipt reports the parent's rung — the exact confusion the
    # receipt exists to remove.
    os.environ["HERMES_PROFILE_RESOLUTION"] = "default"

    hermes_home_env = os.environ.get("HERMES_HOME", "")
    if profile_name is None and hermes_home_env:
        if Path(hermes_home_env).parent.name == "profiles":
            # Record WHICH rung answered, additively. This early return is the
            # one that skips the sticky marker entirely, so a child spawned from
            # a profile-shaped env stays on that profile forever and nothing
            # downstream could tell that apart from an explicit choice. The
            # gateway reads this back at boot (hermes_cli/gateway_home_receipt).
            # Spelled inline rather than imported: this pre-parse runs BEFORE any
            # hermes module is importable, which is its entire reason to exist.
            os.environ["HERMES_PROFILE_RESOLUTION"] = "env_profile_dir"
            return
    resolution = "flag" if profile_name is not None else "default"

    # 2. If no flag, check active_profile in the hermes root.
    #
    # EXCEPTION: a supervised s6 gateway child (exported by the container
    # run-script as HERMES_S6_SUPERVISED_CHILD=1) must NOT follow the sticky
    # active_profile. Each supervised slot has a fixed profile identity: named
    # slots pass ``-p <name>`` explicitly (handled in step 1 above), and the
    # reserved ``gateway-default`` slot runs bare ``hermes gateway run`` to mean
    # "the root HERMES_HOME profile". If the reserved default child read
    # active_profile here, switching the active profile (e.g. via the dashboard)
    # would silently redirect the default gateway into that profile — yielding a
    # duplicate gateway for the active profile and no real default gateway. See
    # the "Docker & Profiles & Dashboard" report.
    if profile_name is None and not os.environ.get("HERMES_S6_SUPERVISED_CHILD"):
        try:
            from hermes_constants import get_default_hermes_root

            active_path = get_default_hermes_root() / "active_profile"
            if active_path.exists():
                name = active_path.read_text(encoding="utf-8").strip()
                if name and name != "default":
                    profile_name = name
                    resolution = "active_profile_marker"
                    consume = 0  # don't strip anything from argv
        except (UnicodeDecodeError, OSError):
            pass  # corrupted file, skip

    # 3. If we found a profile, resolve and set HERMES_HOME
    if profile_name is not None:
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
            print(
                f"Warning: profile override failed ({exc}), using default",
                file=sys.stderr,
            )
            return
        os.environ["HERMES_HOME"] = hermes_home
        os.environ["HERMES_PROFILE_RESOLUTION"] = resolution
        # Strip the flag from argv so argparse doesn't choke
        if consume > 0 and profile_index is not None:
            start = profile_index + 1  # +1 because argv is sys.argv[1:]
            sys.argv = sys.argv[:start] + sys.argv[start + consume :]
