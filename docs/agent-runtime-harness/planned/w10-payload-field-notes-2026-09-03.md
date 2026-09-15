# W10 lane `payload` — hermes field notes (2026-09-03)

Running record for the hermes half of wave 10's `payload` lane. The launcher half
is recorded in `EterniaLauncher/docs/tooling/W10_PAYLOAD_FIELD_NOTES_2026-09-03.md`;
the two are split by repo, not by subject, per the field-notes ruling.

Worktree `X:/Eternia/_worktrees/w10-payload-h`, branch `w10/payload-h`, base
`504953f6ad`.

## The row

Filed in the launcher's `Launcher_Brain/20 — Active Initiatives/spatial-queue.md`
(hermes agents cannot write that vault):

> **Nothing in this repo can see hermes add a payload field; the one lane that
> can SKIPS in CI**

Three dated amendments and a re-derivation. Short version: the launcher/hermes
character payload is a cross-repo contract with a default-deny comparison on the
launcher side and an "additive is safe" habit on this one, and no shared schema.
Three instances, all found by a person:

| hermes | move | what it did |
|---|---|---|
| `34a8dad32e` | ADDED `handednessAccepted` | `bundle_character.dart` threw for every character on every machine with hermes installed, and was silently fine everywhere else |
| `4659127eba` | REMOVED `cardSafe` (split, not aliased) | the launcher's client kept asking and read every live crop as unjudged — a permanent silent `false` |
| `a4f8e62af7` | added the CONDITIONAL `sheet` slot | a key present in one mode and absent in the other, which a captured snapshot cannot describe at all |

The row's target shape: hermes publishes the payload's KEY SET as an artifact the
launcher can commit and diff, modelled on the argv dump one lane over
(`scripts/dump_cli_contract.py` here; `tool/hermes_cli_contract/` + a gate that
does NOT skip when hermes is absent, there).

## Re-derivation before building

- `characters` had 17 subcommands and none was `payload-contract` — the row's
  2026-09-02 re-derivation was still accurate at `504953f6ad`.
- `harness contracts dump` is the Mission Control EVENT manifest, confirmed, and
  is not the sprite payload. Not reused.
- The one producer is `agent/charsheet/draft.py::sprite_payload`; the `thumb`
  payload's producer is `CharacterDraft.row_thumb` in the same file. That second
  kind is in scope because `cardSafe` — the removal instance — was a `thumb` key,
  and because the launcher's strict reader for it (`charaRowThumbFromPayload`)
  lives in the same file the row names.

## What was built

`hermes_cli/charsheet_payload_contract.py` + the
`characters payload-contract --json` verb (`_cmd_characters_payload_contract`).

Three decisions worth recording, each of which was a wrong first answer:

1. **Derived by RUNNING the verbs, not by reading them.** A hand-written key list
   is a snapshot with exactly the blindness of the capture it replaces. The
   module redirects the character library to a temp directory
   (`HERMES_SHARED_CHARACTERS`), installs two synthetic characters and two
   synthetic drafts, calls the real `_cmd_characters_*` handlers with `--json`
   and reads the key paths off what they PRINT.
2. **The ENVELOPE is part of the contract.** `charaRowThumbFromPayload` reads
   `draft` and `stage` first, and neither is in `row_thumb`'s return —
   `_characters_verb` merges them. Probing the backend function alone would have
   been silent about exactly the keys the launcher's thumb reader opens with.
   Reading stdout gets them for free.
3. **Two probes, because some map KEYS are data.** First cut intersected the key
   paths of one probe with another; `directions.mirrored.w` survived it, because
   8-way and 4-way BOTH mirror `w`. The rule is now structural: a map whose own
   key set MOVED between the two vocabularies is dynamic — reported as ONE key
   with `"dynamic": true`, its children dropped. Nothing declares which maps
   those are; the disagreement is the measurement. `framesByRow` and
   `directions.mirrored` are what it finds.

Conditional keys are handled by probing each MODE and marking a path carried by
some modes and not others — which is `sheet` (`no-sheet` only) against
`spritesheetBase64` (`sheet` only), the third instance, now expressible.

Measured output at this commit: `sprite` 32 keys (2 conditional, 2 dynamic),
`thumb` 16 keys. No values, ever, so the dump is byte-stable across runs even
though the probes carry temp paths, `mtime_ns:size` revisions and clock-stamped
draft ids.

## Tests

`tests/hermes_cli/test_charsheet_payload_contract.py`, 8 cases. The load-bearing
one is `test_a_planted_key_in_the_producer_shows_up_in_the_dump`: it wraps
`sprite_payload` and `row_thumb` to ADD a key and REMOVE one, in both kinds,
asserts the dump moved, then restores the producers and asserts the same build
says the opposite. Both directions because the removal is the worse half of the
class and the row's title says only "add".

Plain `setattr` in a `try/finally` rather than `monkeypatch` there:
the plant has to be undone MID-test to take the second measurement, and
`monkeypatch.undo()` unwinds the whole stack including this tree's autouse pins,
whose tripwire correctly reds on that. (Measured — it did.)

## Commands and exits

```
C:/Python312/python.exe -m pytest tests/hermes_cli/test_charsheet_payload_contract.py -p no:cacheprovider   → 8 passed
C:/Python312/python.exe -m pytest tests/hermes_cli/test_cli_contract_dump.py tests/hermes_cli/test_harness_json_root_observability.py -p no:cacheprovider   → 10 passed
C:/Python312/python.exe -m pytest tests/agent/test_charsheet_draft.py -p no:cacheprovider   → 134 passed
C:/Python312/python.exe -m pytest tests/hermes_cli/test_harness_characters_cli.py tests/hermes_cli/test_harness_flag_and_control_reachability.py tests/hermes_cli/test_harness_parts_namespace.py -p no:cacheprovider   → 110 passed
C:/Python312/python.exe scripts/dump_cli_contract.py --write   → exit 0, 190 command paths
```

`tests/fixtures/hermes_cli_contract.json` is regenerated in the same commit: a
new subcommand moves the argparse tree, and the freshness gate
(`test_cli_contract_dump.py`) is the reason that is not optional.

The verb routes its output through `_characters_emit`, like every other
`characters` verb, so the root-observability gate treats it the way it treats
`sprite` and `thumb` — no ledger entry needed, and none added.

## What is left

- The launcher's own `test/features/mission_control/fixtures/hermes_cli_contract.json`
  is now one subcommand behind. It is NOT refreshed here: this branch has not
  landed, and a launcher fixture describing an unlanded hermes would be a lie in
  the other direction. Re-vendor after this lands, per that fixture's README
  sync table.
- Only two payload KINDS are covered (`sprite`, `thumb`). `status` and `list` are
  decoded by the same launcher file and are not in the contract; their payloads
  are dict-comprehension-keyed (`turnaround`/`rows` by direction and row key)
  and describing them is a schema question, not a key-set one. Rowed for the
  operator in the final report rather than half-built here.
