#!/usr/bin/env python3
"""Run Claude Code in an isolated workspace and record session metadata."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import signal
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from taskgen.claude.cost import format_cost_summary
from taskgen.claude.workspace import (
    MissingWorkspaceOutputsError,
    WorkspaceOutputError,
    prepare_workspace,
    sync_workspace_outputs,
    write_status,
)
from taskgen.common import (
    phase_subject_lock,
    phase_subject_lock_delegated_by_parent,
    project_root,
)
from taskgen.config import (
    EFFORT_LEVELS,
    ModelConfig,
    claude_code_timeout_for_phase,
    load_model_config,
    phase_effort_lookup_keys,
)


PERMISSION_ARGS = ("--permission-mode", "bypassPermissions")
TIMEOUT_EXIT_CODE = 124
OUTPUT_SYNC_EXIT_CODE = 1


def executable_candidates(root: Path) -> list[Path]:
    candidates: list[Path] = []
    for candidate in sorted((root / "cc-binary").glob("claude-*")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            candidates.append(candidate)
    return candidates


def resolve_claude_command(root: Path, config: ModelConfig | None = None) -> list[str]:
    resolved_config = config or load_model_config(root)
    configured = None
    if resolved_config.claude_code_path is not None:
        configured = Path(resolved_config.claude_code_path)
        if not configured.is_absolute():
            configured = root / configured
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


def build_claude_command(
    root: Path,
    model: str | None,
    effort: str | None,
    prompt: str,
    *,
    config: ModelConfig | None = None,
) -> list[str]:
    command = [
        *resolve_claude_command(root, config),
        "--verbose",
        "--output-format=stream-json",
    ]
    if model:
        command.extend(["--model", model])
    if effort:
        command.extend(["--effort", effort])
    command.extend([*PERMISSION_ARGS, "--print", "--", prompt])
    return command


def run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{os.getpid()}"


class ActiveRunsSharedLock:
    """Hold the shared side of the cleanup-vs-run advisory lock."""

    def __init__(self, root: Path) -> None:
        self.path = root / "runs/.active-runs.lock"
        self.handle: BinaryIO | None = None

    def __enter__(self) -> ActiveRunsSharedLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_SH)
        except BaseException:
            self.handle.close()
            self.handle = None
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.handle is None:
            return
        try:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        finally:
            try:
                self.handle.close()
            except OSError:
                pass
            self.handle = None


def terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Force-stop the isolated process group created for one Claude run."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    else:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    process.wait()


def run_claude_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_handle: BinaryIO,
    timeout_sec: float,
) -> tuple[int, bool]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        return process.wait(timeout=timeout_sec), False
    except subprocess.TimeoutExpired:
        terminate_process_tree(process)
        return TIMEOUT_EXIT_CODE, True
    except BaseException:
        # KeyboardInterrupt and unexpected wait failures must not orphan the
        # independently-sessioned Claude process or any tool subprocesses.
        try:
            terminate_process_tree(process)
        except BaseException:
            pass
        raise


def run_claude_logged(args: argparse.Namespace) -> int:
    root = project_root()
    try:
        lock_is_delegated = phase_subject_lock_delegated_by_parent(root, args.subject)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    subject_lock = (
        contextlib.nullcontext()
        if lock_is_delegated
        else phase_subject_lock(root, args.phase, args.subject)
    )
    with subject_lock, ActiveRunsSharedLock(root):
        resolved_run_id = run_id()
        workspace_payload = prepare_workspace(
            root,
            args.phase,
            args.subject,
            args.prompt_file,
            resolved_run_id,
        )
        run_dir = Path(str(workspace_payload["run_dir"]))
        active_marker = run_dir / ".active"
        active_marker.write_text(
            json.dumps({"pid": os.getpid(), "run_id": resolved_run_id}) + "\n",
            encoding="utf-8",
        )
        try:
            return run_prepared_claude_logged(
                args,
                root=root,
                resolved_run_id=resolved_run_id,
                workspace_payload=workspace_payload,
            )
        finally:
            try:
                active_marker.unlink(missing_ok=True)
            except OSError:
                pass


def run_prepared_claude_logged(
    args: argparse.Namespace,
    *,
    root: Path,
    resolved_run_id: str,
    workspace_payload: dict[str, object],
) -> int:
    run_dir = Path(str(workspace_payload["run_dir"]))
    workspace_dir = Path(str(workspace_payload["workspace_dir"]))
    claude_config_dir = Path(str(workspace_payload["claude_config_dir"]))
    stream_log = Path(str(workspace_payload["stream_log"]))
    cost_path = Path(str(workspace_payload["cost_path"]))
    status_path = Path(str(workspace_payload["status_path"]))
    prompt_copy = Path(str(workspace_payload["prompt_copy"]))
    workspace_prompt = Path(str(workspace_payload["workspace_prompt"]))
    claude_settings_path = Path(str(workspace_payload["claude_settings_path"]))
    worktree_guard_path = Path(str(workspace_payload["worktree_guard_path"]))

    prompt = prompt_copy.read_text(encoding="utf-8")
    config = load_model_config(root)
    model = args.model or config.default_model
    effort = args.effort
    if effort is None:
        for key in phase_effort_lookup_keys(args.phase):
            effort = config.phase_efforts.get(key)
            if effort is not None:
                break
    if effort is None:
        effort = config.default_effort
    timeout_sec = claude_code_timeout_for_phase(config, args.phase)
    command = build_claude_command(root, model, effort, prompt, config=config)

    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir)
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = env.get(
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "1",
    )
    env["IS_SANDBOX"] = "1"

    timed_out = False
    with stream_log.open("wb") as log_handle:
        exit_code, timed_out = run_claude_process(
            command,
            cwd=workspace_dir,
            env=env,
            log_handle=log_handle,
            timeout_sec=timeout_sec,
        )
        if timed_out:
            log_handle.write(
                f"\nClaude Code timed out after {timeout_sec:g} seconds.\n".encode("utf-8")
            )

    synced_outputs: list[str] = []
    missing_outputs: list[str] = []
    output_sync_error: str | None = None
    if exit_code == 0:
        try:
            synced_outputs = sync_workspace_outputs(root, workspace_dir, args.phase, args.subject)
        except MissingWorkspaceOutputsError as exc:
            missing_outputs = exc.missing_outputs
            output_sync_error = str(exc)
            exit_code = OUTPUT_SYNC_EXIT_CODE
        except (WorkspaceOutputError, OSError) as exc:
            output_sync_error = str(exc)
            exit_code = OUTPUT_SYNC_EXIT_CODE
        if output_sync_error:
            with stream_log.open("ab") as log_handle:
                log_handle.write(
                    f"\nOutput synchronization failed: {output_sync_error}\n".encode("utf-8")
                )

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
        claude_settings_path=claude_settings_path,
        worktree_guard_path=worktree_guard_path,
        synced_outputs=synced_outputs,
        exit_code=exit_code,
        timed_out=timed_out,
        timeout_sec=timeout_sec,
        missing_outputs=missing_outputs,
        output_sync_error=output_sync_error,
    )

    cost = status.get("cost")
    cost_summary = format_cost_summary(cost if isinstance(cost, dict) else {})
    print(f"claude session dir: {run_dir}", file=sys.stderr)
    print(f"claude stream log: {stream_log}", file=sys.stderr)
    print(f"claude workspace: {workspace_dir}", file=sys.stderr)
    if timed_out:
        print(f"claude timeout: exceeded {timeout_sec:g} seconds", file=sys.stderr)
    if output_sync_error:
        print(f"claude output sync failed: {output_sync_error}", file=sys.stderr)
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
