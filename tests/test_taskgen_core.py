from __future__ import annotations

import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WORKTREE_GUARD_SCRIPT = SRC / "taskgen/claude/worktree_guard.py"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from taskgen.claude.cost import OpenRouterQueryError
from taskgen.claude.cost import fetch_openrouter_generation_stats, read_float_env
from taskgen.claude.cost import parse_claude_stream_log
from taskgen.claude.cost import format_cost_summary
from taskgen.claude.cost import summarize_claude_stream_log
from taskgen.claude.runner import build_claude_command, run_claude_logged, run_claude_process
from taskgen.claude.workspace import (
    CLAUDE_PERMISSION_DENY_RULES,
    MissingWorkspaceOutputsError,
    WorkspaceOutputError,
    output_path_sha256,
    output_sync_journal_path,
    phase_input_paths,
    phase_output_paths,
    prepare_workspace,
    recover_interrupted_output_sync,
    sync_workspace_outputs,
)
from taskgen.cli import (
    PhaseRunResult,
    build_phase_process_command,
    command_pipeline,
    get_phase,
    load_pipeline_idea_ids,
    pipeline_phase1_count_matches,
)
from taskgen.config import (
    load_model_config,
    resolve_claude_code_path,
    resolve_claude_code_timeout_sec,
    resolve_effort_level,
)
from taskgen.common import (
    ValidationReport,
    append_jsonl_object,
    delegated_phase_subject_lock_kwargs,
    directory_tree_sha256,
    phase_subject_lock,
    phase_subject_lock_delegated_by_parent,
    validate_claude_session_reference,
)
from taskgen.harbor.oracle_nop import extract_reward, run_harbor_check
from taskgen.maintenance.clean_intermediate import clean_targets, command_clean
from taskgen.phases.phase1_seed_brainstorm import render_phase1_prompt, validate_brainstorm_data
from taskgen.phases.phase2_skillnet_research import render_phase2_prompt, validate_phase2
from taskgen.phases.phase3_task_generation import render_phase3_prompt, validate_phase3
from taskgen.phases.phase4_oracle_nop_check import (
    append_manifest_event as append_phase4_manifest_event,
    command_run as command_run_phase4,
    validate_harbor_check,
    validate_phase4,
)
from taskgen.phases.phase5_task_review import (
    append_manifest_event as append_phase5_manifest_event,
    ensure_phase5_inputs,
    render_phase5_prompt,
    validate_phase5,
)
from taskgen.phases.phase6_task_repair import (
    ensure_phase6_inputs,
    render_phase6_prompt,
    validate_new_session_synced_task,
)
from taskgen.phases.phase7_finalize import (
    FinalizationError,
    accepted_task_path,
    append_manifest_event as append_phase7_manifest_event,
    ensure_phase7_inputs,
    finalization_journal_path,
    move_final_task,
    recover_interrupted_finalization,
    rejected_task_path,
    run_phase7_locked,
    validate_phase7,
)


def pinned_claude_bash_permission_match(rule: str, command: str) -> bool:
    """Model simple wildcard matching in the project's pinned Claude Code."""

    prefix = "Bash("
    if not rule.startswith(prefix) or not rule.endswith(")"):
        raise ValueError(f"not a scoped Bash permission rule: {rule}")
    pattern = rule[len(prefix) : -1]

    regex = "".join(".*" if character == "*" else re.escape(character) for character in pattern)
    return re.fullmatch(regex, command, flags=re.DOTALL) is not None


def write_fake_claude_session(project: Path, phase: str, subject: str, run_id: str = "run-1") -> str:
    session = project / "runs/claude-sessions" / phase / subject / run_id
    session.mkdir(parents=True, exist_ok=True)
    if phase == "seed-brainstorm":
        synced_outputs = [f"runs/brainstorm/{subject}/seed_brainstorm.json"]
    elif phase == "skillnet-research":
        synced_outputs = [f"runs/skillnet/{subject}"]
    elif phase == "task-generation":
        seed_id, idea_id = subject.split("__", 1)
        synced_outputs = [f"generated/working/{seed_id}/{idea_id}"]
    elif phase == "task-review":
        synced_outputs = [f"runs/reviews/{subject}"]
    elif phase == "task-repair":
        seed_id, idea_id = subject.split("__", 1)
        synced_outputs = [f"generated/working/{seed_id}/{idea_id}"]
    else:
        raise AssertionError(f"unsupported fake Claude phase: {phase}")
    (session / "status.json").write_text(
        json.dumps(
            {
                "phase": phase,
                "subject": subject,
                "run_id": run_id,
                "exit_code": 0,
                "timed_out": False,
                "synced_outputs": synced_outputs,
            }
        ),
        encoding="utf-8",
    )
    return session.relative_to(project).as_posix()


def review_markdown_fixture(decision: str) -> str:
    return (
        "# Task review\n\n"
        "## Summary\n\nReview summary.\n\n"
        "## Modification items\n\n"
        + ("Changes are listed in review.json.\n\n" if decision == "needs_modification" else "None.\n\n")
        + "## Blocking reasons\n\n"
        + ("The task concept is unsuitable.\n\n" if decision == "rejected" else "None.\n\n")
        + f"## Final decision\n\n{decision}\n"
    )


def fake_claude_runner_fixture(
    root: Path,
    timeout_sec: int | float = 75,
) -> tuple[dict[str, Path], SimpleNamespace]:
    workspace_dir = root / "workspace"
    workspace_dir.mkdir()
    run_dir = root / "run"
    run_dir.mkdir()
    prompt_copy = run_dir / "prompt.md"
    prompt_copy.write_text("prompt", encoding="utf-8")
    claude_config_dir = run_dir / "claude-config"
    claude_config_dir.mkdir()
    workspace_claude = workspace_dir / ".claude"
    workspace_claude.mkdir()
    claude_settings_path = workspace_claude / "settings.json"
    claude_settings_path.write_text("{}\n", encoding="utf-8")
    worktree_guard_path = workspace_claude / "worktree-guard.py"
    worktree_guard_path.write_text("", encoding="utf-8")
    (root / "model.json").write_text(
        json.dumps({"claude_code_timeout_sec": timeout_sec}),
        encoding="utf-8",
    )
    workspace_payload = {
        "run_dir": run_dir,
        "workspace_dir": workspace_dir,
        "claude_config_dir": claude_config_dir,
        "stream_log": run_dir / "claude-code.jsonl",
        "cost_path": run_dir / "cost.json",
        "status_path": run_dir / "status.json",
        "prompt_copy": prompt_copy,
        "workspace_prompt": workspace_dir / "prompt.md",
        "claude_settings_path": claude_settings_path,
        "worktree_guard_path": worktree_guard_path,
    }
    args = SimpleNamespace(
        phase="seed-brainstorm",
        subject="seed-a",
        prompt_file=root / "input-prompt.md",
        model=None,
        effort=None,
    )
    return workspace_payload, args


def process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():
        try:
            state = proc_stat.read_text(encoding="utf-8").split()[2]
        except (FileNotFoundError, IndexError, OSError):
            return False
        if state in {"Z", "X"}:
            return False
    return True


def wait_for_process_exit(pid: int, timeout_sec: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if not process_is_running(pid):
            return True
        time.sleep(0.01)
    return not process_is_running(pid)


def force_kill_processes(pid_paths: tuple[Path, ...]) -> None:
    for pid_path in pid_paths:
        try:
            pid = int(pid_path.read_text(encoding="utf-8"))
            os.kill(pid, signal.SIGKILL)
        except (FileNotFoundError, ProcessLookupError, ValueError):
            pass


def difficulty_profile_fixture() -> dict[str, object]:
    return {
        "minimum_independent_subskills": 3,
        "too_easy_antipatterns": ["single obvious command"],
        "hardening_levers": ["derive one parameter from visible artifacts"],
        "fairness_bounds": ["oracle remains deterministic and bounded"],
    }


def difficulty_hardening_fixture() -> dict[str, object]:
    return {
        "minimum_complexity_contract": "Require at least three independent reasoning or tooling steps.",
        "too_easy_risks": ["single command or guaranteed direct lookup"],
        "recommended_hardening": ["add a bounded inference or validation stage"],
        "do_not_simplify": ["do not reduce the task to a one-loop fixture"],
    }


class ModelConfigTests(unittest.TestCase):
    def test_claude_code_timeout_defaults_to_half_hour(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_model_config(Path(tmp))

        self.assertEqual(config.claude_code_timeout_sec, 1800.0)
        self.assertEqual(config.claude_code_phase_timeouts_sec, {})
        self.assertEqual(config.harbor_check_timeout_sec, 10800.0)

    def test_phase_timeout_overrides_global_timeout_for_canonical_phase_and_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "model.json").write_text(
                json.dumps(
                    {
                        "claude_code_timeout_sec": 1800,
                        "claude_code_phase_timeouts_sec": {
                            "phase3": 10800,
                            "task-repair": 7200.5,
                        },
                    }
                ),
                encoding="utf-8",
            )

            config = load_model_config(root)

            self.assertEqual(config.claude_code_phase_timeouts_sec["phase3"], 10800.0)
            self.assertEqual(
                config.claude_code_phase_timeouts_sec["task-repair"],
                7200.5,
            )
            self.assertEqual(resolve_claude_code_timeout_sec(root, "task-generation"), 10800.0)
            self.assertEqual(resolve_claude_code_timeout_sec(root, "phase6"), 7200.5)
            self.assertEqual(resolve_claude_code_timeout_sec(root, "seed-brainstorm"), 1800.0)

    def test_invalid_phase_timeout_configuration_fails_fast(self) -> None:
        invalid_values = (
            [],
            {"phase3": 0},
            {"phase3": -1},
            {"phase3": True},
            {"phase3": "10800"},
            {"phase3": float("nan")},
            {"phase3": float("inf")},
            {"phasex": 10800},
        )
        for invalid_value in invalid_values:
            with self.subTest(invalid_value=invalid_value), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "model.json").write_text(
                    json.dumps({"claude_code_phase_timeouts_sec": invalid_value}),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(SystemExit, "claude_code_phase_timeouts_sec"):
                    load_model_config(root)

    def test_harbor_timeout_is_configurable_and_strictly_positive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "model.json").write_text(
                json.dumps({"harbor_check_timeout_sec": 42.5}),
                encoding="utf-8",
            )
            self.assertEqual(load_model_config(root).harbor_check_timeout_sec, 42.5)

        for invalid_timeout in (0, -1, True, "10800", float("nan"), float("inf")):
            with self.subTest(invalid_timeout=invalid_timeout), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "model.json").write_text(
                    json.dumps({"harbor_check_timeout_sec": invalid_timeout}),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(SystemExit, "harbor_check_timeout_sec"):
                    load_model_config(root)

    def test_unknown_top_level_model_key_fails_fast(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "model.json").write_text(
                json.dumps({"claude_timeout_sec": 1800}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(SystemExit, "unknown top-level key"):
                load_model_config(root)

    def test_load_model_json_and_resolve_relative_claude_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "model.json").write_text(
                json.dumps(
                    {
                        "claude_code_path": "cc-binary/claude-test",
                        "claude_code_timeout_sec": 75.5,
                        "default_model": "test/model",
                        "default_effort": "high",
                        "phase_efforts": {"phase1": "max", "task-review": "medium"},
                    }
                ),
                encoding="utf-8",
            )

            config = load_model_config(root)

            self.assertEqual(config.default_model, "test/model")
            self.assertEqual(config.default_effort, "high")
            self.assertEqual(config.claude_code_timeout_sec, 75.5)
            self.assertEqual(config.phase_efforts["phase1"], "max")
            self.assertEqual(config.phase_efforts["task-review"], "medium")
            self.assertEqual(resolve_claude_code_path(root), root / "cc-binary/claude-test")
            self.assertEqual(resolve_effort_level(root, None, "phase1"), "max")
            self.assertEqual(resolve_effort_level(root, None, "seed-brainstorm"), "max")
            self.assertEqual(resolve_effort_level(root, None, "phase5"), "medium")
            self.assertEqual(resolve_effort_level(root, None, "unknown"), "high")
            self.assertEqual(resolve_effort_level(root, "low", "phase1"), "low")

    def test_invalid_effort_fails_fast(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "model.json").write_text(
                json.dumps({"default_effort": "extreme"}),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit):
                load_model_config(root)

    def test_invalid_claude_code_timeout_fails_fast(self) -> None:
        for invalid_timeout in (0, -1, True, "1800", float("nan"), float("inf"), float("-inf")):
            with self.subTest(invalid_timeout=invalid_timeout), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "model.json").write_text(
                    json.dumps({"claude_code_timeout_sec": invalid_timeout}),
                    encoding="utf-8",
                )

                with self.assertRaises(SystemExit):
                    load_model_config(root)

    def test_invalid_phase_effort_fails_fast(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "model.json").write_text(
                json.dumps({"phase_efforts": {"phase2": "extreme"}}),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit):
                load_model_config(root)

    def test_unknown_phase_effort_key_fails_fast(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "model.json").write_text(
                json.dumps({"phase_efforts": {"phasex": "high"}}),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit):
                load_model_config(root)


class ClaudeRunnerTests(unittest.TestCase):
    def test_claude_command_does_not_duplicate_workspace_restrictions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "cc-binary/claude-test"
            binary.parent.mkdir()
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(binary, 0o755)
            (root / "model.json").write_text(
                json.dumps({"claude_code_path": "cc-binary/claude-test"}),
                encoding="utf-8",
            )

            command = build_claude_command(root, "claude-opus-4-8", "high", "prompt")

        self.assertNotIn("--disallowedTools", command)
        self.assertNotIn("--disallowed-tools", command)
        self.assertIn("--permission-mode", command)
        self.assertLess(command.index("--permission-mode"), command.index("--print"))

    def test_run_claude_passes_phase_timeout_override_to_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_payload, args = fake_claude_runner_fixture(root)
            args.phase = "task-generation"
            (root / "model.json").write_text(
                json.dumps(
                    {
                        "claude_code_timeout_sec": 75,
                        "claude_code_phase_timeouts_sec": {"phase3": 10800},
                    }
                ),
                encoding="utf-8",
            )
            process = MagicMock()
            process.wait.return_value = 1

            with (
                patch("taskgen.claude.runner.project_root", return_value=root),
                patch("taskgen.claude.runner.run_id", return_value="run-1"),
                patch("taskgen.claude.runner.prepare_workspace", return_value=workspace_payload),
                patch("taskgen.claude.runner.build_claude_command", return_value=["claude"]),
                patch("taskgen.claude.runner.subprocess.Popen", return_value=process) as popen,
                patch("taskgen.claude.runner.write_status", return_value={}) as write_status,
            ):
                exit_code = run_claude_logged(args)

        self.assertEqual(exit_code, 1)
        self.assertIs(popen.call_args.kwargs["start_new_session"], True)
        process.wait.assert_called_once_with(timeout=10800.0)
        self.assertIs(write_status.call_args.kwargs["timed_out"], False)
        self.assertEqual(write_status.call_args.kwargs["timeout_sec"], 10800.0)
        self.assertEqual(
            write_status.call_args.kwargs["claude_settings_path"],
            workspace_payload["claude_settings_path"],
        )
        self.assertEqual(
            write_status.call_args.kwargs["worktree_guard_path"],
            workspace_payload["worktree_guard_path"],
        )

    def test_run_claude_records_timeout_without_syncing_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_payload, args = fake_claude_runner_fixture(root)
            process = MagicMock()
            process.pid = 999_999
            process.wait.side_effect = [
                subprocess.TimeoutExpired(cmd=["claude"], timeout=75),
                -signal.SIGKILL,
            ]

            with (
                patch("taskgen.claude.runner.project_root", return_value=root),
                patch("taskgen.claude.runner.run_id", return_value="run-1"),
                patch("taskgen.claude.runner.prepare_workspace", return_value=workspace_payload),
                patch("taskgen.claude.runner.build_claude_command", return_value=["claude"]),
                patch("taskgen.claude.runner.subprocess.Popen", return_value=process),
                patch("taskgen.claude.runner.os.killpg") as killpg,
                patch("taskgen.claude.runner.sync_workspace_outputs") as sync_outputs,
                patch("taskgen.claude.runner.write_status", return_value={}) as write_status,
            ):
                exit_code = run_claude_logged(args)

        self.assertEqual(exit_code, 124)
        sync_outputs.assert_not_called()
        write_status.assert_called_once()
        self.assertEqual(write_status.call_args.kwargs["exit_code"], 124)
        self.assertEqual(write_status.call_args.kwargs["synced_outputs"], [])
        self.assertIs(write_status.call_args.kwargs["timed_out"], True)
        self.assertEqual(write_status.call_args.kwargs["timeout_sec"], 75.0)
        self.assertEqual(
            process.wait.call_args_list,
            [call(timeout=75.0), call()],
        )
        killpg.assert_called_once_with(process.pid, signal.SIGKILL)

    def test_successful_claude_with_missing_output_fails_without_overwriting_old_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_payload, args = fake_claude_runner_fixture(root)
            destination = root / "runs/brainstorm/seed-a/seed_brainstorm.json"
            destination.parent.mkdir(parents=True)
            destination.write_text("old-result", encoding="utf-8")
            process = MagicMock()
            process.wait.return_value = 0

            with (
                patch("taskgen.claude.runner.project_root", return_value=root),
                patch("taskgen.claude.runner.run_id", return_value="run-1"),
                patch("taskgen.claude.runner.prepare_workspace", return_value=workspace_payload),
                patch("taskgen.claude.runner.build_claude_command", return_value=["claude"]),
                patch("taskgen.claude.runner.subprocess.Popen", return_value=process),
                patch("taskgen.claude.runner.write_status", return_value={}) as write_status,
            ):
                exit_code = run_claude_logged(args)

            self.assertEqual(exit_code, 1)
            self.assertEqual(destination.read_text(encoding="utf-8"), "old-result")
            self.assertEqual(
                write_status.call_args.kwargs["missing_outputs"],
                ["output/seed_brainstorm.json"],
            )
            self.assertIn("missing declared workspace output", write_status.call_args.kwargs["output_sync_error"])
            self.assertEqual(write_status.call_args.kwargs["synced_outputs"], [])

    @unittest.skipUnless(os.name == "posix", "process-group cleanup requires POSIX")
    def test_wait_exception_cleans_process_group_before_reraising(self) -> None:
        process = MagicMock()
        process.pid = 43210
        process.wait.side_effect = [KeyboardInterrupt(), -signal.SIGKILL]

        with (
            tempfile.TemporaryFile("w+b") as log_handle,
            patch("taskgen.claude.runner.subprocess.Popen", return_value=process),
            patch("taskgen.claude.runner.os.killpg") as killpg,
        ):
            with self.assertRaises(KeyboardInterrupt):
                run_claude_process(
                    ["claude"],
                    cwd=Path.cwd(),
                    env={},
                    log_handle=log_handle,
                    timeout_sec=10,
                )

        killpg.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertEqual(process.wait.call_args_list, [call(timeout=10), call()])

    @unittest.skipUnless(os.name == "posix", "process-group cleanup requires POSIX")
    def test_timeout_cleans_up_real_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_payload, args = fake_claude_runner_fixture(root, timeout_sec=0.5)
            parent_pid_path = root / "parent.pid"
            child_pid_path = root / "child.pid"
            parent_marker = root / "parent.marker"
            child_marker = root / "child.marker"
            child_script = root / "child.py"
            child_script.write_text(
                "import os\n"
                "import sys\n"
                "import time\n"
                "from pathlib import Path\n"
                "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8')\n"
                "while True:\n"
                "    with Path(sys.argv[2]).open('a', encoding='utf-8') as marker:\n"
                "        marker.write('child\\n')\n"
                "    time.sleep(0.02)\n",
                encoding="utf-8",
            )
            parent_script = root / "parent.py"
            parent_script.write_text(
                "import os\n"
                "import subprocess\n"
                "import sys\n"
                "import time\n"
                "from pathlib import Path\n"
                "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[4], sys.argv[5]])\n"
                "Path(sys.argv[2]).write_text(str(os.getpid()), encoding='utf-8')\n"
                "while True:\n"
                "    with Path(sys.argv[3]).open('a', encoding='utf-8') as marker:\n"
                "        marker.write('parent\\n')\n"
                "    time.sleep(0.02)\n",
                encoding="utf-8",
            )
            command = [
                sys.executable,
                str(parent_script),
                str(child_script),
                str(parent_pid_path),
                str(parent_marker),
                str(child_pid_path),
                str(child_marker),
            ]
            pid_paths = (parent_pid_path, child_pid_path)

            try:
                with (
                    patch("taskgen.claude.runner.project_root", return_value=root),
                    patch("taskgen.claude.runner.run_id", return_value="run-1"),
                    patch("taskgen.claude.runner.prepare_workspace", return_value=workspace_payload),
                    patch("taskgen.claude.runner.build_claude_command", return_value=command),
                    patch("taskgen.claude.runner.sync_workspace_outputs") as sync_outputs,
                    patch("taskgen.claude.runner.write_status", return_value={}) as write_status,
                ):
                    exit_code = run_claude_logged(args)

                self.assertEqual(exit_code, 124)
                sync_outputs.assert_not_called()
                self.assertEqual(write_status.call_args.kwargs["exit_code"], 124)
                self.assertIs(write_status.call_args.kwargs["timed_out"], True)
                self.assertEqual(write_status.call_args.kwargs["timeout_sec"], 0.5)
                for path in (*pid_paths, parent_marker, child_marker):
                    self.assertTrue(path.exists(), f"process fixture did not create {path.name}")

                pids = [int(path.read_text(encoding="utf-8")) for path in pid_paths]
                for pid in pids:
                    self.assertTrue(wait_for_process_exit(pid), f"process {pid} survived timeout cleanup")

                marker_sizes = {path: path.stat().st_size for path in (parent_marker, child_marker)}
                self.assertTrue(all(size > 0 for size in marker_sizes.values()))
                time.sleep(0.2)
                self.assertEqual(
                    {path: path.stat().st_size for path in marker_sizes},
                    marker_sizes,
                    "a timed-out process continued writing after run_claude_logged returned",
                )
            finally:
                force_kill_processes(pid_paths)


class ClaudePermissionPolicyTests(unittest.TestCase):
    def matching_rules(self, command: str) -> list[str]:
        return [
            rule
            for rule in CLAUDE_PERMISSION_DENY_RULES
            if pinned_claude_bash_permission_match(rule, command)
        ]

    def test_deny_rules_are_unique_and_keep_bash_available(self) -> None:
        self.assertEqual(
            len(CLAUDE_PERMISSION_DENY_RULES),
            len(set(CLAUDE_PERMISSION_DENY_RULES)),
        )
        for rule in CLAUDE_PERMISSION_DENY_RULES:
            with self.subTest(rule=rule):
                self.assertTrue(rule.startswith("Bash("))
                self.assertTrue(rule.endswith(")"))
                self.assertNotEqual(rule, "Bash(*)")
        self.assertNotIn("Bash", CLAUDE_PERMISSION_DENY_RULES)

    def test_deny_rules_cover_full_filesystem_parent_and_worktree_commands(self) -> None:
        commands = (
            "find /",
            "find / -xdev -type f",
            "/usr/bin/find / -maxdepth 2",
            "find -L / -type f",
            "find -- /",
            "grep -R needle /",
            "grep -r needle / --exclude-dir proc",
            "grep --recursive needle /",
            "grep needle -R /",
            "grep needle / -R",
            "rg needle /",
            "rg --files /",
            "du /",
            "du -ah /",
            "ls -R /",
            "ls -laR /",
            "ls --recursive /",
            "ls / -R",
            "ls -l / --recursive",
            "find ..",
            "find ../sibling -type f",
            "find -L .. -type f",
            "grep -R needle ..",
            "grep --recursive needle ../sibling",
            "rg --files ..",
            "rg needle ../sibling",
            "du ..",
            "du -sh ../sibling",
            "ls -R ..",
            "ls --recursive ../sibling",
            "ls .. -R",
            "ls -l ../sibling --recursive",
            "locate",
            "locate settings.json",
            "git worktree list",
            "env git worktree add ../agent",
            "/usr/bin/git worktree remove ../agent",
            "git -C . worktree add ../agent",
            "git --no-pager worktree list",
            "git -c foo.bar=baz worktree add ../agent",
            "git -C . --no-pager worktree list",
            "/usr/bin/git --git-dir=.git worktree list",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(
                    self.matching_rules(command),
                    f"no deny rule matched {command!r}",
                )

    def test_deny_rules_do_not_block_normal_workspace_commands(self) -> None:
        commands = (
            "pwd",
            "find . -maxdepth 2 -type f",
            "find inputs -type f",
            "grep -R needle .",
            "grep --recursive needle inputs",
            "rg needle .",
            "rg --files .",
            "du -sh .",
            "ls -R output",
            "ls output -R",
            "git status",
            "git --no-pager status",
            "git -C . status",
            "git log worktree",
            "git diff -- worktree",
            "git show worktree",
            "git worktree-helper --help",
            "python3 helper.py",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(
                    self.matching_rules(command),
                    [],
                    f"workspace command was unexpectedly denied: {command!r}",
                )


class ClaudeWorktreeGuardTests(unittest.TestCase):
    def run_guard(self, hook_input: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(WORKTREE_GUARD_SCRIPT)],
            input=json.dumps(hook_input),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_agent_worktree_isolation_is_removed_without_changing_other_input(self) -> None:
        tool_input = {
            "isolation": "worktree",
            "description": "research",
            "model": "sonnet",
            "run_in_background": False,
            "subagent_type": "seed-brainstormer",
            "future_field": {"nested": [1, 2, 3]},
        }
        result = self.run_guard(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Agent",
                "tool_input": tool_input,
            }
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        output = payload["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], "allow")
        expected_input = dict(tool_input)
        expected_input.pop("isolation")
        self.assertEqual(output["updatedInput"], expected_input)
        self.assertIn("existing isolated workspace", output["additionalContext"])

    def test_legacy_task_isolation_is_also_removed(self) -> None:
        result = self.run_guard(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Task",
                "tool_input": {"isolation": "worktree", "prompt": "work"},
            }
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["updatedInput"], {"prompt": "work"})

    def test_agent_without_isolation_and_unrelated_tools_are_unchanged(self) -> None:
        for tool_name, tool_input in (
            ("Agent", {"description": "research"}),
            ("Bash", {"command": "pwd"}),
        ):
            with self.subTest(tool_name=tool_name):
                result = self.run_guard(
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_name": tool_name,
                        "tool_input": tool_input,
                    }
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")

    def test_enter_worktree_is_denied(self) -> None:
        result = self.run_guard(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "EnterWorktree",
                "tool_input": {},
            }
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("worktree is disabled", output["permissionDecisionReason"])

    def test_worktree_create_fails_closed(self) -> None:
        result = self.run_guard(
            {
                "hook_event_name": "WorktreeCreate",
                "name": "agent-test",
            }
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("blocks Claude Code worktree creation", result.stderr)

    def test_malformed_hook_input_fails_closed(self) -> None:
        result = subprocess.run(
            [sys.executable, str(WORKTREE_GUARD_SCRIPT)],
            input="not-json",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("rejected malformed hook input", result.stderr)

    def test_incomplete_hook_input_fails_closed(self) -> None:
        inputs = (
            {},
            {"hook_event_name": "Unknown"},
            {"hook_event_name": "PreToolUse"},
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Agent",
                "tool_input": [],
            },
        )
        for hook_input in inputs:
            with self.subTest(hook_input=hook_input):
                result = self.run_guard(hook_input)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("rejected malformed hook input", result.stderr)


class PipelineTests(unittest.TestCase):
    def test_loads_pipeline_idea_ids_from_brainstorm(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            brainstorm_dir = project / "runs/brainstorm/seed-a"
            brainstorm_dir.mkdir(parents=True)
            (brainstorm_dir / "seed_brainstorm.json").write_text(
                json.dumps(
                    {
                        "seed_id": "seed-a",
                        "ideas": [
                            {"idea_id": "idea-1"},
                            {"idea_id": "idea-2"},
                            {"idea_id": "idea-1"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(load_pipeline_idea_ids(project, "seed-a"), ["idea-1", "idea-2"])
            self.assertTrue(pipeline_phase1_count_matches(project, "seed-a", 2))
            self.assertFalse(pipeline_phase1_count_matches(project, "seed-a", 3))

    def test_builds_phase1_command_with_idea_count(self) -> None:
        command = build_phase_process_command(
            get_phase("phase1"),
            "run",
            "seed-a",
            idea_count=4,
        )

        self.assertEqual(command[-2:], ["--idea-count", "4"])

    def test_rejects_idea_count_for_non_phase1_command(self) -> None:
        with self.assertRaises(SystemExit):
            build_phase_process_command(
                get_phase("phase2"),
                "run",
                "seed-a",
                idea_count=4,
            )

    def test_incomplete_pipeline_dry_run_returns_nonzero(self) -> None:
        with (
            tempfile.TemporaryDirectory() as project_tmp,
            patch("taskgen.cli.project_root", return_value=Path(project_tmp)),
            patch(
                "taskgen.cli.run_or_skip_phase",
                side_effect=[
                    PhaseRunResult(exit_code=0, ran=True),
                    PhaseRunResult(exit_code=0, ran=True),
                ],
            ),
        ):
            exit_code = command_pipeline(
                SimpleNamespace(
                    seed_id="seed-a",
                    idea_id=None,
                    idea_count=None,
                    max_repairs=1,
                    force=False,
                    dry_run=True,
                    model=None,
                    effort=None,
                )
            )

        self.assertEqual(exit_code, 1)


class CommonHardeningTests(unittest.TestCase):
    def test_phase_subject_lock_is_reentrant_in_same_thread(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            reached_inner = False

            with phase_subject_lock(project, "phase1", "seed-a"):
                with phase_subject_lock(project, "phase2", "seed-a"):
                    reached_inner = True

            self.assertTrue(reached_inner)
            self.assertEqual(len(list((project / "runs/locks").glob("subject-*.lock"))), 1)

    def test_subject_lock_delegation_passes_actual_descriptors_through_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            with phase_subject_lock(project, "phase1", "seed-a"):
                delegated = delegated_phase_subject_lock_kwargs(project, "seed-a")
                environment = delegated["env"]
                descriptors = delegated["pass_fds"]
                existing_pythonpath = environment.get("PYTHONPATH")
                environment["PYTHONPATH"] = str(SRC) + (
                    f":{existing_pythonpath}" if existing_pythonpath else ""
                )
                child_code = (
                    "import sys\n"
                    "from pathlib import Path\n"
                    "from taskgen.common import phase_subject_lock_delegated_by_parent\n"
                    "root = Path(sys.argv[1])\n"
                    "valid = phase_subject_lock_delegated_by_parent(root, 'seed-a')\n"
                    "try:\n"
                    "    phase_subject_lock_delegated_by_parent(root, 'seed-b')\n"
                    "except RuntimeError:\n"
                    "    wrong_rejected = True\n"
                    "else:\n"
                    "    wrong_rejected = False\n"
                    "raise SystemExit(0 if valid and wrong_rejected else 1)\n"
                )
                completed = subprocess.run(
                    [
                        "bash",
                        "-c",
                        'exec "$@"',
                        "bash",
                        sys.executable,
                        "-c",
                        child_code,
                        str(project),
                    ],
                    check=False,
                    capture_output=True,
                    env=environment,
                    pass_fds=descriptors,
                )

                self.assertEqual(completed.returncode, 0, completed.stderr.decode())
                self.assertEqual(len(descriptors), 2)
                for descriptor in descriptors:
                    os.fstat(descriptor)

    def test_invalid_subject_lock_delegation_fails_fast(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp, patch.dict(
            os.environ,
            {"_TASKGEN_PARENT_PHASE_SUBJECT_LOCK": "{}"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid inherited"):
                phase_subject_lock_delegated_by_parent(Path(project_tmp), "seed-a")

    def test_manifest_unlock_failure_after_fsync_does_not_report_append_failure(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            manifest = Path(project_tmp) / "runs/task-manifest.jsonl"

            def fail_only_unlock(_fd: int, operation: int) -> None:
                import fcntl

                if operation == fcntl.LOCK_UN:
                    raise OSError("simulated unlock failure")

            with patch("taskgen.common.fcntl.flock", side_effect=fail_only_unlock):
                append_jsonl_object(manifest, {"event": "accepted"})

            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["event"], "accepted")


class ClaudeWorkspaceTests(unittest.TestCase):
    def test_rejects_path_traversal_subject(self) -> None:
        with self.assertRaises(SystemExit):
            phase_input_paths("seed-brainstorm", "../outside")

    def test_rejects_unknown_phase(self) -> None:
        with self.assertRaises(SystemExit):
            phase_input_paths("unknown-phase", "subject")

    def test_prepare_rejects_prompt_outside_project(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp, tempfile.TemporaryDirectory() as outside_tmp:
            project = Path(project_tmp)
            outside_prompt = Path(outside_tmp) / "prompt.md"
            outside_prompt.write_text("prompt", encoding="utf-8")

            with self.assertRaises(SystemExit):
                prepare_workspace(project, "seed-brainstorm", "seed-a", outside_prompt, "run-1")

    def test_prepare_copies_claude_definitions_to_workspace_only(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            (project / "prompts").mkdir()
            prompt = project / "prompts/seed-brainstorm.md"
            prompt.write_text("prompt", encoding="utf-8")
            (project / "seeds/seed-a").mkdir(parents=True)
            (project / "cc-definitions/agents").mkdir(parents=True)
            (project / "cc-definitions/agents/seed-brainstormer.md").write_text(
                "agent",
                encoding="utf-8",
            )
            (project / "cc-definitions/skills/demo-skill").mkdir(parents=True)
            (project / "cc-definitions/skills/demo-skill/SKILL.md").write_text(
                "skill",
                encoding="utf-8",
            )

            payload = prepare_workspace(project, "seed-brainstorm", "seed-a", prompt, "run-1")
            workspace = Path(str(payload["workspace_dir"]))
            runtime = Path(str(payload["claude_config_dir"]))
            settings_path = Path(str(payload["claude_settings_path"]))
            guard_path = Path(str(payload["worktree_guard_path"]))

            self.assertTrue((workspace / ".claude/agents/seed-brainstormer.md").is_file())
            self.assertTrue((workspace / ".claude/skills/demo-skill/SKILL.md").is_file())
            self.assertFalse((runtime / "skills").exists())
            self.assertEqual(settings_path, workspace / ".claude/settings.json")
            self.assertEqual(guard_path.parent, workspace / ".claude/hooks")
            self.assertTrue(settings_path.is_file())
            self.assertTrue(guard_path.is_file())
            self.assertFalse(settings_path.is_symlink())
            self.assertFalse(guard_path.is_symlink())
            self.assertEqual(settings_path.stat().st_mode & 0o022, 0)
            self.assertEqual(guard_path.stat().st_mode & 0o022, 0)
            self.assertFalse((workspace / ".claude/settings.local.json").exists())
            self.assertFalse((project / ".claude").exists())

            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(set(settings), {"permissions", "hooks"})
            self.assertEqual(set(settings["permissions"]), {"deny"})
            deny_rules = settings["permissions"]["deny"]
            self.assertEqual(deny_rules, list(CLAUDE_PERMISSION_DENY_RULES))
            for rule in (
                "Bash(*find * /)",
                "Bash(*find * / *)",
                "Bash(*grep * /)",
                "Bash(*rg * / *)",
                "Bash(*du * / *)",
                "Bash(*ls *--recursive* / *)",
                "Bash(*find * ..)",
                "Bash(*find * ../*)",
                "Bash(*grep * ../*)",
                "Bash(*rg * ../*)",
                "Bash(*du * ../*)",
                "Bash(*ls -*R* ../*)",
                "Bash(*/git worktree *)",
                "Bash(git -* worktree *)",
            ):
                self.assertIn(rule, deny_rules)
            self.assertNotIn("EnterWorktree", deny_rules)
            pre_tool_use = settings["hooks"]["PreToolUse"]
            self.assertEqual(len(pre_tool_use), 1)
            self.assertEqual(pre_tool_use[0]["matcher"], "Agent|Task|EnterWorktree")
            handler = pre_tool_use[0]["hooks"][0]
            self.assertEqual(
                handler,
                {
                    "type": "command",
                    "command": sys.executable,
                    "args": [str(guard_path)],
                    "timeout": 5,
                },
            )
            self.assertEqual(
                settings["hooks"]["WorktreeCreate"][0]["hooks"][0],
                handler,
            )

    def test_generated_guard_enforces_policy_from_path_with_special_characters(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp) / "project with spaces [guard]"
            (project / "prompts").mkdir(parents=True)
            prompt = project / "prompts/seed-brainstorm.md"
            prompt.write_text("prompt", encoding="utf-8")
            (project / "seeds/seed-a").mkdir(parents=True)

            payload = prepare_workspace(project, "seed-brainstorm", "seed-a", prompt, "run-1")
            workspace = Path(str(payload["workspace_dir"]))
            settings_path = Path(str(payload["claude_settings_path"]))
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            pre_tool_handler = settings["hooks"]["PreToolUse"][0]["hooks"][0]
            worktree_create_handler = settings["hooks"]["WorktreeCreate"][0]["hooks"][0]

            def run_handler(
                handler: dict[str, object],
                hook_input: dict[str, object],
            ) -> subprocess.CompletedProcess[str]:
                command = handler["command"]
                arguments = handler["args"]
                self.assertIsInstance(command, str)
                self.assertIsInstance(arguments, list)
                return subprocess.run(
                    [command, *arguments],
                    input=json.dumps(hook_input),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=workspace,
                    check=False,
                )

            agent_result = run_handler(
                pre_tool_handler,
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Agent",
                    "tool_input": {"isolation": "worktree", "prompt": "work"},
                },
            )
            self.assertEqual(agent_result.returncode, 0, agent_result.stderr)
            agent_output = json.loads(agent_result.stdout)["hookSpecificOutput"]
            self.assertEqual(agent_output["permissionDecision"], "allow")
            self.assertEqual(agent_output["updatedInput"], {"prompt": "work"})

            enter_result = run_handler(
                pre_tool_handler,
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "EnterWorktree",
                    "tool_input": {},
                },
            )
            self.assertEqual(enter_result.returncode, 0, enter_result.stderr)
            enter_output = json.loads(enter_result.stdout)["hookSpecificOutput"]
            self.assertEqual(enter_output["permissionDecision"], "deny")

            unrelated_result = run_handler(
                pre_tool_handler,
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_input": {"command": "pwd"},
                },
            )
            self.assertEqual(unrelated_result.returncode, 0, unrelated_result.stderr)
            self.assertEqual(unrelated_result.stdout, "")

            create_result = run_handler(
                worktree_create_handler,
                {"hook_event_name": "WorktreeCreate", "name": "agent-test"},
            )
            self.assertEqual(create_result.returncode, 2)
            self.assertIn("blocks Claude Code worktree creation", create_result.stderr)

    def test_skillnet_research_outputs_seed_directory(self) -> None:
        self.assertEqual(
            phase_output_paths("skillnet-research", "seed-a"),
            [("output/skillnet", "runs/skillnet/seed-a")],
        )

    def test_repair_outputs_working_task(self) -> None:
        self.assertEqual(
            phase_output_paths("task-repair", "seed-a__idea-1"),
            [("output/task", "generated/working/seed-a/idea-1")],
        )

    def test_task_finalize_is_not_a_claude_workspace_phase(self) -> None:
        with self.assertRaises(SystemExit):
            phase_output_paths("task-finalize", "seed-a__idea-1")

    def test_task_generation_merges_generated_skill_packages(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            (project / "prompts").mkdir()
            prompt = project / "prompts/task-generation.md"
            prompt.write_text("prompt", encoding="utf-8")
            (project / "seeds/seed-a").mkdir(parents=True)
            (project / "runs/brainstorm/seed-a").mkdir(parents=True)
            (project / "runs/brainstorm/seed-a/seed_brainstorm.json").write_text("{}", encoding="utf-8")
            (project / "runs/skillnet/seed-a/idea-1/skills/taskgen-idea-1-demo").mkdir(parents=True)
            (project / "runs/skillnet/seed-a/idea-1/skills/taskgen-idea-1-demo/SKILL.md").write_text(
                "---\nname: taskgen-idea-1-demo\ndescription: demo\n---\n\nUse this demo skill.\n",
                encoding="utf-8",
            )
            (project / "runs/skillnet/seed-a/idea-1/skills/taskgen-idea-1-demo/references").mkdir()
            (project / "runs/skillnet/seed-a/idea-1/skills/taskgen-idea-1-demo/references/note.md").write_text(
                "reference",
                encoding="utf-8",
            )
            (project / "runs/skillnet/seed-a/idea-1/skill_summary.json").write_text("{}", encoding="utf-8")
            (project / "cc-definitions/skills/tb-harbor-task-generation").mkdir(parents=True)
            (project / "cc-definitions/skills/tb-harbor-task-generation/SKILL.md").write_text(
                "---\nname: tb-harbor-task-generation\ndescription: base\n---\n",
                encoding="utf-8",
            )

            payload = prepare_workspace(project, "task-generation", "seed-a__idea-1", prompt, "run-1")
            workspace = Path(str(payload["workspace_dir"]))

            self.assertTrue(
                (workspace / ".claude/skills/tb-harbor-task-generation/SKILL.md").is_file()
            )
            self.assertTrue(
                (workspace / ".claude/skills/taskgen-idea-1-demo/SKILL.md").is_file()
            )
            self.assertTrue(
                (workspace / ".claude/skills/taskgen-idea-1-demo/references/note.md").is_file()
            )
            self.assertFalse((workspace / "skillnet/seed-a/idea-1/raw").exists())

    def test_task_generation_allows_empty_generated_skill_packages(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            (project / "prompts").mkdir()
            prompt = project / "prompts/task-generation.md"
            prompt.write_text("prompt", encoding="utf-8")
            (project / "seeds/seed-a").mkdir(parents=True)
            (project / "runs/brainstorm/seed-a").mkdir(parents=True)
            (project / "runs/brainstorm/seed-a/seed_brainstorm.json").write_text("{}", encoding="utf-8")
            (project / "runs/skillnet/seed-a/idea-1/skills").mkdir(parents=True)
            (project / "runs/skillnet/seed-a/idea-1/skill_summary.json").write_text("{}", encoding="utf-8")
            (project / "cc-definitions/skills/tb-harbor-task-generation").mkdir(parents=True)
            (project / "cc-definitions/skills/tb-harbor-task-generation/SKILL.md").write_text(
                "---\nname: tb-harbor-task-generation\ndescription: base\n---\n",
                encoding="utf-8",
            )

            payload = prepare_workspace(project, "task-generation", "seed-a__idea-1", prompt, "run-1")
            workspace = Path(str(payload["workspace_dir"]))

            self.assertEqual(payload["generated_skill_packages"], [])
            self.assertTrue((workspace / ".claude/skills/tb-harbor-task-generation/SKILL.md").is_file())

    def test_task_generation_validation_artifacts_stay_in_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            workspace = project / "runs/workspace/task-generation/seed-a__idea-1/run-1"
            (workspace / "output/task").mkdir(parents=True)
            (workspace / "output/task/instruction.md").write_text("Task", encoding="utf-8")
            (workspace / "output/phase3-validation").mkdir(parents=True)
            (workspace / "output/phase3-validation/oracle.log").write_text("log", encoding="utf-8")

            synced = sync_workspace_outputs(project, workspace, "task-generation", "seed-a__idea-1")

            self.assertEqual(synced, ["generated/working/seed-a/idea-1"])
            self.assertTrue((project / "generated/working/seed-a/idea-1/instruction.md").is_file())
            self.assertFalse((project / "generated/working/seed-a/idea-1/phase3-validation").exists())
            self.assertTrue((workspace / "output/phase3-validation/oracle.log").is_file())

    def test_review_and_repair_prompts_are_rendered_with_concrete_ids(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            (project / "prompts").mkdir()
            (project / "prompts/task-review.md").write_text(
                "Read task/<seed_id>/<idea_id> and oracle-nop-check/<task_id>.",
                encoding="utf-8",
            )
            (project / "prompts/task-repair.md").write_text(
                "Repair from review/<task_id> for task/<seed_id>/<idea_id>.",
                encoding="utf-8",
            )

            review_prompt = render_phase5_prompt(project, "seed-a", "idea-1")
            repair_prompt = render_phase6_prompt(project, "seed-a", "idea-1")

            self.assertEqual(
                review_prompt.read_text(encoding="utf-8"),
                "Read task/seed-a/idea-1 and oracle-nop-check/seed-a__idea-1.",
            )
            self.assertEqual(
                repair_prompt.read_text(encoding="utf-8"),
                "Repair from review/seed-a__idea-1 for task/seed-a/idea-1.",
            )

    def test_missing_declared_output_preserves_existing_project_output(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            workspace = project / "runs/workspace/seed-brainstorm/seed-a/run-1"
            workspace.mkdir(parents=True)
            destination = project / "runs/brainstorm/seed-a/seed_brainstorm.json"
            destination.parent.mkdir(parents=True)
            destination.write_text("old-result", encoding="utf-8")

            with self.assertRaises(MissingWorkspaceOutputsError) as raised:
                sync_workspace_outputs(project, workspace, "seed-brainstorm", "seed-a")

            self.assertEqual(raised.exception.missing_outputs, ["output/seed_brainstorm.json"])
            self.assertEqual(destination.read_text(encoding="utf-8"), "old-result")

    def test_interrupted_output_sync_restores_backup_before_missing_output_failure(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            workspace = project / "runs/workspace/seed-brainstorm/seed-a/run-2"
            workspace.mkdir(parents=True)
            destination = project / "runs/brainstorm/seed-a/seed_brainstorm.json"
            destination.parent.mkdir(parents=True)
            destination.write_text("old-result", encoding="utf-8")
            token = "123-" + "a" * 32
            stage = destination.parent / f".taskgen-output-stage-{token}-0"
            backup = destination.parent / f".taskgen-output-backup-{token}-0"
            stage.write_text("partial-new-result", encoding="utf-8")
            os.replace(destination, backup)
            journal = output_sync_journal_path(project, "seed-brainstorm", "seed-a")
            journal.parent.mkdir(parents=True)
            journal.write_text(
                json.dumps(
                    {
                        "phase": "seed-brainstorm",
                        "subject": "seed-a",
                        "state": "staged",
                        "token": token,
                        "records": [
                            {
                                "project_rel_path": "runs/brainstorm/seed-a/seed_brainstorm.json",
                                "destination": str(destination),
                                "stage": str(stage),
                                "backup": str(backup),
                                "destination_existed": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(MissingWorkspaceOutputsError):
                sync_workspace_outputs(project, workspace, "seed-brainstorm", "seed-a")

            self.assertEqual(destination.read_text(encoding="utf-8"), "old-result")
            self.assertFalse(stage.exists())
            self.assertFalse(backup.exists())
            self.assertFalse(journal.exists())

    def test_committed_output_sync_restores_backup_when_destination_digest_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            workspace = project / "runs/workspace/seed-brainstorm/seed-a/run-2"
            workspace.mkdir(parents=True)
            destination = project / "runs/brainstorm/seed-a/seed_brainstorm.json"
            destination.parent.mkdir(parents=True)
            destination.write_text("corrupted-new-result", encoding="utf-8")
            token = "123-" + "c" * 32
            stage = destination.parent / f".taskgen-output-stage-{token}-0"
            backup = destination.parent / f".taskgen-output-backup-{token}-0"
            backup.write_text("old-result", encoding="utf-8")
            journal = output_sync_journal_path(project, "seed-brainstorm", "seed-a")
            journal.parent.mkdir(parents=True)
            journal.write_text(
                json.dumps(
                    {
                        "phase": "seed-brainstorm",
                        "subject": "seed-a",
                        "state": "committed",
                        "token": token,
                        "records": [
                            {
                                "project_rel_path": "runs/brainstorm/seed-a/seed_brainstorm.json",
                                "destination": str(destination),
                                "stage": str(stage),
                                "backup": str(backup),
                                "destination_existed": True,
                                "output_sha256": "0" * 64,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(MissingWorkspaceOutputsError):
                sync_workspace_outputs(project, workspace, "seed-brainstorm", "seed-a")

            self.assertEqual(destination.read_text(encoding="utf-8"), "old-result")
            self.assertFalse(backup.exists())
            self.assertFalse(journal.exists())

    def test_multi_output_recovery_does_not_partially_roll_back_without_every_backup(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            first = project / "out/first.json"
            second = project / "out/second.json"
            first.parent.mkdir(parents=True)
            first.write_text("new-first", encoding="utf-8")
            second.write_text("new-second", encoding="utf-8")
            token = "123-" + "e" * 32
            first_stage = first.parent / f".taskgen-output-stage-{token}-0"
            second_stage = second.parent / f".taskgen-output-stage-{token}-1"
            first_backup = first.parent / f".taskgen-output-backup-{token}-0"
            second_backup = second.parent / f".taskgen-output-backup-{token}-1"
            first_backup.write_text("old-first", encoding="utf-8")
            journal = output_sync_journal_path(project, "test-phase", "subject")
            journal.parent.mkdir(parents=True)
            journal.write_text(
                json.dumps(
                    {
                        "phase": "test-phase",
                        "subject": "subject",
                        "state": "committed",
                        "token": token,
                        "records": [
                            {
                                "project_rel_path": "out/first.json",
                                "destination": str(first),
                                "stage": str(first_stage),
                                "backup": str(first_backup),
                                "destination_existed": True,
                                "output_sha256": "0" * 64,
                            },
                            {
                                "project_rel_path": "out/second.json",
                                "destination": str(second),
                                "stage": str(second_stage),
                                "backup": str(second_backup),
                                "destination_existed": True,
                                "output_sha256": output_path_sha256(second),
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            destinations = [
                {"project_rel_path": "out/first.json", "destination": first},
                {"project_rel_path": "out/second.json", "destination": second},
            ]

            with self.assertRaisesRegex(WorkspaceOutputError, "cannot atomically roll back"):
                recover_interrupted_output_sync(
                    project,
                    "test-phase",
                    "subject",
                    destinations,
                )

            self.assertEqual(first.read_text(encoding="utf-8"), "new-first")
            self.assertEqual(second.read_text(encoding="utf-8"), "new-second")
            self.assertEqual(first_backup.read_text(encoding="utf-8"), "old-first")
            self.assertTrue(journal.is_file())

    def test_publish_failure_rolls_back_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            workspace = project / "runs/workspace/seed-brainstorm/seed-a/run-1"
            source = workspace / "output/seed_brainstorm.json"
            source.parent.mkdir(parents=True)
            source.write_text("new-result", encoding="utf-8")
            destination = project / "runs/brainstorm/seed-a/seed_brainstorm.json"
            destination.parent.mkdir(parents=True)
            destination.write_text("old-result", encoding="utf-8")
            real_replace = os.replace

            def fail_stage_install(source_path: object, destination_path: object) -> None:
                source_candidate = Path(os.fspath(source_path))
                destination_candidate = Path(os.fspath(destination_path))
                if (
                    source_candidate.name.startswith(".taskgen-output-stage-")
                    and destination_candidate == destination
                ):
                    raise OSError("simulated publish failure")
                real_replace(source_path, destination_path)

            with (
                patch("taskgen.claude.workspace.os.replace", side_effect=fail_stage_install),
                self.assertRaisesRegex(WorkspaceOutputError, "failed to publish"),
            ):
                sync_workspace_outputs(project, workspace, "seed-brainstorm", "seed-a")

            self.assertEqual(destination.read_text(encoding="utf-8"), "old-result")
            self.assertEqual(list(destination.parent.glob(".taskgen-*")), [])

    def test_stage_fsync_failure_preserves_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            workspace = project / "runs/workspace/seed-brainstorm/seed-a/run-1"
            source = workspace / "output/seed_brainstorm.json"
            source.parent.mkdir(parents=True)
            source.write_text("new-result", encoding="utf-8")
            destination = project / "runs/brainstorm/seed-a/seed_brainstorm.json"
            destination.parent.mkdir(parents=True)
            destination.write_text("old-result", encoding="utf-8")

            with (
                patch(
                    "taskgen.claude.workspace.fsync_path_tree",
                    side_effect=OSError("simulated fsync failure"),
                ),
                self.assertRaisesRegex(WorkspaceOutputError, "failed to stage"),
            ):
                sync_workspace_outputs(project, workspace, "seed-brainstorm", "seed-a")

            self.assertEqual(destination.read_text(encoding="utf-8"), "old-result")
            self.assertEqual(list(destination.parent.glob(".taskgen-*")), [])
            self.assertFalse(output_sync_journal_path(project, "seed-brainstorm", "seed-a").exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "symbolic links are unavailable")
    def test_workspace_output_symlink_is_rejected_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            workspace = project / "runs/workspace/seed-brainstorm/seed-a/run-1"
            source = workspace / "output/seed_brainstorm.json"
            source.parent.mkdir(parents=True)
            actual = workspace / "actual.json"
            actual.write_text("new-result", encoding="utf-8")
            source.symlink_to(actual)
            destination = project / "runs/brainstorm/seed-a/seed_brainstorm.json"
            destination.parent.mkdir(parents=True)
            destination.write_text("old-result", encoding="utf-8")

            with self.assertRaisesRegex(WorkspaceOutputError, "symbolic links are not allowed"):
                sync_workspace_outputs(project, workspace, "seed-brainstorm", "seed-a")

            self.assertEqual(destination.read_text(encoding="utf-8"), "old-result")

    def test_subject_ids_reject_reserved_separator_dot_segments_and_excessive_length(self) -> None:
        invalid_calls = (
            ("seed-brainstorm", "."),
            ("seed-brainstorm", ".."),
            ("seed-brainstorm", "seed__part"),
            ("seed-brainstorm", "s" * 129),
            ("task-generation", "seed-a__."),
            ("task-generation", "seed-a__.."),
            ("task-generation", "seed-a__idea__part"),
            ("task-generation", f"seed-a__{'i' * 121}"),
        )
        for phase, subject in invalid_calls:
            with self.subTest(phase=phase, subject=subject), self.assertRaises(SystemExit):
                phase_output_paths(phase, subject)

    def test_prepare_rejects_reused_or_invalid_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            prompt = project / "prompts/seed-brainstorm.md"
            prompt.parent.mkdir()
            prompt.write_text("prompt", encoding="utf-8")
            (project / "seeds/seed-a").mkdir(parents=True)

            for invalid_run_id in (".", "..", "r" * 129):
                with self.subTest(run_id=invalid_run_id), self.assertRaises(SystemExit):
                    prepare_workspace(
                        project,
                        "seed-brainstorm",
                        "seed-a",
                        prompt,
                        invalid_run_id,
                    )

            existing = project / "runs/claude-sessions/seed-brainstorm/seed-a/run-1"
            existing.mkdir(parents=True)
            with self.assertRaisesRegex(SystemExit, "already exists"):
                prepare_workspace(project, "seed-brainstorm", "seed-a", prompt, "run-1")


class ClaudeSessionReferenceTests(unittest.TestCase):
    def test_valid_session_requires_exact_success_status_and_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            session_ref = write_fake_claude_session(
                project,
                "task-review",
                "seed-a__idea-1",
            )
            expected_outputs = ["runs/reviews/seed-a__idea-1"]

            self.assertEqual(
                validate_claude_session_reference(
                    project,
                    session_ref,
                    expected_phase="task-review",
                    expected_subject="seed-a__idea-1",
                    expected_outputs=expected_outputs,
                ),
                [],
            )

            status_path = project / session_ref / "status.json"
            status = json.loads(status_path.read_text(encoding="utf-8"))
            status.update(
                {
                    "exit_code": 1,
                    "timed_out": True,
                    "synced_outputs": ["generated/working/seed-a/idea-1"],
                }
            )
            status_path.write_text(json.dumps(status), encoding="utf-8")

            errors = validate_claude_session_reference(
                project,
                session_ref,
                expected_phase="task-review",
                expected_subject="seed-a__idea-1",
                expected_outputs=expected_outputs,
            )
            combined = "\n".join(errors)
            self.assertIn("exit_code must be 0", combined)
            self.assertIn("timed_out must be false", combined)
            self.assertIn("synced_outputs must exactly equal", combined)

    @unittest.skipUnless(hasattr(os, "symlink"), "symbolic links are unavailable")
    def test_session_reference_cannot_escape_expected_session_root(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp, tempfile.TemporaryDirectory() as outside_tmp:
            project = Path(project_tmp)
            outside = Path(outside_tmp)
            (outside / "status.json").write_text("{}", encoding="utf-8")
            link = project / "runs/claude-sessions/task-review/seed-a__idea-1/run-1"
            link.parent.mkdir(parents=True)
            link.symlink_to(outside, target_is_directory=True)

            errors = validate_claude_session_reference(
                project,
                link.relative_to(project).as_posix(),
                expected_phase="task-review",
                expected_subject="seed-a__idea-1",
                expected_outputs=["runs/reviews/seed-a__idea-1"],
            )

            self.assertIn("must stay inside the project", "\n".join(errors))


class ClaudeCostTests(unittest.TestCase):
    def test_parse_result_cost_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "claude-code.txt"
            log_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "system",
                                "subtype": "init",
                                "session_id": "session-1",
                                "model": "test/model",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "result",
                                "subtype": "success",
                                "session_id": "session-1",
                                "total_cost_usd": 0.12,
                                "usage": {"input_tokens": 10, "output_tokens": 5, "cost": 0.12},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            summary = parse_claude_stream_log(log_path)

            self.assertEqual(summary["session_id"], "session-1")
            self.assertEqual(summary["model"], "test/model")
            self.assertEqual(summary["total_cost_usd"], 0.12)
            self.assertEqual(summary["provider_usage_cost"], 0.12)
            self.assertEqual(summary["usage"]["input_tokens"], 10)

    def test_parse_openrouter_generation_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "claude-code.txt"
            log_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "assistant",
                                "message": {
                                    "id": "gen-1",
                                    "usage": {"input_tokens": 0, "output_tokens": 0},
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "assistant",
                                "message": {
                                    "id": "gen-1",
                                    "usage": {"input_tokens": 0, "output_tokens": 0},
                                },
                            }
                        ),
                        json.dumps({"type": "assistant", "message_id": "gen-2"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            summary = parse_claude_stream_log(log_path)

            self.assertEqual(summary["openrouter_generation_ids"], ["gen-1", "gen-2"])
            self.assertEqual(summary["openrouter_generation_count"], 2)

    def test_openrouter_generation_query_overrides_stream_cost_when_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "claude-code.txt"
            log_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "system",
                                "subtype": "init",
                                "session_id": "session-1",
                                "model": "configured/model",
                            }
                        ),
                        json.dumps({"type": "assistant", "message": {"id": "gen-1"}}),
                        json.dumps({"type": "assistant", "message": {"id": "gen-2"}}),
                        json.dumps(
                            {
                                "type": "result",
                                "subtype": "success",
                                "total_cost_usd": 9.99,
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            def fake_fetch(generation_id: str, api_key: str) -> dict[str, object]:
                self.assertEqual(api_key, "test-key")
                costs = {"gen-1": 0.25, "gen-2": 0.75}
                return {
                    "id": generation_id,
                    "model": "served/model",
                    "provider_name": "OpenRouterProvider",
                    "native_tokens_prompt": 10,
                    "native_tokens_completion": 5,
                    "total_cost": costs[generation_id],
                }

            summary = summarize_claude_stream_log(
                log_path,
                openrouter_api_key="test-key",
                fetch_openrouter_generation=fake_fetch,
            )

            self.assertEqual(summary["claude_stream_total_cost_usd"], 9.99)
            self.assertEqual(summary["total_cost_usd"], 1.0)
            self.assertEqual(summary["cost_source"], "openrouter_generation_api")
            self.assertEqual(summary["openrouter"]["successful_generation_count"], 2)
            self.assertEqual(summary["openrouter"]["failed_generation_count"], 0)
            self.assertEqual(summary["openrouter"]["totals"]["native_tokens_prompt"], 20)
            self.assertEqual(summary["openrouter"]["by_model"]["served/model"]["generation_count"], 2)

    def test_openrouter_partial_query_keeps_stream_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "claude-code.txt"
            log_path.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "assistant", "message": {"id": "gen-1"}}),
                        json.dumps({"type": "assistant", "message": {"id": "gen-2"}}),
                        json.dumps(
                            {
                                "type": "result",
                                "subtype": "success",
                                "total_cost_usd": 9.99,
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            def fake_fetch(generation_id: str, api_key: str) -> dict[str, object]:
                del api_key
                if generation_id == "gen-2":
                    raise OpenRouterQueryError("not ready", 404)
                return {
                    "id": generation_id,
                    "model": "served/model",
                    "provider_name": "OpenRouterProvider",
                    "total_cost": 0.25,
                }

            with patch.dict(os.environ, {"TASKGEN_OPENROUTER_RETRIES": "0"}):
                summary = summarize_claude_stream_log(
                    log_path,
                    openrouter_api_key="test-key",
                    fetch_openrouter_generation=fake_fetch,
                )

            self.assertEqual(summary["total_cost_usd"], 9.99)
            self.assertEqual(summary["cost_source"], "claude_stream_log")
            self.assertEqual(summary["openrouter_total_cost_usd"], 0.25)
            self.assertEqual(summary["openrouter"]["successful_generation_count"], 1)
            self.assertEqual(summary["openrouter"]["failed_generation_count"], 1)

    def test_format_cost_summary(self) -> None:
        summary = {
            "model": "test/model",
            "total_cost_usd": 1.25,
            "num_turns": 3,
            "duration_ms": 12500,
            "result_subtype": "success",
        }

        self.assertEqual(
            format_cost_summary(summary),
            "model=test/model, cost=$1.250000, turns=3, duration=12.5s, result=success",
        )

    def test_successful_generation_missing_cost_does_not_override_stream_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "claude-code.txt"
            log_path.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "assistant", "message": {"id": "gen-1"}}),
                        json.dumps({"type": "assistant", "message": {"id": "gen-2"}}),
                        json.dumps({"type": "result", "total_cost_usd": 2.0}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            def fake_fetch(generation_id: str, _api_key: str) -> dict[str, object]:
                if generation_id == "gen-1":
                    return {"id": generation_id, "total_cost": 0.25}
                return {"id": generation_id, "model": "served/model"}

            summary = summarize_claude_stream_log(
                log_path,
                openrouter_api_key="test-key",
                fetch_openrouter_generation=fake_fetch,
            )

            self.assertEqual(summary["total_cost_usd"], 2.0)
            self.assertEqual(summary["cost_source"], "claude_stream_log")
            self.assertEqual(summary["openrouter_total_cost_usd"], 0.25)
            self.assertFalse(summary["openrouter"]["cost_complete"])
            self.assertEqual(summary["openrouter"]["valid_generation_cost_count"], 1)

    def test_openrouter_generation_limit_reports_truncation(self) -> None:
        stats = fetch_openrouter_generation_stats(
            ["gen-1", "gen-2"],
            "test-key",
            fetch_generation=lambda generation_id, _key: {
                "id": generation_id,
                "total_cost": 0.5,
            },
            retry_count=0,
            max_generation_count=1,
            deadline_seconds=5,
        )

        self.assertTrue(stats["truncated"])
        self.assertFalse(stats["complete"])
        self.assertFalse(stats["cost_complete"])
        self.assertEqual(stats["queried_generation_count"], 1)
        self.assertEqual(stats["unqueried_generation_count"], 1)

    def test_nonfinite_openrouter_float_environment_value_uses_default(self) -> None:
        for raw_value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(raw_value=raw_value), patch.dict(
                os.environ,
                {"TASKGEN_TEST_FLOAT": raw_value},
            ):
                self.assertEqual(
                    read_float_env("TASKGEN_TEST_FLOAT", 3.5, 0.1, 10.0),
                    3.5,
                )


class Phase1ValidationTests(unittest.TestCase):
    def test_render_phase1_prompt_uses_exact_idea_count(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            (project / "prompts").mkdir()
            (project / "prompts/seed-brainstorm.md").write_text(
                "{{SEED_ID}}\n{{SEED_PATH}}\n{{IDEA_COUNT_REQUIREMENT}}\n",
                encoding="utf-8",
            )

            prompt_path = render_phase1_prompt(project, "seed-a", 4)

            prompt = prompt_path.read_text(encoding="utf-8")
            self.assertIn("seed-a", prompt)
            self.assertIn("seed/seed-a", prompt)
            self.assertIn("Produce exactly 4 substantially different TB3 task ideas.", prompt)
            self.assertIn("must contain exactly 4 items", prompt)

    def test_render_phase1_prompt_uses_default_idea_count_when_unspecified(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            (project / "prompts").mkdir()
            (project / "prompts/seed-brainstorm.md").write_text(
                "{{IDEA_COUNT_REQUIREMENT}}\n",
                encoding="utf-8",
            )

            prompt_path = render_phase1_prompt(project, "seed-a")

            prompt = prompt_path.read_text(encoding="utf-8")
            self.assertIn("Produce 3-5 substantially different TB3 task ideas by default.", prompt)

    def test_allows_more_than_five_ideas(self) -> None:
        report = ValidationReport(phase="phase1", seed_id="seed-a")
        idea = {
            "title": "Title",
            "scenario": "Scenario",
            "core_transfer": "Transfer",
            "changed_dimensions": ["artifact", "scenario"],
            "expected_artifacts": ["/app/output.txt"],
            "verifier_sketch": "Check the output.",
            "risk_notes": ["Risk"],
            "skillnet_queries": ["query"],
            "difficulty_profile": difficulty_profile_fixture(),
        }
        data = {
            "seed_id": "seed-a",
            "source_path": "seed/seed-a",
            "task_understanding": "Understanding",
            "core_capabilities": ["Capability"],
            "ideas": [
                {"idea_id": f"idea-{index}", **idea}
                for index in range(1, 7)
            ],
            "avoid": ["Avoid"],
        }

        validate_brainstorm_data(data, "seed-a", report)

        self.assertEqual(report.errors, [])

    def test_rejects_wrong_idea_count_when_expected(self) -> None:
        report = ValidationReport(phase="phase1", seed_id="seed-a")
        idea = {
            "title": "Title",
            "scenario": "Scenario",
            "core_transfer": "Transfer",
            "changed_dimensions": ["artifact", "scenario"],
            "expected_artifacts": ["/app/output.txt"],
            "verifier_sketch": "Check the output.",
            "risk_notes": ["Risk"],
            "skillnet_queries": ["query"],
            "difficulty_profile": difficulty_profile_fixture(),
        }
        data = {
            "seed_id": "seed-a",
            "source_path": "seed/seed-a",
            "task_understanding": "Understanding",
            "core_capabilities": ["Capability"],
            "ideas": [
                {"idea_id": "idea-1", **idea},
                {"idea_id": "idea-2", **idea},
            ],
            "avoid": ["Avoid"],
        }

        validate_brainstorm_data(data, "seed-a", report, expected_idea_count=3)

        self.assertIn("$.ideas must contain exactly 3 idea(s), got 2", report.errors)

    def test_rejects_non_path_safe_idea_id(self) -> None:
        report = ValidationReport(phase="phase1", seed_id="seed-a")
        data = {
            "seed_id": "seed-a",
            "source_path": "seed/seed-a",
            "task_understanding": "Understanding",
            "core_capabilities": ["Capability"],
            "ideas": [
                {
                    "idea_id": "idea 1",
                    "title": "Title",
                    "scenario": "Scenario",
                    "core_transfer": "Transfer",
                    "changed_dimensions": ["artifact", "scenario"],
                    "expected_artifacts": ["/app/output.txt"],
                    "verifier_sketch": "Check the output.",
                    "risk_notes": ["Risk"],
                    "skillnet_queries": ["query"],
                    "difficulty_profile": difficulty_profile_fixture(),
                }
            ],
            "avoid": ["Avoid"],
        }

        validate_brainstorm_data(data, "seed-a", report)

        self.assertIn("idea_id must be path-friendly", "\n".join(report.errors))


class Phase2ValidationTests(unittest.TestCase):
    def write_no_match_fixture(self, project: Path) -> tuple[Path, Path]:
        seed_id = "seed-a"
        idea_id = "idea-1"
        brainstorm_dir = project / "runs/brainstorm" / seed_id
        brainstorm_dir.mkdir(parents=True)
        (brainstorm_dir / "seed_brainstorm.json").write_text(
            json.dumps(
                {
                    "seed_id": seed_id,
                    "source_path": "seed/seed-a",
                    "task_understanding": "Understand the task.",
                    "core_capabilities": ["Capability"],
                    "ideas": [
                        {
                            "idea_id": idea_id,
                            "title": "Idea title",
                            "scenario": "Scenario",
                            "core_transfer": "Transfer",
                            "changed_dimensions": ["artifact", "scenario"],
                            "expected_artifacts": ["/app/output.txt"],
                            "verifier_sketch": "Check output.",
                            "risk_notes": ["Risk"],
                            "skillnet_queries": ["query"],
                            "difficulty_profile": difficulty_profile_fixture(),
                        }
                    ],
                    "avoid": ["Avoid"],
                }
            ),
            encoding="utf-8",
        )
        skillnet_dir = project / "runs/skillnet" / seed_id
        idea_dir = skillnet_dir / idea_id
        (idea_dir / "skills").mkdir(parents=True)
        (idea_dir / "raw").mkdir()
        (idea_dir / "raw/search.txt").write_text("No useful results.", encoding="utf-8")
        index_path = skillnet_dir / "skillnet_index.json"
        index_path.write_text(
            json.dumps(
                {
                    "seed_id": seed_id,
                    "brainstorm_ref": "brainstorm/seed-a/seed_brainstorm.json",
                    "generated_at": "2026-06-24T00:00:00Z",
                    "ideas": [
                        {
                            "idea_id": idea_id,
                            "title": "Idea title",
                            "status": "no_strong_match",
                            "skill_summary_ref": "skillnet/seed-a/idea-1/skill_summary.json",
                            "skill_count": 0,
                            "skill_names": [],
                            "notes": ["No match"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        summary_path = idea_dir / "skill_summary.json"
        summary_path.write_text(
            json.dumps(
                {
                    "seed_id": seed_id,
                    "idea_id": idea_id,
                    "title": "Idea title",
                    "status": "no_strong_match",
                    "selected_skills": [],
                    "tooling_notes": ["Tooling"],
                    "environment_notes": ["Environment"],
                    "verifier_notes": ["Verifier"],
                    "implementation_risks": ["Risk"],
                    "recommended_direction": "Proceed without a selected skill.",
                    "difficulty_hardening": difficulty_hardening_fixture(),
                }
            ),
            encoding="utf-8",
        )
        return index_path, summary_path

    def test_rejects_boolean_skill_count(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            index_path, _ = self.write_no_match_fixture(project)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index["ideas"][0]["skill_count"] = True
            index_path.write_text(json.dumps(index), encoding="utf-8")

            report = validate_phase2(project, "seed-a", require_manifest=False)

            self.assertIn("skill_count must be an integer", "\n".join(report.errors))

    def test_rejects_timestamp_without_timezone_and_title_drift(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            index_path, summary_path = self.write_no_match_fixture(project)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index["generated_at"] = "2026-06-24T00:00:00"
            index["ideas"][0]["title"] = "Changed title"
            index_path.write_text(json.dumps(index), encoding="utf-8")
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary["title"] = "Changed title"
            summary_path.write_text(json.dumps(summary), encoding="utf-8")

            report = validate_phase2(project, "seed-a", require_manifest=False)
            combined = "\n".join(report.errors)

            self.assertIn("must include an ISO-8601 timezone offset", combined)
            self.assertIn("title must exactly match the phase1 brainstorm title", combined)

    def test_validates_curated_skill_packages(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            seed_id = "seed-a"
            idea_id = "idea-1"
            skill_names = [
                "taskgen-idea-1-alpha",
                "taskgen-idea-1-beta",
                "taskgen-idea-1-gamma",
            ]
            (project / "runs/brainstorm/seed-a").mkdir(parents=True)
            (project / "runs/brainstorm/seed-a/seed_brainstorm.json").write_text(
                json.dumps(
                    {
                        "seed_id": seed_id,
                        "source_path": "seed/seed-a",
                        "task_understanding": "Understand the task.",
                        "core_capabilities": ["Capability"],
                        "ideas": [
                            {
                                "idea_id": idea_id,
                                "title": "Idea title",
                                "scenario": "Scenario",
                                "core_transfer": "Transfer",
                                "changed_dimensions": ["artifact", "scenario"],
                                "expected_artifacts": ["/app/output.txt"],
                                "verifier_sketch": "Check output.",
                                "risk_notes": ["Risk"],
                                "skillnet_queries": ["query"],
                                "difficulty_profile": difficulty_profile_fixture(),
                            }
                        ],
                        "avoid": ["Avoid"],
                    }
                ),
                encoding="utf-8",
            )
            skillnet_dir = project / "runs/skillnet/seed-a"
            idea_dir = skillnet_dir / idea_id
            (idea_dir / "skills").mkdir(parents=True)
            (idea_dir / "raw").mkdir()
            (idea_dir / "raw/results.jsonl").write_text("{}", encoding="utf-8")
            for skill_name in skill_names:
                package = idea_dir / "skills" / skill_name
                (package / "references").mkdir(parents=True)
                (package / "SKILL.md").write_text(
                    f"---\nname: {skill_name}\ndescription: Useful for task generation.\n---\n\n"
                    "Use this curated skill for the selected task idea.\n",
                    encoding="utf-8",
                )
                (package / "references/note.md").write_text("note", encoding="utf-8")
            (skillnet_dir / "skillnet_index.json").write_text(
                json.dumps(
                    {
                        "seed_id": seed_id,
                        "brainstorm_ref": "brainstorm/seed-a/seed_brainstorm.json",
                        "generated_at": "2026-06-24T00:00:00Z",
                        "ideas": [
                            {
                                "idea_id": idea_id,
                                "title": "Idea title",
                                "status": "ready",
                                "skill_summary_ref": "skillnet/seed-a/idea-1/skill_summary.json",
                                "skill_count": 3,
                                "skill_names": skill_names,
                                "notes": ["Ready"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (idea_dir / "skill_summary.json").write_text(
                json.dumps(
                    {
                        "seed_id": seed_id,
                        "idea_id": idea_id,
                        "title": "Idea title",
                        "status": "ready",
                        "selected_skills": [
                            {
                                "name": skill_name,
                                "path": f"skills/{skill_name}",
                                "source": "skillnet",
                                "why_selected": "Relevant pattern.",
                                "usable_for": ["Generation"],
                                "limits": ["Use only curated parts."],
                            }
                            for skill_name in skill_names
                        ],
                        "tooling_notes": ["Tooling"],
                        "environment_notes": ["Environment"],
                        "verifier_notes": ["Verifier"],
                        "implementation_risks": ["Risk"],
                        "recommended_direction": "Build this task shape.",
                        "difficulty_hardening": difficulty_hardening_fixture(),
                    }
                ),
                encoding="utf-8",
            )

            report = validate_phase2(project, seed_id, require_manifest=False)

            self.assertEqual(report.errors, [])

    def test_allows_no_strong_match_without_selected_skills(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            seed_id = "seed-a"
            idea_id = "idea-1"
            (project / "runs/brainstorm/seed-a").mkdir(parents=True)
            (project / "runs/brainstorm/seed-a/seed_brainstorm.json").write_text(
                json.dumps(
                    {
                        "seed_id": seed_id,
                        "source_path": "seed/seed-a",
                        "task_understanding": "Understand the task.",
                        "core_capabilities": ["Capability"],
                        "ideas": [
                            {
                                "idea_id": idea_id,
                                "title": "Idea title",
                                "scenario": "Scenario",
                                "core_transfer": "Transfer",
                                "changed_dimensions": ["artifact", "scenario"],
                                "expected_artifacts": ["/app/output.txt"],
                                "verifier_sketch": "Check output.",
                                "risk_notes": ["Risk"],
                                "skillnet_queries": ["query"],
                                "difficulty_profile": difficulty_profile_fixture(),
                            }
                        ],
                        "avoid": ["Avoid"],
                    }
                ),
                encoding="utf-8",
            )
            skillnet_dir = project / "runs/skillnet/seed-a"
            idea_dir = skillnet_dir / idea_id
            (idea_dir / "skills").mkdir(parents=True)
            (idea_dir / "raw").mkdir()
            (idea_dir / "raw/search-01-keyword.txt").write_text("No useful results.", encoding="utf-8")
            (skillnet_dir / "skillnet_index.json").write_text(
                json.dumps(
                    {
                        "seed_id": seed_id,
                        "brainstorm_ref": "brainstorm/seed-a/seed_brainstorm.json",
                        "generated_at": "2026-06-24T00:00:00Z",
                        "ideas": [
                            {
                                "idea_id": idea_id,
                                "title": "Idea title",
                                "status": "no_strong_match",
                                "skill_summary_ref": "skillnet/seed-a/idea-1/skill_summary.json",
                                "skill_count": 0,
                                "skill_names": [],
                                "notes": ["No strong SkillNet match."],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (idea_dir / "skill_summary.json").write_text(
                json.dumps(
                    {
                        "seed_id": seed_id,
                        "idea_id": idea_id,
                        "title": "Idea title",
                        "status": "no_strong_match",
                        "selected_skills": [],
                        "tooling_notes": ["No direct SkillNet tooling match."],
                        "environment_notes": ["Use baseline environment design."],
                        "verifier_notes": ["Use outcome-based tests from the idea."],
                        "implementation_risks": ["May require phase3 to infer patterns without SkillNet support."],
                        "recommended_direction": "Proceed from the brainstorm and keep the task simple.",
                        "difficulty_hardening": difficulty_hardening_fixture(),
                    }
                ),
                encoding="utf-8",
            )

            report = validate_phase2(project, seed_id, require_manifest=False)

            self.assertEqual(report.errors, [])


class PromptRenderingHardeningTests(unittest.TestCase):
    def test_all_claude_prompt_renderers_reject_unknown_template_markers(self) -> None:
        cases = (
            ("seed-brainstorm.md", lambda root: render_phase1_prompt(root, "seed-a")),
            ("skillnet-research.md", lambda root: render_phase2_prompt(root, "seed-a")),
            ("task-review.md", lambda root: render_phase5_prompt(root, "seed-a", "idea-1")),
            ("task-repair.md", lambda root: render_phase6_prompt(root, "seed-a", "idea-1")),
        )
        for filename, renderer in cases:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as project_tmp:
                project = Path(project_tmp)
                prompt = project / "prompts" / filename
                prompt.parent.mkdir(parents=True)
                prompt.write_text("Unresolved: {{UNKNOWN_MARKER}}", encoding="utf-8")

                with self.assertRaisesRegex(SystemExit, "unreplaced marker"):
                    renderer(project)


class Phase3PromptTests(unittest.TestCase):
    def test_rejects_unreplaced_prompt_markers(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            (project / "prompts").mkdir()
            (project / "prompts/task-generation.md").write_text(
                "Seed {{SEED_ID}} unknown {{UNKNOWN_MARKER}}",
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit):
                render_phase3_prompt(project, "seed-a", "idea-1")


class Phase3ValidationTests(unittest.TestCase):
    def write_phase3_inputs(self, project: Path, *, bad_instruction: bool = False) -> None:
        seed_id = "seed-a"
        idea_id = "idea-1"
        (project / "seeds/seed-a").mkdir(parents=True)
        (project / "seeds/seed-a/instruction.md").write_text("Original seed task.", encoding="utf-8")
        (project / "runs/brainstorm/seed-a").mkdir(parents=True)
        (project / "runs/brainstorm/seed-a/seed_brainstorm.json").write_text(
            json.dumps(
                {
                    "seed_id": seed_id,
                    "source_path": "seed/seed-a",
                    "task_understanding": "Understand the task.",
                    "core_capabilities": ["Capability"],
                    "ideas": [
                        {
                            "idea_id": idea_id,
                            "title": "Idea title",
                            "scenario": "Scenario",
                            "core_transfer": "Transfer",
                            "changed_dimensions": ["artifact", "scenario"],
                            "expected_artifacts": ["/app/output.txt"],
                            "verifier_sketch": "Check output.",
                            "risk_notes": ["Risk"],
                            "skillnet_queries": ["query"],
                            "difficulty_profile": difficulty_profile_fixture(),
                        }
                    ],
                    "avoid": ["Avoid"],
                }
            ),
            encoding="utf-8",
        )

        idea_dir = project / "runs/skillnet/seed-a/idea-1"
        (idea_dir / "skills").mkdir(parents=True)
        (idea_dir / "raw").mkdir()
        (idea_dir / "raw/search.txt").write_text("No strong reusable skill.", encoding="utf-8")
        (project / "runs/skillnet/seed-a/skillnet_index.json").write_text(
            json.dumps(
                {
                    "seed_id": seed_id,
                    "brainstorm_ref": "brainstorm/seed-a/seed_brainstorm.json",
                    "generated_at": "2026-06-24T00:00:00Z",
                    "ideas": [
                        {
                            "idea_id": idea_id,
                            "title": "Idea title",
                            "status": "no_strong_match",
                            "skill_summary_ref": "skillnet/seed-a/idea-1/skill_summary.json",
                            "skill_count": 0,
                            "skill_names": [],
                            "notes": ["No strong SkillNet match."],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (idea_dir / "skill_summary.json").write_text(
            json.dumps(
                {
                    "seed_id": seed_id,
                    "idea_id": idea_id,
                    "title": "Idea title",
                    "status": "no_strong_match",
                    "selected_skills": [],
                    "tooling_notes": ["Use shell and Python if useful."],
                    "environment_notes": ["Place task files under /app."],
                    "verifier_notes": ["Check final artifact content."],
                    "implementation_risks": ["Avoid overfitting to one command."],
                    "recommended_direction": "Create a compact artifact-production task.",
                    "difficulty_hardening": difficulty_hardening_fixture(),
                }
            ),
            encoding="utf-8",
        )

        task = project / "generated/working/seed-a/idea-1"
        (task / "environment").mkdir(parents=True)
        (task / "solution").mkdir()
        (task / "tests").mkdir()
        if bad_instruction:
            instruction = "\n".join(
                [
                    "# Generated Task",
                    "",
                    "## Background",
                    "你是一个专家，请一步一步思考。",
                    "",
                    "## Requirements",
                    "Create ./output.txt.",
                    "",
                    "## Notes",
                    "Use this exact sequence of commands.",
                ]
            )
        else:
            instruction = (
                "Create `/app/output.txt` containing the normalized account id from `/app/input.txt`.\n\n"
                "You have 1800 seconds to complete this task. "
                "Do not cheat by using online solutions or hints specific to this task.\n"
            )
        (task / "instruction.md").write_text(instruction, encoding="utf-8")
        (task / "task.toml").write_text(
            "\n".join(
                [
                    'schema_version = "1.1"',
                    'artifacts = ["/app/output.txt"]',
                    "",
                    "[metadata]",
                    'author_name = ""',
                    'author_email = ""',
                    'author_organization = ""',
                    'difficulty_explanation = "The task requires inspecting an input artifact, identifying the normalization rule, and producing an exact output without relying on a prescribed command sequence."',
                    'solution_explanation = "The reference solution reads the visible input, applies the account-id normalization rule, and writes the required artifact at the declared path."',
                    'verification_explanation = "The verifier checks the declared output artifact for existence and exact normalized content, then writes a scalar Harbor reward."',
                    'category = "data_processing"',
                    'tags = ["normalization", "filesystem", "shell"]',
                    "expert_time_estimate_hours = 1.5",
                    'relevant_experience = ""',
                    "",
                    "[agent]",
                    "timeout_sec = 1800.0",
                    "",
                    "[verifier]",
                    "timeout_sec = 300.0",
                    'environment_mode = "separate"',
                    "",
                    "[environment]",
                    "build_timeout_sec = 600.0",
                    "cpus = 1",
                    "memory_mb = 2048",
                    "storage_mb = 10240",
                    "gpus = 0",
                    "allow_internet = true",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (task / "environment/Dockerfile").write_text(
            "FROM ubuntu:24.04\nWORKDIR /app\n",
            encoding="utf-8",
        )
        (task / "solution/solve.sh").write_text(
            "#!/bin/bash\necho acct-123 > /app/output.txt\n",
            encoding="utf-8",
        )
        (task / "solution/solve.sh").chmod(0o755)
        (task / "tests/Dockerfile").write_text(
            "FROM python:3.13-slim-bookworm\nCOPY . /tests/\nRUN mkdir -p /app\n",
            encoding="utf-8",
        )
        (task / "tests/test.sh").write_text(
            "#!/bin/bash\n"
            "test -f /app/output.txt\n"
            "if [ $? -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; "
            "else echo 0 > /logs/verifier/reward.txt; fi\n",
            encoding="utf-8",
        )
        (task / "tests/test.sh").chmod(0o755)

    def test_validates_generated_task_layout(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            self.write_phase3_inputs(project)

            report = validate_phase3(project, "seed-a", "idea-1", require_manifest=False)

            self.assertEqual(report.errors, [])

    def test_allows_instruction_quality_issues_for_later_review(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            self.write_phase3_inputs(project, bad_instruction=True)

            report = validate_phase3(project, "seed-a", "idea-1", require_manifest=False)

            self.assertEqual(report.errors, [])

    def test_phase3_keeps_required_layout_check_but_allows_task_toml_quality_issues(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            self.write_phase3_inputs(project)
            task = project / "generated/working/seed-a/idea-1"
            (task / "task.toml").write_text(
                "\n".join(
                    [
                        'version = "1.0"',
                        "",
                        "[metadata]",
                        "",
                        "[agent]",
                        "timeout_sec = 1800.0",
                        "",
                        "[verifier]",
                        "timeout_sec = 300.0",
                        "",
                        "[environment]",
                        "cpus = 1",
                        'memory = "2G"',
                        'storage = "10G"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            (task / "tests/Dockerfile").unlink()

            report = validate_phase3(project, "seed-a", "idea-1", require_manifest=False)

            combined_errors = "\n".join(report.errors)
            self.assertIn("tests/Dockerfile", combined_errors)
            self.assertNotIn("artifacts", combined_errors)
            self.assertNotIn("environment_mode", combined_errors)
            self.assertNotIn("memory_mb", combined_errors)

    def test_allows_empty_synthetic_author_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            self.write_phase3_inputs(project)

            report = validate_phase3(project, "seed-a", "idea-1", require_manifest=False)

            self.assertEqual(report.errors, [])

    def test_allows_metadata_quality_issues_for_later_review(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            self.write_phase3_inputs(project)
            task_toml = project / "generated/working/seed-a/idea-1/task.toml"
            text = task_toml.read_text(encoding="utf-8")
            text = text.replace('category = "data_processing"\n', "")
            text = text.replace('tags = ["normalization", "filesystem", "shell"]\n', "")
            task_toml.write_text(text, encoding="utf-8")

            report = validate_phase3(project, "seed-a", "idea-1", require_manifest=False)

            self.assertEqual(report.errors, [])

    def test_allows_verifier_quality_issues_for_later_review(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            self.write_phase3_inputs(project)
            test_sh = project / "generated/working/seed-a/idea-1/tests/test.sh"
            test_sh.write_text(
                "#!/bin/bash\napt-get install -y pytest\necho 0 > /logs/verifier/reward.txt\n",
                encoding="utf-8",
            )

            report = validate_phase3(project, "seed-a", "idea-1", require_manifest=False)

            self.assertEqual(report.errors, [])

    def test_rejects_transient_files_in_generated_task(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            self.write_phase3_inputs(project)
            task = project / "generated/working/seed-a/idea-1"
            (task / "output/phase3-validation/harbor-jobs").mkdir(parents=True)
            (task / "phase6-validation/harbor-jobs").mkdir(parents=True)
            (task / "output/phase3-validation/oracle.log").write_text("log", encoding="utf-8")
            (task / "tests/__pycache__").mkdir()
            (task / "tests/__pycache__/test_outputs.cpython-310.pyc").write_bytes(b"pyc")
            (task / "environment/debug.log").write_text("debug", encoding="utf-8")

            report = validate_phase3(project, "seed-a", "idea-1", require_manifest=False)

            combined_errors = "\n".join(report.errors)
            self.assertIn("transient or validation directory: output", combined_errors)
            self.assertIn("transient or validation directory: output/phase3-validation", combined_errors)
            self.assertIn("transient or validation directory: output/phase3-validation/harbor-jobs", combined_errors)
            self.assertIn("transient or validation directory: phase6-validation", combined_errors)
            self.assertIn("transient or validation directory: tests/__pycache__", combined_errors)
            self.assertIn("transient file: tests/__pycache__/test_outputs.cpython-310.pyc", combined_errors)
            self.assertIn("transient file: environment/debug.log", combined_errors)

    def test_allows_instruction_suffix_issues_for_later_review(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            self.write_phase3_inputs(project)
            instruction = project / "generated/working/seed-a/idea-1/instruction.md"
            instruction.write_text(
                "Create `/app/output.txt` containing the normalized account id from `/app/input.txt`.\n",
                encoding="utf-8",
            )

            report = validate_phase3(project, "seed-a", "idea-1", require_manifest=False)

            self.assertEqual(report.errors, [])

    def test_rejects_forbidden_runner_path_text(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            self.write_phase3_inputs(project)
            instruction = project / "generated/working/seed-a/idea-1/instruction.md"
            instruction.write_text(
                "Inspect runs/workspace/task-generation before writing `/app/output.txt`.\n",
                encoding="utf-8",
            )

            report = validate_phase3(project, "seed-a", "idea-1", require_manifest=False)

            self.assertIn("forbidden runner/seed path reference", "\n".join(report.errors))

    def test_requires_skillnet_index_for_manifest_consistency(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            self.write_phase3_inputs(project)
            (project / "runs/skillnet/seed-a/skillnet_index.json").unlink()

            report = validate_phase3(project, "seed-a", "idea-1", require_manifest=False)

            self.assertIn("missing JSON file", "\n".join(report.errors))


class HarborHardeningTests(unittest.TestCase):
    def test_boolean_reward_is_not_numeric(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result_path = root / "job/result.json"
            result_path.parent.mkdir(parents=True)
            result_path.write_text(
                json.dumps({"verifier_result": {"rewards": {"reward": True}}}),
                encoding="utf-8",
            )

            self.assertIsNone(extract_reward(root))

            log = root / "oracle.log"
            log.write_text("log", encoding="utf-8")
            report = ValidationReport(phase="phase4", seed_id="seed-a")
            validate_harbor_check(
                {
                    "exit_code": 0,
                    "reward": True,
                    "log": str(log),
                    "job_dir": str(result_path.parent),
                    "timed_out": False,
                    "timeout_sec": 10,
                },
                "oracle",
                1.0,
                report,
            )
            self.assertIn("$.oracle.reward must be 1.0", report.errors)

    @unittest.skipUnless(os.name == "posix", "process-group cleanup requires POSIX")
    def test_harbor_timeout_records_status_and_kills_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task"
            task.mkdir()
            jobs = root / "jobs"
            out = root / "out"
            process = MagicMock()
            process.pid = 54321
            process.poll.return_value = None
            process.wait.side_effect = [
                subprocess.TimeoutExpired(cmd=["harbor"], timeout=2.5),
                -signal.SIGKILL,
            ]

            with (
                patch("taskgen.harbor.oracle_nop.subprocess.Popen", return_value=process) as popen,
                patch("taskgen.harbor.oracle_nop.os.killpg") as killpg,
            ):
                result = run_harbor_check(
                    ["harbor"],
                    task,
                    jobs,
                    out,
                    "oracle",
                    "oracle",
                    timeout_sec=2.5,
                )

            self.assertEqual(result.exit_code, 124)
            self.assertTrue(result.timed_out)
            self.assertEqual(result.timeout_sec, 2.5)
            self.assertIsNone(result.reward)
            self.assertIs(popen.call_args.kwargs["start_new_session"], True)
            killpg.assert_called_once_with(process.pid, signal.SIGKILL)
            self.assertEqual(process.wait.call_args_list, [call(timeout=2.5), call()])
            self.assertIn("timed out after 2.5 seconds", result.log.read_text(encoding="utf-8"))


class Phase4ValidationTests(unittest.TestCase):
    def write_generated_task(self, project: Path) -> Path:
        task = project / "generated/working/seed-a/idea-1"
        task.mkdir(parents=True)
        (task / "task.toml").write_text('version = "1.0"\n', encoding="utf-8")
        return task

    def write_phase4_status(
        self,
        project: Path,
        task: Path,
        *,
        passed: bool = True,
        oracle_reward: float | None = 1.0,
        nop_reward: float | None = 0.0,
        oracle_exit: int = 0,
        nop_exit: int = 0,
    ) -> None:
        task_id = "seed-a__idea-1"
        out_dir = project / "runs/oracle-nop-check" / task_id
        jobs_dir = out_dir / "harbor-jobs/run-1"
        oracle_job = jobs_dir / "oracle"
        nop_job = jobs_dir / "nop"
        oracle_job.mkdir(parents=True)
        nop_job.mkdir(parents=True)
        oracle_log = out_dir / "oracle.log"
        nop_log = out_dir / "nop.log"
        oracle_log.write_text("oracle", encoding="utf-8")
        nop_log.write_text("nop", encoding="utf-8")
        status = {
            "task_id": task_id,
            "task_path": str(task.resolve()),
            "run_id": "run-1",
            "task_tree_sha256": directory_tree_sha256(task),
            "passed": passed,
            "oracle": {
                "exit_code": oracle_exit,
                "reward": oracle_reward,
                "log": str(oracle_log),
                "job_dir": str(oracle_job),
                "timed_out": oracle_exit == 124,
                "timeout_sec": 10800.0,
            },
            "nop": {
                "exit_code": nop_exit,
                "reward": nop_reward,
                "log": str(nop_log),
                "job_dir": str(nop_job),
                "timed_out": nop_exit == 124,
                "timeout_sec": 10800.0,
            },
            "jobs_dir": str(jobs_dir),
        }
        (out_dir / "oracle-nop-status.json").write_text(json.dumps(status), encoding="utf-8")
        append_phase4_manifest_event(project, "seed-a", "idea-1", status)

    def test_validates_phase4_oracle_nop_status(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            task = self.write_generated_task(project)
            self.write_phase4_status(project, task)

            report = validate_phase4(project, "seed-a", "idea-1")

            self.assertEqual(report.errors, [])

    def test_rejects_stale_phase4_status_after_task_tree_changes(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            task = self.write_generated_task(project)
            self.write_phase4_status(project, task)
            (task / "changed-after-check.txt").write_text("changed", encoding="utf-8")

            report = validate_phase4(project, "seed-a", "idea-1")

            self.assertIn("status is stale", "\n".join(report.errors))

    def test_uses_stable_pipeline_task_id_even_with_legacy_task_name(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            task = self.write_generated_task(project)
            (task / "task.toml").write_text(
                '[task]\nname = "custom/legacy-name"\n',
                encoding="utf-8",
            )
            self.write_phase4_status(project, task)

            report = validate_phase4(project, "seed-a", "idea-1")

            self.assertEqual(report.errors, [])

    def test_rejects_failed_phase4_status(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            task = self.write_generated_task(project)
            self.write_phase4_status(
                project,
                task,
                passed=False,
                oracle_reward=0.0,
                nop_reward=1.0,
                oracle_exit=1,
            )

            report = validate_phase4(project, "seed-a", "idea-1")

            combined_errors = "\n".join(report.errors)
            self.assertIn("$.passed must be true", combined_errors)
            self.assertIn("$.oracle.exit_code must be 0", combined_errors)
            self.assertIn("$.oracle.reward must be 1.0", combined_errors)
            self.assertIn("$.nop.reward must be 0.0", combined_errors)

    def test_failed_phase4_status_can_be_reviewable(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            task = self.write_generated_task(project)
            self.write_phase4_status(
                project,
                task,
                passed=False,
                oracle_reward=0.0,
                nop_reward=1.0,
                oracle_exit=1,
            )

            report = validate_phase4(project, "seed-a", "idea-1", require_passed=False)

            self.assertEqual(report.errors, [])
            self.assertIn("available for phase5 review", "\n".join(report.warnings))

    def test_phase4_run_returns_success_when_failed_status_is_reviewable(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            task = self.write_generated_task(project)

            def fake_oracle_nop(_: object) -> int:
                self.write_phase4_status(
                    project,
                    task,
                    passed=False,
                    oracle_reward=0.0,
                    nop_reward=1.0,
                    oracle_exit=1,
                )
                return 1

            with (
                patch("taskgen.phases.phase4_oracle_nop_check.project_root", return_value=project),
                patch(
                    "taskgen.phases.phase4_oracle_nop_check.validate_phase3",
                    return_value=ValidationReport(phase="phase3", seed_id="seed-a"),
                ),
                patch("taskgen.phases.phase4_oracle_nop_check.run_oracle_nop_command", fake_oracle_nop),
            ):
                exit_code = command_run_phase4(
                    SimpleNamespace(seed_id="seed-a", idea_id="idea-1", dry_run=False)
                )

            self.assertEqual(exit_code, 0)


class Phase5ValidationTests(unittest.TestCase):
    def write_generated_task(self, project: Path) -> Path:
        task = project / "generated/working/seed-a/idea-1"
        task.mkdir(parents=True)
        (task / "task.toml").write_text('version = "1.0"\n', encoding="utf-8")
        return task

    def write_phase4_status(
        self,
        project: Path,
        task: Path,
        *,
        passed: bool = True,
        oracle_reward: float | None = 1.0,
        nop_reward: float | None = 0.0,
        oracle_exit: int = 0,
        nop_exit: int = 0,
    ) -> None:
        task_id = "seed-a__idea-1"
        out_dir = project / "runs/oracle-nop-check" / task_id
        jobs_dir = out_dir / "harbor-jobs/run-1"
        oracle_job = jobs_dir / "oracle"
        nop_job = jobs_dir / "nop"
        oracle_job.mkdir(parents=True)
        nop_job.mkdir(parents=True)
        oracle_log = out_dir / "oracle.log"
        nop_log = out_dir / "nop.log"
        oracle_log.write_text("oracle", encoding="utf-8")
        nop_log.write_text("nop", encoding="utf-8")
        status = {
            "task_id": task_id,
            "task_path": str(task.resolve()),
            "run_id": "run-1",
            "task_tree_sha256": directory_tree_sha256(task),
            "passed": passed,
            "oracle": {
                "exit_code": oracle_exit,
                "reward": oracle_reward,
                "log": str(oracle_log),
                "job_dir": str(oracle_job),
                "timed_out": oracle_exit == 124,
                "timeout_sec": 10800.0,
            },
            "nop": {
                "exit_code": nop_exit,
                "reward": nop_reward,
                "log": str(nop_log),
                "job_dir": str(nop_job),
                "timed_out": nop_exit == 124,
                "timeout_sec": 10800.0,
            },
            "jobs_dir": str(jobs_dir),
        }
        (out_dir / "oracle-nop-status.json").write_text(json.dumps(status), encoding="utf-8")
        append_phase4_manifest_event(project, "seed-a", "idea-1", status)

    def write_review(
        self,
        project: Path,
        *,
        decision: str = "ready",
        modification_items: list[dict[str, object]] | None = None,
        blocking_reasons: list[dict[str, object]] | None = None,
        extra: dict[str, object] | None = None,
    ) -> None:
        review_dir = project / "runs/reviews/seed-a__idea-1"
        review_dir.mkdir(parents=True)
        payload: dict[str, object] = {
            "task_id": "seed-a__idea-1",
            "decision": decision,
            "summary": "Review summary.",
            "modification_items": modification_items if modification_items is not None else [],
            "blocking_reasons": blocking_reasons if blocking_reasons is not None else [],
        }
        if extra:
            payload.update(extra)
        (review_dir / "review.json").write_text(json.dumps(payload), encoding="utf-8")
        (review_dir / "review.md").write_text(
            review_markdown_fixture(decision),
            encoding="utf-8",
        )
        session_ref = write_fake_claude_session(project, "task-review", "seed-a__idea-1")
        append_phase5_manifest_event(project, "seed-a", "idea-1", decision, session_ref)

    def prepare_valid_inputs(self, project: Path) -> None:
        task = self.write_generated_task(project)
        self.write_phase4_status(project, task)

    def test_validates_ready_review(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            self.prepare_valid_inputs(project)
            self.write_review(project)

            report = validate_phase5(project, "seed-a", "idea-1")

            self.assertEqual(report.errors, [])

    def test_review_is_invalidated_by_a_new_phase4_run_until_reviewed_again(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            self.prepare_valid_inputs(project)
            self.write_review(project)
            status_path = project / "runs/oracle-nop-check/seed-a__idea-1/oracle-nop-status.json"
            status = json.loads(status_path.read_text(encoding="utf-8"))
            old_jobs = Path(status["jobs_dir"])
            new_jobs = old_jobs.parent / "run-2"
            os.replace(old_jobs, new_jobs)
            status["run_id"] = "run-2"
            status["jobs_dir"] = str(new_jobs)
            status["oracle"]["job_dir"] = str(new_jobs / "oracle")
            status["nop"]["job_dir"] = str(new_jobs / "nop")
            status_path.write_text(json.dumps(status), encoding="utf-8")
            append_phase4_manifest_event(project, "seed-a", "idea-1", status)

            report = validate_phase5(project, "seed-a", "idea-1")

            self.assertIn("no matching reviewed event", "\n".join(report.errors))

    def test_rejects_review_markdown_decision_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            self.prepare_valid_inputs(project)
            self.write_review(
                project,
                decision="needs_modification",
                modification_items=[
                    {
                        "area": "instruction",
                        "priority": "must_fix",
                        "message": "Fix the instruction.",
                        "evidence": ["instruction.md"],
                        "repair_direction": "Rewrite it.",
                    }
                ],
            )
            review_md = project / "runs/reviews/seed-a__idea-1/review.md"
            review_md.write_text(review_markdown_fixture("ready"), encoding="utf-8")

            report = validate_phase5(project, "seed-a", "idea-1")

            self.assertIn("same decision as review.json", "\n".join(report.errors))

    def test_validates_needs_modification_review(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            self.prepare_valid_inputs(project)
            self.write_review(
                project,
                decision="needs_modification",
                modification_items=[
                    {
                        "area": "instruction",
                        "priority": "must_fix",
                        "message": "Instruction must be shortened.",
                        "evidence": ["instruction.md is verbose"],
                        "repair_direction": "Rewrite as a compact task statement.",
                    }
                ],
            )

            report = validate_phase5(project, "seed-a", "idea-1")

            self.assertEqual(report.errors, [])

    def test_validates_review_after_failed_phase4_status(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            task = self.write_generated_task(project)
            self.write_phase4_status(
                project,
                task,
                passed=False,
                oracle_reward=0.0,
                nop_reward=1.0,
                oracle_exit=1,
            )
            self.write_review(
                project,
                decision="needs_modification",
                modification_items=[
                    {
                        "area": "oracle-nop",
                        "priority": "must_fix",
                        "message": "Oracle/nop status did not pass.",
                        "evidence": ["oracle reward was 0.0", "nop reward was 1.0"],
                        "repair_direction": "Fix the task so oracle passes and nop fails.",
                    }
                ],
            )
            (project / "prompts").mkdir()
            (project / "prompts/task-review.md").write_text("review prompt", encoding="utf-8")
            (project / "scripts").mkdir()
            (project / "scripts/run-claude-logged.sh").write_text("#!/bin/sh\n", encoding="utf-8")

            errors = ensure_phase5_inputs(project, "seed-a", "idea-1")
            report = validate_phase5(project, "seed-a", "idea-1")

            self.assertEqual(errors, [])
            self.assertEqual(report.errors, [])
            self.assertIn("available for phase5 review", "\n".join(report.warnings))

    def test_allows_custom_review_area_and_priority_values(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            self.prepare_valid_inputs(project)
            self.write_review(
                project,
                decision="needs_modification",
                modification_items=[
                    {
                        "area": "custom-review-category",
                        "priority": "later",
                        "message": "Instruction can be tightened.",
                        "evidence": ["instruction.md has extra prose"],
                        "repair_direction": "Trim the task statement.",
                    }
                ],
            )

            report = validate_phase5(project, "seed-a", "idea-1")

            self.assertEqual(report.errors, [])

    def test_rejects_extra_top_level_review_fields(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            self.prepare_valid_inputs(project)
            self.write_review(project, extra={"checks": {}})

            report = validate_phase5(project, "seed-a", "idea-1")

            self.assertIn("unexpected top-level field", "\n".join(report.errors))

    def test_rejects_decision_array_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            self.prepare_valid_inputs(project)
            self.write_review(project, decision="needs_modification", modification_items=[])

            report = validate_phase5(project, "seed-a", "idea-1")

            self.assertIn("must be non-empty", "\n".join(report.errors))


class Phase6RepairTests(unittest.TestCase):
    def write_generated_task(self, project: Path) -> Path:
        task = project / "generated/working/seed-a/idea-1"
        task.mkdir(parents=True)
        (task / "task.toml").write_text('version = "1.0"\n', encoding="utf-8")
        return task

    def write_phase4_status(
        self,
        project: Path,
        task: Path,
        *,
        passed: bool = True,
        oracle_reward: float | None = 1.0,
        nop_reward: float | None = 0.0,
        oracle_exit: int = 0,
        nop_exit: int = 0,
    ) -> None:
        task_id = "seed-a__idea-1"
        out_dir = project / "runs/oracle-nop-check" / task_id
        jobs_dir = out_dir / "harbor-jobs/run-1"
        oracle_job = jobs_dir / "oracle"
        nop_job = jobs_dir / "nop"
        oracle_job.mkdir(parents=True)
        nop_job.mkdir(parents=True)
        oracle_log = out_dir / "oracle.log"
        nop_log = out_dir / "nop.log"
        oracle_log.write_text("oracle", encoding="utf-8")
        nop_log.write_text("nop", encoding="utf-8")
        status = {
            "task_id": task_id,
            "task_path": str(task.resolve()),
            "run_id": "run-1",
            "task_tree_sha256": directory_tree_sha256(task),
            "passed": passed,
            "oracle": {
                "exit_code": oracle_exit,
                "reward": oracle_reward,
                "log": str(oracle_log),
                "job_dir": str(oracle_job),
                "timed_out": oracle_exit == 124,
                "timeout_sec": 10800.0,
            },
            "nop": {
                "exit_code": nop_exit,
                "reward": nop_reward,
                "log": str(nop_log),
                "job_dir": str(nop_job),
                "timed_out": nop_exit == 124,
                "timeout_sec": 10800.0,
            },
            "jobs_dir": str(jobs_dir),
        }
        (out_dir / "oracle-nop-status.json").write_text(json.dumps(status), encoding="utf-8")
        append_phase4_manifest_event(project, "seed-a", "idea-1", status)

    def write_review(self, project: Path, decision: str) -> None:
        review_dir = project / "runs/reviews/seed-a__idea-1"
        review_dir.mkdir(parents=True)
        modification_items = []
        blocking_reasons = []
        if decision == "needs_modification":
            modification_items = [
                {
                    "area": "instruction",
                    "priority": "must_fix",
                    "message": "Instruction must be shortened.",
                    "evidence": ["instruction.md is verbose"],
                    "repair_direction": "Rewrite as a compact task statement.",
                }
            ]
        if decision == "rejected":
            blocking_reasons = [
                {
                    "area": "difficulty",
                    "message": "Task concept is unsuitable.",
                    "evidence": ["reviewer rejected the concept"],
                }
            ]
        (review_dir / "review.json").write_text(
            json.dumps(
                {
                    "task_id": "seed-a__idea-1",
                    "decision": decision,
                    "summary": "Review summary.",
                    "modification_items": modification_items,
                    "blocking_reasons": blocking_reasons,
                }
            ),
            encoding="utf-8",
        )
        (review_dir / "review.md").write_text(
            review_markdown_fixture(decision),
            encoding="utf-8",
        )
        session_ref = write_fake_claude_session(project, "task-review", "seed-a__idea-1")
        append_phase5_manifest_event(project, "seed-a", "idea-1", decision, session_ref)

    def prepare_inputs(self, project: Path, decision: str) -> None:
        task = self.write_generated_task(project)
        self.write_phase4_status(project, task)
        self.write_review(project, decision)
        (project / "prompts").mkdir()
        (project / "prompts/task-repair.md").write_text("repair prompt", encoding="utf-8")
        (project / "scripts").mkdir()
        (project / "scripts/run-claude-logged.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    def test_phase6_accepts_needs_modification_review(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            self.prepare_inputs(project, "needs_modification")

            errors = ensure_phase6_inputs(project, "seed-a", "idea-1")

            self.assertEqual(errors, [])

    def test_phase6_rejects_ready_review(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            self.prepare_inputs(project, "ready")

            errors = ensure_phase6_inputs(project, "seed-a", "idea-1")

            self.assertIn("needs_modification", "\n".join(errors))

    def test_phase6_requires_synced_repaired_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            (session / "status.json").write_text(
                json.dumps({"synced_outputs": ["runs/reviews/seed-a__idea-1"]}),
                encoding="utf-8",
            )

            errors = validate_new_session_synced_task(session, "generated/working/seed-a/idea-1")

            self.assertIn("did not sync repaired task", "\n".join(errors))


class CleanupHardeningTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "symlink"), "symbolic links are unavailable")
    def test_cleanup_refuses_symlinked_runs_ancestor_without_touching_external_files(self) -> None:
        for apply in (False, True):
            with (
                self.subTest(apply=apply),
                tempfile.TemporaryDirectory() as project_tmp,
                tempfile.TemporaryDirectory() as outside_tmp,
            ):
                project = Path(project_tmp)
                outside = Path(outside_tmp)
                victim = outside / "reviews/subject/review.json"
                victim.parent.mkdir(parents=True)
                victim.write_text("external", encoding="utf-8")
                (project / "runs").symlink_to(outside, target_is_directory=True)

                with patch(
                    "taskgen.maintenance.clean_intermediate.project_root",
                    return_value=project,
                ):
                    exit_code = command_clean(
                        SimpleNamespace(
                            apply=apply,
                            drop_manifest=False,
                            force_active=False,
                            discard_transactions=False,
                        )
                    )

                self.assertEqual(exit_code, 1)
                self.assertTrue((project / "runs").is_symlink())
                self.assertEqual(victim.read_text(encoding="utf-8"), "external")

    def test_cleanup_preserves_manifest_by_default_and_drops_only_when_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            manifest = project / "runs/task-manifest.jsonl"
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{"event":"audit"}\n', encoding="utf-8")
            artifact = project / "runs/prompts/seed-a/prompt.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("prompt", encoding="utf-8")

            with patch("taskgen.maintenance.clean_intermediate.project_root", return_value=project):
                exit_code = command_clean(
                    SimpleNamespace(apply=True, drop_manifest=False, force_active=False)
                )

            self.assertEqual(exit_code, 0)
            self.assertTrue(manifest.is_file())
            self.assertFalse(artifact.exists())
            self.assertNotIn(manifest, clean_targets(project))
            self.assertIn(manifest, clean_targets(project, drop_manifest=True))

            with patch("taskgen.maintenance.clean_intermediate.project_root", return_value=project):
                exit_code = command_clean(
                    SimpleNamespace(apply=True, drop_manifest=True, force_active=False)
                )
            self.assertEqual(exit_code, 0)
            self.assertFalse(manifest.exists())

    def test_cleanup_refuses_active_run_marker(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            marker = project / "runs/claude-sessions/task-review/subject/run-1/.active"
            marker.parent.mkdir(parents=True)
            marker.write_text("{}", encoding="utf-8")
            artifact = project / "runs/reviews/subject/review.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("{}", encoding="utf-8")

            with patch("taskgen.maintenance.clean_intermediate.project_root", return_value=project):
                exit_code = command_clean(
                    SimpleNamespace(apply=True, drop_manifest=False, force_active=False)
                )

            self.assertEqual(exit_code, 1)
            self.assertTrue(marker.exists())
            self.assertTrue(artifact.exists())

    def test_cleanup_refuses_shared_pipeline_activity_lock(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            artifact = project / "runs/reviews/subject/review.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("{}", encoding="utf-8")

            with phase_subject_lock(project, "phase5", "subject"), patch(
                "taskgen.maintenance.clean_intermediate.project_root",
                return_value=project,
            ):
                exit_code = command_clean(
                    SimpleNamespace(apply=True, drop_manifest=False, force_active=False)
                )

            self.assertEqual(exit_code, 1)
            self.assertTrue(artifact.exists())

    def test_cleanup_requires_explicit_override_for_pending_recovery_journal(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            journal = project / "runs/finalization-transactions/pending.json"
            journal.parent.mkdir(parents=True)
            journal.write_text("{}", encoding="utf-8")

            with patch("taskgen.maintenance.clean_intermediate.project_root", return_value=project):
                refused = command_clean(
                    SimpleNamespace(
                        apply=True,
                        drop_manifest=False,
                        force_active=False,
                        discard_transactions=False,
                    )
                )
            self.assertEqual(refused, 1)
            self.assertTrue(journal.exists())

            with patch("taskgen.maintenance.clean_intermediate.project_root", return_value=project):
                discarded = command_clean(
                    SimpleNamespace(
                        apply=True,
                        drop_manifest=False,
                        force_active=False,
                        discard_transactions=True,
                    )
                )
            self.assertEqual(discarded, 0)
            self.assertFalse(journal.exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "symbolic links are unavailable")
    def test_cleanup_unlinks_broken_directory_symlink_and_restores_skeleton(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            reviews = project / "runs/reviews"
            reviews.parent.mkdir(parents=True)
            reviews.symlink_to(project / "missing-reviews", target_is_directory=True)

            with patch("taskgen.maintenance.clean_intermediate.project_root", return_value=project):
                exit_code = command_clean(
                    SimpleNamespace(apply=True, drop_manifest=False, force_active=False)
                )

            self.assertEqual(exit_code, 0)
            self.assertTrue(reviews.is_dir())
            self.assertFalse(reviews.is_symlink())

    @unittest.skipUnless(hasattr(os, "symlink"), "symbolic links are unavailable")
    def test_cleanup_unlinks_leaf_directory_symlink_without_touching_target(self) -> None:
        with (
            tempfile.TemporaryDirectory() as project_tmp,
            tempfile.TemporaryDirectory() as outside_tmp,
        ):
            project = Path(project_tmp)
            outside_reviews = Path(outside_tmp)
            victim = outside_reviews / "subject/review.json"
            victim.parent.mkdir(parents=True)
            victim.write_text("external", encoding="utf-8")
            reviews = project / "runs/reviews"
            reviews.parent.mkdir(parents=True)
            reviews.symlink_to(outside_reviews, target_is_directory=True)

            with patch("taskgen.maintenance.clean_intermediate.project_root", return_value=project):
                exit_code = command_clean(
                    SimpleNamespace(apply=True, drop_manifest=False, force_active=False)
                )

            self.assertEqual(exit_code, 0)
            self.assertTrue(reviews.is_dir())
            self.assertFalse(reviews.is_symlink())
            self.assertEqual(victim.read_text(encoding="utf-8"), "external")

    @unittest.skipUnless(hasattr(os, "symlink"), "symbolic links are unavailable")
    def test_cleanup_skips_unrelated_symlink_while_removing_local_pycache(self) -> None:
        with (
            tempfile.TemporaryDirectory() as project_tmp,
            tempfile.TemporaryDirectory() as outside_tmp,
        ):
            project = Path(project_tmp)
            local_cache = project / "src/local/__pycache__"
            local_cache.mkdir(parents=True)
            (local_cache / "module.pyc").write_bytes(b"cache")
            external_cache = Path(outside_tmp) / "vendor/__pycache__"
            external_cache.mkdir(parents=True)
            external_file = external_cache / "vendor.pyc"
            external_file.write_bytes(b"external")
            (project / "src/vendor").symlink_to(
                external_cache.parent,
                target_is_directory=True,
            )

            with patch("taskgen.maintenance.clean_intermediate.project_root", return_value=project):
                exit_code = command_clean(
                    SimpleNamespace(apply=True, drop_manifest=False, force_active=False)
                )

            self.assertEqual(exit_code, 0)
            self.assertFalse(local_cache.exists())
            self.assertEqual(external_file.read_bytes(), b"external")


class Phase7FinalizeTests(unittest.TestCase):
    def write_generated_task(self, project: Path) -> Path:
        task = project / "generated/working/seed-a/idea-1"
        (task / "environment").mkdir(parents=True)
        (task / "solution").mkdir()
        (task / "tests").mkdir()
        (task / "instruction.md").write_text("Task\n", encoding="utf-8")
        (task / "task.toml").write_text('version = "1.0"\n', encoding="utf-8")
        (task / "environment/Dockerfile").write_text("FROM ubuntu:24.04\n", encoding="utf-8")
        (task / "solution/solve.sh").write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
        (task / "tests/Dockerfile").write_text("FROM ubuntu:24.04\n", encoding="utf-8")
        (task / "tests/test.sh").write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
        return task

    def write_phase4_status(
        self,
        project: Path,
        task: Path,
        *,
        passed: bool = True,
        oracle_reward: float | None = 1.0,
        nop_reward: float | None = 0.0,
        oracle_exit: int = 0,
        nop_exit: int = 0,
    ) -> None:
        task_id = "seed-a__idea-1"
        out_dir = project / "runs/oracle-nop-check" / task_id
        jobs_dir = out_dir / "harbor-jobs/run-1"
        oracle_job = jobs_dir / "oracle"
        nop_job = jobs_dir / "nop"
        oracle_job.mkdir(parents=True)
        nop_job.mkdir(parents=True)
        oracle_log = out_dir / "oracle.log"
        nop_log = out_dir / "nop.log"
        oracle_log.write_text("oracle", encoding="utf-8")
        nop_log.write_text("nop", encoding="utf-8")
        status = {
            "task_id": task_id,
            "task_path": str(task.resolve()),
            "run_id": "run-1",
            "task_tree_sha256": directory_tree_sha256(task),
            "passed": passed,
            "oracle": {
                "exit_code": oracle_exit,
                "reward": oracle_reward,
                "log": str(oracle_log),
                "job_dir": str(oracle_job),
                "timed_out": oracle_exit == 124,
                "timeout_sec": 10800.0,
            },
            "nop": {
                "exit_code": nop_exit,
                "reward": nop_reward,
                "log": str(nop_log),
                "job_dir": str(nop_job),
                "timed_out": nop_exit == 124,
                "timeout_sec": 10800.0,
            },
            "jobs_dir": str(jobs_dir),
        }
        (out_dir / "oracle-nop-status.json").write_text(json.dumps(status), encoding="utf-8")
        append_phase4_manifest_event(project, "seed-a", "idea-1", status)

    def write_review(self, project: Path, decision: str) -> None:
        review_dir = project / "runs/reviews/seed-a__idea-1"
        review_dir.mkdir(parents=True)
        modification_items = []
        blocking_reasons = []
        if decision == "needs_modification":
            modification_items = [
                {
                    "area": "instruction",
                    "priority": "must_fix",
                    "message": "Instruction must be shortened.",
                    "evidence": ["instruction.md is verbose"],
                    "repair_direction": "Rewrite as a compact task statement.",
                }
            ]
        if decision == "rejected":
            blocking_reasons = [
                {
                    "area": "difficulty",
                    "message": "Task concept is unsuitable.",
                    "evidence": ["reviewer rejected the concept"],
                }
            ]
        (review_dir / "review.json").write_text(
            json.dumps(
                {
                    "task_id": "seed-a__idea-1",
                    "decision": decision,
                    "summary": "Review summary.",
                    "modification_items": modification_items,
                    "blocking_reasons": blocking_reasons,
                }
            ),
            encoding="utf-8",
        )
        (review_dir / "review.md").write_text(
            review_markdown_fixture(decision),
            encoding="utf-8",
        )
        session_ref = write_fake_claude_session(project, "task-review", "seed-a__idea-1")
        append_phase5_manifest_event(project, "seed-a", "idea-1", decision, session_ref)

    def prepare_inputs(self, project: Path, decision: str) -> Path:
        task = self.write_generated_task(project)
        self.write_phase4_status(project, task)
        self.write_review(project, decision)
        return task

    def prepare_interrupted_ready_switch(
        self,
        project: Path,
        *,
        nonce: str,
        corrupt_destination: bool = False,
    ) -> dict[str, Path]:
        source = self.prepare_inputs(project, "ready")
        accepted = accepted_task_path(project, "seed-a", "idea-1")
        rejected = rejected_task_path(project, "seed-a", "idea-1")
        accepted.mkdir(parents=True)
        rejected.mkdir(parents=True)
        (accepted / "old.txt").write_text("accepted", encoding="utf-8")
        (rejected / "old.txt").write_text("rejected", encoding="utf-8")
        stage = accepted.parent / f".taskgen-stage-{nonce}"
        destination_backup = accepted.parent / f".taskgen-final-backup-{nonce}"
        counterpart_backup = rejected.parent / f".taskgen-counterpart-backup-{nonce}"
        source_backup = source.parent / f".taskgen-working-backup-{nonce}"
        shutil.copytree(source, stage)
        os.replace(accepted, destination_backup)
        os.replace(rejected, counterpart_backup)
        os.replace(stage, accepted)
        os.replace(source, source_backup)
        if corrupt_destination:
            (accepted / "task.toml").write_text('version = "corrupted"\n', encoding="utf-8")
        journal = finalization_journal_path(project, source)
        journal.parent.mkdir(parents=True)
        journal.write_text(
            json.dumps(
                {
                    "state": "switched",
                    "source": str(source),
                    "destination": str(accepted),
                    "counterpart": str(rejected),
                    "stage": str(stage),
                    "destination_backup": str(destination_backup),
                    "counterpart_backup": str(counterpart_backup),
                    "source_backup": str(source_backup),
                    "destination_existed": True,
                    "counterpart_existed": True,
                }
            ),
            encoding="utf-8",
        )
        return {
            "source": source,
            "destination": accepted,
            "counterpart": rejected,
            "stage": stage,
            "destination_backup": destination_backup,
            "counterpart_backup": counterpart_backup,
            "source_backup": source_backup,
            "journal": journal,
        }

    def test_validates_ready_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            task = self.prepare_inputs(project, "ready")
            rejected = rejected_task_path(project, "seed-a", "idea-1")
            rejected.mkdir(parents=True)

            move_final_task(task, accepted_task_path(project, "seed-a", "idea-1"), rejected)
            append_phase7_manifest_event(project, "seed-a", "idea-1", "ready")

            report = validate_phase7(project, "seed-a", "idea-1")

            self.assertEqual(report.errors, [])
            self.assertFalse(rejected.exists())
            self.assertFalse(task.exists())

    def test_phase7_rejects_needs_modification_review(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            self.prepare_inputs(project, "needs_modification")

            errors = ensure_phase7_inputs(project, "seed-a", "idea-1")

            self.assertIn("needs_modification", "\n".join(errors))

    def test_phase7_requires_passing_phase4_status(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            task = self.write_generated_task(project)
            self.write_phase4_status(
                project,
                task,
                passed=False,
                oracle_reward=0.0,
                nop_reward=1.0,
                oracle_exit=1,
            )
            self.write_review(project, "ready")

            errors = ensure_phase7_inputs(project, "seed-a", "idea-1")

            self.assertIn("phase7 requires phase4 oracle/nop to pass", "\n".join(errors))

    def test_validates_rejected_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            task = self.prepare_inputs(project, "rejected")

            move_final_task(task, rejected_task_path(project, "seed-a", "idea-1"), accepted_task_path(project, "seed-a", "idea-1"))
            append_phase7_manifest_event(project, "seed-a", "idea-1", "rejected")

            report = validate_phase7(project, "seed-a", "idea-1")

            self.assertEqual(report.errors, [])
            self.assertTrue(rejected_task_path(project, "seed-a", "idea-1").is_dir())
            self.assertFalse(accepted_task_path(project, "seed-a", "idea-1").exists())
            self.assertFalse(task.exists())

    def test_rejected_finalization_accepts_reviewable_failed_phase4_status(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            task = self.write_generated_task(project)
            self.write_phase4_status(
                project,
                task,
                passed=False,
                oracle_reward=0.0,
                nop_reward=1.0,
                oracle_exit=1,
            )
            self.write_review(project, "rejected")

            self.assertEqual(ensure_phase7_inputs(project, "seed-a", "idea-1"), [])
            move_final_task(
                task,
                rejected_task_path(project, "seed-a", "idea-1"),
                accepted_task_path(project, "seed-a", "idea-1"),
            )
            append_phase7_manifest_event(project, "seed-a", "idea-1", "rejected")

            report = validate_phase7(project, "seed-a", "idea-1")
            self.assertEqual(report.errors, [])
            self.assertIn("available for phase5 review", "\n".join(report.warnings))

    def test_phase7_manifest_failure_leaves_valid_final_for_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            source = self.prepare_inputs(project, "ready")
            accepted = accepted_task_path(project, "seed-a", "idea-1")
            rejected = rejected_task_path(project, "seed-a", "idea-1")
            accepted.mkdir(parents=True)
            rejected.mkdir(parents=True)
            (accepted / "old-accepted.txt").write_text("accepted", encoding="utf-8")
            (rejected / "old-rejected.txt").write_text("rejected", encoding="utf-8")

            with patch(
                "taskgen.phases.phase7_finalize.append_manifest_event",
                side_effect=FinalizationError("simulated manifest failure"),
            ):
                exit_code = run_phase7_locked(
                    project,
                    SimpleNamespace(seed_id="seed-a", idea_id="idea-1", dry_run=False),
                )

            self.assertEqual(exit_code, 1)
            self.assertFalse(source.exists())
            self.assertTrue((accepted / "task.toml").is_file())
            self.assertFalse((accepted / "old-accepted.txt").exists())
            self.assertFalse(rejected.exists())
            transaction_artifacts = [
                path
                for root in (source.parent, accepted.parent, rejected.parent)
                for path in root.glob(".*-*")
            ]
            self.assertEqual(transaction_artifacts, [])

            recovery_exit = run_phase7_locked(
                project,
                SimpleNamespace(seed_id="seed-a", idea_id="idea-1", dry_run=False),
            )
            self.assertEqual(recovery_exit, 0)
            self.assertEqual(validate_phase7(project, "seed-a", "idea-1").errors, [])

    def test_phase7_stage_fsync_failure_rolls_back_without_switching_paths(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            source = self.prepare_inputs(project, "ready")
            accepted = accepted_task_path(project, "seed-a", "idea-1")
            rejected = rejected_task_path(project, "seed-a", "idea-1")
            accepted.mkdir(parents=True)
            rejected.mkdir(parents=True)
            (accepted / "old.txt").write_text("accepted", encoding="utf-8")
            (rejected / "old.txt").write_text("rejected", encoding="utf-8")

            with patch(
                "taskgen.phases.phase7_finalize.fsync_path_tree",
                side_effect=OSError("simulated fsync failure"),
            ):
                exit_code = run_phase7_locked(
                    project,
                    SimpleNamespace(seed_id="seed-a", idea_id="idea-1", dry_run=False),
                )

            self.assertEqual(exit_code, 1)
            self.assertTrue((source / "task.toml").is_file())
            self.assertEqual((accepted / "old.txt").read_text(encoding="utf-8"), "accepted")
            self.assertEqual((rejected / "old.txt").read_text(encoding="utf-8"), "rejected")
            self.assertFalse(finalization_journal_path(project, source).exists())
            transaction_artifacts = [
                path
                for root in (source.parent, accepted.parent, rejected.parent)
                for path in root.glob(".*-*")
            ]
            self.assertEqual(transaction_artifacts, [])

    def test_phase7_recovers_final_task_when_manifest_event_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            source = self.prepare_inputs(project, "ready")
            accepted = accepted_task_path(project, "seed-a", "idea-1")
            rejected = rejected_task_path(project, "seed-a", "idea-1")
            move_final_task(source, accepted, rejected)

            exit_code = run_phase7_locked(
                project,
                SimpleNamespace(seed_id="seed-a", idea_id="idea-1", dry_run=False),
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(validate_phase7(project, "seed-a", "idea-1").errors, [])
            manifest_text = (project / "runs/task-manifest.jsonl").read_text(encoding="utf-8")
            self.assertIn('"event": "accepted"', manifest_text)

    def test_phase7_recovers_kill_interrupted_switch_and_cleans_hidden_backups(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            source = self.prepare_inputs(project, "ready")
            accepted = accepted_task_path(project, "seed-a", "idea-1")
            rejected = rejected_task_path(project, "seed-a", "idea-1")
            accepted.mkdir(parents=True)
            rejected.mkdir(parents=True)
            (accepted / "old.txt").write_text("accepted", encoding="utf-8")
            (rejected / "old.txt").write_text("rejected", encoding="utf-8")
            nonce = "b" * 32
            stage = accepted.parent / f".taskgen-stage-{nonce}"
            destination_backup = accepted.parent / f".taskgen-final-backup-{nonce}"
            counterpart_backup = rejected.parent / f".taskgen-counterpart-backup-{nonce}"
            source_backup = source.parent / f".taskgen-working-backup-{nonce}"
            shutil.copytree(source, stage)
            os.replace(accepted, destination_backup)
            os.replace(rejected, counterpart_backup)
            os.replace(stage, accepted)
            os.replace(source, source_backup)
            journal = finalization_journal_path(project, source)
            journal.parent.mkdir(parents=True)
            journal.write_text(
                json.dumps(
                    {
                        "state": "switched",
                        "source": str(source),
                        "destination": str(accepted),
                        "counterpart": str(rejected),
                        "stage": str(stage),
                        "destination_backup": str(destination_backup),
                        "counterpart_backup": str(counterpart_backup),
                        "source_backup": str(source_backup),
                        "destination_existed": True,
                        "counterpart_existed": True,
                    }
                ),
                encoding="utf-8",
            )

            exit_code = run_phase7_locked(
                project,
                SimpleNamespace(seed_id="seed-a", idea_id="idea-1", dry_run=False),
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(validate_phase7(project, "seed-a", "idea-1").errors, [])
            self.assertFalse(source.exists())
            self.assertFalse(rejected.exists())
            self.assertFalse(journal.exists())
            for temporary in (
                stage,
                destination_backup,
                counterpart_backup,
                source_backup,
            ):
                self.assertFalse(os.path.lexists(temporary))

    def test_phase7_dry_run_only_plans_pending_commit_or_rollback(self) -> None:
        for corrupt_destination, expected_action in ((False, "commit"), (True, "rollback")):
            with (
                self.subTest(expected_action=expected_action),
                tempfile.TemporaryDirectory() as project_tmp,
            ):
                project = Path(project_tmp)
                paths = self.prepare_interrupted_ready_switch(
                    project,
                    nonce=("2" if corrupt_destination else "3") * 32,
                    corrupt_destination=corrupt_destination,
                )

                def snapshot() -> dict[str, tuple[str, object]]:
                    result: dict[str, tuple[str, object]] = {}
                    for name, path in paths.items():
                        if path.is_dir() and not path.is_symlink():
                            result[name] = ("directory", directory_tree_sha256(path))
                        elif path.is_file() and not path.is_symlink():
                            result[name] = ("file", path.read_bytes())
                        elif path.is_symlink():
                            result[name] = ("symlink", os.readlink(path))
                        else:
                            result[name] = ("missing", None)
                    return result

                before = snapshot()
                manifest_before = (project / "runs/task-manifest.jsonl").read_bytes()

                exit_code = run_phase7_locked(
                    project,
                    SimpleNamespace(seed_id="seed-a", idea_id="idea-1", dry_run=True),
                )

                self.assertEqual(exit_code, 0)
                self.assertEqual(snapshot(), before)
                self.assertEqual(
                    (project / "runs/task-manifest.jsonl").read_bytes(),
                    manifest_before,
                )

    def test_phase7_rolls_back_corrupted_interrupted_destination(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            source = self.prepare_inputs(project, "ready")
            accepted = accepted_task_path(project, "seed-a", "idea-1")
            rejected = rejected_task_path(project, "seed-a", "idea-1")
            accepted.mkdir(parents=True)
            rejected.mkdir(parents=True)
            (accepted / "old.txt").write_text("accepted", encoding="utf-8")
            (rejected / "old.txt").write_text("rejected", encoding="utf-8")
            nonce = "d" * 32
            stage = accepted.parent / f".taskgen-stage-{nonce}"
            destination_backup = accepted.parent / f".taskgen-final-backup-{nonce}"
            counterpart_backup = rejected.parent / f".taskgen-counterpart-backup-{nonce}"
            source_backup = source.parent / f".taskgen-working-backup-{nonce}"
            shutil.copytree(source, stage)
            os.replace(accepted, destination_backup)
            os.replace(rejected, counterpart_backup)
            os.replace(stage, accepted)
            os.replace(source, source_backup)
            (accepted / "task.toml").write_text('version = "corrupted"\n', encoding="utf-8")
            journal = finalization_journal_path(project, source)
            journal.parent.mkdir(parents=True)
            journal.write_text(
                json.dumps(
                    {
                        "state": "switched",
                        "source": str(source),
                        "destination": str(accepted),
                        "counterpart": str(rejected),
                        "stage": str(stage),
                        "destination_backup": str(destination_backup),
                        "counterpart_backup": str(counterpart_backup),
                        "source_backup": str(source_backup),
                        "destination_existed": True,
                        "counterpart_existed": True,
                    }
                ),
                encoding="utf-8",
            )

            recover_interrupted_finalization(
                project,
                source,
                accepted,
                rejected,
                seed_id="seed-a",
                idea_id="idea-1",
            )

            self.assertTrue((source / "task.toml").is_file())
            self.assertEqual((accepted / "old.txt").read_text(encoding="utf-8"), "accepted")
            self.assertEqual((rejected / "old.txt").read_text(encoding="utf-8"), "rejected")
            self.assertFalse(journal.exists())
            for temporary in (
                stage,
                destination_backup,
                counterpart_backup,
                source_backup,
            ):
                self.assertFalse(os.path.lexists(temporary))

    def test_phase7_unrecoverable_rollback_leaves_every_path_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            source = self.prepare_inputs(project, "ready")
            accepted = accepted_task_path(project, "seed-a", "idea-1")
            rejected = rejected_task_path(project, "seed-a", "idea-1")
            accepted.mkdir(parents=True)
            rejected.mkdir(parents=True)
            (accepted / "old.txt").write_text("accepted", encoding="utf-8")
            (rejected / "old.txt").write_text("rejected", encoding="utf-8")
            nonce = "f" * 32
            stage = accepted.parent / f".taskgen-stage-{nonce}"
            destination_backup = accepted.parent / f".taskgen-final-backup-{nonce}"
            counterpart_backup = rejected.parent / f".taskgen-counterpart-backup-{nonce}"
            source_backup = source.parent / f".taskgen-working-backup-{nonce}"
            shutil.copytree(source, stage)
            os.replace(accepted, destination_backup)
            os.replace(rejected, counterpart_backup)
            os.replace(stage, accepted)
            os.replace(source, source_backup)
            shutil.rmtree(source_backup)
            (accepted / "task.toml").write_text('version = "corrupted"\n', encoding="utf-8")
            journal = finalization_journal_path(project, source)
            journal.parent.mkdir(parents=True)
            journal.write_text(
                json.dumps(
                    {
                        "state": "switched",
                        "source": str(source),
                        "destination": str(accepted),
                        "counterpart": str(rejected),
                        "stage": str(stage),
                        "destination_backup": str(destination_backup),
                        "counterpart_backup": str(counterpart_backup),
                        "source_backup": str(source_backup),
                        "destination_existed": True,
                        "counterpart_existed": True,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(FinalizationError, "cannot be rolled back safely"):
                recover_interrupted_finalization(
                    project,
                    source,
                    accepted,
                    rejected,
                    seed_id="seed-a",
                    idea_id="idea-1",
                )

            self.assertFalse(source.exists())
            self.assertEqual(
                (accepted / "task.toml").read_text(encoding="utf-8"),
                'version = "corrupted"\n',
            )
            self.assertEqual(
                (destination_backup / "old.txt").read_text(encoding="utf-8"),
                "accepted",
            )
            self.assertEqual(
                (counterpart_backup / "old.txt").read_text(encoding="utf-8"),
                "rejected",
            )
            self.assertTrue(journal.is_file())

    def test_phase7_committed_journal_with_working_source_is_left_for_manual_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            source = self.prepare_inputs(project, "ready")
            accepted = accepted_task_path(project, "seed-a", "idea-1")
            rejected = rejected_task_path(project, "seed-a", "idea-1")
            shutil.copytree(source, accepted)
            nonce = "1" * 32
            stage = accepted.parent / f".taskgen-stage-{nonce}"
            destination_backup = accepted.parent / f".taskgen-final-backup-{nonce}"
            counterpart_backup = rejected.parent / f".taskgen-counterpart-backup-{nonce}"
            source_backup = source.parent / f".taskgen-working-backup-{nonce}"
            journal = finalization_journal_path(project, source)
            journal.parent.mkdir(parents=True)
            journal.write_text(
                json.dumps(
                    {
                        "state": "committed",
                        "source": str(source),
                        "destination": str(accepted),
                        "counterpart": str(rejected),
                        "stage": str(stage),
                        "destination_backup": str(destination_backup),
                        "counterpart_backup": str(counterpart_backup),
                        "source_backup": str(source_backup),
                        "destination_existed": False,
                        "counterpart_existed": False,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(FinalizationError, "unexpectedly retains a working task"):
                recover_interrupted_finalization(
                    project,
                    source,
                    accepted,
                    rejected,
                    seed_id="seed-a",
                    idea_id="idea-1",
                )

            self.assertTrue((source / "task.toml").is_file())
            self.assertTrue((accepted / "task.toml").is_file())
            self.assertTrue(journal.is_file())

    @unittest.skipUnless(hasattr(os, "symlink"), "symbolic links are unavailable")
    def test_phase7_rejects_symlinked_final_parent_outside_project(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp, tempfile.TemporaryDirectory() as outside_tmp:
            project = Path(project_tmp)
            source = self.prepare_inputs(project, "ready")
            accepted_parent = project / "generated/accepted"
            accepted_parent.parent.mkdir(parents=True, exist_ok=True)
            accepted_parent.symlink_to(Path(outside_tmp), target_is_directory=True)

            exit_code = run_phase7_locked(
                project,
                SimpleNamespace(seed_id="seed-a", idea_id="idea-1", dry_run=False),
            )

            self.assertEqual(exit_code, 1)
            self.assertTrue(source.is_dir())
            self.assertEqual(list(Path(outside_tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
