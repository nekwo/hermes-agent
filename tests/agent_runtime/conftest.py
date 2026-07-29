import pytest


@pytest.fixture
def bundled_persona_profiles():
    """Provision the Hermes profile homes the bundled personas BIND.

    Since ``9ad9c8017`` (*fix(harness): provision bundled agent personas*,
    2026-07-19) ``default_personas()`` binds a real profile to every advertised
    agent — ``dev`` → ``launcher-dev``, ``backend_dev`` → ``backend-dev``,
    ``qa`` → ``qa`` — and ``GPTPersonaRuntime._invoke_agent`` refuses to run a
    persona whose bound profile does not exist ("every advertised agent must
    have a real Hermes profile behind it"). The autouse hermetic fixture points
    ``HERMES_HOME`` at a per-test tempdir, where none of them do.

    Production provisions them with ``harness init --with-bundled-personas``;
    a test that drives the REAL run path is asking for the same precondition.
    Opt in explicitly — tests that assert the *missing*-profile refusal must not
    have it provisioned underneath them.
    """

    from agent_runtime.personas import BUNDLED_PERSONA_PROFILES
    from hermes_cli.profiles import get_profile_dir

    homes = []
    for profile in dict.fromkeys(BUNDLED_PERSONA_PROFILES.values()):
        home = get_profile_dir(profile)
        home.mkdir(parents=True, exist_ok=True)
        homes.append(home)
    return homes


@pytest.fixture(autouse=True)
def isolate_agent_runtime_root(tmp_path, monkeypatch):
    root = tmp_path / "agent-runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    yield root


# S7-B RULING-0 COMPAT STRIP (2026-07-16): the ``history_in_frame_config`` and
# ``inline_prompt_payloads_config`` kill-switch fixtures were removed with the
# flags they flipped. The evicted/hoisted read-model shape is the only shape;
# tests that used to assert the legacy full-in-frame / inline content now
# re-target the on-disk archive artifacts + the paged history / on-demand fetch
# paths (see test_snapshot_history_eviction.py, test_snapshot.py,
# test_persona_assignments.py, test_stage52_role_envelopes.py).


def _bundled_stage_shape(blueprint_id, stage_id):
    """Return the *unspecialized* bundled stage for ``stage_id``.

    Derived, not hardcoded: re-instantiating the blueprint gives exactly what
    production hands a graph before any task specialization runs, so a restore
    built from it can never drift from
    ``agent_runtime/blueprints/neko_two_dev_default.yaml``. Returns ``None`` for
    a plan that did not come from a bundled blueprint (legacy/typed plans).
    """

    from agent_runtime.blueprints import BlueprintStore, instantiate_blueprint
    from agent_runtime.default_plan import DEFAULT_TASK_BLUEPRINT_BINDINGS, DEFAULT_TASK_BLUEPRINT_ID

    if str(blueprint_id or "") != DEFAULT_TASK_BLUEPRINT_ID:
        return None
    try:
        bp = BlueprintStore().get(DEFAULT_TASK_BLUEPRINT_ID)
        pristine = instantiate_blueprint(bp, goal="", bindings=dict(DEFAULT_TASK_BLUEPRINT_BINDINGS))
    except Exception:
        return None
    return next((stage for stage in pristine.stages if stage.id == stage_id), None)


def release_to_implementation(task, *, owner_slots=("dev", "backend_dev")):
    """Graph-type ``task`` and release it to its first implementation stage.

    Stage 15.3/15.4 (`docs/agent-runtime-harness/15-legacy-orchestrator-retirement.md`):
    routing is now graph-derived and there is exactly one orchestrator, so a
    mission must be **scoped** before any dev slot is dispatched. Tests that
    exercise *dev-run mechanics* — proof collection, budgets, session reuse,
    workdir resolution, worker sessions, assignments — used to reach a dev run
    by handing the engine a plan-less task, which the retired legacy
    orchestrator routed to a dev slot by inferring one from the mission text.

    Those tests are not about scoping. This helper gives them the precondition
    explicitly: instantiate the default graph, mark the scope stage passed, and
    park the mission on the implementation stage its ``affected_repos`` select.
    """

    from agent_runtime.default_plan import ensure_default_mission_plan
    from agent_runtime.states import StageStatus

    plan = ensure_default_mission_plan(task)
    for stage in plan.stages:
        if stage.kind == "scope" or stage.id == "scope":
            stage.status = StageStatus.PASSED
    # Priority order, not set membership: when the mission's repo scope does not
    # single out a lane, "the dev stage" means the generic `dev` implementation
    # lane, not the backend one. A repo-scoped mission has the other lane already
    # marked PASSED/not_applicable by `specialize_default_plan_for_task`, so the
    # scoped case still resolves to the right owner.
    target = None
    for slot in owner_slots:
        target = next(
            (
                stage
                for stage in plan.stages
                if (stage.owner_slot or stage.owner) == slot and stage.status != StageStatus.PASSED
            ),
            None,
        )
        if target is not None:
            break
    reopened = False
    if target is None:
        # Every product lane was marked not_applicable by the repo
        # specialization — the case for a mission pinned to `hermes-agent`,
        # which the default graph has no lane for. These tests still mean "the
        # mission is on its implementation stage", so re-open the `dev` lane
        # instead of leaving the graph fully passed.
        target = next(
            (stage for stage in plan.stages if (stage.owner_slot or stage.owner) == owner_slots[0]),
            None,
        )
        reopened = target is not None
    if target is not None:
        # Routing is stage-derived now: the workdir/repo a dev run uses comes
        # from the STAGE, not from `task.affected_repos`. A mission pinned to a
        # single repo therefore has its implementation lane in that repo — pin
        # it here so repo/workdir assertions keep testing what they mean.
        repos = [str(item) for item in (getattr(task, "affected_repos", None) or []) if str(item or "").strip()]
        if len(repos) == 1:
            target.repo = repos[0]
        if reopened:
            # `_mark_default_stage_out_of_scope` rewrote the lane into a
            # `proof_only` shell — it does not only change `kind`, it also
            # disables the proof gate (`proof_gate={"required": False}`,
            # `proof_recipe_id=None`, `requires_visual_proof=False`,
            # `test_plan=[]`, `affected_paths=[]`, `blocks_qa_until=False`,
            # `requires_product_edit=False`). Re-opening the lane means undoing
            # ALL of it: a live implementation stage with a disabled proof gate
            # is a shape production cannot produce, and restoring only `kind`
            # left every reopened-lane fixture running ungated.
            # RESTORE only — the values come from re-instantiating the bundled
            # blueprint (see `_bundled_stage_shape`), never invented here, so
            # `requires_product_edit` stays whatever the bundled lane declares
            # (False today; only a blueprint YAML sets it True).
            pristine = _bundled_stage_shape(plan.blueprint_id, target.id)
            if pristine is not None:
                target.kind = pristine.kind
                target.proof_gate = dict(pristine.proof_gate or {})
                target.proof_recipe_id = pristine.proof_recipe_id
                target.requires_product_edit = pristine.requires_product_edit
                target.requires_visual_proof = pristine.requires_visual_proof
                target.blocks_qa_until = pristine.blocks_qa_until
                target.test_plan = list(pristine.test_plan or [])
                target.affected_paths = list(pristine.affected_paths or [])
            else:
                # Not a bundled-blueprint plan (legacy/typed): the shell rewrite
                # cannot have run either, so only the kind needs asserting.
                target.kind = "implementation"
            target.audit_notes = [
                note for note in list(target.audit_notes or []) if "not_applicable" not in note and "out of scope" not in note
            ]
    if target is not None:
        target.status = StageStatus.IMPLEMENTING
        plan.current_stage_id = target.id
        task.current_stage_id = target.id
    return task
