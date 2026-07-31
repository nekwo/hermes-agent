---
sidebar_position: 8
title: "MCP Config Reference"
description: "Reference for Hermes Agent MCP configuration keys, filtering semantics, and utility-tool policy"
---

# MCP Config Reference

This page is the compact reference companion to the main MCP docs.

For conceptual guidance, see:
- [MCP (Model Context Protocol)](/user-guide/features/mcp)
- [Use MCP with Hermes](/guides/use-mcp-with-hermes)

## Root config shape

```yaml
mcp_servers:
  <server_name>:
    command: "..."      # stdio servers
    args: []
    env: {}

    # OR
    url: "..."          # HTTP servers
    headers: {}

    # Optional HTTP/SSE TLS settings:
    ssl_verify: true                # bool or path to a CA bundle (PEM)
    client_cert: "/path/to/cert.pem"  # mTLS client certificate (see below)
    # client_key: "/path/to/key.pem"  # optional, when key lives in a separate file

    enabled: true
    timeout: 120
    connect_timeout: 60
    supports_parallel_tool_calls: false
    tools:
      include: []
      exclude: []
      resources: true
      prompts: true
```

## Server keys

| Key | Type | Applies to | Meaning |
|---|---|---|---|
| `command` | string | stdio | Executable to launch |
| `args` | list | stdio | Arguments for the subprocess |
| `env` | mapping | stdio | Environment passed to the subprocess |
| `url` | string | HTTP | Remote MCP endpoint |
| `headers` | mapping | HTTP | Headers for remote server requests |
| `ssl_verify` | bool or string | HTTP | TLS verification. `true` (default) uses system CAs, `false` disables verification (insecure), or a string path to a custom CA bundle (PEM) |
| `client_cert` | string or list | HTTP | mTLS client certificate. String = path to a PEM file containing cert + key. List `[cert, key]` = separate files. List `[cert, key, password]` = encrypted key |
| `client_key` | string | HTTP | Path to the client private key, when `client_cert` is a string and the key is in a separate file |
| `enabled` | bool | both | Skip the server entirely when false |
| `timeout` | number | both | Tool call timeout in seconds (default: `300`) |
| `connect_timeout` | number | both | Initial connection timeout in seconds (default: `60`) |
| `supports_parallel_tool_calls` | bool | both | Allow tools from this server to run concurrently |
| `skip_preflight` | bool | HTTP | Bypass the fail-fast content-type probe for valid Streamable HTTP endpoints whose HEAD/GET answers a non-MCP content type (default: `false`) |
| `transport` | string | HTTP | Set to `sse` to use the SSE transport instead of Streamable HTTP |
| `keepalive_interval` | number | both | Liveness ping cadence in seconds (default: `180`, floored at 5s). Set below the server's session TTL for servers that GC idle sessions quickly |
| `idle_timeout_seconds` | number | stdio | Optional stdio server recycle after idle time (`0` disables). May also live under a `lifecycle:` mapping |
| `max_lifetime_seconds` | number | stdio | Optional stdio server recycle after age (`0` disables). May also live under a `lifecycle:` mapping |
| `tools` | mapping | both | Filtering and utility-tool policy |
| `auth` | string | HTTP | Authentication method. Set to `oauth` to enable OAuth 2.1 with PKCE |
| `sampling` | mapping | both | Server-initiated LLM request policy (see MCP guide) |
| `elicitation` | mapping | both | Server-initiated user-input requests. `enabled` (default `true`) and `timeout` in seconds (default `300`). Form-mode requests route through the approval surface; URL-mode is declined (see MCP guide) |

## Environment variable references

String values anywhere in a server entry (`env`, `headers`, `args`, `url`, …) may reference environment variables with `${VAR}` or the Cursor-style SecretRef form `${env:VAR}` — both resolve to the same variable, so MCP snippets copied from Cursor / Claude configs work unchanged:

```yaml
mcp_servers:
  github:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "${env:GITHUB_TOKEN}"   # same as "${GITHUB_TOKEN}"
```

Values resolve from the active profile's secret scope (falling back to the process environment), so put the secret in `~/.hermes/.env`. An unset variable keeps its literal placeholder.

## One-shot stdio env overrides

Durable `env` (above) is the right place for long-lived environment values that
belong with the server definition. For values you want to inject **for a
single run** — temp file paths, nonces, local ports, short-lived credentials —
Hermes supports two non-persistent override surfaces. Neither writes anything
back to `config.yaml`.

| Surface | Where set | Scope |
|---|---|---|
| `hermes mcp test <name> --env KEY=VALUE` | CLI flag | The single `mcp test` invocation |
| `HERMES_MCP_ENV_<SERVER>_<KEY>=value` | Parent process env | Every stdio MCP discovery/session for that server in this process |

Both layers are merged on top of the safe baseline and the durable `env`. The
final merge order, lowest to highest precedence, is:

1. Safe baseline (`PATH`, `HOME`, `USER`, `LANG`, `LC_ALL`, `TERM`, `SHELL`,
   `TMPDIR`, plus all `XDG_*` variables) from the parent process.
2. Durable `mcp_servers.<name>.env` from `config.yaml`.
3. Process-level namespaced overlay (`HERMES_MCP_ENV_<SERVER>_<KEY>`).
4. CLI `--env KEY=VALUE` overlay (currently only on `hermes mcp test`).

### Server-name normalization

For the `HERMES_MCP_ENV_<SERVER>_<KEY>` form, the server name is uppercased and
every run of non-alphanumeric characters becomes a single `_`:

| Configured name | Env prefix |
|---|---|
| `launcher-qa` | `HERMES_MCP_ENV_LAUNCHER_QA_` |
| `stagec-launcher-qa` | `HERMES_MCP_ENV_STAGEC_LAUNCHER_QA_` |
| `my.server` | `HERMES_MCP_ENV_MY_SERVER_` |

Note that `foo-bar` and `foo_bar` both normalize to `FOO_BAR_`. If you use
similar names, pick distinct ones.

### Security model

Hermes deliberately does **not** inherit arbitrary parent env vars into MCP
subprocesses — see the safe-baseline list above. The two override surfaces are
the explicit opt-in:

- The `HERMES_MCP_ENV_<SERVER>_<KEY>` prefix is the namespace boundary. A
  parent env var without that prefix will not cross into the child.
- `--env` is an explicit per-run flag.

Hermes never logs override values. The CLI prints the count and key names
(`Applied 2 one-shot env override(s): RUNTIME_FILE, TOKEN`) but not values.

### Caveats

- Don't put long-lived secrets in shell history. Prefer temp files or a secret
  manager and inject the resolved value into the env immediately before the
  command.
- On Windows PowerShell, inline POSIX-style `KEY=VALUE cmd` syntax does not
  work. Use `$env:HERMES_MCP_ENV_FOO_TOKEN = "…"; hermes mcp test foo` or a
  short `.ps1` wrapper.

## `tools` policy keys

| Key | Type | Meaning |
|---|---|---|
| `include` | string or list | Whitelist server-native MCP tools. Entries may be exact names or fnmatch-style globs (`*_radar_*`, `get_zones_*`) |
| `exclude` | string or list | Blacklist server-native MCP tools. Same exact-name / glob semantics as `include` |
| `resources` | bool-like | Enable/disable `list_resources` + `read_resource` |
| `prompts` | bool-like | Enable/disable `list_prompts` + `get_prompt` |

## Filtering semantics

### `include`

If `include` is set, only those server-native MCP tools are registered.

```yaml
tools:
  include: [create_issue, list_issues]
```

### `exclude`

If `exclude` is set and `include` is not, every server-native MCP tool except those names is registered.

```yaml
tools:
  exclude: [delete_customer]
```

### Precedence

If both are set, `include` wins.

```yaml
tools:
  include: [create_issue]
  exclude: [create_issue, delete_issue]
```

Result:
- `create_issue` is still allowed
- `delete_issue` is ignored because `include` takes precedence

## Utility-tool policy

Hermes may register these utility wrappers per MCP server:

Resources:
- `list_resources`
- `read_resource`

Prompts:
- `list_prompts`
- `get_prompt`

### Disable resources

```yaml
tools:
  resources: false
```

### Disable prompts

```yaml
tools:
  prompts: false
```

### Capability-aware registration

Even when `resources: true` or `prompts: true`, Hermes only registers those utility tools if the MCP session actually exposes the corresponding capability.

So this is normal:
- you enable prompts
- but no prompt utilities appear
- because the server does not support prompts

## `enabled: false`

```yaml
mcp_servers:
  legacy:
    url: "https://mcp.legacy.internal"
    enabled: false
```

Behavior:
- no connection attempt
- no discovery
- no tool registration
- config remains in place for later reuse

## Empty result behavior

If filtering removes all server-native tools and no utility tools are registered, Hermes does not create an empty MCP runtime toolset for that server.

## Example configs

### Safe GitHub allowlist

```yaml
mcp_servers:
  github:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "***"
    tools:
      include: [list_issues, create_issue, update_issue, search_code]
      resources: false
      prompts: false
```

### Stripe blacklist

```yaml
mcp_servers:
  stripe:
    url: "https://mcp.stripe.com"
    headers:
      Authorization: "Bearer ***"
    tools:
      exclude: [delete_customer, refund_payment]
```

### Resource-only docs server

```yaml
mcp_servers:
  docs:
    url: "https://mcp.docs.example.com"
    tools:
      include: []
      resources: true
      prompts: false
```

### TLS client certificate (mTLS)

For HTTP/SSE servers that require a client certificate, set `client_cert` (and optionally `client_key`):

```yaml
mcp_servers:
  # Combined cert + key in a single PEM file
  internal_api:
    url: "https://mcp.internal.example.com/mcp"
    client_cert: "~/secrets/mcp-client.pem"

  # Separate cert and key files
  partner_api:
    url: "https://mcp.partner.example.com/mcp"
    client_cert: "~/secrets/client.crt"
    client_key: "~/secrets/client.key"

  # Encrypted key with a passphrase (3-element list form)
  bank_api:
    url: "https://mcp.bank.example.com/mcp"
    client_cert: ["~/secrets/client.crt", "~/secrets/client.key", "my-passphrase"]

  # Custom CA bundle (private CA / self-signed server)
  lab_api:
    url: "https://mcp.lab.local/mcp"
    ssl_verify: "~/secrets/lab-ca.pem"
    client_cert: "~/secrets/lab-client.pem"
```

Notes:
- Paths support `~` expansion. Missing files fail fast at connect time with a server-scoped error message.
- `ssl_verify: false` disables server certificate verification entirely. Don't use this with real services.
- Works on both Streamable HTTP and SSE transports.

## Reloading config

After changing MCP config, reload servers with:

```text
/reload-mcp
```

## Tool naming

Server-native MCP tools become:

```text
mcp__<server>__<tool>
```

Examples:
- `mcp__github__create_issue`
- `mcp__filesystem__read_file`
- `mcp__my_api__query_data`

Utility tools follow the same prefixing pattern:
- `mcp__<server>__list_resources`
- `mcp__<server>__read_resource`
- `mcp__<server>__list_prompts`
- `mcp__<server>__get_prompt`

The double-underscore delimiter (`mcp__…__…`) matches the convention used by Claude Code, Codex, and OpenCode, and disambiguates the server/tool boundary even when either component contains underscores.

### Name sanitization

Any character that is not a letter, digit, or underscore (hyphens, dots, spaces, etc.) in both server names and tool names is replaced with an underscore before registration. This ensures tool names are valid identifiers for LLM function-calling APIs.

For example, a server named `my-api` exposing a tool called `list-items.v2` becomes:

```text
mcp__my_api__list_items_v2
```

Keep this in mind when writing `include` / `exclude` filters — use the **original** MCP tool name (with hyphens/dots), not the sanitized version.

## OAuth 2.1 authentication

For HTTP servers that require OAuth, set `auth: oauth` on the server entry:

```yaml
mcp_servers:
  protected_api:
    url: "https://mcp.example.com/mcp"
    auth: oauth
```

Behavior:
- Hermes uses the MCP SDK's OAuth 2.1 PKCE flow (metadata discovery, dynamic client registration, token exchange, and refresh)
- On first connect, a browser window opens for authorization
- Tokens are persisted to `~/.hermes/mcp-tokens/<server>.json` and reused across sessions
- Token refresh is automatic; re-authorization only happens when refresh fails
- Only applies to HTTP/StreamableHTTP transport (`url`-based servers)
