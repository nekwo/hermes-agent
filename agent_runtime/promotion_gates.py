from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .decision_schema import DecisionPayloadInvalid
from .mission_plan import task_stage_records
from .models import Proof, Task
from .proof_rules import ProofType
from .stage_intent import stage_requires_product_edit


@dataclass(frozen=True, slots=True)
class PromotionLane:
    id: str
    label: str


PROMOTION_LANES: tuple[PromotionLane, ...] = (
    PromotionLane("local", "local deterministic product tests"),
    PromotionLane("local_docker_postgres", "local Docker/PostgreSQL integration tests"),
    PromotionLane("staging_k8", "remote test staging k8 pod validation"),
    PromotionLane("prod_rollout", "production pod rollout"),
)

_PRODUCT_REPOS = {"EterniaBackend", "EterniaLauncher"}
_LOCAL_MARKERS: dict[str, tuple[str, ...]] = {
    "EterniaBackend": ("manage.py check", "manage.py test", "scripts/test.sh"),
    "EterniaLauncher": ("flutter analyze", "flutter build", "flutter test"),
}
_LOCAL_DOCKER_MARKERS = (
    "tests/docker/",
    "tests/docker",
    "docker compose",
    "docker-compose",
)
_POSTGRES_MARKERS = (
    "postgres",
    "postgresql",
)
_NON_RELEASE_DB_MARKERS = ("--sqlite", "sqlite", "mocked-only")
_K8_MARKERS = ("kubectl", "k8", "k8s", "kubernetes", "pod", "pods", "rollout")
_GIT_PUSH_MARKERS = ("git push",)
_GIT_SYNC_MARKERS = ("git pull --rebase", "git fetch", "git rebase")
_STAGING_WORD = re.compile(r"(?<![a-z0-9])(?:staging|stagec|test[-_ ]?staging)(?![a-z0-9])", re.I)
_PROD_WORD = re.compile(r"(?<![a-z0-9])(?:prod|production)(?![a-z0-9])", re.I)
_NON_RELEASE_PATH_PREFIXES = ("docs/", "doc/", "documentation/")
_NON_RELEASE_PATH_NAMES = {"readme.md", "changelog.md"}


def product_promotion_required(task: Task) -> bool:
    """Return true when a task edits product code that must pass release promotion."""

    stages = list(getattr(getattr(task, "mission_plan", None), "stages", []) or [])
    for stage in stages:
        repo = str(getattr(stage, "repo", "") or "").strip()
        if (
            repo in _PRODUCT_REPOS
            and bool(getattr(stage, "requires_product_edit", False))
            and not _stage_is_non_release_path_only(stage)
        ):
            return True
    for stage in list(task_stage_records(task)):
        if stage_requires_product_edit(task, stage) and not _stage_is_non_release_path_only(stage):
            repos = {str(repo).strip() for repo in (getattr(task, "affected_repos", []) or []) if str(repo).strip()}
            return not repos or bool(repos.intersection(_PRODUCT_REPOS))
    return False


def _stage_is_non_release_path_only(stage: Any) -> bool:
    paths = [str(path).strip() for path in (getattr(stage, "affected_paths", []) or []) if str(path).strip()]
    return bool(paths) and all(_is_non_release_path(path) for path in paths)


def _is_non_release_path(path: str) -> bool:
    normalized = str(path or "").replace("\\", "/").strip().lower().lstrip("./")
    if normalized in _NON_RELEASE_PATH_NAMES:
        return True
    return any(normalized.startswith(prefix) for prefix in _NON_RELEASE_PATH_PREFIXES)


def validate_product_promotion_gate(task: Task, proof_ids: Iterable[str], *, proof_store=None) -> None:
    if proof_store is None or not product_promotion_required(task):
        return
    proofs = _collect_proofs(task, proof_ids, proof_store=proof_store)
    required_lanes = _required_promotion_lanes(task)
    lane_times = _promotion_lane_times(task, proofs)
    satisfied = set(lane_times)
    missing = [lane for lane in required_lanes if lane.id not in satisfied]
    if not missing:
        ordered_times = [lane_times[lane.id] for lane in required_lanes]
        if ordered_times != sorted(ordered_times):
            raise DecisionPayloadInvalid(
                "product-edit production promotion gate is out of order: "
                f"required order is {_required_order_text(required_lanes)}."
            )
        return
    missing_labels = ", ".join(lane.label for lane in missing)
    raise DecisionPayloadInvalid(
        "product-edit production promotion gate is incomplete: "
        f"missing passed proof for {missing_labels}. Required order is {_required_order_text(required_lanes)}."
    )


def satisfied_promotion_lanes(task: Task, proofs: Iterable[Proof]) -> set[str]:
    return set(_promotion_lane_times(task, proofs))


def _promotion_lane_times(task: Task, proofs: Iterable[Proof]) -> dict[str, Any]:
    repos = _product_repos(task)
    satisfied: dict[str, Any] = {}
    for proof in proofs:
        if proof.type != ProofType.TEST_RUN:
            continue
        metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
        if str(metadata.get("status") or "").strip().lower() != "passed":
            continue
        text = _proof_text(proof)
        intent = str(metadata.get("proof_intent") or "").strip().lower()
        if _is_local_proof(text, intent, repos):
            satisfied.setdefault("local", proof.created_at)
        if _is_local_docker_postgres_proof(text, intent):
            satisfied.setdefault("local_docker_postgres", proof.created_at)
        if _is_staging_k8_proof(text, intent):
            satisfied.setdefault("staging_k8", proof.created_at)
        if _is_prod_rollout_proof(text, intent):
            satisfied.setdefault("prod_rollout", proof.created_at)
    return satisfied


def _collect_proofs(task: Task, proof_ids: Iterable[str], *, proof_store) -> list[Proof]:
    wanted = list(dict.fromkeys([*(getattr(task, "proof_ids", []) or []), *[str(item).strip() for item in proof_ids if str(item).strip()]]))
    proofs: list[Proof] = []
    for proof_id in wanted:
        try:
            proof = proof_store.get(proof_id)
        except Exception:
            continue
        if proof.task_id == task.id:
            proofs.append(proof)
    return proofs


def _product_repos(task: Task) -> set[str]:
    repos = {str(repo).strip() for repo in (getattr(task, "affected_repos", []) or []) if str(repo).strip()}
    plan = getattr(task, "mission_plan", None)
    for stage in list(getattr(plan, "stages", []) or []):
        repo = str(getattr(stage, "repo", "") or "").strip()
        if repo:
            repos.add(repo)
        repos.update(_repo_labels_from_text(" ".join([str(getattr(stage, "title", "") or ""), str(getattr(stage, "objective", "") or "")])))
    for stage in list(task_stage_records(task)):
        repo = str(getattr(stage, "repo_scope_label", "") or getattr(stage, "repo_scope", "") or "").strip()
        if repo:
            repos.add(repo)
        repos.update(_repo_labels_from_text(" ".join([str(getattr(stage, "title", "") or ""), str(getattr(stage, "objective", "") or "")])))
    return repos.intersection(_PRODUCT_REPOS) or repos


def _repo_labels_from_text(text: str) -> set[str]:
    lowered = text.lower()
    labels: set[str] = set()
    if "eterniabackend" in lowered or "backend" in lowered:
        labels.add("EterniaBackend")
    if "eternialauncher" in lowered or "launcher" in lowered:
        labels.add("EterniaLauncher")
    return labels


def _required_promotion_lanes(task: Task) -> tuple[PromotionLane, ...]:
    repos = _product_repos(task)
    required = [PROMOTION_LANES[0]]
    if not repos or "EterniaBackend" in repos:
        required.append(PROMOTION_LANES[1])
    required.extend(PROMOTION_LANES[2:])
    return tuple(required)


def _required_order_text(lanes: Iterable[PromotionLane]) -> str:
    ordered = tuple(lanes)
    return ", ".join(lane.label for lane in ordered[:-1]) + f", then {ordered[-1].label}"


def _proof_text(proof: Proof) -> str:
    metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
    parts: list[str] = [
        str(proof.title or ""),
        str(proof.path_or_value or ""),
        str(metadata.get("command") or ""),
        str(metadata.get("proof_intent") or ""),
        str(metadata.get("stdout_excerpt") or ""),
        str(metadata.get("stderr_excerpt") or ""),
    ]
    return " ".join(parts).lower()


def _is_local_proof(text: str, intent: str, repos: set[str]) -> bool:
    if "auto_final_gate_after_delivery" in intent or "local" in intent:
        if "docker" in intent or "postgres" in intent or "postgresql" in intent:
            return False
        return True
    markers: list[str] = []
    for repo in repos or _PRODUCT_REPOS:
        markers.extend(_LOCAL_MARKERS.get(repo, ()))
    return any(marker.lower() in text for marker in markers)


def _is_local_docker_postgres_proof(text: str, intent: str) -> bool:
    if any(marker in text or marker in intent for marker in _NON_RELEASE_DB_MARKERS):
        return False
    if "local_docker_postgres" in intent or "docker_postgres" in intent:
        return True
    has_docker = any(marker in text for marker in _LOCAL_DOCKER_MARKERS)
    has_postgres = any(marker in text for marker in _POSTGRES_MARKERS)
    return has_docker and has_postgres


def _is_staging_k8_proof(text: str, intent: str) -> bool:
    if "staging_k8" in intent or "staging_pod" in intent:
        return True
    return bool(_STAGING_WORD.search(text)) and any(marker in text for marker in _K8_MARKERS)


def _is_prod_rollout_proof(text: str, intent: str) -> bool:
    if "prod_rollout" in intent or "production_rollout" in intent or "prod_pod" in intent:
        if any(marker in text for marker in _GIT_PUSH_MARKERS):
            return _is_push_triggered_prod_proof(text)
        return True
    return bool(_PROD_WORD.search(text)) and (any(marker in text for marker in _K8_MARKERS) or _is_push_triggered_prod_proof(text))


def _is_push_triggered_prod_proof(text: str) -> bool:
    if not any(marker in text for marker in _GIT_PUSH_MARKERS):
        return False
    if not any(marker in text for marker in _GIT_SYNC_MARKERS):
        return False
    return bool(_PROD_WORD.search(text)) or "deploy" in text or "deployment" in text
