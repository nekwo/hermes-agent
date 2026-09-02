# CLI ergonomics — field notes, 2026-09-02

Running record for the two-row CLI-ergonomics lane worked on branch
`feat/cli-persona-choices-sprite-no-sheet`, cut from `origin/main` at
`0c9fb95410`. Written as the work happened; the rows' own conclusions are
folded into `06-office-and-board.md` and into the charsheet skill's
`SKILL.md` / `FIELD-NOTES.md`, which are the truth. This file keeps the
evidence those two state the conclusion of — including the half of row 1 that
was built on a premise the code does not support.

## 1. Row 1's headline premise is FALSE, and the honest finding is smaller

The row read: *"The placement verb cannot tell an operator what is placeable …
no subcommand enumerates placeable templates."*

`harness agent list` enumerates them, and always has. `_cmd_agent_list` walks
`ensure_persisted_personas(cfg)` — the exact `{**catalog, **stored}` merge that
`agent_create.persona_roster` reads and that `_persona_is_unknown` checks
against — and emits one row per definition. Run live against the operator root
at `0c9fb95410`, before any change in this branch:

```
$ harness agent list --json
{"count": 5, "item_kind": "agent", "items": [
  {"id": "neko_supervisor", "name": "Neko Mission Lead", "profile": "neko",  …},
  {"id": "dev",             "name": "Launcher Dev Agent", "profile": "gpt-launcher", …},
  {"id": "backend_dev",     "name": "Backend Dev Agent",  "profile": "backend-dev", …},
  {"id": "qa",              "name": "QA Agent",           "profile": "launcher-qa", …},
  … ]}
```

Those are the five ids `--persona` accepts by bare id. The refusal already
named this verb (`persona_not_found_message`, "run `harness agent list` …"), so
the cure and the listing were both in place and pointing at each other.

**So a `persona templates` verb was NOT built.** It would have been a second
door onto the same enumeration — rung 2 of the Footprint Ladder to solve a
problem rung 1 already covers, and the "overreached / resurrected an approach"
close in AGENTS.md's premise section is written for exactly this shape.

What the row got RIGHT, and what was actually built:

* `harness persona list` returns durable persona INSTANCES
  (`_cmd_persona_list` → `PersonaInstanceStore.ensure_for_personas`), not
  templates. True, and the reason the row's author reached the wrong
  conclusion: the verb whose name says "persona" is the one that does not list
  personas.
* `--persona` takes free text with no `choices=`. True and unchanged — a
  `choices=` list on the parser would have to be computed at `build_parser`
  time, which runs for EVERY harness call, and would put a config load and a
  store read in front of `harness --help`. The refusal is the right place.
* **Nothing anywhere said `--persona` takes two spellings.** True, and this is
  the real gap. `agent list` prints a `profile` column, but that is the
  persona's `hermes_profile` BINDING; reading it as the second spelling is
  wrong for a shared profile and there was no way to tell from the output.

## 2. The `profile:<token>` spelling is not always safe to advertise

`agent create --persona profile:<token>` parses for ANY token — `_persona_is_unknown`
exempts every `profile:`-prefixed id from the roster check by decision D-U1,
because the launcher's template browser sends ids for profiles that own no
persona row. The synthesis happens in the CLI's `_persona_by_id`, which fills
the synthesised persona's model / provider / api_mode / toolsets from
`profile_persona_resolution` — and that function returns a persona only when the
profile has exactly ONE owner (`PROFILE_CHAT_TOOLSET_AMBIGUOUS` otherwise).

So for a profile two personas declare, `profile:<that>` parses, creates, and
produces a DIFFERENT agent from either id that shares it: same name, none of
the defaults. `accepted_persona_spellings` therefore computes ownership over the
roster batch it was handed rather than reading `hermes_profile` off the row, and
offers the second spelling only to a sole owner. Both the refusal's list and the
`agent list` column spend that one function, so they cannot disagree about it.

## 3. Row 2's premise holds, and the launcher had already written the cure down

`sprite_payload` read the sheet bytes and base64-encoded them on every call, with
no way to ask for the shape alone. The launcher's own client says so in its
doc comment, unprompted, before this lane existed
(`lib/shared/charsheet/hermes_character_client.dart`, `sprite`): *"The verb has
no metadata-only mode and returns no path for the sheet, so this is
all-or-nothing"*, followed by *"see the note in the launcher field notes for the
`--no-sheet` mode hermes would need for the metadata-only case to be cheap."*
The flag is spelled `--no-sheet` because that is the spelling the consumer had
already written down.

The added key is **`sheet`** — the absolute path — and it is that word rather
than `path` or `sheetPath` because `characters list`'s installed rows already
call it `sheet` (`_characters_installed_rows`). One name for one file across
both verbs.

**The pets sibling was deliberately NOT mirrored.** `harness pets sprite` is
served by `_pet_sprite_payload_for_launcher`, a separate function in
`hermes_cli/harness.py` with its own `spritesheetBase64` and its own
`framesPerState`/`framesByState` keys that the character payload does not carry.
The row's condition was "mirror it ONLY if the same payload function serves it";
it does not, so the flag stops at the character verb. Filed as a row rather than
built.

## 4. A size probe that would have lied

The first cut of the metadata-only test asserted
`len(json.dumps(lean)) * 4 < len(json.dumps(full))` and went red: on the
test fixture's 6-row sheet the full payload is 1,908 bytes and the lean one
1,222. That is not a bug in the flag — it is the same trap the charsheet field
notes already record about the small 4-way sheet ("the size scales with the
sheet, so the small case proves nothing about the real one"), reached from the
other direction. A RATIO probe on a fixture sheet asserts nothing about the
468.8 KiB live case.

Both size probes are now stated as an exact saving —
`len(dumps(full)) - len(dumps(lean)) == entry("spritesheetBase64", full) - entry("sheet", lean)`
— which is scale-free and says the actual claim: the whole base64 came off, and
only the path went on.

## 5. Key ORDER is real in `sprite_payload` and not real at the CLI

The "default stays byte-identical" claim was pinned twice, and the two pins are
not the same claim:

* At `sprite_payload`, the key order is whatever the dict literal builds, so the
  conditional entry is spelled in the base64's ORIGINAL position rather than
  appended. `test_the_default_sprite_payload_is_untouched_by_the_no_sheet_mode`
  pins the first five keys in sequence.
* At the CLI, `emit_json` sorts, so the emitted bytes are alphabetical whatever
  the payload built and an order pin there is unfalsifiable. The first attempt
  at a CLI order pin failed for that reason and was replaced by an EXACT key-set
  comparison (`==`, not `>=`) — which catches the mutation the superset probes
  in that file cannot: adding `sheet` to every payload rather than only the
  metadata-only one.

## 6. Owed at landing

* The launcher's committed copy of the hermes CLI contract dump is now stale by
  one flag (`harness characters sprite --no-sheet`). Nothing was REMOVED, so no
  operator button exits 2 — but the fixture lies until it is re-synced and the
  sync recorded in the launcher's `tool/hermes_cli_contract/README.md`. This
  session cannot write the launcher vault; the row is handed back.
* `verify_harness_skill_install.py` refreshed the installed
  `harness-charsheet-authoring` package in the live `X:\Eternia\.hermes` store
  when `SKILL.md` changed. That is the documented post-merge step, but it is a
  write outside this repo and it happened from a worktree.
