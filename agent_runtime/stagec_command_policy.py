"""Stage C proof-command argument validation — a KEEP module.

Extracted from ``agent_runtime/proof_command_policy.py`` ahead of the mission-lane
removal (``docs/agent-runtime-harness/16-mission-lane-removal.md`` S1/S6). The rest
of ``proof_command_policy`` is task-shaped: it reads ``Task.non_goals`` /
``acceptance_criteria`` / ``risk_flags`` to decide whether a *goal* demanded a
bounded smoke proof, and it dies with the goal/task lane.

The two checks below are **not** task-shaped. They inspect the Stage C MCP command
string itself and reject argument shapes that the ``launcher_qa`` MCP server would
either ignore or silently mis-answer:

* :func:`reject_invalid_stagec_screenshot_window_args` — ``screenshot_window``
  accepts ``max_retries`` / ``retry_delay_ms``, not the ``screenshot_*`` names that
  belong to the composed ``open_app_tab`` path. Passing the wrong ones produces a
  screenshot with none of the requested stabilisation.
* :func:`reject_unpinned_mission_control_stagec_commands` — a Mission Control
  capture that does not pin ``hermes_profile`` / ``harness_runtime_root`` /
  ``hermes_home`` photographs *some* runtime, not Tony's. Unpinned pixels are not
  evidence.

Both survive the removal because Stage C visual proof survives the removal.

Dependency note: the raised type is ``DecisionPayloadInvalid`` from
``.decision_schema``, kept so existing ``except DecisionPayloadInvalid`` handlers
still catch these. That is the ONLY tie to the decision lane; if
``decision_schema`` is ever retired, re-home the exception class here rather than
re-homing these checks back into a dying module.
"""

from __future__ import annotations

from .decision_schema import DecisionPayloadInvalid

# ``screenshot_window`` argument names that belong to the composed ``open_app_tab``
# screenshot path and are silently ignored by ``screenshot_window`` itself.
_INVALID_SCREENSHOT_WINDOW_ARGS = (
    "screenshot_stabilize_ms",
    "screenshot_max_retries",
    "screenshot_retry_delay_ms",
)

# The three env pins that bind a Mission Control capture to Tony's live runtime.
_MISSION_CONTROL_RUNTIME_PINS = ("hermes_profile", "harness_runtime_root", "hermes_home")


def reject_invalid_stagec_screenshot_window_args(commands: list[str]) -> None:
    """Raise if a ``screenshot_window`` call passes composed-path-only argument names."""

    for command in commands:
        for segment in tool_segments(command, "mcp_launcher_qa_screenshot_window"):
            if any(arg in segment for arg in _INVALID_SCREENSHOT_WINDOW_ARGS):
                raise DecisionPayloadInvalid(
                    "Stage C screenshot_window proof command policy failed: "
                    "mcp_launcher_qa_screenshot_window accepts max_retries and retry_delay_ms, "
                    "not screenshot_stabilize_ms, screenshot_max_retries, or screenshot_retry_delay_ms. "
                    "Use screenshot_* only on the open_app_tab composed screenshot path, or add a bounded "
                    "wait before screenshot_window and pass max_retries/retry_delay_ms to screenshot_window."
                )


def reject_unpinned_mission_control_stagec_commands(commands: list[str]) -> None:
    """Raise if a Mission Control ``open_app_tab``/``launch_or_attach`` omits a runtime pin."""

    for command in commands:
        lowered = command.lower()
        if "mcp_launcher_qa_open_app_tab" not in lowered and "mcp_launcher_qa_launch_or_attach" not in lowered:
            continue
        if "missioncontrol" not in lowered and "mission control" not in lowered:
            continue
        missing = [field for field in _MISSION_CONTROL_RUNTIME_PINS if field not in lowered]
        if missing:
            raise DecisionPayloadInvalid(
                "Mission Control Stage C proof command policy failed: open_app_tab/launch_or_attach "
                "must pin Tony's Harness runtime by passing hermes_profile, harness_runtime_root, "
                f"and hermes_home; missing {', '.join(missing)}. Unpinned Mission Control pixels do "
                "not prove the live runtime root/profile."
            )


def tool_segments(command: str, tool_name: str) -> list[str]:
    """Slice ``command`` into the lowercased argument runs belonging to ``tool_name``.

    A shell line can chain several MCP tool invocations; each segment runs from the
    tool name to the next separator (``&&``, ``;``, ``|``) or the next
    ``-Tool mcp_launcher_qa_*``, so an argument on a *later* tool is never blamed on
    an earlier one.
    """

    lowered = str(command or "").lower()
    marker = tool_name.lower()
    segments: list[str] = []
    search_from = 0
    while True:
        start = lowered.find(marker, search_from)
        if start < 0:
            return segments
        end_candidates = [
            candidate
            for candidate in (
                lowered.find("&&", start + len(marker)),
                lowered.find(";", start + len(marker)),
                lowered.find("|", start + len(marker)),
                lowered.find("-tool mcp_launcher_qa_", start + len(marker)),
            )
            if candidate >= 0
        ]
        end = min(end_candidates) if end_candidates else len(lowered)
        segments.append(lowered[start:end])
        search_from = start + len(marker)
