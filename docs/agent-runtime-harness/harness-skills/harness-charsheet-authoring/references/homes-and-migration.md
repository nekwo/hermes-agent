# Homes, roots and migration — the preflight long form

**When to open:** a draft the operator names is not in your `list`; a payload or
refusal talks about `hermes_home`; `status --json` reports a home you did not
expect; the image provider fails a probe; you are asked to run `backfill-home`
or `migrate-home`. The one-pair preflight in `SKILL.md` is the whole standard
path — this file is the war stories behind it.

## Preflight — three probes, before any generation

**1. Which home am I authoring into — and which library does it reach?** The
character library is **install-wide**: `<hermes_root>/shared/characters`
(`characters_dir()` → `hermes_constants.get_shared_characters_dir()`), drafts
under `.drafts/<draft-id>/`, installed sheets under `<slug>/`. **One directory
for every persona and every profile under one hermes root** (§13.27). It sits
beside `profiles/`, not inside any one of them, and every profile home computes
the same answer from its own `HERMES_HOME` with no env injection — the same
property `shared/skills` has.

So the question that used to matter — *can my home see the operator's draft?* —
has no answer any more, because it has no content. It cannot. Every draft on
this install is in your list. If the operator names one and it is not there, the
draft does not exist on this install or your ROOT is wrong, and those are the
only two possibilities left.

Your turn's `HERMES_HOME` is still **your persona's own profile home** — the
runtime rebinds it for the duration of the turn from the persona's
`hermes_profile` (`profile_context.persona_profile_context`, which rebinds a
ContextVar *and* `os.environ`) — and it still matters, for credentials
(`auth.json` is per-home, probe 2) and for everything else profile-scoped. What
it no longer does is scope the library. The operative instruction is unchanged
and is now about the ROOT: *read back the home the runtime resolved, never
assert one*:

```
hermes harness status --json      # → .runtime_health.hermes_home
hermes harness characters list --json
```

**Echo that path in your first reply, in prose.** Nothing else carries it — the
`CHARSHEET-QA:` line deliberately carries no home and is not to grow one
(§13.22's reader half stands), so your transcript is the only place the home
your turn resolved will ever exist. Under one library that echo is not a
scoping check any more; it is the discipline that surfaces a mis-resolved ROOT.
A wrong profile is now harmless for characters. A wrong root is a different
install, and the symptom is an empty list where the operator expects a draft.

Two ways the root still goes wrong, both seen live:

- **A relative `HERMES_HOME` resolves against the shell's cwd.** `HERMES_HOME`
  is used as written (`hermes_constants._hermes_home_from_env`), and the CLI
  trusts any value whose immediate parent directory is named `profiles`. So
  `HERMES_HOME=profiles/base` run from a repo checkout authors into a
  `shared/characters` *inside the repo working tree* — a whole second library
  nobody else can see. A `fox-scout` character sits in one today and had to be
  gitignored. Always absolute.
- **A home that is not profile-shaped is re-pointed at the sticky active
  profile.** If `HERMES_HOME` names the hermes ROOT (or is unset), the CLI reads
  the `active_profile` marker and rewrites the home to that profile
  (`hermes_cli/main.py:_apply_profile_override`). Both spellings land on the
  same library when the root is the same, which is exactly the bleed the one
  library made harmless — but the rewrite is still a different SHAPE, and it is
  what makes an unset `HERMES_HOME` reach the platform-default shadow root
  instead of this install's.
- **Never rebuild a path from a slug or an attempt number.** Every payload names
  its own `directory` / `path` / `source`. Read those — and treat an absent path
  as absent whichever way it arrives (`null` or `""`). Never turn one into a
  bare `MEDIA:` line. That includes the library path itself: you never compose
  `shared/characters`, you read `directory` off the row.

**A `hermes_home` on a draft is provenance, not an address.** `characters list
--json` carries `hermesHome` per draft (string or `null`). It records which
profile's turn authored the draft — the profile-side complement of
`authoredBy`'s persona — and it is NOT where the draft sits, which is the
library and is the same for all of them (§13.27, re-deriving §13.26). A draft
naming a home that is not yours is telling you who authored it, not that you are
looking in the wrong place. Never chase it.

**2. Does the image provider actually return an image?** Credentials are
per-home (`auth.json`), and a plan-gated account fails *politely*: HTTP 200, the
`image_generation` tool silently stripped from the request, and a model that says
it has no such tool. It reads exactly like a broken plugin. Spend one cheap
`image_generate` call before committing to a 16-generation run. (`image_generate`
is the TOOL; `image_gen` is the toolset that carries it, and separately the
config section that selects the provider — only one of the two is callable, and
they differ by one word.) If nothing comes back, report "the image provider is
unavailable in this home" and name the home — never start the pipeline and
discover it on generation twelve.

- **A `401 token_expired` right after a plan change is a STALE STORED TOKEN, not
  a missing credential.** An account upgrade invalidates the token already on
  disk, and it keeps a local expiry hours out — so the probe fails while
  `auth.json` looks perfectly healthy. This fires immediately after the operator
  did the right thing about the trap above, which makes it the most confusing
  one: sending them back to check `auth.json` placement is the correct answer
  for a home with no credentials and the wrong answer here. Force a refresh and
  re-probe **before** reporting the provider unavailable; the refreshed token
  reads the new plan.

**3. Who is authoring — and pass it, verbatim.** `--authored-by` on `start` is
free text nobody validates, and it is the only provenance a draft carries. Your
own identity clause states the id you must use: *"You are (display name)
(Mission Control persona id: `<id>`)"*. Copy what is inside that parenthesis. On
a live run an agent instead wrote a slugified copy of its instance DISPLAY name,
and that value resolves to nothing — not a persona id, not a profile.

The cost is downstream and it is not small. The console addresses a draft as
*(persona, draft id)*, so a draft with **no** `authored_by` refuses to resume at
all — a typed refusal that names what it does not know (§13.21) — and a draft
carrying an id no roster holds refuses as "persona unresolved". Omitting the
flag is not neutral: it strands the draft.

## The `migrate-home` and `backfill-home` lore

**`migrate-home` is an operator's verb and its receipt is the whole point.** It
exists for one historical shape: a home that still holds a pre-library
`<HERMES_HOME>/characters` store from before the library was install-wide. It
moves those entries into the library — drafts keep their directory leaf names so
a stored draft id still resolves, installed characters keep their slugs, a draft
carrying no `hermes_home` is stamped with the home it is LEAVING before it moves
(afterwards the directory no longer witnesses where it lived), a destination
collision is a per-entry refusal rather than an overwrite, and nothing at all is
deleted — the emptied source tree is left standing as its own tombstone. The
receipt names both addresses on every row; hand it to the operator verbatim
rather than summarising it. Do not fire it as part of an authoring flow: it is
run once per home, by an operator who decided to.

**On THIS install it has already been run (2026-08-28) — `base` and `alice`,
the only two homes that held a legacy store, and a sweep found no others.** So
if you are asked to migrate here, expect an empty receipt and say so rather
than hunting for the drafts: the verb is idempotent, a second run moves
nothing, and an empty `moved` list is the correct answer, not a failure. The
emptied `<HERMES_HOME>/characters` trees you may still see on disk are the
tombstones the ruling leaves behind on purpose — they are not un-migrated
work.

## Footnote — the long entrypoint spelling

`SKILL.md` teaches the bare `hermes` entrypoint and nothing else, because the
long spelling is what agents then copy into every trace row. It is recorded
here for a restricted shell where the console script is not on `PATH`:

`hermes` == `python -m hermes_cli.main`.
