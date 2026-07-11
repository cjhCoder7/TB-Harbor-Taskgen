#!/usr/bin/env python3
"""Phase 1 runner and validator: seed brainstorm.

This phase reads a read-only seed task through Claude Code and deterministically
records:

- `runs/brainstorm/<seed_id>/seed_brainstorm.json`
- one matching `brainstormed` event in `runs/task-manifest.jsonl`

The phase runner renders a seed-specific prompt, starts Claude through the
logged wrapper, and validates the resulting artifact after Claude exits
successfully.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from taskgen.common import (
    ValidationReport,
    append_jsonl_object,
    delegated_phase_subject_lock_kwargs,
    load_json,
    phase_subject_lock,
    print_report,
    project_root,
    read_jsonl_objects,
    require_no_template_markers,
    require_object,
    require_string,
    require_string_list,
    select_new_claude_session,
    validate_claude_session_reference,
    validate_idea_identifier,
    validate_seed_identifier,
)
from taskgen.config import (
    EFFORT_LEVELS,
    resolve_effort_level,
    resolve_model_name,
)


PHASE_KEY = "phase1"
PHASE_NAME = "seed-brainstorm"
BRAINSTORM_FILENAME = "seed_brainstorm.json"
IDEA_ID_RE = re.compile(r"[A-Za-z0-9._-]+")


def positive_int_arg(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--idea-count must be an integer >= 1") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("--idea-count must be an integer >= 1")
    return parsed


def validate_seed_id(seed_id: str) -> list[str]:
    return validate_seed_identifier(seed_id)


def seed_path_for(root: Path, seed_id: str) -> Path:
    return (root / "seeds" / seed_id).resolve()


def seed_path_containment_error(root: Path, seed_id: str) -> str | None:
    try:
        seed_path = seed_path_for(root, seed_id)
        seeds_root = (root / "seeds").resolve()
    except (OSError, RuntimeError) as exc:
        return f"cannot resolve seed path safely: {exc}"
    try:
        seed_path.relative_to(seeds_root)
    except ValueError:
        return f"seed path must stay under {seeds_root}: {seed_path}"
    return None


def brainstorm_ref_for(seed_id: str) -> str:
    return f"runs/brainstorm/{seed_id}/{BRAINSTORM_FILENAME}"


def workspace_seed_ref_for(seed_id: str) -> str:
    return f"seed/{seed_id}"


def session_root_for(root: Path, seed_id: str) -> Path:
    return root / "runs/claude-sessions" / PHASE_NAME / seed_id


def list_session_dirs(root: Path, seed_id: str) -> set[Path]:
    session_root = session_root_for(root, seed_id)
    if not session_root.is_dir():
        return set()
    return {path for path in session_root.iterdir() if path.is_dir()}


def session_ref_for(root: Path, session_dir: Path) -> str:
    return session_dir.relative_to(root).as_posix()


def find_new_session_ref(root: Path, seed_id: str, before: set[Path]) -> str | None:
    session_dir, _ = select_new_claude_session(
        root,
        expected_phase=PHASE_NAME,
        expected_subject=seed_id,
        expected_outputs=[brainstorm_ref_for(seed_id)],
        before=before,
    )
    return session_ref_for(root, session_dir) if session_dir is not None else None


def append_manifest_event(root: Path, seed_id: str, claude_session_ref: str) -> None:
    manifest_path = root / "runs/task-manifest.jsonl"
    payload = {
        "event": "brainstormed",
        "seed_id": seed_id,
        "brainstorm_ref": brainstorm_ref_for(seed_id),
        "claude_session_ref": claude_session_ref,
        "status": "working",
        "reason": "phase 1 brainstorm completed",
    }
    append_jsonl_object(manifest_path, payload)


def ensure_seed_layout(root: Path, seed_id: str, errors: list[str]) -> None:
    seed_path = seed_path_for(root, seed_id)
    if not seed_path.is_dir():
        errors.append(f"missing seed directory: {seed_path}")
        return

    for required in ("instruction.md", "task.toml"):
        candidate = seed_path / required
        if not candidate.is_file():
            errors.append(f"missing seed file: {candidate}")
    for required_dir in ("environment", "solution", "tests"):
        candidate = seed_path / required_dir
        if not candidate.is_dir():
            errors.append(f"missing seed directory: {candidate}")


def ensure_phase1_inputs(root: Path, seed_id: str) -> list[str]:
    errors = validate_seed_id(seed_id)
    if errors:
        return errors

    containment_error = seed_path_containment_error(root, seed_id)
    if containment_error:
        return [containment_error]

    ensure_seed_layout(root, seed_id, errors)
    for required in (
        "prompts/seed-brainstorm.md",
        "cc-definitions/agents/seed-brainstormer.md",
        "cc-definitions/skills/tb-harbor-task-generation/SKILL.md",
        "scripts/run-claude-logged.sh",
    ):
        candidate = root / required
        if not candidate.exists():
            errors.append(f"missing phase1 project file: {candidate}")

    return errors


def idea_count_requirement(idea_count: int | None) -> str:
    if idea_count is None:
        return (
            "Produce 3-5 substantially different TB3 task ideas by default. "
            "The validator allows more ideas, but every idea must be concrete and useful."
        )
    return (
        f"Produce exactly {idea_count} substantially different TB3 task ideas. "
        f"The `ideas` array must contain exactly {idea_count} items."
    )


def render_phase1_prompt(root: Path, seed_id: str, idea_count: int | None = None) -> Path:
    template_path = root / "prompts/seed-brainstorm.md"
    output_path = root / "runs/prompts" / seed_id / "seed-brainstorm.md"
    prompt = template_path.read_text(encoding="utf-8")
    prompt = prompt.replace("{{SEED_ID}}", seed_id)
    prompt = prompt.replace("{{SEED_PATH}}", workspace_seed_ref_for(seed_id))
    prompt = prompt.replace("{{IDEA_COUNT_REQUIREMENT}}", idea_count_requirement(idea_count))
    require_no_template_markers(prompt, "phase1 prompt")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(prompt, encoding="utf-8")
    return output_path


def validate_manifest_event(root: Path, seed_id: str, brainstorm_ref: str, report: ValidationReport) -> None:
    manifest_path = root / "runs/task-manifest.jsonl"
    report.checked_paths.append(str(manifest_path))
    if not manifest_path.exists():
        report.errors.append(f"missing manifest: {manifest_path}")
        return

    found = False
    candidate_errors: list[str] = []
    for line_number, event in read_jsonl_objects(manifest_path, report):
        if not (
            event.get("event") == "brainstormed"
            and event.get("seed_id") == seed_id
            and event.get("brainstorm_ref") == brainstorm_ref
        ):
            continue

        line_errors = validate_manifest_candidate(root, event, report)
        if line_errors:
            candidate_errors.append(f"{manifest_path}:{line_number}: " + "; ".join(line_errors))
        else:
            found = True

    if not found:
        report.errors.append(
            "manifest has no matching brainstormed event for "
            f"seed_id={seed_id!r}, brainstorm_ref={brainstorm_ref!r}, "
            "status='working', and a valid claude_session_ref"
        )
        report.errors.extend(candidate_errors)


def validate_manifest_candidate(root: Path, event: dict[str, Any], report: ValidationReport) -> list[str]:
    errors: list[str] = []
    if event.get("status") != "working":
        errors.append("status must be 'working'")
    if not isinstance(event.get("reason"), str) or not event["reason"].strip():
        errors.append("reason must be a non-empty string")

    errors.extend(
        validate_claude_session_reference(
            root,
            event.get("claude_session_ref"),
            expected_phase=PHASE_NAME,
            expected_subject=str(event.get("seed_id")),
            expected_outputs=[brainstorm_ref_for(str(event.get("seed_id")))],
            report=report,
        )
    )
    return errors


def validate_seed_layout(root: Path, seed_id: str, report: ValidationReport) -> None:
    seed_path = seed_path_for(root, seed_id)
    report.checked_paths.append(str(seed_path))
    if not seed_path.is_dir():
        report.errors.append(f"missing seed directory: {seed_path}")
        return

    for required in ("instruction.md", "task.toml"):
        candidate = seed_path / required
        report.checked_paths.append(str(candidate))
        if not candidate.is_file():
            report.errors.append(f"missing seed file: {candidate}")
    for required_dir in ("environment", "solution", "tests"):
        candidate = seed_path / required_dir
        report.checked_paths.append(str(candidate))
        if not candidate.is_dir():
            report.errors.append(f"missing seed directory: {candidate}")


def validate_brainstorm_data(
    data: dict[str, Any],
    seed_id: str,
    report: ValidationReport,
    *,
    expected_idea_count: int | None = None,
) -> None:
    actual_seed_id = require_string(data, "seed_id", "$", report)
    if actual_seed_id is not None and actual_seed_id != seed_id:
        report.errors.append(f"$.seed_id must equal {seed_id!r}, got {actual_seed_id!r}")

    source_path = require_string(data, "source_path", "$", report)
    expected_source_path = workspace_seed_ref_for(seed_id)
    if source_path is not None and source_path != expected_source_path:
        report.warnings.append(f"$.source_path should be {expected_source_path!r}: {source_path!r}")

    require_string(data, "task_understanding", "$", report)
    require_string_list(data, "core_capabilities", "$", report, min_items=1)
    require_string_list(data, "avoid", "$", report, min_items=1)
    validate_ideas(data.get("ideas"), report, expected_idea_count=expected_idea_count)


def validate_ideas(
    ideas: Any,
    report: ValidationReport,
    *,
    expected_idea_count: int | None = None,
) -> None:
    if not isinstance(ideas, list):
        report.errors.append("$.ideas must be a list")
        return

    if not ideas:
        report.errors.append("$.ideas must contain at least 1 idea")
    if expected_idea_count is not None and len(ideas) != expected_idea_count:
        report.errors.append(
            f"$.ideas must contain exactly {expected_idea_count} idea(s), got {len(ideas)}"
        )

    seen_idea_ids: set[str] = set()
    for index, idea_value in enumerate(ideas):
        idea_path = f"$.ideas[{index}]"
        idea = require_object(idea_value, idea_path, report)
        if idea is None:
            continue
        validate_idea(idea, idea_path, seen_idea_ids, report)


def validate_idea(
    idea: dict[str, Any],
    idea_path: str,
    seen_idea_ids: set[str],
    report: ValidationReport,
) -> None:
    idea_id = require_string(idea, "idea_id", idea_path, report)
    if idea_id is not None:
        if idea_id in seen_idea_ids:
            report.errors.append(f"{idea_path}.idea_id is duplicated: {idea_id!r}")
        seen_idea_ids.add(idea_id)
        if not IDEA_ID_RE.fullmatch(idea_id):
            report.errors.append(f"{idea_path}.idea_id must be path-friendly: {idea_id!r}")
        report.errors.extend(validate_idea_identifier(idea_id))

    for key in ("title", "scenario", "core_transfer", "verifier_sketch"):
        require_string(idea, key, idea_path, report)

    changed_dimensions = require_string_list(idea, "changed_dimensions", idea_path, report, min_items=2)
    if changed_dimensions is not None and len(set(changed_dimensions)) < 2:
        report.errors.append(f"{idea_path}.changed_dimensions must contain at least 2 distinct items")
    require_string_list(idea, "expected_artifacts", idea_path, report, min_items=1)
    require_string_list(idea, "risk_notes", idea_path, report, min_items=1)
    require_string_list(idea, "skillnet_queries", idea_path, report, min_items=1)
    validate_difficulty_profile(idea.get("difficulty_profile"), idea_path, report)


def validate_difficulty_profile(value: Any, idea_path: str, report: ValidationReport) -> None:
    path = f"{idea_path}.difficulty_profile"
    profile = require_object(value, path, report)
    if profile is None:
        return

    require_positive_int(profile, "minimum_independent_subskills", path, report, min_value=2)
    require_string_list(profile, "too_easy_antipatterns", path, report, min_items=1)
    require_string_list(profile, "hardening_levers", path, report, min_items=1)
    require_string_list(profile, "fairness_bounds", path, report, min_items=1)


def require_positive_int(
    obj: dict[str, Any],
    key: str,
    path: str,
    report: ValidationReport,
    *,
    min_value: int = 1,
) -> int | None:
    value = obj.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < min_value:
        report.errors.append(f"{path}.{key} must be an integer >= {min_value}")
        return None
    return value


def validate_phase1(
    root: Path,
    seed_id: str,
    *,
    require_manifest: bool = True,
    expected_idea_count: int | None = None,
) -> ValidationReport:
    report = ValidationReport(phase=PHASE_KEY, seed_id=seed_id)

    seed_errors = validate_seed_id(seed_id)
    if seed_errors:
        report.errors.extend(seed_errors)
        return report

    containment_error = seed_path_containment_error(root, seed_id)
    if containment_error:
        report.errors.append(containment_error)
        return report

    validate_seed_layout(root, seed_id, report)

    brainstorm_ref = brainstorm_ref_for(seed_id)
    brainstorm_path = root / brainstorm_ref
    data = load_json(brainstorm_path, report)
    data = require_object(data, "$", report) if data is not None else None
    if data is None:
        return report

    validate_brainstorm_data(data, seed_id, report, expected_idea_count=expected_idea_count)

    if require_manifest:
        validate_manifest_event(root, seed_id, brainstorm_ref, report)
    return report


def build_claude_command(
    root: Path,
    seed_id: str,
    prompt_path: Path,
    model: str | None,
    effort: str | None,
) -> list[str]:
    command = [
        str(root / "scripts/run-claude-logged.sh"),
        PHASE_NAME,
        seed_id,
        str(prompt_path.relative_to(root)),
    ]
    if model:
        command.extend(["--model", model])
    if effort:
        command.extend(["--effort", effort])
    return command


def _command_run_locked(args: argparse.Namespace) -> int:
    root = project_root()
    errors = ensure_phase1_inputs(root, args.seed_id)
    if errors:
        print(f"cannot run phase1 for seed {args.seed_id}; prerequisites failed", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    prompt_path = render_phase1_prompt(root, args.seed_id, args.idea_count)
    model = resolve_model_name(root, args.model)
    effort = resolve_effort_level(root, args.effort, PHASE_KEY)
    command = build_claude_command(root, args.seed_id, prompt_path, model, effort)

    print("phase1 prompt:", prompt_path)
    print("phase1 command:", " ".join(command))
    if args.dry_run:
        return 0

    before_sessions = list_session_dirs(root, args.seed_id)
    exit_code = subprocess.run(
        command,
        cwd=root,
        check=False,
        **delegated_phase_subject_lock_kwargs(root, args.seed_id),
    ).returncode
    if exit_code != 0:
        return exit_code

    session_dir, session_errors = select_new_claude_session(
        root,
        expected_phase=PHASE_NAME,
        expected_subject=args.seed_id,
        expected_outputs=[brainstorm_ref_for(args.seed_id)],
        before=before_sessions,
    )
    if session_dir is None:
        print("cannot append manifest: Claude session validation failed", file=sys.stderr)
        for error in session_errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print()
    print("validating phase1 brainstorm output...")
    brainstorm_report = validate_phase1(
        root,
        args.seed_id,
        require_manifest=False,
        expected_idea_count=args.idea_count,
    )
    brainstorm_exit_code = print_report(brainstorm_report, as_json=False)
    if brainstorm_exit_code != 0:
        return brainstorm_exit_code

    append_manifest_event(root, args.seed_id, session_ref_for(root, session_dir))

    print()
    print("validating phase1 manifest...")
    return print_report(
        validate_phase1(root, args.seed_id, expected_idea_count=args.idea_count),
        as_json=False,
    )


def command_run(args: argparse.Namespace) -> int:
    root = project_root()
    with phase_subject_lock(root, PHASE_NAME, args.seed_id):
        return _command_run_locked(args)


def command_validate(args: argparse.Namespace) -> int:
    return print_report(validate_phase1(project_root(), args.seed_id), args.json)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run phase1, then validate its output.")
    run.add_argument("seed_id")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Render the prompt and print the command without running Claude or validation.",
    )
    run.add_argument("--model", help="Claude model to use. Defaults to model.json default_model when omitted.")
    run.add_argument(
        "--idea-count",
        type=positive_int_arg,
        help="Exact number of brainstorm ideas to request and validate for this phase1 run.",
    )
    run.add_argument(
        "--effort",
        choices=EFFORT_LEVELS,
        help="Claude Code effort level for this run. Defaults to model.json phase_efforts.phase1, then default_effort.",
    )
    run.set_defaults(func=command_run)

    validate = subparsers.add_parser("validate", help="Validate phase1 output.")
    validate.add_argument("seed_id")
    validate.add_argument("--json", action="store_true", help="Emit machine-readable validation output.")
    validate.set_defaults(func=command_validate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
