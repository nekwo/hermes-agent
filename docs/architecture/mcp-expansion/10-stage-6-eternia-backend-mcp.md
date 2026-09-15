# Stage 6 — Eternia Backend MCP

> Parent: [`../mcp-expansion-roadmap.md`](../mcp-expansion-roadmap.md) §Layer 3 → `eternia_backend_mcp`.
> Audit context: [`00-deep-audit.md`](00-deep-audit.md) §A.6, [`08-second-pass-audit-and-expansion.md`](08-second-pass-audit-and-expansion.md) §R8 (gap).

This stage was missing in the first pass; it is the backend counterpart to Stage 1's launcher MCP.

## Goal

Wrap the backend gate scripts ([`eternia-backend/scripts/test.sh`](../../../../../Unreal%20Engine/Engine/EterniaBackend/eternia-backend/scripts/test.sh)) and the local Docker stack as typed MCP tools so a PM or release run can call them without operators memorizing flag combinations, and so Stage 5 release classification has a reliable evidence source.

This is **product-private** — encodes backend-repo paths, k8s namespaces, and Eternia-specific service names.

## Inventory (existing)

| Asset | Path | Role |
|---|---|---|
| Full test gate | `eternia-backend/scripts/test.sh` (no flag) | Postgres + Redis + Centrifugo via `docker-compose.test.yml`, runs `manage.py check`, `makemigrations --check --dry-run`, `migrate`, then `test`. **The doctrine's required gate** for deploy readiness. |
| With Keycloak | `scripts/test.sh --keycloak` | Adds Keycloak realm to the stack. |
| Sqlite escape hatch | `scripts/test.sh --sqlite` | Skips every `@requires_*` test. **Explicit Tony-only** per doctrine. |
| Infra-only | `scripts/test.sh --infra-only` / `--infra-down` / `--teardown` | Stack lifecycle. |
| Compose file | `docker-compose.test.yml` | Postgres + Redis + Centrifugo + optional Keycloak profile. |
| Staging deploy | `scripts/deploy-stagec-staging-image.ps1`, `scripts/deploy.sh` | k8s staging deploys. |
| K8s manifests | `eternia-backend/k8s/` | reference deployments (backend, centrifugo, livekit, redis, voice-transcribe). |
| Health probe | `scripts/probe_redis_heartbeat.py` | example of standalone health probe pattern. |
| Doctrine | [Agent QA & Release Doctrine §Backend deployment doctrine summary](../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Agent%20QA%20%26%20Release%20Doctrine.md#backend-deployment-doctrine-summary) | "Postgres/Docker full gate required before backend push/deploy readiness claims. SQLite is only an explicit Tony-requested escape hatch. CI may be authoritative if local WSL hangs." |

## Tool surface

### Service stack lifecycle

| Tool | Args | Behavior |
|---|---|---|
| `eternia_backend_start_local_infra` | `with_keycloak: bool = false`, `dry_run=true` | `scripts/test.sh --infra-only [--keycloak]`. Returns when containers report healthy. Streams progress as MCP notification events; final result is `{state: "healthy", services: [...], duration_s}`. |
| `eternia_backend_stop_local_infra` | `dry_run=true` | `scripts/test.sh --infra-down`. |
| `eternia_backend_start_keycloak_stack` | `dry_run=true` | Shortcut for `start_local_infra(with_keycloak=true)`. Surfaced separately because keycloak boot is the slow step (max_tries=40 vs 20) — agents need to know it's a different latency class. |
| `eternia_backend_check_services` | `services?` (subset; default all) | Reads `docker compose ps --format json` for the test compose file, returns `[{name, state, health, restart_count}]`. Read-only. |

### Gate execution

| Tool | Args | Returns |
|---|---|---|
| `eternia_backend_run_postgres_full_gate` | `test_args[]`, `with_keycloak=false`, `teardown=false`, `commit?`, `dry_run=true` | Runs `scripts/test.sh [--keycloak] [args...] [--teardown]`. Captures exit code, duration, and the four static-preflight outputs (`manage.py check`, `makemigrations --check --dry-run`, `migrate`, `test`). Returns a structured envelope **with no raw logs** — full log saved to `qa-artifacts/backend_full_gate_<ts>/test_run.log` and the envelope cites the path. |
| `eternia_backend_run_sqlite_escape_hatch` | `test_args[]` | `scripts/test.sh --sqlite [args...]`. **Forces the returned envelope's `classification` to ineligible for `PASS`** at the Stage 5 layer — caller must explicitly mark the run as `sqlite_escape_hatch=true` for the classifier. |
| `eternia_backend_check_only` | — | `python manage.py check` standalone. Fastest signal (~1s). |
| `eternia_backend_makemigrations_dry_check` | — | `python manage.py makemigrations --check --dry-run`. Detects model/migration drift. |

### Deploy readiness

| Tool | Args | Returns |
|---|---|---|
| `eternia_backend_classify_deploy_readiness` | `target_env` (`staging`/`production`), `gate_results[]`, `branch_state`, `ci_evidence_url?` | Wraps [Stage 5 `arcadia_release_classify`](06-stage-5-release-mcp.md) with backend-specific overrides: `postgres_full` required; `sqlite_escape_hatch` blocks PASS; production target additionally requires `manage_check` + `makemigrations` + Postgres full **on the deploy commit** (not just on any prior commit). |
| `eternia_backend_verify_secret_contract` | `target_env` | Checks the k8s secret references the backend deploy expects (per [`Environment & Staging Policy.md`](../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Environment%20%26%20Staging%20Policy.md)). Does NOT fetch values. Returns `{secret_name, present: bool, last_rotated?: ts}`. |

### Staging deploy (mutating)

| Tool | Args | Behavior |
|---|---|---|
| `eternia_backend_build_staging_image` | `commit`, `dry_run=true` | Resolves to the `Dockerfile.stagec` build invocation. Returns image digest, tag, registry. |
| `eternia_backend_deploy_staging_image` | `image_digest`, `dry_run=true` | Wraps `scripts/deploy-stagec-staging-image.ps1`. **Refuses without an upstream `eternia_backend_classify_deploy_readiness` result tagged `PASS`** (caller must supply the classification artifact path; tool verifies it). |

Production deploy is **deliberately not exposed.** Per the doctrine, production touches require Tony — Stage 6 does not include a `deploy_production` tool.

## Static preflight breakdown (encoded)

`test.sh` runs four steps before the test suite. Stage 6 exposes them individually so a failure surfaces precisely:

| Step | Time | Failure class |
|---|---|---|
| `manage.py check` | <1s | `HOST_ENV_MISSING` if Django config broken |
| `makemigrations --check --dry-run` | <1s | `NEEDS_FIX` if model drift detected |
| `migrate` | ~5–15s | `HOST_ENV_MISSING` if DB unreachable |
| `test ...` | varies | `QA_GATE_FAIL` if any test fails |

`run_postgres_full_gate` reports the step that failed first; downstream steps return `NOT_RUN`.

## Sqlite escape hatch contract

Per doctrine, `--sqlite` is "the explicit Tony-requested escape hatch." Stage 6 enforces:

- `run_sqlite_escape_hatch` does NOT call `arcadia_release_classify` itself.
- The returned envelope contains `classification_eligibility: "sqlite_only"`, which the Stage 5 classifier reads and translates to "PASS requires `sqlite_escape_hatch=true` AND `next_actions` cite re-running without `--sqlite`."
- A test asserts no PR that includes only `sqlite_escape_hatch` evidence ever produces a deploy-readiness PASS.

## CI parity rule (encoded)

Per doctrine: "GitHub Actions may be authoritative if local WSL full gate hangs, but local PASS should not be claimed without the local full gate."

`classify_deploy_readiness` accepts `ci_evidence_url: str | null`:

- If local `postgres_full` is `NOT_RUN` AND `ci_evidence_url` is present AND the URL matches the allowlist (`github.com/<org>/eternia-backend/actions/runs/...`):
  - PASS allowed only with `rationale: "ci_authoritative_local_hang"`.
  - `next_actions[]` includes `"document local hang in closure note; re-run when env permits"`.
- Otherwise → `NOT_RUN_MISSING_CONTEXT`.

## Permission gate

| Profile | `eternia-backend-mcp` allowed? |
|---|---|
| `alice` | yes (full) |
| `pm` | yes (lifecycle + check + classify) |
| `reviewer` | yes (read-only — `check_services`, `verify_secret_contract`, `classify_deploy_readiness`) |
| `claude_backend`, `gpt_backend`, `spark_backend` | yes (gate-run + check + makemigrations) |
| `spark_logreader` | check-only |
| `claude_launcher*`, `launcher-qa*` | no — launcher-side workers do not run backend gates |

## Audit log + artifacts

- Every gate run writes to `qa-artifacts/backend_<gate>_<ts>/` with:
  - `test_run.log` (full output, pre-redaction)
  - `redaction_scan.safe.json`
  - `gate_envelope.safe.json` (the structured return value)
  - `compose_ps.safe.json` (service health snapshot)
- Every mutating call writes a `backend_mcp_<verb>` row to `control_events` ([§R1](08-second-pass-audit-and-expansion.md#r1--audit-log-location-stage-2--stage-4--cross-cutting-4-correction)).
- Logs run through `agent/redact.py` before any envelope returns. Redaction includes Django session middleware secrets, `DJANGO_SECRET_KEY`, Keycloak `KEYCLOAK_BACKEND_CLIENT_SECRET`, Centrifugo `CENTRIFUGO_TOKEN_HMAC_SECRET_KEY`.

## Concurrency

- `run_postgres_full_gate` acquires an advisory lock at `~/.hermes/locks/eternia-backend-gate.lock` — only one full gate at a time per host. Concurrent calls return `PROCESS_STALE`-class error with the holding PID.
- `check_services` / `check_only` / `makemigrations_dry_check` are reentrant — no lock.
- `build_staging_image` / `deploy_staging_image` acquire a deploy-pipeline lock; concurrent deploy attempts fail-closed.

## Acceptance

Stage 6 is done when:

1. `hermes mcp eternia backend serve` runs from the `pm` or `alice` profile and exposes the 10 tools above.
2. `run_postgres_full_gate` against a clean checkout returns `{exit_code: 0, gate_envelope: ...}` and matches the doctrine PASS shape.
3. `run_sqlite_escape_hatch` produces an envelope that Stage 5 classifier refuses to grade PASS without explicit `sqlite_escape_hatch=true`.
4. Redaction scan on a captured `test_run.log` containing fake `DJANGO_SECRET_KEY=…` finds it and the envelope omits it.
5. `deploy_staging_image` refuses without a verified PASS classification artifact.
6. The CI-authoritative-local-hang path is unit-tested with a fixture URL.

## Risks

- **Docker not installed.** `test.sh` already handles this (exit 127 with explicit message). Stage 6 maps that to `HOST_ENV_MISSING`, not a generic crash.
- **WSL Docker hang.** Doctrine explicitly allows CI authoritative path; encoded in `classify_deploy_readiness`. Tool must NOT mask the hang — `run_postgres_full_gate` has a hard timeout (default 30min) that returns `PROCESS_STALE`.
- **Stale containers from a prior run.** `start_local_infra` is idempotent; `check_services` reports `restart_count` which signals crash loops.
- **`.env.test` template drift.** `ensure_env_file` in `test.sh` copies from `.env.test.example` if missing. Stage 6's `run_postgres_full_gate` first verifies `.env.test.example` exists; if not, returns `HOST_ENV_MISSING` with the missing-file path.
- **Tests accidentally running against dev DB.** `test.sh` exports `DJANGO_USE_SQLITE=""` to force-clear the env. Stage 6 does the same defensively and asserts the env at gate-start.

## Out of scope

- Production deploys. Tony only.
- Database migrations on production. Tony only.
- Centrifugo / LiveKit / Voice deploy. Backend MCP wraps `eternia-backend`; sibling services get their own MCPs if needed.
- Cross-service load tests. Out of MCP scope; those are operator scripts.
