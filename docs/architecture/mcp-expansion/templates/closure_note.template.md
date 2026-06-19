---
type: closure
target: {{target}}
classification: {{classification}}
rubric_version: {{rubric_version}}
commit: {{commit}}
branch: {{branch}}
dirty: {{dirty}}
created_at: {{ts_utc}}
manifest_path: {{manifest_path}}
created_by_profile: {{profile}}
---

# {{target}} closure — {{classification}} @ {{commit_short}}

> Closure note written by `arcadia_release_create_closure_note` ([Stage 5](../06-stage-5-release-mcp.md)).
> Rubric version: **{{rubric_version}}**.

## Gates run

| Gate | Status | Evidence |
|------|--------|----------|
{{#gates}}
| `{{gate}}` | {{status}} | `{{evidence_path}}` |
{{/gates}}

## Branch state

`{{branch}}`{{#dirty}} *(dirty)*{{/dirty}}, {{ahead_of_main}} ahead, {{behind_main}} behind main.

{{#blockers.length}}
## Blockers

{{#blockers}}
- **{{gate}}** ({{status}}): {{reason}}{{#evidence_path}} — `{{evidence_path}}`{{/evidence_path}}
{{/blockers}}
{{/blockers.length}}
{{^blockers.length}}
## Blockers

_none_
{{/blockers.length}}

## Next actions

{{#next_actions}}
- {{.}}
{{/next_actions}}

## Rationale

Doctrine bullets matched:

{{#rationale.doctrine_bullets_matched}}
- "{{.}}"
{{/rationale.doctrine_bullets_matched}}

{{#rationale.caveat}}
> ⚠ **Caveat:** {{rationale.caveat}}
{{/rationale.caveat}}

{{#sqlite_escape_hatch}}
> ⚠ **SQLite escape hatch was used.** Per [Agent QA & Release Doctrine](../../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Agent%20QA%20%26%20Release%20Doctrine.md#backend-deployment-doctrine-summary), this run is **not deploy-ready** until `scripts/test.sh` (no flag) passes.
{{/sqlite_escape_hatch}}

{{#ci_evidence_url}}
> ℹ **CI-authoritative path.** Local full gate did not run; CI run at {{ci_evidence_url}} is the evidence of record. Re-run locally when the host permits.
{{/ci_evidence_url}}

{{#credential_contract}}
## Credential contract verified

- Keycloak client: `{{credential_contract.keycloak_client}}`
- Realm: `{{credential_contract.realm}}`
- k8s secret: `{{credential_contract.k8s_secret_path}}`
- Callback URL: `{{credential_contract.callback_url}}`
{{/credential_contract}}

## Linked artifacts

- Manifest: `{{manifest_path}}`
{{#linked_artifacts}}
- {{kind}}: `{{path}}` (sha256: `{{sha256_short}}`)
{{/linked_artifacts}}
