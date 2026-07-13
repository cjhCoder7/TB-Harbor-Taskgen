#!/usr/bin/env python3
"""Prepare and finalize isolated Claude Code workspaces."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import uuid
from pathlib import Path
from typing import Any

from taskgen.claude.cost import summarize_claude_stream_log, write_cost_summary
from taskgen.common import (
    directory_tree_sha256,
    fsync_parent_directory,
    fsync_path_tree,
    phase_subject_lock,
    validate_idea_identifier,
    validate_path_segment,
    validate_seed_identifier,
)


SUPPORTED_PHASES = {
    "seed-brainstorm",
    "skillnet-research",
    "task-generation",
    "task-review",
    "task-repair",
}
CLAUDE_DEFINITIONS_DIR = "cc-definitions"
WORKTREE_GUARD_FILENAME = "taskgen-worktree-guard.py"
WORKTREE_GUARD_SETTINGS_FILENAME = "settings.json"


class WorkspaceOutputError(RuntimeError):
    """A declared workspace output could not be safely published."""


class MissingWorkspaceOutputsError(WorkspaceOutputError):
    def __init__(self, missing_outputs: list[str]) -> None:
        self.missing_outputs = missing_outputs
        super().__init__(f"missing declared workspace output(s): {', '.join(missing_outputs)}")


def path_lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def require_contained_path(base: Path, candidate: Path, label: str) -> Path:
    """Resolve a path and reject any symlink chain that leaves ``base``."""
    try:
        resolved_base = base.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=False)
        resolved_candidate.relative_to(resolved_base)
    except (OSError, ValueError, RuntimeError) as exc:
        raise WorkspaceOutputError(f"{label} must stay inside {base}: {candidate}") from exc
    return resolved_candidate


def unsafe_copy_source_reason(source: Path) -> str | None:
    """Return why a source tree cannot be copied, rejecting links/special files."""
    try:
        metadata = source.lstat()
    except OSError as exc:
        return f"cannot inspect {source}: {exc}"
    if stat.S_ISLNK(metadata.st_mode):
        return f"symbolic links are not allowed: {source}"
    if stat.S_ISREG(metadata.st_mode):
        return None
    if not stat.S_ISDIR(metadata.st_mode):
        return f"only regular files and directories may be copied: {source}"

    pending = [source]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            return f"cannot inspect {directory}: {exc}"
        for entry in entries:
            try:
                entry_metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                return f"cannot inspect {entry.path}: {exc}"
            entry_path = Path(entry.path)
            if stat.S_ISLNK(entry_metadata.st_mode):
                return f"symbolic links are not allowed: {entry_path}"
            if stat.S_ISDIR(entry_metadata.st_mode):
                pending.append(entry_path)
            elif not stat.S_ISREG(entry_metadata.st_mode):
                return f"only regular files and directories may be copied: {entry_path}"
    return None


def remove_path(path: Path) -> None:
    if not path_lexists(path):
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def copy_new_path(source: Path, destination: Path) -> None:
    if path_lexists(destination):
        raise FileExistsError(f"copy destination already exists: {destination}")
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=False)
    else:
        shutil.copy2(source, destination)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_parent_directory(path)
    finally:
        if path_lexists(temporary):
            try:
                temporary.unlink()
            except OSError:
                pass


def install_worktree_guard(workspace: Path) -> tuple[Path, Path]:
    """Install a per-run Claude hook that keeps subagents in ``workspace``."""

    source = Path(__file__).with_name("worktree_guard.py")
    unsafe_reason = unsafe_copy_source_reason(source)
    if unsafe_reason:
        raise SystemExit(f"cannot install Claude worktree guard: {unsafe_reason}")

    workspace_claude = workspace / ".claude"
    hook_path = workspace_claude / "hooks" / WORKTREE_GUARD_FILENAME
    copy_path(source, hook_path)
    hook_path.chmod(0o444)

    hook_handler = {
        "type": "command",
        "command": sys.executable,
        "args": [str(hook_path)],
        "timeout": 5,
    }
    settings_path = workspace_claude / WORKTREE_GUARD_SETTINGS_FILENAME
    write_json_atomic(
        settings_path,
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Agent|Task|EnterWorktree",
                        "hooks": [hook_handler],
                    }
                ],
                "WorktreeCreate": [{"hooks": [hook_handler]}],
            }
        },
    )
    settings_path.chmod(0o600)
    return settings_path, hook_path


def require_safe_segment(value: str, label: str) -> None:
    errors = validate_path_segment(value, label)
    if errors:
        raise SystemExit("; ".join(errors))


def require_seed_subject(value: str) -> None:
    errors = validate_seed_identifier(value)
    if errors:
        raise SystemExit("; ".join(errors))


def require_safe_run_id(value: str) -> None:
    require_safe_segment(value, "run_id")
    if len(value) > 128:
        raise SystemExit("run_id must be at most 128 characters")


def require_supported_phase(phase: str) -> None:
    if phase not in SUPPORTED_PHASES:
        known = ", ".join(sorted(SUPPORTED_PHASES))
        raise SystemExit(f"unsupported Claude phase: {phase}; known phases: {known}")


def resolve_project_file(root: Path, path: Path, label: str) -> Path:
    try:
        resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
    except (OSError, RuntimeError) as exc:
        raise SystemExit(f"cannot resolve {label} {path}: {exc}") from None
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
    if subject.count("__") != 1:
        raise SystemExit(f"subject must be <seed_id>__<idea_id> for phase {phase}: {subject}")
    seed_id, idea_id = subject.split("__", 1)
    if not seed_id or not idea_id:
        raise SystemExit(f"subject must contain non-empty seed and idea ids: {subject}")
    errors = [*validate_seed_identifier(seed_id), *validate_idea_identifier(idea_id)]
    if errors:
        raise SystemExit("; ".join(errors))
    return seed_id, idea_id


def copy_required(root: Path, workspace: Path, source_rel_path: str, workspace_rel_path: str) -> None:
    source = root / source_rel_path
    if not path_lexists(source):
        raise SystemExit(f"required workspace input is missing: {source_rel_path}")
    try:
        resolved_source = require_contained_path(root, source, "workspace input")
    except WorkspaceOutputError as exc:
        raise SystemExit(str(exc)) from None
    copy_path(resolved_source, workspace / workspace_rel_path)


def copy_optional(root: Path, workspace: Path, source_rel_path: str, workspace_rel_path: str) -> None:
    source = root / source_rel_path
    if path_lexists(source):
        try:
            resolved_source = require_contained_path(root, source, "optional workspace input")
        except WorkspaceOutputError as exc:
            raise SystemExit(str(exc)) from None
        copy_path(resolved_source, workspace / workspace_rel_path)


def copy_path(source: Path, destination: Path) -> None:
    unsafe_reason = unsafe_copy_source_reason(source)
    if unsafe_reason:
        raise SystemExit(unsafe_reason)
    destination.parent.mkdir(parents=True, exist_ok=True)
    remove_path(destination)
    copy_new_path(source, destination)


def merge_skill_packages(source: Path, destination: Path) -> list[str]:
    if not source.is_dir():
        raise SystemExit(f"required generated skill package directory is missing: {source}")

    copied: list[str] = []
    destination.mkdir(parents=True, exist_ok=True)
    for package in sorted(source.iterdir(), key=lambda path: path.name):
        if package.is_symlink():
            raise SystemExit(f"generated skill package must not be a symbolic link: {package}")
        if not package.is_dir():
            continue
        skill_file = package / "SKILL.md"
        if not skill_file.is_file():
            raise SystemExit(f"generated skill package is missing SKILL.md: {package}")
        target = destination / package.name
        if target.exists():
            raise SystemExit(f"generated skill package conflicts with existing workspace skill: {package.name}")
        unsafe_reason = unsafe_copy_source_reason(package)
        if unsafe_reason:
            raise SystemExit(unsafe_reason)
        shutil.copytree(package, target, symlinks=False)
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
        require_seed_subject(subject)
        required.append((f"seeds/{subject}", f"seed/{subject}"))
    elif phase == "skillnet-research":
        require_seed_subject(subject)
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
        require_seed_subject(subject)
        return [
            (
                "output/seed_brainstorm.json",
                f"runs/brainstorm/{subject}/seed_brainstorm.json",
            )
        ]
    if phase == "skillnet-research":
        require_seed_subject(subject)
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
        seed_id, idea_id = parse_seed_and_idea(subject, phase)
        return [("output/review", f"runs/reviews/{seed_id}__{idea_id}")]
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

    required, optional = phase_input_paths(phase, subject)
    output_paths = phase_output_paths(phase, subject)
    require_safe_run_id(run_id)

    run_dir = root / "runs/claude-sessions" / phase / subject / run_id
    workspace = root / "runs/workspace" / phase / subject / run_id
    claude_config_dir = run_dir / ".claude-runtime"

    if path_lexists(run_dir) or path_lexists(workspace):
        raise SystemExit(
            f"Claude run_id already exists for phase/subject and will not be reused: {run_id}"
        )
    try:
        require_contained_path(root, run_dir, "Claude session directory")
        require_contained_path(root, workspace, "Claude workspace directory")
    except WorkspaceOutputError as exc:
        raise SystemExit(str(exc)) from None
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    workspace.parent.mkdir(parents=True, exist_ok=True)

    run_dir.mkdir()
    try:
        workspace.mkdir()
    except BaseException:
        remove_path(run_dir)
        raise
    ensure_runtime_dirs(claude_config_dir)

    prompt_copy = run_dir / "prompt.md"
    workspace_prompt = workspace / "prompt.md"
    shutil.copy2(prompt, prompt_copy)
    shutil.copy2(prompt, workspace_prompt)

    project_agents = root / CLAUDE_DEFINITIONS_DIR / "agents"
    workspace_agents = workspace / ".claude/agents"
    if project_agents.is_dir():
        try:
            resolved_agents = require_contained_path(root, project_agents, "Claude agent definitions")
        except WorkspaceOutputError as exc:
            raise SystemExit(str(exc)) from None
        copy_path(resolved_agents, workspace_agents)

    project_skills = root / CLAUDE_DEFINITIONS_DIR / "skills"
    workspace_skills = workspace / ".claude/skills"
    if project_skills.is_dir():
        try:
            resolved_skills = require_contained_path(root, project_skills, "Claude skill definitions")
        except WorkspaceOutputError as exc:
            raise SystemExit(str(exc)) from None
        copy_path(resolved_skills, workspace_skills)

    generated_skill_packages: list[str] = []
    if phase == "task-generation":
        seed_id, idea_id = parse_seed_and_idea(subject, phase)
        generated_skills_source = root / "runs/skillnet" / seed_id / idea_id / "skills"
        try:
            generated_skills_source = require_contained_path(
                root,
                generated_skills_source,
                "generated skill packages",
            )
        except WorkspaceOutputError as exc:
            raise SystemExit(str(exc)) from None
        generated_skill_packages = merge_skill_packages(
            generated_skills_source,
            workspace_skills,
        )

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
    claude_settings_path, worktree_guard_path = install_worktree_guard(workspace)

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
        "claude_settings_path": str(claude_settings_path),
        "worktree_guard_path": str(worktree_guard_path),
        "workspace_inputs": [workspace_rel_path for _, workspace_rel_path in required + optional],
        "generated_skill_packages": generated_skill_packages,
        "output_mappings": [
            {"workspace": source_rel_path, "project": project_rel_path}
            for source_rel_path, project_rel_path in output_paths
        ],
    }
    return payload


def output_sync_journal_path(root: Path, phase: str, subject: str) -> Path:
    digest = hashlib.sha256(f"{phase}\0{subject}".encode("utf-8")).hexdigest()[:24]
    return root / "runs/output-sync-transactions" / f"{digest}.json"


def output_path_sha256(path: Path) -> str:
    metadata = path.lstat()
    if stat.S_ISDIR(metadata.st_mode):
        return directory_tree_sha256(path)
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"cannot hash non-regular output: {path}")
    digest = hashlib.sha256()
    digest.update(f"F\0{stat.S_IMODE(metadata.st_mode):o}\0".encode("utf-8"))
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_output_directories(records: list[dict[str, Any]]) -> None:
    for destination in {Path(record["destination"]) for record in records}:
        fsync_parent_directory(destination)


def recover_interrupted_output_sync(
    root: Path,
    phase: str,
    subject: str,
    destinations: list[dict[str, Any]],
) -> None:
    """Recover the previous sync transaction for this exact phase/subject."""
    journal_path = output_sync_journal_path(root, phase, subject)
    require_contained_path(root, journal_path, "output sync transaction journal")
    if not path_lexists(journal_path):
        return
    if journal_path.is_symlink() or not journal_path.is_file():
        raise WorkspaceOutputError(f"output sync journal must be a regular file: {journal_path}")
    try:
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceOutputError(f"cannot read output sync journal {journal_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise WorkspaceOutputError(f"output sync journal must contain an object: {journal_path}")
    if payload.get("phase") != phase or payload.get("subject") != subject:
        raise WorkspaceOutputError("output sync journal phase/subject does not match this run")
    state = payload.get("state")
    if state not in {"preparing", "staged", "committed"}:
        raise WorkspaceOutputError(f"output sync journal has invalid state: {state!r}")
    token = payload.get("token")
    if not isinstance(token, str) or "-" not in token:
        raise WorkspaceOutputError("output sync journal has an invalid token")
    pid_text, nonce = token.split("-", 1)
    if not pid_text.isdigit() or len(nonce) != 32 or any(ch not in "0123456789abcdef" for ch in nonce):
        raise WorkspaceOutputError("output sync journal has an invalid token")
    journal_records = payload.get("records")
    if not isinstance(journal_records, list) or len(journal_records) != len(destinations):
        raise WorkspaceOutputError("output sync journal mapping count does not match this phase")

    recovered_records: list[dict[str, Any]] = []
    for index, (journal_record, expected) in enumerate(zip(journal_records, destinations)):
        if not isinstance(journal_record, dict):
            raise WorkspaceOutputError("output sync journal record must be an object")
        destination = Path(str(journal_record.get("destination", "")))
        expected_destination = Path(expected["destination"])
        if (
            journal_record.get("project_rel_path") != expected["project_rel_path"]
            or Path(os.path.abspath(destination)) != Path(os.path.abspath(expected_destination))
        ):
            raise WorkspaceOutputError("output sync journal destination does not match this phase")
        stage = Path(str(journal_record.get("stage", "")))
        backup = Path(str(journal_record.get("backup", "")))
        if (
            stage.parent != expected_destination.parent
            or stage.name != f".taskgen-output-stage-{token}-{index}"
            or backup.parent != expected_destination.parent
            or backup.name != f".taskgen-output-backup-{token}-{index}"
        ):
            raise WorkspaceOutputError("output sync journal contains unsafe temporary paths")
        if not isinstance(journal_record.get("destination_existed"), bool):
            raise WorkspaceOutputError("output sync journal has an invalid destination flag")
        require_contained_path(root, stage, "output sync recovery stage")
        require_contained_path(root, backup, "output sync recovery backup")
        for temporary in (stage, backup):
            if path_lexists(temporary) and temporary.is_symlink():
                raise WorkspaceOutputError(
                    f"output sync recovery temporary path must not be a symlink: {temporary}"
                )
        recovered_records.append(
            {
                "destination": expected_destination,
                "stage": stage,
                "backup": backup,
                "destination_existed": journal_record["destination_existed"],
                "output_sha256": journal_record.get("output_sha256"),
            }
        )

    errors: list[str] = []
    recover_as_committed = state == "committed"
    if recover_as_committed:
        for record in recovered_records:
            destination = record["destination"]
            expected_digest = record["output_sha256"]
            destination_valid = (
                isinstance(expected_digest, str)
                and len(expected_digest) == 64
                and all(character in "0123456789abcdef" for character in expected_digest)
                and path_lexists(destination)
                and not destination.is_symlink()
            )
            if destination_valid:
                try:
                    destination_valid = output_path_sha256(destination) == expected_digest
                except OSError:
                    destination_valid = False
            if not destination_valid:
                recover_as_committed = False
        if not recover_as_committed:
            for record in recovered_records:
                if record["destination_existed"] and not path_lexists(record["backup"]):
                    errors.append(
                        "cannot atomically roll back invalid committed outputs because "
                        f"a backup is unavailable: {record['destination']}"
                    )
        if recover_as_committed and not errors:
            for record in recovered_records:
                for temporary in (record["backup"], record["stage"]):
                    try:
                        remove_path(temporary)
                    except OSError as exc:
                        errors.append(str(exc))
    if not recover_as_committed and not errors:
        for record in reversed(recovered_records):
            destination = record["destination"]
            backup = record["backup"]
            try:
                if record["destination_existed"]:
                    if path_lexists(backup):
                        remove_path(destination)
                        os.replace(backup, destination)
                    elif not path_lexists(destination):
                        raise OSError(f"cannot restore missing output destination: {destination}")
                else:
                    remove_path(destination)
                    remove_path(backup)
                remove_path(record["stage"])
            except OSError as exc:
                errors.append(str(exc))

    if errors:
        raise WorkspaceOutputError(
            "output sync recovery was incomplete: " + "; ".join(errors)
        )
    try:
        fsync_output_directories(recovered_records)
        journal_path.unlink()
        fsync_parent_directory(journal_path)
    except OSError as exc:
        raise WorkspaceOutputError(
            f"cannot persist recovered output sync transaction: {exc}"
        ) from exc


def sync_workspace_outputs(
    project_root: Path,
    workspace_dir: Path,
    phase: str,
    subject: str,
) -> list[str]:
    root = project_root.resolve()
    if not root.is_dir():
        raise WorkspaceOutputError(f"project root does not exist or is not a directory: {root}")
    try:
        workspace = workspace_dir.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceOutputError(f"workspace does not exist: {workspace_dir}") from exc
    require_contained_path(root, workspace, "workspace")
    if workspace == root or not workspace.is_dir():
        raise WorkspaceOutputError(f"workspace is not a directory: {workspace}")

    mappings = phase_output_paths(phase, subject)
    destination_records: list[dict[str, Any]] = []
    for workspace_rel_path, project_rel_path in mappings:
        destination_candidate = root / project_rel_path
        require_contained_path(root, destination_candidate, "project output destination")
        destination_candidate.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = require_contained_path(
            root,
            destination_candidate.parent,
            "project output destination parent",
        )
        destination = resolved_parent / destination_candidate.name
        if path_lexists(destination):
            require_contained_path(root, destination, "existing project output destination")
            if destination.is_symlink():
                raise WorkspaceOutputError(
                    f"project output destination must not be a symbolic link: {destination}"
                )
        destination_records.append(
            {
                "workspace_rel_path": workspace_rel_path,
                "project_rel_path": project_rel_path,
                "destination": destination,
            }
        )

    recover_interrupted_output_sync(root, phase, subject, destination_records)

    missing_outputs: list[str] = []
    records: list[dict[str, Any]] = []
    token = f"{os.getpid()}-{uuid.uuid4().hex}"
    for index, destination_record in enumerate(destination_records):
        workspace_rel_path = destination_record["workspace_rel_path"]
        project_rel_path = destination_record["project_rel_path"]
        destination = destination_record["destination"]
        source_candidate = workspace / workspace_rel_path
        if not path_lexists(source_candidate):
            missing_outputs.append(workspace_rel_path)
            continue
        source = require_contained_path(workspace, source_candidate, "workspace output source")
        unsafe_reason = unsafe_copy_source_reason(source_candidate)
        if unsafe_reason:
            raise WorkspaceOutputError(unsafe_reason)

        stage = destination.parent / f".taskgen-output-stage-{token}-{index}"
        backup = destination.parent / f".taskgen-output-backup-{token}-{index}"
        if path_lexists(stage) or path_lexists(backup):
            raise WorkspaceOutputError(f"temporary output path unexpectedly exists near {destination}")
        records.append(
            {
                "source": source,
                "destination": destination,
                "stage": stage,
                "backup": backup,
                "project_rel_path": project_rel_path,
                "destination_existed": path_lexists(destination),
                "backed_up": False,
                "installed": False,
            }
        )

    if missing_outputs:
        raise MissingWorkspaceOutputsError(missing_outputs)

    if not records:
        return []

    journal_path = output_sync_journal_path(root, phase, subject)
    require_contained_path(root, journal_path, "output sync transaction journal")
    if journal_path.is_symlink():
        raise WorkspaceOutputError(f"output sync journal must not be a symlink: {journal_path}")
    journal_payload: dict[str, Any] = {
        "phase": phase,
        "subject": subject,
        "state": "preparing",
        "token": token,
        "records": [
            {
                "project_rel_path": record["project_rel_path"],
                "destination": str(record["destination"]),
                "stage": str(record["stage"]),
                "backup": str(record["backup"]),
                "destination_existed": record["destination_existed"],
            }
            for record in records
        ],
    }
    write_json_atomic(journal_path, journal_payload)

    operation = "stage"
    committed = False
    try:
        for record in records:
            copy_new_path(record["source"], record["stage"])
            fsync_path_tree(record["stage"])
        for journal_record, record in zip(journal_payload["records"], records):
            journal_record["output_sha256"] = output_path_sha256(record["stage"])
        journal_payload["state"] = "staged"
        write_json_atomic(journal_path, journal_payload)

        operation = "publish"
        for record in records:
            destination = record["destination"]
            backup = record["backup"]
            if path_lexists(destination):
                os.replace(destination, backup)
                record["backed_up"] = True
            os.replace(record["stage"], destination)
            record["installed"] = True
        for destination_path in {Path(record["destination"]) for record in records}:
            fsync_parent_directory(destination_path)
        journal_payload["state"] = "committed"
        write_json_atomic(journal_path, journal_payload)
        committed = True
    except BaseException as exc:
        if committed:
            raise
        rollback_errors: list[str] = []
        for record in reversed(records):
            try:
                if record["destination_existed"]:
                    if path_lexists(record["backup"]):
                        remove_path(record["destination"])
                        os.replace(record["backup"], record["destination"])
                    elif not path_lexists(record["destination"]):
                        raise OSError(
                            f"cannot restore missing output destination: {record['destination']}"
                        )
                else:
                    remove_path(record["destination"])
                    remove_path(record["backup"])
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        for record in records:
            try:
                remove_path(record["stage"])
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if not rollback_errors:
            try:
                fsync_output_directories(records)
                journal_path.unlink(missing_ok=True)
                fsync_parent_directory(journal_path)
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if not isinstance(exc, Exception):
            raise
        detail = f"failed to {operation} workspace outputs: {exc}"
        if rollback_errors:
            detail += f"; rollback error(s): {'; '.join(rollback_errors)}"
        raise WorkspaceOutputError(detail) from exc

    cleanup_failed = False
    for record in records:
        for temporary in (record["backup"], record["stage"]):
            try:
                remove_path(temporary)
            except OSError as exc:
                cleanup_failed = True
                print(
                    f"warning: cannot remove published-output temporary path {temporary}: {exc}",
                    file=sys.stderr,
                )
    if not cleanup_failed:
        try:
            fsync_output_directories(records)
            journal_path.unlink(missing_ok=True)
            fsync_parent_directory(journal_path)
        except OSError as exc:
            print(f"warning: cannot remove output sync journal {journal_path}: {exc}", file=sys.stderr)
    return [record["project_rel_path"] for record in records]


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
    claude_settings_path: Path,
    worktree_guard_path: Path,
    synced_outputs: list[str],
    exit_code: int,
    timed_out: bool = False,
    timeout_sec: float | None = None,
    missing_outputs: list[str] | None = None,
    output_sync_error: str | None = None,
) -> dict[str, object]:
    require_safe_run_id(run_id)
    payload: dict[str, Any] = {
        "phase": phase,
        "subject": subject,
        "run_id": run_id,
        "project_root": str(project_root),
        "workspace_dir": str(workspace_dir),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "timeout_sec": timeout_sec,
        "prompt": str(prompt_copy),
        "workspace_prompt": str(workspace_prompt),
        "stream_log": str(stream_log),
        "cost_path": str(cost_path),
        "claude_config_dir": str(claude_config_dir),
        "claude_settings_path": str(claude_settings_path),
        "worktree_guard_path": str(worktree_guard_path),
        "raw_sessions_dir": str(claude_config_dir / "projects"),
        "synced_outputs": synced_outputs,
        "missing_outputs": list(missing_outputs or []),
        "output_sync_error": output_sync_error,
        "total_cost_usd": None,
        "cost": None,
        "cost_pending": True,
    }
    # Persist the run result before optional remote cost enrichment. If the
    # provider is unavailable, callers still get a usable session status.
    write_json_atomic(status_path, payload)

    try:
        cost_summary = summarize_claude_stream_log(stream_log)
    except Exception as exc:
        cost_summary = {
            "stream_log": str(stream_log),
            "parsed": False,
            "summary_error": str(exc)[:500],
        }
    try:
        write_cost_summary(cost_summary, cost_path)
    except Exception as exc:
        payload["cost_write_error"] = str(exc)[:500]

    payload["total_cost_usd"] = cost_summary.get("total_cost_usd")
    payload["cost"] = cost_summary
    payload["cost_pending"] = False
    write_json_atomic(status_path, payload)
    return payload


def command_prepare(args: argparse.Namespace) -> int:
    with phase_subject_lock(args.project_root, args.phase, args.subject):
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
    try:
        with phase_subject_lock(args.project_root, args.phase, args.subject):
            synced = sync_workspace_outputs(
                args.project_root,
                args.workspace_dir,
                args.phase,
                args.subject,
            )
    except WorkspaceOutputError as exc:
        raise SystemExit(str(exc)) from None
    print(json.dumps(synced, ensure_ascii=False))
    return 0


def command_status(args: argparse.Namespace) -> int:
    synced_outputs = json.loads(args.synced_outputs_json)
    missing_outputs = json.loads(args.missing_outputs_json)
    if not isinstance(synced_outputs, list) or not all(
        isinstance(item, str) for item in synced_outputs
    ):
        raise SystemExit("--synced-outputs-json must encode a list of strings")
    if not isinstance(missing_outputs, list) or not all(
        isinstance(item, str) for item in missing_outputs
    ):
        raise SystemExit("--missing-outputs-json must encode a list of strings")
    with phase_subject_lock(args.project_root, args.phase, args.subject):
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
            claude_settings_path=(
                args.workspace_dir / ".claude" / WORKTREE_GUARD_SETTINGS_FILENAME
            ),
            worktree_guard_path=(
                args.workspace_dir / ".claude/hooks" / WORKTREE_GUARD_FILENAME
            ),
            synced_outputs=synced_outputs,
            exit_code=args.exit_code,
            timed_out=args.timed_out,
            timeout_sec=args.timeout_sec,
            missing_outputs=missing_outputs,
            output_sync_error=args.output_sync_error,
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
    status.add_argument("--missing-outputs-json", default="[]")
    status.add_argument("--output-sync-error")
    status.add_argument("--exit-code", type=int, required=True)
    status.add_argument("--timed-out", action="store_true")
    status.add_argument("--timeout-sec", type=float)
    status.set_defaults(func=command_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
