# Provider logins first-class — Settings + agent console (Plan PL, 2026-08-16)

> **Home.** Hermes repo, beside `AGENT_CREATE_ONE_CALL_PLAN_2026-08-16.md`,
> whose house S-stage format this plan follows. Scope spans BOTH repos:
> hermes fork `X:/Eternia/hermes-agent` (main `db73fe0b2a`) and launcher
> `X:/Unreal Engine/Engine/Launcher/EterniaLauncher` (main `3633cba4f`).
> Informed by upstream's Electron desktop app, read from
> `upstream/main:apps/desktop/` — judged, not transcribed (§2.1).

**Evidence tags used throughout:**

- **READ** — file:line inspected this session (fork HEAD, upstream/main ref,
  or launcher working tree).
- **RAN** — command/grep executed this session.
- **RELAYED** — statement from the commissioning brief (including its live
  `hermes auth list` output) or an operator ruling not re-measured here.
- **ASSUMPTION A-n** — unverified; stage PL-0 verifies before anything builds
  on it.

**Verdict up front.** Hermes is already the single credential authority
(auth.json + .env + config mirrors behind one lifecycle choke point, READ
`hermes_cli/credential_lifecycle.py:1-40`), and the launcher already has a
typed, provenance-tagged read path (`provider_visibility/v2` →
`HermesConnectionSnapshot` → `hermesProviderLanes`, READ
`mission_control_hermes_visibility.dart`). What is missing is exactly three
things: (1) the launcher cannot SEE a provider that has no credential at all —
lanes are built only from credentials-present and logins-present, so
"never configured" is unrepresentable and indistinguishable from "dead"
(READ `:754-812`, `:981-986`); (2) the launcher cannot PERFORM a login — every
affordance dead-ends at display-only tiles and "open Settings to connect"
copy with nothing to click (READ `hermes_install_panel.dart:1386-2200`,
`mission_agent_model_switcher_view_model.dart:366`); (3) the fork has no
non-interactive login verbs — `hermes auth add` is a TTY flow
(`masked_secret_prompt`, blocking device-code prints, READ
`auth_commands.py:164-435`), while the non-interactive machinery that upstream
Desktop drives (session-based start/poll device-code, PKCE submit) sits in our
fork's `hermes_cli/web_server.py:9714-10850` welded to FastAPI. The plan:
extend the visibility contract additively (v2→v3 catalog block, one authority
for provider identity, flows, and models.dev mapping), add two small
non-interactive CLI verbs that route through the EXISTING choke points with
secrets never in argv, and wire the existing launcher tiles/picker to them.
Settings owns credential actions; the console owns consequences. Nothing gets
a second store, a third transport, or a new surface.

## 0. The ask

Make provider logins first-class in the launcher's Harness Settings and the
Mission Control agent console. The defect class this designs against
(RELAYED, and the same lane-ambiguity class as the office bugs): a provider
whose credential fails auth contributes no usable lane, its models vanish
from pickers, and a dead credential renders identically to a
never-configured provider — "no data" and "data you cannot reach" must never
share a pixel. Two live defects (silent-401 model vanish; Limits "no usage
data") are a parallel agent's; this plan absorbs them as requirements (§8).

## 1. Baseline

### 1.1 Hermes fork — what exists (all READ unless noted)

- **Stores.** Credentials live under HERMES_HOME: `auth.json` (pooled
  credentials + OAuth token blocks, cross-process file locking —
  `hermes_cli/auth.py:6,902-961,1103`), `.env` (canonical API-key secret
  store), `config.yaml` mirrors. `credential_lifecycle.py` is the declared
  single choke point for save/remove across all three, with an explicit
  secrecy contract ("no function in this module logs, prints, or returns a
  credential value", `:37-39`) and the #51071/#59761/#62269 bug family as its
  rationale (`:14-30`).
- **Typed visibility.** `build_provider_visibility()`
  (`hermes_cli/harness.py:2973`, schema `hermes.provider_visibility/v2`
  `:3009`) emits `providers[].credentials[].health` (typed
  healthy/auth_failed/rate_limited/exhausted/dead), plus failure-isolated
  `environment` (`:3021`), `api_keys` (`:3025`), `auth_logins` (`:3029`)
  blocks. Tests: `tests/test_provider_visibility_v2.py`,
  `tests/cli/test_harness_providers.py` (RAN, ls).
- **Provider identity.** `hermes_cli/provider_catalog.py:83`
  `provider_catalog()` → `ProviderDescriptor` (slug, name, auth type, env
  vars); `:179` by-slug index. Standalone module, no FastAPI import.
- **Interactive-only login.** `auth add` prompts for API keys
  (`auth_commands.py:200`) or runs blocking browser/device-code flows
  (`:227-435`; device-code functions at `auth.py:7823,7893,8230,8524`).
  `--api-key` exists as a getattr but no parser flag defines it (RAN grep of
  `_parser.py` — no match), so non-interactive today means piping stdin into
  a `getpass` fallback (`secret_prompt.py:56-81`) — undefined-on-Windows
  behavior (A-2).
- **Non-interactive machinery, wrong wrapper.** The fork's own
  `web_server.py` carries the full session-based flow upstream Desktop uses:
  `GET /api/providers/oauth` (`:9764`, with `token_preview` = last-N-chars
  never the full token), start/submit/poll/cancel (`:10749-10850`),
  `_build_oauth_catalog()` (`:9714`) layering OAuth overrides on
  `provider_catalog()`. All of it lives inside the FastAPI dashboard process
  the launcher does not run.
- **Recovery verbs.** `auth remove` routes source-specific cleanup through
  `agent.credential_sources` RemovalSteps (`auth_commands.py:464-499`);
  `auth reset` clears exhaustion statuses (`:502-507`).

### 1.2 Launcher — what exists (all READ)

- **One process identity.** Every hermes child — the persistent
  `harness serve --ndjson` child AND every one-shot CLI probe — is spawned
  from the same `MissionControlProcessIdentity` env projection
  (`mission_control_provider.dart:50-77`), with the HERMES_HOME triple
  recorded in transport receipts (`:111-150`). Logins performed through this
  identity land in the same store the runtime reads. (The ambient-shell
  HERMES_HOME may differ — receipts exist precisely to catch that,
  `:135-146`.)
- **Two lanes, not three.** Serve-first argv with silent CLI fallback
  (`runMissionControlCommandPreferServe`,
  `mission_control_serve_session_io.dart:1685-1727`) plus a serve-first
  streaming runner (`:1739+`). Receipts record argv.
- **Typed snapshot.** `HermesVisibilityService.probe` runs
  `harness status --json` + `harness providers --json` in parallel, with
  provenance enum (typedV2/typedV1/textFallback/unavailable) and per-section
  degradation notes (`mission_control_hermes_visibility.dart:824-1043`).
  `hermesProviderLanes` (`:754-812`) is the shared lane authority; the
  models.dev alias map is launcher-hardcoded (`hermesCatalogAliasFor`,
  `:667-676`, hand-verified 2026-07-08 per its own comment).
- **Display-only Settings.** Harness Settings drawer
  (`secondary_drawers.dart:698,776`) hosts `_HermesConnectionPanel`
  (`hermes_install_panel.dart:1386-1430`); `_HermesProviderTile` (`:1901`)
  renders per-lane health cues (`re-auth required`, `rate-limited`, …) but
  offers NO action — no connect, no re-auth, no remove.
  `_HermesApiKeySummary` (`:2168`) renders `configuredApiKeys` /
  `missingApiKeyCount` as a second, parallel representation of key state.
- **Console.** The model switcher already renders unconnected lanes as
  disabled sections carrying `issue`/`issueState`
  (`mission_agent_model_switcher_view_model.dart:134-163,353-368`), and its
  "Manage providers…" footer routes to the Harness Settings drawer
  (`canvas_shell.dart:434`). The per-provider Limits panel is
  `provider_usage_menu.dart` fed by `hermes harness usage --json`
  (`mission_control_account_usage.dart:4,314`).
- **Live shape** (RELAYED from the brief): `opencode-zen` holds an
  `auth failed (401)` API key; `anthropic` (oauth), `huggingface` (api_key),
  `openai-codex` (oauth device_code) healthy.

### 1.3 What "never configured" looks like today — the gap, precisely

`hermesProviderLanes` unions credential-holding providers with auth-login
lanes (`:786-811`). A provider with neither — say the operator has no
Fireworks key — produces NO lane; the Settings roster and every picker
simply omit it. Meanwhile a 401'd provider produces an unconnected lane with
an issue. So the two states ARE distinguishable in the data model — but only
when the credential exists; the roster cannot say "here is what you COULD
connect", and nothing can be clicked in either case. The commissioning
brief's "indistinguishable" is true at the surface level: both render as
absence-of-usable-models in the dropdown's connected half.

## 2. Upstream study — judged

### 2.1 Worth taking (and how it maps)

- **U-1 Backend-authoritative provider identity.** Desktop's Keys tab groups
  by the backend's `provider_label`/`provider` from the unified catalog —
  "the SAME provider identity `hermes model` uses. This is authoritative"
  (READ upstream `providers-settings.tsx:60-80`). Take it: the v3 catalog
  block (PL-1) makes hermes the one authority for lane identity, display
  name, auth flows, key-var name, AND the models.dev catalog id — which
  deletes the launcher's hand-verified alias map.
- **U-2 Session-based, poll-driven OAuth with the secret never touching the
  client.** Desktop shows `verification_uri` + `user_code` and polls; tokens
  land server-side; status carries `token_preview` only (READ upstream
  `hermes.ts:1081-1130`, our `web_server.py:9764-9786`). Take the SHAPE, not
  the transport: PL-2's `auth login --json` NDJSON stream is the same
  contract over the launcher's existing process-stream lane.
- **U-3 Deferred credential warning.** Desktop stashes
  "No API key configured…" warnings and gates at SUBMIT time — "popping the
  blocking onboarding overlay here punishes merely LOOKING at an
  unconfigured profile" (READ upstream `store/onboarding.ts:393-425`,
  `provider-setup-errors.ts`). Take verbatim as console policy (PL-5).
- **U-4 Reauth vs connectivity discipline.** "Only a confirmed 401/403 (or
  an explicitly tagged auth rejection) means reauthentication; timeout,
  network, malformed-response, and server failures remain connectivity
  errors" (READ upstream `AGENTS.md` auth corollaries;
  `boot-failure-reauth.ts:52-66`). Our typed health already encodes this
  server-side; PL-4 pins it at the UI: probe failure ⇒ neutral copy, never a
  sign-in nag.
- **U-5 The at-rest secret contract as a test discipline.** Upstream's
  `at-rest-connection-token.spec.ts` asserts the RAW BYTES of the secret are
  absent from every file the app writes, that the credential still works,
  and deliberately does NOT assert the storage mechanism ("that would be a
  change-detector", READ spec header). Take the analog: PL-4's sentinel test
  — a fake key entered through the launcher flow must appear in NO receipt
  log, snapshot, or launcher-written file, while `auth list` shows the
  credential present.
- **U-6 Disconnect semantics.** Hermes-managed credentials get a real remove
  affordance; external-CLI-owned credentials (claude_code, gh_cli) get the
  documented removal command shown to the user, never a silent delete —
  "Hermes never deletes creds another tool owns behind a silent API call"
  (READ upstream `providers-settings.tsx:330-345`). Our `auth remove`
  RemovalStep registry already implements the cleanup side; PL-4 surfaces
  the split.
- **U-7 One picker, two entrances.** Desktop's Settings provider list is "a
  near-1:1 replica of the first-run onboarding picker … the leaf cards are
  the exact shared components" (READ `providers-settings.tsx:126-137`). Our
  analog: one `ProviderConnectController` + one tile/sheet set, mounted by
  the Settings drawer and deep-linked from the console — never a second
  credential-entry UI in the console.

### 2.2 NOT worth taking, and why

- **Electron's client-side token store** (safeStorage, `connection.json`,
  dashboard tokens). That machinery exists because Desktop can attach to
  REMOTE gateways and must hold a session credential of its own. Our
  launcher spawns a local hermes under its own identity; a launcher-side
  secret store would be the forbidden second store. The launcher should hold
  a credential value for exactly the lifetime of one entry gesture, in
  memory, and never persist it.
- **The `/api/env` PUT / reveal surface as a UI model.** Desktop's Keys tab
  is an env-var editor with a reveal endpoint (`web_server.py:7429`).
  Reveal-style read-back is precisely what our secrecy constraint forbids,
  and env-var-editing invites drift from the pool. The launcher writes
  through login verbs only; key state is displayed as presence + preview,
  never value.
- **The full first-run onboarding overlay.** Mission Control already has a
  runtime gate and install panel (`_HermesRuntimeGate`,
  `hermes_install_panel.dart:14-44`); a modal onboarding replica would be a
  new surface where the goal is fewer. The submit-time gate (U-3) covers the
  unconfigured-runtime case without one.
- **The tui_gateway credential methods as a launcher transport.** The fork's
  `tui_gateway/methods_complete.py:359-467` already fronts the lifecycle
  choke point over JSON-RPC — but adopting it gives the launcher a THIRD
  hermes transport (serve NDJSON + argv + tui-gateway WS). Rejected;
  PL-2 gets the same effect over the lanes that already exist.
- **Desktop's hardcoded `PROVIDER_DISPLAY` order/title table**
  (`onboarding/providers.tsx:6-17`) — presentation metadata belongs in the
  catalog the backend serves (U-1), not a client table that drifts.
- **Desktop's poll/mtime refresh push.** Per the standing PUSH/RPC ruling
  (RELAYED, memory 2026-08-13): upstream's push is weaker than ours; the
  snapshot invalidation + our own event lane stay.

## 3. Target architecture (one paragraph)

Hermes stays the only credential store and becomes the only provider-identity
authority: `provider_visibility/v3` adds an additive `catalog` block naming
every connectable provider (id, display name, supported flows, key-var,
models.dev id, docs URL, external-owner disconnect command), so the launcher
can render never-configured lanes and delete its alias hardcode. Two new
non-interactive fork verbs — `hermes auth set-key <provider> --stdin`
(routes `save_provider_env_credential`; secret arrives on stdin, exists only
in launcher memory during the gesture) and `hermes auth login <provider>
--json` (NDJSON event stream: `code` → `pending`* → `done|error`, wrapping
the existing device-code functions) — are driven from ONE launcher
`ProviderConnectController`, spawned DIRECT (never through serve, so no
secret and no minutes-long flow transits the serve protocol; receipts record
argv + event kinds only). The Harness Settings drawer's existing provider
tiles gain the per-state affordance (Connect / Sign in again / Reset status /
Remove / run-this-command-yourself); the console keeps consequences only —
picker sections with typed reasons and a provider-anchored jump to Settings,
plus a submit-time gate. Every distinguishable state gets named copy; two
parallel representations (API-key summary, alias map) are deleted.

## 4. The named states

The contract every surface renders from — each state has a detection
predicate on existing typed data (plus the v3 catalog), and no two states may
share copy:

| # | State | Predicate (snapshot terms) | Roster copy / affordance |
|---|---|---|---|
| N1 | Never configured | in v3 `catalog`, no credentials, no login | "Not connected" · **Connect** |
| N2 | Configured, auth-failed (the silent-401) | credential present, `issueState == authFailed`, code 401/403 | "Re-auth required — {health.message}" · **Sign in again** (+ Remove) |
| N3 | Expired / dead OAuth | `issueState == dead`, or OAuth login `logged_in == false` with a prior refresh | "Session expired" · **Sign in again** |
| N4 | Rate-limited / exhausted | `issueState == rateLimited|exhausted`, `retryAt` | "Rate-limited — retry {countdown}" · **Reset status** (auth reset), no sign-in nag (U-4) |
| N5 | Network-down / probe degraded | `probeFailure != null` or source ≠ typedV2 | degradation note verbatim; NO per-provider verdicts (U-4: connectivity is not reauth) |
| N6 | Hermes absent / not installed | existing `_HermesRuntimeGate` states | install panel (unchanged) |
| N7 | Healthy | healthy credential or logged-in OAuth | "Connected" · kind badges · token/key preview (v3) · Remove |
| N8 | External-tool-owned | v3 `disconnect_command != null` | "Managed by {tool}" · shows the documented command, never a silent delete (U-6) |

## 5. Stages

### PL-0 — verify the assumptions (both repos, read-only; no code)

**Goal.** No unmarked assumption survives into PL-1/PL-2.

- **A-1 (the load-bearing one): does a running serve child observe a
  credential added/updated by a sibling CLI process without restart?**
  auth.json has cross-process locking, but the runtime may cache provider
  clients or env; `tui_gateway` patches `os.environ` after saving
  (`methods_complete.py:384`) — a hint that process-env staleness is real.
  Read the runtime's client-construction path (where `load_pool` / env vars
  are read per turn). If stale: PL-2 must also define the nudge (candidate:
  an `auth reset`-adjacent poke or a serve-side pool-invalidate verb) and
  PL-4 must fire it after every successful login. Do NOT test this against
  the live root.
- **A-2:** piped-stdin behavior of `masked_secret_prompt` → `getpass` on
  Windows (`secret_prompt.py:56-81`) — decides whether `--stdin` reads the
  raw first line itself (preferred: deterministic) or reuses the prompt path.
- **A-3:** the four device-code functions (`auth.py:7823,7893,8230,8524`) —
  do they print/block in a shape that accepts an event callback, or does
  PL-2 need a small extract-emit refactor per provider? Also: which of the
  four share the `web_server.py` `_start_device_code_flow` worker logic that
  could be hoisted instead.
- **A-4:** the parallel defect agent's landed shape for the silent-401
  dropdown fix and the Limits panel (§8) — re-read
  `mission_control_hermes_visibility.dart` and `provider_usage_menu.dart`
  at PL-3 start; rebase, don't re-diagnose.
- **A-5:** can `_build_oauth_catalog()`'s override table
  (`web_server.py:9491,9714`) be imported without dragging FastAPI in, or
  does PL-1 hoist it into `provider_catalog.py`? (Expected: hoist — the
  module docstring at `provider_catalog.py:7` already names
  `_OAUTH_PROVIDER_CATALOG` as a hand-maintained list it exists to unify.)
- **A-6:** confirm `HERMES_HOME` resolution parity — the identity triple the
  launcher passes (`mission_control_provider.dart:111-121`) vs what
  `get_hermes_home()` resolves for the spawned verb, and the shared-auth
  fallback (`auth.py:928-961`), so a login lands in the store the serve
  child reads. Receipts already record both resolutions; cite one live
  receipt (read-only) as evidence.

**Does NOT do.** Touch anything. Output: §10 table updated, A-tags resolved.

### PL-1 — hermes: `provider_visibility/v3` catalog block

**Change surface.** `hermes_cli/harness.py:2973-3033`: add a failure-isolated
`catalog` block beside `environment`/`api_keys`/`auth_logins` — one entry per
`provider_catalog()` descriptor (+ OAuth overrides per A-5): `{id, name,
flows: ["api_key"|"device_code"|"pkce"|"external"], key_var, models_dev_id,
docs_url, disconnect_command|null}`. Schema string moves to
`hermes.provider_visibility/v3`; every v2 block is byte-compatible (additive
only — the launcher's v2 parser must keep working unchanged against v3
output, exactly how v1→v2 was handled, READ
`mission_control_hermes_visibility.dart:30-49`). `models_dev_id` is the
server-side home for the mapping currently hardcoded in the launcher
(`openaicodex→openai`, `opencodezen→opencode`, `xaioauth→xai`,
`minimaxoauth→minimax`, `qwenoauth→alibaba`, READ `:667-676`) — seed it from
that map, then it drifts with the fork, not the launcher. Also add
`token_preview` (last-4, matching `web_server.py`'s existing preview rule) to
each credential row.

**Tests** (`tests/test_provider_visibility_v2.py` grows a v3 section):
- `catalog block lists a provider with no credentials` — kill-mutation:
  derive catalog from the credentialed set (the exact bug this plan exists
  to prevent).
- `catalog failure is isolated: a raising catalog builder still emits
  providers + environment` — kill: let the exception escape the block.
- `v2 consumers: payload minus catalog is unchanged` — kill: rename any
  existing field.
- `no credential value appears anywhere in the payload` (serialize, grep for
  a seeded sentinel secret) — kill: include `access_token` in a row.

**Rollback.** Revert; additive block, no reader depends on it yet.
**Does NOT do.** Touch web_server, auth verbs, or any launcher file.

### PL-2 — hermes: non-interactive login verbs, secrets never in argv

**Change surface.** `hermes_cli/auth_commands.py` + `_parser.py`:

1. `hermes auth set-key <provider> [--label L] --stdin` — reads the key as
   the first stdin line (raw read per A-2, no prompt), routes
   `save_provider_env_credential` (`credential_lifecycle.py`) when the
   provider has a registered key-var, else the manual-pool path
   (`auth_add_command`'s api_key branch) — ONE verb, choke-point-first, so
   the #62269 mirror-drift family cannot recur via the launcher. Prints a
   JSON ack `{ok, provider, label, key_var|null}` — never the value.
2. `hermes auth login <provider> --json` — device-code flows only. Emits
   NDJSON: `{"event":"code","user_code":…,"verification_uri":…,
   "expires_at":…}` → `{"event":"pending"}` heartbeats →
   `{"event":"done","ok":true,"label":…}` or
   `{"event":"error","reason":…}`. Wraps the existing per-provider login
   functions (A-3 shape); on SIGTERM/stdin-close, exits without persisting.
   PKCE (anthropic paste-code) is explicitly NOT in this stage — deferred
   (§9), the anthropic lane is healthy today (RELAYED) and has the API-key
   path meanwhile.
3. If A-1 found staleness: the post-login nudge verb/flag, named here once
   decided.

**Tests** (`tests/cli/test_auth_noninteractive.py`, new; 30 s cap, no
`integration` marker; fake token endpoints, tmp HERMES_HOME):
- `set-key --stdin stores via the lifecycle choke point and the pool sees
  it` — kill: write .env only (pool misses it).
- `set-key never echoes the secret` (capture stdout+stderr, grep sentinel) —
  kill: print the ack with the value.
- `argv of set-key contains no secret by construction` (parser test: no
  value-bearing flag exists) — kill: add `--api-key <value>` back.
- `auth login --json emits code before any network poll and done after
  token persist` (fake device-code server) — kill: reorder, or persist
  before `done`.
- `killing auth login mid-poll leaves auth.json unchanged` — kill: persist
  partial creds on the code event.

**Rollback.** Revert; verbs are additive, nothing calls them yet.
**Does NOT do.** Any launcher change; any change to interactive `auth add`
(operators keep their TTY flow); PKCE.

### PL-3 — launcher: parse v3, lanes for the never-configured, delete the alias map

**Change surface.** `mission_control_hermes_visibility.dart`:
- Parse the `catalog` block into `HermesProviderDescriptor` on the snapshot
  (nullable list — absent on v2, and `degradationNote` copy gains a line for
  "connectable-provider catalog unavailable on this hermes").
- `hermesProviderLanes` (`:754-812`) unions in descriptor-only lanes
  (state N1) after credentialed and login lanes; lane display name and
  models.dev id come from the descriptor when present.
- `laneCatalogIds` (`:981-986`) and `hermesCatalogAliasFor` (`:667-676`):
  prefer `models_dev_id`; the hardcoded map survives only as the v2
  fallback, tagged for PL-6 retirement.

Collision note: this file is the parallel agent's likely landing zone for
the silent-401 interim fix — REBASE on their landed shape (A-4). If their
fix already forces failing lanes into `laneCatalogIds`/sections, this stage
inherits it; the v3 path supersedes the MECHANISM (identity from the server)
but must preserve their pinned BEHAVIOR (failing lane visible with reason).

**Tests** (`mission_control_hermes_visibility_test.dart` +
`mission_agent_model_switcher_view_model_test.dart`):
- `v3 payload yields an N1 lane for a provider with no credentials` — kill:
  build lanes from credentials/logins only.
- `v2 payload (no catalog) yields today's lanes exactly` — kill: make v3
  parsing mandatory.
- `alias map unused when models_dev_id present` — kill: keep consulting the
  map first (drift risk this stage exists to remove).
- Switcher: `N1 lane renders as an unconnected section distinct from N2`
  (different `issueState`) — kill: collapse both to "not signed in".

**Rollback.** Revert; v2 behavior is the built-in fallback.
**Does NOT do.** Any widget/affordance work; any deletion (PL-6).

### PL-4 — launcher: Settings performs logins

**Change surface.**
- New `lib/features/mission_control/data/provider_connect_controller.dart`:
  owns the two flows against the process identity. API key: masked
  `TextField` (in a dialog owned by the Settings drawer), value held only in
  a local variable, direct spawn `auth set-key <provider> --stdin` via
  `runMissionControlCommand` (NOT `…PreferServe` — secrets and long flows
  never transit the serve protocol; receipts record argv only, which by
  PL-2 construction carries no secret), write key + `\n` to child stdin,
  zero the field, await JSON ack. OAuth: direct-spawn streaming runner on
  `auth login <provider> --json`; render `user_code` + `verification_uri`
  (copy button, open-browser button), pending spinner, terminal state;
  cancel kills the child. On any success: fire the A-1 nudge (if needed) and
  `ref.invalidate(missionControlHermesConnectionProvider)` (the existing
  refresh seam, `hermes_install_panel.dart:824`).
- `_HermesProviderTile` (`hermes_install_panel.dart:1901-2096`): per-state
  trailing affordance per the §4 table (N1 Connect · N2/N3 Sign in again ·
  N4 Reset status via `auth reset <provider>` · N7 Remove via
  `auth remove <provider> <target>` with confirm · N8 shows
  `disconnect_command` as copyable text). N5 renders the degradation note
  and disables all actions.

**Tests** (widget + controller, fake runners):
- `sentinel secret entered through the flow appears in no receipt line, no
  snapshot, no log sink` (U-5 analog: pump the flow with a recording receipt
  sink and grep everything written) — kill: route set-key through
  `runMissionControlCommandPreferServe`, or pass the key as argv.
- `each §4 state renders its own copy and affordance` (golden or predicate
  per state) — kill: merge N1 and N2 rendering.
- `oauth flow surfaces code before done; cancel kills the child` — kill:
  swallow the code event.
- `success invalidates the connection provider exactly once` — kill: drop
  the invalidate (stale "still failing" tile after a successful re-auth —
  the confusion this plan exists to end).

**Rollback.** Revert widgets/controller; PL-2 verbs stay as operator tools.
**Does NOT do.** Console changes; any first-run/onboarding surface; env-var
editing; reveal.

### PL-5 — launcher: console consequences

**Change surface.**
- Model switcher (`mission_agent_model_switcher_view_model.dart` + its
  panel): unconnected sections get state-specific copy from §4 (N1
  "Not connected — connect in Settings"; N2/N3 the health message + "Sign in
  again in Settings"; N4 the retry countdown) and the existing "Manage
  providers…" footer (`canvas_shell.dart:434`) gains a provider-anchored
  variant (open the drawer scrolled to that tile). The console NEVER hosts
  credential entry (U-7 discipline, inverted: one home).
- Submit-time gate (U-3): when the effective lane for a send resolves to
  N1/N2/N3, the composer surfaces one inline line ("{provider}: re-auth
  required — fix in Settings") BEFORE dispatching a doomed turn; merely
  opening a console with a dead lane shows nothing modal.
- Limits panel (`provider_usage_menu.dart`): render the §4 state name for
  lanes where usage is absent BECAUSE the lane cannot run (N2 ⇒ "re-auth
  required", not "no usage data") — rebased on the parallel agent's fix
  (A-4), which owns the data-side diagnosis.

**Tests.** View-model: `N2 lane yields section copy carrying the 401 message`
— kill: fall back to bare "Connect in Settings". Composer gate:
`send against an N2 lane surfaces the gate line and still allows override`
— kill: hard-block the send (the gate informs; hermes remains the authority
on whether a turn fails). Panel: `Limits shows N2 copy when the lane is
auth-failed` — kill: show "no usage data".

**Rollback.** Revert; Settings-side stages stand alone.
**Does NOT do.** Change routing/turn dispatch; touch usage data collection.

### PL-6 — retirement (gated, both repos)

Grep-gated worklist, doc-03 discipline (retirement is a worklist, not a
same-day delete):
- `hermesCatalogAliasFor` + its test rows — gate: zero v2-fallback receipts
  across N operator sessions (v3 hermes everywhere).
- `_HermesApiKeySummary` (`hermes_install_panel.dart:2168-2199`) and the
  snapshot's `configuredApiKeys`/`missingApiKeyCount` surface — key state
  becomes a per-lane fact (v3 `key_var` + credential presence); gate: the
  tile renders it.
- `parseHermesAuthList` text fallback — gate: fleet hermes ≥ v2 everywhere
  (it is the LAST resort for pre-typed hermes; do not delete early).
- The switcher's generic "open Settings to connect" strings — replaced by
  §4 copy in PL-5; delete the dead constants.

**Does NOT do.** Delete any hermes verb (argv verbs remain operator tools).

## 6. Platform facts

- Fork boundary (RELAYED, memory 2026-08-13; RAN `ls-tree` both refs this
  session): `agent_runtime/` is fork-only; `apps/desktop/`, `gateway/`,
  `tui_gateway/` are upstream-owned surfaces present in the fork tree.
  `hermes_cli/` is shared lineage — PL-1/PL-2 land in files upstream also
  edits; expect merge friction, keep the diffs additive and small.
- The launcher's serve child and CLI probes share one env projection
  (`mission_control_provider.dart:69`); the known base-vs-alice profile trap
  (RELAYED, memory 2026-08-13) is about WHICH home that is — receipts
  record both resolutions (`:135-146`), and PL-0/A-6 cites one.
- auth.json writes take a cross-process advisory lock
  (`auth.py:1103`) — a login CLI racing the serve child is lock-safe at the
  store level; A-1 is about read-side caching, not write races.
- The serve exec lane rejects unknown verbs from a stale child argparse-side
  and falls back to fresh CLI (`mission_control_serve_session_io.dart:
  1673-1727`) — irrelevant to PL-4 by construction since login spawns
  direct, but it is why PL-2's verbs need no serve-side registration.

## 7. Adversarial pass — what I most expect to be wrong

1. **A-1 (runtime credential staleness) is the plan's biggest unknown.** If
   the serve child caches provider clients per session, a successful re-auth
   in Settings fixes `auth list` while live chat turns keep failing with the
   OLD credential — which would make this plan REPRODUCE the exact
   ambiguity it exists to kill, one layer down. PL-0 exists for this; if
   staleness is real and un-nudgeable cheaply, PL-2 grows the invalidate
   verb and PL-4 must not report "Connected" until a post-nudge probe
   agrees.
2. **Windows stdin plumbing (A-2).** `getpass` on a non-tty Windows pipe has
   historically odd fallbacks; if the raw-first-line read can't be made
   deterministic, set-key may need a temp-file-descriptor scheme — more
   surface, same contract (never argv, never at rest).
3. **The device-code wrap (A-3) may be four refactors, not one wrapper** —
   each login function prints its own UX today. Budget risk, not design
   risk; the NDJSON contract stays fixed while providers migrate one at a
   time (flow absent from `auth login` ⇒ tile falls back to "run
   `hermes auth add X` in a terminal" copy — still honest, still actionable).
4. **Parallel-agent collision** (§8): both of us edit
   `mission_control_hermes_visibility.dart` and the Limits panel. Their fix
   is live-defect triage and should land FIRST; PL-3/PL-5 rebase. If their
   fix widened `laneCatalogIds` to include failing lanes, nothing here is
   redundant — this plan replaces the identity mechanism and adds the verbs;
   their behavioral pins become PL-3's regression tests.
5. **Schema-bump blast radius.** Renaming the schema string to v3 might trip
   a consumer that string-matches `/v2` exactly (RAN grep found the string
   in `skills_inventory.py` and tests). PL-1 must grep-audit every
   `provider_visibility` consumer and either keep the string stable with an
   additive `catalog` key (preferred if any consumer pins it) or bump all
   consumers in the same change. Decide at PL-1, out loud.
6. **What this pass could not answer:** A-1, A-2, A-3, A-5, A-6 — all PL-0
   items by name.

## 8. Boundary with the parallel defect agent + standing constraints

A second agent is diagnosing (a) the silent-401 model-dropdown vanish and
(b) the Limits "no usage data" defect. This plan does NOT re-diagnose either;
they are requirements N2 (visible, distinguishable, actionable) and the
PL-5 Limits copy rule. Ordering ruling proposed: their interim fixes land
first (small, live-defect); PL-3/PL-5 rebase on them and convert their pins
into regression tests. Nothing in this plan makes their fixes wasted work —
the fixes' TESTS survive; only the identity-mapping mechanism is later
superseded by v3 (PL-6 gates the removal).

Constraints: never write under `X:/Eternia/.hermes/` (live root, read-only);
never print/copy/rotate a credential value; no `harness serve` children from
planning sessions; port 8090 untouched; `dart format` forbidden; python
tests ≤30 s, no `integration` marker; additive schema changes only; no
commits to `main`.

## 9. Deferred

- Anthropic PKCE paste-code flow from the launcher (`auth login anthropic`)
  — needs a `submit`-style second leg; the lane is healthy today and keeps
  the terminal path.
- OAuth expiry PRE-warning (N3 before it happens, from `expires_at`) — needs
  v3 to carry expiry, cheap follow-on to PL-1, but no surface asks for it
  yet.
- First-run "no provider at all" full-screen experience — the N1 roster +
  submit gate cover the workflow; revisit only if operators still get lost.
- Custom/OpenAI-compatible endpoints (upstream's `LocalEndpointRow`) — a
  different feature (endpoint + key), not a login; would ride the same
  controller if commissioned.
- Serve-RPC auth methods (`runtime.auth.*`) — if the CALL-half migration
  (PUSH/RPC ruling) later moves argv verbs to JSON-RPC wholesale, the PL-2
  verbs migrate with it; designing that lane here would couple this plan to
  that program's schedule.

## 10. Verification log

| # | Fact | How established |
|---|---|---|
| P-R1 | Credential stores + lifecycle choke point + secrecy contract | READ credential_lifecycle.py:1-40; auth.py:6,902-961,1103 |
| P-R2 | provider_visibility v2 builder + failure-isolated blocks | READ harness.py:2973-3033 |
| P-R3 | provider_catalog standalone; OAuth overrides welded to web_server | READ provider_catalog.py:7,83,179; web_server.py:9491,9714 |
| P-R4 | Fork already carries Desktop's whole OAuth HTTP machinery | READ web_server.py:9764-9807,10749-10850; RAN grep |
| P-R5 | `auth add` interactive-only; no `--api-key` parser flag; getpass stdin fallback | READ auth_commands.py:164-240; RAN grep _parser.py; READ secret_prompt.py:56-86 |
| P-R6 | `auth remove` RemovalStep cleanup; `auth reset` status clear | READ auth_commands.py:464-507 |
| P-R7 | Launcher: one process identity for serve + probes; env-triple receipts | READ mission_control_provider.dart:50-150 |
| P-R8 | Launcher: serve-first runner + stale-fallback idempotency; streaming lane exists | READ mission_control_serve_session_io.dart:1673-1739; realm_sync_providers.dart:29-39 |
| P-R9 | Snapshot provenance enum, typed health, lanes; alias map hardcode; lanes omit credential-less providers | READ mission_control_hermes_visibility.dart:30-49,204-271,667-676,754-812,981-986 |
| P-R10 | Settings tiles display-only; parallel API-key summary; drawer hosting | READ hermes_install_panel.dart:1386-2199; secondary_drawers.dart:698,776 |
| P-R11 | Switcher renders unconnected sections w/ issue; Manage-providers footer | READ mission_agent_model_switcher_view_model.dart:134-368; canvas_shell.dart:434 |
| P-R12 | Upstream: catalog-authoritative grouping; settings=onboarding picker; disconnect semantics | READ upstream providers-settings.tsx |
| P-R13 | Upstream: deferred credential warning; submit-time gate | READ upstream store/onboarding.ts:393-425; provider-setup-errors.ts |
| P-R14 | Upstream: reauth vs connectivity classification | READ upstream AGENTS.md; boot-failure-reauth.ts:52-66 |
| P-R15 | Upstream: at-rest secret contract as non-change-detector test | READ upstream e2e/at-rest-connection-token.spec.ts header |
| P-R16 | tui_gateway fronts lifecycle choke point over its own RPC | READ tui_gateway/methods_complete.py:359-467 |
| P-R17 | Live credential shape incl. opencode-zen 401 | RELAYED (brief, 2026-08-16) |
| P-R18 | PUSH/CALL ruling; profiles/base HERMES_HOME trap | RELAYED (memory 2026-08-13) |
| P-A1 | Serve child observes credential changes without restart | ASSUMPTION — PL-0 |
| P-A2 | Deterministic non-tty stdin secret read on Windows | ASSUMPTION — PL-0 |
| P-A3 | Device-code fns wrappable with an emit callback | ASSUMPTION — PL-0 |
| P-A5 | OAuth override table importable / hoistable w/o FastAPI | ASSUMPTION — PL-0 |
| P-A6 | Login verb resolves the same HERMES_HOME the serve child uses | ASSUMPTION — PL-0 (receipt cite) |
