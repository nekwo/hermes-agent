# Templates

Markdown skeletons referenced from the stage docs. Workers / orchestrators emit these verbatim with placeholders filled in.

| File | Emitted by | Lives in |
|------|------------|----------|
| [`handoff.template.md`](handoff.template.md) | `arcadia_brain_create_handoff` ([Stage 3](../04-stage-3-arcadia-brain-mcp.md)) | `ArcadiaLabs_Brain/handoffs/<DATE>-<slug>.md` |
| [`closure_note.template.md`](closure_note.template.md) | `arcadia_release_create_closure_note` ([Stage 5](../06-stage-5-release-mcp.md)) | `ArcadiaLabs_Brain/closures/<DATE>-<target>-<class>-<commit>.md` |
| [`escalation.template.md`](escalation.template.md) | `arcadia_pm_escalate_gap` ([Stage 4.5](../09-stage-4.5-arcadia-pm-mcp.md)) | `ArcadiaLabs_Brain/escalations/<DATE>-<slug>.md` |
| [`recovery_card.template.md`](recovery_card.template.md) | `arcadia_pm_create_recovery_card` ([Stage 4.5](../09-stage-4.5-arcadia-pm-mcp.md)) | kanban card `body` field |
