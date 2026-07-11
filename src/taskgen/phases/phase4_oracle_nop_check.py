#!/usr/bin/env python3
"""Phase 4 runner and validator: Harbor oracle/nop checks."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

from taskgen.common import (
    ValidationReport,
    append_jsonl_object,
    directory_tree_sha256,
    load_json,
    phase_subject_lock,
    print_report,
    project_root,
    read_jsonl_objects,
    require_object,
    validate_path_segment,
)
from taskgen.harbor.oracle_nop import (
    command_run as run_oracle_nop_command,
    numeric_reward,
    reward_equals,
)
from taskgen.phases.phase3_task_generation import (
    generated_task_ref_for,
    subject_for,
    validate_idea_id,
    validate_phase3,
    validate_seed_id,
)


PHASE_KEY = "phase4"


def generated_task_path(root: Path, seed_id: str, idea_id: str) -> Path:
    return root / generated_task_ref_for(seed_id, idea_id)


def expected_task_id(root: Path, seed_id: str, idea_id: str) -> str:
    return subject_for(seed_id, idea_id)


def status_path_for(root: Path, seed_id: str, idea_id: str) -> Path:
    return root / "runs/oracle-nop-check" / expected_task_id(root, seed_id, idea_id) / "oracle-nop-status.json"


def generated_task_ref(seed_id: str, idea_id: str) -> str:
    return generated_task_ref_for(seed_id, idea_id)


def append_manifest_event(root: Path, seed_id: str, idea_id: str, payload: dict[str, Any]) -> None:
    task_id = expected_task_id(root, seed_id, idea_id)
    event = {
        "event": "checked",
        "seed_id": seed_id,
        "idea_id": idea_id,
        "task_id": task_id,
        "task_path": generated_task_ref(seed_id, idea_id),
        "oracle_nop_ref": f"runs/oracle-nop-check/{task_id}/oracle-nop-status.json",
        "run_id": payload.get("run_id"),
        "task_tree_sha256": payload.get("task_tree_sha256"),
        "passed": payload.get("passed") is True,
        "status": "checked" if payload.get("passed") is True else "failed",
        "reason": "phase 4 Harbor oracle/nop check completed",
    }
    manifest_path = root / "runs/task-manifest.jsonl"
    append_jsonl_object(manifest_path, event)


def validate_phase4(
    root: Path,
    seed_id: str,
    idea_id: str,
    *,
    require_manifest: bool = True,
    require_passed: bool = True,
) -> ValidationReport:
    report = ValidationReport(phase=PHASE_KEY, seed_id=seed_id)
    id_errors = validate_seed_id(seed_id) + validate_idea_id(idea_id)
    if id_errors:
        report.errors.extend(id_errors)
        return report

    task_path = generated_task_path(root, seed_id, idea_id)
    report.checked_paths.append(str(task_path))
    if not task_path.is_dir():
        report.errors.append(f"missing generated task directory: {task_path}")
    elif not (task_path / "task.toml").is_file():
        report.errors.append(f"missing generated task file: {task_path / 'task.toml'}")

    status_path = status_path_for(root, seed_id, idea_id)
    data = load_json(status_path, report)
    if data is None:
        return report

    payload = require_object(data, "$", report)
    if payload is None:
        return report

    validate_status_payload(
        root,
        task_path,
        expected_task_id(root, seed_id, idea_id),
        payload,
        report,
        require_passed=require_passed,
    )
    if require_manifest:
        validate_manifest_event(root, seed_id, idea_id, payload, report)
    return report


def validate_status_payload(
    root: Path,
    task_path: Path,
    task_id: str,
    payload: dict[str, Any],
    report: ValidationReport,
    *,
    require_passed: bool = True,
    expected_status_task_path: Path | None = None,
) -> None:
    expected_jobs_path: Path | None = None
    if payload.get("task_id") != task_id:
        report.errors.append(f"oracle/nop status task_id must be {task_id!r}")

    status_task_path = payload.get("task_path")
    if not isinstance(status_task_path, str) or not status_task_path.strip():
        report.errors.append("$.task_path must be a non-empty string")
    else:
        candidate = Path(status_task_path)
        recorded_task_path = expected_status_task_path or task_path
        report.checked_paths.append(str(candidate))
        try:
            if candidate.resolve() != recorded_task_path.resolve():
                report.errors.append(
                    f"oracle/nop status task_path does not match generated task: {status_task_path}"
                )
        except (OSError, RuntimeError):
            report.errors.append(f"oracle/nop status task_path cannot be resolved: {status_task_path}")

    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        report.errors.append("$.run_id must be a non-empty string")
        run_id = None
    elif validate_path_segment(run_id, "$.run_id"):
        report.errors.extend(validate_path_segment(run_id, "$.run_id"))

    expected_hash = payload.get("task_tree_sha256")
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        report.errors.append("$.task_tree_sha256 must be a lowercase SHA-256 hex digest")
    elif task_path.is_dir():
        try:
            actual_hash = directory_tree_sha256(task_path)
        except (OSError, RuntimeError) as exc:
            report.errors.append(f"cannot hash generated task directory: {exc}")
        else:
            if actual_hash != expected_hash:
                report.errors.append(
                    "oracle/nop status is stale: task tree changed after the recorded Harbor run"
                )

    jobs_dir = payload.get("jobs_dir")
    if not isinstance(jobs_dir, str) or not jobs_dir.strip():
        report.errors.append("$.jobs_dir must be a non-empty string")
    else:
        jobs_path = Path(jobs_dir)
        validate_existing_dir(jobs_path, "$.jobs_dir", report)
        if run_id is not None:
            try:
                expected_jobs_path = (
                    root / "runs/oracle-nop-check" / task_id / "harbor-jobs" / run_id
                ).resolve()
                if jobs_path.resolve() != expected_jobs_path:
                    report.errors.append(
                        f"$.jobs_dir must match task_id/run_id: {expected_jobs_path}"
                    )
            except (OSError, RuntimeError) as exc:
                report.errors.append(f"$.jobs_dir cannot be resolved: {exc}")

    passed = payload.get("passed")
    if not isinstance(passed, bool):
        report.errors.append("$.passed must be a boolean")
    elif require_passed and passed is not True:
        report.errors.append("$.passed must be true for phase4")
    elif not require_passed and passed is not True:
        report.warnings.append("phase4 oracle/nop did not pass; status is available for phase5 review")

    require_success = require_passed or passed is True
    out_dir = root / "runs/oracle-nop-check" / task_id
    validate_harbor_check(
        payload.get("oracle"),
        "oracle",
        1.0,
        report,
        require_success=require_success,
        expected_log=out_dir / "oracle.log",
        expected_job_dir=expected_jobs_path / "oracle" if expected_jobs_path is not None else None,
    )
    validate_harbor_check(
        payload.get("nop"),
        "nop",
        0.0,
        report,
        require_success=require_success,
        expected_log=out_dir / "nop.log",
        expected_job_dir=expected_jobs_path / "nop" if expected_jobs_path is not None else None,
    )

    oracle = payload.get("oracle")
    nop = payload.get("nop")
    if isinstance(oracle, dict) and isinstance(nop, dict) and isinstance(passed, bool):
        computed_passed = (
            oracle.get("exit_code") == 0
            and nop.get("exit_code") == 0
            and reward_equals(numeric_reward(oracle.get("reward")), 1.0)
            and reward_equals(numeric_reward(nop.get("reward")), 0.0)
        )
        if passed != computed_passed:
            report.errors.append("$.passed does not match the recorded Harbor check results")


def validate_harbor_check(
    value: Any,
    agent: str,
    expected_reward: float,
    report: ValidationReport,
    *,
    require_success: bool = True,
    expected_log: Path | None = None,
    expected_job_dir: Path | None = None,
) -> None:
    path = f"$.{agent}"
    if not isinstance(value, dict):
        report.errors.append(f"{path} must be an object")
        return

    exit_code = value.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        report.errors.append(f"{path}.exit_code must be an integer")
    elif require_success and exit_code != 0:
        report.errors.append(f"{path}.exit_code must be 0")

    timed_out = value.get("timed_out")
    if not isinstance(timed_out, bool):
        report.errors.append(f"{path}.timed_out must be a boolean")
    elif timed_out and exit_code != 124:
        report.errors.append(f"{path}.exit_code must be 124 when timed_out is true")
    elif not timed_out and exit_code == 124:
        report.errors.append(f"{path}.timed_out must be true when exit_code is 124")

    timeout_sec = value.get("timeout_sec")
    if (
        not isinstance(timeout_sec, (int, float))
        or isinstance(timeout_sec, bool)
        or not math.isfinite(float(timeout_sec))
        or float(timeout_sec) <= 0
    ):
        report.errors.append(f"{path}.timeout_sec must be a positive finite number")

    raw_reward = value.get("reward")
    reward = numeric_reward(raw_reward)
    if require_success and not reward_equals(reward, expected_reward):
        report.errors.append(f"{path}.reward must be {expected_reward}")
    elif not require_success and raw_reward is not None and reward is None:
        report.errors.append(f"{path}.reward must be numeric or null")

    log = value.get("log")
    if not isinstance(log, str) or not log.strip():
        report.errors.append(f"{path}.log must be a non-empty string")
    else:
        log_path = Path(log)
        validate_existing_file(log_path, f"{path}.log", report)
        if expected_log is not None:
            try:
                if log_path.resolve() != expected_log.resolve():
                    report.errors.append(f"{path}.log must be {expected_log}")
            except (OSError, RuntimeError) as exc:
                report.errors.append(f"{path}.log cannot be resolved safely: {exc}")

    job_dir = value.get("job_dir")
    if not isinstance(job_dir, str) or not job_dir.strip():
        report.errors.append(f"{path}.job_dir must be a non-empty string")
    else:
        job_path = Path(job_dir)
        if expected_job_dir is not None:
            try:
                if job_path.resolve() != expected_job_dir.resolve():
                    report.errors.append(f"{path}.job_dir must be {expected_job_dir}")
            except (OSError, RuntimeError) as exc:
                report.errors.append(f"{path}.job_dir cannot be resolved safely: {exc}")
        if job_path.is_dir():
            validate_existing_dir(job_path, f"{path}.job_dir", report)
        elif require_success or exit_code == 0:
            validate_existing_dir(job_path, f"{path}.job_dir", report)
        else:
            report.checked_paths.append(str(job_path))
            report.warnings.append(
                f"{path}.job_dir was not created by the failed Harbor check: {job_path}"
            )


def validate_existing_file(path: Path, label: str, report: ValidationReport) -> None:
    report.checked_paths.append(str(path))
    if not path.is_file():
        report.errors.append(f"{label} does not point to an existing file: {path}")


def validate_existing_dir(path: Path, label: str, report: ValidationReport) -> None:
    report.checked_paths.append(str(path))
    if not path.is_dir():
        report.errors.append(f"{label} does not point to an existing directory: {path}")


def validate_manifest_event(
    root: Path,
    seed_id: str,
    idea_id: str,
    payload: dict[str, Any],
    report: ValidationReport,
) -> None:
    manifest_path = root / "runs/task-manifest.jsonl"
    report.checked_paths.append(str(manifest_path))
    if not manifest_path.is_file():
        report.errors.append(f"missing manifest: {manifest_path}")
        return

    task_id = expected_task_id(root, seed_id, idea_id)
    expected_ref = f"runs/oracle-nop-check/{task_id}/oracle-nop-status.json"
    found = False
    for _line_number, event in read_jsonl_objects(manifest_path, report):
        if (
            event.get("event") == "checked"
            and event.get("seed_id") == seed_id
            and event.get("idea_id") == idea_id
            and event.get("task_id") == task_id
            and event.get("task_path") == generated_task_ref(seed_id, idea_id)
            and event.get("oracle_nop_ref") == expected_ref
            and event.get("passed") == (payload.get("passed") is True)
            and event.get("run_id") == payload.get("run_id")
            and event.get("task_tree_sha256") == payload.get("task_tree_sha256")
            and event.get("status") == ("checked" if payload.get("passed") is True else "failed")
        ):
            found = True
            break

    if not found:
        report.errors.append(f"manifest has no matching checked event for task_id={task_id!r}")


def command_run(args: argparse.Namespace) -> int:
    root = project_root()
    task_ref = generated_task_ref_for(args.seed_id, args.idea_id)
    task_id = expected_task_id(root, args.seed_id, args.idea_id)
    if args.dry_run:
        print(f"scripts/run-harbor-oracle-nop.sh {task_ref} --task-id {task_id}")
        return 0

    with phase_subject_lock(root, PHASE_KEY, task_id):
        return run_phase4_locked(root, args, task_ref, task_id)


def run_phase4_locked(
    root: Path,
    args: argparse.Namespace,
    task_ref: str,
    task_id: str,
) -> int:

    phase3_report = validate_phase3(root, args.seed_id, args.idea_id)
    if not phase3_report.passed:
        print(
            f"cannot run phase4 for seed {args.seed_id} idea {args.idea_id}; "
            "phase3 validation failed",
            file=sys.stderr,
        )
        for error in phase3_report.errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    run_oracle_nop_command(argparse.Namespace(task_path=task_ref, task_id=task_id))
    pre_manifest_report = validate_phase4(
        root,
        args.seed_id,
        args.idea_id,
        require_manifest=False,
        require_passed=False,
    )
    if not pre_manifest_report.passed:
        return print_report(pre_manifest_report, as_json=False)

    status_path = status_path_for(root, args.seed_id, args.idea_id)
    try:
        status_payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        print(f"cannot append phase4 manifest event; cannot read status: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"cannot append phase4 manifest event; invalid status JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(status_payload, dict):
        print("cannot append phase4 manifest event; status must be a JSON object", file=sys.stderr)
        return 1
    append_manifest_event(root, args.seed_id, args.idea_id, status_payload)

    validate_code = print_report(
        validate_phase4(root, args.seed_id, args.idea_id, require_passed=False),
        as_json=False,
    )
    return validate_code


def command_validate(args: argparse.Namespace) -> int:
    return print_report(validate_phase4(project_root(), args.seed_id, args.idea_id), args.json)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="phase4_oracle_nop_check.py", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run Harbor oracle/nop checks for one generated task.")
    run.add_argument("seed_id")
    run.add_argument("--idea-id", required=True)
    run.add_argument("--dry-run", action="store_true", help="Print the Harbor check command without running it.")
    run.set_defaults(func=command_run)

    validate = subparsers.add_parser("validate", help="Validate Harbor oracle/nop check output.")
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
