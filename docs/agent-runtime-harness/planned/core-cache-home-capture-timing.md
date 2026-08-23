# Planned — capture the fingerprint home before a persona scope can be live

**Status:** NOT IMPLEMENTED. Convicted by a shipped census rule; the fix is not written.
**Owning doc:** [`../04-boot-and-lifecycle.md`](../04-boot-and-lifecycle.md) Stage 8.
**Module:** `agent_runtime/core_cache.py::resolved_fingerprint_home` (`:1170`).

**Distinct from [`core-cache-input-closure.md`](core-cache-input-closure.md).** That plan
widens WHICH inputs are fingerprinted, for `fingerprint_mismatch` / `never_converged`.
This one is about WHEN the home under which they are stat'd is captured. Same module,
different defect, different gate — a perfect input closure captured under the wrong home
still demotes.

## The claim

`home_mismatch` fired on this install, and this install has **one profile**. On a
single-profile install that reason is not an ordinary cache miss — it is a producer
defect, and the runtime already says so in its own words.

## Evidence

Twice, live, in `X:/Eternia/.hermes/profiles/base/logs/agent.log` — 2026-08-21 16:04:32
and 2026-08-22 13:36, each the same shape: one boot, three callers, all demoted for the
same reason. The 13:36 firing in full:

```
13:36:41,090 INFO snapshot_core_cache core_source=cache stale=true caller=hub reason=home_mismatch
13:36:41,090 INFO snapshot_core_cache core_source=rebuilt caller=prewarm reason=home_mismatch inputs=2429
13:36:42,167 INFO snapshot_core_cache core_source=cache stale=true caller=cli reason=home_mismatch
13:36:42,170 INFO snapshot_core_cache core_source=rebuilt caller=cli reason=home_mismatch inputs=2429
```

That boot then paid a cold build: `snapshot_build_core role=led caller=prewarm
generation=1 build_ms=7597` at 13:36:48. The 2026-08-21 16:04:32 firing carries the same
one-boot/three-caller signature — this is a repeating shape, not a single sighting.

The launcher spawns this serve with a single home —
`HERMES_HOME` and `HERMES_HEAD_HOME` both at the base profile
(`EterniaLauncher .../mission_control_hermes_installer.dart:861-866`). There is no
second root for the two runs to legitimately disagree about.

## Why this is a defect and not noise — already ruled, already executable

The census rule is written at `core_cache.py:185` (MC-2) and **executed as code** at
`agent_runtime/core_cache_census.py:368-375`:

> `home_mismatch: N — NOT an [ordinary miss] … live while a build stat'd — a defect to
> go fix.`

The mechanism named there: the persisted pair was keyed under a different Hermes home
than the reading process resolved, i.e. the two runs asked different QUESTIONS, and it
is emitted INSTEAD of `fingerprint_mismatch` so the distinction is countable rather than
inferred. On a single-profile operator boot it is evidence that **a persona scope was
live while a build stat'd — the capture in `resolved_fingerprint_home` was taken too
late.**

## The mechanism to fix

`resolved_fingerprint_home()` (`core_cache.py:1170-1192`) is capture-once-then-frozen,
reading `get_hermes_head_home()` lazily on **first use**. First use is whichever build
or consult happens first — not a defined point in the process lifecycle. Persona turns
install a context-local home override (`persona_profile_context`), and
`_pinned_to_fingerprint_home` (`:1212`) then pins the walk to whatever the lazy capture
happened to catch.

The lane already records whether the capture was trustworthy —
`hermes_head_home_is_authoritative()` at capture time, persisted beside the home
precisely so a demote can name it (`:1170-1179`). That flag is the diagnostic to read
first.

Direction (not yet chosen): capture the home at a defined boot instant — the serve
entry point, before any persona scope can exist — rather than lazily on first
fingerprint use. `reset_fingerprint_home()` (`:1195`) already exists as the tests-only
inverse, so the seam is present.

## Staged implementation (coordinator, 2026-08-22)

- **HC-1**: add an explicit capture call (`capture_fingerprint_home()` or an
  eager first-use at a named site) invoked from the serve entry point before
  any persona scope can install a context-local override; the CLI one-shot
  path captures at command dispatch, same rule. The lazy path stays as the
  fallback but logs that it fired (a lazy capture in a serve process is the
  defect recurring — make it loud).
- **HC-2**: a test that reds if the serve path regresses to lazy first-use
  capture (gate 2), plus the existing multi-home demote behavior pinned
  unchanged (gate 3) and the no-sidecar skip pinned (gate 4).
- **HC-3**: the live proof is gate 1 — ten operator boots, zero
  `reason=home_mismatch`, reported by the census, after deploy.

## Gate

1. Ten consecutive serve boots on the operator's single-profile install produce **zero**
   `reason=home_mismatch` lines. The census reports the zero; a silent window is not a
   clean one (`core_cache_census.py` refuses that reading).
2. The capture instant is a NAMED point in the boot sequence, asserted by a test that
   reds if a lazy first-use capture is reintroduced.
3. A genuine multi-home install still demotes `home_mismatch` rather than silently
   serving across roots — the reason must keep its meaning, not be suppressed.
4. A pair carrying no `sidecar.fingerprint_home` at all is still skipped rather than
   demoted, so installs predating MC-2 are unaffected.

## Explicitly out of scope

Removing the `home_mismatch` reason, or folding it back into `fingerprint_mismatch`.
The distinction is the whole reason the defect is visible.
