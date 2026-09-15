# Machine roots — cross-platform config path portability

## The problem

Profile `config.yaml` files bind capabilities to locations of *other* checkouts:

```yaml
mcp_servers:
  launcher_qa:
    command: X:\Unreal Engine\Engine\Launcher\EterniaLauncher\tool\stagec_qa_mcp_server\build\stagec_qa_mcp_server.exe
    env:
      STAGEC_QA_REPO_ROOT: X:\Unreal Engine\Engine\Launcher\EterniaLauncher
      STAGEC_SCREENSHOT_HELPER: X:\Unreal Engine\...\Capture-StageCWindowScreenshot.ps1
```

Those entries are unusable on a second machine and on any non-Windows OS.
**Relative paths do not fix this**: the relative distance between the Hermes
install and an unrelated checkout is *more* machine-specific, not less.

## The model

Keep the capability/binding split the harness already uses:

| Layer | Where | Portable? | Synced? |
| --- | --- | --- | --- |
| Capability — "this persona needs `launcher_qa`" | `AgentPersona.required_mcp_servers` | yes | yes |
| Config binding — "`launcher_qa` lives at `${roots.eternia_launcher}/tool/...`" | `mcp_servers.<name>.command` | yes | yes |
| Machine binding — "`eternia_launcher` is `X:\Unreal Engine\...`" | `machine_roots.json` | **no** | **never** |

The config keeps a logical root plus a repo-relative tail. Only the last row is
machine-specific, and it never leaves the machine.

## Grammar

| Token | Expands to |
| --- | --- |
| `${roots.<name>}` | the absolute local path bound to `<name>` in `machine_roots.json` |
| `${exe_suffix}` | `.exe` on Windows, empty string elsewhere |

```yaml
mcp_servers:
  launcher_qa:
    platforms: [windows]          # honest availability gate (optional)
    command: ${roots.eternia_launcher}\tool\stagec_qa_mcp_server\build\stagec_qa_mcp_server${exe_suffix}
    env:
      STAGEC_QA_REPO_ROOT: ${roots.eternia_launcher}
```

Tails are split on **both** `/` and `\` and re-joined with `pathlib`, so the same
config text resolves correctly on Windows, macOS, and Linux. Nothing in the
resolver concatenates a hardcoded separator.

`platforms:` is a declarative availability gate. An entry that genuinely cannot
run anywhere but Windows (a `.ps1` helper, a `powershell.exe` command) declares
it, and on macOS/Linux the capability is reported as **unavailable** rather than
handed a binding that can only fail at spawn time. An entry with no `platforms:`
key is platform-agnostic, which is what every pre-existing config is.

## Registry

`machine_roots.json`, looked up machine-wide first and then per-profile
(per-key override):

1. `<default hermes root>/machine_roots.json`
2. `$HERMES_HOME/machine_roots.json`

```json
{
  "schema_version": 1,
  "roots": {
    "eternia_launcher": "X:\\Unreal Engine\\Engine\\Launcher\\EterniaLauncher",
    "eternia_backend": "X:\\Unreal Engine\\Engine\\EterniaBackend\\eternia-backend",
    "hermes_agent": "X:\\Eternia\\hermes-agent"
  }
}
```

The filename is in `realm_sync.HARD_EXCLUDED_PATH_PARTS`, so a publish that
would carry it fails closed with `sync_secret_excluded` — same treatment as
`auth.json` and `state.db`.

## Resolution chokepoint

Everything goes through `agent_runtime/machine_roots.py`. There is one private
walker; the public surface is two views of it:

* `expand_config_paths(value, ...)` — raises `MachineRootError` (strict; use
  where a dead path must never reach a spawn or a workdir).
* `path_token_issues(value, ...)` — returns typed `PathTokenIssue` rows (use
  where the caller reports instead of raising).
* `resolve_mcp_servers(servers, ...)` — expansion + platform gate for an
  `mcp_servers` map; unresolvable and platform-gated entries are **dropped**
  with a loud typed log, never spawned against a literal token.

Consumers:

| Seam | File | Behaviour on failure |
| --- | --- | --- |
| Runtime MCP client | `tools/mcp_tool.py::_load_mcp_config` | drop the server + `logger.error` with the typed code |
| `hermes mcp test/add` probe | `hermes_cli/mcp_config.py::_resolve_mcp_server_config` | raise; the CLI prints the typed reason |
| Readiness | `agent_runtime/profile_readiness.py` | `mcp_attention` + `machine_root_issues[]` |
| Persona `repo_scope` | `agent_runtime/config.py::_expand_machine_root_tokens` | keeps the literal token (never blanked, never guessed) so readiness can name the fix |

Failure codes: `unbound_root`, `root_target_missing`, `invalid_root_token`,
`invalid_registry`, `platform_unsupported`.

## Backward compatibility

Resolution is a **no-op** for any value with no token. Every existing config
that stores a plain absolute path keeps working byte-identically — see
`tests/agent_runtime/test_machine_roots.py::test_existing_mcp_config_without_tokens_survives_resolution_byte_identical`.

## CLI

```bash
hermes harness roots list
hermes harness roots set eternia_launcher "X:\Unreal Engine\Engine\Launcher\EterniaLauncher" --yes
hermes harness roots unset eternia_launcher --yes
hermes harness roots migrate --dry-run          # preview; writes NOTHING
hermes harness roots migrate --yes              # apply
```

`migrate` rewrites existing absolute paths into token form as a **text** edit
(the live configs carry hand-written comments that a YAML round-trip would
erase). Safety is by verification, not by trust: every planned file is re-parsed,
its tokens re-expanded through the same chokepoint, and compared structurally
against the original. A plan whose verification is non-empty is reported
`migration_verification_failed` and is never written. `--dry-run` is honoured at
the store chokepoint (`write_machine_roots` / `apply_config_migration`), not in
the CLI handler, so a verb that forgets to thread `args.dry_run` cannot mutate
on a preview.

## Applying the migration to a live runtime

Token-form configs are only understood by a process running this code. A live
`hermes serve` (or any long-running agent) started before the change holds the
OLD resolver in memory and would spawn the literal `${roots.…}` token. Sequence
an apply as: land the code → restart `serve` / respawn agents → `roots migrate
--yes`.
