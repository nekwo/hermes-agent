"""Fork desktop build-lock process table boundary."""
import os
import sys
import time as _time
from pathlib import Path

class _PsutilDesktopProcessLister:
    """The production lister for the build-lock sweep: the live table.

    Satisfies ``hermes_cli.profiles._ProcessLister``. Imports of the shared
    types are function-local because this module's import cost is measured
    (see ``_boot_clock``) and the sweep is a Windows-only, rebuild-time path.
    """

    def read(self):
        try:
            import psutil  # type: ignore
        except Exception:
            # No inspector on this machine — the typed "cannot look" arm.
            return None
        from hermes_cli.profiles import _ProcessTable

        return _ProcessTable(
            self_pid=os.getpid(),
            # This sweep walks no ancestor chain and filters by no user: its
            # whole discriminator is "does this exe live inside THIS build's
            # release tree". Empty means "not collected"; nothing reads them.
            ancestor_pids=frozenset(),
            current_username=None,
            processes=self._iter_processes(psutil),
        )

    @staticmethod
    def _iter_processes(psutil):
        """Yield lazily: the caller filters rows as they arrive."""
        from hermes_cli.profiles import _ProcessFacts

        try:
            rows = psutil.process_iter(["pid", "exe"])
        except Exception:
            return
        for proc in rows:
            try:
                info = proc.info
                pid = info.get("pid")
                if pid is None:
                    continue
                yield _ProcessFacts(
                    pid=pid, exe=info.get("exe"), inspector_handle=proc
                )
            except Exception:
                continue

_DESKTOP_PROCESS_LISTER = _PsutilDesktopProcessLister()

_DESKTOP_LOCK_RELEASE_TIMEOUT = 5.0

def _stop_desktop_processes_locking_build(desktop_dir: Path) -> list[int]:
    """Terminate any running desktop app executing from this build's ``release``
    dir so a rebuild can replace its (otherwise locked) executable.

    On Windows a running ``Hermes.exe`` keeps an exclusive lock on
    ``release/win-unpacked/Hermes.exe``. electron-builder's pack then can't
    delete the stale binary and dies with ``remove …\\Hermes.exe: Access is
    denied`` / ``ERR_ELECTRON_BUILDER_CANNOT_EXECUTE`` (before-pack hits the same
    EPERM cleaning the dir). The retry path repeats the failure because the lock
    is still held. POSIX lets you unlink a running binary, so this is a no-op
    off-Windows.

    Scope is deliberately narrow: only processes whose executable lives *inside*
    this desktop's ``release`` tree are stopped — a packaged install elsewhere or
    an unrelated "Hermes" process is never touched. Best-effort: never raises.
    Returns the PIDs we asked to stop.

    Reads its rows from ``_DESKTOP_PROCESS_LISTER`` — see the seam above. Every
    filter is unchanged (win32 only, release dir must exist, never this process,
    exe must resolve INSIDE the release tree); only where the rows come from
    moved. Termination goes through each row's ``inspector_handle`` rather than
    re-resolving its pid, because between the walk and the act a pid can belong
    to something else.
    """
    if sys.platform != "win32":
        return []
    try:
        release_dir = (desktop_dir / "release").resolve()
    except OSError:
        return []
    if not release_dir.is_dir():
        return []

    table = _DESKTOP_PROCESS_LISTER.read()
    if table is None:
        # No inspector: this build cannot name a single locker. Unchanged from
        # before the seam — the rebuild proceeds and fails loudly on the locked
        # file if one is actually held (see the escalation note in ML-16).
        return []

    me = table.self_pid
    victims = []
    for row in table.processes:
        exe = row.exe
        if not exe or row.pid == me:
            continue
        try:
            exe_path = Path(exe).resolve()
        except (OSError, ValueError):
            continue
        if release_dir in exe_path.parents:
            victims.append(row)

    stopped: list[int] = []
    for row in victims:
        handle = row.inspector_handle
        if handle is None:
            continue
        try:
            handle.terminate()
            stopped.append(int(row.pid))
        except Exception:
            continue
    if stopped:
        # Wait for the handles (and thus the file locks) to actually release,
        # then kill whatever still holds one. ``psutil.wait_procs`` used to do
        # this; it is a convenience wrapper over ``Process.wait``, so waiting on
        # the rows directly keeps the entire sweep inside the injected table —
        # same total budget, same kill escalation, and no module-level psutil
        # reference left for a mutant to reach around the seam through.
        deadline = _time.monotonic() + _DESKTOP_LOCK_RELEASE_TIMEOUT
        for row in victims:
            handle = row.inspector_handle
            if handle is None:
                continue
            try:
                handle.wait(timeout=max(deadline - _time.monotonic(), 0.0))
            except Exception:
                try:
                    handle.kill()
                except Exception:
                    continue
    return stopped
