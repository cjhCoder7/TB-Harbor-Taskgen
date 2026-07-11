#!/usr/bin/env python3
"""Phase 2 runner and validator: SkillNet research.

This phase reads one phase1 brainstorm artifact and asks Claude Code to run
SkillNet searches for every idea. The expected result is a seed-level SkillNet
index plus one curated skill package set per idea:

- `runs/skillnet/<seed_id>/skillnet_index.json`
- `runs/skillnet/<seed_id>/<idea_id>/skill_summary.json`
- `runs/skillnet/<seed_id>/<idea_id>/skills/<skill_name>/...`
- `runs/skillnet/<seed_id>/<idea_id>/raw/...`
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
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
    validate_path_segment,
    validate_seed_identifier,
)
from taskgen.config import (
    EFFORT_LEVELS,
    resolve_effort_level,
    resolve_model_name,
)
from taskgen.phases.phase1_seed_brainstorm import (
    BRAINSTORM_FILENAME,
    brainstorm_ref_for,
    validate_brainstorm_data,
)


PHASE_KEY = "phase2"
PHASE_NAME = "skillnet-research"
INDEX_FILENAME = "skillnet_index.json"
STATUS_VALUES = {"ready", "partial", "no_strong_match", "failed"}


def validate_seed_id(seed_id: str) -> list[str]:
    return validate_seed_identifier(seed_id)


def workspace_brainstorm_ref_for(seed_id: str) -> str:
    return f"brainstorm/{seed_id}/{BRAINSTORM_FILENAME}"


def skillnet_root_ref_for(seed_id: str) -> str:
    return f"runs/skillnet/{seed_id}"


def skillnet_index_ref_for(seed_id: str) -> str:
    return f"{skillnet_root_ref_for(seed_id)}/{INDEX_FILENAME}"


def workspace_skillnet_index_ref_for(seed_id: str) -> str:
    return f"skillnet/{seed_id}/{INDEX_FILENAME}"


def workspace_skill_summary_ref_for(seed_id: str, idea_id: str) -> str:
    return f"skillnet/{seed_id}/{idea_id}/skill_summary.json"


def skill_name_prefix_for(idea_id: str) -> str:
    return f"taskgen-{idea_id}-"


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
        expected_outputs=[skillnet_root_ref_for(seed_id)],
        before=before,
    )
    return session_ref_for(root, session_dir) if session_dir is not None else None


def append_manifest_event(root: Path, seed_id: str, claude_session_ref: str) -> None:
    manifest_path = root / "runs/task-manifest.jsonl"
    payload = {
        "event": "skillnet_done",
        "seed_id": seed_id,
        "brainstorm_ref": brainstorm_ref_for(seed_id),
        "skillnet_ref": skillnet_index_ref_for(seed_id),
        "claude_session_ref": claude_session_ref,
        "status": "working",
        "reason": "phase 2 SkillNet research completed",
    }
    append_jsonl_object(manifest_path, payload)


def ensure_phase2_inputs(root: Path, seed_id: str) -> list[str]:
    errors = validate_seed_id(seed_id)
    if errors:
        return errors

    for required in (
        brainstorm_ref_for(seed_id),
        "prompts/skillnet-research.md",
        "cc-definitions/agents/skillnet-researcher.md",
        "cc-definitions/skills/tb-harbor-task-generation/SKILL.md",
        "scripts/run-claude-logged.sh",
    ):
        candidate = root / required
        if not candidate.exists():
            errors.append(f"missing phase2 project file: {candidate}")
    return errors


def render_phase2_prompt(root: Path, seed_id: str) -> Path:
    template_path = root / "prompts/skillnet-research.md"
    output_path = root / "runs/prompts" / seed_id / "skillnet-research.md"
    prompt = template_path.read_text(encoding="utf-8")
    prompt = prompt.replace("{{SEED_ID}}", seed_id)
    prompt = prompt.replace("{{BRAINSTORM_PATH}}", workspace_brainstorm_ref_for(seed_id))
    prompt = prompt.replace("{{OUTPUT_PATH}}", "output/skillnet")
    prompt = prompt.replace("{{SKILLNET_INDEX_REF}}", workspace_skillnet_index_ref_for(seed_id))
    require_no_template_markers(prompt, "phase2 prompt")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(prompt, encoding="utf-8")
    return output_path


def require_int(obj: dict[str, Any], key: str, path: str, report: ValidationReport) -> int | None:
    value = obj.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        report.errors.append(f"{path}.{key} must be an integer")
        return None
    return value


def validate_iso8601_timestamp(value: str, path: str, report: ValidationReport) -> None:
    if "T" not in value:
        report.errors.append(f"{path} must be an ISO-8601 date-time with a timezone")
        return
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        report.errors.append(f"{path} must be a valid ISO-8601 date-time")
        return
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        report.errors.append(f"{path} must include an ISO-8601 timezone offset")


def require_status(obj: dict[str, Any], path: str, report: ValidationReport) -> str | None:
    status = require_string(obj, "status", path, report)
    if status is not None and status not in STATUS_VALUES:
        report.errors.append(f"{path}.status must be one of {sorted(STATUS_VALUES)}, got {status!r}")
        return None
    return status


def validate_skill_count(status: str | None, count: int, path: str, report: ValidationReport) -> None:
    if status == "ready" and not 3 <= count <= 5:
        report.errors.append(f"{path} must contain 3-5 selected skills when status is 'ready'")
    elif status == "partial" and not 1 <= count <= 5:
        report.errors.append(f"{path} must contain 1-5 selected skills when status is 'partial'")
    elif status in {"no_strong_match", "failed"} and count > 5:
        report.errors.append(f"{path} must contain at most 5 selected skills")


def load_brainstorm_ideas(root: Path, seed_id: str, report: ValidationReport) -> dict[str, str]:
    brainstorm_path = root / brainstorm_ref_for(seed_id)
    data = load_json(brainstorm_path, report)
    data = require_object(data, "$.brainstorm", report) if data is not None else None
    if data is None:
        return {}

    validate_brainstorm_data(data, seed_id, report)
    ideas = data.get("ideas")
    if not isinstance(ideas, list):
        return {}

    idea_titles: dict[str, str] = {}
    for index, idea_value in enumerate(ideas):
        if not isinstance(idea_value, dict):
            continue
        idea_id = idea_value.get("idea_id")
        title = idea_value.get("title")
        if isinstance(idea_id, str) and idea_id.strip() and isinstance(title, str) and title.strip():
            idea_id_errors = validate_idea_identifier(idea_id)
            if idea_id_errors:
                report.errors.extend(idea_id_errors)
                continue
            idea_titles[idea_id] = title
    return idea_titles


def validate_index_entry(
    entry: dict[str, Any],
    path: str,
    seed_id: str,
    idea_titles: dict[str, str],
    report: ValidationReport,
) -> tuple[str | None, dict[str, Any]]:
    idea_id = require_string(entry, "idea_id", path, report)
    status = require_status(entry, path, report)
    title = require_string(entry, "title", path, report)
    skill_summary_ref = require_string(entry, "skill_summary_ref", path, report)
    skill_count = require_int(entry, "skill_count", path, report)
    skill_names = require_string_list(entry, "skill_names", path, report)
    require_string_list(entry, "notes", path, report)

    if idea_id is not None:
        report.errors.extend(validate_idea_identifier(idea_id))
        if idea_id not in idea_titles:
            report.errors.append(f"{path}.idea_id is not present in phase1 brainstorm: {idea_id!r}")
        if title is not None and idea_titles.get(idea_id) and title != idea_titles[idea_id]:
            report.errors.append(f"{path}.title must exactly match the phase1 brainstorm title")
        expected_ref = workspace_skill_summary_ref_for(seed_id, idea_id)
        if skill_summary_ref is not None and skill_summary_ref != expected_ref:
            report.errors.append(f"{path}.skill_summary_ref must be {expected_ref!r}")

    if skill_names is not None:
        seen: set[str] = set()
        for index, skill_name in enumerate(skill_names):
            if skill_name in seen:
                report.errors.append(f"{path}.skill_names[{index}] is duplicated: {skill_name!r}")
            seen.add(skill_name)
            validate_skill_name(skill_name, idea_id, f"{path}.skill_names[{index}]", report)

    if skill_count is not None and skill_names is not None:
        if skill_count != len(skill_names):
            report.errors.append(f"{path}.skill_count must equal len(skill_names)")
        validate_skill_count(status, skill_count, f"{path}.skill_count", report)

    return idea_id, {
        "status": status,
        "title": title,
        "skill_names": skill_names if isinstance(skill_names, list) else [],
        "skill_count": skill_count,
    }


def validate_skill_name(
    skill_name: str,
    idea_id: str | None,
    path: str,
    report: ValidationReport,
) -> None:
    errors = validate_path_segment(skill_name, path)
    report.errors.extend(errors)
    if idea_id is not None and not skill_name.startswith(skill_name_prefix_for(idea_id)):
        report.errors.append(
            f"{path} must start with {skill_name_prefix_for(idea_id)!r} to avoid skill name conflicts"
        )


def validate_skill_package_frontmatter(
    skill_file: Path,
    expected_name: str,
    report: ValidationReport,
) -> None:
    report.checked_paths.append(str(skill_file))
    if skill_file.is_symlink():
        report.errors.append(f"skill package SKILL.md must not be a symlink: {skill_file}")
        return
    if not skill_file.is_file():
        report.errors.append(f"missing skill package SKILL.md: {skill_file}")
        return

    try:
        lines = skill_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        report.errors.append(f"cannot read skill package file {skill_file}: {exc}")
        return
    if not lines or lines[0].strip() != "---":
        report.errors.append(f"{skill_file} must start with YAML frontmatter")
        return

    end_index: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break
    if end_index is None:
        report.errors.append(f"{skill_file} must close YAML frontmatter with ---")
        return

    frontmatter: dict[str, str] = {}
    for line in lines[1:end_index]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        frontmatter[key.strip()] = value.strip().strip("'\"")

    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if name != expected_name:
        report.errors.append(f"{skill_file} frontmatter name must be {expected_name!r}")
    if not description:
        report.errors.append(f"{skill_file} frontmatter description must be a non-empty string")
    if len(lines[end_index + 1 :]) < 3:
        report.warnings.append(f"{skill_file} has very little body content")


def validate_selected_skill(
    value: Any,
    path: str,
    root: Path,
    seed_id: str,
    idea_id: str,
    report: ValidationReport,
) -> str | None:
    skill = require_object(value, path, report)
    if skill is None:
        return None

    name = require_string(skill, "name", path, report)
    skill_path = require_string(skill, "path", path, report)
    require_string(skill, "source", path, report)
    require_string(skill, "why_selected", path, report)
    require_string_list(skill, "usable_for", path, report, min_items=1)
    require_string_list(skill, "limits", path, report)

    if name is None:
        return None

    validate_skill_name(name, idea_id, f"{path}.name", report)
    expected_path = f"skills/{name}"
    if skill_path is not None and skill_path != expected_path:
        report.errors.append(f"{path}.path must be {expected_path!r}")

    package_dir = root / skillnet_root_ref_for(seed_id) / idea_id / "skills" / name
    report.checked_paths.append(str(package_dir))
    if not package_dir.is_dir():
        report.errors.append(f"missing selected skill package directory: {package_dir}")
    elif package_dir.is_symlink():
        report.errors.append(f"selected skill package must not be a symlink: {package_dir}")
    else:
        for package_path in sorted(package_dir.rglob("*")):
            if package_path.is_symlink():
                report.errors.append(
                    f"selected skill package must not contain symlinks: {package_path}"
                )
        validate_skill_package_frontmatter(package_dir / "SKILL.md", name, report)
    return name


def validate_skill_summary(
    root: Path,
    seed_id: str,
    idea_id: str,
    expected_title: str,
    report: ValidationReport,
) -> dict[str, Any] | None:
    summary_path = root / skillnet_root_ref_for(seed_id) / idea_id / "skill_summary.json"
    data = load_json(summary_path, report)
    summary = require_object(data, f"$.summaries.{idea_id}", report) if data is not None else None
    if summary is None:
        return None

    actual_seed_id = require_string(summary, "seed_id", "$.summary", report)
    if actual_seed_id is not None and actual_seed_id != seed_id:
        report.errors.append(f"$.summary.seed_id must equal {seed_id!r}")
    actual_idea_id = require_string(summary, "idea_id", "$.summary", report)
    if actual_idea_id is not None and actual_idea_id != idea_id:
        report.errors.append(f"$.summary.idea_id must equal {idea_id!r}")
    title = require_string(summary, "title", "$.summary", report)
    if title is not None and title != expected_title:
        report.errors.append(f"$.summary.title must exactly match the phase1 brainstorm title for {idea_id!r}")
    status = require_status(summary, "$.summary", report)

    selected = summary.get("selected_skills")
    if not isinstance(selected, list):
        report.errors.append("$.summary.selected_skills must be a list")
        selected = []
    validate_skill_count(status, len(selected), "$.summary.selected_skills", report)

    selected_names: list[str] = []
    seen: set[str] = set()
    for index, selected_value in enumerate(selected):
        name = validate_selected_skill(
            selected_value,
            f"$.summary.selected_skills[{index}]",
            root,
            seed_id,
            idea_id,
            report,
        )
        if name is None:
            continue
        if name in seen:
            report.errors.append(f"$.summary.selected_skills[{index}].name is duplicated: {name!r}")
        seen.add(name)
        selected_names.append(name)

    for key in (
        "tooling_notes",
        "environment_notes",
        "verifier_notes",
        "implementation_risks",
    ):
        require_string_list(summary, key, "$.summary", report, min_items=1)
    require_string(summary, "recommended_direction", "$.summary", report)
    validate_difficulty_hardening(summary.get("difficulty_hardening"), report)

    raw_dir = root / skillnet_root_ref_for(seed_id) / idea_id / "raw"
    report.checked_paths.append(str(raw_dir))
    if not raw_dir.is_dir():
        report.errors.append(f"missing raw SkillNet evidence directory: {raw_dir}")
    elif not any(raw_dir.iterdir()):
        report.warnings.append(f"raw SkillNet evidence directory is empty: {raw_dir}")

    skills_dir = root / skillnet_root_ref_for(seed_id) / idea_id / "skills"
    report.checked_paths.append(str(skills_dir))
    if not skills_dir.is_dir():
        report.errors.append(f"missing selected skill package root directory: {skills_dir}")

    return {
        "status": status,
        "title": title,
        "skill_names": selected_names,
        "skill_count": len(selected_names),
    }


def validate_difficulty_hardening(value: Any, report: ValidationReport) -> None:
    path = "$.summary.difficulty_hardening"
    hardening = require_object(value, path, report)
    if hardening is None:
        return

    require_string(hardening, "minimum_complexity_contract", path, report)
    require_string_list(hardening, "too_easy_risks", path, report, min_items=1)
    require_string_list(hardening, "recommended_hardening", path, report, min_items=1)
    require_string_list(hardening, "do_not_simplify", path, report, min_items=1)


def validate_skillnet_index(
    root: Path,
    seed_id: str,
    idea_titles: dict[str, str],
    report: ValidationReport,
) -> dict[str, dict[str, Any]]:
    index_path = root / skillnet_index_ref_for(seed_id)
    data = load_json(index_path, report)
    index = require_object(data, "$.index", report) if data is not None else None
    if index is None:
        return {}

    actual_seed_id = require_string(index, "seed_id", "$.index", report)
    if actual_seed_id is not None and actual_seed_id != seed_id:
        report.errors.append(f"$.index.seed_id must equal {seed_id!r}")

    brainstorm_ref = require_string(index, "brainstorm_ref", "$.index", report)
    expected_brainstorm_ref = workspace_brainstorm_ref_for(seed_id)
    if brainstorm_ref is not None and brainstorm_ref != expected_brainstorm_ref:
        report.errors.append(f"$.index.brainstorm_ref must be {expected_brainstorm_ref!r}")

    generated_at = require_string(index, "generated_at", "$.index", report)
    if generated_at is not None:
        validate_iso8601_timestamp(generated_at, "$.index.generated_at", report)
    ideas = index.get("ideas")
    if not isinstance(ideas, list):
        report.errors.append("$.index.ideas must be a list")
        return {}

    entries: dict[str, dict[str, Any]] = {}
    for item_index, entry_value in enumerate(ideas):
        path = f"$.index.ideas[{item_index}]"
        entry = require_object(entry_value, path, report)
        if entry is None:
            continue
        idea_id, details = validate_index_entry(entry, path, seed_id, idea_titles, report)
        if idea_id is None:
            continue
        if idea_id in entries:
            report.errors.append(f"{path}.idea_id is duplicated: {idea_id!r}")
        entries[idea_id] = details

    expected_ids = set(idea_titles)
    actual_ids = set(entries)
    for missing in sorted(expected_ids - actual_ids):
        report.errors.append(f"$.index.ideas is missing brainstorm idea: {missing!r}")
    for extra in sorted(actual_ids - expected_ids):
        report.errors.append(f"$.index.ideas contains non-brainstorm idea: {extra!r}")
    return entries


def validate_manifest_event(root: Path, seed_id: str, skillnet_ref: str, report: ValidationReport) -> None:
    manifest_path = root / "runs/task-manifest.jsonl"
    report.checked_paths.append(str(manifest_path))
    if not manifest_path.exists():
        report.errors.append(f"missing manifest: {manifest_path}")
        return

    found = False
    candidate_errors: list[str] = []
    for line_number, event in read_jsonl_objects(manifest_path, report):
        if not (
            event.get("event") == "skillnet_done"
            and event.get("seed_id") == seed_id
            and event.get("skillnet_ref") == skillnet_ref
        ):
            continue

        line_errors = validate_manifest_candidate(root, event, report)
        if line_errors:
            candidate_errors.append(f"{manifest_path}:{line_number}: " + "; ".join(line_errors))
        else:
            found = True

    if not found:
        report.errors.append(
            "manifest has no matching skillnet_done event for "
            f"seed_id={seed_id!r}, skillnet_ref={skillnet_ref!r}, "
            "status='working', and a valid claude_session_ref"
        )
        report.errors.extend(candidate_errors)


def validate_manifest_candidate(root: Path, event: dict[str, Any], report: ValidationReport) -> list[str]:
    errors: list[str] = []
    if event.get("status") != "working":
        errors.append("status must be 'working'")
    if not isinstance(event.get("reason"), str) or not event["reason"].strip():
        errors.append("reason must be a non-empty string")
    if event.get("brainstorm_ref") != brainstorm_ref_for(str(event.get("seed_id"))):
        errors.append("brainstorm_ref does not match seed_id")

    seed_id = str(event.get("seed_id"))
    errors.extend(
        validate_claude_session_reference(
            root,
            event.get("claude_session_ref"),
            expected_phase=PHASE_NAME,
            expected_subject=seed_id,
            expected_outputs=[skillnet_root_ref_for(seed_id)],
            report=report,
        )
    )
    return errors


def validate_phase2(root: Path, seed_id: str, *, require_manifest: bool = True) -> ValidationReport:
    report = ValidationReport(phase=PHASE_KEY, seed_id=seed_id)

    seed_errors = validate_seed_id(seed_id)
    if seed_errors:
        report.errors.extend(seed_errors)
        return report

    idea_titles = load_brainstorm_ideas(root, seed_id, report)
    index_entries = validate_skillnet_index(root, seed_id, idea_titles, report)

    for idea_id, title in sorted(idea_titles.items()):
        summary = validate_skill_summary(root, seed_id, idea_id, title, report)
        index_entry = index_entries.get(idea_id)
        if summary is None or index_entry is None:
            continue
        if summary.get("status") != index_entry.get("status"):
            report.errors.append(f"index status differs from summary status for idea {idea_id!r}")
        if summary.get("skill_count") != index_entry.get("skill_count"):
            report.errors.append(f"index skill_count differs from summary selected skill count for idea {idea_id!r}")
        if summary.get("skill_names") != index_entry.get("skill_names"):
            report.errors.append(f"index skill_names differs from summary selected skill names for idea {idea_id!r}")

    if require_manifest:
        validate_manifest_event(root, seed_id, skillnet_index_ref_for(seed_id), report)
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
    errors = ensure_phase2_inputs(root, args.seed_id)
    if errors:
        print(f"cannot run phase2 for seed {args.seed_id}; prerequisites failed", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    prompt_path = render_phase2_prompt(root, args.seed_id)
    model = resolve_model_name(root, args.model)
    effort = resolve_effort_level(root, args.effort, PHASE_KEY)
    command = build_claude_command(root, args.seed_id, prompt_path, model, effort)

    print("phase2 prompt:", prompt_path)
    print("phase2 command:", " ".join(command))
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
        expected_outputs=[skillnet_root_ref_for(args.seed_id)],
        before=before_sessions,
    )
    if session_dir is None:
        print("cannot append manifest: Claude session validation failed", file=sys.stderr)
        for error in session_errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print()
    print("validating phase2 SkillNet output...")
    skillnet_report = validate_phase2(root, args.seed_id, require_manifest=False)
    skillnet_exit_code = print_report(skillnet_report, as_json=False)
    if skillnet_exit_code != 0:
        return skillnet_exit_code

    append_manifest_event(root, args.seed_id, session_ref_for(root, session_dir))

    print()
    print("validating phase2 manifest...")
    return print_report(validate_phase2(root, args.seed_id), as_json=False)


def command_run(args: argparse.Namespace) -> int:
    root = project_root()
    with phase_subject_lock(root, PHASE_NAME, args.seed_id):
        return _command_run_locked(args)


def command_validate(args: argparse.Namespace) -> int:
    return print_report(validate_phase2(project_root(), args.seed_id), args.json)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run phase2, then validate its output.")
    run.add_argument("seed_id")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Render the prompt and print the command without running Claude or validation.",
    )
    run.add_argument("--model", help="Claude model to use. Defaults to model.json default_model when omitted.")
    run.add_argument(
        "--effort",
        choices=EFFORT_LEVELS,
        help="Claude Code effort level for this run. Defaults to model.json phase_efforts.phase2, then default_effort.",
    )
    run.set_defaults(func=command_run)

    validate = subparsers.add_parser("validate", help="Validate phase2 output.")
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
