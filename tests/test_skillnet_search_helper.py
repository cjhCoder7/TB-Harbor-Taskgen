from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
HELPER_PATH = (
    ROOT
    / "cc-definitions"
    / "skills"
    / "tb-harbor-task-generation"
    / "scripts"
    / "skillnet_search.py"
)
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from taskgen.claude.workspace import prepare_workspace
from taskgen.phases.phase2_skillnet_research import ensure_phase2_inputs


def load_helper_module():
    spec = importlib.util.spec_from_file_location("taskgen_skillnet_search_helper", HELPER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load helper module: {HELPER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helper = load_helper_module()


def response_payload(results: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {
            "success": True,
            "data": results,
            "meta": {"total": len(results), "search_mode": "keyword"},
        }
    ).encode("utf-8")


class SkillNetSearchRetryTests(unittest.TestCase):
    def run_search(self, request_fn, sleep_fn=lambda _delay: None, **overrides):
        arguments = {
            "query": "opa policy",
            "mode": "keyword",
            "limit": 10,
            "threshold": 0.65,
            "timeout": 15.0,
            "retries": 2,
            "backoff": 1.0,
            "api_url": "http://skillnet.invalid/v1/search",
            "request_fn": request_fn,
            "sleep_fn": sleep_fn,
        }
        arguments.update(overrides)
        return helper.search_with_retry(**arguments)

    def test_transient_failures_retry_in_order_then_succeed(self) -> None:
        responses = iter(
            [
                helper.HTTPResponse(503, {}, b""),
                helper.HTTPResponse(429, {"Retry-After": "0.25"}, b""),
                helper.HTTPResponse(
                    200,
                    {},
                    response_payload([{"skill_name": "policy-testing"}]),
                ),
            ]
        )
        request_urls: list[str] = []
        sleeps: list[float] = []

        def request(url: str, _timeout: float):
            request_urls.append(url)
            return next(responses)

        result = self.run_search(request, sleeps.append)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["attempt_count"], 3)
        self.assertEqual(
            [attempt["outcome"] for attempt in result["attempts"]],
            ["http_error", "http_error", "succeeded"],
        )
        self.assertEqual(sleeps, [1.0, 0.25])
        self.assertEqual(len(set(request_urls)), 1)
        query = parse_qs(urlsplit(request_urls[0]).query)
        self.assertEqual(query["q"], ["opa policy"])
        self.assertEqual(query["mode"], ["keyword"])
        self.assertNotIn("threshold", query)

    def test_non_retryable_4xx_stops_immediately(self) -> None:
        calls = 0
        sleeps: list[float] = []

        def request(_url: str, _timeout: float):
            nonlocal calls
            calls += 1
            return helper.HTTPResponse(404, {}, b"")

        result = self.run_search(request, sleeps.append)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["attempt_count"], 1)
        self.assertFalse(result["attempts"][0]["retryable"])
        self.assertEqual(calls, 1)
        self.assertEqual(sleeps, [])

    def test_network_failure_can_recover(self) -> None:
        calls = 0
        sleeps: list[float] = []

        def request(_url: str, _timeout: float):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise helper.URLError("temporary failure")
            return helper.HTTPResponse(200, {}, response_payload([]))

        result = self.run_search(request, sleeps.append)

        self.assertEqual(result["status"], "no_results")
        self.assertEqual(
            [attempt["outcome"] for attempt in result["attempts"]],
            ["network_error", "no_results"],
        )
        self.assertEqual(sleeps, [1.0])

    def test_protocol_failures_use_bounded_exponential_backoff(self) -> None:
        calls = 0
        sleeps: list[float] = []

        def request(_url: str, _timeout: float):
            nonlocal calls
            calls += 1
            return helper.HTTPResponse(200, {}, b"not-json")

        result = self.run_search(request, sleeps.append)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["attempt_count"], 3)
        self.assertEqual(calls, 3)
        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertEqual(
            [attempt["error_type"] for attempt in result["attempts"]],
            ["protocol_error", "protocol_error", "protocol_error"],
        )

    def test_empty_success_is_not_a_service_failure(self) -> None:
        result = self.run_search(
            lambda _url, _timeout: helper.HTTPResponse(200, {}, response_payload([]))
        )

        self.assertEqual(result["status"], "no_results")
        self.assertEqual(result["attempt_count"], 1)
        self.assertEqual(result["results"], [])

    def test_vector_request_uses_default_threshold(self) -> None:
        request_urls: list[str] = []

        def request(url: str, _timeout: float):
            request_urls.append(url)
            return helper.HTTPResponse(200, {}, response_payload([]))

        result = self.run_search(request, mode="vector")

        query = parse_qs(urlsplit(request_urls[0]).query)
        self.assertEqual(query["threshold"], ["0.65"])
        self.assertNotIn("page", query)
        self.assertEqual(result["parameters"]["threshold"], 0.65)

    def test_retry_after_rejects_non_finite_values_and_caps_delay(self) -> None:
        self.assertIsNone(helper._safe_retry_after({"Retry-After": "nan"}))
        self.assertIsNone(helper._safe_retry_after({"Retry-After": "inf"}))
        self.assertEqual(helper._safe_retry_after({"Retry-After": "90"}), 30.0)


class SkillNetSearchCliTests(unittest.TestCase):
    def test_cli_writes_json_and_retries_against_local_server(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            calls = 0
            queries: list[dict[str, list[str]]] = []

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                type(self).calls += 1
                type(self).queries.append(parse_qs(urlsplit(self.path).query))
                if type(self).calls == 1:
                    body = b'{"error":"temporary"}'
                    self.send_response(503)
                else:
                    body = response_payload([{"skill_name": "archive-analysis"}])
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as workspace_tmp:
                output = "output/skillnet/idea-1/raw/search-01-keyword.json"
                result = subprocess.run(
                    [
                        sys.executable,
                        str(HELPER_PATH),
                        "--query",
                        "archive format",
                        "--mode",
                        "keyword",
                        "--output",
                        output,
                        "--backoff",
                        "0.001",
                        "--api-url",
                        f"http://127.0.0.1:{server.server_port}/v1/search",
                    ],
                    cwd=workspace_tmp,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                evidence = json.loads((Path(workspace_tmp) / output).read_text(encoding="utf-8"))

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "succeeded")
            self.assertEqual(evidence["attempt_count"], 2)
            self.assertEqual(evidence["results"][0]["skill_name"], "archive-analysis")
            self.assertEqual(Handler.calls, 2)
            self.assertEqual(Handler.queries[0]["q"], ["archive format"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_cli_rejects_output_outside_skillnet_root(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    str(HELPER_PATH),
                    "--query",
                    "query",
                    "--output",
                    "../outside.json",
                ],
                cwd=workspace_tmp,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("output path must be below output/skillnet/", result.stderr)

    def test_cli_total_failure_still_writes_evidence_and_returns_one(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            calls = 0

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                type(self).calls += 1
                body = b'{"error":"temporary"}'
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as workspace_tmp:
                output = "output/skillnet/idea-1/raw/failed.json"
                result = subprocess.run(
                    [
                        sys.executable,
                        str(HELPER_PATH),
                        "--query",
                        "query",
                        "--output",
                        output,
                        "--retries",
                        "1",
                        "--backoff",
                        "0.001",
                        "--api-url",
                        f"http://127.0.0.1:{server.server_port}/v1/search",
                    ],
                    cwd=workspace_tmp,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                evidence = json.loads((Path(workspace_tmp) / output).read_text(encoding="utf-8"))

            self.assertEqual(result.returncode, 1)
            self.assertEqual(evidence["status"], "failed")
            self.assertEqual(evidence["attempt_count"], 2)
            self.assertEqual(Handler.calls, 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_cli_rejects_unbounded_or_non_finite_retry_options(self) -> None:
        cases = (("--timeout", "nan"), ("--backoff", "inf"), ("--retries", "5"))
        with tempfile.TemporaryDirectory() as workspace_tmp:
            for option, value in cases:
                with self.subTest(option=option, value=value):
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(HELPER_PATH),
                            "--query",
                            "query",
                            "--output",
                            "output/skillnet/test.json",
                            option,
                            value,
                        ],
                        cwd=workspace_tmp,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )

                    self.assertEqual(result.returncode, 2)

    def test_output_root_symlink_cannot_escape_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_tmp, tempfile.TemporaryDirectory() as outside_tmp:
            workspace = Path(workspace_tmp)
            (workspace / "output").symlink_to(Path(outside_tmp), target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "must stay inside"):
                helper.resolve_output_path("output/skillnet/test.json", workspace)


class SkillNetSearchIntegrationTests(unittest.TestCase):
    def test_phase2_workspace_contains_runnable_helper(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            prompt = project / "prompts/skillnet-research.md"
            prompt.parent.mkdir(parents=True)
            prompt.write_text("prompt", encoding="utf-8")
            (project / "runs/brainstorm/seed-a").mkdir(parents=True)
            (project / "runs/brainstorm/seed-a/seed_brainstorm.json").write_text(
                "{}\n",
                encoding="utf-8",
            )
            skill_dir = project / "cc-definitions/skills/tb-harbor-task-generation"
            (skill_dir / "scripts").mkdir(parents=True)
            shutil.copy2(HELPER_PATH, skill_dir / "scripts/skillnet_search.py")
            (skill_dir / "SKILL.md").write_text("skill", encoding="utf-8")

            payload = prepare_workspace(project, "skillnet-research", "seed-a", prompt, "run-1")
            copied_helper = (
                Path(str(payload["workspace_dir"]))
                / ".claude/skills/tb-harbor-task-generation/scripts/skillnet_search.py"
            )
            result = subprocess.run(
                [sys.executable, str(copied_helper), "--help"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertTrue(copied_helper.is_file())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Invoke this helper separately for each query", result.stdout)

    def test_phase2_preflight_requires_helper(self) -> None:
        with tempfile.TemporaryDirectory() as project_tmp:
            project = Path(project_tmp)
            required_without_helper = (
                "runs/brainstorm/seed-a/seed_brainstorm.json",
                "prompts/skillnet-research.md",
                "cc-definitions/agents/skillnet-researcher.md",
                "cc-definitions/skills/tb-harbor-task-generation/SKILL.md",
                "scripts/run-claude-logged.sh",
            )
            for relative in required_without_helper:
                path = project / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n", encoding="utf-8")

            errors = ensure_phase2_inputs(project, "seed-a")

            self.assertEqual(len(errors), 1)
            self.assertIn("scripts/skillnet_search.py", errors[0])

    def test_prompt_uses_serial_helper_without_global_vector_circuit_breaker(self) -> None:
        prompt = (ROOT / "prompts/skillnet-research.md").read_text(encoding="utf-8")

        self.assertIn("one query at a time", prompt)
        self.assertIn("scripts/skillnet_search.py", prompt)
        self.assertIn("threshold `0.65`", prompt)
        self.assertIn("If it fails, do not run a second keyword query", prompt)
        self.assertNotIn("After the first server-side vector failure", prompt)


if __name__ == "__main__":
    unittest.main()
