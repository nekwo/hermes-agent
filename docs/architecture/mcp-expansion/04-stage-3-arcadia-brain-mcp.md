# Stage 3 — Arcadia Brain MCP

> Parent: [`../mcp-expansion-roadmap.md`](../mcp-expansion-roadmap.md) §Layer 2 → `arcadia_brain_mcp` + §Recommended staged build → Stage 3.
> Audit context: [`00-deep-audit.md`](00-deep-audit.md) §A.7.

## Goal

Make TonyBrain + ArcadiaLabs_Brain + child brains addressable as typed MCP tools so agents stop poking raw markdown files and so brain mutations leave an append-only audit trail.

This is **private** to Arcadia — the vault layout, the `.local.md` convention, and the brain-network routing rules in [`CLAUDE.md`](../../../../../Unreal%20Engine/Engine/Launcher/EterniaLauncher/CLAUDE.md) are not upstreamable.

## Inventory (existing, do not duplicate)

| Asset | Path | Role |
|---|---|---|
| Parent brain | `x:\Unreal Engine\Engine\ArcadiaLabs_Brain` | shared operative layer |
| Personal parent link | `ArcadiaLabs_Brain/Parent Brain.local.md` | gitignored, per-machine |
| Committed personal template | `ArcadiaLabs_Brain/Parent Brain.local.example.md` | safe template |
| Launcher child brain | `x:\Unreal Engine\Engine\Launcher\EterniaLauncher\Launcher_Brain\` | feature/architecture/stage notes |
| Backend child brain | `x:\Unreal Engine\Engine\EterniaBackend\eternia-backend\EterniaBackend_Brain\` | backend ops |
| `brain-writer` profile | `~/.hermes/profiles/brain-writer/` | already exists — the profile this MCP gates write access through |
| Brain-routing rules (canonical) | [`CLAUDE.md`](../../../../../Unreal%20Engine/Engine/Launcher/EterniaLauncher/CLAUDE.md#brain-routing-rules) | source-of-truth for which brain owns which fact |

## Vault allowlist (load-bearing)

Stage 3's permission model is **directory allowlist**, not RBAC. The MCP refuses any path outside the configured vaults. Default config:

```yaml
arcadia_brain_mcp:
  vaults:
    arcadia:
      root: "X:\\Unreal Engine\\Engine\\ArcadiaLabs_Brain"
      writable: true
      excludes: ["Parent Brain.local.md", "**/.obsidian/**"]
    launcher:
      root: "X:\\Unreal Engine\\Engine\\Launcher\\EterniaLauncher\\Launcher_Brain"
      writable: true
      excludes: ["**/.obsidian/**"]
    backend:
      root: "X:\\Unreal Engine\\Engine\\EterniaBackend\\eternia-backend\\EterniaBackend_Brain"
      writable: true
      excludes: ["**/.obsidian/**"]
    tony_personal:
      root: "<TONY_BRAIN_ROOT — gitignored, set via env>"
      writable: false                 # read-only by default; explicit promotion required
      excludes: []
  mutation_log_path: "{vault.root}/.brain-mutation-log.jsonl"
```

The MCP MUST:

1. Reject any path that resolves outside `vaults.<v>.root` after `os.path.realpath` (symlink hardening).
2. Refuse `.local.md` files by default (they are per-machine personal scratch — never write through MCP).
3. Refuse `.obsidian/` reads/writes unless the operator request explicitly says "I am configuring Obsidian" — per the CLAUDE.md rule that `.obsidian/` is "vault configuration/runtime state."
4. Use repo-relative output paths in tool responses (per CLAUDE.md: "Prefer repo-relative links/paths in committed notes").

## Tool surface

### Read tools

| Tool | Args | Returns |
|---|---|---|
| `arcadia_brain_list_vaults` | — | `[{name, root, writable, file_count}]` |
| `arcadia_brain_search` | `query`, `vault?`, `limit=20`, `match=any/all` | `[{vault, path, snippet, score}]` (FTS over indexed brain markdown) |
| `arcadia_brain_get_note` | `vault`, `path` | `{path, frontmatter, body, links_in, links_out}` |
| `arcadia_brain_list_notes` | `vault`, `dir?`, `glob?` | `[{path, mtime, size}]` |
| `arcadia_brain_resolve_link` | `vault`, `wiki_link` | `{path, exists}` — resolves `[[..]]` against the vault index |
| `arcadia_brain_get_project_state` | `vault`, `project_slug` | structured frontmatter (status, owner, last_update) |

### Mutating tools (append-only by default)

| Tool | Args | Behavior |
|---|---|---|
| `arcadia_brain_append_note` | `vault`, `path`, `markdown`, `dry_run=true` | Appends to bottom of an existing note OR creates a new one. Records to mutation log. Refuses to overwrite existing lines. |
| `arcadia_brain_create_handoff` | `vault="arcadia"`, `title`, `target_repo`, `summary`, `commands_run[]`, `artifacts[]`, `classification`, `dry_run=true` | Writes a structured handoff note under `ArcadiaLabs_Brain/handoffs/<DATE>-<slug>.md` following the [Agent QA & Release Doctrine](../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Agent%20QA%20%26%20Release%20Doctrine.md) handoff shape (workspace, commit hash, exit codes, artifact roots, redaction scan result, not-tested scope, failure class). |
| `arcadia_brain_link_artifact` | `vault`, `note_path`, `artifact_kind`, `repo_relative_path`, `description?`, `dry_run=true` | Appends a "Linked artifacts" section/row to a note. Validates the path exists (relative to the vault's host repo). |
| `arcadia_brain_update_project_state` | `vault`, `project_slug`, `patch` (frontmatter map), `dry_run=true` | Replaces only the named frontmatter keys; preserves body verbatim. |
| `arcadia_brain_sync_shared_context` | `from_vault`, `to_vault`, `note_path`, `dry_run=true` | Cross-brain link: writes a stub note in the destination vault that links back to the source. Used when a child-brain fact must surface in the parent brain. |

### Tools deliberately omitted

- `arcadia_brain_delete_note` — no delete via MCP. Use git.
- `arcadia_brain_rewrite_note` — no overwrite via MCP. Use git or local edit.
- `arcadia_brain_search_personal` — TonyBrain remains personal; the brain MCP never indexes content from `tony_personal` (only allows resolving direct path reads when `writable: false`).

## Append-only mutation log

Each writable vault gets `.brain-mutation-log.jsonl` at its root. Every mutating tool appends one line:

```json
{
  "ts": "2026-05-15T18:42:11Z",
  "tool": "arcadia_brain_create_handoff",
  "caller_profile": "alice",
  "caller_session_id": "<short id>",
  "vault": "arcadia",
  "path": "handoffs/2026-05-15-stagec-closure.md",
  "dry_run": false,
  "result_class": "ok",
  "diff_summary": {"added_lines": 47, "modified_lines": 0}
}
```

- Log is gitignored by default (vault-internal audit; not for code review).
- A tool `arcadia_brain_show_mutation_log` (read-only, last-N) is part of the read surface above.
- Rotation: handled by the existing `curator` config (`interval_hours: 168`, `archive_after_days: 90`) at vault scope.

## Brain index dependency

`arcadia_brain_search` needs an index. Two options:

1. **Live grep on every call.** Simple, slow at scale (~100ms acceptable for ~1k notes).
2. **Pre-built SQLite FTS** under `{vault.root}/.brain-index.db`, rebuilt on a hook (Stage 4 agentops can wire it).

Stage 3 ships option 1 with a TODO note for option 2 once the vault crosses ~5k notes.

## Acceptance

Stage 3 is done when:

1. `hermes mcp arcadia brain serve` runs and exposes the tool surface above.
2. The `brain-writer` profile's `config.yaml` lists `arcadia-brain-mcp`; no other profile does by default.
3. A handoff written via `arcadia_brain_create_handoff` matches the shape in [Agent QA & Release Doctrine](../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Agent%20QA%20%26%20Release%20Doctrine.md).
4. Path-escape tests cover: `../`, absolute paths outside vault, symlinks pointing outside vault, `.local.md` writes, `.obsidian/` writes.
5. The `tony_personal` vault refuses all writes — verified by test.
6. Mutation log is append-only — refuse-on-conflict test passes.

## Risks

- **Path escape via symlinks.** Mitigation: `os.path.realpath` + `is_relative_to(vault.root)` on every path. Test the failure modes.
- **Cross-vault link creating loops.** Mitigation: `arcadia_brain_sync_shared_context` writes a stub one direction only (the destination's stub points to the source).
- **Overwriting a hand-edited section.** Mitigation: `append_note` refuses any operation that would touch existing bytes; create_handoff refuses if the file already exists.
- **Frontmatter merge corrupts YAML.** Mitigation: `update_project_state` parses with `yaml.safe_load`, validates, then re-emits — if either step fails, returns `error_class: HOST_ENV_MISSING` (frontmatter is malformed) and writes nothing.
- **TonyBrain leak via search.** Mitigation: `arcadia_brain_search` enumerates vaults from config, never globs `tony_personal` contents (only allows direct path reads with `read_note`).

## Out of scope

- Obsidian plugin bridging. The MCP writes markdown that Obsidian reads on next refresh; we do not call Obsidian's API.
- Auto-graph generation. Obsidian builds its own backlinks graph; the MCP's `links_in / links_out` is best-effort regex on `[[..]]` and `]()` patterns.
- Migrating existing brain content. Stage 3 is purely additive.
