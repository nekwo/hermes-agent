"""Non-blocking ``clarify`` bridge for the Mission Control operator/relay lane.

The CLI/gateway ``clarify`` callback blocks on a human input queue (see
``hermes_cli/callbacks.clarify_callback``). A one-shot Mission Control chat turn
has no such interactive queue to block on — it is spawned, runs, and returns a
reply. So on this lane ``clarify`` must not block: calling it RECORDS the
structured question and returns a sentinel that tells the model to end its turn
with the question as its reply.

The mission-chat handler reads the recorded :class:`MissionChatClarifyCapture`
after the run and threads it back to whoever asked — the operator (rendered as
pickable options in the HUD) or the parent agent that briefed this one via
``agent_chat_send``. The answer arrives as the next message in the SAME session,
so a clarification is an ordinary conversational turn, never a typed failure.

This is the seam that lets a child surface context only it has ("which dev —
launcher or backend?") instead of the parent guessing.
"""

from __future__ import annotations

from dataclasses import dataclass

# Mirror tools.clarify_tool.MAX_CHOICES: the UI appends its own "Other" row.
MAX_CHOICES = 4


@dataclass(slots=True)
class MissionChatClarifyCapture:
    """Records the first ``clarify`` call of a mission-chat turn.

    First call wins: the model is told to stop after asking, so a second call
    should not happen — but if it does, the question we already reported to the
    caller stays authoritative rather than being overwritten mid-turn.
    """

    question: str | None = None
    choices: list[str] | None = None

    @property
    def requested(self) -> bool:
        return bool(self.question)

    @property
    def request(self) -> dict[str, object] | None:
        """Structured payload threaded back to the caller, or None."""
        if not self.requested:
            return None
        payload: dict[str, object] = {"question": self.question}
        if self.choices:
            payload["choices"] = list(self.choices)
        return payload

    def callback(self, question: str, choices: list[str] | None = None) -> str:
        text = (question or "").strip()
        if not text:
            return "clarify requires a non-empty question — nothing was recorded."
        if self.question is None:
            self.question = text
            cleaned = [
                choice.strip()
                for choice in (choices or [])
                if isinstance(choice, str) and choice.strip()
            ]
            self.choices = cleaned[:MAX_CHOICES] or None
        return self._sentinel()

    def _sentinel(self) -> str:
        options_hint = ""
        if self.choices:
            options_hint = (
                " You offered these options, so they can pick one: "
                + "; ".join(self.choices)
                + "."
            )
        return (
            "Your clarifying question was routed to whoever asked you — the "
            "operator, or the agent that briefed you — and their answer will "
            "arrive as the next message in this same conversation. End your turn "
            "now with the question as your reply, phrased in your own natural "
            "voice." + options_hint + " Do not act or guess until they answer."
        )
