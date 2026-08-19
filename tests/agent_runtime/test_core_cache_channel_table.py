"""THE GATE on the core-cache receipt channel table (ML-14 / C22).

``agent_runtime.core_cache`` emits eleven receipt shapes on one logger, out of
three reason vocabularies that all word themselves ``reason=``. Two spellings
COLLIDE across two events — ``fingerprint_unavailable`` and
``build_stamp_unknown`` are emitted by the write lane and by the read lane's
demote alike — so a census that greps a reason without its family token counts
two different facts as one. That is the class ML-6 retired launcher-side, where
one refusal was worded ``patch_gap:`` on one channel and ``REFUSED gap:`` on the
other and a census MEASURED a false zero. A measured false zero is worse than no
census at all.

The module docstring's table is the index that makes a census possible. A table
is only worth its length if it cannot silently drift from the code, so this file
drives it both directions and from both ends:

1. **Completeness, forward** — every token the writers can emit (every
   ``RECEIPT_``/``DEMOTE_``/``REFUSAL_``/``DIFF_``/``CORE_SOURCE_`` constant,
   plus every ``reason=<token>`` hand-worded into a log format string) is named
   in some row.
2. **Completeness, reverse** — every bare token the table names exists in that
   derived set, and every family the table names is a family some line actually
   leads with. A row for a receipt nobody emits is a census instruction that
   returns zero forever, which is the same defect wearing the other face.
3. **The rendered line, not the constant** — the writers are DRIVEN and the
   emitted text is matched against the spelling this table tells an operator to
   grep. Direction 1 reads the constants, so a line that hand-worded its token
   would satisfy it; only this one catches that.
4. **The collision, driven from both sides** — the two events that share
   ``reason=fingerprint_unavailable`` are emitted and their family tokens
   compared, so the table's discriminator claim is a fact rather than prose.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

import pytest

from agent_runtime import core_cache


#: The prefixes that name this lane's receipt vocabulary. Anything a writer can
#: put on a line as a typed token lives behind one of them.
_VOCABULARY_PREFIXES = ("RECEIPT_", "DEMOTE_", "REFUSAL_", "DIFF_", "CORE_SOURCE_")

_LOGGER_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}

#: A reason hand-worded straight into a format string (``reason=serialize``)
#: rather than passed as a constant. These are exactly the tokens direction 1
#: would otherwise miss, so they are derived from the source, not listed.
_HAND_WORDED_REASON = re.compile(r"reason=([a-z][a-z0-9_]*)")

#: A bare snake_case token inside a table cell.
_BARE_TOKEN = re.compile(r"^[a-z][a-z0-9_]*$")

#: A token spelled with the FIELD that carries it — ``reason=entries_io``,
#: ``diff_scope=every_pass``, ``core_source=cache``.
#:
#: **This was the reverse direction's hole, found by H3/MCF-21 and measured.**
#: The gate below resolved bare spans only, so a row naming its token in this
#: spelling was invisible to it — and that is the spelling C22 EXISTS to index,
#: since the whole filing was "three ``reason=`` vocabularies on one logger, two
#: of them colliding". Proven at the moment the ``entries=false`` row was retired:
#: with the writer deleted, re-adding the row spelled ``reason=entries_io`` left
#: all eleven cases green, while the identical row spelled ``entries_io``
#: reddened this one. So the row for a retired reason could survive forever —
#: "a census instruction that returns zero forever", which is the exact defect
#: this direction's own docstring says it exists to prevent.
_FIELD_TOKEN = re.compile(r"^[a-z][a-z0-9_]*=([a-z][a-z0-9_]*)$")

#: ``ok=true``, ``stale=true``, ``entries=false``. The lane's BOOLEAN fields, and
#: the one ``key=value`` shape whose right-hand side is not a vocabulary token —
#: resolving them would make the gate demand a writer for ``true``. Listed rather
#: than pattern-matched, because "which fields are booleans" is a fact about this
#: lane and not a syntactic property.
_BOOLEAN_FIELD_VALUES = frozenset({"true", "false"})


def _named_token(span: str) -> str | None:
    """The vocabulary token a table cell's ``…``-quoted span names, if any.

    Anything with a dot, a slash, a space or a capital is a field path, a file, a
    sentence or a class name — none of which is a vocabulary token, and each of
    which the reverse direction must not try to resolve.
    """

    if _BARE_TOKEN.match(span):
        return span
    field = _FIELD_TOKEN.match(span)
    if field is not None and field.group(1) not in _BOOLEAN_FIELD_VALUES:
        return field.group(1)
    return None


# --------------------------------------------------------------------------- #
# What the code can emit
# --------------------------------------------------------------------------- #
def _module_source() -> str:
    return Path(core_cache.__file__).read_text(encoding="utf-8")


def _log_format_strings() -> list[str]:
    """The first (format) argument of every ``logger.<level>(...)`` call.

    AST rather than a regex, per the repo rule for structural gates: this
    module's docstring quotes its own log lines a dozen times, and a text scan
    would read the documentation as the code it is documenting.
    """

    found: list[str] = []
    for node in ast.walk(ast.parse(_module_source())):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _LOGGER_METHODS:
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "logger"):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.append(first.value)
    return found


def _families() -> set[str]:
    """The leading token of every emitted line — the census discriminator."""

    return {
        fmt.split()[0]
        for fmt in _log_format_strings()
        if fmt.split() and fmt.split()[0].startswith("snapshot_")
    }


def _vocabulary() -> set[str]:
    constants = {
        value
        for name, value in vars(core_cache).items()
        if name.startswith(_VOCABULARY_PREFIXES) and isinstance(value, str)
    }
    hand_worded = {
        match
        for fmt in _log_format_strings()
        for match in _HAND_WORDED_REASON.findall(fmt)
    }
    return constants | hand_worded


# --------------------------------------------------------------------------- #
# What the table says
# --------------------------------------------------------------------------- #
def _table_rows() -> list[tuple[str, str, str]]:
    """``(receipt, second channel, what to grep there)`` per row."""

    rows: list[tuple[str, str, str]] = []
    for line in (core_cache.__doc__ or "").splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) != 3:
            continue
        if set(cells[0]) <= set("-: "):  # the header separator
            continue
        if cells[0].startswith("receipt"):  # the header itself
            continue
        rows.append((cells[0], cells[1], cells[2]))
    return rows


def _quoted_spans() -> list[str]:
    """Every ``…``-quoted span inside the ROWS.

    Rows only: the prose around the table legitimately names things that are not
    this lane's vocabulary (``label_core``, ``persona_chat_mints``, the other
    receipt families the scope paragraph rules out), and a reverse check that
    tried to resolve those would be a check about paragraphs.
    """

    spans: list[str] = []
    for row in _table_rows():
        for cell in row:
            spans.extend(re.findall(r"``(.+?)``", cell))
    return spans


def _row_families() -> set[str]:
    return {
        span.split()[0]
        for span in _quoted_spans()
        if span.startswith("snapshot_") and span.split()
    }


# --------------------------------------------------------------------------- #
# Anti-vacuity: the parsers found a real table over real code
# --------------------------------------------------------------------------- #
def test_the_parsers_are_reading_something():
    """A parser that resolved nothing would pass every gate below forever."""

    rows = _table_rows()
    assert len(rows) >= 11, f"only {len(rows)} table rows parsed out of the docstring"
    assert len(_log_format_strings()) >= 14, "the logger-call walker found almost nothing"
    assert len(_vocabulary()) >= 20, f"the vocabulary is suspiciously small: {_vocabulary()}"
    assert len(_families()) >= 4, f"families: {_families()}"
    # Both spellings a row can use must resolve to the same token, or the reverse
    # direction is blind to whichever one it cannot read — which is what it was,
    # for the ``reason=`` spelling, until H3 (see :data:`_FIELD_TOKEN`).
    assert _named_token("entries_io") == "entries_io"
    assert _named_token("reason=entries_io") == "entries_io"
    assert _named_token("ok=false") is None, "a boolean field is not a vocabulary token"
    assert _named_token('parity.core_source == "cache"') is None


# --------------------------------------------------------------------------- #
# 1 + 2: completeness, both directions
# --------------------------------------------------------------------------- #
def test_every_token_a_writer_can_emit_is_named_in_the_table():
    """FORWARD. Adding a receipt means adding a row."""

    spans = _quoted_spans()
    missing = sorted(
        token
        for token in _vocabulary()
        if not any(re.search(rf"\b{re.escape(token)}\b", span) for span in spans)
    )

    assert missing == [], (
        "these tokens are emitted (or emittable) by agent_runtime/core_cache.py "
        f"and no row of its channel table names them: {missing}\n\n"
        "A token with no row is a fact an operator can only find by reading the "
        "source — which is the state C22 filed: three `reason=` vocabularies on "
        "one logger, two of them colliding, and no single place saying which "
        "family carries which. Add the row."
    )


def test_every_token_the_table_names_is_one_a_writer_emits():
    """REVERSE. A row for a dead kind is a census instruction returning zero."""

    vocabulary = _vocabulary()
    # Family tokens are snake_case too, and they are checked — more strictly,
    # by set equality — in the test below. Resolving them here as well would
    # make one drift red two tests with two different reasons.
    dead = sorted(
        {
            token
            for span in _quoted_spans()
            if not span.startswith("snapshot_")
            for token in [_named_token(span)]
            if token is not None and token not in vocabulary
        }
    )

    assert dead == [], (
        f"the channel table names these tokens, and no writer emits them: {dead}\n\n"
        "A row naming a kind nothing emits is worse than a missing row: it tells "
        "a census to grep something that returns zero forever, and a measured "
        "zero reads as evidence."
    )


def test_every_family_the_table_names_is_a_family_a_line_leads_with():
    """The first column IS the discriminator, so it must be the real one."""

    assert _row_families() == _families(), (
        f"table families {sorted(_row_families())} != emitted families "
        f"{sorted(_families())}"
    )


# --------------------------------------------------------------------------- #
# 3: the RENDERED line carries the spelling the table tells you to grep
# --------------------------------------------------------------------------- #
def _lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records]


def test_the_bound_refusal_renders_the_spelling_the_table_documents(caplog):
    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        core_cache._receipt_fingerprint_refused(
            scope=core_cache.REFUSAL_SCOPE_STORE_ROOT, root="/store", bound=7
        )

    line = "\n".join(_lines(caplog))
    assert "snapshot_core_cache fingerprint_refused" in line, line
    assert "reason=entries_exceeded" in line, line
    assert "scope=store_root" in line, line


@pytest.mark.parametrize(
    ("common", "last", "expected"),
    [
        (frozenset({"/a"}), ("/a",), "diff_scope=every_pass"),
        (frozenset(), ("/a",), "diff_scope=last_pair"),
        (None, None, "diff_scope=none"),
    ],
)
def test_the_never_converged_receipt_renders_its_census_discriminator(
    caplog, common, last, expected
):
    """C22(i)'s rule, driven: the token alone over-reports, ``diff_scope=`` is
    what separates self-perturbation from a store that is simply moving. All
    three arms must render, or the rule the table states is unfollowable."""

    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        core_cache._receipt_never_converged(builds=3, common=common, last=last)

    line = "\n".join(_lines(caplog))
    assert "snapshot_core_cache never_converged" in line, line
    assert expected in line, line


def test_the_no_entries_arm_says_so_in_its_own_words(caplog):
    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        core_cache._receipt_never_converged(builds=3, common=None, last=None)

    line = "\n".join(_lines(caplog))
    assert "diff_reason=no_entries" in line, line
    assert "diff=diff_unavailable" in line, line


def test_the_demote_renders_the_family_and_the_reason(caplog):
    with caplog.at_level(logging.INFO, logger="agent_runtime.core_cache"):
        core_cache._log_demote(
            caller="probe", reason=core_cache.DEMOTE_FINGERPRINT_UNAVAILABLE, key=None
        )

    line = "\n".join(_lines(caplog))
    assert "snapshot_core_cache core_source=rebuilt" in line, line
    assert "reason=fingerprint_unavailable" in line, line


# --------------------------------------------------------------------------- #
# 4: the collision, driven from BOTH sides
# --------------------------------------------------------------------------- #
def test_the_two_events_that_share_a_reason_are_told_apart_by_their_family(
    caplog, monkeypatch
):
    """The table's central claim, made a fact.

    ``reason=fingerprint_unavailable`` is emitted by the WRITE lane and by the
    READ lane's demote. If both lines were greppable only by their reason, a
    census asking "how often did the cache demote for a missing fingerprint?"
    would silently include every write that refused for one. Both are emitted
    here and the discriminator is asserted to be present and different.
    """

    # The write lane, driven to its fingerprint refusal without touching the
    # filesystem: a stamp exists, the fingerprint does not.
    monkeypatch.setattr(core_cache, "build_stamp_token", lambda: "stamp")
    monkeypatch.setattr(core_cache, "build_input_fingerprint", lambda: None)

    with caplog.at_level(logging.INFO, logger="agent_runtime.core_cache"):
        assert core_cache.write_back({}, fingerprint=None) is False
        core_cache._log_demote(
            caller="probe", reason=core_cache.DEMOTE_FINGERPRINT_UNAVAILABLE, key=None
        )

    sharing = [
        line for line in _lines(caplog) if "reason=fingerprint_unavailable" in line
    ]
    assert len(sharing) == 2, sharing

    families = {line.split()[0] for line in sharing}
    assert families == {"snapshot_core_cache_write", "snapshot_core_cache"}, (
        "the two events sharing reason=fingerprint_unavailable no longer differ "
        f"in their family token: {families}. The table's discriminator is gone, "
        "and a census keyed on the reason now counts them as one fact."
    )
