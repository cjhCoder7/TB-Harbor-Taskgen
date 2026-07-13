from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import taskgen.cli as cli_module
import taskgen.openai_gateway as gateway_module
from taskgen.cli import build_parser, command_pipeline, command_run, phase_runtime_options
from taskgen.config import (
    load_model_config,
    resolve_openai_effort_level,
    resolve_openai_model_name,
)
from taskgen.openai_gateway import openai_gateway


FAKE_UPSTREAM_URL = "https://unit-test-provider.invalid/v1"
FAKE_UPSTREAM_KEY = "unit-test-upstream-key"


def write_model_config(root: Path, payload: dict[str, object]) -> None:
    (root / "model.json").write_text(json.dumps(payload), encoding="utf-8")


@contextmanager
def mocked_gateway_runtime(
    *,
    environment: dict[str, str] | None = None,
    write_side_effect: object | None = None,
):
    process = MagicMock()
    process.pid = 24_680
    gateway_environment = environment or {
        "OPENAI_BASE_URL": FAKE_UPSTREAM_URL,
        "OPENAI_API_KEY": FAKE_UPSTREAM_KEY,
    }

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, gateway_environment, clear=True))
        stack.enter_context(
            patch.object(
                gateway_module,
                "_resolve_litellm_executable",
                return_value="/unit-test/litellm",
            )
        )
        stack.enter_context(
            patch.object(gateway_module, "_reserve_loopback_port", return_value=41_231)
        )
        stack.enter_context(
            patch.object(gateway_module.secrets, "token_urlsafe", return_value="local-token")
        )
        popen = stack.enter_context(
            patch.object(gateway_module.subprocess, "Popen", return_value=process)
        )
        wait_until_ready = stack.enter_context(
            patch.object(gateway_module, "_wait_until_ready")
        )
        terminate = stack.enter_context(
            patch.object(gateway_module, "_terminate_process_group")
        )
        stack.enter_context(
            patch.object(
                gateway_module,
                "_cleanup_signal_handlers",
                side_effect=lambda: nullcontext(),
            )
        )
        stack.enter_context(patch("builtins.print"))
        if write_side_effect is not None:
            stack.enter_context(
                patch.object(
                    gateway_module,
                    "_write_private_text",
                    side_effect=write_side_effect,
                )
            )

        yield SimpleNamespace(
            process=process,
            popen=popen,
            wait_until_ready=wait_until_ready,
            terminate=terminate,
        )


class OpenAIModelConfigTests(unittest.TestCase):
    def test_openai_config_is_strictly_parsed_and_kept_separate_from_claude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_model_config(
                root,
                {
                    "default_model": "claude-default",
                    "default_effort": "max",
                    "openai": {
                        "openai_default_model": "  vendor-native/model:preview  ",
                        "openai_default_effort": "medium",
                        "openai_phase_efforts": {
                            "phase1": "high",
                            "task-generation": "xhigh",
                        },
                    },
                },
            )

            config = load_model_config(root)

        self.assertEqual(config.default_model, "claude-default")
        self.assertEqual(config.default_effort, "max")
        self.assertIsNotNone(config.openai)
        assert config.openai is not None
        self.assertEqual(config.openai.default_model, "vendor-native/model:preview")
        self.assertEqual(config.openai.default_effort, "medium")
        self.assertEqual(config.openai.phase_efforts["phase1"], "high")
        self.assertEqual(config.openai.phase_efforts["task-generation"], "xhigh")

    def test_openai_config_rejects_invalid_shapes_unknown_keys_and_values(self) -> None:
        invalid_configs = (
            ({"openai": []}, r"model\.json\.openai must be an object"),
            (
                {"openai": {"default_model": "renamed"}},
                r"model\.json\.openai contains unknown key",
            ),
            (
                {"openai": {"openai_default_model": "  "}},
                r"openai_default_model must be a non-empty string",
            ),
            (
                {"openai": {"openai_default_effort": "extreme"}},
                r"openai_default_effort must be one of",
            ),
            (
                {"openai": {"openai_phase_efforts": []}},
                r"openai_phase_efforts must be an object",
            ),
            (
                {"openai": {"openai_phase_efforts": {"unknown-phase": "high"}}},
                r"openai_phase_efforts has unknown phase key",
            ),
            (
                {"openai": {"openai_phase_efforts": {"phase2": "extreme"}}},
                r"openai_phase_efforts\.phase2 must be one of",
            ),
        )

        for payload, message_pattern in invalid_configs:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                write_model_config(root, payload)
                with self.assertRaisesRegex(SystemExit, message_pattern):
                    load_model_config(root)

    def test_openai_resolution_prefers_explicit_then_phase_then_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_model_config(
                root,
                {
                    "openai": {
                        "openai_default_model": "native-default-model",
                        "openai_default_effort": "low",
                        "openai_phase_efforts": {"task-generation": "xhigh"},
                    }
                },
            )

            self.assertEqual(
                resolve_openai_model_name(root, "explicit-native-model"),
                "explicit-native-model",
            )
            self.assertEqual(resolve_openai_model_name(root, None), "native-default-model")
            self.assertEqual(
                resolve_openai_effort_level(root, "high", "phase3"),
                "high",
            )
            self.assertEqual(
                resolve_openai_effort_level(root, None, "phase3"),
                "xhigh",
            )
            self.assertEqual(
                resolve_openai_effort_level(root, None, "phase2"),
                "low",
            )

    def test_openai_resolution_never_falls_back_to_claude_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_model_config(
                root,
                {
                    "default_model": "claude-default",
                    "default_effort": "max",
                    "openai": {},
                },
            )

            with self.assertRaisesRegex(SystemExit, "openai_default_model"):
                resolve_openai_model_name(root, None)
            with self.assertRaisesRegex(SystemExit, "openai_default_effort"):
                resolve_openai_effort_level(root, None, "phase1")


class OpenAICliTests(unittest.TestCase):
    def test_parser_accepts_openai_for_run_pipeline_and_run_all(self) -> None:
        parser = build_parser()
        argument_vectors = (
            ["run", "phase1", "seed-a", "--openai"],
            ["pipeline", "seed-a", "--openai"],
            ["run-all", "seed-a", "--openai"],
        )

        for arguments in argument_vectors:
            with self.subTest(arguments=arguments):
                args = parser.parse_args(arguments)
                self.assertIs(args.openai, True)

        self.assertIs(
            parser.parse_args(["run", "phase1", "seed-a"]).openai,
            False,
        )

    def test_phase_runtime_options_preserves_native_model_name_and_phase_effort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_model_config(
                root,
                {
                    "openai": {
                        "openai_default_model": "provider/model.with-punctuation:2026",
                        "openai_default_effort": "medium",
                        "openai_phase_efforts": {"skillnet-research": "xhigh"},
                    }
                },
            )
            args = SimpleNamespace(openai=True, model=None, effort=None)

            model, effort = phase_runtime_options(root, args, "phase2")

            self.assertEqual(model, "provider/model.with-punctuation:2026")
            self.assertEqual(effort, "xhigh")

            explicit_args = SimpleNamespace(
                openai=True,
                model="another/provider-native-model",
                effort="high",
            )
            self.assertEqual(
                phase_runtime_options(root, explicit_args, "skillnet-research"),
                ("another/provider-native-model", "high"),
            )

    def test_run_dry_run_resolves_openai_options_without_starting_gateway(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["run", "phase1", "seed-a", "--openai", "--dry-run"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_model_config(
                root,
                {
                    "openai": {
                        "openai_default_model": "native-model",
                        "openai_default_effort": "low",
                        "openai_phase_efforts": {"phase1": "xhigh"},
                    }
                },
            )
            completed = SimpleNamespace(returncode=0)
            with (
                patch.object(cli_module, "project_root", return_value=root),
                patch.object(cli_module, "openai_gateway") as gateway,
                patch.object(cli_module.subprocess, "run", return_value=completed) as run,
            ):
                exit_code = command_run(args)

        self.assertEqual(exit_code, 0)
        gateway.assert_not_called()
        phase_command = run.call_args.args[0]
        self.assertIn("--dry-run", phase_command)
        self.assertEqual(
            phase_command[phase_command.index("--model") + 1],
            "native-model",
        )
        self.assertEqual(
            phase_command[phase_command.index("--effort") + 1],
            "xhigh",
        )

    def test_pipeline_dry_run_does_not_start_gateway(self) -> None:
        args = build_parser().parse_args(["pipeline", "seed-a", "--openai", "--dry-run"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(cli_module, "project_root", return_value=root),
                patch.object(cli_module, "pipeline_activity_lock", return_value=nullcontext()),
                patch.object(
                    cli_module,
                    "_command_pipeline_with_activity_lock",
                    return_value=0,
                ) as pipeline,
                patch.object(cli_module, "openai_gateway") as gateway,
            ):
                exit_code = command_pipeline(args)

        self.assertEqual(exit_code, 0)
        pipeline.assert_called_once_with(args)
        gateway.assert_not_called()

    def test_live_run_and_pipeline_each_start_exactly_one_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_model_config(
                root,
                {
                    "openai": {
                        "openai_default_model": "native-model",
                        "openai_default_effort": "high",
                    }
                },
            )
            completed = SimpleNamespace(returncode=0)
            run_args = build_parser().parse_args(["run", "phase1", "seed-a", "--openai"])
            with (
                patch.object(cli_module, "project_root", return_value=root),
                patch.object(cli_module.subprocess, "run", return_value=completed),
                patch.object(cli_module, "openai_gateway", return_value=nullcontext()) as gateway,
            ):
                self.assertEqual(command_run(run_args), 0)
            gateway.assert_called_once_with("native-model")

            pipeline_args = build_parser().parse_args(["pipeline", "seed-a", "--openai"])
            with (
                patch.object(cli_module, "project_root", return_value=root),
                patch.object(cli_module, "pipeline_activity_lock", return_value=nullcontext()),
                patch.object(
                    cli_module,
                    "_command_pipeline_with_activity_lock",
                    return_value=0,
                ),
                patch.object(cli_module, "openai_gateway", return_value=nullcontext()) as gateway,
            ):
                self.assertEqual(command_pipeline(pipeline_args), 0)
            gateway.assert_called_once_with("native-model")

    def test_openai_is_rejected_for_non_claude_phase(self) -> None:
        args = build_parser().parse_args(
            ["run", "phase4", "seed-a", "--idea-id", "idea-a", "--openai"]
        )
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            cli_module,
            "project_root",
            return_value=Path(tmp),
        ):
            with self.assertRaisesRegex(SystemExit, "phase4 does not accept --openai"):
                command_run(args)


class OpenAIShellEntryPointTests(unittest.TestCase):
    @staticmethod
    def _write_executable(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def test_taskgen_shell_sources_only_the_environment_selected_by_exact_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = root / "scripts"
            fake_bin = root / "bin"
            scripts.mkdir()
            fake_bin.mkdir()
            self._write_executable(
                scripts / "taskgen.sh",
                (ROOT / "scripts/taskgen.sh").read_text(encoding="utf-8"),
            )
            (scripts / "env_init.sh").write_text(
                "export SELECTED_ENV=claude\nexport DEFAULT_ONLY=present\n",
                encoding="utf-8",
            )
            (scripts / "env_openai_init.sh").write_text(
                "export SELECTED_ENV=openai\n",
                encoding="utf-8",
            )
            self._write_executable(
                fake_bin / "python3",
                "#!/usr/bin/env bash\n"
                "printf '%s|%s\\n' \"${SELECTED_ENV:-unset}\" \"${DEFAULT_ONLY:-absent}\"\n"
                "printf '<%s>' \"$@\"\n",
            )
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}:{environment.get('PATH', '')}"
            environment.pop("SELECTED_ENV", None)
            environment.pop("DEFAULT_ONLY", None)

            default_result = subprocess.run(
                [scripts / "taskgen.sh", "phases"],
                env=environment,
                check=True,
                text=True,
                capture_output=True,
            )
            openai_result = subprocess.run(
                [scripts / "taskgen.sh", "run", "phase1", "seed-a", "--openai"],
                env=environment,
                check=True,
                text=True,
                capture_output=True,
            )
            similar_result = subprocess.run(
                [scripts / "taskgen.sh", "phases", "--openai=false"],
                env=environment,
                check=True,
                text=True,
                capture_output=True,
            )

        self.assertTrue(default_result.stdout.startswith("claude|present\n"))
        self.assertTrue(openai_result.stdout.startswith("openai|absent\n"))
        self.assertIn("<--openai>", openai_result.stdout)
        self.assertTrue(similar_result.stdout.startswith("claude|present\n"))

    def test_child_wrappers_do_not_overwrite_active_gateway_environment(self) -> None:
        for wrapper_name in ("run-claude-logged.sh", "run-harbor-oracle-nop.sh"):
            with self.subTest(wrapper=wrapper_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                scripts = root / "scripts"
                fake_bin = root / "bin"
                scripts.mkdir()
                fake_bin.mkdir()
                self._write_executable(
                    scripts / wrapper_name,
                    (ROOT / f"scripts/{wrapper_name}").read_text(encoding="utf-8"),
                )
                (scripts / "env_init.sh").write_text(
                    "export ANTHROPIC_BASE_URL=https://default.invalid\n",
                    encoding="utf-8",
                )
                self._write_executable(
                    fake_bin / "python3",
                    "#!/usr/bin/env bash\n"
                    "printf '%s\\n' \"${ANTHROPIC_BASE_URL:-unset}\"\n",
                )
                environment = os.environ.copy()
                environment["PATH"] = f"{fake_bin}:{environment.get('PATH', '')}"
                environment["TASKGEN_OPENAI_GATEWAY_ACTIVE"] = "1"
                environment["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:41231"

                result = subprocess.run(
                    [scripts / wrapper_name],
                    env=environment,
                    check=True,
                    text=True,
                    capture_output=True,
                )

            self.assertEqual(result.stdout, "http://127.0.0.1:41231\n")


class OpenAIGatewayTests(unittest.TestCase):
    def test_temporary_gateway_config_uses_litellm_auth_without_intercepting_requests(self) -> None:
        written: dict[str, str] = {}

        def capture_write(path: Path, content: str) -> None:
            written[path.name] = content

        model = "provider-native/model:preview"
        with mocked_gateway_runtime(write_side_effect=capture_write):
            with openai_gateway(model):
                pass

        config_text = written["config.json"]
        config = json.loads(config_text)
        self.assertNotIn(FAKE_UPSTREAM_KEY, config_text)
        self.assertNotIn(FAKE_UPSTREAM_URL, config_text)
        self.assertEqual(config["model_list"][0]["model_name"], model)
        self.assertEqual(
            config["model_list"][0]["litellm_params"],
            {
                "model": f"openai/{model}",
                "api_base": "os.environ/OPENAI_BASE_URL",
                "api_key": "os.environ/OPENAI_API_KEY",
                "additional_drop_params": ["user"],
            },
        )
        self.assertEqual(
            config["general_settings"]["master_key"],
            "os.environ/LITELLM_MASTER_KEY",
        )
        auth_source = written.get("taskgen_litellm_auth.py", "")
        self.assertNotIn("count_tokens", auth_source)

    def test_claude_environment_is_isolated_then_fully_restored(self) -> None:
        original_environment = {
            "OPENAI_BASE_URL": FAKE_UPSTREAM_URL,
            "OPENAI_API_KEY": FAKE_UPSTREAM_KEY,
            "OPENROUTER_API_KEY": "old-router-key",
            "OPENROUTER_BASE_URL": "https://old-router.invalid/api",
            "ANTHROPIC_BASE_URL": "https://old-anthropic.invalid/api",
            "ANTHROPIC_AUTH_TOKEN": "old-auth-token",
            "ANTHROPIC_API_KEY": "old-anthropic-key",
            "ANTHROPIC_MODEL": "old-model",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "old-haiku",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "old-sonnet",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "old-opus",
            "CLAUDE_CODE_SUBAGENT_MODEL": "old-subagent",
            "MAX_THINKING_TOKENS": "8192",
            "UNRELATED_SETTING": "preserve-me",
        }
        model = "provider-native-model"

        with mocked_gateway_runtime(environment=original_environment) as runtime:
            baseline = dict(os.environ)
            with openai_gateway(model) as gateway:
                self.assertEqual(gateway.base_url, "http://127.0.0.1:41231")
                self.assertEqual(os.environ["ANTHROPIC_BASE_URL"], gateway.base_url)
                self.assertEqual(
                    os.environ["ANTHROPIC_AUTH_TOKEN"],
                    "sk-taskgen-local-token",
                )
                for removed_key in gateway_module.REMOVED_FROM_CLAUDE_ENV:
                    self.assertNotIn(removed_key, os.environ)
                for model_key in gateway_module.MODEL_ENV_KEYS:
                    self.assertEqual(os.environ[model_key], model)
                self.assertNotIn("CLAUDE_CODE_DISABLE_THINKING", os.environ)
                self.assertEqual(os.environ["MAX_THINKING_TOKENS"], "8192")
                self.assertEqual(os.environ["TASKGEN_OPENAI_GATEWAY_ACTIVE"], "1")
                self.assertEqual(os.environ["UNRELATED_SETTING"], "preserve-me")

                proxy_environment = runtime.popen.call_args.kwargs["env"]
                self.assertEqual(proxy_environment["OPENAI_BASE_URL"], FAKE_UPSTREAM_URL)
                self.assertEqual(proxy_environment["OPENAI_API_KEY"], FAKE_UPSTREAM_KEY)

            self.assertEqual(dict(os.environ), baseline)

    def test_proxy_does_not_force_chat_completions_for_anthropic_messages(self) -> None:
        environment = {
            "OPENAI_BASE_URL": FAKE_UPSTREAM_URL,
            "OPENAI_API_KEY": FAKE_UPSTREAM_KEY,
            "LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES": "true",
        }
        with mocked_gateway_runtime(environment=environment) as runtime:
            with openai_gateway("native-model"):
                proxy_environment = runtime.popen.call_args.kwargs["env"]
                self.assertNotIn(
                    "LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES",
                    proxy_environment,
                )

    def test_process_group_cleanup_runs_after_normal_and_exceptional_use(self) -> None:
        with mocked_gateway_runtime() as runtime:
            with openai_gateway("native-model"):
                pass
            runtime.terminate.assert_called_once_with(runtime.process, 10.0)

    def test_forced_process_group_cleanup_and_signal_exit_code(self) -> None:
        process = MagicMock()
        process.pid = 12_345
        with patch.object(gateway_module.os, "killpg") as kill_group:
            gateway_module._terminate_process_group(process, 0)

        self.assertEqual(
            kill_group.call_args_list,
            [call(12_345, signal.SIGTERM), call(12_345, signal.SIGKILL)],
        )
        process.wait.assert_called_once_with(timeout=0)

        with self.assertRaises(SystemExit) as raised:
            gateway_module._exit_for_signal(signal.SIGTERM, None)
        self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)

        with mocked_gateway_runtime() as runtime:
            with self.assertRaisesRegex(RuntimeError, "body failed"):
                with openai_gateway("native-model"):
                    raise RuntimeError("body failed")
            runtime.terminate.assert_called_once_with(runtime.process, 10.0)


if __name__ == "__main__":
    unittest.main()
