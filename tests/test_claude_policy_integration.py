from __future__ import annotations

import json
import os
import pwd
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CLAUDE_BINARY = ROOT / "cc-binary/claude-2.1.169-linux-x64"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from taskgen.claude import workspace as claude_workspace


ResponseFactory = Callable[[int, dict[str, Any]], bytes]


def _message(message_id: str) -> dict[str, Any]:
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-8",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    return "".join(
        f"event: {event_name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
        for event_name, payload in events
    ).encode("utf-8")


def _tool_response(
    message_id: str,
    tool_use_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
) -> bytes:
    return _sse(
        [
            ("message_start", {"type": "message_start", "message": _message(message_id)}),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": tool_use_id,
                        "name": tool_name,
                        "input": {},
                    },
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(tool_input, separators=(",", ":")),
                    },
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                    "usage": {"output_tokens": 1},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
    )


def _text_response(message_id: str, text: str) -> bytes:
    return _sse(
        [
            ("message_start", {"type": "message_start", "message": _message(message_id)}),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 1},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
    )


class _FakeAnthropicServer:
    def __init__(self, response_factory: ResponseFactory) -> None:
        self.requests: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self._response_factory = response_factory
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *args: object) -> None:
                del args

            def do_POST(self) -> None:
                try:
                    length = int(self.headers.get("content-length", "0"))
                    request = json.loads(self.rfile.read(length))
                    if not isinstance(request, dict):
                        raise TypeError("Anthropic request must be a JSON object")
                    index = len(owner.requests)
                    owner.requests.append(request)
                    response = owner._response_factory(index, request)
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("cache-control", "no-cache")
                    self.send_header("content-length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)
                except (BrokenPipeError, ConnectionResetError):
                    return
                except BaseException as exc:  # pragma: no cover - diagnostic path
                    owner.errors.append(f"{type(exc).__name__}: {exc}")
                    self.send_error(500)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> _FakeAnthropicServer:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class ClaudePolicyIntegrationTests(unittest.TestCase):
    """Exercise project settings with Claude Code itself and a local fake API."""

    launch_prefix: list[str]
    run_uid: int
    run_gid: int
    hook_python: Path

    @classmethod
    def setUpClass(cls) -> None:
        if not CLAUDE_BINARY.is_file() or not os.access(CLAUDE_BINARY, os.X_OK):
            raise unittest.SkipTest(f"Claude Code integration binary is absent: {CLAUDE_BINARY}")

        cls.launch_prefix = []
        cls.run_uid = os.geteuid()
        cls.run_gid = os.getegid()
        cls.hook_python = Path(sys.executable)
        if os.geteuid() == 0:
            setpriv = shutil.which("setpriv")
            system_python = Path("/usr/bin/python3")
            if setpriv is None:
                raise unittest.SkipTest("root integration run requires setpriv")
            if not system_python.is_file() or not os.access(system_python, os.X_OK):
                raise unittest.SkipTest("root integration run requires /usr/bin/python3 for hooks")
            try:
                nobody = pwd.getpwnam("nobody")
            except KeyError as exc:
                raise unittest.SkipTest("root integration run requires a nobody account") from exc
            if nobody.pw_uid == 0 or nobody.pw_gid == 0:
                raise unittest.SkipTest("nobody must be an unprivileged account")
            cls.run_uid = nobody.pw_uid
            cls.run_gid = nobody.pw_gid
            cls.hook_python = system_python
            cls.launch_prefix = [
                setpriv,
                f"--reuid={cls.run_uid}",
                f"--regid={cls.run_gid}",
                "--clear-groups",
            ]

        version = subprocess.run(
            [*cls.launch_prefix, str(CLAUDE_BINARY), "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
        if version.returncode != 0 or "2.1.169" not in version.stdout:
            raise unittest.SkipTest(
                f"expected Claude Code 2.1.169, got: {version.stdout or version.stderr}"
            )

    def _install_generated_settings(self, workspace: Path) -> tuple[Path, Path]:
        # A root test process is dropped to nobody before Claude starts. Generate
        # the same exec-form hook with a Python executable that remains reachable
        # after that privilege drop.
        with patch.object(claude_workspace.sys, "executable", str(self.hook_python)):
            return claude_workspace.install_worktree_guard(workspace)

    def _handoff_tree(self, root: Path) -> None:
        if self.run_uid == os.geteuid() and self.run_gid == os.getegid():
            return
        for path in [root, *root.rglob("*")]:
            os.chown(path, self.run_uid, self.run_gid, follow_symlinks=False)

    def _environment(self, temporary: Path, server: _FakeAnthropicServer) -> dict[str, str]:
        home = temporary / "home"
        config = temporary / "config"
        home.mkdir(exist_ok=True)
        config.mkdir(exist_ok=True)
        env = os.environ.copy()
        for key in (
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_FOUNDRY",
            "CLAUDE_CODE_USE_VERTEX",
        ):
            env.pop(key, None)
        env.update(
            {
                "ANTHROPIC_API_KEY": "offline-integration-test-key",
                "ANTHROPIC_BASE_URL": server.base_url,
                "CLAUDE_CONFIG_DIR": str(config),
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_AUTOUPDATER": "1",
                "DISABLE_ERROR_REPORTING": "1",
                "DISABLE_TELEMETRY": "1",
                "HOME": str(home),
                "IS_SANDBOX": "1",
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
                "SHELL": "/bin/bash",
            }
        )
        return env

    def _command(self, *extra: str) -> list[str]:
        return [
            *self.launch_prefix,
            str(CLAUDE_BINARY),
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-chrome",
            "--no-session-persistence",
            "--verbose",
            "--output-format=stream-json",
            "--model",
            "claude-opus-4-8",
            "--permission-mode",
            "bypassPermissions",
            *extra,
            "--print",
            "--",
            "offline policy integration test",
        ]

    def _run(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float = 45,
    ) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            self.fail(
                f"Claude Code offline integration timed out\nstdout:\n{stdout}\nstderr:\n{stderr}"
            )
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def _stream_events(self, result: subprocess.CompletedProcess[str]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    def _init_repository(self, workspace: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
        subprocess.run(
            ["git", "config", "user.email", "policy-test@example.invalid"],
            cwd=workspace,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Policy Test"], cwd=workspace, check=True
        )
        (workspace / "tracked.txt").write_text("policy integration needle\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=workspace, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=workspace, check=True)

    def _worktree_count(self, workspace: Path) -> int:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={workspace}",
                "worktree",
                "list",
                "--porcelain",
            ],
            cwd=workspace,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return sum(line.startswith("worktree ") for line in result.stdout.splitlines())

    def test_generated_deny_rules_are_enforced_by_claude_code(self) -> None:
        dangerous_commands = [
            ("find_root_exact", "false && find /"),
            ("find_root_trailing_args", "find / -maxdepth 0"),
            ("find_root_last_arg", "find -maxdepth 0 /"),
            ("grep_R_root", "false && grep -R taskgen-policy-never /"),
            ("grep_r_root", "false && grep -r taskgen-policy-never /"),
            ("grep_recursive_root", "false && grep --recursive taskgen-policy-never /"),
            ("grep_late_R_root", "false && grep taskgen-policy-never -R /"),
            ("rg_root", "false && rg taskgen-policy-never /"),
            ("rg_files_root", "false && rg --files /"),
            ("du_root_exact", "false && du /"),
            ("du_root_last_arg", "du --exclude='*' /"),
            ("du_root_trailing_args", "du / --exclude='*'"),
            ("ls_R_root", "false && ls -R /"),
            ("ls_recursive_root", "false && ls --recursive /"),
            ("ls_late_R_root", "false && ls / -R"),
            ("ls_late_recursive_root", "false && ls / --recursive"),
            ("find_parent_exact", "false && find .."),
            ("find_parent_trailing_args", "find .. -maxdepth 0"),
            ("find_parent_path", "false && find ../sibling"),
            ("grep_R_parent", "false && grep -R taskgen-policy-never .."),
            ("grep_recursive_parent_path", "false && grep --recursive taskgen-policy-never ../sibling"),
            ("rg_parent", "false && rg taskgen-policy-never .."),
            ("rg_files_parent_path", "false && rg --files ../sibling"),
            ("du_parent_exact", "false && du .."),
            ("du_parent_path", "false && du ../sibling"),
            ("ls_R_parent", "false && ls -R .."),
            ("ls_recursive_parent_path", "false && ls --recursive ../sibling"),
            ("ls_late_R_parent", "false && ls .. -R"),
            ("ls_late_recursive_parent_path", "false && ls ../sibling --recursive"),
            ("locate", "locate taskgen-policy-never"),
            ("git_worktree", "git worktree list"),
            ("absolute_git_worktree", "/usr/bin/git worktree list"),
            ("git_C_worktree", "git -C . worktree list"),
            ("absolute_git_C_worktree", "/usr/bin/git -C . worktree list"),
            ("git_global_option_worktree", "git --no-pager worktree list"),
        ]

        def respond(index: int, _request: dict[str, Any]) -> bytes:
            if index < len(dangerous_commands):
                label, command = dangerous_commands[index]
                return _tool_response(
                    f"msg_policy_{index}",
                    f"toolu_{label}",
                    "Bash",
                    {"command": command, "description": f"policy case {label}", "timeout": 5000},
                )
            return _text_response("msg_policy_done", "POLICY_DONE")

        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            workspace = temporary / "workspace"
            workspace.mkdir()
            self._init_repository(workspace)
            self._install_generated_settings(workspace)
            with _FakeAnthropicServer(respond) as server:
                env = self._environment(temporary, server)
                self._handoff_tree(temporary)
                result = self._run(self._command(), cwd=workspace, env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(server.errors, [])
        self.assertEqual(len(server.requests), len(dangerous_commands) + 1)
        events = self._stream_events(result)
        final = next(event for event in events if event.get("type") == "result")
        denied_ids = {
            denial["tool_use_id"] for denial in final.get("permission_denials", [])
        }
        expected_ids = {f"toolu_{label}" for label, _command in dangerous_commands}
        self.assertEqual(
            denied_ids,
            expected_ids,
            "Claude Code did not enforce every generated deny rule; "
            f"missing={sorted(expected_ids - denied_ids)}, "
            f"unexpected={sorted(denied_ids - expected_ids)}",
        )

    def test_workspace_local_near_misses_remain_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            workspace = temporary / "workspace"
            workspace.mkdir()
            self._init_repository(workspace)
            self._install_generated_settings(workspace)
            marker_dir = workspace / "markers"
            marker_dir.mkdir()
            local_commands: list[tuple[str, str, Path]] = []
            for label, local_command in (
                ("find_local", "find . -maxdepth 0 >/dev/null"),
                ("grep_local", "grep -R 'policy integration needle' . >/dev/null"),
                ("rg_local", "rg 'policy integration needle' . >/dev/null 2>&1"),
                ("du_local", "du . >/dev/null"),
                ("ls_local", "ls -R . >/dev/null"),
                ("ls_late_R_local", "ls . -R >/dev/null"),
                ("git_status", "git status --short >/dev/null"),
            ):
                marker = marker_dir / label
                command = f"{local_command}; printf ok > {shlex.quote(str(marker))}"
                local_commands.append((label, command, marker))

            def respond(index: int, _request: dict[str, Any]) -> bytes:
                if index < len(local_commands):
                    label, command, _marker = local_commands[index]
                    return _tool_response(
                        f"msg_local_{index}",
                        f"toolu_{label}",
                        "Bash",
                        {"command": command, "description": f"local case {label}", "timeout": 5000},
                    )
                return _text_response("msg_local_done", "LOCAL_DONE")

            with _FakeAnthropicServer(respond) as server:
                env = self._environment(temporary, server)
                self._handoff_tree(temporary)
                result = self._run(self._command(), cwd=workspace, env=env)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(server.errors, [])
            events = self._stream_events(result)
            final = next(event for event in events if event.get("type") == "result")
            self.assertEqual(final.get("permission_denials"), [])
            for label, _command, marker in local_commands:
                with self.subTest(label=label):
                    self.assertEqual(marker.read_text(encoding="utf-8"), "ok")

    def test_real_pre_tool_use_hooks_rewrite_task_and_deny_enter_worktree(self) -> None:
        def respond(index: int, _request: dict[str, Any]) -> bytes:
            if index == 0:
                return _tool_response(
                    "msg_task",
                    "toolu_task_policy",
                    "Task",
                    {
                        "description": "exercise task worktree guard",
                        "prompt": "Reply exactly SUBAGENT_OK and use no tools.",
                        "subagent_type": "general-purpose",
                        "isolation": "worktree",
                        "run_in_background": False,
                    },
                )
            if index == 1:
                return _text_response("msg_subagent", "SUBAGENT_OK")
            if index == 2:
                return _tool_response(
                    "msg_enter_worktree",
                    "toolu_enter_worktree_policy",
                    "EnterWorktree",
                    {"name": "must-not-exist"},
                )
            return _text_response("msg_hooks_done", "HOOKS_DONE")

        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            workspace = temporary / "workspace"
            workspace.mkdir()
            self._init_repository(workspace)
            settings_path, _guard_path = self._install_generated_settings(workspace)
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(
                settings["hooks"]["PreToolUse"][0]["matcher"],
                "Agent|Task|EnterWorktree",
            )
            with _FakeAnthropicServer(respond) as server:
                env = self._environment(temporary, server)
                self._handoff_tree(temporary)
                result = self._run(self._command(), cwd=workspace, env=env)

            self.assertEqual(self._worktree_count(workspace), 1)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(server.errors, [])
        self.assertEqual(len(server.requests), 4)
        events = self._stream_events(result)
        self.assertTrue(
            any(event.get("subtype") == "task_started" for event in events),
            result.stdout,
        )
        self.assertTrue(
            any(
                event.get("subtype") == "task_notification"
                and event.get("status") == "completed"
                for event in events
            ),
            result.stdout,
        )
        final = next(event for event in events if event.get("type") == "result")
        self.assertEqual(
            [denial["tool_use_id"] for denial in final.get("permission_denials", [])],
            ["toolu_enter_worktree_policy"],
        )
        third_request = json.dumps(server.requests[2], ensure_ascii=False)
        self.assertIn("PreToolUse:Agent hook additional context", third_request)
        self.assertIn("existing isolated workspace", third_request)
        self.assertIn("entering a Git worktree is disabled", result.stdout)

    def test_real_worktree_create_hook_aborts_before_any_api_request(self) -> None:
        def respond(_index: int, _request: dict[str, Any]) -> bytes:
            return _text_response("msg_unexpected", "UNEXPECTED_API_REQUEST")

        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            workspace = temporary / "workspace"
            workspace.mkdir()
            self._init_repository(workspace)
            self._install_generated_settings(workspace)
            with _FakeAnthropicServer(respond) as server:
                env = self._environment(temporary, server)
                self._handoff_tree(temporary)
                result = self._run(
                    self._command("--worktree", "must-not-exist"),
                    cwd=workspace,
                    env=env,
                )
            worktree_count = self._worktree_count(workspace)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(server.errors, [])
        self.assertEqual(server.requests, [], "WorktreeCreate must fail before inference")
        self.assertEqual(worktree_count, 1)
        self.assertIn("WorktreeCreate hook failed", result.stderr)
        self.assertIn("blocks Claude Code worktree creation", result.stderr)


if __name__ == "__main__":
    unittest.main()
