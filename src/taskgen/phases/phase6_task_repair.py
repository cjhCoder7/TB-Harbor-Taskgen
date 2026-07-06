#!/usr/bin/env python3
"""Phase 6 runner and validator: task repair."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from taskgen.common import (
    ValidationReport,
    load_json,
    print_report,
    project_root,
    require_object,
)
from taskgen.config import EFFORT_LEVELS, resolve_effort_level, resolve_model_name
from taskgen.phases.phase3_task_generation import (
    generated_task_ref_for,
    validate_idea_id,
    validate_phase3,
    validate_seed_id,
)
from taskgen.phases.phase5_task_review import (
    review_json_path_for,
    subject_for,
    validate_phase5,
)


PHASE_KEY = "phase6"
PHASE_NAME = "task-repair"
TASK_REPAIR_PROMPT = "task-repair.md"


def session_root_for(root: Path, seed_id: str, idea_id: str) -> Path:
    return root / "runs/claude-sessions" / PHASE_NAME / subject_for(seed_id, idea_id)


def generated_prompt_path_for(root: Path, seed_id: str, idea_id: str) -> Path:
    return root / "runs/prompts" / seed_id / idea_id / TASK_REPAIR_PROMPT


def session_ref_for(root: Path, session_dir: Path) -> str:
    return session_dir.relative_to(root).as_posix()


def list_session_dirs(root: Path, seed_id: str, idea_id: str) -> set[Path]:
    session_root = session_root_for(root, seed_id, idea_id)
    if not session_root.is_dir():
        return set()
    return {path for path in session_root.iterdir() if path.is_dir()}


def find_new_session_dir(root: Path, seed_id: str, idea_id: str, before: set[Path]) -> Path | None:
    created = list(list_session_dirs(root, seed_id, idea_id) - before)
    if not created:
        return None
    created.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
    return created[0]


def review_ref(seed_id: str, idea_id: str) -> str:
    return f"runs/reviews/{subject_for(seed_id, idea_id)}/review.json"


def oracle_status_ref(seed_id: str, idea_id: str) -> str:
    return f"runs/oracle-nop-check/{subject_for(seed_id, idea_id)}/oracle-nop-status.json"


def append_manifest_event(root: Path, seed_id: str, idea_id: str, claude_session_ref: str) -> None:
    task_id = subject_for(seed_id, idea_id)
    payload: dict[str, Any] = {
        "event": "repaired",
        "seed_id": seed_id,
        "idea_id": idea_id,
        "task_id": task_id,
        "task_path": generated_task_ref_for(seed_id, idea_id),
        "review_ref": review_ref(seed_id, idea_id),
        "oracle_nop_ref": oracle_status_ref(seed_id, idea_id),
        "claude_session_ref": claude_session_ref,
        "status": "working",
        "reason": "phase 6 task repair completed",
    }
    manifest_path = root / "runs/task-manifest.jsonl"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def ensure_phase6_inputs(root: Path, seed_id: str, idea_id: str) -> list[str]:
    errors = validate_seed_id(seed_id) + validate_idea_id(idea_id)
    if errors:
        return errors

    for required in (
        generated_task_ref_for(seed_id, idea_id),
        f"runs/reviews/{subject_for(seed_id, idea_id)}",
        f"prompts/{TASK_REPAIR_PROMPT}",
        "scripts/run-claude-logged.sh",
    ):
        candidate = root / required
        if not candidate.exists():
            errors.append(f"missing phase6 project file: {candidate}")

    phase5_report = validate_phase5(root, seed_id, idea_id)
    if not phase5_report.passed:
        errors.append("phase5 validation must pass before phase6")
        errors.extend(phase5_report.errors)
        return errors

    decision = load_review_decision(root, seed_id, idea_id, errors)
    if decision is not None and decision != "needs_modification":
        errors.append(
            "phase6 can only run when review decision is "
            f"'needs_modification', got {decision!r}"
        )
    return errors


def load_review_decision(root: Path, seed_id: str, idea_id: str, errors: list[str]) -> str | None:
    review_path = review_json_path_for(root, seed_id, idea_id)
    try:
        payload = json.loads(review_path.read_text(encoding="utf-8"))
    except OSError as exc:
        errors.append(f"cannot read review json: {review_path}: {exc}")
        return None
    except json.JSONDecodeError as exc:
        errors.append(f"invalid JSON in review json: {review_path}: {exc}")
        return None

    if not isinstance(payload, dict):
        errors.append(f"review json must contain an object: {review_path}")
        return None
    decision = payload.get("decision")
    if not isinstance(decision, str) or not decision.strip():
        errors.append("$.decision must be a non-empty string")
        return None
    return decision


def render_phase6_prompt(root: Path, seed_id: str, idea_id: str) -> Path:
    template_path = root / "prompts" / TASK_REPAIR_PROMPT
    output_path = generated_prompt_path_for(root, seed_id, idea_id)
    task_id = subject_for(seed_id, idea_id)
    prompt = template_path.read_text(encoding="utf-8")
    replacements = {
        "<seed_id>": seed_id,
        "<idea_id>": idea_id,
        "<task_id>": task_id,
    }
    for marker, value in replacements.items():
        prompt = prompt.replace(marker, value)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(prompt, encoding="utf-8")
    return output_path


def build_claude_command(
    root: Path,
    seed_id: str,
    idea_id: str,
    prompt_path: Path,
    model: str | None,
    effort: str | None,
) -> list[str]:
    command = [
        str(root / "scripts/run-claude-logged.sh"),
        PHASE_NAME,
        subject_for(seed_id, idea_id),
        str(prompt_path.relative_to(root)),
    ]
    if model:
        command.extend(["--model", model])
    if effort:
        command.extend(["--effort", effort])
    return command


def validate_phase6(root: Path, seed_id: str, idea_id: str, *, require_manifest: bool = True) -> ValidationReport:
    report = ValidationReport(phase=PHASE_KEY, seed_id=seed_id)
    id_errors = validate_seed_id(seed_id) + validate_idea_id(idea_id)
    if id_errors:
        report.errors.extend(id_errors)
        return report

    task_report = validate_phase3(root, seed_id, idea_id, require_manifest=False)
    report.checked_paths.extend(task_report.checked_paths)
    report.errors.extend(task_report.errors)
    report.warnings.extend(task_report.warnings)

    review_data = load_json(review_json_path_for(root, seed_id, idea_id), report)
    if review_data is not None:
        review = require_object(review_data, "$.review", report)
        if review is not None and review.get("decision") != "needs_modification":
            report.warnings.append(
                "latest review decision is not 'needs_modification'; "
                "phase6 validation only checks the repaired working task"
            )
    if require_manifest:
        validate_manifest_event(root, seed_id, idea_id, report)
    return report


def validate_manifest_event(root: Path, seed_id: str, idea_id: str, report: ValidationReport) -> None:
    manifest_path = root / "runs/task-manifest.jsonl"
    report.checked_paths.append(str(manifest_path))
    if not manifest_path.is_file():
        report.errors.append(f"missing manifest: {manifest_path}")
        return

    task_id = subject_for(seed_id, idea_id)
    found = False
    candidate_errors: list[str] = []
    for line_number, line in enumerate(manifest_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            report.errors.append(f"invalid JSONL at {manifest_path}:{line_number}: {exc}")
            continue
        if not isinstance(event, dict):
            continue
        if not (
            event.get("event") == "repaired"
            and event.get("seed_id") == seed_id
            and event.get("idea_id") == idea_id
            and event.get("task_id") == task_id
            and event.get("task_path") == generated_task_ref_for(seed_id, idea_id)
            and event.get("review_ref") == review_ref(seed_id, idea_id)
            and event.get("oracle_nop_ref") == oracle_status_ref(seed_id, idea_id)
        ):
            continue

        line_errors = validate_manifest_candidate(root, event, report)
        if line_errors:
            candidate_errors.append(f"{manifest_path}:{line_number}: " + "; ".join(line_errors))
        else:
            found = True
            break

    if not found:
        report.errors.append(f"manifest has no matching repaired event for task_id={task_id!r}")
        report.errors.extend(candidate_errors)


def validate_manifest_candidate(root: Path, event: dict[str, Any], report: ValidationReport) -> list[str]:
    errors: list[str] = []
    if event.get("status") != "working":
        errors.append("status must be 'working'")
    if not isinstance(event.get("reason"), str) or not event["reason"].strip():
        errors.append("reason must be a non-empty string")

    claude_session_ref = event.get("claude_session_ref")
    if not isinstance(claude_session_ref, str) or not claude_session_ref.strip():
        errors.append("claude_session_ref must be a non-empty string")
        return errors

    session_path = root / claude_session_ref
    report.checked_paths.append(str(session_path))
    if not session_path.is_dir():
        errors.append(f"claude_session_ref does not point to a directory: {claude_session_ref}")
    elif not (session_path / "status.json").is_file():
        errors.append(f"claude_session_ref is missing status.json: {claude_session_ref}")
    return errors


def validate_new_session_synced_task(session_dir: Path, expected_output: str) -> list[str]:
    status_path = session_dir / "status.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except OSError as exc:
        return [f"cannot read Claude session status: {status_path}: {exc}"]
    except json.JSONDecodeError as exc:
        return [f"invalid JSON in Claude session status: {status_path}: {exc}"]

    synced = status.get("synced_outputs")
    if not isinstance(synced, list) or expected_output not in synced:
        return [
            "Claude repair run did not sync repaired task output; "
            f"expected {expected_output!r} in status.synced_outputs"
        ]
    return []


def command_run(args: argparse.Namespace) -> int:
    root = project_root()
    errors = ensure_phase6_inputs(root, args.seed_id, args.idea_id)
    if errors:
        print(
            f"cannot run phase6 for seed {args.seed_id} idea {args.idea_id}; prerequisites failed",
            file=sys.stderr,
        )
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    prompt_path = render_phase6_prompt(root, args.seed_id, args.idea_id)
    model = resolve_model_name(root, args.model)
    effort = resolve_effort_level(root, args.effort, PHASE_KEY)
    command = build_claude_command(root, args.seed_id, args.idea_id, prompt_path, model, effort)

    print("phase6 prompt:", prompt_path)
    print("phase6 command:", " ".join(command))
    if args.dry_run:
        return 0

    before_sessions = list_session_dirs(root, args.seed_id, args.idea_id)
    exit_code = subprocess.run(command, cwd=root, check=False).returncode
    if exit_code != 0:
        return exit_code

    session_dir = find_new_session_dir(root, args.seed_id, args.idea_id, before_sessions)
    if session_dir is None:
        print("cannot validate repair run: no new Claude phase6 session directory was found", file=sys.stderr)
        return 1

    expected_output = generated_task_ref_for(args.seed_id, args.idea_id)
    sync_errors = validate_new_session_synced_task(session_dir, expected_output)
    if sync_errors:
        for error in sync_errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print()
    print("validating phase6 repaired task...")
    repair_report = validate_phase6(root, args.seed_id, args.idea_id, require_manifest=False)
    repair_exit_code = print_report(repair_report, as_json=False)
    if repair_exit_code != 0:
        return repair_exit_code

    append_manifest_event(root, args.seed_id, args.idea_id, session_ref_for(root, session_dir))

    print()
    print("validating phase6 manifest...")
    return print_report(validate_phase6(root, args.seed_id, args.idea_id), as_json=False)


def command_validate(args: argparse.Namespace) -> int:
    return print_report(validate_phase6(project_root(), args.seed_id, args.idea_id), args.json)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run phase6 repair, then validate the repaired task.")
    run.add_argument("seed_id")
    run.add_argument("--idea-id", required=True)
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the Claude repair command without running Claude or validation.",
    )
    run.add_argument("--model", help="Claude model to use. Defaults to model.json default_model when omitted.")
    run.add_argument(
        "--effort",
        choices=EFFORT_LEVELS,
        help="Claude Code effort level for this run. Defaults to model.json phase_efforts.phase6, then default_effort.",
    )
    run.set_defaults(func=command_run)

    validate = subparsers.add_parser("validate", help="Validate phase6 repaired task output.")
    validate.add_argument("seed_id")
    validate.add_argument("--idea-id", required=True)
    validate.add_argument("--json", action="store_true", help="Emit machine-readable validation output.")
    validate.set_defaults(func=command_validate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
