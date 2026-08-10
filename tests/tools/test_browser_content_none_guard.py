"""Tests for None guard on browser_tool LLM response content.

browser_tool.py has two call sites that access response.choices[0].message.content
without checking for None — _extract_relevant_content (line 996) and
browser_vision (line 1626). When reasoning-only models (DeepSeek-R1, QwQ)
return content=None, these produce null snapshots or null analysis.

These tests verify both sites are guarded.
"""

import types
from unittest.mock import patch


# ── helpers ────────────────────────────────────────────────────────────────

def _make_response(content):
    """Build a minimal OpenAI-compatible ChatCompletion response stub."""
    message = types.SimpleNamespace(content=content)
    choice = types.SimpleNamespace(message=message)
    return types.SimpleNamespace(choices=[choice])


# ── _extract_relevant_content (line 996) ──────────────────────────────────

class TestExtractRelevantContentNoneGuard:
    """tools/browser_tool.py — _extract_relevant_content()"""

    def test_none_content_falls_back_to_truncated(self):
        """When LLM returns None content, should fall back to truncated snapshot."""
        with patch("tools.browser_tool.call_llm", return_value=_make_response(None)), \
             patch("tools.browser_tool._get_extraction_model", return_value="test-model"):
            from tools.browser_tool import _extract_relevant_content
            result = _extract_relevant_content("This is a long snapshot text", "find the button")

        assert result is not None
        assert isinstance(result, str)
        assert len(result) > 0


    def test_empty_string_content_falls_back(self):
        """Empty string content should also fall back to truncated."""
        with patch("tools.browser_tool.call_llm", return_value=_make_response("   ")), \
             patch("tools.browser_tool._get_extraction_model", return_value="test-model"):
            from tools.browser_tool import _extract_relevant_content
            result = _extract_relevant_content("This is a long snapshot text", "task")

        assert result is not None
        assert len(result) > 0


# ── browser_vision (line 1626) ────────────────────────────────────────────

class TestBrowserVisionNoneGuard:
    """tools/browser_tool.py — browser_vision() analysis extraction"""

    def test_none_content_produces_fallback_message(self):
        """When LLM returns None content, analysis should have a fallback message."""
        response = _make_response(None)
        analysis = (response.choices[0].message.content or "").strip()
        fallback = analysis or "Vision analysis returned no content."

        assert fallback == "Vision analysis returned no content."

    def test_normal_content_passes_through(self):
        """Normal analysis content should pass through unchanged."""
        response = _make_response("  The page shows a login form.  ")
        analysis = (response.choices[0].message.content or "").strip()
        fallback = analysis or "Vision analysis returned no content."

        assert fallback == "The page shows a login form."


# ── source line verification ──────────────────────────────────────────────

class TestBrowserSourceLinesAreGuarded:
    """EVERY ``…message.content`` read in browser_tool.py is None-guarded.

    This is a module-wide property — a NEW unguarded call site must fail it —
    which is why it is checked statically instead of only behaviourally. The
    two behavioural classes above pin the two sites that exist today; this pins
    that no third one appears unguarded.

    It is PARSED, not grepped. The two assertions it replaces were hand-copied
    line spellings — ``"return response.choices[0].message.content\\n"`` and
    ``"analysis = response.choices[0].message.content\\n"`` — and the first
    named a shape the module has never had, because that site ASSIGNS rather
    than returns. Deleting the production guard left it GREEN: a vacuous test
    standing exactly where a regression gate was supposed to be. A substring
    scan also breaks on any reformatting, which is the same defect in slower
    motion. Resolved AST nodes have neither failure mode.
    """

    @staticmethod
    def _unguarded_content_reads() -> list[int]:
        """Return the line numbers of un-None-guarded ``.message.content`` reads.

        A read counts as guarded when it is the LEFT operand of an ``or`` —
        i.e. the ``(… or "")`` form — which is the shape the fallback relies on.
        """
        import ast
        import pathlib

        from tools import browser_tool

        tree = ast.parse(
            pathlib.Path(browser_tool.__file__).read_text(encoding="utf-8")
        )
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        unguarded: list[int] = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute) and node.attr == "content"):
                continue
            # Only LLM-response reads: `<...>.message.content`.
            if not (
                isinstance(node.value, ast.Attribute)
                and node.value.attr == "message"
            ):
                continue
            parent = parents.get(node)
            guarded = (
                isinstance(parent, ast.BoolOp)
                and isinstance(parent.op, ast.Or)
                and parent.values[0] is node
            )
            if not guarded:
                unguarded.append(node.lineno)
        return unguarded

    def test_every_message_content_read_is_none_guarded(self):
        unguarded = self._unguarded_content_reads()
        assert not unguarded, (
            "browser_tool.py reads an LLM response `.message.content` without "
            'the None guard at line(s) '
            f'{unguarded} — reasoning-only models (DeepSeek-R1, QwQ) return '
            'content=None there. Apply the `(… or "").strip()` form.'
        )
