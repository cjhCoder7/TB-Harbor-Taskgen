#!/usr/bin/env python3
"""Phase 3 runner and validator: Harbor task generation.

This phase reads one brainstorm idea plus its curated SkillNet preparation
artifacts through Claude Code and records a generated working task:

- `generated/working/<seed_id>/<idea_id>/`
- one matching `generated` event in `runs/task-manifest.jsonl`

The phase runner renders an idea-specific prompt, starts Claude through the
logged wrapper, and validates the resulting task directory after Claude exits
successfully.
"""

from __future__ import annotations

import argparse
import json
import re
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
    validate_path_segment,
)
from taskgen.config import (
    EFFORT_LEVELS,
    resolve_effort_level,
    resolve_model_name,
)
from taskgen.phases.phase1_seed_brainstorm import (
    brainstorm_ref_for,
    validate_brainstorm_data,
)
from taskgen.phases.phase2_skillnet_research import (
    skillnet_index_ref_for,
    skillnet_root_ref_for,
    validate_skill_summary,
    workspace_skill_summary_ref_for,
)


PHASE_KEY = "phase3"
PHASE_NAME = "task-generation"
TASK_GENERATION_PROMPT = "task-generation.md"
TASK_AGENT = "tb-harbor-task-generator.md"

TEMPLATE_MARKER_PATTERN = re.compile(r"{{[^{}]+}}")
FORBIDDEN_TEXT_PATTERNS = (
    "/shared/users/",
    "/var/lib/.xstor/",
    "runs/claude-sessions",
    "runs/workspace",
    "claude-code.txt",
    "task-manifest.jsonl",
)
FORBIDDEN_ROOT_DIRS = {"seed", "brainstorm", "skillnet", "raw", ".claude"}
FORBIDDEN_TASK_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".tox",
    ".venv",
    "harbor-jobs",
    "output",
    "phase3-validation",
    "phase6-validation",
}
FORBIDDEN_FILENAMES = {
    "claude-code.txt",
    "cost.json",
    "prompt.md",
    "seed_brainstorm.json",
    "skill_summary.json",
    "skillnet_index.json",
    "status.json",
    "task-manifest.jsonl",
}
FORBIDDEN_TASK_FILE_SUFFIXES = {".log", ".pyc"}
REQUIRED_TASK_FILES = ("instruction.md", "task.toml")
REQUIRED_TASK_DIRS = ("environment", "solution", "tests")
TASK_DOCKERFILE = "environment/Dockerfile"
TASK_TEST_ENTRYPOINT = "tests/test.sh"
TASK_TEST_DOCKERFILE = "tests/Dockerfile"
TASK_SOLUTION_ENTRYPOINT = "solution/solve.sh"
TEXT_FILE_SIZE_LIMIT = 1_000_000
TEXT_SUFFIXES = {
    ".cfg",
    ".conf",
    ".csv",
    ".env",
    ".ini",
    ".json",
    ".log",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".txt",
    ".yaml",
    ".yml",
}


def validate_seed_id(seed_id: str) -> list[str]:
    return validate_path_segment(seed_id, "seed_id")


def validate_idea_id(idea_id: str) -> list[str]:
    return validate_path_segment(idea_id, "idea_id")


def subject_for(seed_id: str, idea_id: str) -> str:
    return f"{seed_id}__{idea_id}"


def workspace_seed_ref_for(seed_id: str) -> str:
    return f"seed/{seed_id}"


def workspace_brainstorm_ref_for(seed_id: str) -> str:
    return f"brainstorm/{seed_id}/seed_brainstorm.json"


def workspace_skill_summary_ref_for_phase3(seed_id: str, idea_id: str) -> str:
    return workspace_skill_summary_ref_for(seed_id, idea_id)


def generated_task_ref_for(seed_id: str, idea_id: str) -> str:
    return f"generated/working/{seed_id}/{idea_id}"


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


def generated_prompt_path_for(root: Path, seed_id: str, idea_id: str) -> Path:
    return root / "runs/prompts" / seed_id / idea_id / TASK_GENERATION_PROMPT


def task_id_for(root: Path, seed_id: str, idea_id: str) -> str:
    return subject_for(seed_id, idea_id)


def append_manifest_event(root: Path, seed_id: str, idea_id: str, claude_session_ref: str) -> None:
    manifest_path = root / "runs/task-manifest.jsonl"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "event": "generated",
        "seed_id": seed_id,
        "idea_id": idea_id,
        "task_id": task_id_for(root, seed_id, idea_id),
        "task_path": generated_task_ref_for(seed_id, idea_id),
        "brainstorm_ref": brainstorm_ref_for(seed_id),
        "skillnet_ref": skillnet_index_ref_for(seed_id),
        "skill_summary_ref": f"{skillnet_root_ref_for(seed_id)}/{idea_id}/skill_summary.json",
        "claude_session_ref": claude_session_ref,
        "status": "working",
        "reason": "phase 3 task generation completed",
    }
    with manifest_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def ensure_phase3_inputs(root: Path, seed_id: str, idea_id: str) -> list[str]:
    errors = validate_seed_id(seed_id) + validate_idea_id(idea_id)
    if errors:
        return errors

    for required in (
        f"seeds/{seed_id}",
        brainstorm_ref_for(seed_id),
        skillnet_index_ref_for(seed_id),
        f"{skillnet_root_ref_for(seed_id)}/{idea_id}/skill_summary.json",
        f"{skillnet_root_ref_for(seed_id)}/{idea_id}/skills",
        f"prompts/{TASK_GENERATION_PROMPT}",
        f"cc-definitions/agents/{TASK_AGENT}",
        "cc-definitions/skills/tb-harbor-task-generation/SKILL.md",
        "scripts/run-claude-logged.sh",
    ):
        candidate = root / required
        if not candidate.exists():
            errors.append(f"missing phase3 project file: {candidate}")
    return errors


def render_phase3_prompt(root: Path, seed_id: str, idea_id: str) -> Path:
    template_path = root / "prompts" / TASK_GENERATION_PROMPT
    output_path = generated_prompt_path_for(root, seed_id, idea_id)
    prompt = template_path.read_text(encoding="utf-8")
    replacements = {
        "{{SEED_ID}}": seed_id,
        "{{IDEA_ID}}": idea_id,
        "{{SUBJECT}}": subject_for(seed_id, idea_id),
        "{{SEED_PATH}}": workspace_seed_ref_for(seed_id),
        "{{BRAINSTORM_PATH}}": workspace_brainstorm_ref_for(seed_id),
        "{{SKILL_SUMMARY_PATH}}": workspace_skill_summary_ref_for_phase3(seed_id, idea_id),
        "{{OUTPUT_PATH}}": "output/task",
        "{{GENERATED_TASK_REF}}": generated_task_ref_for(seed_id, idea_id),
    }
    for marker, value in replacements.items():
        prompt = prompt.replace(marker, value)
    unreplaced_markers = sorted(set(TEMPLATE_MARKER_PATTERN.findall(prompt)))
    if unreplaced_markers:
        raise SystemExit(
            "phase3 prompt contains unreplaced marker(s): "
            + ", ".join(unreplaced_markers)
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(prompt, encoding="utf-8")
    return output_path


def load_brainstorm_idea(
    root: Path,
    seed_id: str,
    idea_id: str,
    report: ValidationReport,
) -> dict[str, Any] | None:
    data = load_json(root / brainstorm_ref_for(seed_id), report)
    brainstorm = require_object(data, "$.brainstorm", report) if data is not None else None
    if brainstorm is None:
        return None

    validate_brainstorm_data(brainstorm, seed_id, report)
    ideas = brainstorm.get("ideas")
    if not isinstance(ideas, list):
        return None

    matches = [
        idea
        for idea in ideas
        if isinstance(idea, dict) and idea.get("idea_id") == idea_id
    ]
    if not matches:
        report.errors.append(f"phase1 brainstorm has no idea_id={idea_id!r}")
        return None
    if len(matches) > 1:
        report.errors.append(f"phase1 brainstorm has duplicate idea_id={idea_id!r}")
    return matches[0]


def validate_phase3_inputs(root: Path, seed_id: str, idea_id: str, report: ValidationReport) -> None:
    idea = load_brainstorm_idea(root, seed_id, idea_id, report)
    expected_title = ""
    if idea is not None:
        title = idea.get("title")
        if isinstance(title, str):
            expected_title = title

    if expected_title:
        validate_skillnet_index_for_idea(root, seed_id, idea_id, expected_title, report)
        validate_skill_summary(root, seed_id, idea_id, expected_title, report)
    else:
        load_json(root / skillnet_index_ref_for(seed_id), report)
        load_json(root / skillnet_root_ref_for(seed_id) / idea_id / "skill_summary.json", report)


def validate_skillnet_index_for_idea(
    root: Path,
    seed_id: str,
    idea_id: str,
    expected_title: str,
    report: ValidationReport,
) -> None:
    index_path = root / skillnet_index_ref_for(seed_id)
    data = load_json(index_path, report)
    index = require_object(data, "$.skillnet_index", report) if data is not None else None
    if index is None:
        return

    actual_seed_id = require_string(index, "seed_id", "$.skillnet_index", report)
    if actual_seed_id is not None and actual_seed_id != seed_id:
        report.errors.append(f"$.skillnet_index.seed_id must equal {seed_id!r}")

    brainstorm_ref = require_string(index, "brainstorm_ref", "$.skillnet_index", report)
    expected_brainstorm_ref = workspace_brainstorm_ref_for(seed_id)
    if brainstorm_ref is not None and brainstorm_ref != expected_brainstorm_ref:
        report.errors.append(
            f"$.skillnet_index.brainstorm_ref must be {expected_brainstorm_ref!r}"
        )

    ideas = index.get("ideas")
    if not isinstance(ideas, list):
        report.errors.append("$.skillnet_index.ideas must be a list")
        return

    matches = [
        entry
        for entry in ideas
        if isinstance(entry, dict) and entry.get("idea_id") == idea_id
    ]
    if not matches:
        report.errors.append(f"skillnet_index.json has no idea_id={idea_id!r}")
        return
    if len(matches) > 1:
        report.errors.append(f"skillnet_index.json has duplicate idea_id={idea_id!r}")

    entry = matches[0]
    title = entry.get("title")
    if isinstance(title, str) and title != expected_title:
        report.warnings.append(f"skillnet_index title differs from phase1 brainstorm title for {idea_id!r}")
    summary_ref = entry.get("skill_summary_ref")
    expected_summary_ref = workspace_skill_summary_ref_for_phase3(seed_id, idea_id)
    if summary_ref != expected_summary_ref:
        report.errors.append(
            f"skillnet_index skill_summary_ref for {idea_id!r} must be {expected_summary_ref!r}"
        )


def validate_required_task_layout(task_path: Path, report: ValidationReport) -> None:
    report.checked_paths.append(str(task_path))
    if not task_path.is_dir():
        report.errors.append(f"missing generated task directory: {task_path}")
        return

    for required_file in REQUIRED_TASK_FILES:
        candidate = task_path / required_file
        report.checked_paths.append(str(candidate))
        if not candidate.is_file():
            report.errors.append(f"missing generated task file: {candidate}")

    for required_dir in REQUIRED_TASK_DIRS:
        candidate = task_path / required_dir
        report.checked_paths.append(str(candidate))
        if not candidate.is_dir():
            report.errors.append(f"missing generated task directory: {candidate}")
        elif not any(candidate.iterdir()):
            report.errors.append(f"generated task directory is empty: {candidate}")

    dockerfile = task_path / TASK_DOCKERFILE
    report.checked_paths.append(str(dockerfile))
    if not dockerfile.is_file():
        report.errors.append(f"missing generated task Dockerfile: {dockerfile}")

    test_entry = task_path / TASK_TEST_ENTRYPOINT
    report.checked_paths.append(str(test_entry))
    if not test_entry.is_file():
        report.errors.append(f"missing generated task test entrypoint: {test_entry}")

    test_dockerfile = task_path / TASK_TEST_DOCKERFILE
    report.checked_paths.append(str(test_dockerfile))
    if not test_dockerfile.is_file():
        report.errors.append(f"missing generated task verifier Dockerfile: {test_dockerfile}")

    solution_entrypoint = task_path / TASK_SOLUTION_ENTRYPOINT
    report.checked_paths.append(str(solution_entrypoint))
    if not solution_entrypoint.is_file():
        report.errors.append(f"missing generated task solution entrypoint: {solution_entrypoint}")


def safe_read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def iter_text_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            if path.stat().st_size > TEXT_FILE_SIZE_LIMIT:
                continue
        except OSError:
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in {"Dockerfile", "test.sh"}:
            files.append(path)
    return files


def validate_no_runner_artifacts(task_path: Path, report: ValidationReport) -> None:
    if not task_path.is_dir():
        return

    for path in sorted(task_path.rglob("*")):
        rel = path.relative_to(task_path).as_posix()
        parts = rel.split("/")
        if parts[0] in FORBIDDEN_ROOT_DIRS:
            report.errors.append(f"generated task contains phase input or runner directory: {rel}")
        if path.is_dir() and path.name in FORBIDDEN_TASK_DIR_NAMES:
            report.errors.append(f"generated task contains transient or validation directory: {rel}")
        if path.name in FORBIDDEN_FILENAMES:
            report.errors.append(f"generated task contains runner/intermediate file: {rel}")
        if path.is_file() and path.suffix.lower() in FORBIDDEN_TASK_FILE_SUFFIXES:
            report.errors.append(f"generated task contains transient file: {rel}")
        if path.is_symlink():
            report.errors.append(f"generated task must not contain symlinks: {rel}")


def validate_no_forbidden_text(task_path: Path, seed_id: str, report: ValidationReport) -> None:
    if not task_path.is_dir():
        return

    dynamic_patterns = [
        f"seeds/{seed_id}",
        f"seed/{seed_id}",
        f"runs/brainstorm/{seed_id}",
        f"runs/skillnet/{seed_id}",
    ]
    for path in iter_text_files(task_path):
        text = safe_read_text(path)
        if text is None:
            continue
        rel = path.relative_to(task_path).as_posix()
        for pattern in (*FORBIDDEN_TEXT_PATTERNS, *dynamic_patterns):
            if pattern in text:
                report.errors.append(f"{rel} contains forbidden runner/seed path reference: {pattern}")


def validate_generated_task(root: Path, seed_id: str, idea_id: str, report: ValidationReport) -> None:
    task_path = root / generated_task_ref_for(seed_id, idea_id)
    validate_required_task_layout(task_path, report)
    if not task_path.is_dir():
        return

    validate_no_runner_artifacts(task_path, report)
    validate_no_forbidden_text(task_path, seed_id, report)


def validate_manifest_event(root: Path, seed_id: str, idea_id: str, report: ValidationReport) -> None:
    manifest_path = root / "runs/task-manifest.jsonl"
    report.checked_paths.append(str(manifest_path))
    if not manifest_path.exists():
        report.errors.append(f"missing manifest: {manifest_path}")
        return

    task_ref = generated_task_ref_for(seed_id, idea_id)
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

        if not (
            event.get("event") == "generated"
            and event.get("seed_id") == seed_id
            and event.get("idea_id") == idea_id
            and event.get("task_path") == task_ref
        ):
            continue

        line_errors = validate_manifest_candidate(root, event, seed_id, idea_id, report)
        if line_errors:
            candidate_errors.append(f"{manifest_path}:{line_number}: " + "; ".join(line_errors))
        else:
            found = True

    if not found:
        report.errors.append(
            "manifest has no matching generated event for "
            f"seed_id={seed_id!r}, idea_id={idea_id!r}, task_path={task_ref!r}, "
            "status='working', and a valid claude_session_ref"
        )
        report.errors.extend(candidate_errors)


def validate_manifest_candidate(
    root: Path,
    event: dict[str, Any],
    seed_id: str,
    idea_id: str,
    report: ValidationReport,
) -> list[str]:
    errors: list[str] = []
    if event.get("status") != "working":
        errors.append("status must be 'working'")
    if not isinstance(event.get("reason"), str) or not event["reason"].strip():
        errors.append("reason must be a non-empty string")
    if event.get("brainstorm_ref") != brainstorm_ref_for(seed_id):
        errors.append("brainstorm_ref does not match seed_id")
    if event.get("skillnet_ref") != skillnet_index_ref_for(seed_id):
        errors.append("skillnet_ref does not match seed_id")
    expected_summary_ref = f"{skillnet_root_ref_for(seed_id)}/{idea_id}/skill_summary.json"
    if event.get("skill_summary_ref") != expected_summary_ref:
        errors.append("skill_summary_ref does not match seed_id and idea_id")
    if not isinstance(event.get("task_id"), str) or not event["task_id"].strip():
        errors.append("task_id must be a non-empty string")

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


def validate_phase3(
    root: Path,
    seed_id: str,
    idea_id: str,
    *,
    require_manifest: bool = True,
) -> ValidationReport:
    report = ValidationReport(phase=PHASE_KEY, seed_id=seed_id)

    id_errors = validate_seed_id(seed_id) + validate_idea_id(idea_id)
    if id_errors:
        report.errors.extend(id_errors)
        return report

    validate_phase3_inputs(root, seed_id, idea_id, report)
    validate_generated_task(root, seed_id, idea_id, report)

    if require_manifest:
        validate_manifest_event(root, seed_id, idea_id, report)
    return report


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


def command_run(args: argparse.Namespace) -> int:
    root = project_root()
    errors = ensure_phase3_inputs(root, args.seed_id, args.idea_id)
    if errors:
        print(
            f"cannot run phase3 for seed {args.seed_id} idea {args.idea_id}; prerequisites failed",
            file=sys.stderr,
        )
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    prompt_path = render_phase3_prompt(root, args.seed_id, args.idea_id)
    model = resolve_model_name(root, args.model)
    effort = resolve_effort_level(root, args.effort, PHASE_KEY)
    command = build_claude_command(root, args.seed_id, args.idea_id, prompt_path, model, effort)

    print("phase3 prompt:", prompt_path)
    print("phase3 command:", " ".join(command))
    if args.dry_run:
        return 0

    before_sessions = list_session_dirs(root, args.seed_id, args.idea_id)
    exit_code = subprocess.run(command, cwd=root, check=False).returncode
    if exit_code != 0:
        return exit_code

    print()
    print("validating phase3 generated task...")
    task_report = validate_phase3(root, args.seed_id, args.idea_id, require_manifest=False)
    task_exit_code = print_report(task_report, as_json=False)
    if task_exit_code != 0:
        return task_exit_code

    claude_session_ref = find_new_session_ref(root, args.seed_id, args.idea_id, before_sessions)
    if claude_session_ref is None:
        print("cannot append manifest: no Claude session directory was found", file=sys.stderr)
        return 1

    append_manifest_event(root, args.seed_id, args.idea_id, claude_session_ref)

    print()
    print("validating phase3 manifest...")
    return print_report(validate_phase3(root, args.seed_id, args.idea_id), as_json=False)


def command_validate(args: argparse.Namespace) -> int:
    return print_report(validate_phase3(project_root(), args.seed_id, args.idea_id), args.json)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run phase3, then validate its output.")
    run.add_argument("seed_id")
    run.add_argument("--idea-id", required=True)
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Render the prompt and print the command without running Claude or validation.",
    )
    run.add_argument("--model", help="Claude model to use. Defaults to model.json default_model when omitted.")
    run.add_argument(
        "--effort",
        choices=EFFORT_LEVELS,
        help="Claude Code effort level for this run. Defaults to model.json phase_efforts.phase3, then default_effort.",
    )
    run.set_defaults(func=command_run)

    validate = subparsers.add_parser("validate", help="Validate phase3 output.")
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
