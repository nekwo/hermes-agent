from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class RedactionStatus(StrEnum):
    SAFE = "safe"
    NEEDS_SCAN = "needs_scan"
    UNSAFE = "unsafe"


SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?i)Authorization:\s*Bearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(X-Amz-Signature|Signature|Credential)=")
]


class RedactionScanner(Protocol):
    def scan_text(self, path: Path) -> RedactionStatus: ...
    def scan_image(self, path: Path) -> RedactionStatus: ...


class BasicRedactionScanner:
    def scan_text(self, path: Path) -> RedactionStatus:
        text = path.read_text(encoding="utf-8", errors="ignore")
        return RedactionStatus.UNSAFE if any(p.search(text) for p in SECRET_PATTERNS) else RedactionStatus.SAFE

    def scan_image(self, path: Path) -> RedactionStatus:
        return RedactionStatus.NEEDS_SCAN
