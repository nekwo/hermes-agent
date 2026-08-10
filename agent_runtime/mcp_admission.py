"""Selective, declared, per-run MCP admission for harness-lane persona runs.

Why this module exists
----------------------
``mcp_lane`` (R0) made one thing honest: the harness / mission-chat lane never
runs ``discover_mcp_tools()``, so a persona whose profile DECLARES MCP servers
gets none of their tools there, and now says so in a typed
``mcp_not_registered_on_lane`` row instead of reporting ``[]``.

This module is R1 of the follow-on: turning that honest refusal into an honest
ADMISSION for a narrow, declared, config-flagged set of personas — so a QA
persona that already declares ``launcher_qa`` in three places can actually run
``mcp__launcher_qa__*`` tools AS ITSELF on the mission-chat lane, with its
chats, trace and roster presence all native.

Design (canonical): ``docs/agent-runtime-harness/mission-chat-mcp-admission.md``.
Read that before changing anything here.

The invariants this module exists to hold
-----------------------------------------
1. **The lane blanket never flips.** ``hermes_cli/main.py::_AGENT_COMMANDS`` is
   untouched and ``discover_mcp_tools()`` is never called from here. Admission
   is per-RUN and per-PERSONA, driven by ``register_mcp_servers({name: cfg})``
   for an explicitly resolved subset — the same per-session mechanism
   ``acp_adapter/server.py::_register_session_mcp_servers`` already uses.
2. **The profile declaration is the admission authority.** A persona can admit
   only servers declared by its backing profile. A profile that declares none
   admits none; role names do not narrow or widen that data-owned set. Machine
   resolution and permission-mode filtering may still narrow it.
3. **``unbounded`` must never cross the declared set.** The chat lane's
   ``unbounded`` mode resolves ``all_registered_toolsets()`` — which, in a
   long-lived multi-persona harness process, would include another persona's
   admitted MCP toolsets. ``scope_toolsets_to_admission`` is applied AFTER
   permission-mode resolution and strips every ``mcp-*`` toolset (and alias)
   this run was not admitted. Since R2 the registry is ALSO empty between
   admitted runs (see below), so this is now the second line of defence rather
   than the only one.
4. **Registration is single-flight and bounded.** ``tools.registry`` and
   ``tools/mcp_tool._servers`` are process-global and a serve process is
   multi-persona (``ThreadPoolExecutor(4)``), so two interleaved admissions
   against one global registry are refused (``mcp_admission_lane_busy``) rather
   than raced. A registration that outruns its budget degrades to
   ``mcp_admission_timeout`` and the turn continues without those tools.

5. **The registry scope belongs to the RUN, the transport belongs to the
   process.** :func:`teardown_mcp_admission` removes the admitted
   ``mcp-<server>`` tools (and, with the last tool, the toolset check and every
   alias pointing at it) at the end of every admitted run, while the connection
   in ``tools/mcp_tool._servers`` stays warm for the next one. Teardown never
   fails a finished turn: every fault is a typed ``mcp_admission_teardown_failed``
   row.
6. **An admitted run is bounded in CALLS, not only in time.** Single-flight
   bounds how many admissions may be in flight; the wall budget and the AS0
   liveness watchdog bound the turn's clock. Neither bounds how many times an
   admitted agent may call ``kill_launcher`` inside one turn. The per-run budget
   (:class:`McpCallBudget`, installed by :func:`admit_mcp_servers` over the
   registered handlers) does: past ``max_tool_calls_per_run`` every further
   admitted MCP call is REFUSED with a typed ``mcp_admission_budget_exhausted``
   row instead of dispatched. The turn is never killed — the agent keeps its
   non-MCP tools and can finish with what it already captured.
7. **The agent is told when it does NOT get what it declared.** A denial or
   degradation is rendered as one compact line
   (:func:`render_mcp_admission_line`) on the same volatile envelope tail the
   wall-budget line rides, so the model reads the truth in-band instead of
   improvising a workaround. Volatile on purpose — never hashed into the HUD
   revision.

R2: why teardown forced the registrar to change
-----------------------------------------------
R1 shipped no teardown, and the consequence was load-bearing: once a server was
admitted in a warm serve process its tools stayed in the process registry until
the process recycled, and a ``read_only`` admission FOLLOWING a
``profile_default`` one re-used the already-registered full surface, because
``register_mcp_servers`` short-circuits on connected servers
(``tools/mcp_tool.py`` — ``if not new_servers: return _existing_tool_names()``).
The registration-time tool filter therefore could not subtract; only
``blocked_tool_names`` kept the mutators out of the model's list.

Open question 2 of the design asked whether ``tools/registry.py`` supports
scoped removal. It does, so R2 takes the design's preferred shape — **tear down
the registry scope, keep the transport warm**. But that same short-circuit means
``register_mcp_servers`` alone can no longer re-register a torn-down warm server:
it would return ``_existing_tool_names()`` forever and the server would stay
tool-less. :func:`_default_registrar` therefore splits the admitted set:

* **warm** (already in ``_servers`` with a live session) ⇒ re-register straight
  off that session through the upstream ``_register_server_tools`` seam — no
  spawn, no handshake, and the per-run ``tools.include`` / ``tools.exclude``
  filter is applied to the already-listed tools;
* **cold** ⇒ ``register_mcp_servers({name: cfg})``, exactly as in R1.

Both paths run the filter, so ``read_only`` after ``profile_default`` now
subtracts AT REGISTRATION TIME rather than relying on ``blocked_tool_names``
(which stays, as defence in depth for a resident actor's cached tool list).

The warm path is the one place this module reaches for an upstream private. It
fails CLOSED — an unavailable seam registers nothing, which surfaces as a typed
``mcp_not_registered_on_lane`` denial rather than a silently full surface — and
``tests/agent_runtime/test_mcp_admission_r2.py`` pins the seam so upstream drift
is loud instead of silent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

# ── typed denial codes ──────────────────────────────────────────────────────
#
# Same vocabulary as the design's §A. ``mcp_not_registered_on_lane`` is reused
# from R0 rather than re-spelled: "admitted, but it did not actually register"
# is exactly what that code already means.

from .mcp_lane import MCP_NOT_REGISTERED_ON_LANE  # noqa: E402  (re-exported below)

MCP_ADMISSION_DISABLED = "mcp_admission_disabled"
MCP_ADMISSION_LANE_BUSY = "mcp_admission_lane_busy"
MCP_ADMISSION_TIMEOUT = "mcp_admission_timeout"
#: DECLARED (by ``required_mcp_servers`` or the profile's own ``mcp_servers``
#: block), but the persona's profile has no ``mcp_servers`` entry to spawn. The
#: readiness taxonomy already calls this ``mcp_attention``; this is its
#: admission-side spelling.
#:
#: S66 dropped "role-admitted" from this line. There is no role admission — S64
#: made declaration the sole authority — and the phrase named a gate that could
#: not fire.
MCP_SERVER_NOT_CONFIGURED = "mcp_server_not_configured"
#: ``read_only`` can only admit a server whose mutating tools we can name. An
#: unknown server has no reviewer-shaped subset, so read_only admits nothing
#: rather than admitting a surface it cannot subtract.
MCP_READ_ONLY_SUBSET_UNKNOWN = "mcp_read_only_subset_unknown"
#: A finished run's registry scope could not be removed. Never fatal — the turn
#: has already produced its answer by the time teardown runs — but never silent
#: either: leftover scope is exactly the residue R2 exists to retire.
MCP_ADMISSION_TEARDOWN_FAILED = "mcp_admission_teardown_failed"
#: The run spent its per-run admitted-MCP-call budget. Every FURTHER admitted MCP
#: call is refused with this row IN PLACE OF a dispatch; the turn itself is never
#: killed, so the agent can still finish and report with what it already has.
#: Design §3's residual-risk mitigation ("Loop → repeated launches/kills") and
#: the one §7 row R2 shipped without.
MCP_ADMISSION_BUDGET_EXHAUSTED = "mcp_admission_budget_exhausted"

#: The admission LANE — the runtime surface a persona turn runs on. Distinct
#: from ``mcp_lane``'s entry-point lane (``harness`` / ``chat`` / …), which
#: answers "did this process run MCP discovery". Both are needed: admission is
#: what puts tools on the harness entry point in the first place.
LANE_MISSION_CHAT = "mission_chat"

#: Toolset-name prefix ``tools/mcp_tool.py`` registers every MCP server under.
_MCP_TOOLSET_PREFIX = "mcp-"

#: How an admitted server's tools reached the registry on THIS run.
#:
#: ``warm`` — the transport was already connected with a live session, so
#: registration re-ran over tools this process had already listed. Measured
#: 2026-08-09 against a real 60-tool stdio MCP server: **6–8 ms** for the whole
#: admission, 0.2–0.3 ms for teardown.
#:
#: ``cold`` — no live session, so ``register_mcp_servers`` spawned the server
#: process and completed the MCP handshake before anything could be listed.
#: Measured on the same server: **3,197 ms** — ~400x the warm path.
#:
#: These exist as a RECORDED fact because the difference was being inferred.
#: ``PERF_SEND_ANALYSIS_2026-08-09`` (F2/T2) attributed a flat 2.35–3.4 s per
#: turn to "re-registration of an unchanged server set" and proposed caching the
#: registration. The measurement says registration is the 6 ms half and the spawn
#: is the whole cost — and a spawn is not cacheable, because a tool call needs a
#: live session, not a remembered schema. Its three probe turns each ran in a
#: FRESH CLI process, so all three were cold: turn-1 numbers reported as steady
#: state. The serve lane keeps transports warm across turns by design
#: (``tools/mcp_tool._servers`` outlives the run; only the registry scope is
#: per-run), so a warm serve turn should read ``warm``. Nothing persisted that,
#: so nobody could check which one the live 3.4 s was. Now the turn says.
TRANSPORT_WARM = "warm"
TRANSPORT_COLD = "cold"

#: The launcher-allowlist PROFILE ROW a ``read_only`` admission compiles from.
#: ``read_only`` is "inspect what others captured", which is exactly what that
#: row was written to express — see the parity fixture below.
READ_ONLY_ALLOWLIST_PROFILE = "reviewer"

#: **Positive** per-server tool allowlist for a ``read_only`` admission — the
#: resolved ALLOW set of the ``reviewer`` row of the launcher's own per-profile
#: allowlist, ``EterniaLauncher docs/stages/qa-reboot/launcher_qa_profile_allowlists.yaml``
#: (v1, 2026-05-17, + the Stage 19 / VOICE_QA §5.E / batched-drill amendments —
#: snapshot pinned at launcher ``3e3feff0``, a 26-tool surface).
#:
#: Positive, not negative, deliberately: a tool the launcher's QA server grows
#: LATER is denied to ``read_only`` by default instead of silently inheriting it.
#: That is the same default-deny the launcher YAML chose for its restricted
#: profiles ("so a future tool added under Stage 19+ does not silently fall into
#: the restricted profiles via a missing entry"), and it is the reason this is
#: the R2 shape rather than R1's exclude list.
#:
#: Hermes OWNS this policy (design open question 6): the launcher file is
#: documentation plus a CI parity fixture, never read at admission time, so a
#: missing checkout or a deploy skew can never change what an agent may call.
#: ``tests/agent_runtime/test_mcp_admission_r2.py`` pins it against a vendored,
#: hash-recorded snapshot of that YAML — see the fixture's refresh instructions.
READ_ONLY_INCLUDED_TOOLS: Mapping[str, tuple[str, ...]] = {
    "launcher_qa": (
        "mcp_launcher_qa_get_auth_state",
        "mcp_launcher_qa_get_buttons",
        "mcp_launcher_qa_get_feed_fixture_state",
        "mcp_launcher_qa_get_media_playback_state",
        "mcp_launcher_qa_get_navigation_state",
        "mcp_launcher_qa_get_runtime_state",
        "mcp_launcher_qa_get_voice_state",
        "mcp_launcher_qa_get_widget_state",
        "mcp_launcher_qa_get_window_metrics",
        "mcp_launcher_qa_read_artifact_index",
        "mcp_launcher_qa_read_trace",
        "mcp_launcher_qa_run_redaction_scan",
    ),
}

#: The complement of :data:`READ_ONLY_INCLUDED_TOOLS` over the server's known
#: surface — the ``denied`` rows of the same ``reviewer`` profile. The include
#: list above is what actually gets REGISTERED; this list is the warm-process
#: backstop, threaded into ``blocked_tool_names`` so a resident actor's cached
#: tool definitions cannot resurrect a mutator that the current run's
#: registration already filtered out.
#:
#: R1 carried only this half, and it was three names SHORT of the reviewer row:
#: ``capture_screenshot`` / ``screenshot_window`` / ``wait_for_state`` all drive
#: a live launcher window (restore + foreground + PrintWindow; a polling loop
#: against the fixture mutex) and the launcher denies them to ``reviewer`` for
#: that reason. R2 adopts the row verbatim, which NARROWS ``read_only``.
#:
#: ``run_actions`` is the reason a POSITIVE include is the right shape. It is a
#: capability MULTIPLEXER — one call executing an ordered list of other verbs —
#: so a name-matching allowlist that admits it hands over every batchable verb
#: the profile is otherwise denied. Under an include list it is denied by
#: construction, because it simply is not on the list; under an exclude list it
#: would have been admitted the moment the launcher shipped it, silently, and
#: nobody would have noticed until an agent batched a ``click_button``.
READ_ONLY_EXCLUDED_TOOLS: Mapping[str, tuple[str, ...]] = {
    "launcher_qa": (
        "mcp_launcher_qa_begin_pkce_login",
        "mcp_launcher_qa_capture_screenshot",
        "mcp_launcher_qa_click_button",
        "mcp_launcher_qa_dismiss_hashtag_onboarding",
        "mcp_launcher_qa_kill_launcher",
        "mcp_launcher_qa_launch_or_attach",
        "mcp_launcher_qa_open_app_tab",
        "mcp_launcher_qa_run_actions",
        "mcp_launcher_qa_screenshot_window",
        "mcp_launcher_qa_scroll",
        "mcp_launcher_qa_scroll_to",
        "mcp_launcher_qa_scroll_to_fixture",
        "mcp_launcher_qa_set_tab",
        "mcp_launcher_qa_wait_for_state",
    ),
}

#: The OPERATING MANUAL for each admissible server: the skill(s) that document
#: how to drive that server's surface, and — the part that matters — what to do
#: when it REFUSES. Admitting a tool surface without its manual is what produced
#: the live 2026-07-29 failure: a QA mission-chat turn drove ``launcher_qa``,
#: hit ``helper_low_information_capture`` from ``screenshot_window``, and burned
#: the turn rediscovering nothing — while the remedy (capture a content-bearing
#: sub-tab, or take the documented sparse-acceptance path) sat in a skill the
#: persona was ALREADY granted and that was never put in context.
#:
#: Why the map lives here rather than in the skill's frontmatter
#: -------------------------------------------------------------
#: ``metadata.hermes.load_policy: required_preload`` is the right mechanism for
#: "this persona always needs this skill" (``harness-runtime-model``), and it is
#: reused verbatim downstream — the resolved names join the SAME required-preload
#: set. It is the wrong mechanism for this fact, twice over:
#:
#: * The condition is not "the persona is granted it", it is "this RUN was
#:   admitted the server". A persona that declares ``launcher_qa`` on a lane
#:   where admission is off, or under a role the config does not name, gets no
#:   MCP tools — and must not pay a 45KB manual for tools it does not have.
#: * ``launcher-stagec-mcp-screenshot`` is not a Harness-owned skill: it is not
#:   under ``docs/agent-runtime-harness/harness-skills`` and ``skill_install``
#:   never writes it, so its frontmatter is a realm-published runtime artifact
#:   that the next realm pull would overwrite. A policy Hermes must hold cannot
#:   live in a file Hermes does not own — the same reasoning that keeps
#:   :data:`READ_ONLY_INCLUDED_TOOLS` here instead of reading the launcher's YAML.
#:
#: Keyed by server, not by role, for the same reason the tool tables are: the
#: manual belongs to the SURFACE. A second role admitted the same server would
#: need the same manual, and a role admitted nothing needs none.
MCP_OPERATING_SKILLS: Mapping[str, tuple[str, ...]] = {
    "launcher_qa": ("launcher-stagec-mcp-screenshot",),
}

_DEFAULT_CONNECT_TIMEOUT_SECONDS = 20.0

#: Per-run budget of ADMITTED MCP tool calls, before an operator config edit.
#:
#: Sized off the real drills rather than a round number: the Stage C 6-row
#: acceptance matrix costs ~60 admitted calls end to end, and the batched
#: ``run_actions`` lane (a 6–9 action drill in ONE call) costs far less. 120 is
#: ~2× the heaviest honest turn we know of, which is the "generous" side of the
#: line — the budget is a loop bound, not a work bound, and a QA turn that trips
#: it has stopped making progress rather than merely being thorough.
_DEFAULT_MAX_TOOL_CALLS_PER_RUN = 120

#: Process-wide admission mutex. Held for the FULL duration of a registration
#: attempt — including past a caller's timeout — because the worker thread, not
#: the caller, releases it. A second admission that arrives while one is in
#: flight is refused, never interleaved.
_ADMISSION_LOCK = threading.Lock()


# ── typed results ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class McpAdmissionDenial:
    """One typed reason a declared MCP server is not available for this run.

    ``row()`` is deliberately the same shape as ``machine_roots.PathTokenIssue.row()``
    and the R0 ``mcp_not_registered_on_lane`` row, so operator surfaces that
    already render typed issue rows need no new case.
    """

    server: str
    code: str
    summary: str
    fix_hint: str = ""

    def row(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "server": self.server,
            "summary": self.summary,
            "fix_hint": self.fix_hint,
        }


@dataclass(frozen=True, slots=True)
class McpAdmission:
    """The resolved, side-effect-free answer to "what may this run register?"

    Resolution NEVER spawns, connects or registers anything — it is pure policy
    over config + the persona's declaration, so an operator can inspect it
    (``hermes harness persona tool-diff <id> --explain-mcp``) before the flag is
    ever flipped, and so the whole transition table is unit-testable.
    """

    lane: str
    role: str
    permission_mode: str
    enabled: bool
    requested: tuple[str, ...] = ()
    server_names: tuple[str, ...] = ()
    denied: tuple[McpAdmissionDenial, ...] = ()
    server_configs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: Prefixed registry names removed from the model's tool list for this run
    #: (the resident-actor backstop behind the ``read_only`` include list; see
    #: the module docstring's R2 section).
    blocked_tool_names: tuple[str, ...] = ()
    connect_timeout_seconds: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS
    #: How many ADMITTED MCP tool calls this run may dispatch in total, across
    #: every admitted server. Resolved here (policy) and enforced at execution by
    #: :class:`McpCallBudget`, so an operator can read the bound out of
    #: ``--explain-mcp`` before the flag is ever flipped.
    max_tool_calls_per_run: int = _DEFAULT_MAX_TOOL_CALLS_PER_RUN

    @property
    def is_empty(self) -> bool:
        return not self.server_names

    def denial_rows(self) -> list[dict[str, Any]]:
        return [denial.row() for denial in self.denied]

    def explain(self) -> dict[str, Any]:
        """Stable, machine-readable operator view. No side effects."""

        return {
            "lane": self.lane,
            "role": self.role,
            "permission_mode": self.permission_mode,
            "enabled": self.enabled,
            "requested": list(self.requested),
            "admitted": list(self.server_names),
            "denied": self.denial_rows(),
            # The COMPILED positive allowlist per admitted server — what will
            # actually be registered. Absent/empty means "everything this server
            # advertises" (the launcher's full-capability glob rows). An operator
            # checking a read_only shape before flipping the flag needs to see
            # the include, not infer it from the block list.
            "tool_include": {
                name: sorted(str(tool) for tool in (config.get("tools") or {}).get("include") or [])
                for name, config in self.server_configs.items()
            },
            "blocked_tool_names": list(self.blocked_tool_names),
            "connect_timeout_seconds": self.connect_timeout_seconds,
            # The call bound this run would enforce. Reported even when nothing
            # is admitted: "how many MCP calls could this persona make" is a
            # question an operator asks BEFORE deciding to admit anything.
            "max_tool_calls_per_run": self.max_tool_calls_per_run,
        }


@dataclass(frozen=True, slots=True)
class McpAdmissionOutcome:
    """What actually happened when an admission was executed."""

    admitted: tuple[str, ...] = ()
    denied: tuple[McpAdmissionDenial, ...] = ()
    registered_tool_names: tuple[str, ...] = ()
    duration_ms: int = 0
    attempted: bool = False
    #: The subset of ``denied`` that EXECUTION minted — busy / timeout /
    #: admitted-but-did-not-register. Kept separate from the policy denials
    #: ``denied`` also carries, because only these were unknowable when the
    #: turn's runtime-context envelope was sealed, and therefore only these need
    #: the runner's in-band backstop. Reporting a policy denial twice would tell
    #: the agent the same thing in two voices.
    execution_denied: tuple[McpAdmissionDenial, ...] = ()
    #: The live per-run call meter installed over this run's admitted tools, or
    #: ``None`` when nothing registered. Carried on the outcome (rather than kept
    #: module-global) because the budget IS per-run: a new admission mints a new
    #: meter, and the old one dies with the run's registry scope.
    call_budget: "McpCallBudget | None" = None
    #: ``{server: "warm" | "cold"}`` as observed BEFORE this run registered
    #: anything — the one attribution that turns ``mcp_admission_ms`` from a
    #: number into an explanation. Empty when a caller supplied its own
    #: ``register`` (tests, previews): classification reads the live transport
    #: map, and a custom registrar means there is no live transport to read.
    transport_paths: Mapping[str, str] = field(default_factory=dict)

    def denial_rows(self) -> list[dict[str, Any]]:
        return [denial.row() for denial in self.denied]

    @property
    def degraded(self) -> bool:
        """Did EXECUTION fail to deliver something the policy already admitted?"""

        return bool(self.attempted and self.execution_denied)


@dataclass(frozen=True, slots=True)
class McpTeardownOutcome:
    """What the end-of-run registry-scope removal actually removed.

    Advisory, never fatal: by the time teardown runs the turn has already
    produced its answer, so a fault here is reported as a typed row and the run
    still completes. ``failures`` being non-empty is the signal that a scope
    outlived its run and the next run's toolset scope
    (:func:`scope_toolsets_to_admission`) is carrying isolation alone.
    """

    servers: tuple[str, ...] = ()
    removed_tool_names: tuple[str, ...] = ()
    failures: tuple[McpAdmissionDenial, ...] = ()
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return not self.failures

    def failure_rows(self) -> list[dict[str, Any]]:
        return [failure.row() for failure in self.failures]


class McpCallBudget:
    """The per-run bound on how many ADMITTED MCP calls a turn may dispatch.

    Why a call budget exists at all
    -------------------------------
    Single-flight bounds how many admissions may be in flight. The wall budget
    and the AS0 liveness watchdog bound the turn's CLOCK. None of them bounds how
    many times an admitted agent may call ``kill_launcher`` inside one turn — a
    model under prompt-injection or simple mechanical looping can spend a whole
    wall budget re-driving a GUI. This is the counter that closes that gap
    (design §3, residual risk 1; §7's owed ``mcp_admission_budget_exhausted``).

    What counts
    -----------
    ONE dispatch of ONE admitted MCP tool = ONE call. That includes the launcher's
    batched ``run_actions`` multiplexer, which executes an ordered list of verbs
    in a single call: §7 says nothing else, and counting the batch as one call is
    what keeps the budget aligned with the seam it is enforced at (a tool
    dispatch), rather than with a payload shape hermes does not parse. It is also
    the direction that keeps the batched lane cheap — which is the lane the QA
    drills are supposed to prefer.

    Nothing else touches the counter: a non-admitted tool (every built-in, every
    other toolset) never passes through the meter, and a call REFUSED after
    exhaustion is recorded as ``refused`` rather than spending budget it does not
    have.

    Scope
    -----
    Per RUN, and per run TOTAL across all admitted servers — deliberately the
    tighter of the two readings the design contains (§3 says "max tool calls per
    admitted server per run"; §7 says "the per-run call budget"). With today's
    single admissible server they are identical; with two admitted servers a
    per-server reading would silently authorise 2× the calls, and the conservative
    direction is the one that cannot surprise an operator upward. Per-server
    counts are still recorded, for the accounting an operator reads afterwards.

    A new meter is minted per admission, so the budget resets per run by
    construction — there is no reset path to forget to call.

    Thread-safe: an agent may dispatch tool calls from more than one thread, and
    a bound that can be raced is not a bound.
    """

    __slots__ = ("limit", "_lock", "_spent", "_refused", "_per_server")

    def __init__(self, limit: int):
        self.limit = max(1, int(limit))
        self._lock = threading.Lock()
        self._spent = 0
        self._refused = 0
        self._per_server: dict[str, int] = {}

    @property
    def spent(self) -> int:
        with self._lock:
            return self._spent

    @property
    def refused(self) -> int:
        with self._lock:
            return self._refused

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.limit - self._spent)

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return self._spent >= self.limit

    def consume(self, server: str, tool: str) -> McpAdmissionDenial | None:
        """Charge one admitted MCP call. ``None`` ⇒ dispatch; a denial ⇒ refuse.

        The check and the increment are one atomic step, so N concurrent calls at
        the boundary cannot all read "one left" and all dispatch.
        """

        with self._lock:
            if self._spent >= self.limit:
                self._refused += 1
                return self._denial(server, tool)
            self._spent += 1
            self._per_server[server] = self._per_server.get(server, 0) + 1
        return None

    def _denial(self, server: str, tool: str) -> McpAdmissionDenial:
        return McpAdmissionDenial(
            server=server,
            code=MCP_ADMISSION_BUDGET_EXHAUSTED,
            summary=(
                f"This run has spent its per-run MCP call budget ({self.limit} admitted "
                f"call(s)), so '{tool}' was NOT executed. No further MCP calls will run "
                "for this turn."
            ),
            fix_hint=(
                "This is a loop bound, not a permission problem: do not retry, do not "
                "hunt for a permission mode, and do not substitute a shell/PowerShell "
                "workaround or a second lane. Finish the turn with what you already "
                "have — report what you captured and what is missing. An operator can "
                "raise agent_runtime.mcp_admission.max_tool_calls_per_run in the ROOT "
                "config.yaml if a drill legitimately needs more."
            ),
        )

    def snapshot(self) -> dict[str, Any]:
        """Accounting for the operator surfaces. Never used to make a decision."""

        with self._lock:
            return {
                "limit": self.limit,
                "spent": self._spent,
                "remaining": max(0, self.limit - self._spent),
                "refused": self._refused,
                "exhausted": self._spent >= self.limit,
                "per_server": dict(self._per_server),
            }


# ── config ──────────────────────────────────────────────────────────────────


def admission_config(cfg: Any | None = None):
    """The ``agent_runtime.mcp_admission`` block from the ROOT runtime config.

    Harness-wide operator policy, so it loads through
    ``config.load_root_runtime_config`` — a sticky-active profile's own
    ``config.yaml`` must not be able to grant itself MCP admission.
    """

    from .runtime_config import McpAdmissionConfig

    if cfg is not None:
        resolved = getattr(cfg, "mcp_admission", None)
        return resolved if resolved is not None else McpAdmissionConfig()
    try:
        from .config import load_root_runtime_config

        return load_root_runtime_config().mcp_admission
    except Exception:  # pragma: no cover - defensive; a config fault must not open the gate
        logger.debug("MCP admission config load failed; treating admission as disabled", exc_info=True)
        return McpAdmissionConfig()


def admission_enabled(cfg: Any | None = None) -> bool:
    """The single kill switch: ``agent_runtime.mcp_admission.enabled``."""

    return bool(getattr(admission_config(cfg), "enabled", False))


# ── resolution (pure) ───────────────────────────────────────────────────────


def resolve_mcp_admission(
    persona,
    *,
    lane: str = LANE_MISSION_CHAT,
    permission_mode: str = "profile_default",
    cfg: Any = None,
) -> McpAdmission:
    """Resolve which declared MCP servers this persona run may register.

    Pure: reads config and the persona's profile declaration, and returns the
    compiled registration inputs. It performs **zero spawns** — a patched
    ``register_mcp_servers`` that fails the test if called is part of the suite.

    S66 removed the ``task`` / ``stage`` parameters. They rode the whole chain
    (here → ``_requested_servers`` → ``_effective_required_mcp_servers``) and
    were ignored at the bottom of it; no production caller passed either. They
    were residue of the retired role/work-description policy, and an accepted-
    and-discarded argument is exactly the silent no-op this campaign keeps
    finding.
    """

    from .personas import role_from_persona

    config = admission_config(cfg)
    lane = str(lane or "").strip()
    permission_mode = str(permission_mode or "profile_default").strip() or "profile_default"
    try:
        role = str(role_from_persona(persona).value)
    except Exception:  # pragma: no cover - defensive
        role = str(getattr(persona, "role", "") or "")
    requested = tuple(_requested_servers(persona))
    timeout = _positive_float(getattr(config, "connect_timeout_seconds", None)) or _DEFAULT_CONNECT_TIMEOUT_SECONDS
    # No "unlimited" spelling: a missing / zero / negative / unparseable value
    # falls back to the default rather than retiring the bound. The root parser
    # already clamps, so this is the second half of the same rule for callers
    # that hand in a hand-built config object (tests, --explain-mcp previews).
    call_budget = _positive_int(getattr(config, "max_tool_calls_per_run", None)) or _DEFAULT_MAX_TOOL_CALLS_PER_RUN

    def _empty(denials: Sequence[McpAdmissionDenial]) -> McpAdmission:
        return McpAdmission(
            lane=lane,
            role=role,
            permission_mode=permission_mode,
            enabled=bool(getattr(config, "enabled", False)),
            requested=requested,
            denied=tuple(denials),
            connect_timeout_seconds=timeout,
            max_tool_calls_per_run=call_budget,
        )

    if not getattr(config, "enabled", False):
        return _empty(
            [
                McpAdmissionDenial(
                    server=name,
                    code=MCP_ADMISSION_DISABLED,
                    summary=(
                        f"MCP admission is disabled, so '{name}' is not registered for this run."
                    ),
                    fix_hint=(
                        "Set agent_runtime.mcp_admission.enabled: true in the ROOT "
                        "config.yaml to admit servers declared by this persona profile."
                    ),
                )
                for name in requested
            ]
        )

    if not requested:
        return _empty([])

    denials: list[McpAdmissionDenial] = []
    candidates = list(requested)

    configured = _configured_servers_for(persona)
    resolvable: dict[str, Any] = {}
    for name in candidates:
        raw = configured.get(name)
        if not isinstance(raw, Mapping):
            denials.append(
                McpAdmissionDenial(
                    server=name,
                    code=MCP_SERVER_NOT_CONFIGURED,
                    # S66: this text used to read "was requested by role
                    # '{role}'". That is WIRE TEXT an operator and the agent
                    # both read on a denied turn, and it named the wrong
                    # authority: nothing requests by role since S64: the request
                    # is `required_mcp_servers` ∪ the profile's own
                    # `mcp_servers` block. Being told a role denied you when a
                    # declaration did sends the fix to the wrong file.
                    summary=(
                        f"'{name}' is declared by this persona/profile, but the profile "
                        "declares no mcp_servers entry to spawn."
                    ),
                    fix_hint=(
                        f"Add an mcp_servers.{name} block to the persona profile's config.yaml "
                        "(portable form: agent_runtime/docs/machine_roots_path_portability.md)."
                    ),
                )
            )
            continue
        resolvable[name] = raw

    resolved: dict[str, Any] = {}
    if resolvable:
        from .machine_roots import resolve_mcp_servers

        issues: list[tuple[str, Any]] = []
        resolved = resolve_mcp_servers(
            resolvable, on_issue=lambda name, issue: issues.append((name, issue))
        )
        for name, issue in issues:
            # Reuse the EXISTING machine_roots taxonomy verbatim (unbound_root,
            # root_target_missing, platform_unsupported, …) rather than minting a
            # parallel one — proving reuse is part of the R1 test plan.
            denials.append(
                McpAdmissionDenial(
                    server=str(name),
                    code=issue.code,
                    summary=issue.summary,
                    fix_hint=issue.fix_hint,
                )
            )

    admitted: list[str] = []
    compiled: dict[str, dict[str, Any]] = {}
    blocked: list[str] = []
    for name in candidates:
        entry = resolved.get(name)
        if not isinstance(entry, Mapping):
            continue
        filtered, excluded, denial = _apply_permission_mode(
            name, dict(entry), permission_mode=permission_mode
        )
        if denial is not None:
            denials.append(denial)
            continue
        filtered["connect_timeout"] = _bounded_connect_timeout(filtered.get("connect_timeout"), timeout)
        admitted.append(name)
        compiled[name] = filtered
        blocked.extend(_prefixed_tool_names(name, excluded))

    return McpAdmission(
        lane=lane,
        role=role,
        permission_mode=permission_mode,
        enabled=True,
        requested=requested,
        server_names=tuple(admitted),
        denied=tuple(denials),
        server_configs=compiled,
        blocked_tool_names=tuple(sorted(set(blocked))),
        connect_timeout_seconds=timeout,
        max_tool_calls_per_run=call_budget,
    )


def _requested_servers(persona) -> list[str]:
    """What the persona DECLARES — required ∪ profile-configured.

    Reuses the two existing declaration surfaces rather than writing a third:
    ``profile_readiness.declared_mcp_server_names`` (required ∪ the profile's
    ``mcp_servers`` block, and deliberately NOT the ambient operator config for
    an unbound persona) unioned with ``_effective_required_mcp_servers``.

    S64 retired the role policy this docstring used to advertise (``role ==
    "qa"`` + visual proof ⇒ ``launcher_qa``). There is no role lane left here:
    an MCP dependency exists ONLY where the persona/profile declares it, which
    is why S66 also removed the ``task`` / ``stage`` parameters that fed it —
    they were inert, and no production caller ever passed either.
    """

    from .profile_readiness import _effective_required_mcp_servers, declared_mcp_server_names

    names: list[str] = []
    for source in (
        declared_mcp_server_names(persona),
        _effective_required_mcp_servers(persona),
    ):
        for value in source or []:
            text = str(value or "").strip()
            if text and text not in names:
                names.append(text)
    return sorted(names)


def _configured_servers_for(persona) -> dict[str, Any]:
    """The persona profile's own ``mcp_servers`` map (never the ambient one)."""

    from .parse_cache import cached_yaml_file
    from .profile_context import resolve_persona_profile
    from .profile_readiness import _configured_mcp_servers

    try:
        binding = resolve_persona_profile(persona)
        if binding.profile_home is None:
            return {}
        raw = cached_yaml_file(binding.profile_home / "config.yaml", default={}) or {}
        return dict(_configured_mcp_servers(raw))
    except Exception:  # pragma: no cover - defensive; a config fault must not open the gate
        logger.debug("MCP admission could not read the persona profile config", exc_info=True)
        return {}


def _apply_permission_mode(
    server: str, config: dict[str, Any], *, permission_mode: str
) -> tuple[dict[str, Any], tuple[str, ...], McpAdmissionDenial | None]:
    """Compose the permission mode onto the EXISTING per-server tool filter.

    ``read_only`` compiles the reviewer-shaped **positive** allowlist into
    ``mcp_servers.<name>.tools.include`` — the filter ``tools/mcp_tool.py``
    already implements (include wins over exclude) — so a denied tool is never
    registered, rather than registered and then blocked. No new filtering code
    path, per the design's §A step 4.

    Returns ``(config, blocked_raw_tool_names, denial)``. The blocked names are
    the reviewer row's ``denied`` set, threaded into ``blocked_tool_names`` as
    the resident-actor backstop; the *registration* boundary is the include list.
    """

    from .tool_permissions import PERMISSION_MODE_READ_ONLY

    if permission_mode != PERMISSION_MODE_READ_ONLY:
        return config, (), None

    included = READ_ONLY_INCLUDED_TOOLS.get(server)
    if included is None:
        return (
            config,
            (),
            McpAdmissionDenial(
                server=server,
                code=MCP_READ_ONLY_SUBSET_UNKNOWN,
                summary=(
                    f"read_only admission has no reviewer-shaped subset for '{server}', "
                    "so nothing is admitted rather than admitting a surface it cannot subtract."
                ),
                fix_hint=(
                    f"Add '{server}' to agent_runtime.mcp_admission.READ_ONLY_INCLUDED_TOOLS "
                    "with a written security note, or run this persona in profile_default."
                ),
            ),
        )

    tools_filter = dict(config.get("tools") or {})
    authored = _name_list(tools_filter.get("include"))
    if authored:
        # A profile-authored include list is already narrower than the server's
        # full surface; read_only can only narrow it further, never resurrect a
        # tool the reviewer row does not allow.
        allowed = [name for name in authored if name in set(included)]
    else:
        allowed = list(included)
    tools_filter["include"] = allowed
    # An explicit include wins over exclude upstream, so the exclude entry is
    # redundant for registration. It is still written so an operator reading the
    # compiled config sees BOTH halves of the decision, and so a future upstream
    # that honours both stays correct.
    tools_filter["exclude"] = sorted(
        set(_name_list(tools_filter.get("exclude"))) | set(READ_ONLY_EXCLUDED_TOOLS.get(server, ()))
    )
    config["tools"] = tools_filter
    return config, tuple(READ_ONLY_EXCLUDED_TOOLS.get(server, ())), None


def _name_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item) for item in value]
    return []


def _prefixed_tool_names(server: str, tool_names: Iterable[str]) -> list[str]:
    """Registry/wire names for raw MCP tool names, via the upstream builder.

    Imported lazily and only on an admitted read_only run: importing
    ``tools.mcp_tool`` pulls the whole MCP SDK (~200ms), which no preview or
    disabled-flag path should ever pay. Mirroring the ``mcp__<server>__<tool>``
    convention locally was rejected — a silently drifting mirror is exactly the
    class of bug ``mcp_lane`` needed a guard test for.
    """

    names = [str(name).strip() for name in tool_names or [] if str(name or "").strip()]
    if not names:
        return []
    try:
        from tools.mcp_tool import mcp_prefixed_tool_name
    except Exception:  # pragma: no cover - MCP SDK absent; the tools.exclude filter still applies
        logger.debug("MCP admission could not resolve prefixed tool names", exc_info=True)
        return []
    return [mcp_prefixed_tool_name(server, name) for name in names]


def _bounded_connect_timeout(configured: Any, budget: float) -> float:
    """Never let a server's own connect timeout outrun the admission budget.

    ``launcher_qa`` declares ``connect_timeout: 60``; a mission-chat turn cannot
    spend that on a capability probe. Clamping here means the spawn attempt
    self-terminates inside the budget instead of leaving a thread wedged behind
    the caller's deadline.
    """

    value = _positive_float(configured)
    if value is None:
        return budget
    return min(value, budget)


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


# ── toolset scoping (pure) ──────────────────────────────────────────────────


def scope_toolsets_to_admission(
    toolsets: Iterable[str] | None, *, admitted_servers: Iterable[str] | None
) -> list[str]:
    """Keep only the MCP toolsets THIS run was admitted; add the admitted ones.

    Applied after permission-mode resolution, which is what makes the
    ``unbounded`` rule hold: ``unbounded`` resolves ``all_registered_toolsets()``,
    and in a warm multi-persona process that set can contain another persona's
    admitted ``mcp-*`` toolsets. Stripping them here means no permission mode can
    widen the admitted set — the security acceptance property.

    Non-MCP toolsets pass through untouched and in order.
    """

    admitted = [str(name).strip() for name in admitted_servers or [] if str(name or "").strip()]
    keep = {f"{_MCP_TOOLSET_PREFIX}{name}" for name in admitted} | set(admitted)
    # ONE registry read per call, not one per toolset name. On every lane that
    # registers no MCP at all — which is every lane until an operator flips the
    # flag — the alias map is empty and the prefix test is the whole check.
    aliases = _mcp_toolset_aliases()
    scoped = [
        name
        for name in (toolsets or [])
        if not _is_mcp_toolset(name, aliases) or str(name) in keep
    ]
    for name in admitted:
        toolset = f"{_MCP_TOOLSET_PREFIX}{name}"
        if toolset not in scoped:
            scoped.append(toolset)
    return scoped


def _mcp_toolset_aliases() -> frozenset[str]:
    """Alias names that point at an ``mcp-*`` toolset in this process."""

    try:
        from tools.registry import registry

        aliases = registry.get_registered_toolset_aliases()
    except Exception:  # pragma: no cover - registry is always importable in-process
        return frozenset()
    return frozenset(
        str(alias)
        for alias, target in (aliases or {}).items()
        if str(target).startswith(_MCP_TOOLSET_PREFIX)
    )


def _is_mcp_toolset(name: Any, aliases: frozenset[str]) -> bool:
    """Is this toolset name an MCP toolset — canonical ``mcp-x`` or an alias?

    The registry registers ``mcp-<server>`` and an alias ``<server>`` pointing at
    it, so a bare server name in ``enabled_toolsets`` resolves to the same tools.
    Both spellings have to be scoped or the strip leaks through the alias.
    """

    text = str(name or "").strip()
    if not text:
        return False
    return text.startswith(_MCP_TOOLSET_PREFIX) or text in aliases


# ── operating skills (pure) ─────────────────────────────────────────────────


def admitted_operating_skill_ids(
    admission: McpAdmission | None, *, granted_skills: Iterable[str] | None
) -> list[str]:
    """The operating manual(s) THIS run must have in context, in grant order.

    Two gates, both required, and each one narrows:

    1. **Admitted.** ``admission.server_names`` is what actually survived the
       profile-declared admission path — flag, declaration, machine
       resolvability, and permission mode. Nothing else is consulted, so a
       persona that merely DECLARES a server (and will get a typed denial line
       instead of tools) never pays for its manual.
    2. **Granted.** The skill must already be on the persona's own skill list.
       This function turns an existing grant into an active load; it never
       invents one. An operator who revokes the grant has revoked the preload,
       with no second place to look.

    Order follows the persona's grant list rather than the server list, so the
    preload set follows the profile grant order. Pure: no registry read, no
    filesystem, no resolve.
    """

    if admission is None or admission.is_empty:
        return []
    wanted: set[str] = set()
    for server in admission.server_names:
        wanted.update(MCP_OPERATING_SKILLS.get(str(server), ()))
    if not wanted:
        return []
    resolved: list[str] = []
    for skill in granted_skills or ():
        name = str(skill or "").strip()
        if name in wanted and name not in resolved:
            resolved.append(name)
    return resolved


# ── requirement-failure composition ─────────────────────────────────────────


def admission_requirement_failures(
    admission: McpAdmission | None,
    *,
    declared_servers: Iterable[str],
    lane: str | None = None,
    registered_servers: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Typed ``requirement_failures`` for a persona, admission-aware.

    One row per declared server, at most, resolved by precedence:

    1. Admitted AND actually registered in this process ⇒ **no row**. This is
       the "visibility is truthful both ways" half — once admission works, the
       R0 drop row must stop being emitted for that server.
    2. Admission produced a typed denial ⇒ that denial's row, which is strictly
       more actionable than the generic lane row.
    3. Otherwise ⇒ the existing R0 ``mcp_not_registered_on_lane`` row.

    With admission disabled (``admission is None`` or ``enabled=False``) this is
    byte-identical to calling ``mcp_lane_requirement_failures`` directly — the
    flag-off path must not change what R0 reports.
    """

    from .mcp_lane import mcp_lane_requirement_failures, registered_mcp_server_names

    declared = [str(name).strip() for name in declared_servers or [] if str(name or "").strip()]
    if admission is None or not admission.enabled:
        return mcp_lane_requirement_failures(
            declared_servers=declared, lane=lane, registered_servers=registered_servers
        )

    registered = (
        registered_mcp_server_names()
        if registered_servers is None
        else frozenset(str(name).strip() for name in registered_servers or [])
    )
    # Admitted for THIS persona and registered in this process. Intersecting is
    # what stops one persona's live admission from silencing another persona's
    # honest drop row.
    effective = frozenset(admission.server_names) & registered
    denials = {denial.server: denial for denial in admission.denied}

    rows: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for name in declared:
        if name in effective:
            continue
        denial = denials.get(name)
        if denial is not None and denial.code != MCP_ADMISSION_DISABLED:
            rows.append(denial.row())
            continue
        unresolved.append(name)
    rows.extend(
        mcp_lane_requirement_failures(
            declared_servers=unresolved, lane=lane, registered_servers=effective
        )
    )
    return rows


# ── execution (the only side-effecting entry point) ─────────────────────────


def admit_mcp_servers(
    admission: McpAdmission | None,
    *,
    register: Callable[[Mapping[str, Mapping[str, Any]]], Any] | None = None,
    timeout_seconds: float | None = None,
    on_budget_exhausted: Callable[[McpAdmissionDenial, dict[str, Any]], None] | None = None,
) -> McpAdmissionOutcome:
    """Register the admitted servers' tools for this run. Bounded, single-flight.

    Returns an outcome rather than raising: a capability probe must never be able
    to fail a turn. Every degradation is typed —
    ``mcp_admission_lane_busy`` (another admission in flight),
    ``mcp_admission_timeout`` (budget exhausted; the registrar keeps running in
    the background and may land for a LATER turn, which is why the timeout row
    says "not available for this turn" rather than "failed"), and
    ``mcp_not_registered_on_lane`` (the registrar returned but the server is not
    in the registry) — so the turn can state the truth and take the
    ``qa.request_screenshot`` fallback.

    Registration is also where the run's CALL budget is armed: every tool this
    admission puts in the registry is metered by a fresh :class:`McpCallBudget`
    before the caller is allowed to construct the agent, so a run can never see
    an admitted tool that is not counted. ``on_budget_exhausted`` is an optional,
    never-raising notification for the operator surfaces (the runner turns it
    into a ``run.progress`` row); it is called on the FIRST refusal only, from
    whichever thread dispatched the tool.
    """

    if admission is None or admission.is_empty:
        return McpAdmissionOutcome(denied=tuple(admission.denied) if admission else ())

    budget = _positive_float(timeout_seconds) or admission.connect_timeout_seconds
    servers = dict(admission.server_configs)
    started = time.perf_counter()

    if not _ADMISSION_LOCK.acquire(blocking=False):
        busy = tuple(
            McpAdmissionDenial(
                server=name,
                code=MCP_ADMISSION_LANE_BUSY,
                summary=(
                    f"Another MCP admission is in flight in this process, so '{name}' "
                    "was not registered for this turn."
                ),
                fix_hint=(
                    "MCP registration is process-global; admissions are serialized on "
                    "purpose. Retry the turn, or use the server's harness-side contract "
                    "(e.g. qa.request_screenshot) for this one."
                ),
            )
            for name in admission.server_names
        )
        return McpAdmissionOutcome(
            attempted=True,
            duration_ms=int((time.perf_counter() - started) * 1000),
            denied=tuple(admission.denied) + busy,
            execution_denied=busy,
        )

    # Classified HERE — after the mutex, before the registrar — because both
    # facts have to be true at once: nothing else can be registering (so the
    # reading is not racing another admission's spawn), and this run has not yet
    # registered anything (so a cold server still reads cold). Only on the
    # production path: a caller-supplied ``register`` means the live transport
    # map is not what will be consulted, and classifying against it would be a
    # confident label for a path that was never taken.
    transport_paths = classify_admission_transport(admission.server_names) if register is None else {}

    done = threading.Event()
    box: dict[str, Any] = {}
    # One meter per admission ⇒ the budget resets per run by construction, with
    # no reset path to forget to call.
    call_budget = McpCallBudget(admission.max_tool_calls_per_run)

    def _work() -> None:
        try:
            registrar = register or _default_registrar
            box["tools"] = registrar(servers)
            # Meter INSIDE the worker, still holding the admission mutex, so a
            # registration that outran the caller's timeout and lands late is
            # metered too. An admitted tool that is not counted would be exactly
            # the unbounded surface this budget exists to retire.
            _install_call_budget(
                admission.server_names, call_budget, on_exhausted=on_budget_exhausted
            )
        except Exception as exc:  # pragma: no cover - defensive; surfaced as a typed denial
            box["error"] = exc
            logger.warning("MCP admission registration failed: %s", exc, exc_info=True)
        finally:
            # The WORKER releases the mutex, never the caller: on a timeout the
            # caller returns while this registration is still connecting, and
            # releasing here is what keeps a second admission from interleaving
            # against the global registry mid-spawn.
            _ADMISSION_LOCK.release()
            done.set()

    worker = threading.Thread(target=_work, name="mcp-admission", daemon=True)
    try:
        worker.start()
    except Exception:  # pragma: no cover - thread exhaustion; never strand the mutex
        _ADMISSION_LOCK.release()
        logger.warning("MCP admission could not start its registration thread", exc_info=True)
        return McpAdmissionOutcome(attempted=True, denied=tuple(admission.denied))
    finished = done.wait(budget)
    duration_ms = int((time.perf_counter() - started) * 1000)

    if not finished:
        timed_out = tuple(
            McpAdmissionDenial(
                server=name,
                code=MCP_ADMISSION_TIMEOUT,
                summary=(
                    f"'{name}' did not finish registering within {budget:.0f}s, so it is "
                    "not available for this turn."
                ),
                fix_hint=(
                    "The turn continues without it — report that plainly and use the "
                    "server's harness-side contract (e.g. qa.request_screenshot). If the "
                    "server is slow to start, start it before the turn rather than raising "
                    "agent_runtime.mcp_admission.connect_timeout_seconds into the turn budget."
                ),
            )
            for name in admission.server_names
        )
        return McpAdmissionOutcome(
            attempted=True,
            duration_ms=duration_ms,
            denied=tuple(admission.denied) + timed_out,
            execution_denied=timed_out,
            # The meter is already armed and the worker will install it if the
            # registration lands late, so the run stays bounded even on the path
            # where the caller gave up on it.
            call_budget=call_budget,
            # A timeout is almost always a COLD spawn that outran the budget, and
            # saying so is the difference between "MCP is slow" and "that server
            # takes longer to start than the turn allows".
            transport_paths=transport_paths,
        )

    from .mcp_lane import registered_mcp_server_names

    registered = registered_mcp_server_names()
    admitted = tuple(name for name in admission.server_names if name in registered)
    missed = tuple(name for name in admission.server_names if name not in registered)
    tool_names = tuple(str(name) for name in (box.get("tools") or []))
    unregistered = tuple(
        McpAdmissionDenial(
            server=name,
            code=MCP_NOT_REGISTERED_ON_LANE,
            summary=(
                f"'{name}' was admitted for this run but did not register — the server "
                "did not connect or advertised no tools."
            ),
            fix_hint=(
                "Check the server is running and its command resolves on this machine "
                "(hermes harness persona tool-diff <persona> --explain-mcp), then retry."
            ),
        )
        for name in missed
    )
    return McpAdmissionOutcome(
        attempted=True,
        admitted=admitted,
        registered_tool_names=tool_names,
        duration_ms=duration_ms,
        denied=tuple(admission.denied) + unregistered,
        execution_denied=unregistered,
        call_budget=call_budget,
        transport_paths=transport_paths,
    )


# ── the per-run call budget's counting seam ─────────────────────────────────
#
# The count is taken at DISPATCH, over exactly the tools this run's admission
# registered. That is the only place where "an admitted MCP call happened" is a
# fact rather than an inference: the tool list is not the control surface here
# (a model can call an advertised tool any number of times), and the runner's
# tool-start progress hook can only OBSERVE — it has no refuse path that does
# not also end the turn, which §7 does not ask for and §3's threat model does
# not want (an agent that loses the launcher should still be able to write up
# what it saw).
#
# Mechanically: ``registry.dispatch`` reads ``entry.handler`` per call, so
# replacing that attribute with a metered wrapper is a complete interception —
# no upstream edit, no parallel dispatch path, and the wrapper dies with the
# registry scope at teardown.

#: Attribute the wrapper stashes the original handler under. Also the marker
#: that answers "is this handler already metered?", which is what keeps a
#: re-admission after a FAILED teardown from stacking two meters on one tool.
_UNMETERED_HANDLER_ATTR = "_mcp_admission_unmetered_handler"


def _install_call_budget(
    servers: Iterable[str],
    budget: "McpCallBudget",
    *,
    on_exhausted: Callable[[McpAdmissionDenial, dict[str, Any]], None] | None = None,
) -> list[str]:
    """Meter every registered tool of the admitted servers. Fails CLOSED.

    A tool that cannot be metered is DEREGISTERED rather than left callable:
    an unbounded admitted tool is the exact exposure the budget exists to close,
    and removing it degrades into surfaces that already exist — if that empties
    the server's scope, the caller's registry read reports the server as
    ``mcp_not_registered_on_lane`` and the turn takes the fallback lane.

    Returns the prefixed names it could not meter (and therefore removed).
    """

    names = [str(name).strip() for name in servers or () if str(name or "").strip()]
    if not names:
        return []
    try:
        from tools.registry import registry
    except Exception:  # pragma: no cover - registry is always importable in-process
        logger.warning(
            "MCP admission could not reach the tool registry to install the per-run "
            "call budget; the admitted tools are not metered",
            exc_info=True,
        )
        return []

    unmetered: list[str] = []
    for server in names:
        toolset = f"{_MCP_TOOLSET_PREFIX}{server}"
        try:
            tool_names = list(registry.get_tool_names_for_toolset(toolset) or [])
        except Exception:  # pragma: no cover - defensive
            logger.warning("MCP admission could not list %r for metering", toolset, exc_info=True)
            continue
        for tool_name in tool_names:
            if _meter_registered_tool(
                registry, server, tool_name, budget, on_exhausted=on_exhausted
            ):
                continue
            unmetered.append(tool_name)
            try:
                registry.deregister(tool_name)
            except Exception:  # pragma: no cover - defensive
                logger.error(
                    "MCP admission could neither meter nor remove %r; it is admitted "
                    "WITHOUT a per-run call bound",
                    tool_name,
                    exc_info=True,
                )
    if unmetered:
        logger.warning(
            "MCP admission removed %d admitted tool(s) it could not meter: %s",
            len(unmetered),
            ", ".join(sorted(unmetered)),
        )
    return unmetered


def _meter_registered_tool(
    registry: Any,
    server: str,
    tool_name: str,
    budget: "McpCallBudget",
    *,
    on_exhausted: Callable[[McpAdmissionDenial, dict[str, Any]], None] | None = None,
) -> bool:
    """Swap one registered tool's handler for a budget-metered one."""

    try:
        entry = registry.get_entry(tool_name)
        if entry is None:  # pragma: no cover - raced against a deregister
            return False
        handler = getattr(entry, "handler", None)
        if not callable(handler):
            return False
        if getattr(entry, "is_async", False):
            # Upstream registers every MCP tool with ``is_async=False`` (the
            # handler bridges to the MCP loop itself), and the refusal path
            # returns a STRING — which an async entry's dispatch would try to
            # await. Rather than guess at a coroutine shape we do not need, an
            # async admitted tool is left unmetered ⇒ removed by the caller.
            # Pinned by a drift test, so upstream changing this is loud.
            logger.warning(
                "MCP admission cannot meter async tool %r; it will not be admitted",
                tool_name,
            )
            return False
        # Unwrap first: a re-admission after a teardown that FAILED would
        # otherwise meter a meter, and the outer one's budget would be the only
        # one anybody could read.
        base = getattr(handler, _UNMETERED_HANDLER_ATTR, handler)
        entry.handler = _metered_handler(
            base, server=server, tool_name=tool_name, budget=budget, on_exhausted=on_exhausted
        )
        return True
    except Exception:
        logger.warning("MCP admission could not meter %r", tool_name, exc_info=True)
        return False


def _metered_handler(
    handler: Callable[..., Any],
    *,
    server: str,
    tool_name: str,
    budget: "McpCallBudget",
    on_exhausted: Callable[[McpAdmissionDenial, dict[str, Any]], None] | None = None,
) -> Callable[..., Any]:
    """``handler(args, **kwargs) -> str``, charged against the run's call budget.

    On exhaustion it returns the typed row INSTEAD of dispatching, in the same
    ``{"error": ...}`` JSON envelope the MCP handlers already return for their
    circuit breaker — so the model reads it as a normal tool refusal with a
    reason, the turn keeps running, and the agent can still write up what it has.
    """

    def _metered(*args: Any, **kwargs: Any) -> Any:
        denial = budget.consume(server, tool_name)
        if denial is None:
            return handler(*args, **kwargs)
        snapshot = budget.snapshot()
        first = snapshot.get("refused") == 1
        if first and on_exhausted is not None:
            try:
                on_exhausted(denial, snapshot)
            except Exception:  # pragma: no cover - a notification must never fail a tool
                logger.debug("MCP admission budget notification failed", exc_info=True)
        # The first refusal is the event; the rest are the loop it exists to
        # bound, and a looping agent must not be able to flood agent.log.
        logger.log(
            logging.WARNING if first else logging.DEBUG,
            "MCP admission budget exhausted (%d/%d, %d refused): refused %r",
            snapshot.get("spent"),
            snapshot.get("limit"),
            snapshot.get("refused"),
            tool_name,
        )
        payload = dict(denial.row())
        payload["error"] = denial.summary
        payload["tool"] = tool_name
        payload["budget"] = snapshot
        return json.dumps(payload, ensure_ascii=False)

    setattr(_metered, _UNMETERED_HANDLER_ATTR, handler)
    return _metered


def _default_registrar(servers: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Register the admitted subset ONLY, warm-aware.

    ``discover_mcp_tools()`` is never called: it would register everything in the
    profile's config, which is precisely the blast radius admission refuses.

    Since R2 tears the registry scope down after every run while leaving the
    transport warm, a server can be CONNECTED and yet have no registered tools —
    a state ``register_mcp_servers`` cannot repair, because it short-circuits on
    connected servers (``if not new_servers: return _existing_tool_names()``).
    So the admitted set is split: warm servers are re-registered off their live
    session (no spawn, no handshake, and this run's ``tools`` filter applied to
    the already-listed tools), cold ones go through ``register_mcp_servers``.
    """

    warm: dict[str, Mapping[str, Any]] = {}
    cold: dict[str, Any] = {}
    live = _live_mcp_sessions()
    for name, cfg in servers.items():
        (warm if name in live else cold)[name] = cfg

    names: list[str] = []
    for name, cfg in warm.items():
        names.extend(_reregister_warm_server(name, dict(cfg)))
    if cold:
        from tools.mcp_tool import register_mcp_servers

        names.extend(register_mcp_servers({name: dict(cfg) for name, cfg in cold.items()}) or [])
    return names


def classify_admission_transport(servers: Iterable[str] | None) -> dict[str, str]:
    """``{server: warm|cold}`` for an admitted set, read before registration.

    Pure observation over ``tools/mcp_tool._servers``: it decides nothing and
    changes nothing. It exists so a turn can SAY which of the two costs it paid
    instead of leaving a reader to infer it from a millisecond count — see
    :data:`TRANSPORT_WARM` for the measurements that make the distinction worth
    recording, and the analysis it corrects.

    Uses the same liveness predicate :func:`_default_registrar` routes on
    (``_live_mcp_sessions``), so the label can never disagree with the path
    actually taken: a session-less cached entry is COLD to both, because
    ``register_mcp_servers`` is what handles the wake.
    """

    names = [str(name).strip() for name in servers or () if str(name or "").strip()]
    if not names:
        return {}
    live = _live_mcp_sessions()
    return {name: (TRANSPORT_WARM if name in live else TRANSPORT_COLD) for name in names}


def _live_mcp_sessions() -> frozenset[str]:
    """Admitted-server names already connected WITH a live session.

    A cached entry whose ``session`` is ``None`` is parked or mid-reconnect;
    ``register_mcp_servers`` has dedicated wake handling for exactly that case,
    so those are deliberately treated as COLD and left to it.
    """

    try:
        from tools.mcp_tool import _servers
    except Exception:  # pragma: no cover - MCP SDK absent ⇒ nothing is warm
        return frozenset()
    try:
        return frozenset(
            str(name)
            for name, server in dict(_servers).items()
            if getattr(server, "session", None) is not None
        )
    except Exception:  # pragma: no cover - defensive
        logger.debug("MCP admission could not read the warm-server map", exc_info=True)
        return frozenset()


def _reregister_warm_server(name: str, config: dict[str, Any]) -> list[str]:
    """Re-register a connected server's tools under THIS run's tool filter.

    The upstream seam (``tools.mcp_tool._register_server_tools``) is the same one
    dynamic ``notifications/tools/list_changed`` refresh uses to nuke-and-repave
    an MCP server's registry scope, which is exactly the operation R2 needs — it
    honours ``tools.include`` / ``tools.exclude``, re-registers the toolset alias,
    and touches no transport.

    Fails CLOSED. If the seam is gone (upstream drift) or the re-registration
    raises, this returns ``[]`` and the caller's registry read then reports the
    server as ``mcp_not_registered_on_lane`` — an honest "you have no tools this
    turn" rather than a silent fallback to whatever was registered before.
    """

    try:
        from tools.mcp_tool import _register_server_tools, _servers

        server = dict(_servers).get(name)
        if server is None:  # pragma: no cover - raced against a disconnect
            return []
        registered = list(_register_server_tools(name, server, config) or [])
        # Keep ``_existing_tool_names()`` consistent with what is really in the
        # registry, so a later cold registration of a DIFFERENT server does not
        # report this one's stale pre-teardown surface.
        try:
            server._registered_tool_names = list(registered)
        except Exception:  # pragma: no cover - exotic server stub
            pass
        return registered
    except Exception:
        logger.warning(
            "MCP admission could not re-register warm server %r; it will report as "
            "not registered for this run",
            name,
            exc_info=True,
        )
        return []


# ── teardown (the run-scoped half of the lifecycle) ─────────────────────────


def teardown_mcp_admission(
    servers: Iterable[str] | None,
    *,
    lock_timeout_seconds: float = 5.0,
) -> McpTeardownOutcome:
    """Remove an admitted run's registry scope. Keeps the transport warm.

    Deregisters every tool in each admitted ``mcp-<server>`` toolset.
    ``registry.deregister`` exempts ``mcp-*`` from the plugin-ownership gate, and
    dropping the LAST tool of a toolset also drops its toolset check and every
    alias pointing at it — so both spellings a run could have resolved
    (``mcp-launcher_qa`` and the bare ``launcher_qa`` alias) go with it.

    The transport is deliberately untouched: ``tools/mcp_tool._servers`` keeps the
    connection, so the next admitted run re-registers off the live session
    instead of paying a fresh spawn + handshake. Process exit still owns the
    connections (``tools.mcp_tool.shutdown_mcp_servers``).

    Only ever called with servers THIS run admitted, and admission only ever runs
    on the harness lane (``persona_runtime.mission_chat_reply`` is the sole
    producer of ``AgentRunRequest.mcp_admission``), so this can never remove a
    scope that an MCP-registering entry point's ``discover_mcp_tools()`` created.

    Never raises. Every fault becomes a typed ``mcp_admission_teardown_failed``
    row: a finished turn must never be failed by its own cleanup.
    """

    names = [str(name).strip() for name in servers or () if str(name or "").strip()]
    started = time.perf_counter()
    if not names:
        return McpTeardownOutcome()

    failures: list[McpAdmissionDenial] = []
    # A registration whose CALLER timed out keeps running on its worker thread
    # (that is why the worker, not the caller, releases the mutex). Waiting for
    # it here is what stops teardown from racing a late registration and leaving
    # exactly the residue it exists to remove.
    held = _ADMISSION_LOCK.acquire(timeout=max(0.0, float(lock_timeout_seconds)))
    if not held:
        failures.append(
            McpAdmissionDenial(
                server=", ".join(names),
                code=MCP_ADMISSION_TEARDOWN_FAILED,
                summary=(
                    "An MCP admission was still in flight when this run's scope was torn "
                    "down; the scope was removed anyway and a late registration may have "
                    "re-added tools."
                ),
                fix_hint=(
                    "Bounded by agent_runtime.mcp_admission.connect_timeout_seconds. The "
                    "next run's toolset scope still refuses any MCP toolset it was not "
                    "admitted, so this is residue, not exposure."
                ),
            )
        )
    try:
        removed = _deregister_toolset_scopes(names, failures)
    finally:
        if held:
            _ADMISSION_LOCK.release()

    return McpTeardownOutcome(
        servers=tuple(names),
        removed_tool_names=tuple(removed),
        failures=tuple(failures),
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


def _deregister_toolset_scopes(
    names: Sequence[str], failures: list[McpAdmissionDenial]
) -> list[str]:
    """Registry-only scope removal. Never imports ``tools.mcp_tool`` (SDK)."""

    try:
        from tools.registry import registry
    except Exception:  # pragma: no cover - registry is always importable in-process
        failures.append(
            McpAdmissionDenial(
                server=", ".join(names),
                code=MCP_ADMISSION_TEARDOWN_FAILED,
                summary="The tool registry was unavailable, so no admitted MCP scope was removed.",
                fix_hint="The next run's toolset scope still refuses un-admitted MCP toolsets.",
            )
        )
        return []

    removed: list[str] = []
    for name in names:
        toolset = f"{_MCP_TOOLSET_PREFIX}{name}"
        try:
            for tool_name in list(registry.get_tool_names_for_toolset(toolset) or []):
                registry.deregister(tool_name)
                removed.append(tool_name)
        except Exception as exc:
            logger.warning("MCP admission teardown failed for %r: %s", name, exc, exc_info=True)
            failures.append(
                McpAdmissionDenial(
                    server=name,
                    code=MCP_ADMISSION_TEARDOWN_FAILED,
                    summary=(
                        f"'{name}' tools could not be deregistered after the run "
                        f"({type(exc).__name__}), so its registry scope outlived it."
                    ),
                    fix_hint=(
                        "Isolation falls back to the per-run toolset scope until the process "
                        "recycles. Check tools/registry.py deregister for this toolset."
                    ),
                )
            )
    return removed


# ── the agent-visible denial line (design §D3) ──────────────────────────────

#: Bullet prefix, so the line sits in the same list the wall-budget line renders
#: into on the runtime-context envelope's volatile tail.
_ADMISSION_LINE_PREFIX = "- MCP tools:"


def render_mcp_admission_line(
    admission: "McpAdmission | None",
    *,
    outcome: "McpAdmissionOutcome | None" = None,
) -> str:
    """One compact, agent-visible line naming what this turn did NOT get.

    Design §D3. The third visibility surface, and the one that retires W3: a QA
    agent that sees no ``mcp__launcher_qa__*`` tools and no explanation invents
    alternatives (which is how ``pwsh -File`` calls end up in agent output and
    why the launcher repo needs a grep gate for them). Telling it the truth in
    band is cheaper than fencing every workaround it can invent.

    Returns ``""`` when there is genuinely nothing to say: no admission at all,
    or an admission that declared nothing and denied nothing. A persona with no
    MCP server still pays nothing.

    **Every admission that HAPPENED names its servers** (operator ruling,
    2026-07-27). One sentence, two shapes:

    * a PARTIAL admission appends the admitted names after the denial wording;
    * a fully-CLEAN admission renders that sentence on its own.

    The denial half alone is actively misleading on a mixed turn: an agent told
    only "launcher_qa is dark" reads "MCP is dark" and improvises around the
    server it actually has — the same W3 improvisation this line exists to stop,
    arrived at from the other direction. The clean case used to stay silent on
    the theory that an agent which HAS the tools needs no telling; that theory
    assumes the agent reads its tool list rather than its turn context, and when
    it does not, silence is indistinguishable from absence — W3 again, from a
    third direction. Naming what it has costs one sentence and removes the
    inference.

    The denial wording itself is untouched in both shapes, so the flag-on /
    flag-off "one voice" contract on it is byte-for-byte the same
    (``test_mcp_lane_agent_context_line``).

    ``outcome`` contributes only its ``execution_denied`` rows (busy / timeout /
    admitted-but-did-not-register). The policy denials it also carries are
    already on the turn's envelope, and saying the same thing twice in two
    voices is how an agent learns to discount both. Pass ``admission=None`` with
    an ``outcome`` to render the execution half alone — that is what the runner's
    in-band backstop does.

    Pure and volatile: rendered per turn onto the runtime-context envelope's
    volatile tail (exactly like ``turn_budget.render_turn_budget_line``) and
    never folded into the hashed HUD body, so a cached ``unchanged`` delivery can
    never show the agent a stale capability claim.
    """

    denials = list(admission.denied) if admission is not None else []
    if outcome is not None:
        seen = {(denial.server, denial.code) for denial in denials}
        denials.extend(
            denial
            for denial in outcome.execution_denied
            if (denial.server, denial.code) not in seen
        )
    if not denials:
        # Nothing was refused. If something was ADMITTED, say so — the positive
        # half is the whole line on a clean turn.
        clean = _admitted_clause(admission, {})
        return f"{_ADMISSION_LINE_PREFIX}{clean}" if clean else ""

    # One entry per server, first (most specific) code wins — resolution denials
    # are ordered narrowest-first and execution rows are appended after them.
    ordered: dict[str, str] = {}
    for denial in denials:
        ordered.setdefault(str(denial.server), str(denial.code))
    detail = ", ".join(f"{server} ({code})" for server, code in ordered.items())
    return (
        f"{_ADMISSION_LINE_PREFIX} {detail} — declared for this persona but NOT available "
        "on this turn, so no mcp__<server>__* tools for it are in your tool list. This is a "
        "capability fact, not a permission problem: do not retry, do not hunt for a "
        "permission mode, and do not substitute a shell/PowerShell workaround or a second "
        "lane. Use the server's harness-side contract instead (for launcher_qa: the "
        "qa.request_screenshot decision contract), and say plainly in your reply that the "
        "tools were unavailable." + _admitted_clause(admission, ordered)
    )


def _admitted_clause(
    admission: "McpAdmission | None", denied_servers: "Mapping[str, str]"
) -> str:
    """The positive half of an admission — ``""`` when nothing was admitted.

    Rendered on BOTH shapes: appended after the denial wording on a partial
    admission, and standing alone as the whole line on a clean one. Same bytes
    either way, so there is one sentence to read and one place to change it.

    Admitted-then-degraded servers are excluded by construction: anything that
    reached ``execution_denied`` is already in ``denied_servers``, so a server
    can never be reported as both available and unavailable on one line.
    """

    names = [
        str(server)
        for server in getattr(admission, "server_names", ()) or ()
        if str(server) and str(server) not in denied_servers
    ]
    if not names:
        return ""
    listed = ", ".join(dict.fromkeys(names))
    return (
        f" Admitted on this turn: {listed}; those servers' mcp__<server>__* tools ARE "
        "in your tool list, so call them directly."
    )
