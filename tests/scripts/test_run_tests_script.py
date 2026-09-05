from pathlib import Path


def test_run_tests_supports_windows_git_bash_venv_layout():
    script = Path("scripts/run_tests.sh").read_text(encoding="utf-8")
    assert '"$candidate/Scripts/activate"' in script
    assert '"$candidate/Scripts/python.exe"' in script


def test_run_tests_does_not_use_global_python_after_venv_detection():
    script = Path("scripts/run_tests.sh").read_text(encoding="utf-8")
    # The runner invocation spends the probed venv python, never a global one,
    # and the runner path travels through $RUNNER_PATH so the Windows arms can
    # rewrite it for a native interpreter.
    assert '"$PYTHON" "$RUNNER_PATH" "$@"' in script
    assert 'RUNNER_PATH="$SCRIPT_DIR/run_tests_parallel.py"' in script
    assert 'PYTHON="$VENV_PYTHON"' in script
    assert 'VENV_PYTHON="$candidate/bin/python"' in script
    assert 'VENV_PYTHON="$candidate/Scripts/python.exe"' in script


def test_the_local_venv_still_outranks_the_shared_canonical_one():
    """A checkout with its own ``.venv`` must keep using it.

    The shared canonical venv exists for worktrees that have NO ``.venv`` — a
    fresh ``git worktree add`` used to need a hand-carried ``HERMES_PYTHON=``.
    It is appended, never prepended: a checkout that pins its own environment
    (and CI, which creates one) must not be silently moved onto a machine-wide
    one, so the ORDER is the claim, not the presence of the candidates.
    """
    script = Path("scripts/run_tests.sh").read_text(encoding="utf-8")
    local = script.index('VENV_CANDIDATES=("$REPO_ROOT/.venv" "$REPO_ROOT/venv")')
    explicit = script.index('VENV_CANDIDATES+=("$HERMES_TEST_VENV")')
    shared = script.index('VENV_CANDIDATES+=("$HOME/.venvs/hermes-test")')
    release = script.index('VENV_CANDIDATES+=("$HOME/.hermes/hermes-agent/venv")')
    assert local < explicit < shared < release
    # Every candidate goes through the same pytest-installed check, so an
    # absent or pytest-less shared venv is skipped exactly like the release
    # venv rather than selected and then failing every file.
    assert 'for candidate in "${VENV_CANDIDATES[@]}"; do' in script


def test_no_machine_specific_venv_path_is_committed_in_the_runner():
    """Every probed candidate must be PORTABLE.

    The shared venv on the workstation that built this lane lives on another
    volume and was briefly spelled here as a literal candidate. A site-local
    absolute path in a shared script is a fact about one machine that everyone
    else has to read past, and it rots silently when that machine changes. It
    is reached through a junction into ``~/.venvs/hermes-test`` instead, so the
    probe stays portable — and this test is what stops the literal coming back
    the next time it is the convenient fix.

    Drive letters in COMMENTS are fine and deliberate (the fence block records
    the real store root it was measured against). Only the probe list is
    constrained.
    """
    script = Path("scripts/run_tests.sh").read_text(encoding="utf-8")
    candidate_lines = [
        line for line in script.splitlines() if line.strip().startswith("VENV_CANDIDATES")
    ]
    assert candidate_lines, "the probe list moved — re-point this test"
    for line in candidate_lines:
        assert ":/" not in line and ":\\" not in line, (
            f"machine-specific path in the venv probe list: {line.strip()}. "
            "Link it into ~/.venvs/hermes-test, or use $HERMES_TEST_VENV."
        )


def test_run_tests_hands_the_gateway_fence_the_real_store_root():
    """The runner must forward the real store root, and NOT ``HERMES_HOME``.

    ``tests/hermes_cli/_gateway_fence.py``'s real-store arm learns the root it
    must refuse from ``HERMES_TEST_REAL_ROOT``. Without this forwarding the
    fence resolved the root from ``HERMES_HOME``, which this script drops on
    purpose — so under the canonical runner it computed the throwaway session
    tempdir and the arm could never fire (measured 2026-09-03: a
    ``hermes config get`` argv aimed at ``X:\\Eternia\\.hermes`` classified
    ALLOWED under the runner, REFUSED under bare pytest).

    Forwarding ``HERMES_HOME`` itself would be the wrong fix and is asserted
    against: ``tests/conftest.py`` must keep installing the hermetic home.
    """
    script = Path("scripts/run_tests.sh").read_text(encoding="utf-8")
    assert '${REAL_HERMES_ROOT:+HERMES_TEST_REAL_ROOT="$REAL_HERMES_ROOT"}' in script
    assert "get_default_hermes_root" in script
    # The hermetic env must not carry HERMES_HOME through.
    exec_block = script[script.index("exec env -i") :]
    assert 'HERMES_HOME="$HERMES_HOME"' not in exec_block
    assert "HERMES_HOME=" not in exec_block


def test_run_tests_forwards_the_branch_measurement_config_var():
    """``HERMES_TEST_COVERAGE_RC`` reaches the hermetic child, absent-safe.

    ``scripts/unreachable_branch_report.py`` measures THROUGH this runner rather
    than re-spelling its ``env -i`` block, so the one variable that switches
    tracing on has to survive the drop. It is forwarded with the same
    ``${VAR:+…}`` guard every other opt-in uses, so a run without it is
    byte-for-byte the run it always was — asserted here, because a plain
    ``VAR="$VAR"`` would hand the child an empty value and make every run a
    traced one.
    """
    script = Path("scripts/run_tests.sh").read_text(encoding="utf-8")
    exec_block = script[script.index("exec env -i") :]
    assert (
        '${HERMES_TEST_COVERAGE_RC:+HERMES_TEST_COVERAGE_RC="$HERMES_TEST_COVERAGE_RC"}'
        in exec_block
    )
    assert 'HERMES_TEST_COVERAGE_RC="$HERMES_TEST_COVERAGE_RC"' not in exec_block.replace(
        '${HERMES_TEST_COVERAGE_RC:+HERMES_TEST_COVERAGE_RC="$HERMES_TEST_COVERAGE_RC"}', ""
    )
