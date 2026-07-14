#!/usr/bin/env python3
"""Run Harbor oracle and nop checks for one generated task."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from taskgen.common import directory_tree_sha256, project_root, write_json_object_atomic
from taskgen.config import resolve_harbor_check_timeout_sec


TIMEOUT_EXIT_CODE = 124


@dataclass(frozen=True)
class HarborCheck:
    agent: str
    exit_code: int
    reward: float | None
    log: Path
    job_dir: Path
    timed_out: bool = False
    timeout_sec: float = 10800.0


def resolve_task_path(root: Path, task_input: str) -> Path:
    candidate = Path(task_input)
    if candidate.is_dir():
        task_path = candidate.resolve()
    else:
        project_candidate = root / task_input
        if not project_candidate.is_dir():
            raise SystemExit(f"task path does not exist: {task_input}")
        task_path = project_candidate.resolve()

    if not (task_path / "task.toml").is_file():
        raise SystemExit(f"task.toml not found under task path: {task_path}")
    return task_path


def derive_task_id(task_path: Path) -> str:
    task_id = task_name_from_toml(task_path / "task.toml")
    if not task_id:
        parent = task_path.parent.name
        base = task_path.name
        if parent and parent not in {"accepted", "rejected", "generated", "tb-harbor-taskgen"}:
            task_id = f"{parent}__{base}"
        else:
            task_id = base

    safe_task_id = re.sub(r"[^A-Za-z0-9._-]+", "_", task_id).strip("._-")
    return safe_task_id or "task"


def task_name_from_toml(task_toml: Path) -> str | None:
    section = None
    try:
        lines = task_toml.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.strip("[]").strip()
            continue
        if section != "task":
            continue
        match = re.match(r"""name\s*=\s*["']([^"']+)["']""", stripped)
        if match:
            return match.group(1).rsplit("/", 1)[-1]
    return None


def utc_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{os.getpid()}"


def resolve_harbor_command() -> list[str]:
    configured = os.environ.get("HARBOR_BIN", "").strip()
    if configured:
        configured_path = Path(configured)
        if configured_path.is_file() and os.access(configured_path, os.X_OK):
            return [str(configured_path)]
        resolved = shutil.which(configured)
        if resolved:
            return [resolved]
        raise SystemExit(f"HARBOR_BIN is set but not executable or not on PATH: {configured}")

    resolved = shutil.which("harbor")
    if resolved:
        return [resolved]
    raise SystemExit("harbor command not found; install Harbor, expose it on PATH, or set HARBOR_BIN")


def numeric_reward(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        reward = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(reward):
        return None
    return reward


def extract_reward(root: Path) -> float | None:
    for result_path in sorted(root.glob("**/result.json")):
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue

        if not isinstance(data, dict):
            continue
        verifier_result = data.get("verifier_result")
        if not isinstance(verifier_result, dict):
            continue

        rewards = verifier_result.get("rewards")
        if isinstance(rewards, dict):
            reward = numeric_reward(rewards.get("reward"))
            if reward is not None:
                return reward

        reward = numeric_reward(verifier_result.get("reward"))
        if reward is not None:
            return reward

    for reward_path in sorted(root.glob("**/verifier/reward.json")):
        try:
            data = json.loads(reward_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            reward = numeric_reward(data.get("reward"))
            if reward is not None:
                return reward

    for reward_path in sorted(root.glob("**/verifier/reward.txt")):
        try:
            text = reward_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        reward = numeric_reward(text)
        if reward is not None:
            return reward

    return None


def run_harbor_check(
    harbor_cmd: list[str],
    task_path: Path,
    jobs_dir: Path,
    out_dir: Path,
    agent: str,
    job_name: str,
    timeout_sec: float = 10800.0,
) -> HarborCheck:
    log_path = out_dir / f"{agent}.log"
    job_root = jobs_dir / job_name
    out_dir.mkdir(parents=True, exist_ok=True)

    timed_out = False
    exit_code = 127
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"Running harbor {agent} check for {task_path}\n")
        log.write(f"Timeout: {timeout_sec:g} seconds\n")
        log.flush()
        command = [
            *harbor_cmd,
            "run",
            "-p",
            str(task_path),
            "-a",
            agent,
            "-o",
            str(jobs_dir),
            "--job-name",
            job_name,
            "-k",
            "1",
            "-y",
        ]
        try:
            process = subprocess.Popen(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            log.write(f"Failed to start Harbor: {exc}\n")
        else:
            try:
                exit_code = process.wait(timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate_process_tree(process)
                exit_code = TIMEOUT_EXIT_CODE
                log.write(f"\nHarbor {agent} check timed out after {timeout_sec:g} seconds.\n")
            except BaseException:
                try:
                    terminate_process_tree(process)
                except BaseException:
                    pass
                raise

    reward = extract_reward(job_root) if job_root.is_dir() else None
    return HarborCheck(
        agent=agent,
        exit_code=exit_code,
        reward=reward,
        log=log_path,
        job_dir=job_root,
        timed_out=timed_out,
        timeout_sec=timeout_sec,
    )


def terminate_process_tree(process: subprocess.Popen[Any]) -> None:
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


def reward_equals(actual: float | None, expected: float) -> bool:
    return actual is not None and math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-9)


def status_payload(
    task_id: str,
    task_path: Path,
    run_id: str,
    jobs_dir: Path,
    task_tree_sha256: str,
    oracle: HarborCheck,
    nop: HarborCheck,
) -> dict[str, Any]:
    passed = (
        oracle.exit_code == 0
        and nop.exit_code == 0
        and reward_equals(oracle.reward, 1.0)
        and reward_equals(nop.reward, 0.0)
    )
    return {
        "task_id": task_id,
        "task_path": str(task_path),
        "run_id": run_id,
        "task_tree_sha256": task_tree_sha256,
        "passed": passed,
        "oracle": {
            "exit_code": oracle.exit_code,
            "reward": oracle.reward,
            "log": str(oracle.log),
            "job_dir": str(oracle.job_dir),
            "timed_out": oracle.timed_out,
            "timeout_sec": oracle.timeout_sec,
        },
        "nop": {
            "exit_code": nop.exit_code,
            "reward": nop.reward,
            "log": str(nop.log),
            "job_dir": str(nop.job_dir),
            "timed_out": nop.timed_out,
            "timeout_sec": nop.timeout_sec,
        },
        "jobs_dir": str(jobs_dir),
    }


def command_run(args: argparse.Namespace) -> int:
    root = project_root()
    task_path = resolve_task_path(root, args.task_path)
    explicit_task_id = getattr(args, "task_id", None)
    task_id = sanitize_task_id(explicit_task_id) if explicit_task_id else derive_task_id(task_path)
    run_id = utc_run_id()
    out_dir = root / "runs/oracle-nop-check" / task_id
    jobs_dir = out_dir / "harbor-jobs" / run_id
    status_path = out_dir / "oracle-nop-status.json"
    jobs_dir.mkdir(parents=True, exist_ok=True)

    harbor_cmd = resolve_harbor_command()
    timeout_sec = resolve_harbor_check_timeout_sec(root)
    try:
        task_tree_hash = directory_tree_sha256(task_path)
    except (OSError, RuntimeError) as exc:
        raise SystemExit(f"cannot hash task directory before Harbor checks: {exc}") from None
    oracle = run_harbor_check(
        harbor_cmd,
        task_path,
        jobs_dir,
        out_dir,
        "oracle",
        "oracle",
        timeout_sec,
    )
    nop = run_harbor_check(
        harbor_cmd,
        task_path,
        jobs_dir,
        out_dir,
        "nop",
        "nop",
        timeout_sec,
    )

    payload = status_payload(
        task_id,
        task_path,
        run_id,
        jobs_dir,
        task_tree_hash,
        oracle,
        nop,
    )
    write_json_object_atomic(status_path, payload)

    print(f"oracle/nop status: {status_path}")
    print(f"oracle/nop result: {'passed' if payload['passed'] else 'failed'}")
    print(
        "oracle: "
        f"reward={oracle.reward if oracle.reward is not None else 'missing'}, "
        f"exit_code={oracle.exit_code}, timed_out={str(oracle.timed_out).lower()}"
    )
    print(
        "nop: "
        f"reward={nop.reward if nop.reward is not None else 'missing'}, "
        f"exit_code={nop.exit_code}, timed_out={str(nop.timed_out).lower()}"
    )
    return 0 if payload["passed"] else 1


def sanitize_task_id(task_id: str) -> str:
    safe_task_id = re.sub(r"[^A-Za-z0-9._-]+", "_", task_id).strip("._-")
    if not safe_task_id:
        raise SystemExit(f"task id becomes empty after sanitization: {task_id!r}")
    return safe_task_id


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run-harbor-oracle-nop.sh", description=__doc__)
    parser.add_argument("task_path")
    parser.add_argument(
        "--task-id",
        help="Override the output task id. Phase runners use this to keep artifact paths stable.",
    )
    parser.set_defaults(func=command_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
