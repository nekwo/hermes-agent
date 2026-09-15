"""One JSON-RPC call to a paired install, dialled and closed. The whole client half.

Four callers and one implementation (R-S2-12): ``agent_chat_installs`` fetching
a far roster, ``agent_chat_threads`` and ``agent_chat_open`` reading through
``@install/``, and ``gateway_announce`` pushing a change outward. Before this,
the only cross-install client was ``agent_chat_dispatch``'s — a 250-line
supervisor built around a chat turn's retry posture, its dispatch row and its
delivery promise. None of that applies to a read that has to answer inside a
model's turn, and copying the parts that did would have meant two dial
implementations disagreeing about timeouts within a month.

Why the timeouts are so much shorter than the dispatch lane's
--------------------------------------------------------------

``PEER_DIAL_TIMEOUT_SECONDS`` on the dispatch lane is 15 seconds because a
dispatch has somewhere to wait: a durable row, a background supervisor and a
delivery that lands in the sender's conversation whenever it is ready. These
reads have nowhere to wait — they run INSIDE a model's turn, and a tool that
blocks a turn for fifteen seconds has spent more of the operator's attention
than the answer is worth. Five seconds to dial and ten for the reply is the
budget a read gets: enough for a LAN round trip on a machine that is awake, and
short enough that an install which is switched off costs a sentence rather than
a stall.

What it returns, and why it never raises
-----------------------------------------

``{"result": {...}}`` or ``{"refusal": {"reason": ..., "message": ...}}``, and
nothing else. Every caller is a TOOL — its output is read by a model — so a
traceback here is a turn that ends in an unhandled exception where an honest
"that machine did not answer" would have let the agent do something else. The
distinction the refusal keeps is the one that matters to a caller: a transport
failure (``peer_unreachable``) is worth retrying later, and a far ``error``
reply is not.

The one mapping worth naming: a far ``-32601`` (method not found) becomes
``capability_missing`` (R-IP17's word), not ``peer_unreachable``. An install
that answers and does not know the verb is a build older than this one — a ROW
STATE, per R-IP16 — and telling a caller "unreachable" for it would send them
looking at a network that is fine.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = [
    "CAPABILITY_MISSING_REASON",
    "PEER_READ_DIAL_TIMEOUT_SECONDS",
    "PEER_READ_REPLY_TIMEOUT_SECONDS",
    "PEER_UNREACHABLE_REASON",
    "call_peer_method",
]

#: Five to dial. See the module docstring: an in-turn read has nowhere to wait.
PEER_READ_DIAL_TIMEOUT_SECONDS = 5.0
#: Ten for the reply. Longer than the dial because the far side may have to
#: resolve a workspace and read a transcript, and shorter than any budget a
#: model's turn would notice losing.
PEER_READ_REPLY_TIMEOUT_SECONDS = 10.0

#: The transport failure word, shared with the dispatch lane's own
#: (``dispatch_store.REMOTE_UNREACHABLE_REASON``) so a caller branching on it
#: does not need to know which lane answered.
PEER_UNREACHABLE_REASON = "peer_unreachable"

#: R-IP16's row state: the install answered and does not know the verb. A build
#: older than this one, which is a fact to render rather than an error to raise.
CAPABILITY_MISSING_REASON = "capability_missing"

#: JSON-RPC's own "no such method". Named rather than inlined because it is the
#: single number this module translates into a domain word.
_ERR_METHOD_NOT_FOUND = -32601


def call_peer_method(
    store_root: Path | str,
    peer_install_id: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    dial_timeout: float = PEER_READ_DIAL_TIMEOUT_SECONDS,
    reply_timeout: float = PEER_READ_REPLY_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Dial a paired install, send ONE request, read its reply, close.

    One request per connection, deliberately. A pooled or reused connection
    would need an owner, a liveness story and a place to be closed when a turn
    ends early — three problems this lane does not have, in exchange for saving
    a TLS handshake on a call that happens a handful of times per turn. The
    connection is closed on EVERY path, including the ones that raise inside the
    read loop, which is what the ``finally`` is for and what a test asserts.

    ``store_root`` is an INPUT and is never resolved here, for
    ``gateway_peers``' reason: several roots coexist on this machine and a
    client free to re-derive its own could dial a peer of one install while
    answering for another. Callers inside a persona turn pass
    ``gateway_targets.peer_store_root()`` — the HEAD home's root, not the
    ambient one, because ``HERMES_HOME`` is flipped process-globally for the
    length of every persona turn.
    """

    from agent_runtime.gateway_peers import dial_peer

    request_id = f"peer-{method}-1"
    connection = None
    try:
        connection, _hello = dial_peer(
            store_root, peer_install_id, timeout_seconds=float(dial_timeout)
        )
    except ConnectionError as exc:
        # The typed transport failure. ``dial_peer`` raises this for every
        # not-reachable condition — no endpoint answered, a revoked row, an
        # expired credential — and each is a state a caller renders rather than
        # a fault it reports.
        return _refuse(PEER_UNREACHABLE_REASON, str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return _refuse(PEER_UNREACHABLE_REASON, f"{type(exc).__name__}: {exc}")

    try:
        connection.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": dict(params or {}),
            }
        )
        deadline = _monotonic() + float(reply_timeout)
        while True:
            if _monotonic() >= deadline:
                return _refuse(
                    PEER_UNREACHABLE_REASON,
                    f"{peer_install_id} did not answer {method} within "
                    f"{reply_timeout:g}s",
                )
            frame = connection.read_frame()
            if frame is None:
                return _refuse(
                    PEER_UNREACHABLE_REASON,
                    f"{peer_install_id} closed the connection before answering "
                    f"{method}",
                )
            if not isinstance(frame, dict) or frame.get("id") != request_id:
                # Stream frames and notifications ride the same socket. Skipping
                # anything that is not OUR reply is what lets this be one call
                # rather than a subscription with a filter.
                continue
            error = frame.get("error")
            if isinstance(error, dict):
                data = error.get("data") if isinstance(error.get("data"), dict) else {}
                reason = str(data.get("reason") or "").strip()
                if not reason:
                    reason = (
                        CAPABILITY_MISSING_REASON
                        if error.get("code") == _ERR_METHOD_NOT_FOUND
                        else "peer_refused"
                    )
                return _refuse(reason, str(error.get("message") or reason), data=data)
            result = frame.get("result")
            return {"result": result if isinstance(result, dict) else {}}
    except Exception as exc:
        return _refuse(PEER_UNREACHABLE_REASON, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _refuse(reason: str, message: str, *, data: dict[str, Any] | None = None) -> dict:
    refusal: dict[str, Any] = {"reason": reason, "message": message[:400]}
    if data:
        # The far side's own ``data`` block travels VERBATIM, minus nothing: a
        # caller that had to learn a second refusal vocabulary for the same
        # question would be branching on which hop answered.
        refusal["data"] = {
            key: value
            for key, value in data.items()
            if isinstance(key, str) and _is_jsonable(value)
        }
    return {"refusal": refusal}


def _is_jsonable(value: Any) -> bool:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


def _monotonic() -> float:
    import time

    return time.monotonic()
