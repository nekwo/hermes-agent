from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from hermes_time import now

from .events import EventLog
from .models import Event


INPUT_TYPES = frozenset(
    {
        "backend_contract",
        "frontend_usage",
        "visual_verification",
        "scope_decision",
        "proof_gap",
        "environment_blocker",
        "user_decision",
    }
)

ROLE_ROUTES = {
    "backend_contract": "backend_dev",
    "frontend_usage": "dev",
    "visual_verification": "qa",
    "scope_decision": "neko_supervisor",
    "proof_gap": "qa",
    "environment_blocker": "neko_supervisor",
    "user_decision": "operator",
}


@dataclass(frozen=True, slots=True)
class MissingInputRequest:
    request_id: str
    task_id: str
    requester_persona_id: str
    input_type: str
    question: str
    route_to: str
    context_ref: str | None = None

    def manifest(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "request_id": self.request_id,
            "task_id": self.task_id,
            "requester_persona_id": self.requester_persona_id,
            "input_type": self.input_type,
            "question": self.question,
            "route_to": self.route_to,
            "context_ref": self.context_ref,
        }


def create_missing_input_request(
    *,
    task_id: str,
    requester_persona_id: str,
    input_type: str,
    question: str,
    context_ref: str | None = None,
    event_log: EventLog | None = None,
) -> MissingInputRequest:
    normalized_type = str(input_type or "").strip()
    normalized_question = " ".join(str(question or "").split())
    if normalized_type not in INPUT_TYPES:
        raise ValueError(f"unknown missing input type: {normalized_type}")
    if len(normalized_question) < 8:
        raise ValueError("missing input question must be specific")
    request = MissingInputRequest(
        request_id=f"input_{uuid.uuid4().hex[:10]}",
        task_id=task_id,
        requester_persona_id=requester_persona_id,
        input_type=normalized_type,
        question=normalized_question[:500],
        route_to=ROLE_ROUTES[normalized_type],
        context_ref=context_ref,
    )
    (event_log or EventLog()).append(
        Event(
            ts=now(),
            type="missing_input.requested",
            task_id=task_id,
            run_id=None,
            persona_id=requester_persona_id,
            payload={
                "request_id": request.request_id,
                "input_type": request.input_type,
                "route_to": request.route_to,
                "question": request.question,
                "context_ref": request.context_ref,
            },
        )
    )
    return request

