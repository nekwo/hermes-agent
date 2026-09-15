## The failure mode

The one-shot flake retry in `_run_one_file` re-runs any non-zero file —
including one the runner just killed for exceeding `--file-timeout`.

That re-run starts immediately, under the very pool contention that caused the
timeout, so it nearly always times out again. And it costs a **second full**
`file_timeout` to reach the same answer.

At the 300s default, one hung file costs 10 minutes instead of 5 — while
holding a worker the entire time, which makes the contention worse for
everything still queued behind it.

This is not Windows-specific; it just shows up fastest on a loaded box.

## Minimal repro

A probe file that records one line per invocation and then hangs, run at
`--file-timeout 5 --file-retries 1`:

```
before:  2 invocations, 11.06s   (assert 2 == 1)
after:   1 invocation,   5.64s
```

## What the fix does

Gates the retry loop on `rc != _TIMEOUT_RC` (124, the code the runner already
stamps on a killed file), and names that constant instead of leaving the
literal inline.

The reasoning: a timeout is the one failure class this retry cannot diagnose.
It is not a flake signal — it is a "this file did not finish in the budget"
signal, and re-running it under identical conditions cannot change that.
Assertion-shaped failures still retry exactly as before, so the flake-laundering
protection is untouched.

I deliberately kept this to the narrow fix. The fuller version — re-running
timeout-shaped stragglers **once, serially, at 1-worker isolation after the
pool drains** — is a real improvement (contention is precisely what that pass
rules out, which is why retrying in-pool is the wrong place for it), but it is
a bigger change and a separate conversation. Happy to open it if you want it.

## What was tested, on what platform

Windows 10 (19045), Python 3.12.5, native (not WSL):

- `python -m py_compile scripts/run_tests_parallel.py` — OK
- New `test_timeout_is_not_retried_by_the_flake_retry` — **fails before this
  change** (`assert 2 == 1`, 11.06s), **passes after** (5.64s). The assertion
  is deterministic (an invocation counter), not timing-based; the wall-clock
  check is only a secondary sanity bound.
- Regression check that assertion failures are *still* retried: the existing
  `test_file_retry_self_heals_and_prints_both_attempts` passes alongside it —
  `4 passed in 38.99s`.

One note on that regression check: on Windows the existing retry test cannot
run on `main` as-is, because `--files` shreds absolute Windows paths on the
`:` split. I verified the two together on a tree that also carries the fix for
that (filed separately). On a POSIX host no such stacking is needed.

The new test writes its probe under the repo root and passes it to `--files`
repo-relative, precisely so it does not depend on that other fix.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
