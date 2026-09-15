"""Downstream diagnostics and context-local browser-probe injection."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from hermes_cli.doctor_report import _section, check_fail, check_info, check_ok, check_warn, doctor_check, Finding

_browser_probe = ContextVar("doctor_browser_probe", default=None)

@contextmanager
def browser_probe_scope(probe):
    token = _browser_probe.set(probe)
    try:
        yield
    finally:
        _browser_probe.reset(token)

def browser_runnable(candidate, default):
    return (_browser_probe.get() or default)(candidate)

def _check_gateway_launcher_interpreter(issues: list[str]) -> None:
    """Fail when the autostart launcher names an interpreter updates don't sync.

    The Windows launcher is written once at install time and then outlives
    every process that could notice it is wrong. When it names an interpreter
    outside the Hermes environment, the gateway boots against a package set no
    update maintains — and the only symptom is a missing-module traceback at
    some later boot, which reads as a code bug rather than an install one.
    Two separate incidents on one install traced back to exactly this: a fatal
    ``concurrent_log_handler`` import death, and a ``nemo_relay`` traceback
    that made a fully healthy boot look like a crash.
    """
    try:
        from hermes_cli import gateway_windows
        from hermes_cli.gateway import (
            ManagedPythonUnavailable,
            is_windows,
            resolve_managed_python,
        )
    except Exception as e:
        check_warn("Gateway launcher interpreter", f"(could not import gateway helpers: {e})")
        return

    if not is_windows():
        return

    rendered = gateway_windows.installed_launcher_interpreter()
    if rendered is None:
        return

    _section("Gateway Launcher")
    try:
        managed = resolve_managed_python()
    except ManagedPythonUnavailable as exc:
        check_warn("Could not verify gateway launcher interpreter", f"({exc})")
        return

    if Path(rendered) == Path(managed):
        check_ok("Gateway launcher uses the Hermes interpreter", f"({rendered})")
        return

    check_fail("Gateway launcher uses an unmanaged interpreter", f"(launcher: {rendered})")
    check_info(f"Hermes is installed into: {managed}")
    check_info("Dependencies sync into the Hermes environment, not that one, so the")
    check_info("gateway can die at boot on a missing module. Re-render the launcher:")
    check_info("  hermes gateway install")
    issues.append(
        f"Gateway launcher points at {rendered}, not the Hermes interpreter "
        f"{managed} — run 'hermes gateway install' from the Hermes environment"
    )

@doctor_check()
def check_gateway_launcher(should_fix: bool, f: Finding) -> None:
    _check_gateway_launcher_interpreter(f.issues)
