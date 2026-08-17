"""Unified provider catalog — one source of truth for the provider universe.

The provider list shown by ``hermes model`` (CLI/TUI) and the desktop Settings
→ Providers tabs (Accounts + API keys) **must be the same set**.  Historically
they were not: the CLI picker read :data:`hermes_cli.models.CANONICAL_PROVIDERS`
(which auto-extends from ``plugins/model-providers/<name>/``), while the desktop
tabs read separate hand-maintained lists (``_OAUTH_PROVIDER_CATALOG``,
``OPTIONAL_ENV_VARS`` + ``PROVIDER_GROUPS``) that nobody kept in sync.  Every
provider added after those lists were written silently went missing from the
GUI — e.g. GitHub Copilot showing up only under "tools", or ``openai-api`` being
configurable from the CLI but not the desktop app.

This module fixes that at the root: it derives ONE descriptor per provider from
the same universe ``hermes model`` renders (``CANONICAL_PROVIDERS``), joining:

* ``auth_type`` / ``api_key_env_vars`` / ``base_url_env_var`` from
  :data:`hermes_cli.auth.PROVIDER_REGISTRY` (credential truth), and
* ``display_name`` / ``description`` / ``signup_url`` from the provider's
  :class:`providers.base.ProviderProfile` when one exists, falling back to the
  ``CANONICAL_PROVIDERS`` entry's ``label`` / ``tui_desc`` and the
  ``OPTIONAL_ENV_VARS`` signup URL otherwise (many profiles leave these blank,
  and four canonical providers have no profile at all — lmstudio, openai-api,
  tencent-tokenhub, xai-oauth — so the fallbacks are load-bearing).

Each descriptor is tagged with the ``tab`` it belongs on (``keys`` vs
``accounts``) based purely on how the provider authenticates.  The desktop
``/api/env`` and ``/api/providers/oauth`` endpoints derive their MEMBERSHIP from
this catalog; the old hand lists are demoted to presentation/override overlays
(bespoke OAuth flow + status resolvers, richer copy, icons, ordering) and no
longer decide which providers exist.

Parity contract (locked by tests): the union of the two tabs equals the
``CANONICAL_PROVIDERS`` universe, i.e. exactly what ``hermes model`` shows.
"""

from __future__ import annotations

from dataclasses import dataclass

# Auth types that authenticate via an account / sign-in flow rather than a
# pasted API key.  These route to the desktop "Accounts" tab; everything else
# (api_key, and aws_sdk which is configured via AWS_REGION/AWS_PROFILE) routes
# to the "API keys" tab.  Mirrors the auth_type strings used in
# hermes_cli.auth.PROVIDER_REGISTRY and providers.base.ProviderProfile.
_ACCOUNTS_AUTH_TYPES: frozenset[str] = frozenset(
    {
        "oauth_device_code",
        "oauth_external",
        "oauth_minimax",
        "external_process",  # copilot-acp: spawns `copilot --acp --stdio`
        "copilot",           # GitHub Copilot token / gh auth
    }
)


@dataclass(frozen=True)
class ProviderDescriptor:
    """One provider, as seen by every surface (CLI picker + both GUI tabs)."""

    slug: str                      # canonical id, e.g. "openai-codex"
    label: str                     # human display name
    description: str               # one-line description
    auth_type: str                 # api_key | oauth_* | external_process | copilot | aws_sdk
    tab: str                       # "keys" | "accounts"
    api_key_env_vars: tuple[str, ...]  # credential env vars (may be empty)
    base_url_env_var: str          # base-URL override env var (may be "")
    signup_url: str                # signup / console URL (may be "")
    order: int                     # CANONICAL_PROVIDERS index — mirrors `hermes model`


def tab_for_auth_type(auth_type: str) -> str:
    """Return the desktop tab ("keys"|"accounts") a provider's auth maps to."""
    return "accounts" if auth_type in _ACCOUNTS_AUTH_TYPES else "keys"


def _split_env_vars(env_vars: tuple[str, ...]) -> tuple[tuple[str, ...], str]:
    """Split a profile's ``env_vars`` into (api_key_vars, base_url_var)."""
    keys = tuple(v for v in env_vars if not (v.endswith("_BASE_URL") or v.endswith("_URL")))
    base = next((v for v in env_vars if v.endswith("_BASE_URL") or v.endswith("_URL")), "")
    return keys, base


def provider_catalog() -> list[ProviderDescriptor]:
    """Return one descriptor per provider in the ``hermes model`` universe.

    Membership is :data:`CANONICAL_PROVIDERS` (the list the CLI/TUI picker
    renders, which auto-extends from provider plugins).  Auth + env come from
    ``PROVIDER_REGISTRY``; display metadata from ``ProviderProfile`` with
    canonical/env fallbacks so providers without a profile (or with blank
    profile metadata) still resolve sensibly.
    """
    from hermes_cli.models import CANONICAL_PROVIDERS

    # PROVIDER_REGISTRY / list_providers are imported lazily and defensively:
    # this module is on the import path of the web server and the CLI, and we
    # never want a provider-plugin import error to blank the whole catalog.
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
    except Exception:
        PROVIDER_REGISTRY = {}

    try:
        from providers import list_providers

        profiles = {p.name: p for p in list_providers()}
    except Exception:
        profiles = {}

    try:
        from hermes_cli.config import OPTIONAL_ENV_VARS
    except Exception:
        OPTIONAL_ENV_VARS = {}

    # Hermes overlays carry auth_type for providers that have no registry/profile
    # entry of their own — notably the ``moa`` virtual provider (auth_type
    # "virtual"), which has no real credential and no network endpoint.
    try:
        from hermes_cli.providers import HERMES_OVERLAYS
    except Exception:
        HERMES_OVERLAYS = {}

    out: list[ProviderDescriptor] = []
    for order, entry in enumerate(CANONICAL_PROVIDERS):
        slug = entry.slug
        cfg = PROVIDER_REGISTRY.get(slug)
        prof = profiles.get(slug)
        overlay = HERMES_OVERLAYS.get(slug)

        # auth_type: registry is authoritative; fall back to profile, then the
        # Hermes overlay (e.g. moa → "virtual"), then api_key.
        auth_type = (
            (getattr(cfg, "auth_type", "") if cfg else "")
            or (getattr(prof, "auth_type", "") if prof else "")
            or (getattr(overlay, "auth_type", "") if overlay else "")
            or "api_key"
        )

        # Credential env vars: registry first (it already normalizes these),
        # else derive from the profile's env_vars tuple.
        if cfg and getattr(cfg, "api_key_env_vars", ()):
            api_key_vars = tuple(cfg.api_key_env_vars)
            base_url_var = getattr(cfg, "base_url_env_var", "") or ""
        elif prof and getattr(prof, "env_vars", ()):
            api_key_vars, base_url_var = _split_env_vars(tuple(prof.env_vars))
        else:
            api_key_vars, base_url_var = (), ""

        label = (
            (getattr(prof, "display_name", "") if prof else "")
            or entry.label
            or slug
        )
        description = (
            (getattr(prof, "description", "") if prof else "")
            or entry.tui_desc
            or label
        )
        signup_url = (getattr(prof, "signup_url", "") if prof else "") or ""
        if not signup_url and api_key_vars:
            info = OPTIONAL_ENV_VARS.get(api_key_vars[0]) or {}
            signup_url = info.get("url") or ""

        out.append(
            ProviderDescriptor(
                slug=slug,
                label=label,
                description=description,
                auth_type=auth_type,
                tab=tab_for_auth_type(auth_type),
                api_key_env_vars=api_key_vars,
                base_url_env_var=base_url_var,
                signup_url=signup_url,
                order=order,
            )
        )
    return out


def provider_catalog_by_slug() -> dict[str, ProviderDescriptor]:
    """Convenience: the catalog keyed by slug."""
    return {d.slug: d for d in provider_catalog()}


# ---------------------------------------------------------------------------
# Login-flow metadata (hoisted 2026-08-16, plan PL-1)
# ---------------------------------------------------------------------------
#
# These rows used to live ONLY inside ``hermes_cli/web_server.py`` as
# ``_OAUTH_PROVIDER_CATALOG`` — welded to the FastAPI dashboard process, and so
# unreachable from any surface that does not run it (the Launcher runs a local
# ``hermes`` CLI, never the dashboard). The module docstring above already names
# that tuple as one of the hand-maintained lists this module exists to unify, so
# the DATA moves here and web_server keeps only the binding of its per-provider
# ``status_fn`` callables (which are dashboard presentation, not identity).
#
# ``flow`` describes the login SHAPE so any client can pick the right UI:
#   ``pkce``        — open a URL, paste the callback code back
#   ``device_code`` — show a user code + verification URI, poll for the token
#   ``external``    — delegated to a third-party CLI; Hermes only reads it
#
# Two rows are deliberately NOT catalog providers but must still be offered as
# sign-ins: the Anthropic PKCE card and the synthetic ``claude-code``
# subscription row.
OAUTH_FLOW_OVERRIDES: tuple[dict, ...] = (
    {
        "id": "nous",
        "name": "Nous Portal",
        "flow": "device_code",
        "cli_command": "hermes auth add nous",
        "docs_url": "https://portal.nousresearch.com",
    },
    {
        "id": "openai-codex",
        "name": "OpenAI OAuth (ChatGPT)",
        "flow": "device_code",
        "cli_command": "hermes auth add openai-codex",
        "docs_url": "https://platform.openai.com/docs",
    },
    {
        "id": "qwen-oauth",
        "name": "Qwen (via Qwen CLI)",
        "flow": "external",
        "cli_command": "hermes auth add qwen-oauth",
        "docs_url": "https://github.com/QwenLM/qwen-code",
    },
    {
        "id": "minimax-oauth",
        "name": "MiniMax (OAuth)",
        # Structurally device-code (verification URI + user code, backend polls
        # the token endpoint) with a PKCE extension for code-binding; the
        # operator experience is the device-code one.
        "flow": "device_code",
        "cli_command": "hermes auth add minimax-oauth",
        "docs_url": "https://www.minimax.io",
    },
    {
        "id": "xai-oauth",
        "name": "xAI Grok OAuth (SuperGrok / Premium+)",
        # Device code is the default because it works in remote shells,
        # containers, and desktop installs without a reachable 127.0.0.1
        # callback.
        "flow": "device_code",
        "cli_command": "hermes auth add xai-oauth",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/guides/xai-grok-oauth",
    },
    {
        "id": "copilot-acp",
        "name": "GitHub Copilot (ACP)",
        "flow": "external",
        "cli_command": "copilot /login",
        "docs_url": "https://docs.github.com/en/copilot",
    },
    {
        "id": "anthropic",
        "name": "Anthropic API Key",
        "flow": "pkce",
        "cli_command": "hermes auth add anthropic",
        "docs_url": "https://docs.claude.com/en/api/getting-started",
    },
    {
        "id": "claude-code",
        "name": "Anthropic OAuth: Required Extra Usage Credits to Use Subscription",
        "flow": "external",
        "cli_command": "claude setup-token",
        "docs_url": "https://docs.claude.com/en/docs/claude-code",
    },
)


# Lanes whose models live under a DIFFERENT models.dev catalog id. Seeded from
# the map the Launcher hardcoded (hand-verified 2026-07-08) so the mapping now
# drifts with the fork that owns the lanes instead of with the client.
MODELS_DEV_LANE_IDS: dict[str, str] = {
    "openai-codex": "openai",
    "opencode-zen": "opencode",
    "xai-oauth": "xai",
    "minimax-oauth": "minimax",
    "qwen-oauth": "alibaba",
}


def models_dev_id_for(slug: str) -> str:
    """The models.dev catalog id serving ``slug``'s model list.

    Defaults to the slug itself — most lanes ARE their catalog id; only the
    hermes-specific OAuth lanes above need redirecting.
    """
    return MODELS_DEV_LANE_IDS.get(slug, slug)


def disconnect_command_for(slug: str, flow: str) -> str | None:
    """The documented command that clears an EXTERNAL provider's credentials.

    External providers store credentials outside Hermes, so Hermes never
    deletes them behind a silent API call — it hands the operator the exact
    command instead. Returns None for providers we cannot safely clear (the UI
    shows a manual hint) and for every non-external flow.

    Claude Code has no scriptable logout (only the interactive ``/logout``), so
    the command removes the same two sources ``read_claude_code_credentials()``
    consults.
    """
    if flow != "external":
        return None
    if slug == "claude-code":
        import sys as _sys

        rm_file = "rm -f ~/.claude/.credentials.json"
        if _sys.platform == "darwin":
            return (
                'security delete-generic-password -s "Claude Code-credentials" '
                f"2>/dev/null; {rm_file}"
            )
        return rm_file
    return None


def provider_login_catalog() -> list[dict]:
    """Every CONNECTABLE provider, whether or not a credential exists for it.

    This is the block that makes "never configured" representable. A client
    that builds its provider roster from credentials-present + logins-present
    (which is what every hermes client did before this existed) can render
    "this key is dead" but literally cannot render "you could connect this" —
    the two states collapse into the same absence-of-models.

    MEMBERSHIP is the union of the whole ``provider_catalog()`` universe (the
    set ``hermes model`` offers) and [OAUTH_FLOW_OVERRIDES] — the latter adds
    the two synthetic sign-in rows that are not catalog providers. Order:
    catalog order first, then any override-only rows.

    Each row: ``{id, name, flows, key_var, models_dev_id, docs_url,
    disconnect_command}``. NEVER a credential value — this block describes what
    CAN be connected, never what is stored.
    """
    overrides = {row["id"]: row for row in OAUTH_FLOW_OVERRIDES}
    rows: list[dict] = []
    seen: set[str] = set()

    def _emit(
        slug: str, name: str, default_flow: str, key_var: str | None, docs_url: str
    ) -> None:
        if slug in seen:
            return
        seen.add(slug)
        override = overrides.get(slug)
        flow = (override or {}).get("flow") or default_flow
        # `flows` is a LIST because a lane can legitimately offer more than one
        # entrance — anthropic takes a pasted API key OR the PKCE sign-in — and
        # a client that only knows one of them offers a dead affordance.
        flows = [flow]
        if key_var and flow != "api_key":
            flows.append("api_key")
        rows.append(
            {
                "id": slug,
                "name": (override or {}).get("name") or name or slug,
                "flows": flows,
                "key_var": key_var,
                "models_dev_id": models_dev_id_for(slug),
                "docs_url": (override or {}).get("docs_url") or docs_url or None,
                "disconnect_command": disconnect_command_for(slug, flow),
            }
        )

    for descriptor in provider_catalog():
        key_var = (
            descriptor.api_key_env_vars[0] if descriptor.api_key_env_vars else None
        )
        _emit(
            descriptor.slug,
            descriptor.label,
            _default_flow_for(descriptor.auth_type),
            key_var,
            descriptor.signup_url,
        )
    for row in OAUTH_FLOW_OVERRIDES:
        _emit(row["id"], row["name"], row["flow"], None, row["docs_url"])
    return rows


def _default_flow_for(auth_type: str) -> str:
    """Login shape implied by a provider's ``auth_type`` when no override row
    names one explicitly."""
    if auth_type == "oauth_device_code":
        return "device_code"
    if auth_type in {"oauth_external", "external_process", "copilot"}:
        return "external"
    if auth_type.startswith("oauth"):
        return "pkce"
    return "api_key"
