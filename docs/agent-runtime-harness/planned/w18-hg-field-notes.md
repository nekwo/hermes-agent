# Wave 18, lane hg — field notes (2026-09-06)

Desktop app (`apps/desktop`), one row: *"Desktop E2E on Linux: two specs poll the
'Background task running' dot as a turn-is-running signal"*. CI oracle: run
34051553815, job `Desktop E2E / Playwright E2E (Linux)` (job id 101536001111).

## What the row said, and what the run actually shows

The row's read is right about the premise and wrong about the proximate cause.
Both are worth writing down, because the second one is a different defect that
the first one was hiding.

### 1. The premise (the row's defect) — real, and the specs are the wrong side

`sessionDotState` in `src/app/chat/sidebar/session-row-state.ts` resolves ONE
mutually-exclusive dot per session and ranks `isWorking` above `hasBackground`.
A session that is running a turn AND holding a live `terminal(background=true)`
process paints `working` (aria-label `Session running`); the `background` label
is by design unreachable until the turn goes idle. Two call sites polled the
background label to learn that a turn had *started* — a wait that can only be
satisfied by the turn *ending*:

- `e2e/sidebar-states.spec.ts:215` (spec at :204)
- `e2e/tile-unread-bug.spec.ts:61` (helper `startTurnAndSwitchAway`, spec :125)

The primitive is not the wrong side. The ordering predates the one-primitive
collapse (`87aaf87748` moved `sessionDotState` out of `session-row.tsx`; it did
not invent the ranking), and it is deliberate in two ways a swap would break:
the running/stalled dot is the only live-turn cue the pane tabs and session
tiles get (`SessionStatusDot` is shared; only the sidebar row also draws the
`sessionShowsRunningArc` arc), and `stalled` would be masked by any background
process. The specs are the wrong side.

Independent confirmation that the background dot only lands after the turn:
`3a3bc41c7e`'s own commit message records the 2026-07-26 failure as "the 'dot
should appear' poll took 7.5s to see the dot ... `waitForFunction(finalText)`
returned in 0.08s because the turn was long done". Same observation, read then
as a wall-clock race rather than as the ordering rule.

Third call site, same wrong premise, currently green by luck:
`sidebar-states.spec.ts:154` ("background dot visible while subagent runs",
`sleep 5`, no sentinel). Its own evidence screenshot from run 34051553815
(`bg-dot-while-subagent-runs.png`) shows the transcript already carrying the
FINAL script text and the composer stack reading "1 Background" — i.e. the poll
was satisfied after the turn settled, in the shrinking window before `sleep 5`
exited. Fixed here too: it now polls the running dot (deterministic) and keeps
its "gone after the process exits" tail, which is the auto-dismiss coverage.

### 2. The proximate cause of the CI red — a blocked command approval

Fixing the poll does NOT turn the job green. The failing run's page snapshot
(`test-results/sidebar-states-.../error-context.md`, captured at the 30s poll
timeout) shows the turn wedged on a terminal-command APPROVAL prompt:

```
- button "Session running E2E_SIDEBAR_CROSS"        <- the running dot IS there
    - status "Session running"
...
- button "Running Running echo \"long bg output\" + 6 commands"
- generic: "2" "7" "s"                              <- 27s elapsed on the call
- button "Run Ctrl⏎" / "More approval options" / "Reject Esc" / "Command"
```

A `background=true` terminal call returns immediately; 27s on the call plus a
Run/Reject bar means the tool never executed. No process spawned, so
`$backgroundStatusBySession` never carried a `running` item and the background
dot was unreachable for the whole test — not because a working turn outranked
it, but because there was nothing to outrank. The subagent row never appears
either, and the composer still reads "Waking up …".

The gated command is the sentinel form `3a3bc41c7e` introduced
(`sidebarCrossBgCommand` in `e2e/mock-server.ts`):

```
echo "long bg output" && for _ in $(seq 1 600); do [ -e "<tmp>" ] && break; sleep 0.1; done && echo "finished"
```

rendered by the desktop as "+ 6 commands". The `sleep 5` fallback the other two
specs use is never gated. What I could rule out from Windows:
`tools.approval.detect_dangerous_command` returns `(False, None, None)` for
BOTH forms, and `tools.tirith_security.check_command_security` returns
`{'action': 'allow', 'findings': [], 'summary': ''}` for both — but `tirith` is
not installed on this box (`ModuleNotFoundError: tirith`), so that second
reading is a fail-open, not a verdict. On a CI runner tirith auto-installs from
GitHub releases (`tools/tirith_security.py`) and is enabled by default, and
`check_all_command_guards` routes any tirith `warn`/`block` straight to the same
approval gate. That makes tirith the leading suspect and leaves the E2E suite
with a network-dependent, non-hermetic input to every terminal-using spec. I did
not guess at a fix: the approval description is inside a collapsed disclosure
and is not in the trace's DOM, so the flagging rule is unproven. Handed back.

## What landed

- `apps/desktop/e2e/sidebar-states.spec.ts`, `apps/desktop/e2e/tile-unread-bug.spec.ts`
  — a `RUNNING_DOT_LABEL` const in each, documenting the ordering rule at the
  place the next author will look, and all three "is the turn running?" polls
  moved onto it. No timeout widened, no assertion loosened; the
  background-dot assertions stay exactly where they belong (after the turn
  settles, with the process held by the sentinel).
- `apps/desktop/src/app/chat/sidebar/session-row-state.test.ts` — the coverage
  hole that permitted the wrong premise. The existing case is named "keeps
  background and unread states below active-turn states" but passes
  `isWorking: false`, so it never exercises the ordering it names. New case
  pins working-over-background, stalled-over-background, and background only
  once idle.

Killing mutation on the new case: hoist the `hasBackground` branch above the
`isWorking` branch in `sessionDotState` — i.e. make the product do what the two
specs believed. Result: `1 failed | 3 passed`, with
`AssertionError: expected 'background' to be 'working'` at the new case and the
three pre-existing cases still green. That asymmetry is the coverage hole,
measured.

## Gates

- `session-row-state.test.ts`: 4 passed, exit 0.
- `npx tsc -p tsconfig.e2e.json --noEmit`: exit 0. `npx tsc -p . --noEmit`: exit 0.
- `npx eslint` on the three changed files: 4 errors / 3 warnings, ALL of them
  pre-existing at `600ec5100f` (import-order at `sidebar-states.spec.ts:12`,
  three `import()` type annotations at `tile-unread-bug.spec.ts:47/54/100`, and
  blank-line warnings) and none in a line this lane wrote. `e2e/` is outside
  the package's own lint scope (`"lint": "eslint src/ electron/"`).

Environment note for whoever picks this up on Windows: the `ui` vitest project
(jsdom) cannot start on this box at all — `html-encoding-sniffer` requires
`@exodus/bytes/encoding-lite.js`, an ES module, and Node 20.17 has no working
`require(esm)` (`--experimental-require-module` blows the stack). An untouched
file (`src/lib/desktop-slash-commands.test.ts`) fails identically, so it is the
runtime, not the change. CI's `setup-node` runs a version where `require(esm)`
is supported. The one test above was run through a scratchpad config pinning
`environment: 'node'` and no setup file, which is honest for a pure-function
module; it was deleted after the run and is not in the tree.

## Proof

The E2E specs cannot run on this box, and not only for the usual reasons
(`npm run build` plus a real `hermes serve` backend): the scripted background
command is POSIX shell (`seq`, `[ -e ]`, `sleep 0.1`), so a Windows terminal
backend would not run it as written. The proof for the spec change is the next
Linux CI run — and it will still be red on the approval block above until that
second row is settled, now failing at `waitForFunction(finalText)` instead of
at the dot poll.
