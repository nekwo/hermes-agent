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

---

# Second pass, 2026-09-06 — rebased onto `962a10d0fb`, twelve moved again

The landing session rebased `w18/he` onto `origin/main` after `w18/hb` landed.
Twelve of the cites this lane had just re-anchored moved AGAIN under hb's product
edits, and the gate came back red at the new base.

## Before (at `962a10d0fb` + this lane's first commit)

```
python scripts/doc_cite_adjacency.py --exclude archive --exclude planned   # exit 1
  machine-checkable 287 · passed, adjacent 192 · passed, inside symbol 11 · FAILED 84
  baseline: 72 waived keys · UNWAIVED FAILURES: 12 · STALE WAIVERS: 0
```

## After

```
python scripts/doc_cite_adjacency.py --exclude archive --exclude planned   # exit 0
  machine-checkable 287 · passed, adjacent 204 · passed, inside symbol 11 · FAILED 72
  baseline: 72 waived keys · UNWAIVED FAILURES: 0 · STALE WAIVERS: 0
```

`adjacent` up by exactly 12, `FAILED` down by exactly 12 and again entirely the
72 waived keys. No waiver key added, none deleted — the baseline file is
untouched this pass.

## The twelve were ONE number: `serve.py` grew by twenty lines

Eleven of the twelve are `hermes_cli/harness_parts/serve.py` and every one of
them moved by **exactly +20**. The other two are
`harness_parts/persona_commands.py` and both moved by **exactly +21**. Two
constants, two files — which is the whole finding, because it means nothing
about the code CHANGED, an insert happened above these cites and every anchor
below it shifted as one block.

| doc:line | cite | before → after | subject the gate judges on |
|---|---|---|---|
| `03-transport-and-wire.md:837` | `serve.py` | `5368-5370` → `5388-5390` | `stream_lane` (the `log_stream_attach` table row) |
| `03-transport-and-wire.md:849` | `serve.py` | `4319` → `4339` | `_room_wants_stale_first` (def) |
| `03-transport-and-wire.md:872` | `serve.py` | `5335` → `5355` | `subscription_dropped` (the emitted key) |
| `04-boot-and-lifecycle.md:26` | `serve.py` | `6219-6221` → `6235-6241` | `_cmd_serve` AND `BootTimeline` |
| `04-boot-and-lifecycle.md:176` | `serve.py` | `3810` → `3830` | `orphaned_turn_sweep_ms` |
| `04-boot-and-lifecycle.md:185` | `serve.py` | `3826` → `3846` | `dispatch_restore_ms` |
| `04-boot-and-lifecycle.md:320` | `serve.py` | `4319` → `4339` | `_room_wants_stale_first` (def) |
| `05-chat-turn-lane.md:158` | `persona_commands.py` | `7028` → `7049` | `_chat_effective_model_payload` (def) |
| `05-chat-turn-lane.md:165` | `persona_commands.py` | `6560` → `6581` | `mission_control_chat_model_override` (the const) |
| `07-observability.md:192` | `serve.py` | `4157` → `4177` | `_deny_subscribe` (def) |
| `07-observability.md:199` | `serve.py` | `3968-3972` → `3988-3992` | `log_line` / `boot_timeline` (the emit) |
| `07-observability.md:199` | `serve.py` | `3894` → `3914` | `boot_timeline` (the `ready`-frame key) |

**Again no prose was wrong.** I read the code at each new anchor and the sentence
above each cite before changing a number, and in all twelve the sentence still
described the code correctly. Count for this pass: **12 re-anchored, 0 prose
corrections.** Two passes, forty-seven cites, zero dead claims — the canon's
sentences are not what rots, its line numbers are.

`04:26` was widened on purpose from the old three-line range to `6235-6241`, the
`def _cmd_serve` line THROUGH `timeline = BootTimeline()`. The sentence is
"`_cmd_serve` starts a `BootTimeline` as its first instruction", and the range
now literally spans both halves of that claim, so the cite carries `adjacent` on
either name and cannot be broken by an insert between them.

## What this pass proves that the first could not

The first pass could say a cite had drifted. This one says how FAST: these twelve
were correct at `57b6964b01` on 2026-09-06 and wrong at `962a10d0fb` the same
day, because one lane landed above them. **A line cite into an actively-edited
module has a half-life measured in landings, not in weeks.** The gate is not
catching stale documentation; it is catching a coordinate system that moves under
concurrent lanes. The durable answer is the `file.py::symbol` form doc 03 and
doc 04 already use for `_room_wants_stale_first` — those two cites still needed
their `:N` updated, but the `::symbol` half told me instantly WHERE to look and
would have kept the sentence readable even while the number was wrong.

## Five stale cites the gate cannot see, fixed anyway

Doc 05's model-selection paragraph (`05-chat-turn-lane.md:164-170`) carries five
bare `:N` continuations that the gate scores UNCHECKED — their sentences' only
identifiers are receipt keys and a regex literal, which no `+/-3` window can
carry. They had drifted much further than the twelve, by one to two THOUSAND
lines, and were pointing at unrelated text I read line by line:

| cite | pointed at | now |
|---|---|---|
| `:6932` `_resolve_chat_model_override` | `"ok": False,` | `:7035`, the `def` |
| `:3471` "called at" | `)` | `:3531`, the call |
| `:5788` scope inside `_chat_effective_model_payload` | `"error_code": "persona_not_persisted"` | `:7085`, the `"scope"` key (`7049-7088` is the function) |
| `:5285` the value regex | `config = _session_model_config(...)` | `:6582`, `_CHAT_PROVIDER_MODEL_RE` |
| `:2745-2760` `CHAT_MODEL_OVERRIDE_PERSIST_FAILED` | unrelated | `:3552-3558`, the refusal payload |

None is waived, so fixing them could not turn a waiver stale, and the gate's
counts are byte-identical before and after (`adjacent` 204, `no subject` 68) —
which is the proof that they were and remain outside what the probe can judge.
**The gate's green is a floor, not a ceiling: the cites it calls UNCHECKED rot
faster than the ones it checks, because nothing has ever pulled them straight.**
There are 68 no-subject and 198 pathless `:N` cites in this canon. That is the
next real sweep, and it cannot be run by the gate — only beside it.

## Killing mutation

`03-transport-and-wire.md:872`, the `subscription_dropped` cite re-anchored
5335 → 5355, moved back ten lines to `serve.py:5345`:

```
UNWAIVED FAILURES: 1
  docs/agent-runtime-harness/03-transport-and-wire.md:872  serve.py:5345
    -> hermes_cli/harness_parts/serve.py  names: subscription_dropped
```

Exit 1, the finding names the cite and the one subject it failed on. Restored;
exit 0.

## Commands, exit codes read without a pipe

| command | exit |
|---|---|
| `python scripts/doc_cite_adjacency.py --exclude archive --exclude planned` (rebased base) | 1 |
| same, after the twelve re-anchors | 0 |
| same, after the five doc-05 continuations | 0 |
| same, under the killing mutation | 1 |
| same, restored | 0 |
| `python scripts/dump_cli_contract.py --check` | 0 (191 command paths, sha256 `a07a5d73615a85ad`) |
| `python -m pytest tests/scripts/test_doc_cite_adjacency.py -q` | 0 (40 passed) |
