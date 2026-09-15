# The persona binding becomes the env authority — HERMES_HOME resolution plan (Plan B, 2026-08-16)

> **Home.** Hermes repo, beside `PROVIDER_LOGIN_FIRST_CLASS_PLAN_2026-08-16.md` and
> `AGENT_CONSOLE_DEAD_LANE_AND_LIMITS_PLAN_2026-08-16.md`, whose house S-stage
> format this follows. Scope spans both repos: hermes fork `X:/Eternia/hermes-agent`
> (main `b6f11b04c5`) and launcher `X:/Unreal Engine/Engine/Launcher/EterniaLauncher`
> (main `3633cba4f`). No production code was changed for this document; every stage
> is future work. Operator ruling this implements (RELAYED): *"let the per persona
> binding resolve it — we need to make it the standard so there is less problems
> with the env being used."*

**Evidence tags**: **READ** (file:line inspected this session) · **RAN** (command
executed this session — read-only process/table queries, never a value printed) ·
**MEASURED-LIVE** (read-only observation of the live runtime root / live
processes this session) · **RELAYED** (commissioning brief or operator ruling,
not re-measured) · **ASSUMPTION A-n** (unverified; stage B-0 settles it).

---

## 0. Verdict up front

**The authority already exists, is already correct, and is already used for the
lane that matters most — and the launcher's one global `HERMES_HOME` is not so
much wrong as mis-titled.** Three facts, established this session, carry the
whole plan:

1. **Chat turns already obey the persona binding, not the launcher env.**
   Every persona chat turn enters `persona_profile_context(binding)` inside the
   serve child (READ `agent_runtime/profile_runner.py:798`), which redirects
   `HERMES_HOME` / `HOME` / `HERMES_AUTH_HOME` to the persona's bound profile
   home (READ `agent_runtime/profile_context.py:264-316`). All five live
   personas carry a binding — `backend_dev→backend-dev`, `base→base`,
   `dev→launcher-dev`, `neko_supervisor→neko`, `qa→launcher-qa` — and all five
   target profiles exist on disk (MEASURED-LIVE, §1.2). The launcher's
   `HERMES_HOME=…/profiles/base` is already only the *ambient default* those
   turns swap away from. The standard the operator asked for is therefore not a
   new mechanism; it is closing the lanes that still bypass this one.

2. **The bypass lane is child processes that inherit a profile-shaped env.**
   `hermes_cli/main.py`'s profile pre-parse (READ `:577-699`) resolves in
   ladder order: explicit `--profile` flag → **trust an inherited `HERMES_HOME`
   whenever its parent directory is named `profiles`** (`:635-647`, issue
   #22502) → the sticky `active_profile` marker. Step 1.5 is the trap: any
   hermes child spawned from the launcher's env (`HERMES_HOME=…\profiles\base`,
   READ `mission_control_settings.dart:159-190`) without an explicit flag stays
   on `base` **and the sticky marker is never consulted**. `profiles/base/.env`
   holds 1 key; `profiles/alice/.env` holds 18 including the three
   `TELEGRAM_*` keys (MEASURED-LIVE, key names only). A gateway trapped this
   way has no bot token at all.

3. **The lanes that pin their profile explicitly are healthy — measured on the
   live orphan.** The running gateway (PID 21004, port 8090,
   `python -m hermes_cli.main --profile alice gateway run`, parent dead) has
   `HERMES_HOME=X:\Eternia\.hermes\profiles\alice` in its actual environment
   block, carries the `TELEGRAM_*` keys, and carries **none** of the launcher's
   signature vars (`ETERNIA_HERMES_ROOT`, `HERMES_HEAD_HOME`,
   `HERMES_AGENT_RUNTIME_ROOT`) — it was not launcher-spawned; it is the
   service-wrapper gateway, and the installed wrapper double-pins
   (`set HERMES_HOME=…\alice` **and** `--profile alice`, READ live
   `profiles/alice/gateway-service/Hermes_Gateway_alice.cmd:4,9`). (RAN: PEB
   environment-block read, key names + path values only.)

So: keep the in-process binding as the one authority (§2 picks this
explicitly), demote the launcher's global setting to what it already truly is —
the default home for surfaces with no persona in hand — and make every
spawn-shaped lane either carry the binding explicitly in argv or state, in a
typed receipt, which home it resolved and why.

---

## 1. Baseline

### 1.1 The mechanism (hermes, all READ)

- `resolve_persona_profile(persona)` → `PersonaProfileBinding`
  (`agent_runtime/profile_context.py:147-172`): `hermes_profile` unset ⇒
  `profile_home=None`, summary *"inherits active Harness profile"*; set but
  missing on disk ⇒ typed `missing_profile`; else the profile dir.
- `persona_profile_context(binding)` (`:176-325`): profile-bound ⇒ saves and
  redirects `HERMES_HOME` / `HOME` (when `<profile>/home` exists) /
  `HERMES_AUTH_HOME` (pinned to the *head* auth home, `:304-307`) and exports
  `HERMES_AGENT_RUNTIME_ROOT` unconditionally; unbound ⇒ exports only the
  runtime root and emits the typed
  `persona_profile_context_no_profile_binding` row (`:233-249`). Its own
  fix-hint (`:243`) already states the standard this plan promotes: *"Set the
  persona's hermes_profile if it needs its own HERMES_HOME / HOME /
  HERMES_AUTH_HOME."*
- The `os.environ` writes are load-bearing for **child processes and raw-env
  readers** (audited 2026-08-09, comment `:279-302`): ContextVars never cross a
  subprocess boundary; spawn sites must build env through
  `tools/environments/local.py` factories (which bridge the override) or they
  hand the child the head home. INVARIANT: sound only while runs are
  serialized by `profile_runner._WORKDIR_LOCK` (`:297-302`).
- Entry points that flow through it today (RAN grep): chat turns
  (`profile_runner.py:798`), readiness probes (`profile_readiness.py:70`),
  prompt observability (`prompt_observability.py:189`), realm sync binding
  reads (`realm_sync.py:1140,1299`), MCP admission (`mcp_admission.py:817`),
  soul overlay reads (`persona_runtime.py:958-961`).
- A first-class **rebind verb already exists**: `move_persona_profile`
  (`agent_runtime/persona_profile_binding.py:520-660`) — typed refusals
  (`persona_not_persisted` / `profile_missing` / `profile_not_ready` /
  `instances_busy`), moves the persona **and every instance projection**, has
  `dry_run`. Synthetic `profile:<name>` chat personas are auto-bound to their
  profile (`hermes_cli/harness_parts/persona_commands.py:5952`).

### 1.2 The live state (MEASURED-LIVE, read-only, key names only)

| Fact | Value |
| --- | --- |
| Personas (`agent-runtime/agents/*.json`) | 5; **all** bind a `hermes_profile`; all 5 targets exist under `X:/Eternia/.hermes/profiles/` |
| Instances (`agent-runtime/persona_instances/*.json`) | 16; 15 carry a `profile_id` agreeing with their persona's binding; **1 null** (`personainst_qa_agent_644595cc`, persona `qa`); plus `personainst_profile_alice` bound `alice` |
| Profiles on disk | 11 dirs; `.env` keys: `base` **1**, `launcher-qa-direct` none, all nine others 18 |
| Live gateway | PID 21004, `--profile alice gateway run`, env `HERMES_HOME=…\profiles\alice`, `TELEGRAM_*` present, launcher signature vars absent, `HERMES_GATEWAY_DETACHED` + `HERMES_GATEWAY_EXTERNAL_SUPERVISOR` set |
| Live serve chain | launcher → `hermes harness serve --ndjson` (PID 20232 → 33404 → 38228); env read of the workers was blocked by the session's permission classifier, but the spawn passes `environment: identity.toProcessEnvironment()` (READ `mission_control_serve_session_io.dart:1651`; Dart merges over the parent env), so `HERMES_HOME=…\profiles\base` by construction |

**How many live instances are mis-homed: zero at the persona-binding layer.**
Every chat turn resolves a ready binding; the one null-`profile_id` instance
still resolves through its persona (`qa → launcher-qa`) *if* run-time
resolution goes persona-first (A-3 verifies the precedence; the instance row
is a projection). The mis-homing that bit the operator is **not stored state —
it is spawn-time env inheritance** on lanes with no persona in hand.

### 1.3 The launcher's projection (all READ)

- `MissionControlProcessIdentity.toProcessEnvironment()`
  (`mission_control_settings.dart:159-190`): `HERMES_HOME =
  <root>/profiles/<resolvedProfile>` for **every** spawn, no persona in the
  decision; `resolvedProfile` falls back to `'base'` (`:154-157`);
  `HERMES_HEAD_HOME` pinned to `profiles/base` and already **demoted** to an
  override checked against the runtime's own `root_anchor` declaration
  (comment `:172-183`) — the precedent this plan follows for `HERMES_HOME`.
- Consumers: the persistent serve child (`mission_control_provider.dart:69`,
  spawned at `mission_control_serve_session_io.dart:1651`), the visibility
  probe (`:387-406` per the dead-lane plan's V-3), history fetch
  (`mission_chat_history_fetch_providers.dart:150` — serve-preferred, argv is
  `harness persona chat history --session-id …`), hygiene
  (`mission_hygiene_providers.dart:16`), realm sync
  (`realm_sync_service.dart:784-1560`), settings drawer probe
  (`secondary_drawers.dart:691`), transport receipts recording the env triple
  both ways (`mission_control_provider.dart:111-150`).
- The settings docstring (`mission_control_settings.dart:19-27`) titles the
  field as *"which Hermes profile home the launcher runs the runtime under"* —
  the mis-title. Under §0 fact 1 it is actually *the ambient default the
  runtime swaps away from per persona*.

### 1.4 The reported break, situated

Operator report (RELAYED): the **alice Telegram gateway stops working "on base
hermes."** What this session could and could not establish:

- The trap exists and is deterministic: any gateway verb run from a
  `profiles/<name>`-shaped env without an explicit flag stays on that profile
  (READ `main.py:635-647`); under `base` that means zero Telegram keys
  (MEASURED-LIVE). A gateway (re)started that way is token-less.
- The **service** lane is *not* that trap: the installed wrapper double-pins
  alice (READ live wrapper), and the live orphan proves the detached respawn
  inherits the corrected env (`hermes_cli/gateway.py:854-885` spawns with
  inherited `os.environ`, *after* `main.py:695` already rewrote `HERMES_HOME`
  from the flag).
- Therefore the incident lane is one this session did not directly observe —
  candidates in A-1. The plan hardens all of them the same way (B-1), so the
  fix does not depend on which one it was, but B-0 names it before B-1 lands.

---

## 2. The design decision

### 2.1 Where the binding resolves for a launcher-initiated spawn

Two options, as commissioned:

- **Option A — the launcher learns the persona's profile and sets the env per
  spawn.** Requires the launcher to read each persona's `hermes_profile`
  (snapshot already carries persona rows) and build a per-spawn env. Rejected:
  it mints a **second resolver** of the same fact in a second language, whose
  drift class is exactly tonight's incident ("three homes gave three different
  answers, all correct"); it cannot help the persistent serve child at all
  (one process, N personas — no single `HERMES_HOME` is right); and it breaks
  the moment any non-launcher client (gateway, cron, shell) spawns the same
  work — the authority would live in the one client that happens to be open.
- **Option B — hermes resolves the binding in-process; the launcher stops
  meaning "this is every persona's home" and starts meaning "this is the
  default home".** One authority (`resolve_persona_profile` +
  `persona_profile_context`), already live for chat turns, readiness, prompt
  observability, realm sync, MCP admission (§1.1). Honest about the serve
  child's reality. Its stated weakness — *"only works if every entry point
  flows through `persona_profile_context`"* — is real, and §1.1's audit shows
  the remaining bypasses are **subprocess spawn sites**, which is what B-1/B-2
  close.

**Ruling implemented: Option B.** The trade-off, stated: Option B accepts that
persona-less surfaces keep an ambient default (and must say so — provenance,
not purity), in exchange for exactly one resolver of "which home does persona
X use". Option A would make every surface *look* explicit while doubling the
resolvers and still lying inside serve.

### 2.2 What the global setting becomes

**A default, not an override — and profiles stay real boundaries.** Per the
operator's instinct (RELAYED, endorsed): do **not** collapse profiles into a
pointer; alice has Telegram keys precisely because base must not. The
launcher's `hermesProfile` setting survives with a narrowed, honestly-titled
meaning: *the profile home for the serve child's own boot and for probes that
have no persona in hand*. It keeps its operator-escape-hatch role
(misprovisioned-profile repoint, `mission_control_settings.dart:20-27`). It
stops being documented, titled, or reasoned about as "the profile the runtime
runs under" — because (§0 fact 1) it already is not.

The launcher **keeps setting `HERMES_HOME`** for its children. Dropping the
env write entirely (the "more honest" extreme) is explicitly deferred, for the
same reason the `HERMES_HEAD_HOME` pin was demoted rather than deleted
(`mission_control_settings.dart:177-183`): removing it today would drop every
spawn back to the ambient ladder (`%LOCALAPPDATA%` shadow-root class,
`agent_runtime/root_anchor.py:1-24` — READ, treated as a hazard, never
exercised). The retirement gate is the same one the head-home pin already has:
the runtime's own `root_anchor` declaration staying quiet across N sessions.

### 2.3 Surfaces with no persona in hand (keep the default; gain provenance)

The connection/visibility probe, the model-catalog read, `harness usage`, the
settings drawer's own probe, hygiene sweeps, realm-sync plumbing, transport
receipts, and the serve child's boot itself (READ call sites §1.3). Persona
chat **history** looks persona-shaped but is deliberately head-homed — the
operator-visible SessionDB lives at the head so all profiles project into one
transcript store (READ `profile_context.py:270-276`,
`mission_control_settings.dart:167-171`) — it stays on the default lane.

### 2.4 Migration: instances and personas with a null binding

- Persona `hermes_profile=None` resolves today to *"inherits active Harness
  profile"* (READ `profile_context.py:149-155`) = the launcher default
  (`base`) under Mission Control, with the typed no-binding row emitted per
  run. **The plan does not change this resolution** — so nobody's behaviour
  changes silently. B-4 changes *creation* (new personas get an explicit
  binding) and offers an explicit, dry-run-first backfill for existing nulls
  using the rebind verb that already exists. Live blast radius of the
  backfill today: **zero personas, one instance projection**
  (`personainst_qa_agent_644595cc`) (MEASURED-LIVE §1.2).

---

## 3. Stages

### B-0 — settle the assumptions (read-only, both repos + live root; no code)

**Goal.** No unmarked assumption survives into B-1/B-2/B-4.

- **A-1 (load-bearing): name the actual lane of the alice-gateway break.**
  The trap is proven (`main.py:635-647` + base's empty `.env`); the incident
  lane is not. Candidates, each checkable read-only: (i) a gateway verb
  (`run`/`restart`/`start`) issued from a base-homed console or launcher
  child; (ii) a *second* gateway coming up under base and contending for the
  gateway port / Telegram polling while alice's sits healthy; (iii) a restart
  wrapper regenerated without the flag while ambient env was profile-shaped;
  (iv) the sticky `active_profile` marker diverting a bare `gateway run`.
  Evidence: gateway logs under `profiles/alice/logs` and `profiles/base`
  (read-only), `schtasks /query` for `Hermes_Gateway*`, launcher console
  history, the orphan's parent CreationDate (16 Aug 16:15:06, RAN) vs launcher
  session times. Output: one named lane, or "not reproducible — B-1's receipt
  is the tripwire".
- **A-2: does any launcher one-shot CLI act *as* a persona?** Audit every
  argv builder feeding `toProcessEnvironment()` spawns (§1.3 list). Expected
  from this session's sample (history fetch is head-homed by design; hygiene,
  sync, probes are persona-less): **no** — which confirms B-3 needs no spawn
  behaviour change. Any counterexample becomes a B-2 item (that spawn must
  carry `--profile <binding>`).
- **A-3: instance-vs-persona binding precedence at run time.** Who reads
  `PersonaInstance.profile_id` on the turn path vs `persona.hermes_profile`
  (readers list: `persona_instance_identity.py:306`, `snapshot.py:1234`,
  `state_patches.py:132,495`, `workspace_template.py:112`,
  `harness_parts/office.py:80`, `persona_commands.py:2364,4342`)? Decides
  whether the null-`profile_id` instance is cosmetic (projection only) or a
  real resolution hole. Expected: projection only — turn resolution is
  persona-first (`profile_runner.py:798` takes the persona binding).
- **A-4: the `agent create` stamping site** (cross-ref
  `AGENT_CREATE_ONE_CALL_PLAN_2026-08-16.md`): where a new persona's
  `hermes_profile` is (not) set, so B-4 lands in the right function.
- **A-5: which spawn sites bypass the env factories.** The
  `profile_context.py:283-296` comment says spawn sites must build env via
  `tools/environments/local.py` factories to bridge the override; grep for
  `subprocess.Popen` / `Process` creation inside turn-reachable code that
  passes raw `os.environ` or no env. Output: the exact list B-2 pins.
- **A-6: gateway supervisor restart argv.** Confirm the external-supervisor /
  detached respawn preserves `--profile` in argv on ALL platforms' lanes
  (`hermes_cli/gateway.py:854-921`, `gateway/restart.py:32` — the latter is
  upstream-owned; read, don't touch).

**Gate.** §8 table updated; every A-tag resolved or explicitly carried as
risk. **Does NOT do:** touch anything; spawn any serve child; write under the
live root.

### B-1 — hermes: a gateway (and any profile-homed service) declares its home, typed, at boot

**Change surface.** `hermes_cli/gateway.py` (shared lineage — additive only;
`gateway/` package is upstream-owned, route around it):

1. At `gateway run` startup, resolve and emit ONE typed line (log + stored
   beside the gateway's pid/state files):
   `{profile, hermes_home, resolution: "flag"|"env_profile_dir"|"active_profile_marker"|"default", env_key_count, telegram_configured: bool}`
   — key *names* counted, never values. This is the same
   `{code, subject, summary, fix_hint}` shape `ProfileContextRow.row()` uses
   (READ `profile_context.py:65-74`), so operator surfaces need no new case.
   `resolution` comes from instrumenting the existing pre-parse: `main.py`
   records *which rung answered* into one env var
   (`HERMES_PROFILE_RESOLUTION`, set at `:647`, `:669`, `:695`) that the
   gateway reads back — a 3-line additive change to the pre-parse, no ladder
   reordering.
2. When the resolution rung is `env_profile_dir` (the step-1.5 trap) and the
   resolved profile's `.env` lacks every `TELEGRAM_*` key while a sibling
   profile's gateway service wrapper exists for a *different* profile: emit a
   typed WARNING row (`gateway_home_suspicious`) naming both homes and the fix
   (`run with --profile <name>`). Warn, don't refuse — a base gateway can be
   legitimate.
3. Service-install lanes (`gateway_windows.py` wrapper writer) already
   double-pin (READ live wrapper); add the pin as an ASSERTED invariant: the
   wrapper generator refuses to write a wrapper whose argv lacks
   `--profile <name>` when installing for a named profile.

**Tests** (`tests/hermes_cli/test_gateway*.py` grow cases; tmp HERMES_HOME,
no live root):
- `boot line reports resolution=flag when --profile given despite a
  conflicting env home` — kill-mutation: report the env home instead of the
  flag-resolved one.
- `boot line reports resolution=env_profile_dir + telegram_configured=false
  under a profile-shaped env with an empty .env` — kill: hardcode
  `resolution="flag"`.
- `suspicious-home warning fires exactly for the trap shape and not for a
  flag-pinned run` — kill: drop the wrapper-exists condition (false alarms on
  every bare base gateway).
- `wrapper generator output contains --profile <name>` — kill: remove the
  flag from the template (regenerating today's healthy wrapper would then
  produce the trap-vulnerable single-pin form).
- `no .env VALUE appears in the boot line` (seed a sentinel value, grep the
  emitted line) — kill: include the resolved env dict.

**Gate.** Tests green; a manual read-only re-observation window later, the
live gateway's next natural restart shows the boot line (do NOT restart it
for this plan). **Rollback.** Revert; all additive. **Does NOT do:** touch
`gateway/restart.py` (upstream), change the ladder's order, refuse any boot.

### B-2 — hermes: every child spawned inside a persona turn carries the binding

**Change surface.** Driven by A-5's list; expected shape:
- Pin with tests that `tools/environments/local.py` env factories resolve
  `HERMES_HOME` through `get_hermes_home()` (the ContextVar-aware resolver)
  at **spawn time**, not from a module-load-time snapshot.
- For each A-5 bypass (spawn site passing raw/ambient env): route it through
  the factory, or — where the child is a full `hermes` CLI invocation — append
  the explicit `--profile <binding.hermes_profile>` to its argv so the child
  self-homes even under a mangled env (belt for the step-1.5 braces).
- The `persona_profile_context` docstring/fix-hint (`profile_context.py:243`)
  and `docs/agent-runtime-harness/env-determinism-audit.md` gain the standard
  as one sentence: *the persona binding is the authority for HERMES_HOME;
  ambient env is a default for persona-less work only.*

**Tests** (extend `tests/test_hermetic_env_blanking.py` /
`tests/agent_runtime/test_profile_context*.py`):
- `a subprocess env built inside persona_profile_context carries the
  binding's HERMES_HOME` — kill-mutation: snapshot `os.environ` before
  context entry and build from the snapshot.
- `a hermes-CLI child argv built for a bound persona contains
  --profile <name>` (per A-5 site) — kill: drop the append; the test must
  fail via the step-1.5 trap being reachable again (assert on argv, not on
  behaviour, so the test stays hermetic).

**Gate.** A-5 list empty or every entry tested. **Rollback.** Per-site
reverts. **Does NOT do:** touch the `_WORKDIR_LOCK` serialization or attempt
context-scoped env (that is the audit doc's Q2-class future work, named in
§6); change any spawn's *behaviour* under a correct env.

### B-3 — launcher: the global setting is demoted to a default, in title, copy, and docs

**Change surface.** No spawn behaviour change (A-2 gate) — this stage makes
the launcher stop *claiming* the authority it does not have:

- `mission_control_settings.dart:19-27` docstring rewritten: the field is the
  **default/console profile home** — serve boot + persona-less probes;
  personas run under their own `hermes_profile` binding
  (`persona_profile_context`); keep the escape-hatch paragraph verbatim.
- Settings drawer copy (`secondary_drawers.dart`, the `hermesProfile` field's
  label/helper): from "profile the launcher runs the runtime under" to
  "Default profile home (probes & serve boot) — each persona runs under its
  own bound profile". Widget-test string updates included, found by test run,
  not by grep-and-hope.
- The dead-lane plan's **S4 provenance line is KEPT** and its copy is
  sharpened here (see §4): `Reading providers from <path> · personas run
  under their own bound profiles`.
- `MissionControlProcessIdentity.toProcessEnvironment()` gains one comment
  block (mirror of the `HERMES_HEAD_HOME` demotion note `:172-183`): the
  `HERMES_HOME` line is a **default**, its per-persona override lives in
  hermes, and its eventual retirement is gated on the `root_anchor`
  declaration + receipt quiet-period — not deleted now.

**Tests.** Existing settings/drawer widget tests updated for the copy; one
new predicate test: the settings docstring/UI never uses the phrase "runs the
runtime under" for the profile field (cheap tripwire against regression to
the mis-title; kill-mutation: restore the old label).

**Gate.** `flutter analyze` clean; drawer widget tests green; Stage C
screenshot of the settings drawer for the ledger (MCP path, read-only).
**Rollback.** Copy revert. **Does NOT do:** remove or rename the setting key
(`hermes_profile` in prefs JSON — compat preserved, READ `:92`); change
`toProcessEnvironment()` output by a single byte; touch
`mission_control_hermes_visibility.dart` / the model-switcher files (other
agents live there — their S4/PL work is reconciled in §4, not edited here).

### B-4 — hermes: explicit binding at creation; dry-run backfill for the nulls

**Change surface.**
- The `agent create` path (exact site per A-4): a new persona's
  `hermes_profile` is stamped **explicitly** — from the caller's argument
  when given, else the literal default `"base"` — never left `None`. The
  typed no-binding row (`profile_context.py:39`) then marks only *legacy*
  rows, and its population shrinks monotonically.
- A backfill lane on the existing rebind verb: `harness agent set-profile
  --backfill-instances` (or a sibling flag per the verb's actual parser) that
  stamps null instance projections from the persona's binding, reusing
  `move_persona_profile`'s machinery and refusal ladder
  (`persona_profile_binding.py:520-660`) — `dry_run` first, per its existing
  contract. Live scope today: one instance projection
  (`personainst_qa_agent_644595cc` → `launcher-qa`), zero personas
  (MEASURED-LIVE).

**Behaviour-change statement (the migration question, answered).** A null
binding resolves to "inherit the active profile home" before AND after this
stage (READ `profile_context.py:149-155` — untouched). Creation-time
stamping changes only rows that do not exist yet; the backfill is
operator-invoked, dry-run-first, and its only live candidate today projects
to the same profile its persona already binds. **No silent change for
anyone.**

**Tests** (`tests/agent_runtime/test_persona_profile_binding*.py` +
create-path tests):
- `created persona carries an explicit hermes_profile ("base" by default)` —
  kill-mutation: return to stamping `None`.
- `backfill dry_run reports the null instance and writes nothing` — kill:
  drop the dry_run early-return (the verb's own contract test pattern).
- `backfill refuses on busy instances with the typed error` — inherited from
  the verb; extend, don't duplicate.

**Gate.** Tests green; a read-only live listing (same query as §1.2) shows
16/16 instances bound after the operator runs the backfill — the operator
runs it, not this plan. **Rollback.** Revert; nulls remain supported forever
(`:244` — "running without one is supported and is not an error").

### B-5 — verification + ledger (no code)

- Re-run §1.2's read-only live queries; append results to this doc's §8.
- Stage C screenshots: settings drawer (B-3), gateway boot line in the log
  viewer if surfaced (B-1) — MCP path, read-only against the live window.
- Hand one sentence to the two neighbour plans' owners (§4): the binding is
  now the documented standard; login/provenance work should name the
  *profile*, not just the path.

**Gate.** Every stage's own gate re-checked green; the §8 table carries a row
per stage with its evidence.

---

## 4. Reconciliation with the two committed plans

### `PROVIDER_LOGIN_FIRST_CLASS_PLAN_2026-08-16.md` (Plan PL)

- **PL-0 / A-6** (*"login verb resolves the same HERMES_HOME the serve child
  uses"*) — **superseded in part, precisely:** the question assumes there is
  ONE home to agree on. Under this plan the sharp form is: **a credential is
  a per-profile fact, and a login must name the profile home it writes to.**
  PL-4's `ProviderConnectController` spawns should carry an explicit
  `--profile <name>` (the console's default profile for console-lane logins;
  a persona's bound profile when the re-auth is *for* that persona's lane) and
  surface the target home in the dialog — the same provenance sentence as
  dead-lane S4. A-6's *mechanical* check (parity of the launcher triple vs
  `get_hermes_home()` for a spawned verb, receipt cite) **stands** — B-0 does
  not re-do it.
- **PL-1 / A-1** (*"does a re-auth reach the running serve child"*) — the
  prior question this session sharpened ("which home was the credential
  written to?") is answered by B-1's typed resolution receipt + PL's own
  receipts; A-1 remains open **only** for read-side caching within one
  correctly-chosen home. Tonight's three-homes-three-answers class stops
  being an A-1 confound.
- No file collision: PL edits `auth_commands.py` / visibility / install
  panel; this plan edits `gateway.py`, `profile_context` docs,
  `persona_profile_binding.py`, settings copy.

### `AGENT_CONSOLE_DEAD_LANE_AND_LIMITS_PLAN_2026-08-16.md` (dead-lane plan)

- **S4 ("Reading providers from `<HERMES_HOME>`") is KEPT, not made
  redundant.** After this plan the probe still legitimately reads the default
  profile home, and personas legitimately read elsewhere — so the provenance
  line becomes *more* load-bearing, not less. B-3 sharpens its copy (…" ·
  personas run under their own bound profiles") and otherwise stays out of
  S4's files (its owner is live in them).
- Its §0 two-homes split table is this plan's §1's strongest corroboration;
  its S5 handoff ("any login UI that reads/writes a different home than the
  serve child recreates both defects") is answered by the PL reconciliation
  above: name the profile, per lane.

---

## 5. What this does NOT fix

- **The dead/duplicated credentials themselves** (base-home Codex 401, root
  `.env` splits) — the dead-lane plan's S1/S5 and the operator's rotation.
- **Read-side credential caching in a running child** (PL A-1) — untouched.
- **The `os.environ` save/mutate/restore concurrency ceiling** — the
  `_WORKDIR_LOCK` invariant (`profile_context.py:297-302`) stands; this plan
  adds no parallelism and removes none.
- **The launcher's `HERMES_HOME` env write** — demoted in meaning, kept in
  bytes; retirement is gated on `root_anchor` declaration quiet-period (§2.2),
  not scheduled here.
- **Whatever actually killed the alice gateway that night**, if A-1 lands on
  "not reproducible" — B-1's boot receipt turns the *next* occurrence from an
  inference into one log line, which is the honest best available.
- **`HERMES_HEAD_HOME` / SessionDB head-homing** — deliberate, correct,
  untouched (§2.3).

## 6. Deliberately deferred

- **Dropping the launcher's `HERMES_HOME` write entirely** (the pure Option
  B endgame) — gated on the root-anchor declaration + receipts staying quiet
  across N operator sessions, same ladder the head-home pin is already on.
- **Context-scoped env / deleting the `os.environ` writes** — the
  env-determinism audit's Q2-class program; every raw-env reader must be
  context-scoped first (`profile_context.py:279-302`).
- **Surfacing `ProfileContextRow` / the B-1 boot line in the Mission Control
  console** — worth one tile, but it rides the PL plan's provenance surfaces;
  no second surface here.
- **Per-instance (not per-persona) profile overrides** — `profile_id` on the
  instance stays a projection; nothing asked for instance-level divergence.
- **`gateway/restart.py` argv hardening** — upstream-owned; if A-6 finds a
  flag-dropping lane there, file it against the fork-boundary worklist, don't
  patch.

## 7. Adversarial pass — what I most expect to be wrong

1. **A-1: I never named the actual break lane.** I proved the trap
   (`main.py:635-647` + base's empty `.env`) and proved the service wrapper
   and the live orphan are healthy — which means the observed failure rode a
   lane I did not see. If it was port contention or Telegram-side session
   theft by a second token-less gateway, B-1 *names* it at next occurrence
   but does not *prevent* it; prevention would need a port/identity guard not
   designed here. This is the plan's weakest joint and B-0's first job.
2. **A-3 could invert.** If some run path resolves the *instance*
   `profile_id` rather than the persona binding, the null instance is a real
   resolution hole (falling to ambient = base) and B-4's backfill is a
   behaviour change for it, not bookkeeping. The stage's gate must then
   re-state the migration answer honestly.
3. **`main.py` is shared lineage.** Even the 3-line
   `HERMES_PROFILE_RESOLUTION` instrumentation (B-1.1) lands in the fork's
   hottest merge-friction file. If friction is too high, fallback: derive the
   rung *inside* `gateway run` by re-checking flag/env/marker — duplicated
   logic, but zero pre-parse changes. Decide at B-1, out loud.
4. **B-3's copy tests are string-brittle** and other agents are live in
   adjacent launcher files (`mission_control_page.dart`,
   `mission_office_layout_controller.dart`, the visibility/switcher pair).
   B-3 deliberately touches only `mission_control_settings.dart` +
   `secondary_drawers.dart`; if their diffs collide even there, B-3 rebases —
   it is copy, it can always rebase.
5. **The serve-worker env was inferred, not measured.** The permission
   classifier blocked the PEB read for PIDs 38228/33404 (RAN: blocked); the
   `HERMES_HOME=base` claim for serve rests on READ of the spawn call
   (`mission_control_serve_session_io.dart:1651` + `toProcessEnvironment()`),
   which is deterministic — but it is code-derived, and §8 tags it so.
6. **`HERMES_AUTH_HOME` head-pinning (`profile_context.py:304-307`) cuts
   across the "credentials are per-profile" story**: auth reads resolve the
   *head* auth file even inside a profile context. If PL's login work assumes
   pure per-profile credential resolution, these two plans disagree in one
   file — flag to PL's owner; B-0 confirms which store the credential path
   actually reads per credential type.

## 8. Verification log

| # | Fact | How established |
| --- | --- | --- |
| B-V1 | Binding mechanism + env redirection + typed degradation rows | READ `agent_runtime/profile_context.py:39-43,147-172,176-325` |
| B-V2 | Chat turns enter the context inside serve; readiness/observability/sync/MCP too | READ `profile_runner.py:798`; RAN grep (callers list §1.1) |
| B-V3 | CLI profile ladder; step-1.5 env trap; marker bypass | READ `hermes_cli/main.py:505-699`, esp. `:635-647,695` |
| B-V4 | Launcher sets one global `HERMES_HOME` for every spawn; `base` fallback; head-home pin demotion precedent | READ `mission_control_settings.dart:105-190` |
| B-V5 | Serve child spawn passes the projection env (merge over parent) | READ `mission_control_serve_session_io.dart:1651`; env read of live workers blocked — code-derived, see §7.5 |
| B-V6 | Live orphan gateway: alice-homed env, TELEGRAM_* present, launcher vars absent, detached/external-supervisor markers | RAN PEB env read PID 21004 (names + path values only); RAN Win32_Process |
| B-V7 | Alice service wrapper double-pins home + flag | READ live `profiles/alice/gateway-service/Hermes_Gateway_alice.cmd` (read-only) |
| B-V8 | Detached gateway respawn inherits corrected `os.environ` | READ `hermes_cli/gateway.py:854-921` |
| B-V9 | 5 personas all bound; targets exist; 16 instances, one null `profile_id`; synthetic `profile:<name>` auto-binds | MEASURED-LIVE store reads; READ `persona_commands.py:5952` |
| B-V10 | `.env` key counts: base 1, alice 18 incl. `TELEGRAM_*` (names only) | MEASURED-LIVE (read-only key-name scan) |
| B-V11 | Rebind verb with typed refusals + dry-run + instance moves exists | READ `persona_profile_binding.py:520-660` |
| B-V12 | Root-anchor hazard: serve boot writes machine-global config; never exercised here | READ `agent_runtime/root_anchor.py:1-71`; no serve spawned |
| B-V13 | Persona chat SessionDB is head-homed by design | READ `profile_context.py:270-276`; `mission_control_settings.dart:167-183` |
| B-V14 | Operator ruling; alice-gateway break report | RELAYED |
| B-A1 | Actual incident lane of the alice-gateway break | ASSUMPTION — B-0 |
| B-A2 | No launcher one-shot CLI acts as a persona | ASSUMPTION — B-0 |
| B-A3 | Turn resolution is persona-first; instance `profile_id` is projection-only | ASSUMPTION — B-0 |
| B-A4 | `agent create` stamping site | ASSUMPTION — B-0 |
| B-A5 | Spawn sites bypassing the env factories (exact list) | ASSUMPTION — B-0 |
| B-A6 | Restart lanes preserve `--profile` on all platforms | ASSUMPTION — B-0 |

---

## 9. B-0 gate results (2026-08-16, read-only)

Four of six assumptions came back REFUTED or CORRECTED. The plan's design
ruling (Option B) survives all of them; two stage descriptions did not.

| Tag | Verdict | Evidence |
| --- | --- | --- |
| **B-A1** | **REFUTED — every named candidate, and the trap itself** | See §9.1. |
| B-A2 | **REFUTED** | Persona-shaped launcher argv exists and passes no `--profile`: `mission_chat_history_fetch_providers.dart:150` (`harness persona chat history --session-id`), `mission_control_bridge.dart:2524` (`persona-instance detail`), and the mission-chat capability family `:3235-3771`. `grep '--profile' lib/` → zero hits. NOT a live mis-homing — all are serve-preferred and the one-shot fallback still enters `persona_profile_context` in-process — but the audit's expected "no" was wrong. |
| B-A3 | **CONFIRMED** | All 12 `resolve_persona_profile` call sites take a PERSONA; the turn path is `persona_runtime.py:142`. `PersonaInstance.profile_id` is read only by display/backing predicates (`persona_instance_summary:2303`, `_row_is_backed`). Projection-only, as expected. |
| B-A4 | **CORRECTED** | `runtime.agent.create` creates INSTANCES, not personas. The null factory is `persona_assignments._profile_id_for_persona_or_template` (`:2276-2280`), reached from `:1642` and `add_instance:1681`. B-4 therefore lands there and does NOT touch `agent_create.py` / `serve_rpc.py`. |
| B-A5 | **ONE bypass, class (b)** | Import-time freezes: NONE repo-wide. Stale dicts: none. The single defect is `tools/mcp_tool.py:_build_safe_env` (`:494-545`) + `_SAFE_ENV_KEYS` (`:372-374`): passes `HOME` raw, drops `HERMES_HOME`, so an MCP child inside a persona turn resolves `<profile>/home/.hermes`. Six live profiles have a `home/` dir. Fixed in B-2. |
| B-A6 | **REFUTED** | `gateway.py:685` omits `--profile` when `profile == "default"`; `gateway/run.py:9519,9651` respawn a bare `hermes gateway restart`; wrappers/units pin correctly only when `HERMES_HOME` is exactly `<root>/profiles/<name>`, otherwise the child re-resolves through the sticky marker and overwrites the pin at `main.py:695`. B-1.3 fences the wrapper generator; the rest is recorded, not patched (upstream / out of scope). |

### 9.1 A-1: the alice-gateway break is not a HERMES_HOME failure

All four candidates the scoping session named are refuted by live read-only
evidence:

- **(i) a console-issued gateway verb** — the launcher contains NO gateway verb
  at all (`grep -n "'gateway'" lib/` → zero matches). It also cannot reap one:
  `ServeOrphanReapPolicy` requires a literal `harness serve` token and
  documents sparing "the `pythonw` agent-gateway (a different verb)".
- **(ii) a second token-less gateway under base** — no base-homed gateway has
  EVER run. `profiles/base/logs/` contains no `gateway*.log`; all 308
  `gateway-exit-diag` rows and every `gateway.log` live under `alice` (plus a
  retired `aliceimagecron`).
- **(iii) a wrapper regenerated without the flag** — the installed wrapper
  double-pins (`.cmd:4,9` and `.vbs:6,17`). But it is **not what has been
  running the gateway**: all 13 starts since 2026-08-01 record
  `stdin_is_tty: true` (a `wscript`-launched hidden service has no tty), from
  three different code checkouts — `X:\Eternia\hermes-agent` ×79,
  `%LOCALAPPDATA%\hermes\hermes-agent` ×44, and on 2026-08-16 two **Claude Code
  agent worktrees**.
- **(iv) the sticky marker diverting a bare run** — the marker reads `alice`,
  so rung 3 sends a bare `gateway run` TO alice. It helps here; it cannot divert.

Positively established, partially: **2 of 10 observed terminations coincide
within seconds with a Windows shutdown** (`1074`/`6006`/`109` at 2026-08-14
12:33 and 2026-08-15 22:53 local, against `last_heartbeat_at` of 12:32:50 and
22:53:12 — the diag timestamps are UTC). Nothing restarts the gateway at boot,
because it is not actually running as the installed service.

**Not established:** the mechanism of the other 8. `last_heartbeat_at` is not a
reliable death clock — two runs stopped heartbeating ~50 s after start yet
persisted for hours — so those terminations cannot be dated from this evidence.

**Consequence for this plan, stated plainly: B-1..B-5 do not fix the observed
alice-gateway break, and must not be described as doing so.** The failure is a
supervision gap, not a profile-resolution failure. B-1's boot receipt makes the
next occurrence one log line instead of an inference, which is the honest
extent of it.

## 10. B-5 verification log

Live re-measure of §1.2, read-only, key names only (2026-08-16):

| Fact | §1.2 said | Re-measured | Agrees? |
| --- | --- | --- | --- |
| Personas, all bound, targets exist | 5 / yes / yes | 5 / yes / yes | yes |
| Instances, null `profile_id` | 16 / 1 (`personainst_qa_agent_644595cc`) | 16 / 1, same id | yes |
| `.env` keys: base / alice | 1 / 18 incl. `TELEGRAM_*` | 1 (no `TELEGRAM_*`) / 18 (has them) | yes |
| `launcher-qa-direct` | no `.env` | no `.env` | yes |
| `active_profile` marker | — | `alice` | new |
| Profiles with a `home/` dir | — | 6: aliceimagecron, base, launcher-dev, neko, qa, unbounded | new (A-5 blast radius) |

**Migration claim — VERIFIED, not trusted.** The dry run was replicated against
the live root in pure stdlib (no hermes import, no write, no `harness` command):
**planned = 1** (`personainst_qa_agent_644595cc` → `launcher-qa`), **busy = 0**,
skipped = 15. The one candidate's target is exactly what its persona already
binds and exactly what `persona_instance_summary` already renders, so a null
resolves identically before and after. No silent behaviour change.

| Stage | Landed | New tests | Mutants killed |
| --- | --- | --- | --- |
| B-1 | resolution rung recorded; typed gateway boot receipt + suspicious-home warning; wrapper-pin invariant | 17 | 10/10 |
| B-2 | `tools/mcp_tool.py` stdio child carries the resolved home; standard stated in 2 places | 7 | 4/4 |
| B-3 | launcher setting demoted in title, copy, docs; projection unchanged | 6 | 5/5 |
| B-4 | creation stamps the persona's profile; dry-run-first backfill | 11 | 7/7 |

**Known behaviour change (B-2):** stdio MCP children previously received no
`HERMES_HOME` and now receive the persona's resolved home.
`HERMES_AUTH_HOME` is deliberately NOT propagated — the head-pin
(`profile_context.py:303-306`) keeps one operator-visible auth/SessionDB store,
and changing it is the provider-login plan's call, not this one.

**Not done:** the B-4 backfill (`backfill_instance_profile_ids`) is a library
function the operator cannot yet invoke — no CLI verb reaches it.
(Superseded 2026-08-19: this paragraph also said `rebind_persona_profile` had
zero callers. It has one — `harness agent set-profile`
(`hermes_cli/harness.py:2769`) — and has had since the verb landed. Its sibling
`backfill_instance_profile_ids` is the one still unwired.) No Stage C screenshot was captured (the live launcher was left
untouched).

---

*Standing constraints honoured: live root `X:/Eternia/.hermes/` read-only; no
credential value printed or copied (key names only); no `harness serve` child
spawned; PID 21004 / port 8090 / the running launcher untouched; `dart
format` forbidden; no production code in this change; not committed to
`main`.*
