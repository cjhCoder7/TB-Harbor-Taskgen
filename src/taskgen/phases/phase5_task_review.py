#!/usr/bin/env python3
"""Phase 5 runner and validator: task review."""

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
    require_string,
)
from taskgen.config import EFFORT_LEVELS, resolve_effort_level, resolve_model_name
from taskgen.phases.phase3_task_generation import (
    generated_task_ref_for,
    validate_idea_id,
    validate_seed_id,
)
from taskgen.phases.phase4_oracle_nop_check import (
    validate_phase4,
)


PHASE_KEY = "phase5"
PHASE_NAME = "task-review"
TASK_REVIEW_PROMPT = "task-review.md"

DECISIONS = {"ready", "needs_modification", "rejected"}
TOP_LEVEL_FIELDS = {
    "task_id",
    "decision",
    "summary",
    "modification_items",
    "blocking_reasons",
}


def subject_for(seed_id: str, idea_id: str) -> str:
    return f"{seed_id}__{idea_id}"


def review_dir_for(root: Path, seed_id: str, idea_id: str) -> Path:
    return root / "runs/reviews" / subject_for(seed_id, idea_id)


def review_json_path_for(root: Path, seed_id: str, idea_id: str) -> Path:
    return review_dir_for(root, seed_id, idea_id) / "review.json"


def review_markdown_path_for(root: Path, seed_id: str, idea_id: str) -> Path:
    return review_dir_for(root, seed_id, idea_id) / "review.md"


def generated_prompt_path_for(root: Path, seed_id: str, idea_id: str) -> Path:
    return root / "runs/prompts" / seed_id / idea_id / TASK_REVIEW_PROMPT


def session_root_for(root: Path, seed_id: str, idea_id: str) -> Path:
    return root / "runs/claude-sessions" / PHASE_NAME / subject_for(seed_id, idea_id)


def list_session_dirs(root: Path, seed_id: str, idea_id: str) -> set[Path]:
    session_root = session_root_for(root, seed_id, idea_id)
    if not session_root.is_dir():
        return set()
    return {path for path in session_root.iterdir() if path.is_dir()}


def session_ref_for(root: Path, session_dir: Path) -> str:
    return session_dir.relative_to(root).as_posix()


def find_new_session_ref(root: Path, seed_id: str, idea_id: str, before: set[Path]) -> str | None:
    created = list(list_session_dirs(root, seed_id, idea_id) - before)
    if not created:
        return None
    created.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
    return session_ref_for(root, created[0])


def review_ref(seed_id: str, idea_id: str) -> str:
    return f"runs/reviews/{subject_for(seed_id, idea_id)}/review.json"


def review_markdown_ref(seed_id: str, idea_id: str) -> str:
    return f"runs/reviews/{subject_for(seed_id, idea_id)}/review.md"


def oracle_status_ref(seed_id: str, idea_id: str) -> str:
    return f"runs/oracle-nop-check/{subject_for(seed_id, idea_id)}/oracle-nop-status.json"


def append_manifest_event(
    root: Path,
    seed_id: str,
    idea_id: str,
    decision: str,
    claude_session_ref: str,
) -> None:
    task_id = subject_for(seed_id, idea_id)
    payload = {
        "event": "reviewed",
        "seed_id": seed_id,
        "idea_id": idea_id,
        "task_id": task_id,
        "review_ref": review_ref(seed_id, idea_id),
        "review_markdown_ref": review_markdown_ref(seed_id, idea_id),
        "oracle_nop_ref": oracle_status_ref(seed_id, idea_id),
        "decision": decision,
        "claude_session_ref": claude_session_ref,
        "status": "reviewed",
        "reason": f"phase 5 review completed with decision {decision!r}",
    }
    manifest_path = root / "runs/task-manifest.jsonl"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def ensure_phase5_inputs(root: Path, seed_id: str, idea_id: str) -> list[str]:
    errors = validate_seed_id(seed_id) + validate_idea_id(idea_id)
    if errors:
        return errors

    for required in (
        generated_task_ref_for(seed_id, idea_id),
        f"runs/oracle-nop-check/{subject_for(seed_id, idea_id)}",
        f"prompts/{TASK_REVIEW_PROMPT}",
        "scripts/run-claude-logged.sh",
    ):
        candidate = root / required
        if not candidate.exists():
            errors.append(f"missing phase5 project file: {candidate}")

    phase4_report = validate_phase4(root, seed_id, idea_id, require_passed=False)
    if not phase4_report.passed:
        errors.append("phase4 oracle/nop status must be reviewable before phase5")
        errors.extend(phase4_report.errors)
    return errors


def render_phase5_prompt(root: Path, seed_id: str, idea_id: str) -> Path:
    template_path = root / "prompts" / TASK_REVIEW_PROMPT
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


def validate_phase5(root: Path, seed_id: str, idea_id: str, *, require_manifest: bool = True) -> ValidationReport:
    report = ValidationReport(phase=PHASE_KEY, seed_id=seed_id)
    id_errors = validate_seed_id(seed_id) + validate_idea_id(idea_id)
    if id_errors:
        report.errors.extend(id_errors)
        return report

    task_id = subject_for(seed_id, idea_id)
    review_json_path = review_json_path_for(root, seed_id, idea_id)
    review_md_path = review_markdown_path_for(root, seed_id, idea_id)

    phase4_report = validate_phase4(root, seed_id, idea_id, require_passed=False)
    report.checked_paths.extend(phase4_report.checked_paths)
    report.warnings.extend(phase4_report.warnings)
    if not phase4_report.passed:
        report.errors.append("phase4 oracle/nop status must be reviewable before phase5 is valid")
        report.errors.extend(phase4_report.errors)

    payload = load_json(review_json_path, report)
    decision: str | None = None
    if payload is not None:
        review = require_object(payload, "$", report)
        if review is not None:
            validate_review_payload(review, task_id, report)
            if isinstance(review.get("decision"), str):
                decision = review["decision"]

    report.checked_paths.append(str(review_md_path))
    if not review_md_path.is_file():
        report.errors.append(f"missing review markdown: {review_md_path}")
    else:
        validate_review_markdown(review_md_path, report)

    if require_manifest and decision is not None:
        validate_manifest_event(root, seed_id, idea_id, decision, report)
    return report


def validate_review_payload(payload: dict[str, Any], task_id: str, report: ValidationReport) -> None:
    actual_fields = set(payload)
    missing = sorted(TOP_LEVEL_FIELDS - actual_fields)
    extra = sorted(actual_fields - TOP_LEVEL_FIELDS)
    if missing:
        report.errors.append(f"review.json missing top-level field(s): {', '.join(missing)}")
    if extra:
        report.errors.append(f"review.json has unexpected top-level field(s): {', '.join(extra)}")

    actual_task_id = require_string(payload, "task_id", "$", report)
    if actual_task_id is not None and actual_task_id != task_id:
        report.errors.append(f"$.task_id must equal {task_id!r}, got {actual_task_id!r}")

    decision = require_string(payload, "decision", "$", report)
    if decision is not None and decision not in DECISIONS:
        report.errors.append(f"$.decision must be one of {sorted(DECISIONS)}")

    require_string(payload, "summary", "$", report)
    modification_items = validate_modification_items(payload.get("modification_items"), report)
    blocking_reasons = validate_blocking_reasons(payload.get("blocking_reasons"), report)

    if decision == "ready":
        if modification_items:
            report.errors.append("$.modification_items must be empty when decision is 'ready'")
        if blocking_reasons:
            report.errors.append("$.blocking_reasons must be empty when decision is 'ready'")
    elif decision == "needs_modification":
        if not modification_items:
            report.errors.append("$.modification_items must be non-empty when decision is 'needs_modification'")
        if blocking_reasons:
            report.errors.append("$.blocking_reasons must be empty when decision is 'needs_modification'")
    elif decision == "rejected":
        if modification_items:
            report.errors.append("$.modification_items must be empty when decision is 'rejected'")
        if not blocking_reasons:
            report.errors.append("$.blocking_reasons must be non-empty when decision is 'rejected'")


def validate_modification_items(value: Any, report: ValidationReport) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        report.errors.append("$.modification_items must be a list")
        return None

    valid_items: list[dict[str, Any]] = []
    for index, item_value in enumerate(value):
        path = f"$.modification_items[{index}]"
        item = require_object(item_value, path, report)
        if item is None:
            continue
        validate_exact_fields(
            item,
            {"area", "priority", "message", "evidence", "repair_direction"},
            path,
            report,
        )
        validate_non_empty_string(item.get("area"), f"{path}.area", report)
        validate_non_empty_string(item.get("priority"), f"{path}.priority", report)
        validate_non_empty_string(item.get("message"), f"{path}.message", report)
        validate_non_empty_string(item.get("repair_direction"), f"{path}.repair_direction", report)
        validate_evidence(item.get("evidence"), f"{path}.evidence", report)
        valid_items.append(item)
    return valid_items


def validate_blocking_reasons(value: Any, report: ValidationReport) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        report.errors.append("$.blocking_reasons must be a list")
        return None

    valid_items: list[dict[str, Any]] = []
    for index, item_value in enumerate(value):
        path = f"$.blocking_reasons[{index}]"
        item = require_object(item_value, path, report)
        if item is None:
            continue
        validate_exact_fields(item, {"area", "message", "evidence"}, path, report)
        validate_non_empty_string(item.get("area"), f"{path}.area", report)
        validate_non_empty_string(item.get("message"), f"{path}.message", report)
        validate_evidence(item.get("evidence"), f"{path}.evidence", report)
        valid_items.append(item)
    return valid_items


def validate_exact_fields(
    obj: dict[str, Any],
    expected: set[str],
    path: str,
    report: ValidationReport,
) -> None:
    actual = set(obj)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        report.errors.append(f"{path} missing field(s): {', '.join(missing)}")
    if extra:
        report.errors.append(f"{path} has unexpected field(s): {', '.join(extra)}")


def validate_non_empty_string(value: Any, path: str, report: ValidationReport) -> None:
    if not isinstance(value, str) or not value.strip():
        report.errors.append(f"{path} must be a non-empty string")


def validate_evidence(value: Any, path: str, report: ValidationReport) -> None:
    if not isinstance(value, list):
        report.errors.append(f"{path} must be a list")
        return
    if not value:
        report.errors.append(f"{path} must contain at least one item")
        return
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            report.errors.append(f"{path}[{index}] must be a non-empty string")


def validate_review_markdown(path: Path, report: ValidationReport) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        report.errors.append(f"cannot read review markdown {path}: {exc}")
        return
    if not text.strip():
        report.errors.append(f"review markdown must be non-empty: {path}")


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
            event.get("event") == "reviewed"
            and event.get("seed_id") == seed_id
            and event.get("idea_id") == idea_id
            and event.get("task_id") == task_id
            and event.get("review_ref") == review_ref(seed_id, idea_id)
            and event.get("review_markdown_ref") == review_markdown_ref(seed_id, idea_id)
            and event.get("oracle_nop_ref") == oracle_status_ref(seed_id, idea_id)
            and event.get("decision") == decision
        ):
            continue

        line_errors = validate_manifest_candidate(root, event, report)
        if line_errors:
            candidate_errors.append(f"{manifest_path}:{line_number}: " + "; ".join(line_errors))
        else:
            found = True
            break

    if not found:
        report.errors.append(f"manifest has no matching reviewed event for task_id={task_id!r}")
        report.errors.extend(candidate_errors)


def validate_manifest_candidate(root: Path, event: dict[str, Any], report: ValidationReport) -> list[str]:
    errors: list[str] = []
    if event.get("status") != "reviewed":
        errors.append("status must be 'reviewed'")
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


def load_review_decision_for_manifest(root: Path, seed_id: str, idea_id: str) -> str | None:
    try:
        payload = json.loads(review_json_path_for(root, seed_id, idea_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    decision = payload.get("decision")
    return decision if isinstance(decision, str) and decision.strip() else None


def command_run(args: argparse.Namespace) -> int:
    root = project_root()
    errors = ensure_phase5_inputs(root, args.seed_id, args.idea_id)
    if errors:
        print(
            f"cannot run phase5 for seed {args.seed_id} idea {args.idea_id}; prerequisites failed",
            file=sys.stderr,
        )
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    prompt_path = render_phase5_prompt(root, args.seed_id, args.idea_id)
    model = resolve_model_name(root, args.model)
    effort = resolve_effort_level(root, args.effort, PHASE_KEY)
    command = build_claude_command(root, args.seed_id, args.idea_id, prompt_path, model, effort)

    print("phase5 prompt:", prompt_path)
    print("phase5 command:", " ".join(command))
    if args.dry_run:
        return 0

    before_sessions = list_session_dirs(root, args.seed_id, args.idea_id)
    exit_code = subprocess.run(command, cwd=root, check=False).returncode
    if exit_code != 0:
        return exit_code

    print()
    print("validating phase5 review output...")
    review_report = validate_phase5(root, args.seed_id, args.idea_id, require_manifest=False)
    review_exit_code = print_report(review_report, as_json=False)
    if review_exit_code != 0:
        return review_exit_code

    claude_session_ref = find_new_session_ref(root, args.seed_id, args.idea_id, before_sessions)
    if claude_session_ref is None:
        print("cannot append manifest: no Claude phase5 session directory was found", file=sys.stderr)
        return 1

    decision = load_review_decision_for_manifest(root, args.seed_id, args.idea_id)
    if decision is None:
        print("cannot append manifest: review decision is missing", file=sys.stderr)
        return 1
    append_manifest_event(root, args.seed_id, args.idea_id, decision, claude_session_ref)

    print()
    print("validating phase5 manifest...")
    return print_report(validate_phase5(root, args.seed_id, args.idea_id), as_json=False)


def command_validate(args: argparse.Namespace) -> int:
    return print_report(validate_phase5(project_root(), args.seed_id, args.idea_id), args.json)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run phase5, then validate its output.")
    run.add_argument("seed_id")
    run.add_argument("--idea-id", required=True)
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the Claude review command without running Claude or validation.",
    )
    run.add_argument("--model", help="Claude model to use. Defaults to model.json default_model when omitted.")
    run.add_argument(
        "--effort",
        choices=EFFORT_LEVELS,
        help="Claude Code effort level for this run. Defaults to model.json phase_efforts.phase5, then default_effort.",
    )
    run.set_defaults(func=command_run)

    validate = subparsers.add_parser("validate", help="Validate phase5 output.")
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
