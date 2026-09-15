## The Windows failure mode

`scripts/run_tests_parallel.py` splits its `--files` and `--paths` values on
`:`. On Windows that cuts the drive qualifier off every absolute path:

```
C:\repo\tests\test_a.py   ->   ["C", "\repo\tests\test_a.py"]
```

Each piece is then joined to the repo root, so the runner tries to open a file
literally named `C`:

```
FileNotFoundError: [Errno 2] No such file or directory: 'X:\repo\C'
```

This breaks `--files` (the hand-off from the CI generate job) and `--paths`
for any absolute path on Windows.

## Minimal repro

The repo's own test suite already reproduces it. `tests/test_run_tests_parallel.py::test_file_retry_self_heals_and_prints_both_attempts`
passes `--files <tmp_path>/test_flaky_probe.py`, which is absolute, so it fails
on **every** Windows run. On a clean checkout of `main`:

```
$ python -m pytest tests/test_run_tests_parallel.py::test_file_retry_self_heals_and_prints_both_attempts -q

E         File "X:\tmp\pr2\scripts\run_tests_parallel.py", line 119, in _approximately_count_tests
E           with open(path, "r", encoding="utf-8") as f:
E       FileNotFoundError: [Errno 2] No such file or directory: 'X:\tmp\pr2\C'
1 failed in 1.59s
```

## What the fix does

Routes both split sites through `_split_path_list`, which re-joins a single
ASCII letter that is immediately followed by a path separator — that is a
drive qualifier, not a list delimiter. A value that already names an existing
path is returned whole.

Relative, colon-joined lists behave exactly as before, so the POSIX and CI
paths are unchanged.

## What was tested, on what platform

Windows 10 (19045), Python 3.12.5, native (not WSL):

- `python -m py_compile scripts/run_tests_parallel.py` — OK
- `tests/test_run_tests_parallel.py::test_file_retry_self_heals_and_prints_both_attempts`
  — **failed before, passes after** (output above)
- 2 new unit tests for `_split_path_list` (drive letters preserved; plain
  relative lists still split, empty segments still dropped) — pass
- All three together: `3 passed in 34.28s`

One caveat, reported honestly: on one of several runs the retry test flaked
with a `UnicodeDecodeError: 'charmap' codec` while decoding runner output. It
is unrelated to this change — that test uses `text=True` without
`encoding="utf-8"`, while the `_run_runner` helper in the same file explicitly
documents that the runner declares UTF-8 stdio and must be decoded that way.
Left alone here to keep the diff to one concern; happy to fix it in a
follow-up if you'd like.

Not tested on macOS/Linux. The change is behavior-preserving for relative
colon lists (covered by the new unit test), which is the only form those
platforms use.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
