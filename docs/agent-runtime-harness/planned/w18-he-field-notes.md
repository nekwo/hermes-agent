# w18/he field notes — the cite-adjacency gate was red on `main` by 35

Lane `he` of wave 18. Row: "`scripts/doc_cite_adjacency.py` is RED on hermes `main`".
Base `57b6964b01`, level with `origin/main` at the time the lane opened.

## Before

```
python scripts/doc_cite_adjacency.py --exclude archive --exclude planned   # exit 1
  machine-checkable 287 · passed, adjacent 169 · passed, inside symbol 11 · FAILED 107
  baseline: 73 waived keys · UNWAIVED FAILURES: 35 · STALE WAIVERS: 1
  NOTE: 1 duplicate baseline key — 01-system-architecture.md|harness.py:5025
```

## After

```
python scripts/doc_cite_adjacency.py --exclude archive --exclude planned   # exit 0
  machine-checkable 287 · passed, adjacent 204 · passed, inside symbol 11 · FAILED 72
  baseline: 72 waived keys · UNWAIVED FAILURES: 0 · STALE WAIVERS: 0
```

`adjacent` is up by exactly the 35 that were failing; `FAILED` is down by the same
35 and is now entirely the 72 waived keys. No key was added. The duplicate-key
NOTE is gone because both instances of that key now pass, not because the cite
was collapsed — doc 01 still cites the verb and its handler in the same breath.

## What the 35 actually were: no prose was wrong

**Every one of the 35 was a live symbol that had MOVED, not a claim that had
died.** That is the finding worth recording, because the row's brief allowed for
stale claims and there were none: I read the code at each new anchor and the
sentence above each cite before changing a number, and in all 35 cases the
sentence still described the code correctly. So the count is **35 re-anchored, 0
prose corrections.**

The reason they moved together is one mechanism. Thirty of the 35 point into
`hermes_cli/harness_parts/serve.py`, which grew by roughly 2,700 lines across the
multi-device program, RL-16..RL-25, the service-stderr lane and the deny-subscribe
receipts — so a cite near the top of that file drifted a little and a cite near
`serve_loop`'s body drifted by ~900. `_cmd_serve` moved 5131 → 6215;
`dispatch_argv` 1638 → 2273; `_room_wants_stale_first` 3375 → 4319;
`subscription_dropped`'s emit 4357 → 5335. Three more point into
`harness_parts/persona_commands.py` (`_cmd_mission_chat_message` 2637 → 2672,
`_chat_effective_model_payload` 6953 → 7028), one into `hermes_cli/harness.py`
(`_usage_lane_detected`), one into `agent_runtime/serve_rpc.py`
(`baseline_offset = event_offset_of(watermark)` 885 → 1056).

**A line number into a growing module is the rot; the prose was never the rot.**
This is the third sweep of this gate and the second where the answer was purely
arithmetic. Where the sentence named a `def`, I anchored to the `def` line or to
the call site inside it, so the cite now has both verdicts available (`adjacent`
on the name AND `in-symbol` on the enclosing function) and survives the next
insert above it. Where the sentence named a receipt key or a constant
(`root_anchor_ms`, `OPS_STDIO_ONLY`, `_CACHEABLE_ARGV`), a bare line is all there
is and the cite is as durable as it can be.

Two anchors are deliberately RANGES rather than lines, matching what the sentence
claims: `04:193`'s ready-frame inventory now cites `serve.py:3827-3894`, the whole
`ready_frame` literal through the `boot_timeline` key the sentence ends on; and
`04:86`'s root anchor cites `serve.py:2766-2796`, the comment block that states
the `%LOCALAPPDATA%` shadow reason through `timeline.mark("root_anchor_ms")`.

## The stale waiver

`docs/agent-runtime-harness/02-runtime-data-and-shapes.md|serve_registry.py:119`,
a 2026-09-01 TABLE ROW waiver, had stopped failing and was deleted. Nothing else
in the baseline was touched: the other 72 keys are not this lane's, and the file's
`_comment` history is left as it stands.

One thing to know about editing under this gate: **a waiver key is
`<doc>|<token>:<line>`, so re-anchoring a cite that IS waived turns its waiver
stale and reds the gate from the other side.** Before touching anything I read
the baseline and checked that none of the 35 collided with a waived key, and that
none of my 35 new numbers landed on one either. The near miss is `04|serve.py:1060`
— the `:1060-1086` continuation that shares doc line 213 with the provider-warmup
cite I re-anchored. It is waived as a PAIRED CITE, it is now certainly pointing at
unrelated text, and it is left alone on purpose: moving it would delete a waiver
this lane was told not to touch. It is real rot for whoever burns the baseline down.

## Killing mutation

`04-boot-and-lifecycle.md:532`, the `OPS_STDIO_ONLY` cite re-anchored 342 → 398,
moved back ten lines to `serve.py:388`:

```
UNWAIVED FAILURES: 1
  docs/agent-runtime-harness/04-boot-and-lifecycle.md:532  serve.py:388
    -> hermes_cli/harness_parts/serve.py  names: OPS_STDIO_ONLY
```

Exit 1, the finding names the cite and the subject it failed on. Restored; exit 0.

## Commands, exit codes read without a pipe

| command | exit |
|---|---|
| `python scripts/doc_cite_adjacency.py --exclude archive --exclude planned` (before) | 1 |
| same, after the re-anchors and the waiver delete | 0 |
| same, under the killing mutation | 1 |
| `python scripts/dump_cli_contract.py --check` | 0 (191 command paths, sha256 `a07a5d73615a85ad`) |
| `python -m pytest tests/scripts/test_doc_cite_report.py -q` | 0 (8 passed) |
| `python -m pytest tests/scripts/test_doc_cite_adjacency.py -q` | 0 (40 passed) |
