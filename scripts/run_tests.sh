#!/usr/bin/env bash
# Canonical test runner for hermes-agent. Run this instead of calling
# `pytest` directly to guarantee your local run matches CI behavior.
#
# What this script enforces:
#   * Per-file isolation via scripts/run_tests_parallel.py — each test
#     file runs in its own freshly-spawned `python -m pytest <file>`
#     subprocess. No xdist, no shared workers, no module-level leakage
#     between files.
#   * TZ=UTC, LANG=C.UTF-8, PYTHONHASHSEED=0 (deterministic)
#   * Env vars blanked (conftest.py also does this, but this
#     is belt-and-suspenders for anyone running pytest outside our
#     conftest path — e.g. on a single file)
#   * Proper venv activation (probes .venv, venv, then ~/.hermes/...)
#
# Usage:
#   scripts/run_tests.sh                            # full suite
#   scripts/run_tests.sh -j 4                       # cap parallelism
#   scripts/run_tests.sh tests/agent/               # discover only here
#   scripts/run_tests.sh tests/agent/ tests/acp/    # multiple roots
#   scripts/run_tests.sh tests/foo.py               # single file
#   scripts/run_tests.sh tests/foo.py -q            # path + bare pytest flag
#   scripts/run_tests.sh tests/foo.py -v --tb=long  # bare flags "just work"
#   scripts/run_tests.sh -k 'pattern'               # value flags pass through too
#   scripts/run_tests.sh tests/foo.py -- --tb=long  # explicit '--' still works
#
# Bare pytest flags (anything starting with '-' that isn't one of this
# runner's own options: -j/--jobs, --paths, --slice, --file-timeout, etc.)
# are forwarded to each per-file pytest invocation automatically — no '--'
# separator required. The explicit '--' form still works and stacks with
# bare flags. Positional path arguments override the default discovery
# root (tests/).

set -euo pipefail

# ── Locate repo root ────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Activate venv ───────────────────────────────────────────────────────────
VENV=""
for candidate in "$REPO_ROOT/.venv" "$REPO_ROOT/venv" "$HOME/.hermes/hermes-agent/venv"; do
  if [ -f "$candidate/bin/activate" ] || [ -f "$candidate/Scripts/activate" ]; then
    VENV="$candidate"
    break
  fi
done

if [ -z "$VENV" ]; then
  echo "error: no virtualenv found in $REPO_ROOT/.venv or $REPO_ROOT/venv" >&2
  exit 1
fi

if [ -x "$VENV/bin/python" ]; then
  PYTHON="$VENV/bin/python"
elif [ -x "$VENV/Scripts/python.exe" ]; then
  PYTHON="$VENV/Scripts/python.exe"
else
  echo "error: virtualenv found at $VENV but no Python executable was found" >&2
  exit 1
fi


# ── Live-gateway plugin (computed before we drop env) ───────────────────────
EXTRA_PYTHONPATH=""
EXTRA_PYTEST_PLUGINS=""
if [ -f "$HOME/.hermes/pytest_live_guard.py" ]; then
  EXTRA_PYTHONPATH="$HOME/.hermes"
  EXTRA_PYTEST_PLUGINS="pytest_live_guard"
fi


# ── Run in hermetic env ──────────────────────────────────────────────────────
# env -i: start with empty environment, opt-in only what we need.
# No credential var can leak — you'd have to explicitly add it here.
#
# The Windows platform vars below are opt-in for the same reason PATH/HOME are:
# they are not credentials, they are how the OS answers "where is the user, and
# where is scratch space". Without USERPROFILE/LOCALAPPDATA/APPDATA, CPython's
# ``Path.home()`` has nothing to resolve against and every import that touches
# it dies at COLLECTION time — a baseline run on Windows reported ~56 files
# failing for reasons no source change caused, which makes the failure-set diff
# (the only honest "is this mine?" signal) unreadable. SYSTEMROOT is required by
# the socket/ssl/subprocess machinery on Windows, and TEMP/TMP keep tempfile off
# a fabricated path. All six are absent-safe: `${VAR:+VAR="$VAR"}` expands to
# nothing on POSIX, so Linux/macOS/CI runs are byte-for-byte unchanged.
#
# SYSTEMROOT is read UPPERCASE on purpose. Windows treats env var names as
# case-insensitive; the shell reading them here does not. git-bash exports the
# variable as `SYSTEMROOT`, so the mixed-case `${SystemRoot:+…}` this line used
# to carry expanded to NOTHING and the var was silently dropped from the
# hermetic env — the exact class of dead guard it was written to prevent. The
# child is still handed it under the spelling Windows itself uses.
echo "▶ running per-file parallel test suite via run_tests_parallel.py"
echo "  (TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0; clean env)"

cd "$REPO_ROOT"

RUNNER_PATH="$SCRIPT_DIR/run_tests_parallel.py"
if command -v cygpath >/dev/null 2>&1 && [[ "$PYTHON" == *.exe ]]; then
  RUNNER_PATH="$(cygpath -w "$RUNNER_PATH")"
elif [[ "$PYTHON" == *.exe && "$RUNNER_PATH" =~ ^/mnt/([A-Za-z])/(.*)$ ]]; then
  drive="${BASH_REMATCH[1]^^}"
  rest="${BASH_REMATCH[2]//\//\\}"
  RUNNER_PATH="${drive}:\\${rest}"
fi

exec env -i \
  PATH="$PATH" \
  HOME="$HOME" \
  ${USERPROFILE:+USERPROFILE="$USERPROFILE"} \
  ${LOCALAPPDATA:+LOCALAPPDATA="$LOCALAPPDATA"} \
  ${APPDATA:+APPDATA="$APPDATA"} \
  ${SYSTEMROOT:+SystemRoot="$SYSTEMROOT"} \
  ${TEMP:+TEMP="$TEMP"} \
  ${TMP:+TMP="$TMP"} \
  TZ=UTC \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PYTHONHASHSEED=0 \
  PYTHONDONTWRITEBYTECODE=1 \
  ${HERMES_RUN_SLOW_PET_TESTS:+HERMES_RUN_SLOW_PET_TESTS="$HERMES_RUN_SLOW_PET_TESTS"} \
  ${EXTRA_PYTHONPATH:+PYTHONPATH="$EXTRA_PYTHONPATH"} \
  ${EXTRA_PYTEST_PLUGINS:+PYTEST_PLUGINS="$EXTRA_PYTEST_PLUGINS"} \
  "$PYTHON" "$RUNNER_PATH" "$@"
