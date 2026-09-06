# w18/hf field notes — 2026-09-06

Lane hf of wave 18: four reds, three Linux-only from CI run 34051553815, one
Windows-only. One commit per row. Notes are written as each row lands.

## Row 4 — `test_two_profiles_get_different_homes` (Windows-only red)

The row asked whether "the two profiles collapse to one home through a
case-folded or short-path comparison". They do not. The product is correct and
the red is in the assertion:

```
assert home_a.endswith("alpha/home")
E   AssertionError: assert False
E    +  ...'...\\.hermes\\profiles\\alpha\\home'.endswith('alpha/home')
```

`get_subprocess_home()` returned two distinct, correct paths — the
`home_a != home_b` line above it passed. What failed is a hardcoded POSIX
separator inside a suffix match. On Linux `os.sep` is `/` and the suffix holds
by accident of the platform; on Windows it can never hold.

The test's real rule is per-profile **identity**, not a string suffix: each
profile's subprocess HOME is that profile's own `{HERMES_HOME}/home`. Rewritten
to assert the exact path (`home_a == str(base / "alpha" / "home")`), which is
separator-correct on both hosts and strictly stronger than the suffix it
replaces. Every other assertion in this file already used that spelling
(`== str(profile_home)`); this case was the outlier.

Killing mutation: made the container branch of `get_subprocess_home()` return
`<profiles>/home` instead of `<profiles>/<name>/home` — i.e. actually collapse
the two profiles onto one home, the failure the row hypothesised — and the
rewritten test reds (exit 1). Product restored.

## Row 1 — `ServeSocket` could not wake a blocked `accept()` off Windows

`_close_listener` called `listener.close()` and nothing else. On Windows
`closesocket` aborts a pending `accept()` in another thread; on Linux and macOS
it does not. `close()`'s `thread.join(2.0)` then moved on regardless, so
`accept_loop_exited` — the field whose entire purpose is "the loop never stops
quietly" — was stamped only where the platform happened to be kind. CI saw the
symptom as a FLAKY (failed attempt 1, passed on retry).

**Which wakeup.** Of the row's three candidates:

- `shutdown(SHUT_RDWR)` before `close()` wakes `accept()` on Linux, returns
  `ENOTCONN` on macOS/BSD and does nothing there. It would swap a
  Windows-shaped assumption for a Linux-shaped one, and a test for it can only
  assert that `shutdown` was *called*, never that the loop ended.
- A self-connect to the bound port ends the loop by opening a real connection
  the service then has to refuse or admit — a wakeup with a visible side effect
  on the very counters this test reads (`accepted`, `pending_peak`).
- A **socketpair the loop selects on** was taken. It is the only one where the
  loop's own shape states the rule (it waits on a peer *or* on the stop signal),
  it assumes nothing about any platform's `close()`, and — the deciding
  point — it makes the test say exactly what the row is about: with the
  listener's `close()` neutered, the loop still ends, and it ends because
  something woke it.

**Shape.** `bind()` creates the pair and sets the listener non-blocking; the
loop selects on `[listener, wake_read]` and only then accepts, treating
`BlockingIOError` (select promised a peer, the kernel had none) as a plain
retry rather than an accept error. `_close_listener` wakes FIRST, then closes:
one byte for a loop already in `select`, then the write end closed so the read
end sits at permanent EOF for a loop that has not reached `select` yet. The
read end is closed by the accept thread itself, the only thread that ever
selects on it — closing it from `close()` would free a descriptor under a live
`select`. `begin_drain()` also reaches `_close_listener` without setting
`_stop`, and that path is covered by the same wake plus the loop's existing
`self._listener is None` check.

Nothing downstream inherits the non-blocking listener: `_serve_connection`
already calls `sock.settimeout(self._hello_deadline)` on the accepted socket as
its first act.

Red-first: `_CloseDoesNotWakeAccept` wraps the real bound listener, delegating
`fileno()` and `accept()` and making `close()` a no-op that counts calls — POSIX
semantics on any host. Without the wakeup the new test reds on Windows with
`assert None == 'listener_closed'`, the exact CI signature. Killing mutation:
deleting the single `self._signal_wakeup()` line from `_close_listener` reds it
again (exit 1). `bare_server` gained a `start_accepting=False` knob so the
substitution can happen between `bind()` and the thread.
