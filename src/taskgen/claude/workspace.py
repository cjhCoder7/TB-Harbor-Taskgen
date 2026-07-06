#!/usr/bin/env python3
"""Prepare and finalize isolated Claude Code workspaces."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from taskgen.claude.cost import summarize_claude_stream_log, write_cost_summary
from taskgen.common import validate_path_segment


SUPPORTED_PHASES = {
    "seed-brainstorm",
    "skillnet-research",
    "task-generation",
    "task-review",
    "task-repair",
}
CLAUDE_DEFINITIONS_DIR = "cc-definitions"


def require_safe_segment(value: str, label: str) -> None:
    errors = validate_path_segment(value, label)
    if errors:
        raise SystemExit("; ".join(errors))


def require_supported_phase(phase: str) -> None:
    if phase not in SUPPORTED_PHASES:
        known = ", ".join(sorted(SUPPORTED_PHASES))
        raise SystemExit(f"unsupported Claude phase: {phase}; known phases: {known}")


def resolve_project_file(root: Path, path: Path, label: str) -> Path:
    resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise SystemExit(f"{label} must be inside project root: {path}") from None
    return resolved


def safe_name(value: str, label: str) -> str:
    translated = value.translate(str.maketrans({"/": "_", ":": "_", " ": "_"}))
    safe = "".join(ch for ch in translated if ch.isalnum() or ch in "._-")
    if not safe:
        raise SystemExit(f"{label} becomes empty after path sanitization: {value}")
    return safe


def parse_seed_and_idea(subject: str, phase: str) -> tuple[str, str]:
    if "__" not in subject:
        raise SystemExit(f"subject must be <seed_id>__<idea_id> for phase {phase}: {subject}")
    seed_id, idea_id = subject.split("__", 1)
    if not seed_id or not idea_id:
        raise SystemExit(f"subject must contain non-empty seed and idea ids: {subject}")
    require_safe_segment(seed_id, "seed_id")
    require_safe_segment(idea_id, "idea_id")
    return seed_id, idea_id


def copy_required(root: Path, workspace: Path, source_rel_path: str, workspace_rel_path: str) -> None:
    source = root / source_rel_path
    if not source.exists():
        raise SystemExit(f"required workspace input is missing: {source_rel_path}")
    copy_path(source, workspace / workspace_rel_path)


def copy_optional(root: Path, workspace: Path, source_rel_path: str, workspace_rel_path: str) -> None:
    source = root / source_rel_path
    if source.exists():
        copy_path(source, workspace / workspace_rel_path)


def copy_path(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.is_dir():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination)


def merge_skill_packages(source: Path, destination: Path) -> list[str]:
    if not source.is_dir():
        raise SystemExit(f"required generated skill package directory is missing: {source}")

    copied: list[str] = []
    destination.mkdir(parents=True, exist_ok=True)
    for package in sorted(source.iterdir(), key=lambda path: path.name):
        if not package.is_dir():
            continue
        skill_file = package / "SKILL.md"
        if not skill_file.is_file():
            raise SystemExit(f"generated skill package is missing SKILL.md: {package}")
        target = destination / package.name
        if target.exists():
            raise SystemExit(f"generated skill package conflicts with existing workspace skill: {package.name}")
        shutil.copytree(package, target, symlinks=True)
        copied.append(package.name)

    return copied


def ensure_runtime_dirs(claude_config_dir: Path) -> None:
    for rel in (
        "debug",
        "projects/-app",
        "shell-snapshots",
        "statsig",
        "todos",
    ):
        (claude_config_dir / rel).mkdir(parents=True, exist_ok=True)


def phase_input_paths(phase: str, subject: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    require_supported_phase(phase)
    required: list[tuple[str, str]] = []
    optional: list[tuple[str, str]] = []

    if phase == "seed-brainstorm":
        require_safe_segment(subject, "subject")
        required.append((f"seeds/{subject}", f"seed/{subject}"))
    elif phase == "skillnet-research":
        require_safe_segment(subject, "subject")
        required.append(
            (
                f"runs/brainstorm/{subject}/seed_brainstorm.json",
                f"brainstorm/{subject}/seed_brainstorm.json",
            )
        )
    elif phase == "task-generation":
        seed_id, idea_id = parse_seed_and_idea(subject, phase)
        required.extend(
            [
                (f"seeds/{seed_id}", f"seed/{seed_id}"),
                (
                    f"runs/brainstorm/{seed_id}/seed_brainstorm.json",
                    f"brainstorm/{seed_id}/seed_brainstorm.json",
                ),
                (
                    f"runs/skillnet/{seed_id}/{idea_id}/skill_summary.json",
                    f"skillnet/{seed_id}/{idea_id}/skill_summary.json",
                ),
            ]
        )
    elif phase == "task-review":
        seed_id, idea_id = parse_seed_and_idea(subject, phase)
        required.extend(
            [
                (f"generated/working/{seed_id}/{idea_id}", f"task/{seed_id}/{idea_id}"),
                (f"runs/oracle-nop-check/{subject}", f"oracle-nop-check/{subject}"),
            ]
        )
    elif phase == "task-repair":
        seed_id, idea_id = parse_seed_and_idea(subject, phase)
        required.extend(
            [
                (f"generated/working/{seed_id}/{idea_id}", f"task/{seed_id}/{idea_id}"),
                (f"runs/reviews/{subject}", f"review/{subject}"),
            ]
        )
        optional.append((f"runs/oracle-nop-check/{subject}", f"oracle-nop-check/{subject}"))
    return required, optional


def phase_output_paths(phase: str, subject: str) -> list[tuple[str, str]]:
    require_supported_phase(phase)
    if phase == "seed-brainstorm":
        require_safe_segment(subject, "subject")
        return [
            (
                "output/seed_brainstorm.json",
                f"runs/brainstorm/{subject}/seed_brainstorm.json",
            )
        ]
    if phase == "skillnet-research":
        require_safe_segment(subject, "subject")
        return [
            (
                "output/skillnet",
                f"runs/skillnet/{subject}",
            )
        ]
    if phase == "task-generation":
        seed_id, idea_id = parse_seed_and_idea(subject, phase)
        return [("output/task", f"generated/working/{seed_id}/{idea_id}")]
    if phase == "task-review":
        return [("output/review", f"runs/reviews/{subject}")]
    if phase == "task-repair":
        seed_id, idea_id = parse_seed_and_idea(subject, phase)
        return [("output/task", f"generated/working/{seed_id}/{idea_id}")]
    return []


def prepare_workspace(
    project_root: Path,
    phase: str,
    subject: str,
    prompt_file: Path,
    run_id: str,
) -> dict[str, object]:
    root = project_root.resolve()
    prompt = resolve_project_file(root, prompt_file, "prompt file")
    if not prompt.is_file():
        raise SystemExit(f"prompt file does not exist: {prompt_file}")

    safe_phase = safe_name(phase, "phase")
    safe_subject = safe_name(subject, "subject")
    run_dir = root / "runs/claude-sessions" / safe_phase / safe_subject / run_id
    workspace = root / "runs/workspace" / safe_phase / safe_subject / run_id
    claude_config_dir = run_dir / ".claude-runtime"

    workspace.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    ensure_runtime_dirs(claude_config_dir)

    prompt_copy = run_dir / "prompt.md"
    workspace_prompt = workspace / "prompt.md"
    shutil.copy2(prompt, prompt_copy)
    shutil.copy2(prompt, workspace_prompt)

    project_agents = root / CLAUDE_DEFINITIONS_DIR / "agents"
    workspace_agents = workspace / ".claude/agents"
    if project_agents.is_dir():
        copy_path(project_agents, workspace_agents)

    project_skills = root / CLAUDE_DEFINITIONS_DIR / "skills"
    workspace_skills = workspace / ".claude/skills"
    if project_skills.is_dir():
        copy_path(project_skills, workspace_skills)

    generated_skill_packages: list[str] = []
    if phase == "task-generation":
        seed_id, idea_id = parse_seed_and_idea(subject, phase)
        generated_skill_packages = merge_skill_packages(
            root / "runs/skillnet" / seed_id / idea_id / "skills",
            workspace_skills,
        )

    required, optional = phase_input_paths(phase, subject)
    for source_rel_path, workspace_rel_path in required:
        copy_required(root, workspace, source_rel_path, workspace_rel_path)
    for source_rel_path, workspace_rel_path in optional:
        copy_optional(root, workspace, source_rel_path, workspace_rel_path)

    workspace_claude = workspace / ".claude"
    workspace_claude.mkdir(parents=True, exist_ok=True)
    for settings_name in ("settings.json", "settings.local.json"):
        settings_path = workspace_claude / settings_name
        if settings_path.exists():
            settings_path.unlink()

    payload = {
        "phase": phase,
        "subject": subject,
        "run_id": run_id,
        "project_root": str(root),
        "run_dir": str(run_dir),
        "workspace_dir": str(workspace),
        "claude_config_dir": str(claude_config_dir),
        "stream_log": str(run_dir / "claude-code.txt"),
        "cost_path": str(run_dir / "cost.json"),
        "status_path": str(run_dir / "status.json"),
        "prompt_copy": str(prompt_copy),
        "workspace_prompt": str(workspace_prompt),
        "workspace_inputs": [workspace_rel_path for _, workspace_rel_path in required + optional],
        "generated_skill_packages": generated_skill_packages,
        "output_mappings": [
            {"workspace": source_rel_path, "project": project_rel_path}
            for source_rel_path, project_rel_path in phase_output_paths(phase, subject)
        ],
    }
    return payload


def sync_workspace_outputs(
    project_root: Path,
    workspace_dir: Path,
    phase: str,
    subject: str,
) -> list[str]:
    root = project_root.resolve()
    workspace = workspace_dir.resolve()
    synced: list[str] = []

    for workspace_rel_path, project_rel_path in phase_output_paths(phase, subject):
        source = workspace / workspace_rel_path
        if not source.exists():
            continue
        destination = root / project_rel_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        if source.is_dir():
            shutil.copytree(source, destination, symlinks=True)
        else:
            shutil.copy2(source, destination)
        synced.append(project_rel_path)

    return synced


def write_status(
    *,
    status_path: Path,
    phase: str,
    subject: str,
    run_id: str,
    project_root: Path,
    workspace_dir: Path,
    prompt_copy: Path,
    workspace_prompt: Path,
    stream_log: Path,
    cost_path: Path,
    claude_config_dir: Path,
    synced_outputs: list[str],
    exit_code: int,
) -> dict[str, object]:
    cost_summary = summarize_claude_stream_log(stream_log)
    write_cost_summary(cost_summary, cost_path)

    payload = {
        "phase": phase,
        "subject": subject,
        "run_id": run_id,
        "project_root": str(project_root),
        "workspace_dir": str(workspace_dir),
        "exit_code": exit_code,
        "prompt": str(prompt_copy),
        "workspace_prompt": str(workspace_prompt),
        "stream_log": str(stream_log),
        "cost_path": str(cost_path),
        "claude_config_dir": str(claude_config_dir),
        "raw_sessions_dir": str(claude_config_dir / "projects"),
        "synced_outputs": synced_outputs,
        "total_cost_usd": cost_summary.get("total_cost_usd"),
        "cost": cost_summary,
    }

    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return payload


def command_prepare(args: argparse.Namespace) -> int:
    payload = prepare_workspace(
        args.project_root,
        args.phase,
        args.subject,
        args.prompt_file,
        args.run_id,
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def command_sync(args: argparse.Namespace) -> int:
    synced = sync_workspace_outputs(
        args.project_root,
        args.workspace_dir,
        args.phase,
        args.subject,
    )
    print(json.dumps(synced, ensure_ascii=False))
    return 0


def command_status(args: argparse.Namespace) -> int:
    write_status(
        status_path=args.status_path,
        phase=args.phase,
        subject=args.subject,
        run_id=args.run_id,
        project_root=args.project_root,
        workspace_dir=args.workspace_dir,
        prompt_copy=args.prompt_copy,
        workspace_prompt=args.workspace_prompt,
        stream_log=args.stream_log,
        cost_path=args.cost_path,
        claude_config_dir=args.claude_config_dir,
        synced_outputs=json.loads(args.synced_outputs_json),
        exit_code=args.exit_code,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--project-root", type=Path, required=True)
    prepare.add_argument("--phase", required=True)
    prepare.add_argument("--subject", required=True)
    prepare.add_argument("--prompt-file", type=Path, required=True)
    prepare.add_argument("--run-id", required=True)
    prepare.set_defaults(func=command_prepare)

    sync = subparsers.add_parser("sync")
    sync.add_argument("--project-root", type=Path, required=True)
    sync.add_argument("--workspace-dir", type=Path, required=True)
    sync.add_argument("--phase", required=True)
    sync.add_argument("--subject", required=True)
    sync.set_defaults(func=command_sync)

    status = subparsers.add_parser("status")
    status.add_argument("--status-path", type=Path, required=True)
    status.add_argument("--phase", required=True)
    status.add_argument("--subject", required=True)
    status.add_argument("--run-id", required=True)
    status.add_argument("--project-root", type=Path, required=True)
    status.add_argument("--workspace-dir", type=Path, required=True)
    status.add_argument("--prompt-copy", type=Path, required=True)
    status.add_argument("--workspace-prompt", type=Path, required=True)
    status.add_argument("--stream-log", type=Path, required=True)
    status.add_argument("--cost-path", type=Path, required=True)
    status.add_argument("--claude-config-dir", type=Path, required=True)
    status.add_argument("--synced-outputs-json", required=True)
    status.add_argument("--exit-code", type=int, required=True)
    status.set_defaults(func=command_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
