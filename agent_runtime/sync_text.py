"""The ONE line-ending canonicalization for realm sync.

Lifted VERBATIM out of ``realm_sync._canonicalize_text_bytes`` on 2026-09-12,
when the SKILL family joined the three-way pull model
(``EterniaLauncher/docs/mission_control/planned/held-skill-publish-direction.md``
§4.1). ``realm_sync._canonicalize_text_bytes`` is now an alias of
:func:`canonicalize_text_bytes`, so the publish path's behaviour is unchanged
and the lift is proven by the existing EOL suite
(``tests/agent_runtime/test_realm_sync_eol.py``) passing unmodified.

Why it had to move: the skill lane needs an EOL-agnostic package hash, and
:mod:`agent_runtime.skill_promotion` may NOT import :mod:`agent_runtime.realm_sync`
(the pull pipeline imports the promotion door, so the dependency is
one-directional — see that module's header). A second copy of this rule in the
skill lane is exactly the defect that produced the phantom hold the note's §1
measured: a Windows editor wrote the canonical package with CRLF, the inbox
mirror held the same content with LF, and two hashes of one package disagreed
forever.
"""

from __future__ import annotations


def canonicalize_text_bytes(raw: bytes) -> bytes:
    """Normalize text bytes' line endings to LF — the ONE canonicalization
    chokepoint for realm sync.

    Realm-sync artifacts are read from stores that write CRLF on Windows
    (``atomic_json_write`` / ``str.write_text`` use text mode) while the pull
    lane writes LF (``json.dumps(...).encode()``). Committing those raw bytes
    turns every publish into a whole-file CRLF<->LF churn and reports
    ``changed=true`` on no-op runs; diffs/merges between members carry EOL noise.
    LF-normalizing at the write/copy boundary keeps the repo tree byte-stable.

    Binary/asset artifacts (skill PNG/JPG/…) are detected by a NUL byte — git's
    own text/binary heuristic — and passed through byte-for-byte untouched. The
    3-way merge classifiers and office/board baselines hash the PARSED model
    (EOL-agnostic), so canonicalizing bytes here never desyncs those hashes.
    """

    if b"\x00" in raw:
        return raw
    return raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
