# Recovery — {{parent_card_title}}

> Recovery card created by `arcadia_pm_create_recovery_card` ([Stage 4.5](../09-stage-4.5-arcadia-pm-mcp.md)).
>
> Parent card: `{{parent_card_id}}`
> Scope: `{{scope}}` ({{scope_reason}})
> Doctrine bullet: _"{{doctrine_bullet}}"_

## Summary

{{summary_md}}

## What the parent gate showed

{{parent_failure_summary_md}}

## Blocker evidence

{{#blocker_evidence_paths}}
- `{{.}}`
{{/blocker_evidence_paths}}

## Acceptance for this card

- The parent gate that flagged this (`{{parent_failure_gate}}`) re-runs green on the same commit.
- Redaction scan over the new run artifacts: 0 findings.
- New gate envelope written and linked from the parent card.
- This card's handoff cites the parent card id `{{parent_card_id}}`.

## Out of scope

- Anything beyond the named `scope: {{scope}}` boundary. Per the "Avoid card sprawl" doctrine bullet, sibling recoveries are rejected by `arcadia_pm_create_recovery_card` unless `force_new=true`.
