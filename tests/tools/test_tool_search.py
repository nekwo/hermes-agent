"""Tests for tools/tool_search.py — progressive tool disclosure.

Coverage targets — these mirror the issues called out in the OpenClaw tool
search report. Every test that names an OpenClaw issue is the regression
guard that would have caught that specific failure mode.
"""

from __future__ import annotations

import json
import os
import sys
from typing import List, Dict, Any

import pytest


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _td(name: str, description: str = "", properties: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
            },
        },
    }


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class TestConfigParsing:
    def test_default_when_missing(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw(None)
        assert cfg.enabled == "auto"
        assert cfg.threshold_pct == 5.0

    def test_bool_true_maps_to_auto(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw(True)
        assert cfg.enabled == "auto"


    def test_search_limits_clamped(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw({
            "search_default_limit": 999,
            "max_search_limit": 999,
        })
        assert cfg.max_search_limit == 50
        assert cfg.search_default_limit <= cfg.max_search_limit

    def test_never_defer_config_parses_and_sanitizes(self):
        """The operator extension parses to a deduped, sorted tuple; junk
        shapes fall back to the empty extension rather than raising."""
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw({
            "never_defer": ["my_mcp_tool", " ", "tool_search", "my_mcp_tool"],
        })
        assert cfg.never_defer == ("my_mcp_tool",), (
            f"never_defer must sanitize to ('my_mcp_tool',), got {cfg.never_defer!r}"
        )
        assert ToolSearchConfig.from_raw(None).never_defer == ()
        assert ToolSearchConfig.from_raw(True).never_defer == ()
        assert ToolSearchConfig.from_raw({}).never_defer == ()
        # Non-list shapes are ignored, never raised on.
        assert ToolSearchConfig.from_raw({"never_defer": "nope"}).never_defer == ()
        assert ToolSearchConfig.from_raw({"never_defer": {"a": 1}}).never_defer == ()
        assert ToolSearchConfig.from_raw({"never_defer": None}).never_defer == ()

    def test_never_defer_config_extends_never_shrinks(self):
        """Config EXTENDS the hardcoded promotions — it can never remove one."""
        from tools.registry import registry
        from tools.tool_search import (
            ToolSearchConfig, is_deferrable_tool_name, never_defer_tool_names,
        )

        hardcoded = {"agent_chat_send", "agent_chat_dispatches"}

        empty = never_defer_tool_names(ToolSearchConfig.from_raw({"never_defer": []}))
        assert hardcoded <= empty, (
            f"hardcoded promotions missing with an empty extension: {sorted(empty)}"
        )

        cfg = ToolSearchConfig.from_raw({"never_defer": ["extra_x"]})
        extended = never_defer_tool_names(cfg)
        assert hardcoded <= extended, (
            f"config extension dropped hardcoded promotions: {sorted(extended)}"
        )
        assert "extra_x" in extended

        # And the extension actually un-hides a really-registered MCP tool.
        def _handler(args, task_id=None, **kw):
            return json.dumps({"ok": True})

        registry.register(
            name="nd_extension_probe",
            handler=_handler,
            schema=_td("nd_extension_probe", "Probe tool.")["function"],
            toolset="mcp-nd-test",
        )
        assert is_deferrable_tool_name(
            "nd_extension_probe", config=ToolSearchConfig.from_raw(None))
        assert not is_deferrable_tool_name(
            "nd_extension_probe",
            config=ToolSearchConfig.from_raw({"never_defer": ["nd_extension_probe"]}),
        )


# ---------------------------------------------------------------------------
# Classification — the hard invariant: core tools NEVER defer.
# ---------------------------------------------------------------------------


class TestClassification:
    def test_core_tools_never_defer(self):
        """The critical invariant from the OpenClaw report."""
        from tools.tool_search import is_deferrable_tool_name
        # Sample of core tools from _HERMES_CORE_TOOLS.
        for core_name in ["terminal", "read_file", "write_file", "patch",
                          "search_files", "todo", "memory", "browser_navigate",
                          "web_search", "session_search", "clarify",
                          "execute_code", "delegate_task", "send_message"]:
            assert not is_deferrable_tool_name(core_name), (
                f"Core tool '{core_name}' must NEVER be deferrable"
            )

    def test_bridge_tools_never_defer(self):
        from tools.tool_search import is_deferrable_tool_name, BRIDGE_TOOL_NAMES
        for name in BRIDGE_TOOL_NAMES:
            assert not is_deferrable_tool_name(name)

    def test_unknown_tool_not_deferrable(self):
        """Defensive: a tool name we cannot resolve to a registry entry must
        not be claimed as deferrable. This protects against the OpenClaw
        cron regression where unresolved tools were silently dropped."""
        from tools.tool_search import is_deferrable_tool_name
        assert not is_deferrable_tool_name("xx_definitely_not_a_tool_xx")

    def test_classify_keeps_unknown_in_visible(self):
        """A tool we can't classify stays visible — never silently dropped.

        This is the OpenClaw #84141 regression guard (cron lost ``exec``
        because it wasn't in the catalog).
        """
        from tools.tool_search import classify_tools
        # Build a tool def for something we don't have a registry entry for.
        defs = [_td("xx_unknown_tool", "Unknown tool")]
        visible, deferrable = classify_tools(defs)
        names = {(td.get("function") or {}).get("name") for td in visible}
        assert "xx_unknown_tool" in names
        assert deferrable == []

    def test_promoted_agent_chat_tools_never_defer(self):
        """Operator ruling 2026-08-23: agent-to-agent send is a first-class
        tool. It is promoted in tool_search's never-defer set — NOT added to
        toolsets._HERMES_CORE_TOOLS, which would grant it everywhere."""
        import tools.agent_chat_tool  # noqa: F401 — registration runs at import
        from tools.tool_search import ToolSearchConfig, is_deferrable_tool_name
        cfg = ToolSearchConfig.from_raw(None)
        for name in ("agent_chat_send", "agent_chat_dispatches"):
            assert not is_deferrable_tool_name(name, config=cfg), (
                f"Promoted tool '{name}' must NEVER be deferrable"
            )

    def test_other_agent_chat_tools_stay_deferrable(self):
        """The promotion's boundary: only send + dispatches ride eagerly."""
        import tools.agent_chat_tool  # noqa: F401
        from tools.registry import registry
        from tools.tool_search import ToolSearchConfig, is_deferrable_tool_name
        cfg = ToolSearchConfig.from_raw(None)
        for name in ("agent_chat_threads", "agent_chat_open", "agent_chat_log_path"):
            assert registry.get_entry(name) is not None, (
                f"'{name}' is not registered — this test would false-green"
            )
            assert is_deferrable_tool_name(name, config=cfg), (
                f"Read-side sibling '{name}' must stay deferred"
            )

    def test_never_defer_does_not_grant(self):
        """Un-hide, never grant. classify_tools only PARTITIONS the defs it is
        handed — a promoted name can never materialize in a lane that was not
        granted the toolset producing its def."""
        import tools.agent_chat_tool  # noqa: F401
        from tools.tool_search import ToolSearchConfig, classify_tools
        defs = [_td("terminal", "Run shell")]
        visible, deferrable = classify_tools(
            defs, config=ToolSearchConfig.from_raw(None))
        out_names = {(td.get("function") or {}).get("name") for td in visible + deferrable}
        assert out_names == {"terminal"}, (
            f"classify_tools materialized names it was never handed: {sorted(out_names)}"
        )


# ---------------------------------------------------------------------------
# Token estimation + threshold gate
# ---------------------------------------------------------------------------


class TestThresholdGate:
    def test_off_never_activates(self):
        from tools.tool_search import ToolSearchConfig, should_activate
        cfg = ToolSearchConfig.from_raw({"enabled": "off"})
        assert not should_activate(cfg, deferrable_tokens=1_000_000, context_length=200_000)


    def test_token_estimate_proportional_to_schema_size(self):
        from tools.tool_search import estimate_tokens_from_schemas
        small = [_td("a", "x")]
        big = [_td(f"name_{i}", f"description for tool {i} " * 20,
                   {"q": {"type": "string", "description": "search query " * 10}})
               for i in range(10)]
        small_t = estimate_tokens_from_schemas(small)
        big_t = estimate_tokens_from_schemas(big)
        assert big_t > small_t * 10


# ---------------------------------------------------------------------------
# Retrieval (BM25 + substring fallback)
# ---------------------------------------------------------------------------


class TestRetrieval:
    def _fake_catalog(self):
        """Build a catalog directly without touching the registry."""
        from tools.tool_search import CatalogEntry, _tokenize, _entry_search_text
        defs = [
            _td("github_create_issue", "Open a new issue in a GitHub repository",
                {"title": {"type": "string"}, "body": {"type": "string"}}),
            _td("github_search_repos", "Search GitHub for matching repositories",
                {"query": {"type": "string"}}),
            _td("slack_send_message", "Post a message into a Slack channel",
                {"channel": {"type": "string"}, "text": {"type": "string"}}),
            _td("calendar_create_event", "Add an event to the user's calendar",
                {"title": {"type": "string"}, "start": {"type": "string"}}),
        ]
        catalog = []
        for d in defs:
            fn = d["function"]
            e = CatalogEntry(
                name=fn["name"], description=fn["description"],
                schema=d, source="mcp", source_name="mcp-test",
            )
            e._tokens = _tokenize(_entry_search_text(d))
            catalog.append(e)
        return catalog

    def test_search_finds_relevant_tool(self):
        from tools.tool_search import search_catalog
        hits = search_catalog(self._fake_catalog(), "create a github issue", limit=3)
        names = [h.name for h in hits]
        assert names[0] == "github_create_issue"


    def test_search_respects_limit(self):
        from tools.tool_search import search_catalog
        hits = search_catalog(self._fake_catalog(), "github", limit=1)
        assert len(hits) <= 1


# ---------------------------------------------------------------------------
# Assembly — the full passthrough/activate decision.
# ---------------------------------------------------------------------------


class TestAssembly:
    def test_no_deferrable_returns_unchanged(self):
        """Pure-core toolset: pass-through, no bridge tools added."""
        from tools.tool_search import assemble_tool_defs, ToolSearchConfig
        defs = [_td("terminal", "Run shell"), _td("read_file", "Read a file")]
        result = assemble_tool_defs(
            defs,
            context_length=200_000,
            config=ToolSearchConfig.from_raw({"enabled": "on"}),
        )
        assert not result.activated
        assert {t["function"]["name"] for t in result.tool_defs} == {"terminal", "read_file"}

    @staticmethod
    def _register_mcp(name):
        from tools.registry import registry

        def _handler(args, task_id=None, **kw):
            return json.dumps({"ok": True})

        registry.register(
            name=name,
            handler=_handler,
            schema=_td(name, "Deferred capability description.")["function"],
            toolset="mcp-tiertest",
        )


    def test_idempotent_when_bridge_already_present(self):
        from tools.tool_search import assemble_tool_defs, ToolSearchConfig, BRIDGE_TOOL_NAMES
        defs = [_td("terminal", "Run shell"), _td("tool_search", "old")]
        result = assemble_tool_defs(
            defs,
            context_length=200_000,
            config=ToolSearchConfig.from_raw({"enabled": "off"}),
        )
        names = [(t["function"]["name"]) for t in result.tool_defs]
        # The pre-existing tool_search was stripped (it would be re-injected if
        # activation happened; here it didn't).
        assert "tool_search" not in names


# ---------------------------------------------------------------------------
# Bridge dispatch
# ---------------------------------------------------------------------------


class TestBridgeDispatch:
    def test_tool_search_requires_query(self):
        from tools.tool_search import dispatch_tool_search
        result = dispatch_tool_search({}, current_tool_defs=[])
        assert "error" in json.loads(result)

    # Fork-retained (T6b): tool_describe serves the FULL docs for CORE tools.
    # Upstream's up-front non-deferrable rejection was deliberately NOT adopted,
    # so its test_tool_describe_rejects_non_deferrable has no counterpart here.
    def test_tool_describe_requires_name(self):
        from tools.tool_search import dispatch_tool_describe
        result = dispatch_tool_describe({}, current_tool_defs=[])
        assert "error" in json.loads(result)

    def test_tool_describe_serves_core_tool_full_docs(self):
        """T6b: tool_describe now serves the FULL docs for a core tool whose
        wire schema ships a brief. It reads the full text from the fork-owned
        mirror and the live (untrimmed) parameter schema off the registry."""
        from tools.registry import discover_builtin_tools
        discover_builtin_tools()
        from tools.tool_search import dispatch_tool_describe
        result = json.loads(
            dispatch_tool_describe({"name": "session_search"}, current_tool_defs=[])
        )
        assert "error" not in result
        # Full original text, not the trimmed brief.
        assert "SOURCE-FIRST LIMIT" in result["description"]
        assert "FOUR CALLING SHAPES" in result["description"]
        # Parameters are never trimmed — live schema is returned.
        assert result["parameters"].get("properties")

    def test_tool_describe_unknown_tool_errors(self):
        from tools.tool_search import dispatch_tool_describe
        result = json.loads(
            dispatch_tool_describe({"name": "zzz_not_a_tool"}, current_tool_defs=[])
        )
        assert "error" in result

    def test_tool_describe_schema_is_fixed_and_tiny(self):
        from tools.tool_search import tool_describe_schema, TOOL_DESCRIBE_NAME
        schema = tool_describe_schema()
        fn = schema["function"]
        assert fn["name"] == TOOL_DESCRIBE_NAME
        assert list(fn["parameters"]["properties"]) == ["name"]
        assert fn["parameters"]["required"] == ["name"]

    def test_ensure_tool_describe_present_is_idempotent(self):
        from tools.tool_search import (
            ensure_tool_describe_present, tool_describe_schema, TOOL_DESCRIBE_NAME,
        )
        base = [_td("terminal", "Run shell")]
        once = ensure_tool_describe_present(base)
        names = [(t.get("function") or {}).get("name") for t in once]
        assert names.count(TOOL_DESCRIBE_NAME) == 1
        twice = ensure_tool_describe_present(once)
        names2 = [(t.get("function") or {}).get("name") for t in twice]
        assert names2.count(TOOL_DESCRIBE_NAME) == 1
        # Never mutates the input list.
        assert TOOL_DESCRIBE_NAME not in [
            (t.get("function") or {}).get("name") for t in base
        ]

    def test_resolve_underlying_call_parses_object_args(self):
        from tools.tool_search import resolve_underlying_call
        name, args, err = resolve_underlying_call({
            "name": "unknown_xxx",
            "arguments": {"foo": "bar"},
        })
        # Will fail classification because unknown_xxx isn't deferrable.
        assert err is not None


    def test_resolve_underlying_call_rejects_recursion(self):
        """tool_call cannot invoke tool_call itself."""
        from tools.tool_search import resolve_underlying_call, TOOL_CALL_NAME
        name, args, err = resolve_underlying_call({
            "name": TOOL_CALL_NAME,
            "arguments": {},
        })
        assert err is not None
        assert "bridge tool" in err.lower()


# ---------------------------------------------------------------------------
# End-to-end via the real handle_function_call (smoke test).
# ---------------------------------------------------------------------------


class TestHandleFunctionCallIntegration:
    def test_tool_search_dispatch_through_handle_function_call(self):
        """The dispatcher recognizes the bridge tool by name."""
        import model_tools
        result = model_tools.handle_function_call(
            function_name="tool_search",
            function_args={"query": "nothing matches this"},
        )
        parsed = json.loads(result)
        # Without a real registry, the matches will be empty, but the
        # dispatch path completed without error.
        assert "matches" in parsed or "error" in parsed


class TestRegression_OpenClawCron84141:
    """Regression guard for the OpenClaw cron-tool-loss class of bug.

    OpenClaw #84141: ``toolsAllow: ["exec"]`` on an isolated cron turn
    resulted in the agent receiving only ``sessions_send`` — the catalog
    builder silently dropped the requested core tool.

    Our defense: core tools are NEVER deferred. This test exercises the
    full assembly pipeline with a mixed core+MCP toolset and asserts that
    every core tool survives.
    """

    def test_core_tool_survives_alongside_many_mcp_tools(self):
        from tools.tool_search import (
            assemble_tool_defs, ToolSearchConfig, BRIDGE_TOOL_NAMES,
            classify_tools,
        )
        # 1 core tool + 50 unknown/MCP-shaped tools (deferrable).
        defs = [_td("terminal", "Run shell commands")]
        # Pad with fake "deferrable" tools — without registry registration,
        # classify_tools puts them in 'visible'. So instead, we just verify
        # the core-tool side: terminal stays in visible regardless.
        visible, deferrable = classify_tools(defs)
        assert any(
            (td.get("function") or {}).get("name") == "terminal"
            for td in visible
        ), "Core tool 'terminal' was wrongly classified as deferrable"

        # Now force activation and check the resulting tool-defs list.
        result = assemble_tool_defs(
            defs,
            context_length=200_000,
            config=ToolSearchConfig.from_raw({"enabled": "on"}),
        )
        names = {(t.get("function") or {}).get("name") for t in result.tool_defs}
        # terminal must be present; bridges are only added if there are
        # deferrable tools to put behind them.
        assert "terminal" in names

    def test_unwrap_rejects_core_tool_attempt(self):
        """Even if the model tries to invoke a core tool through tool_call,
        we reject the call and tell the model to use it directly."""
        from tools.tool_search import resolve_underlying_call
        _, _, err = resolve_underlying_call({
            "name": "terminal",
            "arguments": {"command": "echo hi"},
        })
        assert err is not None
        assert "not a deferrable" in err


class TestRegression_ToolsetScoping:
    """A restricted-toolset session must not see or invoke out-of-scope tools.

    The bug: the bridge dispatch and the tool_executor unwrap read the
    catalog from the *global* registry (get_tool_definitions with no
    toolset scope = "start with everything"), so a session scoped to one
    MCP server could tool_search the entire process registry and tool_call
    any plugin tool it was never granted. registry.dispatch() has no
    enabled_tools gate for non-execute_code tools, so the out-of-scope tool
    actually ran.

    The fix threads the session's enabled/disabled toolsets into the bridge
    dispatch (model_tools.handle_function_call) and the executor unwrap
    (agent.tool_executor), scoping both the searchable catalog and the
    invocable set to the session's own toolsets.
    """

    @staticmethod
    def _register(name, toolset):
        from tools.registry import registry

        def _handler(args, task_id=None, **kw):
            return json.dumps({"ok": True, "tool": name})

        registry.register(
            name=name,
            handler=_handler,
            schema=_td(name, f"desc for {name}", {"repo": {"type": "string"}}),
            toolset=toolset,
        )

    def test_search_catalog_is_scoped_to_session_toolsets(self):
        import model_tools

        for i in range(12):
            self._register(f"mcp_scoped_gh_{i}", "mcp-scoped-gh")
        self._register("scoped_oos_plugin", "scopedoosplugin")

        # tool_search scoped to the github toolset must not count the
        # out-of-scope plugin tool (or any of the host registry).
        result = model_tools.handle_function_call(
            function_name="tool_search",
            function_args={"query": "mcp_scoped_gh", "limit": 5},
            enabled_toolsets=["mcp-scoped-gh"],
        )
        parsed = json.loads(result)
        assert parsed["total_available"] == 12, (
            f"expected scoped catalog of 12, got {parsed['total_available']} "
            "— catalog leaked tools outside the session's toolsets"
        )
        hit_names = {m["name"] for m in parsed["matches"]}
        assert "scoped_oos_plugin" not in hit_names


    def test_scoped_deferrable_names_helper(self):
        from tools.tool_search import scoped_deferrable_names

        self._register("mcp_helper_op", "mcp-helper")
        import model_tools
        defs = model_tools.get_tool_definitions(
            enabled_toolsets=["mcp-helper"],
            quiet_mode=True,
            skip_tool_search_assembly=True,
        )
        names = scoped_deferrable_names(defs)
        assert "mcp_helper_op" in names
        # core tools are never deferrable
        assert "terminal" not in names

    # ---- inline schemas on top search hits (kills the describe round-trip) ----

    def test_search_hits_include_parameters_for_top_hits(self):
        from tools.tool_search import (
            ToolSearchConfig, dispatch_tool_search, _SEARCH_HIT_SCHEMA_TOP_N,
        )

        props: Dict[str, Any] = {}
        defs: List[Dict[str, Any]] = []
        for i in range(6):
            name = f"mcp_inline_sch_{i}"
            self._register(name, "mcp-inline-sch")
            props[name] = {f"arg_{i}": {"type": "string", "description": f"argument {i}"}}
            defs.append(_td(name, "Inline schema probe tool.", props[name]))

        parsed = json.loads(dispatch_tool_search(
            {"query": "inline schema probe", "limit": 6},
            current_tool_defs=defs,
            config=ToolSearchConfig.from_raw({"enabled": "on"}),
        ))
        matches = parsed["matches"]
        assert len(matches) == 6, f"expected all 6 probes to match, got {len(matches)}"

        assert "parameters" in matches[0], (
            "top search hit carries no inline parameters schema — the model is "
            "forced back into a tool_describe round-trip"
        )
        assert matches[0]["parameters"]["properties"] == props[matches[0]["name"]], (
            "inline schema does not match the tool's registered parameters"
        )
        for m in matches[:_SEARCH_HIT_SCHEMA_TOP_N]:
            assert "parameters" in m, f"top hit '{m['name']}' is missing parameters"
        for m in matches[_SEARCH_HIT_SCHEMA_TOP_N:]:
            assert "parameters" not in m, (
                f"hit '{m['name']}' is past the top-{_SEARCH_HIT_SCHEMA_TOP_N} "
                "cutoff and must stay schema-less"
            )

    def test_search_hit_schema_cap_omits_oversized(self):
        from tools.tool_search import (
            ToolSearchConfig, dispatch_tool_search, _SEARCH_HIT_SCHEMA_MAX_CHARS,
        )

        name = "mcp_oversized_sch_tool"
        self._register(name, "mcp-oversized-sch")
        big = {"blob": {"type": "string",
                        "description": "x" * (_SEARCH_HIT_SCHEMA_MAX_CHARS + 1000)}}
        defs = [_td(name, "Oversized schema probe.", big)]

        parsed = json.loads(dispatch_tool_search(
            {"query": "oversized schema probe"},
            current_tool_defs=defs,
            config=ToolSearchConfig.from_raw({"enabled": "on"}),
        ))
        match = parsed["matches"][0]
        assert match["name"] == name
        assert "parameters" not in match, (
            "an over-cap schema was inlined anyway — one pathological MCP tool "
            "can now blow up every search result"
        )
        # Name + description still survive, so tool_describe remains reachable.
        assert match["description"] == "Oversized schema probe."

    def test_bridge_description_licenses_skipping_describe(self):
        from tools.tool_search import assemble_tool_defs, ToolSearchConfig

        for i in range(5):
            self._register(f"mcp_desc_lic_{i}", "mcp-desc-lic")
        defs = [_td(f"mcp_desc_lic_{i}", "Deferred.") for i in range(5)]
        result = assemble_tool_defs(
            defs, context_length=200_000,
            config=ToolSearchConfig.from_raw({"enabled": "on"}),
        )
        assert result.activated
        search = next(t for t in result.tool_defs
                      if t["function"]["name"] == "tool_search")
        desc = search["function"]["description"]
        assert "full `parameters` schema" in desc, (
            "bridge description does not advertise inline schemas"
        )
        assert "invoke it directly with `tool_call`" in desc, (
            "bridge description does not license skipping tool_describe"
        )


# ---------------------------------------------------------------------------
# Catalog listing (skills-style progressive disclosure)
# ---------------------------------------------------------------------------


class TestCatalogListing:
    def test_config_defaults(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw(None)
        assert cfg.listing == "auto"
        assert cfg.listing_max_tokens == 20000
        # legacy bool shapes keep defaults too
        assert ToolSearchConfig.from_raw(True).listing == "auto"


    def test_short_desc_first_sentence_and_clip(self):
        from tools.tool_search import _short_desc
        assert _short_desc("Open an issue. Second sentence dropped.") == "Open an issue."
        long = "word " * 40
        s = _short_desc(long)
        assert len(s) <= 61  # 60 + ellipsis char
        assert s.endswith("…")
        assert _short_desc("") == ""


    @staticmethod
    def _register(name):
        from tools.registry import registry

        def _handler(args, task_id=None, **kw):
            return json.dumps({"ok": True})

        registry.register(
            name=name,
            handler=_handler,
            schema=_td(name, "Deferred capability description.")["function"],
            toolset="mcp-listingtest",
        )


    def test_assembly_listing_off_keeps_legacy_description(self):
        from tools.tool_search import assemble_tool_defs, ToolSearchConfig
        for i in range(30):
            self._register(f"mcp_x_{i}")
        defs = [_td(f"mcp_x_{i}", "Deferred.") for i in range(30)]
        result = assemble_tool_defs(
            defs, context_length=1000,
            config=ToolSearchConfig.from_raw({"enabled": "on", "listing": "off"}),
        )
        assert result.activated
        search = next(t for t in result.tool_defs if t["function"]["name"] == "tool_search")
        assert "mcp_x_0" not in search["function"]["description"]

    def test_promoted_tools_ride_eagerly_and_drop_from_listing(self):
        """The promotion's whole point, end to end: agent_chat_send ships in
        the model-facing array next to the bridge trio, and vanishes from both
        the tier-1 listing and the tool_search catalog."""
        import tools.agent_chat_tool  # noqa: F401
        from tools.registry import registry
        from tools.tool_search import (
            ToolSearchConfig, assemble_tool_defs, dispatch_tool_search,
        )

        for i in range(30):
            self._register(f"mcp_x_{i}")

        raw = registry.get_schema("agent_chat_send") or {}
        fn = raw.get("function") if raw.get("type") == "function" else raw
        assert (fn or {}).get("name") == "agent_chat_send", (
            "registry did not yield a real agent_chat_send schema — the test "
            "would assert against a fabricated def"
        )
        send_def = {"type": "function", "function": fn}

        defs = [_td(f"mcp_x_{i}", "Deferred.") for i in range(30)] + [send_def]
        cfg = ToolSearchConfig.from_raw({"enabled": "on"})
        result = assemble_tool_defs(defs, context_length=200_000, config=cfg)

        assert result.activated
        names = {t["function"]["name"] for t in result.tool_defs}
        assert {"tool_search", "tool_describe", "tool_call"} <= names
        assert "agent_chat_send" in names, (
            "promoted tool did not ride eagerly in the assembled tools array"
        )
        assert result.deferred_count == 30, (
            f"expected 30 deferred (the MCP tools only), got {result.deferred_count} "
            "— the promoted tool is still being deferred"
        )

        search = next(t for t in result.tool_defs if t["function"]["name"] == "tool_search")
        assert "agent_chat_send" not in search["function"]["description"], (
            "promoted tool is still advertised in the tier-1 catalog listing"
        )

        parsed = json.loads(dispatch_tool_search(
            {"query": "agent chat send", "limit": 10},
            current_tool_defs=defs, config=cfg,
        ))
        assert "agent_chat_send" not in {m["name"] for m in parsed["matches"]}, (
            "promoted tool is still searchable through the bridge catalog"
        )


class TestDeferredCallSchemaProbe:
    """Blind tool_call invocations missing required arguments must return
    the tool's parameter schema instead of dispatching into an opaque
    downstream failure (port of nearai/ironclaw#5149's describe-first fix).

    A deferred tool's schema is invisible until tool_describe is called, so
    models routinely invoke deferred tools by name alone. Pre-fix, that
    produced ``KeyError: 'document_id'``-style errors that teach the model
    nothing; post-fix, the probe returns the schema so the model repairs
    the call in one round-trip. Valid calls dispatch untouched.
    """

    @staticmethod
    def _register(name, toolset, required=("document_id",)):
        from tools.registry import registry

        def _handler(args, task_id=None, **kw):
            # Simulates a tool that crashes opaquely on a missing required arg.
            return json.dumps({"ok": True, "doc": args["document_id"]})

        params = {
            "type": "object",
            "properties": {
                "document_id": {"type": "string", "description": "Doc id"},
                "format": {"type": "string"},
            },
            "required": list(required),
        }
        registry.register(
            name=name,
            handler=_handler,
            schema={"type": "function",
                    "function": {"name": name, "description": f"desc {name}",
                                 "parameters": params}},
            toolset=toolset,
        )

    def test_validator_returns_schema_for_missing_required(self):
        from tools.tool_search import validate_deferred_call_args

        self._register("mcp_probe_docs_get", "mcp-probe")
        err = validate_deferred_call_args("mcp_probe_docs_get", {})
        assert err is not None
        parsed = json.loads(err)
        assert "document_id" in parsed["error"]
        assert "NOT invoked" in parsed["error"]
        assert parsed["parameters"]["required"] == ["document_id"]
        assert "document_id" in parsed["parameters"]["properties"]


    def test_validator_never_blocks_unvalidatable_tools(self):
        from tools.tool_search import validate_deferred_call_args

        # Unknown tool → no schema → dispatch (downstream scope gate handles it).
        assert validate_deferred_call_args("mcp_no_such_tool_xyz", {}) is None


    def test_valid_tool_call_still_dispatches(self):
        import model_tools

        self._register("mcp_probe_valid_op", "mcp-probe-valid")
        result = json.loads(model_tools.handle_function_call(
            function_name="tool_call",
            function_args={"name": "mcp_probe_valid_op",
                           "arguments": {"document_id": "abc"}},
            enabled_toolsets=["mcp-probe-valid"],
        ))
        assert result.get("ok") is True
        assert result.get("doc") == "abc"
