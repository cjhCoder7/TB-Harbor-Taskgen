from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from taskgen.claude.cost import OpenRouterQueryError
from taskgen.claude.cost import parse_claude_stream_log
from taskgen.claude.cost import format_cost_summary
from taskgen.claude.cost import summarize_claude_stream_log
from taskgen.claude.runner import build_claude_command
from taskgen.claude.workspace import (
    phase_input_paths,
    phase_output_paths,
    prepare_workspace,
    sync_workspace_outputs,
)
from taskgen.cli import build_phase_process_command, get_phase, load_pipeline_idea_ids, pipeline_phase1_count_matches
from taskgen.config import load_model_config, resolve_claude_code_path, resolve_effort_level
from taskgen.common import ValidationReport
from taskgen.phases.phase1_seed_brainstorm import render_phase1_prompt, validate_brainstorm_data
from taskgen.phases.phase2_skillnet_research import validate_phase2
from taskgen.phases.phase3_task_generation import render_phase3_prompt, validate_phase3
from taskgen.phases.phase4_oracle_nop_check import (
    append_manifest_event as append_phase4_manifest_event,
    command_run as command_run_phase4,
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
    accepted_task_path,
    append_manifest_event as append_phase7_manifest_event,
    ensure_phase7_inputs,
    move_final_task,
    rejected_task_path,
    validate_phase7,
)


def write_fake_claude_session(project: Path, phase: str, subject: str, run_id: str = "run-1") -> str:
    session = project / "runs/claude-sessions" / phase / subject / run_id
    session.mkdir(parents=True, exist_ok=True)
    (session / "status.json").write_text(json.dumps({"exit_code": 0}), encoding="utf-8")
    return session.relative_to(project).as_posix()


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
    def test_load_model_json_and_resolve_relative_claude_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "model.json").write_text(
                json.dumps(
                    {
                        "claude_code_path": "cc-binary/claude-test",
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
    def test_claude_command_blocks_full_filesystem_scans(self) -> None:
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

        self.assertIn("--disallowedTools", command)
        self.assertIn("Bash(*find / *)", command)
        self.assertIn("Bash(*grep -R / *)", command)
        self.assertIn("Bash(*rg --files / *)", command)
        self.assertIn("Bash(*locate *)", command)
        self.assertLess(command.index("--disallowedTools"), command.index("--print"))


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

            self.assertTrue((workspace / ".claude/agents/seed-brainstormer.md").is_file())
            self.assertTrue((workspace / ".claude/skills/demo-skill/SKILL.md").is_file())
            self.assertFalse((runtime / "skills").exists())

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
            "passed": passed,
            "oracle": {
                "exit_code": oracle_exit,
                "reward": oracle_reward,
                "log": str(oracle_log),
                "job_dir": str(oracle_job),
            },
            "nop": {
                "exit_code": nop_exit,
                "reward": nop_reward,
                "log": str(nop_log),
                "job_dir": str(nop_job),
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
            "passed": passed,
            "oracle": {
                "exit_code": oracle_exit,
                "reward": oracle_reward,
                "log": str(oracle_log),
                "job_dir": str(oracle_job),
            },
            "nop": {
                "exit_code": nop_exit,
                "reward": nop_reward,
                "log": str(nop_log),
                "job_dir": str(nop_job),
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
        (review_dir / "review.md").write_text("Final decision: ready\n", encoding="utf-8")
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
            "passed": passed,
            "oracle": {
                "exit_code": oracle_exit,
                "reward": oracle_reward,
                "log": str(oracle_log),
                "job_dir": str(oracle_job),
            },
            "nop": {
                "exit_code": nop_exit,
                "reward": nop_reward,
                "log": str(nop_log),
                "job_dir": str(nop_job),
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
        (review_dir / "review.md").write_text(f"Final decision: {decision}\n", encoding="utf-8")
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
            "passed": passed,
            "oracle": {
                "exit_code": oracle_exit,
                "reward": oracle_reward,
                "log": str(oracle_log),
                "job_dir": str(oracle_job),
            },
            "nop": {
                "exit_code": nop_exit,
                "reward": nop_reward,
                "log": str(nop_log),
                "job_dir": str(nop_job),
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
        (review_dir / "review.md").write_text(f"Final decision: {decision}\n", encoding="utf-8")
        session_ref = write_fake_claude_session(project, "task-review", "seed-a__idea-1")
        append_phase5_manifest_event(project, "seed-a", "idea-1", decision, session_ref)

    def prepare_inputs(self, project: Path, decision: str) -> Path:
        task = self.write_generated_task(project)
        self.write_phase4_status(project, task)
        self.write_review(project, decision)
        return task

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


if __name__ == "__main__":
    unittest.main()
