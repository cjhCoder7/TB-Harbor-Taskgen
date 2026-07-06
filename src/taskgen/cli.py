#!/usr/bin/env python3
"""Top-level workflow entry point for the TB Harbor task generation MVP.

This module owns the phase registry and delegates implemented phase behavior to
phase-specific modules. Each phase module owns its own run and validation logic.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from taskgen.common import project_root, render_template, resolve_display_path, validate_path_segment
from taskgen.config import EFFORT_LEVELS


@dataclass(frozen=True)
class Artifact:
    label: str
    template: str


@dataclass(frozen=True)
class Phase:
    key: str
    aliases: tuple[str, ...]
    title: str
    description: str
    inputs: tuple[Artifact, ...]
    outputs: tuple[Artifact, ...]
    command_hint: str
    module: str | None = None

    @property
    def implemented(self) -> bool:
        return self.module is not None


@dataclass(frozen=True)
class PhaseRunResult:
    exit_code: int
    ran: bool = False
    skipped: bool = False


PHASES: tuple[Phase, ...] = (
    Phase(
        key="phase1",
        aliases=("brainstorm", "seed-brainstorm"),
        title="Seed Brainstorm",
        description="Read one seed and produce 3-5 substantially different task ideas with explicit difficulty profiles.",
        inputs=(
            Artifact("seed", "seeds/{seed_id}/"),
            Artifact("prompt", "prompts/seed-brainstorm.md"),
            Artifact("agent", "cc-definitions/agents/seed-brainstormer.md"),
            Artifact("skill", "cc-definitions/skills/tb-harbor-task-generation/SKILL.md"),
        ),
        outputs=(
            Artifact("brainstorm", "runs/brainstorm/{seed_id}/seed_brainstorm.json"),
            Artifact("manifest", "runs/task-manifest.jsonl"),
        ),
        command_hint="scripts/taskgen.sh run phase1 {seed_id}",
        module="taskgen.phases.phase1_seed_brainstorm",
    ),
    Phase(
        key="phase2",
        aliases=("skillnet", "skillnet-research"),
        title="SkillNet Research",
        description="Research SkillNet for all brainstormed ideas and curate per-idea skill packages plus difficulty-hardening guidance.",
        inputs=(
            Artifact("brainstorm", "runs/brainstorm/{seed_id}/seed_brainstorm.json"),
            Artifact("prompt", "prompts/skillnet-research.md"),
            Artifact("agent", "cc-definitions/agents/skillnet-researcher.md"),
            Artifact("skill", "cc-definitions/skills/tb-harbor-task-generation/SKILL.md"),
        ),
        outputs=(
            Artifact("skillnet index", "runs/skillnet/{seed_id}/skillnet_index.json"),
            Artifact("idea skill summary", "runs/skillnet/{seed_id}/{idea_id}/skill_summary.json"),
            Artifact("idea skill packages", "runs/skillnet/{seed_id}/{idea_id}/skills/"),
            Artifact("manifest", "runs/task-manifest.jsonl"),
        ),
        command_hint="scripts/taskgen.sh run phase2 {seed_id}",
        module="taskgen.phases.phase2_skillnet_research",
    ),
    Phase(
        key="phase3",
        aliases=("generate", "task-generation"),
        title="Task Generation",
        description="Generate a complete Harbor task directory for one idea.",
        inputs=(
            Artifact("seed", "seeds/{seed_id}/"),
            Artifact("brainstorm", "runs/brainstorm/{seed_id}/seed_brainstorm.json"),
            Artifact("skill summary", "runs/skillnet/{seed_id}/{idea_id}/skill_summary.json"),
            Artifact("generated skills", "runs/skillnet/{seed_id}/{idea_id}/skills/"),
            Artifact("prompt", "prompts/task-generation.md"),
            Artifact("agent", "cc-definitions/agents/tb-harbor-task-generator.md"),
            Artifact("skill", "cc-definitions/skills/tb-harbor-task-generation/SKILL.md"),
        ),
        outputs=(
            Artifact("working task", "generated/working/{seed_id}/{idea_id}/"),
            Artifact("manifest", "runs/task-manifest.jsonl"),
        ),
        command_hint=(
            "scripts/taskgen.sh run phase3 {seed_id} --idea-id {idea_id}"
        ),
        module="taskgen.phases.phase3_task_generation",
    ),
    Phase(
        key="phase4",
        aliases=("check", "oracle-nop"),
        title="Harbor Oracle / Nop Check",
        description="Run oracle and nop checks for one generated task.",
        inputs=(
            Artifact("working task", "generated/working/{seed_id}/{idea_id}/"),
        ),
        outputs=(
            Artifact("oracle/nop status", "runs/oracle-nop-check/{task_id}/oracle-nop-status.json"),
            Artifact("oracle log", "runs/oracle-nop-check/{task_id}/oracle.log"),
            Artifact("nop log", "runs/oracle-nop-check/{task_id}/nop.log"),
        ),
        command_hint=(
            "scripts/taskgen.sh run phase4 {seed_id} --idea-id {idea_id}"
        ),
        module="taskgen.phases.phase4_oracle_nop_check",
    ),
    Phase(
        key="phase5",
        aliases=("review", "task-review"),
        title="Task Review",
        description="Review one checked task for quality and difficulty-calibration issues.",
        inputs=(
            Artifact("working task", "generated/working/{seed_id}/{idea_id}/"),
            Artifact("oracle/nop status", "runs/oracle-nop-check/{task_id}/oracle-nop-status.json"),
        ),
        outputs=(
            Artifact("review json", "runs/reviews/{task_id}/review.json"),
            Artifact("review markdown", "runs/reviews/{task_id}/review.md"),
        ),
        command_hint=(
            "scripts/taskgen.sh run phase5 {seed_id} --idea-id {idea_id}"
        ),
        module="taskgen.phases.phase5_task_review",
    ),
    Phase(
        key="phase6",
        aliases=("repair", "task-repair"),
        title="Task Repair",
        description="Repair one task when phase5 review returns needs_modification, including bounded difficulty repairs.",
        inputs=(
            Artifact("working task", "generated/working/{seed_id}/{idea_id}/"),
            Artifact("review json", "runs/reviews/{task_id}/review.json"),
            Artifact("oracle/nop status", "runs/oracle-nop-check/{task_id}/oracle-nop-status.json"),
        ),
        outputs=(
            Artifact("repaired working task", "generated/working/{seed_id}/{idea_id}/"),
        ),
        command_hint=(
            "scripts/taskgen.sh run phase6 {seed_id} --idea-id {idea_id}"
        ),
        module="taskgen.phases.phase6_task_repair",
    ),
    Phase(
        key="phase7",
        aliases=("finalize", "archive", "organize", "task-finalize"),
        title="Finalize / Organize",
        description="Move ready or rejected tasks into final accepted/rejected directories.",
        inputs=(
            Artifact("working task", "generated/working/{seed_id}/{idea_id}/"),
            Artifact("review json", "runs/reviews/{task_id}/review.json"),
            Artifact("oracle/nop status", "runs/oracle-nop-check/{task_id}/oracle-nop-status.json"),
        ),
        outputs=(
            Artifact("accepted task", "generated/accepted/{task_id}/"),
            Artifact("rejected task", "generated/rejected/{task_id}/"),
            Artifact("manifest", "runs/task-manifest.jsonl"),
        ),
        command_hint=(
            "scripts/taskgen.sh run phase7 {seed_id} --idea-id {idea_id}"
        ),
        module="taskgen.phases.phase7_finalize",
    ),
)

PHASE_BY_NAME = {
    name: phase
    for phase in PHASES
    for name in (phase.key, *phase.aliases)
}
IDEA_SCOPED_PHASES = {"phase3", "phase4", "phase5", "phase6", "phase7"}
CLAUDE_RUN_PHASES = {"phase1", "phase2", "phase3", "phase5", "phase6"}


def get_phase(name: str) -> Phase:
    try:
        return PHASE_BY_NAME[name]
    except KeyError:
        known = ", ".join(phase.key for phase in PHASES)
        raise SystemExit(f"unknown phase: {name}; known phases: {known}") from None


def require_phase_module(phase: Phase) -> str:
    if phase.module is None:
        raise SystemExit(f"{phase.key} is registered but not implemented yet")
    return phase.module


def python_env(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    src_path = str(root / "src")
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{src_path}:{current}" if current else src_path
    return env


def build_phase_process_command(
    phase: Phase,
    action: str,
    seed_id: str,
    *,
    idea_id: str | None = None,
    dry_run: bool = False,
    as_json: bool = False,
    model: str | None = None,
    effort: str | None = None,
) -> list[str]:
    phase_module = require_phase_module(phase)
    command = [sys.executable, "-m", phase_module, action, seed_id]
    if phase.key in IDEA_SCOPED_PHASES:
        if not idea_id:
            raise SystemExit(f"{phase.key} requires --idea-id")
        command.extend(["--idea-id", idea_id])
    elif idea_id:
        raise SystemExit(f"{phase.key} does not accept --idea-id")

    if dry_run:
        if action != "run":
            raise SystemExit("--dry-run is only valid for phase run commands")
        command.append("--dry-run")
    if as_json:
        if action != "validate":
            raise SystemExit("--json is only valid for phase validate commands")
        command.append("--json")
    if model:
        if phase.key not in CLAUDE_RUN_PHASES:
            raise SystemExit(f"{phase.key} does not accept --model")
        if action != "run":
            raise SystemExit("--model is only valid for phase run commands")
        command.extend(["--model", model])
    if effort:
        if phase.key not in CLAUDE_RUN_PHASES:
            raise SystemExit(f"{phase.key} does not accept --effort")
        if action != "run":
            raise SystemExit("--effort is only valid for phase run commands")
        command.extend(["--effort", effort])
    return command


def command_display(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def validate_phase_json(
    root: Path,
    phase_name: str,
    seed_id: str,
    idea_id: str | None = None,
) -> tuple[int, dict[str, object] | None]:
    phase = get_phase(phase_name)
    command = build_phase_process_command(
        phase,
        "validate",
        seed_id,
        idea_id=idea_id,
        as_json=True,
    )
    result = subprocess.run(
        command,
        cwd=root,
        env=python_env(root),
        check=False,
        text=True,
        capture_output=True,
    )
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        if result.stdout:
            print(result.stdout, end="")
        return result.returncode or 1, None
    return result.returncode, payload


def phase_is_valid(root: Path, phase_name: str, seed_id: str, idea_id: str | None = None) -> bool:
    exit_code, payload = validate_phase_json(root, phase_name, seed_id, idea_id)
    return exit_code == 0 and payload is not None and payload.get("passed") is True


def run_or_skip_phase(
    root: Path,
    phase_name: str,
    seed_id: str,
    *,
    idea_id: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    model: str | None = None,
    effort: str | None = None,
) -> PhaseRunResult:
    phase = get_phase(phase_name)
    label = f"{phase.key} {seed_id}" + (f" --idea-id {idea_id}" if idea_id else "")
    if not force and phase_is_valid(root, phase.key, seed_id, idea_id):
        print(f"pipeline: {label} already valid; skipping")
        return PhaseRunResult(exit_code=0, skipped=True)

    command = build_phase_process_command(
        phase,
        "run",
        seed_id,
        idea_id=idea_id,
        dry_run=dry_run,
        model=model,
        effort=effort,
    )
    print(f"pipeline: running {label}")
    print(f"pipeline command: {command_display(command)}")
    if dry_run:
        return PhaseRunResult(exit_code=0, ran=True)

    exit_code = subprocess.run(command, cwd=root, env=python_env(root), check=False).returncode
    return PhaseRunResult(exit_code=exit_code, ran=exit_code == 0)


def load_pipeline_idea_ids(root: Path, seed_id: str) -> list[str]:
    errors = validate_path_segment(seed_id, "seed_id")
    if errors:
        raise SystemExit("; ".join(errors))

    brainstorm_path = root / "runs/brainstorm" / seed_id / "seed_brainstorm.json"
    try:
        payload = json.loads(brainstorm_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"cannot read phase1 brainstorm output: {brainstorm_path}: {exc}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in phase1 brainstorm output: {brainstorm_path}: {exc}") from None

    ideas = payload.get("ideas") if isinstance(payload, dict) else None
    if not isinstance(ideas, list) or not ideas:
        raise SystemExit(f"phase1 brainstorm output has no ideas: {brainstorm_path}")

    idea_ids: list[str] = []
    for index, idea in enumerate(ideas):
        if not isinstance(idea, dict):
            raise SystemExit(f"phase1 brainstorm idea at index {index} must be an object")
        idea_id = idea.get("idea_id")
        if not isinstance(idea_id, str) or not idea_id.strip():
            raise SystemExit(f"phase1 brainstorm idea at index {index} has no idea_id")
        id_errors = validate_path_segment(idea_id, f"ideas[{index}].idea_id")
        if id_errors:
            raise SystemExit("; ".join(id_errors))
        if idea_id not in idea_ids:
            idea_ids.append(idea_id)
    return idea_ids


def review_decision_for(root: Path, seed_id: str, idea_id: str) -> str:
    review_path = root / "runs/reviews" / f"{seed_id}__{idea_id}" / "review.json"
    try:
        payload = json.loads(review_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"cannot read phase5 review output: {review_path}: {exc}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in phase5 review output: {review_path}: {exc}") from None

    decision = payload.get("decision") if isinstance(payload, dict) else None
    if not isinstance(decision, str) or not decision.strip():
        raise SystemExit(f"phase5 review output has no decision: {review_path}")
    return decision


def phase4_status_passed_for(root: Path, seed_id: str, idea_id: str) -> bool | None:
    status_path = root / "runs/oracle-nop-check" / f"{seed_id}__{idea_id}" / "oracle-nop-status.json"
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    passed = payload.get("passed")
    return passed if isinstance(passed, bool) else None


def run_pipeline_idea(
    root: Path,
    args: argparse.Namespace,
    idea_id: str,
    *,
    force_generation: bool = False,
) -> int:
    seed_id = args.seed_id
    print()
    print(f"pipeline: processing {seed_id} --idea-id {idea_id}")

    if not force_generation and phase_is_valid(root, "phase7", seed_id, idea_id):
        print(f"pipeline: phase7 {seed_id} --idea-id {idea_id} already finalized; skipping")
        return 0

    phase3 = run_or_skip_phase(
        root,
        "phase3",
        seed_id,
        idea_id=idea_id,
        force=force_generation,
        dry_run=args.dry_run,
        model=args.model,
        effort=args.effort,
    )
    if phase3.exit_code != 0:
        return phase3.exit_code

    force_dynamic = args.force or phase3.ran
    repair_round = 0
    while True:
        phase4 = run_or_skip_phase(
            root,
            "phase4",
            seed_id,
            idea_id=idea_id,
            force=force_dynamic,
            dry_run=args.dry_run,
        )
        if phase4.exit_code != 0:
            print(
                "pipeline: phase4 did not produce a reviewable oracle/nop status, "
                "so this idea stops here",
                file=sys.stderr,
            )
            return phase4.exit_code
        if phase4.ran and phase4_status_passed_for(root, seed_id, idea_id) is False:
            print(
                "pipeline: phase4 oracle/nop did not pass; continuing to phase5 "
                "so review can drive phase6 repair"
            )

        phase5 = run_or_skip_phase(
            root,
            "phase5",
            seed_id,
            idea_id=idea_id,
            force=force_dynamic or phase4.ran,
            dry_run=args.dry_run,
            model=args.model,
            effort=args.effort,
        )
        if phase5.exit_code != 0:
            return phase5.exit_code

        if args.dry_run and not phase5.skipped:
            print("pipeline: dry-run stops before reading phase5 decision")
            return 0

        decision = review_decision_for(root, seed_id, idea_id)
        print(f"pipeline: phase5 decision for {seed_id} --idea-id {idea_id}: {decision}")
        if decision in {"ready", "rejected"}:
            phase7 = run_or_skip_phase(
                root,
                "phase7",
                seed_id,
                idea_id=idea_id,
                force=args.force,
                dry_run=args.dry_run,
            )
            return phase7.exit_code

        if decision != "needs_modification":
            print(f"pipeline: unsupported phase5 decision: {decision!r}", file=sys.stderr)
            return 1

        if repair_round >= args.max_repairs:
            print(
                f"pipeline: repair budget exhausted for {seed_id} --idea-id {idea_id}; "
                "latest review still needs modification",
                file=sys.stderr,
            )
            return 1

        repair_round += 1
        print(
            f"pipeline: entering phase6 repair round {repair_round}/{args.max_repairs} "
            f"for {seed_id} --idea-id {idea_id}"
        )
        phase6 = run_or_skip_phase(
            root,
            "phase6",
            seed_id,
            idea_id=idea_id,
            force=True,
            dry_run=args.dry_run,
            model=args.model,
            effort=args.effort,
        )
        if phase6.exit_code != 0:
            return phase6.exit_code

        force_dynamic = True


def command_pipeline(args: argparse.Namespace) -> int:
    root = project_root()
    if args.max_repairs < 0:
        raise SystemExit("--max-repairs must be >= 0")

    phase1 = run_or_skip_phase(
        root,
        "phase1",
        args.seed_id,
        force=args.force,
        dry_run=args.dry_run,
        model=args.model,
        effort=args.effort,
    )
    if phase1.exit_code != 0:
        return phase1.exit_code

    phase2 = run_or_skip_phase(
        root,
        "phase2",
        args.seed_id,
        force=args.force or phase1.ran,
        dry_run=args.dry_run,
        model=args.model,
        effort=args.effort,
    )
    if phase2.exit_code != 0:
        return phase2.exit_code

    if args.idea_id:
        idea_ids = [args.idea_id]
    else:
        if args.dry_run:
            try:
                idea_ids = load_pipeline_idea_ids(root, args.seed_id)
            except SystemExit:
                print(
                    "pipeline: dry-run cannot infer idea ids until phase1 output exists; "
                    "pass --idea-id or run phase1 first"
                )
                return 0
        else:
            idea_ids = load_pipeline_idea_ids(root, args.seed_id)

    failures: list[tuple[str, int]] = []
    for idea_id in idea_ids:
        exit_code = run_pipeline_idea(
            root,
            args,
            idea_id,
            force_generation=args.force or phase2.ran,
        )
        if exit_code != 0:
            failures.append((idea_id, exit_code))
            if not args.continue_on_error:
                return exit_code

    if failures:
        print()
        print("pipeline: completed with failed ideas:", file=sys.stderr)
        for idea_id, exit_code in failures:
            print(f"- {idea_id}: exit code {exit_code}", file=sys.stderr)
        return 1
    return 0


def command_phases(_: argparse.Namespace) -> int:
    for phase in PHASES:
        status = f"implemented via {phase.module}" if phase.implemented else "not implemented yet"
        print(f"{phase.key}: {phase.title}")
        print(f"  {phase.description}")
        print(f"  aliases: {', '.join(phase.aliases)}")
        print(f"  status: {status}")
    return 0


def command_paths(args: argparse.Namespace) -> int:
    root = project_root()
    idea_id = args.idea_id or "<idea_id>"
    task_id = args.task_id or "<task_id>"
    for phase in PHASES:
        print(f"{phase.key}: {phase.title}")
        print("  inputs:")
        for artifact in phase.inputs:
            rendered = render_template(artifact.template, args.seed_id, idea_id, task_id)
            print(f"  - {artifact.label}: {resolve_display_path(root, rendered)}")
        print("  outputs:")
        for artifact in phase.outputs:
            rendered = render_template(artifact.template, args.seed_id, idea_id, task_id)
            print(f"  - {artifact.label}: {resolve_display_path(root, rendered)}")
    return 0


def command_command(args: argparse.Namespace) -> int:
    phase = get_phase(args.phase)
    idea_id = args.idea_id or "<idea_id>"
    task_id = args.task_id or "<task_id>"
    print(render_template(phase.command_hint, args.seed_id, idea_id, task_id))
    return 0


def command_run(args: argparse.Namespace) -> int:
    root = project_root()
    phase = get_phase(args.phase)
    phase_module = require_phase_module(phase)

    command = [sys.executable, "-m", phase_module, "run", args.seed_id]
    if phase.key in IDEA_SCOPED_PHASES:
        if not args.idea_id:
            raise SystemExit(f"{phase.key} requires --idea-id")
        command.extend(["--idea-id", args.idea_id])
    elif args.idea_id:
        raise SystemExit(f"{phase.key} does not accept --idea-id")
    if args.dry_run:
        command.append("--dry-run")
    if args.model:
        if phase.key not in CLAUDE_RUN_PHASES:
            raise SystemExit(f"{phase.key} does not accept --model")
        command.extend(["--model", args.model])
    if args.effort:
        if phase.key not in CLAUDE_RUN_PHASES:
            raise SystemExit(f"{phase.key} does not accept --effort")
        command.extend(["--effort", args.effort])

    return subprocess.run(command, cwd=root, env=python_env(root), check=False).returncode


def command_validate(args: argparse.Namespace) -> int:
    root = project_root()
    phase = get_phase(args.phase)
    phase_module = require_phase_module(phase)

    command = [sys.executable, "-m", phase_module, "validate", args.seed_id]
    if phase.key in IDEA_SCOPED_PHASES:
        if not args.idea_id:
            raise SystemExit(f"{phase.key} requires --idea-id")
        command.extend(["--idea-id", args.idea_id])
    elif args.idea_id:
        raise SystemExit(f"{phase.key} does not accept --idea-id")
    if args.json:
        command.append("--json")

    return subprocess.run(command, cwd=root, env=python_env(root), check=False).returncode


def command_next(args: argparse.Namespace) -> int:
    root = project_root()
    phase1 = get_phase("phase1")
    phase_module = require_phase_module(phase1)

    command = [sys.executable, "-m", phase_module, "validate", args.seed_id, "--json"]
    result = subprocess.run(
        command,
        cwd=root,
        env=python_env(root),
        check=False,
        text=True,
        capture_output=True,
    )
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(result.stdout, end="")
        return result.returncode or 1

    if result.returncode == 0 and payload.get("passed") is True:
        print(f"phase1 is valid for seed {args.seed_id}; next phase: phase2 SkillNet Research")
        print(render_template(get_phase("phase2").command_hint, args.seed_id, "<idea_id>", "<task_id>"))
        return 0

    print(f"phase1 is not valid for seed {args.seed_id}; fix phase1 output before continuing")
    for error in payload.get("errors", []):
        print(f"- {error}")
    return result.returncode or 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    phases = subparsers.add_parser("phases", help="List the MVP phases and implementation coverage.")
    phases.set_defaults(func=command_phases)

    paths = subparsers.add_parser("paths", help="Print expected input/output paths for all phases.")
    paths.add_argument("seed_id")
    paths.add_argument("--idea-id")
    paths.add_argument("--task-id")
    paths.set_defaults(func=command_paths)

    command = subparsers.add_parser("command", help="Print the command hint for one phase.")
    command.add_argument("phase")
    command.add_argument("seed_id")
    command.add_argument("--idea-id")
    command.add_argument("--task-id")
    command.set_defaults(func=command_command)

    run = subparsers.add_parser("run", help="Run one implemented phase, then validate its output.")
    run.add_argument("phase")
    run.add_argument("seed_id")
    run.add_argument("--idea-id", help="Idea id for idea-scoped phases such as phase3.")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Render the phase prompt and print the command without running Claude or validation.",
    )
    run.add_argument("--model", help="Claude model to use. Defaults to model.json default_model when omitted.")
    run.add_argument(
        "--effort",
        choices=EFFORT_LEVELS,
        help="Claude Code effort level for this run. Defaults to model.json phase_efforts for the phase, then default_effort.",
    )
    run.set_defaults(func=command_run)

    pipeline = subparsers.add_parser(
        "pipeline",
        aliases=("run-all",),
        help="Run the full seed-to-final task pipeline with an automatic phase6 repair loop.",
    )
    pipeline.add_argument("seed_id")
    pipeline.add_argument("--idea-id", help="Run only one idea. Defaults to every idea from phase1 output.")
    pipeline.add_argument(
        "--max-repairs",
        type=int,
        default=2,
        help="Maximum automatic phase6 repair rounds per idea when phase5 returns needs_modification.",
    )
    pipeline.add_argument(
        "--force",
        action="store_true",
        help="Rerun phases even when their current validation already passes.",
    )
    pipeline.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue with later ideas after one idea fails.",
    )
    pipeline.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the phase commands that would run without executing them.",
    )
    pipeline.add_argument("--model", help="Claude model to use for Claude-backed phases.")
    pipeline.add_argument(
        "--effort",
        choices=EFFORT_LEVELS,
        help="Claude Code effort level for Claude-backed phases.",
    )
    pipeline.set_defaults(func=command_pipeline)

    validate = subparsers.add_parser("validate", help="Validate one implemented phase artifact.")
    validate.add_argument("phase")
    validate.add_argument("seed_id")
    validate.add_argument("--idea-id", help="Idea id for idea-scoped phases such as phase3.")
    validate.add_argument("--json", action="store_true", help="Emit machine-readable validation output.")
    validate.set_defaults(func=command_validate)

    next_phase = subparsers.add_parser("next", help="Report whether phase1 is complete and what comes next.")
    next_phase.add_argument("seed_id")
    next_phase.set_defaults(func=command_next)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
