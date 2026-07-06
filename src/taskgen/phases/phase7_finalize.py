#!/usr/bin/env python3
"""Phase 7 runner and validator: finalize ready or rejected tasks."""

from __future__ import annotations

import argparse
import json
import shutil
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
from taskgen.phases.phase3_task_generation import (
    generated_task_ref_for,
    validate_idea_id,
    validate_seed_id,
)
from taskgen.phases.phase4_oracle_nop_check import validate_harbor_check, validate_phase4
from taskgen.phases.phase5_task_review import (
    review_json_path_for,
    review_markdown_path_for,
    subject_for,
    validate_review_markdown,
    validate_review_payload,
    validate_phase5,
)


PHASE_KEY = "phase7"
FINAL_DECISIONS = {"ready", "rejected"}
TASK_REQUIRED_FILES = (
    "instruction.md",
    "task.toml",
    "environment/Dockerfile",
    "solution/solve.sh",
    "tests/Dockerfile",
    "tests/test.sh",
)


def working_task_path(root: Path, seed_id: str, idea_id: str) -> Path:
    return root / generated_task_ref_for(seed_id, idea_id)


def accepted_task_path(root: Path, seed_id: str, idea_id: str) -> Path:
    return root / "generated/accepted" / subject_for(seed_id, idea_id)


def rejected_task_path(root: Path, seed_id: str, idea_id: str) -> Path:
    return root / "generated/rejected" / subject_for(seed_id, idea_id)


def oracle_status_ref(seed_id: str, idea_id: str) -> str:
    return f"runs/oracle-nop-check/{subject_for(seed_id, idea_id)}/oracle-nop-status.json"


def review_ref(seed_id: str, idea_id: str) -> str:
    return f"runs/reviews/{subject_for(seed_id, idea_id)}/review.json"


def oracle_status_path_for(root: Path, seed_id: str, idea_id: str) -> Path:
    return root / oracle_status_ref(seed_id, idea_id)


def load_review_decision(root: Path, seed_id: str, idea_id: str, report: ValidationReport) -> str | None:
    data = load_json(review_json_path_for(root, seed_id, idea_id), report)
    review = require_object(data, "$", report) if data is not None else None
    if review is None:
        return None

    decision = review.get("decision")
    if not isinstance(decision, str) or not decision.strip():
        report.errors.append("$.decision must be a non-empty string")
        return None
    return decision


def validate_final_review(root: Path, seed_id: str, idea_id: str, report: ValidationReport) -> str | None:
    task_id = subject_for(seed_id, idea_id)
    review_json_path = review_json_path_for(root, seed_id, idea_id)
    review_md_path = review_markdown_path_for(root, seed_id, idea_id)

    payload = load_json(review_json_path, report)
    review = require_object(payload, "$", report) if payload is not None else None
    decision: str | None = None
    if review is not None:
        validate_review_payload(review, task_id, report)
        raw_decision = review.get("decision")
        if isinstance(raw_decision, str):
            decision = raw_decision

    report.checked_paths.append(str(review_md_path))
    if not review_md_path.is_file():
        report.errors.append(f"missing review markdown: {review_md_path}")
    else:
        validate_review_markdown(review_md_path, report)
    return decision


def validate_final_oracle_status(root: Path, seed_id: str, idea_id: str, report: ValidationReport) -> None:
    status_path = oracle_status_path_for(root, seed_id, idea_id)
    data = load_json(status_path, report)
    payload = require_object(data, "$.oracle_nop", report) if data is not None else None
    if payload is None:
        return

    task_id = subject_for(seed_id, idea_id)
    if payload.get("task_id") != task_id:
        report.errors.append(f"oracle/nop status task_id must be {task_id!r}")

    status_task_path = payload.get("task_path")
    if not isinstance(status_task_path, str) or not status_task_path.strip():
        report.errors.append("$.task_path must be a non-empty string")
    else:
        candidate = Path(status_task_path)
        expected = working_task_path(root, seed_id, idea_id)
        report.checked_paths.append(str(candidate))
        try:
            if candidate.resolve() != expected.resolve():
                report.errors.append(
                    f"oracle/nop status task_path does not match finalized source task: {status_task_path}"
                )
        except OSError:
            report.errors.append(f"oracle/nop status task_path cannot be resolved: {status_task_path}")

    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        report.errors.append("$.run_id must be a non-empty string")

    jobs_dir = payload.get("jobs_dir")
    if not isinstance(jobs_dir, str) or not jobs_dir.strip():
        report.errors.append("$.jobs_dir must be a non-empty string")
    else:
        jobs_path = Path(jobs_dir)
        report.checked_paths.append(str(jobs_path))
        if not jobs_path.is_dir():
            report.errors.append(f"$.jobs_dir does not point to an existing directory: {jobs_path}")

    if payload.get("passed") is not True:
        report.errors.append("$.passed must be true for phase7")

    validate_harbor_check(payload.get("oracle"), "oracle", 1.0, report)
    validate_harbor_check(payload.get("nop"), "nop", 0.0, report)


def validate_phase7(root: Path, seed_id: str, idea_id: str) -> ValidationReport:
    report = ValidationReport(phase=PHASE_KEY, seed_id=seed_id)
    id_errors = validate_seed_id(seed_id) + validate_idea_id(idea_id)
    if id_errors:
        report.errors.extend(id_errors)
        return report

    decision = validate_final_review(root, seed_id, idea_id, report)
    validate_final_oracle_status(root, seed_id, idea_id, report)
    if decision == "needs_modification":
        report.errors.append("review decision is 'needs_modification'; run phase6 before phase7")
        return report
    if decision not in FINAL_DECISIONS:
        report.errors.append("review decision must be 'ready' or 'rejected' for phase7")
        return report

    accepted = accepted_task_path(root, seed_id, idea_id)
    rejected = rejected_task_path(root, seed_id, idea_id)
    if decision == "ready":
        validate_final_task_dir(accepted, "accepted task", report)
        if rejected.exists():
            report.errors.append(f"rejected task directory must not exist for ready decision: {rejected}")
    else:
        validate_final_task_dir(rejected, "rejected task", report)
        if accepted.exists():
            report.errors.append(f"accepted task directory must not exist for rejected decision: {accepted}")

    source = working_task_path(root, seed_id, idea_id)
    report.checked_paths.append(str(source))
    if source.exists():
        report.errors.append(f"working task directory must be removed after phase7 finalization: {source}")

    validate_manifest_event(root, seed_id, idea_id, decision, report)
    return report


def validate_final_task_dir(path: Path, label: str, report: ValidationReport) -> None:
    report.checked_paths.append(str(path))
    if not path.is_dir():
        report.errors.append(f"missing {label} directory: {path}")
        return
    for rel_path in TASK_REQUIRED_FILES:
        candidate = path / rel_path
        report.checked_paths.append(str(candidate))
        if not candidate.is_file():
            report.errors.append(f"missing {label} file: {candidate}")


def validate_manifest_event(
    root: Path,
    seed_id: str,
    idea_id: str,
    decision: str,
    report: ValidationReport,
) -> None:
    manifest_path = root / "runs/task-manifest.jsonl"
    report.checked_paths.append(str(manifest_path))
    if not manifest_path.is_file():
        report.errors.append(f"missing manifest: {manifest_path}")
        return

    expected_event = "accepted" if decision == "ready" else "rejected"
    expected_task_id = subject_for(seed_id, idea_id)
    expected_task_path = (
        f"generated/accepted/{expected_task_id}"
        if decision == "ready"
        else f"generated/rejected/{expected_task_id}"
    )
    found = False
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
        if (
            event.get("event") == expected_event
            and event.get("seed_id") == seed_id
            and event.get("idea_id") == idea_id
            and event.get("task_id") == expected_task_id
            and event.get("task_path") == expected_task_path
            and event.get("review_ref") == review_ref(seed_id, idea_id)
            and event.get("oracle_nop_ref") == oracle_status_ref(seed_id, idea_id)
        ):
            found = True
            break

    if not found:
        report.errors.append(
            f"manifest has no matching {expected_event} event for task_id={expected_task_id!r}"
        )


def ensure_phase7_inputs(root: Path, seed_id: str, idea_id: str) -> list[str]:
    report = validate_phase5(root, seed_id, idea_id)
    errors = validate_seed_id(seed_id) + validate_idea_id(idea_id)
    if not report.passed:
        errors.append("phase5 validation must pass before phase7")
        errors.extend(report.errors)
        return errors

    phase4_report = validate_phase4(root, seed_id, idea_id)
    if not phase4_report.passed:
        errors.append("phase7 requires phase4 oracle/nop to pass; run phase6 repair first")
        errors.extend(phase4_report.errors)
        return errors

    decision_report = ValidationReport(phase=PHASE_KEY, seed_id=seed_id)
    decision = load_review_decision(root, seed_id, idea_id, decision_report)
    errors.extend(decision_report.errors)
    if decision == "needs_modification":
        errors.append("phase7 cannot run while review decision is 'needs_modification'; run phase6 first")
    elif decision not in FINAL_DECISIONS:
        errors.append(f"phase7 requires review decision 'ready' or 'rejected', got {decision!r}")
    return errors


def move_final_task(source: Path, destination: Path, counterpart: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.rmtree(destination) if destination.is_dir() else destination.unlink()
    if counterpart.exists():
        shutil.rmtree(counterpart) if counterpart.is_dir() else counterpart.unlink()
    shutil.copytree(source, destination, symlinks=True)
    shutil.rmtree(source) if source.is_dir() else source.unlink()


def append_manifest_event(root: Path, seed_id: str, idea_id: str, decision: str) -> None:
    event_name = "accepted" if decision == "ready" else "rejected"
    task_id = subject_for(seed_id, idea_id)
    task_path = (
        f"generated/accepted/{task_id}"
        if decision == "ready"
        else f"generated/rejected/{task_id}"
    )
    payload: dict[str, Any] = {
        "event": event_name,
        "seed_id": seed_id,
        "idea_id": idea_id,
        "task_id": task_id,
        "task_path": task_path,
        "source_task_ref": generated_task_ref_for(seed_id, idea_id),
        "review_ref": review_ref(seed_id, idea_id),
        "oracle_nop_ref": oracle_status_ref(seed_id, idea_id),
        "status": event_name,
        "reason": f"phase 7 finalized review decision {decision!r}",
    }
    manifest_path = root / "runs/task-manifest.jsonl"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def command_run(args: argparse.Namespace) -> int:
    root = project_root()
    errors = ensure_phase7_inputs(root, args.seed_id, args.idea_id)
    if errors:
        print(
            f"cannot run phase7 for seed {args.seed_id} idea {args.idea_id}; prerequisites failed",
            file=sys.stderr,
        )
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    report = ValidationReport(phase=PHASE_KEY, seed_id=args.seed_id)
    decision = load_review_decision(root, args.seed_id, args.idea_id, report)
    if report.errors or decision not in FINAL_DECISIONS:
        for error in report.errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    source = working_task_path(root, args.seed_id, args.idea_id)
    if decision == "ready":
        destination = accepted_task_path(root, args.seed_id, args.idea_id)
        counterpart = rejected_task_path(root, args.seed_id, args.idea_id)
    else:
        destination = rejected_task_path(root, args.seed_id, args.idea_id)
        counterpart = accepted_task_path(root, args.seed_id, args.idea_id)

    print(f"phase7 decision: {decision}")
    print(f"phase7 source: {source}")
    print(f"phase7 destination: {destination}")
    if counterpart.exists():
        print(f"phase7 removes counterpart: {counterpart}")
    if args.dry_run:
        return 0

    move_final_task(source, destination, counterpart)
    append_manifest_event(root, args.seed_id, args.idea_id, decision)
    return print_report(validate_phase7(root, args.seed_id, args.idea_id), as_json=False)


def command_validate(args: argparse.Namespace) -> int:
    return print_report(validate_phase7(project_root(), args.seed_id, args.idea_id), args.json)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Finalize one ready or rejected task.")
    run.add_argument("seed_id")
    run.add_argument("--idea-id", required=True)
    run.add_argument("--dry-run", action="store_true", help="Print finalization action without copying files.")
    run.set_defaults(func=command_run)

    validate = subparsers.add_parser("validate", help="Validate phase7 finalized output.")
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
