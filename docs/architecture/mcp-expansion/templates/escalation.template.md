---
type: escalation
kind: {{kind}}
created_at: {{ts_utc}}
created_by_profile: {{profile}}
decision_needed_by: {{decision_needed_by}}
parent_card_id: {{parent_card_id}}
---

# Escalation — {{kind}} — {{ts_utc}}

> Written by `arcadia_pm_escalate_gap` ([Stage 4.5](../09-stage-4.5-arcadia-pm-mcp.md)).
> PM escalated because the rule cascade matched: **{{rule_id}}** — _"{{doctrine_bullet}}"_

## What blocked

{{summary_md}}

## Evidence

{{#evidence_paths}}
- `{{.}}`
{{/evidence_paths}}

## What PM tried (or deliberately did not try)

{{#attempts}}
- {{.}}
{{/attempts}}
{{^attempts}}
_(none — this kind escalates immediately per doctrine)_
{{/attempts}}

## Decision needed

{{decision_md}}

## Who decides

`{{owner}}` _(per doctrine rule `{{rule_id}}`)_

## Linked cards

{{#linked_cards}}
- `{{card_id}}` — {{title}} (`{{status}}`)
{{/linked_cards}}
