---
type: handoff
target_repo: {{target_repo}}
commit: {{commit}}
branch: {{branch}}
dirty: {{dirty}}
classification: {{classification}}
created_by_profile: {{profile}}
created_at: {{ts_utc}}
worker_id: {{worker_id}}
card_id: {{card_id}}
---

# {{title}}

> Handoff written by `arcadia_brain_create_handoff` ([Stage 3](../04-stage-3-arcadia-brain-mcp.md)).
> Conforms to [Agent QA & Release Doctrine §Evidence beats claims](../../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Agent%20QA%20%26%20Release%20Doctrine.md#evidence-beats-claims).

## Summary

{{summary_md}}

## Workspace

- **Repo**: `{{target_repo}}`
- **Commit**: `{{commit}}`  ({{branch}}{{#dirty}}, dirty{{/dirty}})
- **Profile**: `{{profile}}`

## Commands run

| Command | Exit | Duration |
|---------|------|----------|
{{#commands}}
| `{{cmd}}` | {{exit_code}} | {{duration_s}}s |
{{/commands}}

## Artifacts

{{#artifacts}}
- `{{kind}}`: `{{path}}` (sha256: `{{sha256_short}}`, {{size_bytes}} B)
{{/artifacts}}

{{#has_screenshots}}
## Screenshots

| Label | Blank-pixel ratio | Path |
|-------|-------------------|------|
{{#screenshots}}
| `{{label}}` | {{blank_pixel_ratio}} | `{{path}}` |
{{/screenshots}}
{{/has_screenshots}}

## Redaction scan

- Files scanned: **{{redaction_scan.files_scanned}}**
- Findings: **{{redaction_scan.findings}}**
- Patterns checked: {{redaction_scan.patterns_checked}}

## Not tested

{{#not_tested}}
- {{.}}
{{/not_tested}}
{{^not_tested}}
_(none — handoff claims full scope)_
{{/not_tested}}

{{#failure_class}}
## Failure class

`{{failure_class}}`

## Owner recommendation

`{{owner_recommendation}}`
{{/failure_class}}

## Linked artifacts

<!-- arcadia_brain_link_artifact appends rows here -->
