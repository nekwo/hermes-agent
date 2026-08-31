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
