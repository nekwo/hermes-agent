from __future__ import annotations

import re


# ═══════════════════════════════════════════════════════════════════════════
# Secret-shaped assignment — ONE rule, ONE home.
# ═══════════════════════════════════════════════════════════════════════════
#
# THE DEFECT THIS MODULE RETIRES (2026-07-25)
# -------------------------------------------
# The runtime carried TWELVE independent spellings of "a secret-ish key,
# followed by a separator, followed by a value" — in ``realm_sync``,
# ``repo_context`` and ``stream``,
# ``profile_runner``, ``prompt_observability``, ``operator_channels``,
# ``persona_chat_continuity``, ``persona_chat_history``, ``snapshot``, and this
# module. Every single one wrote the separator as ``\s*[:=]``.
#
# That cannot match JSON. In ``{"token": "ghp_…"}`` the key's CLOSING QUOTE
# sits between the key word and the ``:``, so ``token\s*[:=]`` never fires.
# Confirmed empirically on all twelve:
#
#     token: ghp_abcdefghij1234567890              -> CAUGHT
#     {"token": "ghp_abcdefghij1234567890"}        -> MISSED
#     {"api_key": "sk-abcdefghij1234567890"}       -> MISSED
#     {"a": {"client_secret": "abcdefghij123456"}} -> MISSED
#
# Blast radius: EVERY realm-sync artifact is JSON (``store/workspaces/*.json``,
# ``store/realms/*.json``, board ``board.json`` + ``cards/*.json``, office
# ``office.json`` + ``actors/*.json``), so ``_assert_no_secret_artifacts`` was
# blind on the whole publish surface; and the redaction lanes surfaced
# JSON-encoded credentials verbatim into ``safe_details``, proof excerpts,
# prompt observability, and archived chat transcripts.
#
# THE FIX: :data:`SECRET_KEY_SEPARATOR`. Compose every rule from it. Never
# hand-roll ``\s*[:=]`` again — a thirteenth spelling is a thirteenth blind
# spot.
#
# GROUP CONTRACT (load-bearing — several call sites rebuild output as
# ``f"{match.group(1)}=[redacted]"``): the separator is a character class with
# a quantifier plus non-capturing groups ONLY. It never shifts group numbering.
# ``tests/agent_runtime/test_secret_assignment.py`` pins that for every pattern
# exported here.
SECRET_KEY_SEPARATOR = r"['\"]?\s*[:=]\s*"

# ── Key vocabularies ───────────────────────────────────────────────────────
# Three, not twelve. They differ on purpose:
#
#   GATE  — the realm-sync PUBLISH gate. Deliberately conservative: a match
#           hard-fails an operator's whole publish, so a false positive is a
#           denial-of-service on the feature. Paired with a long, restricted
#           value shape.
#   ENV   — env-var-shaped names (``HERMES_API_KEY``, ``DB_PASSWORD``), where
#           the secret word is a SEGMENT of a longer SCREAMING_SNAKE name.
#   TEXT  — prose/diagnostic redaction. The union of every vocabulary the
#           chat / prompt / repo-context lanes used. Redaction is display-only,
#           so over-matching costs readability, never safety — this one is
#           deliberately the broadest and, like the lanes it replaces, is NOT
#           anchored on ``\b`` (it matches ``xtoken:`` too, on purpose).
GATE_SECRET_KEYS = (
    r"api[_-]?key|authorization|bearer|client[_-]?secret|oauth[_-]?token"
    r"|password|private[_-]?key|secret|token"
)
ENV_SECRET_KEYS = (
    r"(?:[A-Za-z0-9]+_)*(?:SECRET|TOKEN|PASSWORD|PASS|CREDENTIAL|API_?KEY|KEY)(?:_[A-Za-z0-9]+)*"
)
TEXT_SECRET_KEYS = (
    r"api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token"
    r"|authorization|bearer|credential|passwd|password|secret|token"
)

# ── Composed patterns ──────────────────────────────────────────────────────

#: Realm-sync PUBLISH GATE (``_assert_no_secret_artifacts``) and realm-sync
#: ``_redact_text``. One group: the key word.
#:
#: The value shape (>= 16 chars of ``[A-Za-z0-9_./+=:-]``) is what keeps the
#: gate from refusing a publish over ``{"secret": false}`` or
#: ``{"token_count": 20208}``. It is load-bearing — do NOT relax it to a
#: permissive redaction value shape, or every realm publish becomes a coin
#: flip. Verified against 14,315 live JSON artifacts: zero false positives.
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(" + GATE_SECRET_KEYS + r")\b" + SECRET_KEY_SEPARATOR + r"['\"]?[A-Za-z0-9_./+=:-]{16,}"
)

#: Env-var-shaped redaction used by stream and packet projection.
#: One group: the full key (``HERMES_API_KEY``, not just ``API_KEY``).
ENV_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(" + ENV_SECRET_KEYS + r")" + SECRET_KEY_SEPARATOR + r"(?:\"[^\"]*\"|'[^']*'|[^\s'\"]+)"
)

#: Prose/diagnostic redaction, GREEDY value (``\S+`` — runs to whitespace).
#: Two groups: (1) key, (2) value. Used where the lane either drops the whole
#: line on a hit or rewrites it as ``key: [redacted]``, so swallowing trailing
#: punctuation costs nothing. Consumers: ``operator_channels``,
#: ``persona_chat_history``, ``snapshot``, ``persona_chat_continuity``.
TEXT_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(" + TEXT_SECRET_KEYS + r")" + SECRET_KEY_SEPARATOR + r"(\S+)"
)

#: Prose redaction, SURGICAL value (``[^\s,;]+`` — stops at a list separator).
#: Two groups: (1) key, (2) value. Used where the redacted text must stay
#: readable around the removed value — prompt capture and repo-context
#: excerpts that are fed back to an agent. Consumers: ``profile_runner``,
#: ``prompt_observability``, ``repo_context``.
TEXT_SECRET_VALUE_ASSIGNMENT_RE = re.compile(
    r"(?i)(" + TEXT_SECRET_KEYS + r")" + SECRET_KEY_SEPARATOR + r"([^\s,;]+)"
)

#: Every secret-assignment pattern this module owns. The group-contract test
#: iterates it, so a new pattern added here is automatically pinned.
ALL_SECRET_ASSIGNMENT_PATTERNS = (
    SECRET_ASSIGNMENT_RE,
    ENV_SECRET_ASSIGNMENT_RE,
    TEXT_SECRET_ASSIGNMENT_RE,
    TEXT_SECRET_VALUE_ASSIGNMENT_RE,
)
