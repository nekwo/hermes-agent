"""
Tests for subagent progress relay (issue #169).

Verifies that:
- KawaiiSpinner.print_above() works with and without active spinner
- _build_child_progress_callback handles CLI/gateway/no-display paths
- Thinking events are relayed correctly
- Parallel callbacks don't share state
"""

import io
import sys
import pytest
from unittest.mock import MagicMock

from agent.display import KawaiiSpinner
from tools.delegate_tool import _build_child_progress_callback


# =========================================================================
# KawaiiSpinner.print_above tests
# =========================================================================

class TestPrintAbove:
    """Tests for KawaiiSpinner.print_above method."""

    def test_print_above_without_spinner_running(self):
        """print_above should write to stdout even when spinner is not running."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("test")
        spinner._out = buf  # Redirect to buffer
        
        spinner.print_above("hello world")
        output = buf.getvalue()
        assert "hello world" in output

    def test_print_above_with_spinner_running(self):
        """print_above should clear spinner line and print text."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("test")
        spinner._out = buf
        spinner.running = True  # Pretend spinner is running (don't start thread)
        
        spinner.print_above("tool line")
        output = buf.getvalue()
        assert "tool line" in output
        assert "\r" in output  # Should start with carriage return to clear spinner line

    def test_print_above_uses_captured_stdout(self):
        """print_above should use self._out, not sys.stdout.
        This ensures it works inside redirect_stdout(devnull)."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("test")
        spinner._out = buf
        
        # Simulate redirect_stdout(devnull)
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            spinner.print_above("should go to buf")
        finally:
            sys.stdout = old_stdout
        
        assert "should go to buf" in buf.getvalue()


# =========================================================================
# _build_child_progress_callback tests
# =========================================================================

class TestBuildChildProgressCallback:
    """Tests for child progress callback builder."""




    def test_gateway_batched_progress(self):
        """Gateway path: each tool.started relays a subagent.tool event, and a
        subagent.progress summary fires once BATCH_SIZE tools accumulate."""
        parent = MagicMock()
        parent._delegate_spinner = None
        parent_cb = MagicMock()
        parent.tool_progress_callback = parent_cb

        cb = _build_child_progress_callback(0, "test goal", parent)

        # Each tool.started relays a subagent.tool event immediately (per-tool relay).
        for i in range(4):
            cb("tool.started", f"tool_{i}", f"arg_{i}", {})
        # 4 per-tool relays so far, no batch summary yet (BATCH_SIZE=5)
        events = [c.args[0] for c in parent_cb.call_args_list]
        assert events == ["subagent.tool"] * 4

        # 5th call triggers another per-tool relay PLUS the batch-size summary
        cb("tool.started", "tool_4", "arg_4", {})
        events = [c.args[0] for c in parent_cb.call_args_list]
        assert events == ["subagent.tool"] * 5 + ["subagent.progress"]
        summary_call = parent_cb.call_args_list[-1]
        summary_text = summary_call.kwargs.get("preview") or summary_call.args[2]
        assert "tool_0" in summary_text
        assert "tool_4" in summary_text


    def test_parallel_callbacks_independent(self):
        """Each child's callback batches tool names independently."""
        parent = MagicMock()
        parent._delegate_spinner = None
        parent_cb = MagicMock()
        parent.tool_progress_callback = parent_cb

        cb0 = _build_child_progress_callback(0, "goal a", parent)
        cb1 = _build_child_progress_callback(1, "goal b", parent)

        # 3 tool.started per child = 6 per-tool relays; neither should hit
        # the batch-size summary (batch size = 5, counted per-child).
        for i in range(3):
            cb0("tool.started", f"tool_{i}", f"a_{i}", {})
            cb1("tool.started", f"other_{i}", f"b_{i}", {})

        events = [c.args[0] for c in parent_cb.call_args_list]
        assert events.count("subagent.tool") == 6
        assert "subagent.progress" not in events

    def test_task_index_prefix_in_batch_mode(self):
        """Batch mode (task_count > 1) should show 1-indexed prefix for all tasks."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("delegating")
        spinner._out = buf
        spinner.running = True
        
        parent = MagicMock()
        parent._delegate_spinner = spinner
        parent.tool_progress_callback = None
        
        # task_index=0 in a batch of 3 → prefix "[1]"
        cb0 = _build_child_progress_callback(0, "test goal", parent, task_count=3)
        cb0("tool.started", "web_search", "test", {})
        output = buf.getvalue()
        assert "[1]" in output

        # task_index=2 in a batch of 3 → prefix "[3]"
        buf.truncate(0)
        buf.seek(0)
        cb2 = _build_child_progress_callback(2, "test goal", parent, task_count=3)
        cb2("tool.started", "web_search", "test", {})
        output = buf.getvalue()
        assert "[3]" in output



# =========================================================================
# Integration: thinking callback in run_agent.py
# =========================================================================

class TestThinkingCallback:
    """Tests for the _thinking callback in AIAgent conversation loop."""

    def _simulate_thinking_callback(self, content, callback, delegate_depth=1):
        """Simulate the exact code path from run_agent.py for the thinking callback.
        
        delegate_depth: simulates self._delegate_depth.
            0 = main agent (should NOT fire), >=1 = subagent (should fire).
        """
        import re
        if (content and callback and delegate_depth > 0):
            _think_text = content.strip()
            _think_text = re.sub(
                r'</?(?:REASONING_SCRATCHPAD|think|reasoning)>', '', _think_text
            ).strip()
            first_line = _think_text.split('\n')[0][:80] if _think_text else ""
            if first_line:
                try:
                    callback("_thinking", first_line)
                except Exception:
                    pass

    def test_thinking_callback_fires_on_content(self):
        """tool_progress_callback should receive _thinking event
        when assistant message has content."""
        calls = []
        self._simulate_thinking_callback(
            "I'll research quantum computing first, then summarize.",
            lambda name, preview=None: calls.append((name, preview))
        )
        assert len(calls) == 1
        assert calls[0][0] == "_thinking"
        assert "quantum computing" in calls[0][1]


    def test_thinking_callback_truncates_long_content(self):
        """Should truncate long content to 80 chars."""
        calls = []
        self._simulate_thinking_callback(
            "A" * 200 + "\nSecond line should be ignored",
            lambda name, preview=None: calls.append((name, preview))
        )
        assert len(calls) == 1
        assert len(calls[0][1]) == 80






class TestNativeReasoningEmit:
    """Tests for the fork-addition thinking emits in the conversation loop
    (trace-visibility G3 + the reply-echo suppression fix). Mirrors the exact
    combined code path in ``agent/conversation_loop.py``: native reasoning is
    extracted first and OWNS the turn's thinking row; the legacy reply-echo
    emit fires only when the provider surfaced no native reasoning. Same
    simulation idiom :class:`TestThinkingCallback` uses for its sibling block."""

    def _simulate_thinking_emits(
        self, native_reasoning, content, callback, delegate_depth=0
    ):
        """Faithful copy of the conversation-loop thinking-emit path.

        ``native_reasoning`` stands in for ``agent._extract_reasoning(...)``;
        ``content`` for ``assistant_message.content``.
        """
        import re
        _native = ""
        if callback and delegate_depth == 0:
            _native = native_reasoning.strip() if native_reasoning else ""
        if content and callback:
            _think_text = re.sub(
                r'</?(?:REASONING_SCRATCHPAD|think|reasoning)>', '',
                content.strip(),
            ).strip()
            first_line = _think_text.split('\n')[0][:80] if _think_text else ""
            if first_line and delegate_depth > 0:
                try:
                    callback("_thinking", first_line)
                except Exception:
                    pass
            elif _think_text and not _native:
                try:
                    callback("reasoning.available", "_thinking", _think_text[:500], None)
                except Exception:
                    pass
        if _native:
            try:
                callback("reasoning.available", "_thinking", _native[:500], None)
            except Exception:
                pass

    def test_native_reasoning_owns_the_row_echo_suppressed(self):
        # THE live-run regression (2026-07-17 retest): native reasoning and the
        # reply both present -> exactly ONE thinking emit, carrying the native
        # text; the reply echo must NOT also surface as a near-duplicate row
        # (whitespace-collapse + the 500-char cap defeat the projection's
        # byte-equal dedup, so suppression must happen at the emit).
        calls = []
        self._simulate_thinking_emits(
            "Let me plan the fan-out: one bounded order per teammate, then summarize.",
            "Dispatched to all three: backend_dev, dev, and qa.",
            lambda *args: calls.append(args),
        )
        assert len(calls) == 1
        assert calls[0][0] == "reasoning.available"
        assert calls[0][1] == "_thinking"
        assert "plan the fan-out" in calls[0][2]
        assert "Dispatched to all three" not in calls[0][2]

    def test_native_reasoning_fires_on_empty_content_turn(self):
        # Reasoning-only codex turn: content is empty, native reasoning present.
        # This is the case the pre-G3 code dropped (emit was inside the content
        # gate).
        calls = []
        self._simulate_thinking_emits(
            "Thinking through the plan; no visible reply text yet.",
            "",
            lambda *args: calls.append(args),
        )
        assert len(calls) == 1
        assert "Thinking through the plan" in calls[0][2]

    def test_native_reasoning_truncated_to_500(self):
        calls = []
        self._simulate_thinking_emits(
            "z" * 900, "a reply", lambda *args: calls.append(args)
        )
        assert len(calls) == 1
        assert len(calls[0][2]) == 500

    def test_fused_reasoning_emits_exactly_once(self):
        # A provider that inlines reasoning into content: native == content.
        # The native emit carries it and the echo stays suppressed — one row,
        # not zero (the retired != guard would have emitted NOTHING on this
        # shape once echo suppression landed).
        text = "This is the whole message, reasoning and reply fused together."
        calls = []
        self._simulate_thinking_emits(
            text, text, lambda *args: calls.append(args)
        )
        assert len(calls) == 1
        assert calls[0][0] == "reasoning.available"
        assert calls[0][2] == text

    def test_subagent_keeps_first_line_relay_only(self):
        # depth > 0 keeps its existing first-line _thinking relay untouched and
        # never emits reasoning.available.
        calls = []
        self._simulate_thinking_emits(
            "child reasoning", "child reply",
            lambda *args: calls.append(args), delegate_depth=1,
        )
        assert len(calls) == 1
        assert calls[0][0] == "_thinking"
        assert calls[0][1] == "child reply"

    def test_echo_fallback_when_native_absent(self):
        # No native reasoning parsed: the legacy reply-echo stand-in still
        # fires so providers without parsed reasoning keep a thinking row.
        calls = []
        self._simulate_thinking_emits(
            None, "just a reply", lambda *args: calls.append(args)
        )
        assert len(calls) == 1
        assert calls[0][0] == "reasoning.available"
        assert calls[0][2] == "just a reply"


# =========================================================================
# Gateway batch flush tests
# =========================================================================

class TestBatchFlush:
    """Tests for gateway batch flush on subagent completion."""

    def test_flush_sends_remaining_batch(self):
        """_flush should send a final subagent.progress summary of any unsent
        tool names in the batch (less than BATCH_SIZE)."""
        parent = MagicMock()
        parent._delegate_spinner = None
        parent_cb = MagicMock()
        parent.tool_progress_callback = parent_cb

        cb = _build_child_progress_callback(0, "test goal", parent)

        # Send 3 tools (below batch size of 5) — each relays subagent.tool
        cb("tool.started", "web_search", "query1", {})
        cb("tool.started", "read_file", "file.txt", {})
        cb("tool.started", "write_file", "out.txt", {})
        events = [c.args[0] for c in parent_cb.call_args_list]
        assert events == ["subagent.tool"] * 3  # per-tool relays so far
        assert "subagent.progress" not in events  # no batch-size summary yet

        # Flush should send the remaining 3 as a summary
        cb._flush()
        events = [c.args[0] for c in parent_cb.call_args_list]
        assert events[-1] == "subagent.progress"
        summary_call = parent_cb.call_args_list[-1]
        summary_text = summary_call.kwargs.get("preview") or summary_call.args[2]
        assert "web_search" in summary_text
        assert "write_file" in summary_text

    def test_flush_noop_when_batch_empty(self):
        """_flush should not send anything when batch is empty."""
        parent = MagicMock()
        parent._delegate_spinner = None
        parent_cb = MagicMock()
        parent.tool_progress_callback = parent_cb

        cb = _build_child_progress_callback(0, "test goal", parent)
        cb._flush()
        parent_cb.assert_not_called()

    def test_flush_noop_when_no_parent_callback(self):
        """_flush should not crash when there's no parent callback."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("test")
        spinner._out = buf
        spinner.running = True

        parent = MagicMock()
        parent._delegate_spinner = spinner
        parent.tool_progress_callback = None

        cb = _build_child_progress_callback(0, "test goal", parent)
        cb("tool.started", "web_search", "test", {})
        cb._flush()  # Should not crash


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

