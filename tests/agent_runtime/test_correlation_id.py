"""EG-2.3 / Plan D — the end-to-end correlation id, hermes half.

The stage's whole claim is that ONE grep over two logs answers "which RPC
produced this launcher update", replacing the timestamp anchoring that produced
a confidently wrong diagnosis (Plan D's opening: anchoring on the launcher's
flush receipt yielded "deletes take 3.8 s" for writes that take 280–368 ms).
This suite proves the hermes half of that grep, in the order Plan D stages it:

**CI-0 — the neutrality claims, pinned BEFORE anything else.** Two of them, and
both are tests of DELIVERED behaviour rather than of this stage's code:

* a payload carrying ``correlation_id`` round-trips the EXISTING frame builders
  into ``entity.correlation_id`` (delta lane) and into the patch ROW (patch
  lane). Nothing in this stage touches ``stream.py``; the slot was already wired
  and contracted, and 60% of the pipe was therefore already shipped.
* absent-when-unset is BYTE-identical to before the key existed. This is the
  fence that keeps the change additive: the golden below is the literal payload
  dict, so a producer that stamped a token unconditionally reds here.

**CI-1 — the write path threads it.** One office write, and the token must be on
BOTH halves of the pair it emits (the domain event AND the ``state.patched``
row), because either alone leaves a diagnostician joining lanes by timestamp for
the other. Plus the 4 KB accounting: the id rides INSIDE the shrink loop, so a
payload at the cap degrades honestly instead of overflowing.

**CI-2 (hermes half) — the serve child says so out loud.** ``office_write`` is
the line the serve child never had; §8 item 5 measured 12 MB of serve-child log
with ZERO office lines.

**CI-3 — the acceptance.** One grep over the joined log yields the ordered
witness for a single token. The launcher's two receipt lines are fenced by the
launcher's own suite (`mission_office_correlation_test.dart`); what is proven
here is that the hermes lines carry the same token, in order, findable by one
pattern.

Every test names the mutation it kills, because a diagnostic field is exactly
the kind of thing that can be present, asserted, and useless.
"""

from __future__ import annotations

import json
import logging
import re

from agent_runtime import serve_rpc
from agent_runtime.models import Event
from agent_runtime.state_patches import (
    CORRELATION_ID_KEY,
    CORRELATION_ID_MAX_LEN,
    PATCH_OP_REFRESH,
    PATCH_OP_REMOVE,
    PATCH_OP_UPSERT,
    build_state_patch,
    normalize_correlation_id,
)
from tests.agent_runtime.test_serve_rpc_office import SHUTDOWN, _reply, _rpc, _run
from tests.agent_runtime.office_seed import seed_workspace_record

WORKSPACE = "ws_corr_test"

#: The instance-bound actor the seed places, canonicalized by the store.
QA_INSTANCE = "personainst_qa_agent_9c8a382f"

#: A token in the shape the launcher mints: ``g-<lane>-<micros>-<rand4>``.
TOKEN = "g-office-1755400000123456-a1b2"


# ── seeding ─────────────────────────────────────────────────────────────────


def _store():
    from agent_runtime.office_store import OfficeStore

    return OfficeStore()


def _actor_payload(x: float, y: float) -> dict:
    return {
        "persona_id": "qa",
        "persona_instance_id": QA_INSTANCE,
        "items": [
            {
                "item_id": QA_INSTANCE,
                "kind": "agent",
                "position": [x, y],
                "folder": "Agents",
                "display_name": "QA Agent",
            }
        ],
    }


def _seed():
    store = _store()
    seed_workspace_record(WORKSPACE)
    store.ensure_surface(WORKSPACE, created_by="seed")
    store.upsert_actor(WORKSPACE, _actor_payload(-8.0, -2.0), updated_by="seed-operator")
    return store


def _events(_store_unused=None) -> list[Event]:
    """Every event in the isolated log, oldest first — the suite's own read.

    Through a fresh ``EventLog`` rather than a store's handle (the sibling office
    suites' pattern): the RPC handlers construct their own ``OfficeStore``, so a
    seed store's handle names a different reader of the same file and would only
    happen to agree.
    """

    from agent_runtime.events import EventLog

    return [event for _, event in EventLog().iter_from_offset(0)]


def _payloads(_store_unused, event_type: str) -> list[dict]:
    return [event.payload for event in _events() if event.type == event_type]


def _upsert(rid: str, params: dict) -> dict:
    return _reply(_run([_rpc(rid, "runtime.office.upsert", params), SHUTDOWN]), rid)


def _remove(rid: str, params: dict) -> dict:
    return _reply(_run([_rpc(rid, "runtime.office.remove", params), SHUTDOWN]), rid)


def _surface_update(rid: str, params: dict) -> dict:
    return _reply(
        _run([_rpc(rid, "runtime.office.surface.update", params), SHUTDOWN]), rid
    )


# ── CI-0(a): the delivered read side, proven with a hand-built event ─────────


def test_a_payload_key_reaches_entity_correlation_id_on_the_delta_lane():
    """DELIVERED behaviour, asserted before this stage's producers exist.

    ``stream._delta_entity`` lifts ``payload["correlation_id"]`` onto the frame's
    entity block, and the contract (`mission-control-stream.md`) names it. This
    test is what makes "60% of the pipe already shipped" a fact rather than a
    reading of the code — and it is why CI-1 attaches the id to a PAYLOAD instead
    of inventing an envelope field.

    *Mutation:* strip ``correlation_id`` in ``_redaction_safe_json`` → the entity
    block answers None and this reds. (That is the kill Plan D §CI-0 names; note
    it lives in ``stream.py``, which this stage does not touch — the mutation was
    run and reverted, not committed.)
    """

    from agent_runtime.stream import delta_frame

    event = Event(
        ts="2026-08-17T00:00:00Z",
        type="office.actor.upserted",
        task_id=None,
        run_id=None,
        persona_id=None,
        payload={"workspace_id": WORKSPACE, CORRELATION_ID_KEY: TOKEN},
    )

    frame = delta_frame(event, offset=7, snapshot={})

    assert frame["entity"][CORRELATION_ID_KEY] == TOKEN
    # And on the per-event list a coalesced demote carries, which is the half
    # that answers "which gestures contributed to this rebuilt core" (§V5).
    from agent_runtime.stream import delta_batch_frame

    batch = delta_batch_frame([(6, event), (7, event)], snapshot={})
    assert [entry[CORRELATION_ID_KEY] for entry in batch["events"]] == [TOKEN, TOKEN]


def test_a_payload_key_reaches_the_patch_row_verbatim_on_the_patch_lane():
    """The other delivered half: patch rows are a verbatim payload SPREAD, so a
    payload key is a row key with no builder change at all.

    *Mutation:* replace the ``**payload`` spread with an explicit key list →
    the row loses the token and this reds.
    """

    from agent_runtime.stream import patch_batch_frame

    event = Event(
        ts="2026-08-17T00:00:00Z",
        type="state.patched",
        task_id=None,
        run_id=None,
        persona_id=None,
        payload=build_state_patch(
            "office_actor",
            f"{WORKSPACE}/{QA_INSTANCE}",
            PATCH_OP_REMOVE,
            correlation_id=TOKEN,
        ),
    )

    frame = patch_batch_frame([(9, event)], base_offset=8)

    assert frame["patches"][0][CORRELATION_ID_KEY] == TOKEN


# ── CI-0(b): absent-when-unset, as a literal golden ─────────────────────────


def test_a_patch_built_without_a_token_is_byte_identical_to_the_pre_stage_shape():
    """The neutrality golden — the fence that keeps this change additive.

    Written as the LITERAL payload dict rather than as ``CORRELATION_ID_KEY not
    in payload``: the weaker form passes against a producer that added any other
    key too, and Plan D's whole cost model rests on existing payloads not moving
    a byte (no fixture regeneration, no ``decision_contract_hash`` move, nothing
    to mirror cross-stack).

    *Mutation:* default the ``correlation_id`` kwarg to a generated value
    (``normalize_correlation_id(...) or _mint()``) → every arm below reds.
    """

    actor_id = f"{WORKSPACE}/{QA_INSTANCE}"

    assert build_state_patch("office_actor", actor_id, PATCH_OP_REMOVE) == {
        "entity": "office_actor",
        "id": actor_id,
        "op": "remove",
    }
    assert build_state_patch("office_actor", actor_id, PATCH_OP_REFRESH) == {
        "entity": "office_actor",
        "id": actor_id,
        "op": "refresh",
    }
    assert build_state_patch(
        "office_surface", WORKSPACE, PATCH_OP_UPSERT, {"folders": ["Agents"]}
    ) == {
        "entity": "office_surface",
        "id": WORKSPACE,
        "op": "upsert",
        "changed": {"folders": ["Agents"]},
    }
    # And the ``created`` sibling still lands in its documented position, so the
    # new key cannot have been threaded by reordering the assembly.
    assert build_state_patch(
        "office_actor", actor_id, PATCH_OP_UPSERT, {"revision": 2}, True
    ) == {
        "entity": "office_actor",
        "id": actor_id,
        "op": "upsert",
        "changed": {"revision": 2},
        "created": True,
    }


def test_the_key_lands_beside_created_without_disturbing_it():
    """Both additive keys at once — the shape a create GESTURE produces.

    Asserted as a whole dict because the interesting failure is an id that
    displaced ``created`` (the coverage gate's input): a client that stopped
    being told the row was new would answer the widened op with a re-hydrate.
    """

    actor_id = f"{WORKSPACE}/{QA_INSTANCE}"

    assert build_state_patch(
        "office_actor", actor_id, PATCH_OP_UPSERT, {"revision": 1}, True, TOKEN
    ) == {
        "entity": "office_actor",
        "id": actor_id,
        "op": "upsert",
        "changed": {"revision": 1},
        "created": True,
        CORRELATION_ID_KEY: TOKEN,
    }


# ── the token's own boundary ────────────────────────────────────────────────


def test_only_a_generated_looking_token_survives_normalization():
    """Charset + length, refused rather than sanitized (Plan D §6: "the id
    becomes a covert channel for text").

    A repaired id would print a value neither side used, which is worse than no
    id — so every rejection answers None and the payload stays clean.

    *Mutation:* drop the regex check and keep only the length cap → the prose and
    newline arms red.
    """

    assert normalize_correlation_id(TOKEN) == TOKEN
    assert normalize_correlation_id("  " + TOKEN + "  ") == TOKEN
    assert normalize_correlation_id("g" * CORRELATION_ID_MAX_LEN) == "g" * CORRELATION_ID_MAX_LEN

    assert normalize_correlation_id(None) is None
    assert normalize_correlation_id("") is None
    assert normalize_correlation_id("   ") is None
    assert normalize_correlation_id("g" * (CORRELATION_ID_MAX_LEN + 1)) is None
    assert normalize_correlation_id("the operator dragged qa to the left") is None
    assert normalize_correlation_id("g-office-1\n2") is None
    assert normalize_correlation_id("password=hunter2") is None
    assert normalize_correlation_id(12345) is None
    assert normalize_correlation_id({"id": TOKEN}) is None


def test_an_illegal_token_can_never_reach_a_payload_even_from_an_in_process_caller():
    """The payload-side fence, exercised through the emitter rather than the
    validator — ``agent_create``'s own parser admits 200 characters, which is
    looser than the cap, so the re-check has to live where every producer passes.

    *Mutation:* pass ``correlation_id`` straight into ``build_state_patch``
    without normalizing → the long-token arm reds with a 200-char id on the wire.
    """

    from agent_runtime.events import EventLog
    from agent_runtime.state_patches import emit_office_actor_remove

    log = EventLog()
    assert emit_office_actor_remove(log, WORKSPACE, QA_INSTANCE, correlation_id="x" * 200)
    assert emit_office_actor_remove(
        log, WORKSPACE, QA_INSTANCE, correlation_id="drag: qa desk"
    )
    rows = [event.payload for event in _events() if event.type == "state.patched"]
    assert len(rows) == 2
    for payload in rows:
        assert CORRELATION_ID_KEY not in payload


# ── CI-1: one write, both halves of the pair ────────────────────────────────


def test_one_upsert_puts_the_token_on_the_domain_event_and_the_paired_patch():
    """The CI-1 claim, and the reason it is asserted on BOTH halves.

    The office lane forwards ``state.patched`` rows; the stream lane's demote
    carries the DOMAIN events. A token on only one of them leaves whichever lane
    the client actually took joining by timestamp — the exact failure this stage
    exists to remove.

    *Mutation (run, observed, reverted):* drop ``correlation_id=correlation_id``
    from ``_emit_actor_patch``'s call → the patch arm reds while the domain arm
    stays green, which is precisely the half-threaded state a single-arm test
    would have shipped.
    """

    store = _seed()

    reply = _upsert(
        "c-upsert",
        {
            "workspace_id": WORKSPACE,
            "actor": _actor_payload(3.5, 9.0),
            "correlation_id": TOKEN,
        },
    )
    assert reply["result"]["actor_key"] == QA_INSTANCE

    fresh = _store()
    upserted = _payloads(fresh, "office.actor.upserted")
    patched = [
        payload
        for payload in _payloads(fresh, "state.patched")
        if payload.get("entity") == "office_actor"
    ]
    # The seed wrote one of each without a token; the RPC wrote the second.
    assert [payload.get(CORRELATION_ID_KEY) for payload in upserted] == [None, TOKEN]
    assert [payload.get(CORRELATION_ID_KEY) for payload in patched] == [None, TOKEN]


def test_one_remove_puts_the_token_on_the_removal_event_and_the_remove_row():
    """The delete gesture's half — the gesture whose latency was misdiagnosed.

    *Mutation:* drop the kwarg from ``_archive_actor_locked``'s
    ``_emit_actor_remove_patch`` call → the row arm reds.
    """

    _seed()

    _remove(
        "c-remove",
        {
            "workspace_id": WORKSPACE,
            "actor_key": QA_INSTANCE,
            "correlation_id": TOKEN,
        },
    )

    fresh = _store()
    removed = _payloads(fresh, "office.actor.removed")
    rows = [
        payload
        for payload in _payloads(fresh, "state.patched")
        if payload.get("op") == PATCH_OP_REMOVE
    ]
    assert [payload.get(CORRELATION_ID_KEY) for payload in removed] == [TOKEN]
    assert [payload.get(CORRELATION_ID_KEY) for payload in rows] == [TOKEN]


def test_a_folder_write_puts_the_token_on_the_surface_event_and_the_surface_row():
    """The third write verb, and the one EG-1.3 just made foldable — a
    folder-only batch used to vanish with neither patch nor resync, so its
    attribution is worth having from the first day it can be folded at all.
    """

    _seed()

    _surface_update(
        "c-folders",
        {
            "workspace_id": WORKSPACE,
            "folders": ["Agents", "Desks", "Ops"],
            "correlation_id": TOKEN,
        },
    )

    fresh = _store()
    updated = _payloads(fresh, "office.surface.updated")
    rows = [
        payload
        for payload in _payloads(fresh, "state.patched")
        if payload.get("entity") == "office_surface"
    ]
    assert [payload.get(CORRELATION_ID_KEY) for payload in updated] == [TOKEN]
    assert [payload.get(CORRELATION_ID_KEY) for payload in rows] == [TOKEN]


def test_an_office_write_with_no_token_leaves_both_halves_byte_identical():
    """Absent-when-unset at the STORE boundary, not just at the builder.

    The whole-payload equality is the point: a producer that stamped an empty
    string, or a ``correlation_id: null``, would pass a containment check and
    would put a new key on every event in the field.

    *Mutation:* have ``_emit`` write the key unconditionally → both arms red.
    """

    _seed()
    _upsert("c-bare", {"workspace_id": WORKSPACE, "actor": _actor_payload(1.0, 1.0)})

    fresh = _store()
    for payload in _payloads(fresh, "office.actor.upserted"):
        assert set(payload) == {
            "workspace_id",
            "actor_key",
            "persona_id",
            "items",
            "revision",
        }
    for payload in _payloads(fresh, "state.patched"):
        assert CORRELATION_ID_KEY not in payload


def test_the_token_is_accounted_inside_the_four_kilobyte_shrink_loop():
    """The sizing claim, re-measured rather than trusted (the ``created``
    precedent's own test shape).

    A ``changed`` map deliberately sized so the assembled payload clears the cap
    WITHOUT the token and would exceed it WITH one: the loop must mark one more
    value and still land inside the cap, never emit an over-cap payload that
    ``EventLog.append`` would refuse.

    *Mutation:* assemble the id AFTER the loop instead of inside it → the size
    assertion reds with a payload past 4096 bytes.
    """

    from agent_runtime.events import EVENT_PAYLOAD_LIMIT_BYTES

    def size(payload: dict) -> int:
        return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    # Eight 478-byte values: each comfortably under the 3,584-byte per-value
    # budget, and together 4,051 bytes assembled — 45 bytes under the cap, which
    # is LESS than the token's own serialized width. Chosen by measurement, not
    # by feel: the whole point is a payload the token cannot fit into unchanged.
    changed = {f"field_{index}": "v" * 478 for index in range(8)}
    actor_id = f"{WORKSPACE}/{QA_INSTANCE}"

    bare = build_state_patch("office_actor", actor_id, PATCH_OP_UPSERT, dict(changed))
    with_token = build_state_patch(
        "office_actor", actor_id, PATCH_OP_UPSERT, dict(changed), None, TOKEN
    )

    assert size(bare) <= EVENT_PAYLOAD_LIMIT_BYTES
    assert size(with_token) <= EVENT_PAYLOAD_LIMIT_BYTES
    assert with_token[CORRELATION_ID_KEY] == TOKEN
    # The token cost something real — the loop marked a value the bare payload
    # carried inline. Without this the test would pass against a payload that
    # simply had room, proving nothing about the accounting.
    bare_markers = sum(
        1 for value in bare["changed"].values() if isinstance(value, dict)
    )
    token_markers = sum(
        1 for value in with_token["changed"].values() if isinstance(value, dict)
    )
    assert (bare_markers, token_markers) == (0, 1)


def test_a_refresh_degrade_keeps_the_token_because_that_is_what_names_the_cause():
    """``refresh`` is the demote's own row, and "which gesture forced a full
    core" is the single most valuable thing to know about one — so the degrade is
    the one path that must NOT drop the id (Plan D §V5's honest half).

    *Mutation:* pass ``None`` on the loop's exhausted-inline degrade → reds.
    """

    from agent_runtime.state_patches import PATCH_VALUE_BUDGET_BYTES

    actor_id = f"{WORKSPACE}/{QA_INSTANCE}"

    # (a) the EXHAUSTED-INLINE degrade — every value is already an oversize
    # marker and the assembled markers still overflow. A hundred over-budget
    # fields is what it takes to reach that line, which is why it is spelled out
    # rather than approximated: a single huge value is marked and FITS, so a
    # test that used one would assert against an ``upsert`` and prove nothing
    # about this branch.
    exhausted = build_state_patch(
        "office_actor",
        actor_id,
        PATCH_OP_UPSERT,
        {f"f{index}": "v" * (PATCH_VALUE_BUDGET_BYTES + 10) for index in range(100)},
        None,
        TOKEN,
    )
    assert exhausted["op"] == PATCH_OP_REFRESH
    assert exhausted[CORRELATION_ID_KEY] == TOKEN
    # No ``changed`` on a refresh, and no ``created`` either — the id is the ONE
    # additive key a degrade keeps.
    assert set(exhausted) == {"entity", "id", "op", CORRELATION_ID_KEY}

    # (b) the empty-``changed`` degrade, the other arm that rewrites the op.
    empty = build_state_patch("office_actor", actor_id, PATCH_OP_UPSERT, None, None, TOKEN)
    assert empty["op"] == PATCH_OP_REFRESH
    assert empty[CORRELATION_ID_KEY] == TOKEN


# ── the RPC boundary ────────────────────────────────────────────────────────


def test_every_write_verb_echoes_the_token_back_and_omits_the_key_without_one():
    """The reply ECHO, which is what lets the launcher's receipt name the token
    from the SERVER's answer rather than from its own memory of what it sent —
    the difference between "we think we sent this" and "the write carried this".

    Absence is asserted as a whole-frame equality on the bare call, because that
    is the pre-stage reply shape every existing client decoder reads.
    """

    _seed()

    bare = _upsert("e-bare", {"workspace_id": WORKSPACE, "actor": _actor_payload(1.0, 1.0)})
    assert bare == {
        "jsonrpc": "2.0",
        "id": "e-bare",
        "result": {"actor_key": QA_INSTANCE, "revision": 2},
    }

    echoed = _upsert(
        "e-token",
        {
            "workspace_id": WORKSPACE,
            "actor": _actor_payload(2.0, 2.0),
            "correlation_id": TOKEN,
        },
    )
    assert echoed == {
        "jsonrpc": "2.0",
        "id": "e-token",
        "result": {
            "actor_key": QA_INSTANCE,
            "revision": 3,
            CORRELATION_ID_KEY: TOKEN,
        },
    }

    removed = _remove(
        "e-remove",
        {
            "workspace_id": WORKSPACE,
            "actor_key": QA_INSTANCE,
            "correlation_id": TOKEN,
        },
    )
    assert removed["result"][CORRELATION_ID_KEY] == TOKEN

    folders = _surface_update(
        "e-folders",
        {
            "workspace_id": WORKSPACE,
            "folders": ["Ops"],
            "correlation_id": TOKEN,
        },
    )
    assert folders["result"][CORRELATION_ID_KEY] == TOKEN


def test_a_free_text_token_is_refused_at_the_boundary_and_writes_nothing():
    """REFUSED, not dropped: this lane has a reply channel, so a client sending
    prose where a generated token belongs is told, and the store is untouched.

    The store re-read is the load-bearing half — a typed refusal in front of a
    store that took the write anyway is the worst outcome of any guard.
    """

    _seed()
    before = _store().get_actor(WORKSPACE, QA_INSTANCE).revision

    reply = _upsert(
        "e-prose",
        {
            "workspace_id": WORKSPACE,
            "actor": _actor_payload(9.0, 9.0),
            "correlation_id": "the operator dragged qa left",
        },
    )

    assert reply["error"]["code"] == serve_rpc.ERR_INVALID_PARAMS
    assert reply["error"]["data"]["reason"] == serve_rpc.CORRELATION_ID_INVALID_REASON
    assert reply["error"]["data"]["workspace_id"] == WORKSPACE
    assert _store().get_actor(WORKSPACE, QA_INSTANCE).revision == before

    # And a non-string, which is the shape a client bug produces rather than a
    # smuggling attempt — same reason, because the cure is the same.
    typed = _upsert(
        "e-int",
        {
            "workspace_id": WORKSPACE,
            "actor": _actor_payload(9.0, 9.0),
            "correlation_id": 12345,
        },
    )
    assert typed["error"]["data"]["reason"] == serve_rpc.CORRELATION_ID_INVALID_REASON
    assert _store().get_actor(WORKSPACE, QA_INSTANCE).revision == before


def test_a_retry_of_one_gesture_carries_the_same_token_on_purpose():
    """Plan D §V1/§6's retry semantics, pinned so nobody "fixes" it.

    The id is the GESTURE's, not the attempt's: two receipts under one token is
    the truth, and a diagnostician counting them LEARNS the retry happened —
    which is exactly the fact the 2026-08-16 flush-receipt error hid. Dedup
    identity stays the idempotency key's job.
    """

    _seed()

    first = _upsert(
        "r1",
        {"workspace_id": WORKSPACE, "actor": _actor_payload(4.0, 4.0), "correlation_id": TOKEN},
    )
    second = _upsert(
        "r2",
        {"workspace_id": WORKSPACE, "actor": _actor_payload(5.0, 5.0), "correlation_id": TOKEN},
    )

    assert first["result"][CORRELATION_ID_KEY] == TOKEN
    assert second["result"][CORRELATION_ID_KEY] == TOKEN
    assert second["result"]["revision"] == first["result"]["revision"] + 1
    tokens = [
        payload.get(CORRELATION_ID_KEY)
        for payload in _payloads(_store(), "office.actor.upserted")
    ]
    assert tokens == [None, TOKEN, TOKEN]


# ── CI-2 (hermes half): the serve child's own line ──────────────────────────


def _office_write_lines(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("office_write ")
    ]


def test_the_serve_child_logs_one_office_write_line_per_write_naming_the_token(caplog):
    """The line §8 item 5 says is missing — 12 MB of serve-child log with zero
    ``office`` lines is what made "is the write lane even alive?" unanswerable.

    Shaped like ``stream.log_stream_attach``'s (leading event word, ``key=value``
    after it) so an operator greps the two together instead of parsing two
    formats.

    *Mutation:* remove the ``log_office_write`` call → the CI-3 join below finds
    the token on the launcher receipt and NOTHING hermes-side, which is the
    "thread but never log" failure Plan D names.
    """

    _seed()
    with caplog.at_level(logging.INFO, logger="agent_runtime.serve_rpc"):
        _upsert(
            "l-token",
            {
                "workspace_id": WORKSPACE,
                "actor": _actor_payload(6.0, 6.0),
                "correlation_id": TOKEN,
            },
        )

    lines = _office_write_lines(caplog)
    assert len(lines) == 1
    assert lines[0] == (
        f"office_write op=runtime.office.upsert corr={TOKEN} "
        f"workspace={WORKSPACE} actor_key={QA_INSTANCE} revision=2"
    )


def test_a_write_with_no_gesture_behind_it_prints_corr_dash_rather_than_nothing(caplog):
    """Absence made VISIBLE (Plan D §CI-2's own rule). An omitted key is
    indistinguishable from an old build; ``corr=-`` says "this write had no
    gesture", which is a fact worth reading.

    Note the asymmetry with the payloads, and it is deliberate: a PAYLOAD must
    stay byte-identical (a wire contract with readers in the field), while this
    line is brand new and has no pre-stage form to preserve.
    """

    _seed()
    with caplog.at_level(logging.INFO, logger="agent_runtime.serve_rpc"):
        _remove("l-bare", {"workspace_id": WORKSPACE, "actor_key": QA_INSTANCE})

    lines = _office_write_lines(caplog)
    assert len(lines) == 1
    assert " corr=- " in lines[0]
    assert lines[0].startswith("office_write op=runtime.office.remove corr=- ")


def test_a_refused_write_logs_no_line_so_the_grep_never_shows_a_write_that_lost(caplog):
    """The receipt means "this write LANDED", never "a write was attempted" —
    the same discipline the launcher's ``fold_applied`` keeps. A line in front of
    a refusal would put a token in the join for a write the store never took, and
    the diagnostician would conclude the opposite of the truth.
    """

    _seed()
    with caplog.at_level(logging.INFO, logger="agent_runtime.serve_rpc"):
        reply = _upsert(
            "l-stale",
            {
                "workspace_id": WORKSPACE,
                "actor": _actor_payload(7.0, 7.0),
                "expect_revision": 99,
                "correlation_id": TOKEN,
            },
        )

    assert reply["error"]["data"]["reason"] == "stale_revision"
    assert _office_write_lines(caplog) == []


# ── CI-3: the acceptance — ONE grep, ordered, over the joined log ────────────


#: The launcher's two receipt lines for one office gesture, in the wire shape
#: `mission_transport_receipt.dart` serializes into
#: `diagnostics/mission_transport/receipts.jsonl`. RELAYED into this test as
#: constants rather than produced here — this process has no launcher — and
#: fenced on the other side by `mission_office_correlation_test.dart`, which
#: asserts the real log emits exactly these two kinds carrying `corr`.
#:
#: They are here because the acceptance is a JOIN, and a join asserted over one
#: side's half is the "locally-invented weaker gate" mistake CI-0 records
#: (launcher `e1d198985`: eleven by-hash checks green while the owning gate was
#: red the whole time). One grep over both logs is the gate that owns this claim.
_LAUNCHER_SEND_RECEIPT = (
    '{{"v":1,"at":"2026-08-17T04:26:40.100Z","kind":"office_write_sent",'
    '"detail":"runtime.office.upsert","corr":"{token}","dropped_before":0}}'
)
_LAUNCHER_FOLD_RECEIPT = (
    '{{"v":1,"at":"2026-08-17T04:26:40.480Z","kind":"fold_applied","offset":9,'
    '"detail":"entries:1","corr":"{token}","dropped_before":0}}'
)

#: The ordered witness ONE pattern must find: launcher send → hermes write →
#: launcher fold, all for the same token. `re.escape` on the token because the
#: minted shape contains `.`-legal characters and a diagnostician's grep must
#: match the literal id, not a pattern it happens to look like.
_WITNESS = ("office_write_sent", "office_write op=", "fold_applied")


def test_one_grep_over_the_two_logs_yields_send_write_fold_for_one_token(caplog, tmp_path):
    """**The stage's exit witness.** CI-3, as an automated test.

    What it does: one real office write through the RPC dispatcher carrying one
    minted token, hermes' own log captured, the launcher's two receipt lines
    joined beside it, and then ONE grep for the token over the joined file. The
    matches must appear in order: the launcher SENT it, hermes WROTE it, the
    launcher FOLDED it.

    Why this replaces the sanctioned diagnostic procedure: the phase table an
    operator used to build by anchoring on the flush receipt is now built by
    grepping one id, so the join no longer NEEDS clock comparison to establish
    which receipt belongs to which gesture — and the 250–650 ms flush lag that
    turned 280–368 ms deletes into "3.8 s" can no longer mis-assign a receipt.

    *Mutations, run and observed:*

    * drop the mint (send the write with no ``correlation_id``) → the hermes line
      reads ``corr=-`` and the grep finds no token on it: the witness collapses
      to the two launcher lines and the ordering assertion reds.
    * thread but never log hermes-side (remove ``log_office_write``) → the two
      launcher lines still match and the middle hop is GONE, so the two-log join
      fails exactly as Plan D predicts.
    """

    _seed()
    with caplog.at_level(logging.INFO, logger="agent_runtime.serve_rpc"):
        reply = _upsert(
            "ci3",
            {
                "workspace_id": WORKSPACE,
                "actor": _actor_payload(11.0, 4.0),
                "correlation_id": TOKEN,
            },
        )

    # The reply echo is the launcher's own proof that the token it is about to
    # print on its receipt is the one the SERVER wrote under.
    assert reply["result"][CORRELATION_ID_KEY] == TOKEN

    hermes_log = tmp_path / "serve_child.log"
    hermes_log.write_text(
        "\n".join(record.getMessage() for record in caplog.records) + "\n",
        encoding="utf-8",
    )
    launcher_log = tmp_path / "receipts.jsonl"
    launcher_log.write_text(
        _LAUNCHER_SEND_RECEIPT.format(token=TOKEN)
        + "\n"
        + _LAUNCHER_FOLD_RECEIPT.format(token=TOKEN)
        + "\n",
        encoding="utf-8",
    )

    # ONE grep over the two logs — `grep <token> receipts.jsonl serve_child.log`,
    # one pattern, one pass. Deliberately not two passes, not a parse and not a
    # sort: anything richer is the procedure this stage promised to retire.
    pattern = re.compile(re.escape(TOKEN))
    joined = [
        line
        for path in (launcher_log, hermes_log)
        for line in path.read_text(encoding="utf-8").splitlines()
        if pattern.search(line)
    ]

    # THREE hits, one per hop, and every hop present.
    assert len(joined) == 3, joined
    for witness in _WITNESS:
        assert any(witness in line for line in joined), (witness, joined)

    # ORDER, established the only honest way for two independently-appended
    # files: WITHIN a log by the log's own append order, and ACROSS logs by the
    # id itself. The launcher's send precedes its fold because that file is
    # append-only; hermes' write sits between them because the token could not
    # have reached the serve child before the launcher sent it, nor the fold
    # receipt before the row existed. Comparing the two files' TIMESTAMPS to
    # order the hops is exactly the anchoring this stage retires — the id is what
    # replaces it, so this assertion refuses to reach for a clock.
    send = next(i for i, line in enumerate(joined) if "office_write_sent" in line)
    fold = next(i for i, line in enumerate(joined) if "fold_applied" in line)
    assert send < fold, joined
    assert sum("office_write op=" in line for line in joined) == 1, joined

    # The patch row the fold receipt is accounting for carries the same token —
    # so the launcher's line is not merely correlated with the write, it is
    # correlated with the ROW the write produced.
    rows = [
        payload
        for payload in _payloads(_store(), "state.patched")
        if payload.get(CORRELATION_ID_KEY) == TOKEN
    ]
    assert len(rows) == 1


# ── CI-4's free half ────────────────────────────────────────────────────────


def test_the_agent_create_placement_joins_the_same_token_as_an_ordinary_drag():
    """CI-4's FREE half, taken (EG-2.3's own scoping): ``runtime.agent.create``
    already reserved ``correlation_id`` from birth and already echoed it on the
    result, so the only hop it was missing is the office one — and that hop is
    now a kwarg on a call the handler already makes.

    Asserted through the STORE rather than through the create handler (which
    needs a persona roster this suite does not seed): the claim under test is
    that the placement's office write threads the token, which is the line the
    create handler now passes.

    The 38 argv capability lanes stay DEFERRED — nothing here threads them, and
    the partial-coverage window Plan D §V3 names is narrowed to the roster half
    rather than closed.
    """

    store = _seed()
    store.upsert_actor(
        WORKSPACE,
        _actor_payload(12.0, 12.0),
        updated_by="agent-create",
        correlation_id=TOKEN,
    )

    fresh = _store()
    assert [
        payload.get(CORRELATION_ID_KEY)
        for payload in _payloads(fresh, "office.actor.upserted")
    ] == [None, TOKEN]
