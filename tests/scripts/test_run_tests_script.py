from pathlib import Path


def test_run_tests_supports_windows_git_bash_venv_layout():
    script = Path("scripts/run_tests.sh").read_text(encoding="utf-8")
    assert "$candidate/Scripts/activate" in script
    assert "$VENV/Scripts/python.exe" in script


def test_run_tests_does_not_use_global_python_after_venv_detection():
    script = Path("scripts/run_tests.sh").read_text(encoding="utf-8")
    assert '"$PYTHON" "$SCRIPT_DIR/run_tests_parallel.py" "$@"' in script
    assert 'if [ -x "$VENV/bin/python" ]; then' in script
    assert 'elif [ -x "$VENV/Scripts/python.exe" ]; then' in script
