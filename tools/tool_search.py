"""Progressive tool disclosure ("tool search") for Hermes Agent.

When enabled, MCP and non-core plugin tools are replaced in the model-visible
tools array by three bridge tools — ``tool_search``, ``tool_describe``,
``tool_call`` — and surfaced on demand. Core Hermes tools never defer.

Design constraints this module is built around (see ``openclaw-tool-search-report``
for the full rationale):

* Core tools defined in ``toolsets._HERMES_CORE_TOOLS`` are *never* deferred.
  Always-load means always-load. No exceptions.
* Tiered disclosure (July 2026 plan): the moment ANY deferrable (MCP/plugin)
  tools are present, they hide behind the bridge. What scales with catalog
  size is the *listing*, not the activation decision:
    - Tier 0 — no MCP/plugin tools: pure passthrough, everything eager.
    - Tier 1 — deferred tools whose catalog listing fits the listing budget
      (``min(threshold_pct`` of context — default 5% — ``, listing_max_tokens)``):
      bridge + skills-style listing (name + short description per tool),
      degrading to a names-only listing when the full form is over budget.
    - Tier 2 — per-tool listing over budget even names-only (e.g.
      Cloudflare's flat API surface, ~3,300 tools whose names alone are
      ~32K tokens): bare bridge + a one-line-per-server summary (server
      name + tool count) so the model still knows WHICH domains are
      reachable; individual tools are discoverable only via ``tool_search``.
* The catalog is stateless across turns and tools-array assemblies. It is
  rebuilt from the current tool-defs list every time. This is the lesson
  from OpenClaw's cron regression (openclaw/openclaw#84141): a session-keyed
  catalog that drifts out of sync with the live tool registry produces
  silent tool dropouts.
* Bridge tools route through ``model_tools.handle_function_call`` exactly
  like a direct call, so guardrails, plugin pre/post hooks, approval flows,
  and tool-result truncation all fire identically.
* Display and trajectory unwrap is implemented here so the user (CLI activity
  feed, gateway, saved trajectories) always sees the underlying tool, not
  the bridge.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from tools.registry import tool_error

logger = logging.getLogger("tools.tool_search")


# Bridge tool names. These names are reserved and may not collide with a
# user/plugin/MCP tool — registration of any tool with these names is
# rejected by the registry's existing override-protection logic.
TOOL_SEARCH_NAME = "tool_search"
TOOL_DESCRIBE_NAME = "tool_describe"
TOOL_CALL_NAME = "tool_call"

BRIDGE_TOOL_NAMES = frozenset({TOOL_SEARCH_NAME, TOOL_DESCRIBE_NAME, TOOL_CALL_NAME})

# When estimating tokens from char count without a real tokenizer, this is
# the cheap rule of thumb that's stable across providers. Roughly 4 chars
# per token for English+JSON. Underestimating leads to false negatives
# (tool search not activated when it should); overestimating leads to false
# positives (activated when not needed). 4.0 errs slightly toward
# underestimating, which is the safer default.
CHARS_PER_TOKEN = 4.0


# ---------------------------------------------------------------------------
# Configuration plumbing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSearchConfig:
    """Resolved, validated tool-search configuration for a single assembly."""

    enabled: str  # "auto" | "on" | "off"
    # Listing budget as a percentage of the model's context window. Under
    # tiered disclosure this no longer gates *activation* (any deferrable
    # tool activates the bridge) — it bounds how much context the embedded
    # catalog listing may consume before disclosure degrades:
    # full listing -> names-only -> bare bridge (tier 2).
    threshold_pct: float  # 0..100
    search_default_limit: int
    max_search_limit: int
    # Catalog listing ("skills-style" progressive disclosure): when active,
    # a grouped name + short-description manifest of every deferred tool is
    # embedded in the tool_search bridge description, so capabilities stay
    # DISCOVERABLE (like the skills listing in the system prompt) while full
    # schemas stay deferred.  "auto" = include when it fits the listing
    # budget (falls back to names-only, then to none = bare bridge);
    # "on" = same rendering, explicit intent; "off" = always bare bridge.
    listing: str = "auto"  # "auto" | "on" | "off"
    # Absolute cap on the embedded listing, regardless of context size.
    # Effective budget = min(listing_max_tokens, threshold_pct% of context).
    listing_max_tokens: int = 20000
    # Operator-extended never-defer tool names; unions with, never replaces,
    # ``_NEVER_DEFER_TOOLS``. A tuple (not a list) so the frozen dataclass
    # stays hashable.
    never_defer: tuple[str, ...] = ()

    @classmethod
    def from_raw(cls, raw: Any) -> "ToolSearchConfig":
        """Build a config from a raw dict / bool / None.

        Accepts the legacy bool shape (``tools.tool_search: true``) and the
        dict shape (``tools.tool_search: {enabled: auto, ...}``). Validates
        and clamps every numeric field; unknown values fall back to safe
        defaults rather than raising, so a typo in user config does not
        break the agent.
        """
        if raw is True:
            return cls(enabled="auto", threshold_pct=5.0,
                       search_default_limit=5, max_search_limit=20)
        if raw is False:
            return cls(enabled="off", threshold_pct=5.0,
                       search_default_limit=5, max_search_limit=20)
        if not isinstance(raw, dict):
            return cls(enabled="auto", threshold_pct=5.0,
                       search_default_limit=5, max_search_limit=20)

        enabled_raw = str(raw.get("enabled", "auto")).strip().lower()
        if enabled_raw in ("true", "1", "yes"):
            enabled = "on"
        elif enabled_raw in ("false", "0", "no"):
            enabled = "off"
        elif enabled_raw in ("auto", "on", "off"):
            enabled = enabled_raw
        else:
            enabled = "auto"

        threshold_pct = _safe_float(raw.get("threshold_pct"), 5.0)
        threshold_pct = max(0.0, min(100.0, threshold_pct))

        max_search_limit = max(1, min(50, _safe_int(raw.get("max_search_limit"), 20)))
        search_default_limit = max(1, min(max_search_limit,
                                          _safe_int(raw.get("search_default_limit"), 5)))

        listing_raw = str(raw.get("listing", "auto")).strip().lower()
        if listing_raw in ("true", "1", "yes"):
            listing = "on"
        elif listing_raw in ("false", "0", "no"):
            listing = "off"
        elif listing_raw in ("auto", "on", "off"):
            listing = listing_raw
        else:
            listing = "auto"
        listing_max_tokens = max(200, min(60000, _safe_int(raw.get("listing_max_tokens"), 20000)))

        # Operator extension to the hardcoded never-defer set. Anything that
        # isn't a list/tuple of names falls back to "no extension" rather than
        # raising — same convention as every other field here.
        never_defer_raw = raw.get("never_defer")
        never_defer_names: set[str] = set()
        if isinstance(never_defer_raw, (list, tuple)):
            for item in never_defer_raw:
                try:
                    cleaned = str(item).strip()
                except Exception:
                    continue
                if not cleaned or cleaned in BRIDGE_TOOL_NAMES:
                    continue
                never_defer_names.add(cleaned)
        never_defer = tuple(sorted(never_defer_names))

        return cls(
            enabled=enabled,
            threshold_pct=threshold_pct,
            search_default_limit=search_default_limit,
            max_search_limit=max_search_limit,
            listing=listing,
            listing_max_tokens=listing_max_tokens,
            never_defer=never_defer,
        )


def _safe_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _safe_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def load_config() -> ToolSearchConfig:
    """Load tool-search config from the user config file."""
    try:
        from hermes_cli.config import load_config as _load
        cfg = _load() or {}
        tools_cfg = cfg.get("tools") if isinstance(cfg.get("tools"), dict) else {}
        if not isinstance(tools_cfg, dict):
            tools_cfg = {}
        return ToolSearchConfig.from_raw(tools_cfg.get("tool_search"))
    except Exception as e:
        logger.debug("Failed to load tool-search config: %s", e)
        return ToolSearchConfig.from_raw(None)


# ---------------------------------------------------------------------------
# Tool classification
# ---------------------------------------------------------------------------


def _core_tool_names() -> frozenset[str]:
    """Return the set of tool names that must NEVER be deferred.

    Imported lazily because ``toolsets`` imports from ``tools.registry``
    and we don't want a hard cycle.
    """
    try:
        from toolsets import _HERMES_CORE_TOOLS
        return frozenset(_HERMES_CORE_TOOLS)
    except Exception:
        return frozenset()


# Fork-owned never-defer promotions (operator ruling 2026-08-23: "agent message
# send should be a first class tool"). These are NOT core tools — putting them
# in toolsets._HERMES_CORE_TOOLS would GRANT them to every platform bundle
# (toolsets.py:464+). This set only un-hides: a tool still has to be granted by
# the lane's toolset resolution before classify_tools ever sees its def.
# send + dispatches (the wait=false read-back sibling) ride eagerly; the three
# read-side siblings (threads/open/log_path) stay deferred by design.
_NEVER_DEFER_TOOLS = frozenset({"agent_chat_send", "agent_chat_dispatches"})


def never_defer_tool_names(config: Optional[ToolSearchConfig] = None) -> frozenset[str]:
    """Hardcoded promotions unioned with the operator's config extension.

    Config EXTENDS the set; nothing in config can remove a hardcoded name.
    """
    if config is None:
        config = load_config()  # never raises
    if not config.never_defer:
        return _NEVER_DEFER_TOOLS
    return _NEVER_DEFER_TOOLS | frozenset(config.never_defer)


def is_deferrable_tool_name(name: str, *,
                            config: Optional[ToolSearchConfig] = None) -> bool:
    """Return True if a tool with this name is *eligible* for deferral.

    A tool is deferrable iff it is registered with an MCP toolset prefix
    OR it is not in ``_HERMES_CORE_TOOLS``. Core tools are never deferred
    even when their toolset is technically plugin-provided (this protects
    against accidental shadowing). Names in ``never_defer_tool_names`` are
    promoted the same way without being granted anywhere new.
    """
    if name in BRIDGE_TOOL_NAMES:
        return False
    if name in _core_tool_names():
        return False
    if name in never_defer_tool_names(config):
        return False
    # Check registry toolset for MCP prefix.
    try:
        from tools.registry import registry
        entry = registry.get_entry(name)
        if entry is None:
            return False
        if entry.toolset.startswith("mcp-"):
            return True
        # Non-MCP, non-core → plugin tool, eligible.
        return True
    except Exception:
        return False


def classify_tools(tool_defs: List[Dict[str, Any]], *,
                   config: Optional[ToolSearchConfig] = None
                   ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split a tool-defs list into (visible, deferrable).

    ``visible`` retains every tool that must stay in the model-facing array:
    every core tool, plus any tool we can't classify. ``deferrable`` is the
    candidate set for catalog entry.

    This only ever *partitions* the defs it is handed — it never introduces a
    def the caller did not already produce. Never-defer promotion therefore
    un-hides, and can never grant.
    """
    if config is None:
        config = load_config()
    visible: List[Dict[str, Any]] = []
    deferrable: List[Dict[str, Any]] = []
    for td in tool_defs:
        fn = td.get("function") or {}
        name = fn.get("name", "")
        if name in BRIDGE_TOOL_NAMES:
            # Should never happen — bridge tools are added after classification —
            # but be defensive.
            continue
        if is_deferrable_tool_name(name, config=config):
            deferrable.append(td)
        else:
            visible.append(td)
    return visible, deferrable


# ---------------------------------------------------------------------------
# Token estimation and threshold gate
# ---------------------------------------------------------------------------


def estimate_tokens_from_schemas(tool_defs: Iterable[Dict[str, Any]]) -> int:
    """Estimate the token cost of a tool-defs list via the chars/4 rule.

    Cheap and stable across providers. The number doesn't need to be exact —
    it gates the activate/skip decision, and a typical 200K context with a
    10% threshold means the decision flips around 20K tokens of schema.
    Order-of-magnitude precision is fine.
    """
    total_chars = 0
    for td in tool_defs:
        try:
            total_chars += len(json.dumps(td, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError):
            total_chars += len(str(td))
    return int(math.ceil(total_chars / CHARS_PER_TOKEN))


def should_activate(
    config: ToolSearchConfig,
    deferrable_tokens: int,
    context_length: Optional[int],
) -> bool:
    """Decide whether tool search should activate for the current assembly.

    ``"off"`` skips unconditionally. ``"on"`` and ``"auto"`` activate whenever
    at least one deferrable tool exists (there's no point swapping a no-op).

    Tiered-disclosure semantics (July 2026): the presence of ANY MCP/plugin
    tool activates the bridge — schemas always defer. What the threshold now
    controls is the *listing budget* (see :func:`listing_token_budget`), not
    activation. ``context_length`` is retained in the signature for
    backward compatibility with existing callers.
    """
    if config.enabled == "off":
        return False
    if deferrable_tokens <= 0:
        return False
    return True


def listing_token_budget(
    config: ToolSearchConfig,
    context_length: Optional[int],
) -> int:
    """Effective token budget for the embedded catalog listing.

    ``min(listing_max_tokens, threshold_pct% of context)``. Without a known
    context size, the percentage leg falls back to a fixed 10K cutoff
    (5% of a typical 200K window).
    """
    if context_length and context_length > 0:
        pct_leg = int(context_length * (config.threshold_pct / 100.0))
    else:
        pct_leg = 10_000
    return max(0, min(config.listing_max_tokens, pct_leg))


# ---------------------------------------------------------------------------
# Catalog + BM25 retrieval
# ---------------------------------------------------------------------------


@dataclass
class CatalogEntry:
    """One deferrable tool, in a form the bridge tools can search and serve."""

    name: str
    description: str
    schema: Dict[str, Any]  # The full {"type":"function", "function": {...}} entry.
    source: str  # "mcp" | "plugin" | "other"
    source_name: str  # Toolset name, e.g. "mcp-github" or "kanban"

    # Pre-tokenized fields for BM25.
    _tokens: List[str] = field(default_factory=list)


_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _tokenize(text: str) -> List[str]:
    if not text:
        return []
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _entry_search_text(td: Dict[str, Any]) -> str:
    """Build the search-text blob for a deferrable tool.

    Includes the tool name (with underscores broken into words so BM25 can
    match against query terms), the description, and the names of the
    top-level parameters. Schema bodies are deliberately excluded —
    indexing them adds noise without improving recall in our measurement.
    """
    fn = td.get("function") or {}
    name = fn.get("name", "")
    desc = fn.get("description", "") or ""
    params = ((fn.get("parameters") or {}).get("properties") or {})
    param_names = " ".join(params.keys())
    # Break snake_case and dotted names into words for BM25.
    name_words = name.replace("_", " ").replace(".", " ").replace("-", " ").replace(":", " ")
    return f"{name_words} {desc} {param_names}"


def _classify_source(name: str) -> Tuple[str, str]:
    """Return (source_kind, source_name) for a registered tool name."""
    try:
        from tools.registry import registry
        entry = registry.get_entry(name)
        if entry is None:
            return ("other", "")
        if entry.toolset.startswith("mcp-"):
            return ("mcp", entry.toolset)
        return ("plugin", entry.toolset)
    except Exception:
        return ("other", "")


def build_catalog(tool_defs: List[Dict[str, Any]]) -> List[CatalogEntry]:
    """Build the deferred-tool catalog from a tool-defs list.

    Caller is expected to pass only the deferrable subset (``classify_tools``
    returns it as the second element).
    """
    catalog: List[CatalogEntry] = []
    for td in tool_defs:
        fn = td.get("function") or {}
        name = fn.get("name", "")
        if not name:
            continue
        desc = fn.get("description", "") or ""
        source, source_name = _classify_source(name)
        entry = CatalogEntry(
            name=name,
            description=desc,
            schema=td,
            source=source,
            source_name=source_name,
            _tokens=_tokenize(_entry_search_text(td)),
        )
        catalog.append(entry)
    return catalog


def _bm25_score(query_tokens: List[str], doc_tokens: List[str],
                doc_lengths: List[int], avg_dl: float,
                doc_freq: Dict[str, int], n_docs: int,
                k1: float = 1.5, b: float = 0.75) -> float:
    """Standard BM25 score for one query against one document.

    Inlined small implementation rather than adding a dependency. Performance
    is fine — the catalog is bounded by N (tools) typically < 500, and we
    score against the in-memory tokens list.
    """
    if not doc_tokens:
        return 0.0
    score = 0.0
    dl = len(doc_tokens)
    # Pre-count tokens in the doc.
    doc_tf: Dict[str, int] = {}
    for t in doc_tokens:
        doc_tf[t] = doc_tf.get(t, 0) + 1
    for q in query_tokens:
        df = doc_freq.get(q, 0)
        if df == 0:
            continue
        idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
        tf = doc_tf.get(q, 0)
        if tf == 0:
            continue
        norm = tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / max(avg_dl, 1.0)))
        score += idf * norm
    return score


def search_catalog(catalog: List[CatalogEntry], query: str, limit: int = 5) -> List[CatalogEntry]:
    """Return the top-``limit`` catalog entries for ``query`` by BM25.

    Falls back to a stable name-substring match when BM25 yields no hits
    above zero. That ensures a query like ``"github"`` against a catalog
    where every tool is named ``github_*`` still returns results — BM25
    can underperform when query and document share only one token that
    appears in every document (zero IDF).
    """
    if not catalog or limit <= 0:
        return []
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    # Precompute doc statistics.
    doc_lengths = [len(e._tokens) for e in catalog]
    avg_dl = sum(doc_lengths) / max(len(doc_lengths), 1)
    doc_freq: Dict[str, int] = {}
    for e in catalog:
        seen = set(e._tokens)
        for t in seen:
            doc_freq[t] = doc_freq.get(t, 0) + 1
    n_docs = len(catalog)

    scored: List[Tuple[float, CatalogEntry]] = []
    for entry in catalog:
        s = _bm25_score(query_tokens, entry._tokens, doc_lengths, avg_dl,
                        doc_freq, n_docs)
        if s > 0:
            scored.append((s, entry))

    if not scored:
        # Substring fallback against the original tool name.
        ql = query.lower()
        for entry in catalog:
            if ql in entry.name.lower():
                scored.append((0.1, entry))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:limit]]


# ---------------------------------------------------------------------------
# Bridge tool schemas
# ---------------------------------------------------------------------------


_SENTENCE_END_RE = re.compile(r"[.!?\n]")


def _short_desc(description: str, max_chars: int = 60) -> str:
    """First sentence of a tool description, clipped to ``max_chars``.

    Mirrors the skills-listing convention: one terse line per capability.
    Whitespace is collapsed; a hard clip never cuts mid-word unless the
    first word itself exceeds the budget.
    """
    text = " ".join((description or "").split())
    if not text:
        return ""
    m = _SENTENCE_END_RE.search(text)
    if m:
        text = text[:m.start() + (1 if text[m.start()] == "." else 0)]
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars]
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return clipped.rstrip(",;: ") + "…"


def _listing_group_label(source_name: str) -> str:
    """Human-facing group heading for a toolset, e.g. ``mcp-github`` -> ``github``."""
    label = source_name or "other"
    if label.startswith("mcp-"):
        label = label[4:]
    return label


def build_catalog_listing(
    deferrable: List[Dict[str, Any]],
    *,
    max_tokens: int = 20000,
) -> Optional[str]:
    """Render a skills-style manifest of the deferred catalog.

    One line per tool — ``name: short description`` — grouped under a
    heading per source (MCP server / plugin toolset), exactly like the
    bundled-skills listing in the system prompt:

        github tools: (44)
        - create_issue: Open a new issue in a GitHub repository.
        - merge_pull_request: Merge an open pull request.
        ...

    Ordering is deterministic (groups and tools sorted by name) so the
    rendered block is byte-stable across assemblies of the same catalog —
    this keeps the request prefix cacheable across turns.

    Token-budget fallbacks (cheap chars/4 estimate, same rule as the
    activation gate):
      1. full listing (names + short descriptions)
      2. names-only listing, still grouped
      3. server-level summary — one line per MCP server / plugin toolset
         (name + tool count), so the model always knows WHICH domains are
         reachable through the bridge even when per-tool names don't fit
      4. ``None`` — only when the summary itself exceeds the budget
    """
    text, _form = build_catalog_listing_with_form(deferrable, max_tokens=max_tokens)
    return text


def build_catalog_listing_with_form(
    deferrable: List[Dict[str, Any]],
    *,
    max_tokens: int = 20000,
) -> Tuple[Optional[str], str]:
    """Like :func:`build_catalog_listing` but also reports the form used.

    Returns ``(text, form)`` where ``form`` is ``"full"`` (names + short
    descriptions), ``"names"`` (names-only fallback), ``"mixed"`` (per-server
    degradation: small servers keep per-tool lines, oversized servers
    collapse to a name + tool-count summary line), ``"groups"`` (every
    server summarized), or ``"none"`` (over budget in every form).

    Degradation is PER SERVER, not global: one huge server (Cloudflare's
    3,320 flat tools) must not cost a small co-attached server (Linear's 24)
    its listing. Greedy fit, smallest rendered group first, is deterministic
    for a given catalog — byte-stable across assemblies, cache-safe.
    """
    if not deferrable:
        return None, "none"

    groups: Dict[str, List[Tuple[str, str]]] = {}
    for td in deferrable:
        fn = td.get("function") or {}
        name = fn.get("name", "")
        if not name:
            continue
        source, source_name = _classify_source(name)
        label = _listing_group_label(source_name if source != "other" else "other")
        groups.setdefault(label, []).append((name, _short_desc(fn.get("description", ""))))

    if not groups:
        return None, "none"

    def render_group(label: str, mode: str) -> str:
        """Render one server's block. mode: 'full' | 'names' | 'summary'."""
        tools = sorted(groups[label])
        if mode == "summary":
            return (f"{label} ({len(tools)} tools — names not listed; "
                    f"discover via `{TOOL_SEARCH_NAME}`)")
        lines = [f"{label} tools ({len(tools)}):"]
        if mode == "full":
            for name, desc in tools:
                lines.append(f"- {name}: {desc}" if desc else f"- {name}")
        else:
            lines.append(", ".join(name for name, _ in tools))
        return "\n".join(lines)

    header = ("Deferred tool catalog (call schemas via "
              f"`{TOOL_DESCRIBE_NAME}`, invoke via `{TOOL_CALL_NAME}`):")

    def assemble(modes: Dict[str, str]) -> str:
        return "\n".join([header] + [render_group(lbl, modes[lbl])
                                     for lbl in sorted(groups)])

    def fits(text: str) -> bool:
        return math.ceil(len(text) / CHARS_PER_TOKEN) <= max_tokens

    # 1. Everything full.
    modes = {lbl: "full" for lbl in groups}
    if fits(assemble(modes)):
        return assemble(modes), "full"

    # 2. Everything names-only.
    modes = {lbl: "names" for lbl in groups}
    if fits(assemble(modes)):
        return assemble(modes), "names"

    # 3. Per-server degradation: collapse the LARGEST rendered groups to
    #    summary lines first, keeping per-tool names for small servers.
    #    Deterministic: size then label. One oversized server (Cloudflare)
    #    must not cost a small co-attached server (Linear) its listing.
    by_size = sorted(groups, key=lambda lbl: (-len(render_group(lbl, "names")), lbl))
    for lbl in by_size:
        modes[lbl] = "summary"
        if fits(assemble(modes)):
            form = "groups" if all(m == "summary" for m in modes.values()) else "mixed"
            return assemble(modes), form

    # 4. Even the all-summary form is over budget.
    return None, "none"


def bridge_tool_schemas(
    deferred_count: int,
    listing: Optional[str] = None,
    listing_form: str = "",
) -> List[Dict[str, Any]]:
    """Build the bridge tool schemas to inject in place of deferred tools.

    The schemas are intentionally short — every byte added here is a byte
    the user pays on every turn. Descriptions are tuned to be unambiguous
    about the call sequence the model should follow.

    When ``listing`` is provided (see :func:`build_catalog_listing`), it is
    embedded in the ``tool_search`` description so every deferred capability
    stays *visible* by name — the skills-listing pattern — closing the
    "model doesn't know what it doesn't know" gap while full parameter
    schemas remain deferred. ``listing_form`` selects the framing: per-tool
    forms ("full"/"names") tell the model it may skip the search when it
    sees the exact name; the server-summary form ("groups") tells it which
    DOMAINS are reachable and that search is mandatory for tool discovery.
    """
    desc_search = (
        f"Search {deferred_count} additional tools that are loaded on demand. "
        "Returns up to ``limit`` matches with name, description, and — for the "
        "top matches — the full `parameters` schema. When a match includes "
        f"`parameters`, invoke it directly with `{TOOL_CALL_NAME}`; "
        f"`{TOOL_DESCRIBE_NAME}` is only needed for matches without one. "
        "Tools listed at the top of this "
        "system prompt are already available and do not need to be searched."
    )
    if listing and listing_form == "groups":
        desc_search += (
            "\n\nThe servers below are connected and their tools ARE available "
            "through this bridge. For any request in these domains, search "
            "here FIRST — do not claim the capability is unavailable and do "
            "not substitute a generic tool (terminal/browser) without "
            "searching.\n\n" + listing
        )
    elif listing:
        desc_search += (
            "\n\nEvery deferred capability is listed below. If a tool name "
            "appears here, do NOT claim it is unavailable — load it with "
            f"`{TOOL_DESCRIBE_NAME}` (skip `{TOOL_SEARCH_NAME}` when you "
            "already see the exact name)."
        )
        if listing_form == "mixed":
            desc_search += (
                " For servers marked 'names not listed', the tools exist "
                f"too — find them with `{TOOL_SEARCH_NAME}` before "
                "concluding anything is missing."
            )
        desc_search += "\n\n" + listing
    desc_describe = (
        f"Load the full JSON schema for one tool returned by `{TOOL_SEARCH_NAME}`. "
        f"Required before `{TOOL_CALL_NAME}` if the tool's parameters are unknown."
    )
    desc_call = (
        "Invoke a deferred tool by name with the given arguments. Argument shape "
        f"matches the tool's schema (see `{TOOL_DESCRIBE_NAME}`). Policy, hooks, "
        "and approvals run exactly as for any directly-listed tool."
    )

    return [
        {
            "type": "function",
            "function": {
                "name": TOOL_SEARCH_NAME,
                "description": desc_search,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Keywords describing the capability you need (e.g. 'create github issue').",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of results to return. Default 5.",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": TOOL_DESCRIBE_NAME,
                "description": desc_describe,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Exact tool name (as returned by tool_search).",
                        },
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": TOOL_CALL_NAME,
                "description": desc_call,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Exact tool name to invoke.",
                        },
                        "arguments": {
                            "type": "object",
                            "description": "Arguments for the tool, matching its schema.",
                        },
                    },
                    "required": ["name", "arguments"],
                },
            },
        },
    ]


# ---------------------------------------------------------------------------
# Standalone tool_describe (details-on-demand, independent of deferral)
# ---------------------------------------------------------------------------
#
# T6b (Context Cost Workstream, 2026-07-18): the core tool schemas ship BRIEF
# descriptions to cut the per-call schema cost; the full documentation stays
# reachable via ``tool_describe``. That means ``tool_describe`` must be present
# in every resolved lane even when tool-search deferral is NOT active (it is
# active only when the deferrable surface crosses the threshold). The schema
# below is fixed and tiny — it rides the wire on every call, so it is kept as
# small as the three-field bridge schema.


def tool_describe_schema() -> Dict[str, Any]:
    """Return the fixed, always-available ``tool_describe`` schema.

    Injected into every resolved lane by ``model_tools`` independent of
    tool-search deferral so the model can always pull a brief-trimmed tool's
    full documentation. Bridge dispatch in ``model_tools.handle_function_call``
    routes it (``is_bridge_tool`` already recognizes the name), and
    ``dispatch_tool_describe`` serves the full registry-held docs + live
    parameter schema.
    """
    return {
        "type": "function",
        "function": {
            "name": TOOL_DESCRIBE_NAME,
            "description": (
                "Load a tool's full documentation and parameter reference by "
                "name. Tool descriptions in this list are brief; call "
                "tool_describe before the first use of an unfamiliar tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Exact tool name to describe.",
                    },
                },
                "required": ["name"],
            },
        },
    }


def ensure_tool_describe_present(tool_defs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ensure ``tool_describe`` is in the model-facing tool list.

    No-op when it is already present (e.g. tool-search deferral already injected
    the full bridge trio, which includes ``tool_describe``). Otherwise appends
    the fixed standalone schema. Returns a new list; never mutates the input.
    """
    for td in tool_defs:
        if (td.get("function") or {}).get("name") == TOOL_DESCRIBE_NAME:
            return tool_defs
    return list(tool_defs) + [tool_describe_schema()]


# ---------------------------------------------------------------------------
# Public entry point: assemble tool-defs with optional tool search
# ---------------------------------------------------------------------------


@dataclass
class AssemblyResult:
    """Outcome of one assembly. Useful for tests and observability."""

    tool_defs: List[Dict[str, Any]]
    activated: bool
    deferred_count: int = 0
    deferred_tokens: int = 0
    threshold_tokens: int = 0
    # Disclosure tier actually applied:
    #   0 = passthrough (no deferrable tools, or tool_search off)
    #   1 = bridge + catalog listing (full or names-only)
    #   2 = bare bridge — catalog too large for any listing form
    tier: int = 0
    listing_form: str = "none"  # "full" | "names" | "none"


def assemble_tool_defs(
    tool_defs: List[Dict[str, Any]],
    *,
    context_length: Optional[int] = None,
    config: Optional[ToolSearchConfig] = None,
) -> AssemblyResult:
    """Return the tool-defs list the model should actually see.

    When tool search is inactive (off, no deferrable tools, or below
    threshold), this is a passthrough. When active, MCP and plugin tools
    are stripped from the visible list and replaced with the three bridge
    tools. Core tools are *never* deferred regardless of config.

    Idempotent: calling with bridge tools already in the input is a no-op
    (they classify as non-core/non-deferrable but their names are reserved,
    so they are filtered out of the deferrable set).
    """
    if config is None:
        config = load_config()

    # Defensive: strip any bridge tools that may already be in the list
    # (e.g. someone called assemble twice).
    incoming = [td for td in tool_defs
                if (td.get("function") or {}).get("name") not in BRIDGE_TOOL_NAMES]

    visible, deferrable = classify_tools(incoming, config=config)
    if not deferrable:
        return AssemblyResult(tool_defs=incoming, activated=False)

    deferrable_tokens = estimate_tokens_from_schemas(deferrable)
    if not should_activate(config, deferrable_tokens, context_length):
        return AssemblyResult(
            tool_defs=incoming,
            activated=False,
            deferred_count=len(deferrable),
            deferred_tokens=deferrable_tokens,
            threshold_tokens=int((context_length or 0) * (config.threshold_pct / 100.0)),
            tier=0,
        )

    listing = None
    listing_form = "none"
    listing_budget = listing_token_budget(config, context_length)
    if config.listing != "off":
        listing, listing_form = build_catalog_listing_with_form(
            deferrable, max_tokens=listing_budget)
    bridge = bridge_tool_schemas(len(deferrable), listing=listing,
                                 listing_form=listing_form)
    result = visible + bridge
    # Tier 1 = per-tool listing for at least part of the catalog (full,
    # names, or mixed). Tier 2 = search-only discovery; the server-level
    # "groups" summary keeps domains visible but individual tools are only
    # reachable via tool_search.
    tier = 1 if listing_form in ("full", "names", "mixed") else 2

    logger.info(
        "tool_search activated (tier %d): %d core/visible tools kept, %d deferred "
        "(~%d tokens), listing %s (budget ~%d tokens)",
        tier, len(visible), len(deferrable), deferrable_tokens,
        listing_form, listing_budget,
    )

    return AssemblyResult(
        tool_defs=result,
        activated=True,
        deferred_count=len(deferrable),
        deferred_tokens=deferrable_tokens,
        threshold_tokens=listing_budget,
        tier=tier,
        listing_form=listing_form,
    )


# ---------------------------------------------------------------------------
# Bridge tool dispatch
# ---------------------------------------------------------------------------


def is_bridge_tool(name: str) -> bool:
    return name in BRIDGE_TOOL_NAMES


# How many search hits carry their full ``parameters`` schema inline. The
# describe round-trip after a search is the single most common wasted model
# iteration in the bridge lane (~3-6 s), and the model almost always invokes
# one of the first few hits — so pay a few hundred tokens on the top matches
# and let it skip describe entirely.
_SEARCH_HIT_SCHEMA_TOP_N = 3
# Per-hit cap on ``json.dumps(parameters)``. A healthy tool's schema is a few
# thousand chars (agent_chat_send is ~3.5 K); this keeps one pathological MCP
# schema from blowing up the whole search result. Over the cap, the hit is
# served schema-less and the model falls back to ``tool_describe``.
_SEARCH_HIT_SCHEMA_MAX_CHARS = 6_000


def _format_search_hit(entry: CatalogEntry, *,
                       include_parameters: bool = False) -> Dict[str, Any]:
    hit: Dict[str, Any] = {
        "name": entry.name,
        "source": entry.source,
        "source_name": entry.source_name,
        # Cap description so a chatty MCP server doesn't blow up the result.
        "description": (entry.description or "")[:400],
    }
    if include_parameters:
        params = ((entry.schema.get("function") or {}).get("parameters")) or {}
        try:
            if len(json.dumps(params, ensure_ascii=False)) <= _SEARCH_HIT_SCHEMA_MAX_CHARS:
                hit["parameters"] = params
        except (TypeError, ValueError):
            pass  # unserializable schema → the describe path still serves it
    return hit


def dispatch_tool_search(args: Dict[str, Any],
                         *,
                         current_tool_defs: List[Dict[str, Any]],
                         config: Optional[ToolSearchConfig] = None) -> str:
    """Execute the ``tool_search`` bridge tool. Returns a JSON string."""
    if config is None:
        config = load_config()
    query = str(args.get("query") or "").strip()
    if not query:
        return tool_error("query is required")

    raw_limit = args.get("limit")
    if raw_limit is None:
        limit = config.search_default_limit
    else:
        limit = max(1, min(config.max_search_limit, _safe_int(raw_limit, config.search_default_limit)))

    _, deferrable = classify_tools(current_tool_defs, config=config)
    catalog = build_catalog(deferrable)
    hits = search_catalog(catalog, query, limit=limit)
    return json.dumps({
        "query": query,
        "total_available": len(catalog),
        "matches": [
            _format_search_hit(h, include_parameters=(i < _SEARCH_HIT_SCHEMA_TOP_N))
            for i, h in enumerate(hits)
        ],
    }, ensure_ascii=False)


def dispatch_tool_describe(args: Dict[str, Any],
                           *,
                           current_tool_defs: List[Dict[str, Any]]) -> str:
    """Execute the ``tool_describe`` bridge tool. Returns a JSON string.

    Serves the FULL documentation for any tool — core or deferrable. This is
    the details-on-demand backend for T6b brief descriptions: the wire schema
    ships a brief, and this returns the full original description (from the
    fork-owned ``tools.tool_full_descriptions`` mirror, when present) plus the
    live parameter schema off the registry (parameters are never trimmed).

    Resolution order:
      1. registry schema by name (works for every registered tool, core or
         MCP) — description falls back to the mirrored full text;
      2. the assembled deferrable defs (an MCP tool present only in
         ``current_tool_defs`` and not resolvable from the process registry).
    """
    name = str(args.get("name") or "").strip()
    if not name:
        return tool_error("name is required")

    # Full (untrimmed) description, if this tool's docs are mirrored (T6b).
    full_desc: Optional[str] = None
    try:
        from tools.tool_full_descriptions import full_tool_description
        full_desc = full_tool_description(name)
    except Exception:
        full_desc = None

    # Standalone path: any registered tool. The wire description may be a
    # trimmed brief; parameters are never trimmed, so read them live off the
    # registry schema and prefer the mirrored full description.
    try:
        from tools.registry import registry
        schema = registry.get_schema(name)
    except Exception:
        schema = None
    if schema is not None:
        return json.dumps({
            "name": name,
            "description": full_desc or schema.get("description", ""),
            "parameters": schema.get("parameters", {}),
        }, ensure_ascii=False)

    # Fallback: a deferrable tool present only in the assembled defs. Anything
    # that is neither registry-resolvable nor a known deferrable name is a
    # spelling error, not a catalog-staleness problem — say so plainly.
    if not is_deferrable_tool_name(name):
        return tool_error(
            f"'{name}' is not a deferrable tool. If you see it in the tools list "
            "already, call it directly; otherwise check the spelling against tool_search."
        )
    _, deferrable = classify_tools(current_tool_defs)
    for td in deferrable:
        fn = td.get("function") or {}
        if fn.get("name") == name:
            return json.dumps({
                "name": name,
                "description": full_desc or fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            }, ensure_ascii=False)

    return tool_error(
        f"'{name}' is not currently available. Re-run tool_search to refresh."
    )


def scoped_deferrable_names(tool_defs: List[Dict[str, Any]]) -> frozenset[str]:
    """Return the set of deferrable tool names present in ``tool_defs``.

    ``tool_defs`` is expected to be the *pre-assembly* tool list for the
    current session's toolset scope (i.e. what
    ``get_tool_definitions(skip_tool_search_assembly=True)`` returns for the
    session's enabled/disabled toolsets). The resulting set is the universe of
    tools the session may legitimately reach through ``tool_call``. Used as a
    scoping gate by both the ``model_tools`` bridge dispatch and the
    ``tool_executor`` unwrap so a restricted-toolset session can never invoke
    an out-of-scope tool via the bridge.
    """
    config = load_config()
    names: set[str] = set()
    for td in tool_defs:
        name = (td.get("function") or {}).get("name", "")
        if name and is_deferrable_tool_name(name, config=config):
            names.add(name)
    return frozenset(names)


def validate_deferred_call_args(name: str, args: Dict[str, Any]) -> Optional[str]:
    """Probe-validate ``tool_call`` arguments against the deferred tool's schema.

    A deferred tool's parameter schema is invisible to the model until it
    calls ``tool_describe`` — so models routinely invoke deferred tools
    "blind" by name alone, omitting required arguments. Dispatching such a
    call produces an opaque downstream failure (``KeyError: 'document_id'``)
    that tells the model nothing about what the tool expects, and cheap
    models loop on it until the iteration budget dies.

    Port of the describe-first probe-validation fix from nearai/ironclaw#5149:
    when required arguments are missing, return the tool's parameter schema
    instead of dispatching blind — the model repairs the call in one
    round-trip. Valid calls (and any call we can't confidently validate)
    dispatch untouched, so this can never block a legitimate invocation.

    Only *key absence* of schema-``required`` fields counts as invalid.
    No type checking, no null rejection — nullable/typed edge cases are the
    tool's own business, and ``coerce_tool_args`` already handles type repair
    downstream. Returns a JSON error string when invalid, ``None`` when the
    call should dispatch.
    """
    try:
        from tools.registry import registry as _registry
        schema = _registry.get_schema(name)
        if not isinstance(schema, dict):
            return None
        fn = schema.get("function") if schema.get("type") == "function" else schema
        if not isinstance(fn, dict):
            return None
        params = fn.get("parameters")
        if not isinstance(params, dict):
            return None
        required = params.get("required")
        if not isinstance(required, list) or not required:
            return None
        missing = [r for r in required if isinstance(r, str) and r not in args]
        if not missing:
            return None
        return tool_error(
            f"tool_call to '{name}' is missing required argument(s): "
            f"{', '.join(missing)}. The tool was NOT invoked.",
            parameters=params,
            hint=(
                "Retry tool_call with 'arguments' matching the parameters "
                "schema above."
            ),
        )
    except Exception:  # pragma: no cover — never block dispatch on validator bugs
        logger.debug("validate_deferred_call_args failed for %s", name, exc_info=True)
        return None


def resolve_underlying_call(args: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
    """Parse a ``tool_call`` invocation into (underlying_name, args, error_msg).

    Used by:
    * the dispatcher in ``model_tools.handle_function_call``,
    * the display layer (so the activity feed shows the underlying tool),
    * the trajectory recorder.

    On parse error, returns ``(None, {}, error_message)``.
    """
    name = str(args.get("name") or "").strip()
    if not name:
        return None, {}, "tool_call requires a 'name' argument"
    if name in BRIDGE_TOOL_NAMES:
        return None, {}, f"tool_call cannot invoke '{name}' (it is itself a bridge tool)"
    raw_args = args.get("arguments")
    if raw_args is None:
        raw_args = {}
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except json.JSONDecodeError as e:
            return None, {}, f"tool_call 'arguments' is not valid JSON: {e}"
    if not isinstance(raw_args, dict):
        return None, {}, "tool_call 'arguments' must be an object"
    if not is_deferrable_tool_name(name):
        return None, {}, (
            f"'{name}' is not a deferrable tool. If it appears in the model-facing tools "
            "list already, call it directly instead of via tool_call."
        )
    return name, raw_args, None


__all__ = [
    "TOOL_SEARCH_NAME",
    "TOOL_DESCRIBE_NAME",
    "TOOL_CALL_NAME",
    "BRIDGE_TOOL_NAMES",
    "ToolSearchConfig",
    "CatalogEntry",
    "AssemblyResult",
    "load_config",
    "never_defer_tool_names",
    "is_deferrable_tool_name",
    "classify_tools",
    "estimate_tokens_from_schemas",
    "should_activate",
    "build_catalog",
    "build_catalog_listing",
    "build_catalog_listing_with_form",
    "listing_token_budget",
    "search_catalog",
    "bridge_tool_schemas",
    "tool_describe_schema",
    "ensure_tool_describe_present",
    "assemble_tool_defs",
    "is_bridge_tool",
    "dispatch_tool_search",
    "dispatch_tool_describe",
    "resolve_underlying_call",
    "scoped_deferrable_names",
    "validate_deferred_call_args",
]
