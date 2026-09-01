"""In-process harness CLI driver for wiring-claim tests.

Replaces the ``subprocess.run([sys.executable, "-m", "hermes_cli.main",
"harness", …])`` child that ``test_realm_sync.py`` and
``test_office_class_key_guard.py`` used to spawn per assertion — ~3 s of
interpreter boot and tree import per call, 37 call sites, ~100 s of a serial
``tests/agent_runtime`` run (hermes-suite-perf plan Stage 6; field notes §6
class 3).

This is NOT a test double. It drives
``hermes_cli.harness_parts.serve.dispatch_argv`` — the production dispatcher
the Mission Control serve lane itself routes every harness request through,
whose contract is "parse and run one request exactly as ``hermes <argv…>``
would, including the harness error-envelope contract". Parser construction,
handler dispatch, exit-code mapping and error envelopes are all the same
production code the child would have run; what changes is only that no fresh
interpreter boots to run them.

What this deliberately does NOT prove, per the Stage 6 classification
(field notes §8): true process-boundary claims — env crossing a real exec
boundary, a fresh interpreter's import set, signal delivery. Every converted
call site asserts verb behavior (store effects, envelope shape, exit code),
none of those. A future test whose CLAIM is the process boundary should spawn
a real child and say so — not route through here.

Known in-process differences, accepted and bounded:

* stdout/stderr are captured with ``contextlib.redirect_*`` — a logging
  handler that bound the real stream at import time writes past the capture.
  No converted site asserts stderr CONTENT (it is only concatenated into
  assertion messages), so this cannot flip a verdict.
* Handler state stays in this process. Store state lives under the per-test
  hermetic ``HERMES_HOME``, so cross-test leakage through the store is
  impossible; module-level memo leakage is the same exposure every other
  in-process test in this directory already has, and the same conftest
  resets cover it.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys


def run_harness_in_process(*args: str) -> subprocess.CompletedProcess:
    """Run ``hermes harness *args`` through the serve lane's dispatcher.

    Returns a ``subprocess.CompletedProcess`` so existing call sites keep
    their ``returncode``/``stdout``/``stderr`` shape unchanged.
    """
    from hermes_cli.harness_parts.serve import dispatch_argv

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = dispatch_argv(["harness", *args])
        except SystemExit as exc:  # argparse --help / usage errors exit here
            if isinstance(exc.code, int):
                code = exc.code
            else:
                code = 0 if exc.code is None else 1
    return subprocess.CompletedProcess(
        args=[sys.executable, "-m", "hermes_cli.main", "harness", *args],
        returncode=code,
        stdout=out.getvalue(),
        stderr=err.getvalue(),
    )
