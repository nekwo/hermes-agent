"""Fork CLI registration and dispatch contracts."""
import json
import logging
import sys
import time as _time
from hermes_cli import _boot_clock
logger = logging.getLogger(__name__)
_FINGERPRINT_HOME_CLI_BOOT_SITE = "hermes_cli.main:harness_command_dispatch"

def cmd_postinstall(args):
    """One-shot bootstrap for pip users: install non-Python deps + run setup."""
    from hermes_cli.main import _has_any_provider_configured, cmd_setup
    from hermes_cli.install_method import stamp_install_method
    from hermes_cli.dep_ensure import ensure_dependency, ensure_git_bash, _DEP_CHECKS
    from hermes_cli.path_setup import register_hermes_command
    from hermes_constants import get_hermes_home

    stamp_install_method("pip")

    emit_json = getattr(args, "json", False)

    print("⚕ Hermes post-install bootstrap")
    print()

    interactive = not (getattr(args, "yes", False) or getattr(args, "non_interactive", False))
    for dep in ("node", "browser", "ripgrep", "ffmpeg"):
        ensure_dependency(dep, interactive=interactive)

    # Provision the shell Hermes runs terminal commands through (Git Bash on
    # Windows; native bash elsewhere) and persist HERMES_GIT_BASH_PATH so the
    # agent never falls through to the System32 WSL stub.
    git_bash_path = ensure_git_bash(interactive=interactive)

    # Put `hermes` on PATH via a stable wrapper shim. The home baked into it is
    # THIS install's canonical resolution and nothing else — the shim outlives
    # the run and repeats whatever it was handed for the life of the install.
    path_result = register_hermes_command(get_hermes_home())

    # Say what happened. The whole result used to be reachable only inside the
    # `--json` branch, so a human running `hermes postinstall` was told nothing
    # at all — neither a refusal nor the "add this dir to your PATH" guidance.
    if path_result.error:
        print()
        print(f"⚠ PATH shim not written [{path_result.error}]: {path_result.note}")
    elif path_result.note:
        print()
        print(f"⚠ {path_result.note}")

    if not _has_any_provider_configured():
        print()
        if interactive:
            cmd_setup(args)
        else:
            print("✓ Post-install complete. Provider setup skipped for non-interactive install.")
    else:
        print()
        print("✓ Post-install complete.")

    if emit_json:
        note = path_result.note
        if git_bash_path is None and sys.platform == "win32":
            gb_note = (
                "Git Bash not found. Install Git for Windows "
                "(https://git-scm.com/download/win) or set HERMES_GIT_BASH_PATH."
            )
            note = f"{gb_note} {note}".strip() if note else gb_note
        summary = {
            "schema": "hermes.postinstall/1",
            "git_bash_path": git_bash_path,
            "shim_path": path_result.shim_path,
            "path_dir": path_result.path_dir,
            "path_registered": path_result.path_registered,
            # Additive to `hermes.postinstall/1`: the refusal's machine-readable
            # code beside the prose, so the installer can branch on it without
            # matching a sentence. `None` on a run that wrote a shim.
            "error": path_result.error,
            "note": note,
            "deps": {name: bool(check()) for name, check in _DEP_CHECKS.items()},
        }
        # MUST be the final stdout line — the launcher scans for this schema.
        print(json.dumps(summary))

def _capture_core_cache_fingerprint_home(args) -> None:
    """Capture the core cache's fingerprint home BEFORE the command runs (HC-1).

    THE RULE, and why it is applied here and only here. ``core_cache`` freezes
    the Hermes home its input closure is stat'd under on FIRST USE, and a first
    use that lands inside ``profile_context.persona_profile_context`` pins that
    persona's home for the life of the process — after which any sidecar this
    process writes is keyed under it, and the NEXT boot demotes the pair
    ``reason=home_mismatch``. A one-shot CLI is not exempt from that: ``hermes
    harness chat send`` runs a persona turn through
    ``profile_runner._execute_agent_run``, whose whole body is inside that
    scope, and a tool in that turn reaching the snapshot is a first fingerprint
    taken under the override. The poisoned pair then outlives the process.

    SCOPE, decided on evidence rather than on caution: only ``hermes harness …``
    can reach this lane at all — ``core_cache``/``agent_runtime.snapshot`` are
    imported by ``hermes_cli.harness``, ``harness_support`` and the four
    ``harness_parts`` modules, and by nothing else under ``hermes_cli``. Every
    other command would pay a ~90ms import of a subtree it never touches, so the
    fork is the same one the error-envelope fork below already makes.

    ``hermes harness serve`` passes through here too, and that is deliberate
    rather than redundant: this is the earliest instant in the process, and
    ``serve_loop`` re-declares its own, more specific site under it. Capture-once
    means the second call is an observation, not a second answer.

    Best effort by contract: an instrument must never be why a command fails.
    """

    if getattr(args, "command", None) != "harness":
        return
    try:
        from agent_runtime import core_cache

        core_cache.declare_fingerprint_home_boot_site(_FINGERPRINT_HOME_CLI_BOOT_SITE)
        core_cache.capture_fingerprint_home()
    except Exception:
        pass


def build_downstream_parsers(subparsers):
    started = _time.monotonic()
    try:
        from hermes_cli.harness import build_parser
        build_parser(subparsers)
    except Exception as exc:
        logger.debug("Harness parser registration failed: %s", exc)
    finally:
        _boot_clock.record_harness_parser_ms(int(max(0.0, _time.monotonic() - started) * 1000))
    from hermes_cli.subcommands.postinstall import build_postinstall_parser
    build_postinstall_parser(subparsers, cmd_postinstall=cmd_postinstall)


def dispatch_command(args):
    _capture_core_cache_fingerprint_home(args)
    try:
        return args.func(args)
    except Exception as exc:
        if getattr(args, "command", None) == "harness":
            from hermes_cli.harness import emit_harness_error
            sys.exit(emit_harness_error(exc, args=args))
        raise
