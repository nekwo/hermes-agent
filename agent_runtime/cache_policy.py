"""Single authority for a session's prompt-cache policy at the snapshot boundary.

The Launcher renders a cache-freshness indicator (is my next turn likely to hit
cache, and roughly how long until it goes cold?). That is only honest if the
Harness — which owns the caching behaviour — tells it two things per session:

* the **mode** — do we get caching through explicit Anthropic-style
  ``cache_control`` markers (a contract we control), automatic server-side
  prefix caching (OpenAI / codex / Kimi / DeepSeek / Qwen — no contract, only a
  documented inactivity window), or none at all; and
* the **TTL and its basis** — a real, contractual expiry we can count down to,
  versus an *estimated* warm window the Launcher must label as an estimate.

This module is the ONE place that classification happens. It reuses the existing
``anthropic_prompt_cache_policy`` authority for "does this provider use
cache_control" rather than re-deriving it, so the countdown can never disagree
with what the agent actually sends on the wire.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional

# OpenAI-wire model/provider families that get automatic server-side prefix
# caching without cache_control markers. OpenAI clears these prefixes after
# roughly 5-10 minutes of inactivity (and always within ~1h) and never reports
# an exact expiry — hence a conservative 5-minute warm window and an "estimated"
# basis the Launcher surfaces as such.
_AUTOMATIC_PREFIX_TOKENS = (
    "openai",
    "codex",
    "gpt-",
    "o1",
    "o3",
    "o4",
    "kimi",
    "moonshot",
    "deepseek",
    "qwen",
    "glm",
    "zhipu",
    "grok",
    "xai",
)
_AUTOMATIC_API_MODES = {"codex", "responses", "openai_responses", "chat_completions"}

# OpenAI documents automatic prefix caches as surviving ~5-10 min of inactivity;
# take the conservative lower bound as the "still warm" window.
_ESTIMATED_WARM_TTL_SECONDS = 300
# Anthropic cache_control tiers (config ``prompt_caching.cache_ttl``).
_CONTRACTUAL_TTL_SECONDS = {"5m": 300, "1h": 3600}

CACHE_MODE_EXPLICIT = "explicit_control"
CACHE_MODE_AUTOMATIC = "automatic_prefix"
CACHE_MODE_NONE = "none"


@dataclass(frozen=True)
class CachePolicy:
    """Resolved cache policy for one session's effective provider/model."""

    mode: str
    ttl_seconds: Optional[int]
    ttl_basis: Optional[str]  # "contractual" | "estimated" | None

    def as_snapshot_fields(self) -> dict[str, Any]:
        """Launcher contract keys. Always present so the client can distinguish
        "no caching here" (mode=none) from "field absent / older snapshot"."""
        return {
            "cache_mode": self.mode,
            "cache_ttl_seconds": self.ttl_seconds,
            "cache_ttl_basis": self.ttl_basis,
        }


_NONE_POLICY = CachePolicy(CACHE_MODE_NONE, None, None)


def _contractual_ttl_seconds() -> int:
    try:
        from hermes_cli.config import load_config

        ttl = (load_config().get("prompt_caching", {}) or {}).get("cache_ttl", "5m")
    except Exception:
        ttl = "5m"
    return _CONTRACTUAL_TTL_SECONDS.get(ttl, 300)


def resolve_cache_policy(
    *,
    provider: Optional[str],
    model: Optional[str],
    api_mode: Optional[str] = None,
    base_url: Optional[str] = None,
) -> CachePolicy:
    """Classify the cache policy for a session's effective provider/model.

    ``explicit_control`` (contractual TTL) is decided by the existing
    ``anthropic_prompt_cache_policy`` authority; everything else falls through
    to the automatic-prefix family check, then to ``none``.
    """
    prov = (provider or "").strip()
    mdl = (model or "").strip()
    if not prov and not mdl:
        return _NONE_POLICY

    try:
        from agent.agent_runtime_helpers import anthropic_prompt_cache_policy

        # Pass every input explicitly; the shim satisfies the ``agent`` fallback
        # positional without a live agent so the classifier is pure here.
        should_cache, _ = anthropic_prompt_cache_policy(
            SimpleNamespace(
                provider=prov, base_url=base_url or "", api_mode=api_mode or "", model=mdl
            ),
            provider=prov,
            base_url=base_url or "",
            api_mode=api_mode or "",
            model=mdl,
        )
    except Exception:
        should_cache = False

    if should_cache:
        return CachePolicy(CACHE_MODE_EXPLICIT, _contractual_ttl_seconds(), "contractual")

    haystack = f"{prov.lower()} {mdl.lower()} {(api_mode or '').lower()}"
    if (api_mode or "").lower() in _AUTOMATIC_API_MODES or any(
        token in haystack for token in _AUTOMATIC_PREFIX_TOKENS
    ):
        return CachePolicy(CACHE_MODE_AUTOMATIC, _ESTIMATED_WARM_TTL_SECONDS, "estimated")

    return _NONE_POLICY
