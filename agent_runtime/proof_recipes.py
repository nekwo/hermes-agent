from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .decision_schema import AgentDecision, DecisionPayloadInvalid
from .models import Task
from .stage_intent import no_product_edit_recipe_conflicts_with_stage
from .states import StageStatus


_RECIPE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")


@dataclass(frozen=True, slots=True)
class ProofRecipe:
    id: str
    commands: tuple[str, ...]
    repo_scope: str | None = None
    mode: str = "no_product_edit"
    version: int = 1
    writes_product_probe: bool = False
    cleanup: str = "manifest_verified"
    timeout_seconds: int | None = None
    description: str = ""
    expected_markers: tuple[str, ...] = field(default_factory=tuple)
    expected_markers_by_command: tuple[tuple[str, ...], ...] = field(default_factory=tuple)

    def metadata(self) -> dict[str, Any]:
        payload = {
            "recipe_id": self.id,
            "recipe_version": self.version,
            "recipe_hash": self.hash,
            "repo_scope": self.repo_scope,
            "mode": self.mode,
            "writes_product_probe": self.writes_product_probe,
            "cleanup": self.cleanup,
            "timeout_seconds": self.timeout_seconds,
            "expected_markers": list(self.expected_markers),
            "expected_markers_by_command": [list(markers) for markers in self.expected_markers_by_command],
        }
        return {key: value for key, value in payload.items() if _metadata_value_present(value)}

    @property
    def hash(self) -> str:
        payload = {
            "id": self.id,
            "commands": list(self.commands),
            "repo_scope": self.repo_scope,
            "mode": self.mode,
            "version": self.version,
            "writes_product_probe": self.writes_product_probe,
            "cleanup": self.cleanup,
            "timeout_seconds": self.timeout_seconds,
            "expected_markers": list(self.expected_markers),
            "expected_markers_by_command": [list(markers) for markers in self.expected_markers_by_command],
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


RECIPES: dict[str, ProofRecipe] = {
    "harness_runtime_status_snapshot": ProofRecipe(
        id="harness_runtime_status_snapshot",
        repo_scope="hermes-agent",
        version=2,
        commands=(
            "python -m hermes_cli.main harness status --json",
            "python -m hermes_cli.main harness snapshot --json",
            "python -m hermes_cli.main harness contracts verify-examples --json",
        ),
        description="Collect redaction-safe Harness status, snapshot, and contract verification proof.",
        expected_markers=("open_tasks", "schema_version", "contract_hash"),
        expected_markers_by_command=(("open_tasks",), ("schema_version",), ("contract_hash", "ok")),
    ),
    "archive_button_cli_contract": ProofRecipe(
        id="archive_button_cli_contract",
        repo_scope="hermes-agent",
        commands=(
            "python -m hermes_cli.main harness task archive --help",
            "python -m hermes_cli.main harness task archive-ready --help",
        ),
        description="Prove Mission Control archive CLI contract is callable.",
        expected_markers=("archive", "archive-ready"),
        expected_markers_by_command=(("archive",), ("archive-ready",)),
    ),
    "backend_contract_smoke": ProofRecipe(
        id="backend_contract_smoke",
        repo_scope="EterniaBackend",
        commands=(
            "python -c \"print('backend_contract_smoke backend_proof emitted contract_packet')\"",
        ),
        description="Bounded backend contract smoke fallback when a product-specific command is not supplied.",
        expected_markers=("backend_contract_smoke", "contract_packet"),
    ),
    "launcher_contract_smoke": ProofRecipe(
        id="launcher_contract_smoke",
        repo_scope="EterniaLauncher",
        commands=(
            "python -c \"print('launcher_contract_smoke contract_packet_consumed backend_proof_consumed')\"",
        ),
        description="Bounded Launcher contract-consumption smoke fallback when a product-specific command is not supplied.",
        expected_markers=("launcher_contract_smoke", "backend_proof_consumed"),
    ),
    "qa_release_verdict_smoke": ProofRecipe(
        id="qa_release_verdict_smoke",
        repo_scope="hermes-agent",
        commands=(
            "python -m hermes_cli.main harness observe --json",
        ),
        description="Collect redaction-safe observability proof before QA verdict.",
        expected_markers=("health", "signals"),
    ),
}


def resolve_proof_recipe(recipe_id: str) -> ProofRecipe:
    safe_id = str(recipe_id or "").strip()
    if not safe_id or not _RECIPE_ID_PATTERN.match(safe_id):
        raise DecisionPayloadInvalid("request_test_run recipe_id must be a redaction-safe token")
    recipe = RECIPES.get(safe_id)
    if recipe is None:
        raise DecisionPayloadInvalid(f"unknown request_test_run recipe_id: {safe_id}")
    return recipe


def normalize_request_test_run_decision(task: Task, decision: AgentDecision) -> ProofRecipe | None:
    payload = decision.payload if isinstance(decision.payload, dict) else {}
    recipe_id = str(payload.get("recipe_id") or "").strip()
    if not recipe_id:
        return None
    recipe = resolve_proof_recipe(recipe_id)
    requested_stage_id = str(payload.get("stage_id") or getattr(task, "current_stage_id", None) or "").strip()
    current_stage_id = str(getattr(task, "current_stage_id", None) or "").strip()
    requested_stage = next((stage for stage in getattr(task, "stages", []) or [] if stage.id == requested_stage_id), None)
    current_stage = next((stage for stage in getattr(task, "stages", []) or [] if stage.id == current_stage_id), None)
    if no_product_edit_recipe_conflicts_with_stage(task, requested_stage, recipe.id):
        raise DecisionPayloadInvalid(
            f"request_test_run recipe_id {recipe.id!r} is no-product-edit proof and cannot satisfy product-edit stage {requested_stage_id!r}"
        )
    current_stage_incomplete = current_stage is not None and current_stage.status not in {StageStatus.READY_FOR_QA, StageStatus.PASSED}
    if current_stage_id and requested_stage_id != current_stage_id and current_stage_incomplete and no_product_edit_recipe_conflicts_with_stage(task, current_stage, recipe.id):
        raise DecisionPayloadInvalid(
            f"request_test_run recipe_id {recipe.id!r} cannot bypass incomplete product-edit stage {current_stage_id!r}"
        )
    explicit_commands = [str(command).strip() for command in payload.get("commands") or [] if str(command).strip()]
    if explicit_commands and explicit_commands != list(recipe.commands):
        raise DecisionPayloadInvalid("request_test_run with recipe_id must not override recipe commands")
    payload["recipe_id"] = recipe.id
    payload["commands"] = list(recipe.commands)
    if recipe.repo_scope:
        payload.setdefault("repo_scope", recipe.repo_scope)
    if not str(payload.get("proof_intent") or "").strip():
        payload["proof_intent"] = recipe.description or f"Run proof recipe {recipe.id}"
    return recipe


def proof_recipe_metadata(payload: dict[str, Any]) -> dict[str, Any] | None:
    recipe_id = str(payload.get("recipe_id") or "").strip()
    if not recipe_id:
        return None
    recipe = resolve_proof_recipe(recipe_id)
    return dict(recipe.metadata())


def _metadata_value_present(value: Any) -> bool:
    if value is None:
        return False
    if value == "":
        return False
    if value == []:
        return False
    return True
