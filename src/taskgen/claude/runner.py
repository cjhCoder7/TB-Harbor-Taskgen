#!/usr/bin/env python3
"""Run Claude Code in an isolated workspace and record session metadata."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from taskgen.claude.cost import format_cost_summary
from taskgen.claude.workspace import prepare_workspace, sync_workspace_outputs, write_status
from taskgen.common import project_root
from taskgen.config import (
    EFFORT_LEVELS,
    resolve_claude_code_path,
    resolve_effort_level,
    resolve_model_name,
)


PERMISSION_ARGS = ("--permission-mode", "bypassPermissions")
DISALLOWED_TOOL_ARGS = (
    "--disallowedTools",
    "Bash(*find / *)",
    "Bash(*find /)",
    "Bash(*grep -R / *)",
    "Bash(*grep -r / *)",
    "Bash(*rg / *)",
    "Bash(*rg --files / *)",
    "Bash(*locate *)",
)


def executable_candidates(root: Path) -> list[Path]:
    candidates: list[Path] = []
    for candidate in sorted((root / "cc-binary").glob("claude-*")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            candidates.append(candidate)
    return candidates


def resolve_claude_command(root: Path) -> list[str]:
    configured = resolve_claude_code_path(root)
    if configured is not None:
        if not configured.is_file() or not os.access(configured, os.X_OK):
            raise SystemExit(f"model.json claude_code_path points to a non-executable path: {configured}")
        return [str(configured)]

    candidates = executable_candidates(root)
    if len(candidates) == 1:
        return [str(candidates[0])]
    if len(candidates) > 1:
        raise SystemExit(
            "multiple executable Claude binaries found under cc-binary; "
            "keep exactly one or configure model.json claude_code_path"
        )

    path_claude = shutil.which("claude")
    if path_claude:
        return [path_claude]

    raise SystemExit(
        "claude binary not found; set model.json claude_code_path, "
        "keep exactly one executable cc-binary/claude-*, or expose claude on PATH"
    )


def build_claude_command(root: Path, model: str | None, effort: str | None, prompt: str) -> list[str]:
    command = [
        *resolve_claude_command(root),
        "--verbose",
        "--output-format=stream-json",
    ]
    if model:
        command.extend(["--model", model])
    if effort:
        command.extend(["--effort", effort])
    command.extend(DISALLOWED_TOOL_ARGS)
    command.extend([*PERMISSION_ARGS, "--print", "--", prompt])
    return command


def run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{os.getpid()}"


def run_claude_logged(args: argparse.Namespace) -> int:
    root = project_root()
    resolved_run_id = run_id()
    workspace_payload = prepare_workspace(
        root,
        args.phase,
        args.subject,
        args.prompt_file,
        resolved_run_id,
    )

    run_dir = Path(str(workspace_payload["run_dir"]))
    workspace_dir = Path(str(workspace_payload["workspace_dir"]))
    claude_config_dir = Path(str(workspace_payload["claude_config_dir"]))
    stream_log = Path(str(workspace_payload["stream_log"]))
    cost_path = Path(str(workspace_payload["cost_path"]))
    status_path = Path(str(workspace_payload["status_path"]))
    prompt_copy = Path(str(workspace_payload["prompt_copy"]))
    workspace_prompt = Path(str(workspace_payload["workspace_prompt"]))

    prompt = prompt_copy.read_text(encoding="utf-8")
    model = resolve_model_name(root, args.model)
    effort = resolve_effort_level(root, args.effort, args.phase)
    command = build_claude_command(root, model, effort, prompt)

    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir)
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = env.get(
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "1",
    )
    env["IS_SANDBOX"] = "1"

    with stream_log.open("wb") as log_handle:
        result = subprocess.run(
            command,
            cwd=workspace_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            check=False,
        )
    exit_code = result.returncode

    if exit_code == 0:
        synced_outputs = sync_workspace_outputs(root, workspace_dir, args.phase, args.subject)
    else:
        synced_outputs = []

    status = write_status(
        status_path=status_path,
        phase=args.phase,
        subject=args.subject,
        run_id=resolved_run_id,
        project_root=root,
        workspace_dir=workspace_dir,
        prompt_copy=prompt_copy,
        workspace_prompt=workspace_prompt,
        stream_log=stream_log,
        cost_path=cost_path,
        claude_config_dir=claude_config_dir,
        synced_outputs=synced_outputs,
        exit_code=exit_code,
    )

    cost = status.get("cost")
    cost_summary = format_cost_summary(cost if isinstance(cost, dict) else {})
    print(f"claude session dir: {run_dir}", file=sys.stderr)
    print(f"claude stream log: {stream_log}", file=sys.stderr)
    print(f"claude workspace: {workspace_dir}", file=sys.stderr)
    print(f"claude summary: {cost_summary}", file=sys.stderr)
    print(f"claude cost file: {cost_path}", file=sys.stderr)
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run-claude-logged.sh", description=__doc__)
    parser.add_argument("phase")
    parser.add_argument("subject")
    parser.add_argument("prompt_file", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--effort", choices=EFFORT_LEVELS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run_claude_logged(args)


if __name__ == "__main__":
    sys.exit(main())
