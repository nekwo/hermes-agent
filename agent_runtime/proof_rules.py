from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable


class ProofType(StrEnum):
    DIFF = "diff"
    DIFF_STAT = "diff_stat"
    COMMIT = "commit"
    TEST_RUN = "test_run"
    SCREENSHOT = "screenshot"
    VIDEO = "video"
    LOG = "log"
    URL = "url"
    TEXT = "text"
    ARTIFACT = "artifact"
    QA_VERDICT = "qa_verdict"
    PM_APPROVAL = "pm_approval"
    REDACTION_SCAN = "redaction_scan"


@dataclass(frozen=True, slots=True)
class ProofRequirement:
    required_types: frozenset[ProofType]

    def missing_from(self, available: Iterable[ProofType | str]) -> frozenset[ProofType]:
        available_types = {item if isinstance(item, ProofType) else ProofType(item) for item in available}
        return frozenset(self.required_types - available_types)

    def is_satisfied_by(self, available: Iterable[ProofType | str]) -> bool:
        return not self.missing_from(available)
