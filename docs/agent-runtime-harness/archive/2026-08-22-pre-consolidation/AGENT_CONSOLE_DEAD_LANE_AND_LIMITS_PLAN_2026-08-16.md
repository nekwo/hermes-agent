# The console that couldn't say why — dead lanes, silent limits, and the two-homes split (2026-08-16)

> **Home.** Hermes repo, beside `OFFICE_OPTIMISTIC_RENDER_REGRESSION_PLAN_2026-08-16.md`, whose
> discipline this follows. Launcher paths relative to `X:/Unreal Engine/Engine/Launcher/EterniaLauncher`
> at `3633cba4f`; hermes paths relative to `X:/Eternia/hermes-agent` at `db73fe0b2a`. No production
> code was changed for this document; every stage is future work.

**Evidence tags**: **READ** (file:line inspected this session) · **RAN** (command executed this
session — read-only hermes verbs / an in-process probe printing class names only, never values) ·
**MEASURED-LIVE** (operator observation, relayed by the coordinator) · **RELAYED** (told to me,
not on disk) · **ASSUMPTION A-n** (unverified; named in §6).

---

## 0. Verdict up front

**The two defects do NOT share a cause, but they share an aggravator nobody named: the console and
the operator's shell interrogate two different Hermes homes.** The launcher runs every probe with
`HERMES_HOME=<root>/profiles/base` (READ `lib/features/mission_control/data/mission_control_settings.dart:159-190`,
`lib/features/mission_control/state/mission_control_provider.dart:387-406`); the operator's shell
uses `X:/Eternia/.hermes`. RAN both, same minutes, same machine:

| Fact | root home (`X:/Eternia/.hermes`) | launcher home (`…/profiles/base`) |
| --- | --- | --- |
| `opencode-zen` credential health | `auth_failed`, code 401 | **healthy** |
| `OPENCODE_ZEN_API_KEY` in `<home>/.env` | present | present, **different value** |
| active model / provider | `gpt-5.6-luna` / OpenAI Codex | **`big-pickle` / OpenCode Zen** |
| `harness usage` → openai-codex lane | available, plan Free, Session 1% used | **`available:false`, "no usage data"** |
| `harness usage` → anthropic lane | present | **absent** (`ANTHROPIC_TOKEN` only in root `.env`) |

So the 401 the coordinator diagnosed from the shell (`hermes auth list`) describes a world the
console never reads, and the "no usage data" the console shows describes a failure the shell cannot
reproduce. Every minute spent diagnosing one surface with the other's tools was the real time lost.

- **Defect 1** (big-pickle unselectable): the launcher-side plumbing for a failing credential is
  NOT missing — the typed 401 flows all the way to a rendered menu row (§1). What is genuinely
  silent at HEAD: the model **search** lane, the all-lanes-down collapse, and the fact that the
  console never states *which home* its facts come from. The coordinator's proposed mechanism
  ("a failing lane contributes nothing to `laneCatalogIds`, so its models are filtered out") is
  **not what the code does** — §1 corrects it with line evidence.
- **Defect 2** (Limits "no usage data"): a swallowed error, not an honest empty. Under the
  launcher's home the Codex `/usage` endpoint returns **HTTP 401**; `fetch_account_usage`'s blanket
  `except Exception: return None` (hermes `agent/account_usage.py:900-901`) erases the class before
  the harness's honest per-lane failure handler (`hermes_cli/harness.py:3344-3355`, which would have
  said `usage fetch failed (HTTPStatusError)`) ever sees it, and the None serializes as the
  unfalsifiable `"no usage data"` (`harness.py:3288-3292`). Deterministically reproduced (§2).

---

## 1. Defect 1 — where the 401 is known, and where honesty actually stops

**The typed lane is intact end to end.** RAN `hermes harness providers --json` (root home): the
payload carries `opencode-zen → credentials[0].health = {state: auth_failed, code: 401, message:
"auth failed (401) (re-auth may be required)"}` — built by `_credential_health`
(hermes `hermes_cli/harness.py:2931-2970`) from the same classifier `hermes auth list` uses.
Launcher side (all READ):

1. `parseHermesProvidersJson` keeps the health verbatim
   (`data/mission_control_hermes_visibility.dart:442-515`).
2. `laneCatalogIds` at `:981-986` is built from **all** `credentialProviders` + `report.authLogins`
   — health is not consulted. A 401'd `opencode-zen` still maps to catalog id `opencode` via
   `hermesCatalogAliasFor` (`:667-676`) and its 91 models load into `snapshot.catalog`. **The
   "filtered out entirely" mechanism is wrong at this layer.**
3. `hermesProviderLanes` (`:754-812`) marks the lane `connected:false` and carries
   `issue`/`issueState` (`:771, :781-782`).
4. The switcher view model builds a section for every lane, connected and not
   (`agent_chat/mission_agent_model_switcher_view_model.dart:353-359`), with `issue`/`issueState`
   (`:800-829`).
5. The menu widget renders unconnected lanes under a "Not connected" label with the verbatim issue
   as the subtitle, amber error icon, press → Manage providers
   (`agent_chat/mission_agent_chat_panel_parts/agent_model_menu.dart:240-261`).

**Where honesty stops (the actual gaps at HEAD):**

- **G-1, the search lane.** `searchMissionAgentModels` skips every `!section.connected` section
  (`mission_agent_model_switcher_view_model.dart:467-468`); the widget then renders
  `No models match "big-pickle"` (`agent_model_menu.dart:409-417`). An operator who types the model
  name — the natural gesture — gets copy indistinguishable from a typo, an unconfigured provider,
  or a dead credential. The paste path already answers honestly for the same state
  (`providerNotConnected` carries `lane.issue`, view model `:627-636`); search does not.
- **G-2, the failing lane's catalog is undiscoverable.** The "Not connected" row is a flat item —
  no submenu, no model count. 91 loaded models render nowhere.
- **G-3, the all-lanes-down collapse.** When NO lane is selectable the entire switcher disables
  with the generic `No connected provider exposes a model catalog — open Settings to connect one.`
  (`:360-368`) even when lanes carry precise issues. In that state the 401 is fully known and
  fully unrendered.
- **G-4, provenance.** No surface states which `HERMES_HOME` the facts describe. Given §0's split,
  `hermes auth list` in a shell is not a diagnostic for the console — and nothing says so. (The
  snapshot already models *source* provenance — `HermesVisibilitySource`, `:30-69` — but not *home*
  provenance.)

**Why the operator couldn't select big-pickle at struggle time:** not reconstructible from disk.
Today, under the launcher's home, the lane is healthy AND both catalog caches
(`<home>/models_dev_cache.json`, root and base, mtimes 2026-08-16 20:07-20:08, identical size)
carry `opencode` with 91 models including `big-pickle` (RAN). So the console *now* should offer it.
At struggle time the base world must have lacked either the lane or the catalog entry (A-1); the
probe artifacts that could date it are self-erasing — the providers probe rewrites
`<home>/auth.json` on every run (RAN: a repeat probe bumped it to 20:19:15), so mtimes date
nothing. The defect worth fixing is therefore the **class** (G-1..G-4: an absent-or-failing lane
explains itself nowhere the operator actually looks), not "make big-pickle appear" — it already
does.

---

## 2. Defect 2 — "no usage data" is a swallowed 401

Chain (all READ unless tagged):

1. The chip probes `hermes harness usage --json` through `HermesAccountUsageService`
   (`data/mission_control_account_usage.dart:340-359`), same profile env as every other probe;
   60 s staleness gate (`state/mission_control_provider.dart:448-458`). The parser is tolerant and
   the menu renders `unavailable_reason` verbatim (`provider_usage_menu.dart:553-574, :713-727`).
   The launcher is innocent here.
2. hermes detects the codex lane (OAuth logged-in or pool entry, `harness.py:3166-3181`) and
   fetches via `_fetch_usage_lane → agent.account_usage.fetch_account_usage`
   (`harness.py:3226-3238`).
3. `fetch_account_usage` wraps ALL three provider fetchers in `except Exception: return None`
   (`agent/account_usage.py:884-902`). `_serialize_usage_lane` renders None on a detected lane as
   `"no usage data"` (`harness.py:3288-3292`).
4. RAN, `HERMES_HOME=<base>`: the envelope is exactly the operator's screenshot — one lane,
   `openai-codex`, `available:false`, `"no usage data"`, zero windows ("Updated just now" is the
   footer's fetch-age label, `provider_usage_menu.dart:877-884`; the "0" badge is read as the
   provider monogram, A-4).
5. RAN, in-process under the same home, class-name-only output: `_fetch_codex_account_usage`
   raises **`HTTPStatusError`, status 401**, at `agent/account_usage.py:524`
   (`raise_for_status`). Under the root home the identical call returns 200 (plan Free, one
   Session window). So the base home resolves a Codex token upstream rejects — which store copy is
   stale is A-3; the fix below does not depend on it.

The bitter part: the harness ALREADY has the honest handler — `_fetch_usage_lanes` catches
per-lane exceptions and emits `usage fetch failed (<ClassName>)` (`harness.py:3344-3355`, tested
at `tests/test_harness_usage.py:75`). The upstream blanket except **starves it**. One layer's
fail-open policy erased the fact the next layer was built to report — the same lane-ambiguity
class as defect 1: "no data" and "data you cannot reach" rendered identically.

**Independence proof:** defect 1 lives on `opencode-zen` (API-key lane, not even a usage-lane
candidate — `_USAGE_LANE_PROVIDERS`, `harness.py:164-169`); defect 2 lives on `openai-codex`
(OAuth, healthy in both homes' provider payloads). Neither fix touches the other's lane. Shared
aggravator only: the per-home split (§0) and the swallowed-failure class.

---

## 3. Fix shape — smallest honest surfacing

The parallel agent is designing first-class provider logins (settings + console). Everything below
is the **interim diagnostic floor** that design will subsume: no new settings surface, no auth
flows, copy + plumbing only. Stages ordered so each is independently shippable.

### S1 — hermes: a failed usage fetch says what failed (defect 2, the fix)

`hermes_cli/harness.py` only — `agent/account_usage.py` is upstream-owned ("must not modify",
`harness.py:3118-3119`; the PUSH/RPC fork-boundary ruling points the same way: route around
upstream, don't patch it).

- In `_fetch_usage_lane` (`:3226-3238`), stop routing the three shared lanes through
  `fetch_account_usage`; dispatch directly to `_fetch_codex_account_usage` /
  `_fetch_anthropic_account_usage` / `_fetch_openrouter_account_usage` (module-level, importable)
  so exceptions propagate to `_fetch_usage_lanes`' existing handler. (The nous lane already
  bypasses.)
- Extend that handler (`:3350-3355`): when the exception is `httpx.HTTPStatusError`, emit
  `usage fetch failed (HTTP <code>)`, and for 401/403 append ` — re-auth may be required`. Keep
  the class-name-only discipline for everything else (no message text — the token/URL leak guard,
  comment at `:3327-3330`, stays intact; a bare status code leaks nothing).
- `"no usage data"` remains ONLY for a genuine `None` (a fetcher declined without raising).

*Test* (`tests/test_harness_usage.py`): monkeypatch `_fetch_codex_account_usage` to raise
`httpx.HTTPStatusError` carrying a 401 response; assert the lane is unavailable with reason
`usage fetch failed (HTTP 401 — re-auth may be required)`. Second case: a raising anthropic
fetcher must not sink the codex lane (extends `:75`). Update `:115`'s None-case docstring — None
now means "declined", not "anything broke". *Mutation proving non-vacuity:* revert
`_fetch_usage_lane` to call `fetch_account_usage` — the 401 test regresses to `"no usage data"`
and goes red.

*Gate:* unit tests green AND `HERMES_HOME=<root>/profiles/base hermes harness usage --json` (re-RAN
post-fix) shows the codex lane reason carrying `HTTP 401`. **Zero launcher changes needed** — the
chip already renders the reason verbatim, so the operator's screenshot becomes "usage fetch failed
(HTTP 401 — re-auth may be required)" with no launcher rebuild.

### S2 — launcher: search names the models it is hiding (defect 1, G-1)

`mission_agent_model_switcher_view_model.dart`: add a pure
`searchMissionAgentUnavailableModels(sections, query)` (same tokenizer as
`searchMissionAgentModels`) that scans `!connected` sections and returns hits carrying the
section's `issue`/`issueState`. `agent_model_menu.dart` `_searchChildren` (`:409-440`): after the
selectable hits (and replacing the bare "No models match" caption when only unavailable hits
exist), render each unavailable hit as a DISABLED row — label = model, subtitle =
`<lane>: <issue ?? 'not connected — connect in Settings'>`, amber icon when `issue != null`,
press → `onManageProviders`. No enabled affordance — selecting a model on a dead lane stays
impossible; the *reason* stops being invisible.

*Test* (`test/features/mission_control/mission_agent_model_switcher_view_model_test.dart`): a
failing `opencode-zen` section whose catalog contains `big-pickle`; query `big-pickle` → zero
selectable hits, one unavailable hit carrying the 401 message. Widget test (menu part, anchored in
`mission_agent_chat_panel_test.dart`): the row renders disabled with the subtitle. *Mutation:*
make the new helper skip sections with `issue != null` — test red.

*Gate:* searching a model that exists only on a dead/unconfigured lane can no longer render copy
identical to a typo.

### S3 — launcher: the all-lanes-down collapse keeps the reasons (defect 1, G-3)

`buildMissionAgentModelSwitcher` (`:360-368`): when `!anySelectable` and any section carries an
`issue`, the `catalogUnavailable` reason becomes the joined lane issues (e.g.
`opencode-zen: auth failed (401) (re-auth may be required)`) instead of the generic sentence.
*Test:* a snapshot whose only lane is failing → `unavailableReason` contains `auth failed (401)`.
*Mutation:* drop the issue branch — test red. *Gate:* a console whose only lane died says which
lane and why, in the disabled pill's tooltip (`agent_model_menu.dart:98-104` already renders
`unavailableReason`).

### S4 — launcher: home provenance, one sentence (defect 1, G-4 — interim for the split)

Carry the probe's `HERMES_HOME` into the snapshot (`HermesVisibilityService`'s provider already
closes over it — `mission_control_provider.dart:389-405`; add a nullable `probedHome` field) and
render it in two cheap places: the switcher's "Manage providers…" row subtitle
(`agent_model_menu.dart:205-213`) and the settings drawer's existing degradation-note slot. Copy:
`Reading providers from <path>`. *Test:* view-model/widget test asserts the path renders; a
snapshot without a home renders nothing. *Mutation:* null out the field wiring — test red.
*Gate:* the next "shell says X, console says Y" session starts with the answer on screen. This
stage is explicitly subsumed by the provider-logins design; it is one field + two captions, worth
shipping first as the interim.

### S5 — verification + handoff (no code)

- Stage C screenshot (MCP path, read-only against the live window) of the model menu and the
  Limits dropdown after S1 ships, for the ledger.
- Operator guidance (advisory only; no credential is touched by this work): the dead 401 key sits
  in the ROOT home's `.env`; the launcher world holds a *different* key that currently reads
  healthy. If the S1 reason confirms the Codex 401 persists, re-auth must happen under the
  launcher's home (`HERMES_HOME=<root>/profiles/base hermes auth …`) or it fixes the wrong world.
- Hand §0's split + A-3 to the provider-logins agent as a named input: any first-class login UI
  that reads/writes a different home than the serve child recreates both defects.

---

## 4. What this does NOT fix

- **The dead OpenCode Zen key itself.** Operator's to rotate; per instruction, untouched.
- **The base-home Codex 401.** S1 makes it *visible*; re-auth/refresh under the right home (or the
  logins agent's token write-through design) makes it go away. Which store copy is stale is A-3.
- **The per-home credential/dotenv split.** Root `.env` carries 15 vars incl `ANTHROPIC_TOKEN`;
  base `.env` carries exactly one. The Limits panel under the launcher will keep omitting
  Anthropic until that design lands. Named, not fixed.
- **G-2 in full** (a browsable catalog under a failing lane). S2 covers the search gesture; a
  disabled 91-model submenu is menu furniture the logins design should own.
- **A warning chip on the trigger pill when the ACTIVE model rides a failing lane.** Real gap,
  same class; deferred to keep this minimal — note the active lane under base is currently
  opencode-zen, so if *that* key dies there, the pill goes silent again until this lands.
- **Which launcher binary the operator was running** during the struggle. Unknowable from here.

## 5. Deliberately deferred

- Making `fetch_account_usage` itself honest — it is upstream; the fork boundary says route
  around, not patch.
- Any settings surface, login wizard, or credential-store unification — the parallel agent's.
- A usage lane for opencode-zen (no provider usage API established; would be invented data).

## 6. Adversarial pass — what I most expect to be wrong

1. **The struggle-time reconstruction (A-1) is the weakest claim.** Today's disk state offers
   big-pickle; I infer the base world lacked the lane or the catalog entry *then*, but probe
   writes erase history (`auth.json` rewritten per probe — RAN). If the console actually DID show
   the lane and the operator missed the bottom "Not connected" group, S2/S3 still fix the right
   thing, but the narrative softens to "the honest copy existed once, in the one place the gesture
   never goes".
2. **A-2: my first base-home probe may itself have seeded `profiles/base/.env`** (its mtime,
   20:16:20, coincides with that probe; repeat probes provably do not rewrite it, first-write
   seeding unfalsified from here). If so, pre-probe base resolved the opencode key differently —
   which strengthens A-1 but means the investigation perturbed the measured system. Either way the
   two homes' key values differ NOW (measured by name-only equality, no values printed), and that
   is the load-bearing fact.
3. **S1's copy contract.** Something downstream might pattern-match the literal `"no usage data"`.
   I found no launcher parsing of it (the strings at
   `mission_control_account_usage_policy.dart:237-239` are launcher-authored fallback-disclosure
   copy, not a parse), and in hermes only `harness.py` + its tests emit/assert it (RAN grep) — but
   the S1 change should re-grep both repos before landing.
4. **The 401 under base could be transient** (a refresh race) rather than a stale store copy. The
   fix is indifferent — S1 reports whatever class/status occurs — but S5's operator guidance would
   change from "re-auth under base" to "wait/refresh". The S1 gate's live re-run decides.
5. **A-4: the screenshot's "0 badge" is read as the `_ProviderMonogram` letter "O"**, not a
   numeric zero. Cosmetic; nothing in the plan depends on it.

## 7. Verification log

| # | Fact | How established |
| --- | --- | --- |
| V-1 | Typed 401 emitted by hermes: `opencode-zen` → `auth_failed`, code 401 (root home) | RAN `hermes harness providers --json`; READ `harness.py:2931-2970` |
| V-2 | Same provider, same machine, launcher home: `healthy`; env model `big-pickle` / OpenCode Zen | RAN with `HERMES_HOME=X:/Eternia/.hermes/profiles/base` |
| V-3 | Launcher probes + catalog read run under `<root>/profiles/<profile>` | READ `mission_control_settings.dart:159-190`, `mission_control_provider.dart:387-406` |
| V-4 | `laneCatalogIds` ignores health; a failing lane's catalog loads (91 models incl `big-pickle`, both homes' caches) | READ `mission_control_hermes_visibility.dart:981-1000`; RAN catalog JSON check |
| V-5 | Failing lane renders with verbatim 401 subtitle in browse mode at launcher HEAD | READ `agent_model_menu.dart:240-261`; view model `:353-359, :800-829` |
| V-6 | Search excludes unconnected lanes; "No models match" carries no reason | READ view model `:467-468`; menu `:409-417` |
| V-7 | All-lanes-down collapse is generic despite known issues | READ view model `:360-368` |
| V-8 | Limits chip renders `unavailable_reason` verbatim; "Updated just now" = fetch-age footer | READ `provider_usage_menu.dart:553-574, :877-884` |
| V-9 | `"no usage data"` emitted only for a None snapshot on a detected lane | READ `harness.py:3288-3292` |
| V-10 | Operator's exact Limits state reproduced under launcher home: codex lane unavailable, `no usage data`; anthropic lane absent | RAN `HERMES_HOME=<base> hermes harness usage --json` |
| V-11 | Root-home usage fetch succeeds (plan Free, Session 1% used) | RAN `hermes harness usage --json` |
| V-12 | The swallowed exception is `HTTPStatusError` 401 at `account_usage.py:524`; blanket except at `:900-901` | RAN in-process probe (class + status only); READ `account_usage.py:884-902` |
| V-13 | An honest per-lane failure handler exists and is starved | READ `harness.py:3344-3355`; `tests/test_harness_usage.py:75` |
| V-14 | `opencode-zen` is not a usage-lane candidate (defect independence) | READ `harness.py:164-169` |
| V-15 | `.env` split: root 15 vars, base 1; `OPENCODE_ZEN_API_KEY` values differ (name-only diff, no values printed) | RAN dotenv name/equality check |
| V-16 | Providers probe rewrites `<home>/auth.json` every run; `.env` not rewritten on a repeat probe | RAN stat before/after repeat probe |
| V-17 | Dotenv resolution is per-`HERMES_HOME` | READ `hermes_cli/env_loader.py:297-318` |
| V-18 | Operator lost real time; screenshot: Codex lane "no usage data", "Updated just now" | MEASURED-LIVE / RELAYED (coordinator) |
