# TB Harbor Taskgen Developer Guide

<p align="center">
  <strong>English</strong>
  ·
  <a href="TB_HARBOR_TASKGEN_MVP_SPEC.zh-CN.md">简体中文</a>
</p>

This document describes the current implementation of `tb-harbor-taskgen` for
developers who need to run, debug, or extend the project. It is intentionally
implementation-aligned: when behavior changes in code, update this guide in the
same change.

## 1. Project Scope

`tb-harbor-taskgen` turns one Terminal-Bench Harbor seed task into one or more
generated TB3 Harbor task candidates. The workflow is phase-based:

1. Read a seed task and brainstorm task ideas.
2. Research SkillNet evidence for each idea.
3. Generate a working Harbor task.
4. Run Harbor oracle/nop checks.
5. Review the task.
6. Repair it when review asks for modification.
7. Move the task to accepted or rejected output.

The pipeline is local. Claude Code is used for phases that need language-model
work; deterministic phases run in Python and Harbor.

## 2. Repository Map

| Path | Purpose |
| --- | --- |
| `src/taskgen/cli.py` | Top-level CLI, phase registry, full pipeline orchestration. |
| `src/taskgen/phases/` | Phase-specific run and validation modules. |
| `src/taskgen/claude/` | Claude workspace preparation, execution wrapper, and cost parsing. |
| `src/taskgen/harbor/oracle_nop.py` | Harbor oracle/nop check runner and status writer. |
| `src/taskgen/maintenance/clean_intermediate.py` | Cleanup for generated run artifacts. |
| `prompts/` | Prompt templates rendered into `runs/prompts/`. |
| `cc-definitions/` | Claude Code agents and reusable project skills copied into workspaces. |
| `scripts/` | Shell entry points that source local environment and call Python modules. |
| `tests/` | Unit tests for configuration, workspace handling, validation, and phase behavior. |
| `runs/` | Runtime artifacts, logs, rendered prompts, workspaces, and manifest. |
| `generated/` | Working and finalized generated task directories. |
| `seeds/` | Input seed tasks consumed by phase1 and phase3. |

## 3. IDs and Path Rules

`seed_id` and `idea_id` must be single path-safe segments matching:

```text
[A-Za-z0-9._-]+
```

They cannot be empty, `.`, or `..`.

The stable task id used by phase4 onward is:

```text
<seed_id>__<idea_id>
```

This id is used for review directories, oracle/nop status directories, final
task directories, and manifest events.

## 4. Configuration and Entry Points

### `model.json`

`model.json` configures the Claude binary, default model, default effort, and
per-phase effort levels:

```json
{
  "claude_code_path": "cc-binary/claude-2.1.169-linux-x64",
  "default_model": "anthropic/claude-opus-4.8",
  "default_effort": "max",
  "phase_efforts": {
    "phase1": "max",
    "phase2": "medium",
    "phase3": "max",
    "phase5": "high",
    "phase6": "high"
  }
}
```

Supported efforts are `low`, `medium`, `high`, `xhigh`, and `max`.

`claude_code_path` is resolved relative to the project root when it is not
absolute. The referenced binary is local and is ignored by git. If this field is
removed, the runner uses exactly one executable `cc-binary/claude-*`, then
falls back to `claude` on `PATH`.

`phase_efforts` accepts canonical phase keys and aliases defined in
`src/taskgen/config.py`. Prefer canonical keys (`phase1` through `phase7`) for
new config.

### Shell Scripts

| Script | Behavior |
| --- | --- |
| `scripts/taskgen.sh` | Runs `python3 -m taskgen.cli`. |
| `scripts/run-claude-logged.sh` | Runs the Claude wrapper and records session metadata. |
| `scripts/run-harbor-oracle-nop.sh` | Runs Harbor oracle/nop checks. |
| `scripts/clean-intermediate.sh` | Cleans intermediate runtime artifacts. |
| `scripts/tool_init.sh` | Installs `harbor==0.13.2` and `skillnet-ai` with `uv tool install`. |

Each shell script sources `scripts/env_init.sh` when present and sets
`PYTHONPATH` to include `src/`. `scripts/env_init.sh` is local-only and ignored;
start from `scripts/env_init.example.sh`.

## 5. CLI

The console script `taskgen` maps to `taskgen.cli:main`. The shell entry point is
usually used during local development:

```bash
scripts/taskgen.sh <command> ...
```

### Inspection Commands

```bash
scripts/taskgen.sh phases
scripts/taskgen.sh paths <seed_id> [--idea-id <idea_id>] [--task-id <task_id>]
scripts/taskgen.sh command <phase> <seed_id> [--idea-id <idea_id>]
scripts/taskgen.sh next <seed_id>
```

### Single Phase Commands

```bash
scripts/taskgen.sh run <phase> <seed_id> [--idea-id <idea_id>] [--dry-run]
scripts/taskgen.sh validate <phase> <seed_id> [--idea-id <idea_id>] [--json]
```

`phase3` through `phase7` require `--idea-id`. `phase1` and `phase2` reject
`--idea-id`.

Claude-backed phases accept `--model` and `--effort`: `phase1`, `phase2`,
`phase3`, `phase5`, `phase6`.

### Full Pipeline

```bash
scripts/taskgen.sh pipeline <seed_id> \
  [--idea-id <idea_id>] \
  [--max-repairs N] \
  [--force] \
  [--continue-on-error] \
  [--dry-run] \
  [--model <model>] \
  [--effort <effort>]
```

The pipeline runs phase1 and phase2 first, then processes either the requested
idea or every idea listed in phase1 output. Existing valid phases are skipped
unless `--force` is set. When phase5 returns `needs_modification`, the pipeline
runs phase6 and then forces phase4 and phase5 again until the review decision is
`ready`, `rejected`, or the repair budget is exhausted.

## 6. Artifact Layout

Runtime artifacts are deterministic and are validated by phase modules.

```text
runs/prompts/<seed_id>/...
runs/brainstorm/<seed_id>/seed_brainstorm.json
runs/skillnet/<seed_id>/skillnet_index.json
runs/skillnet/<seed_id>/<idea_id>/skill_summary.json
runs/skillnet/<seed_id>/<idea_id>/skills/
runs/skillnet/<seed_id>/<idea_id>/raw/
runs/oracle-nop-check/<task_id>/oracle-nop-status.json
runs/oracle-nop-check/<task_id>/oracle.log
runs/oracle-nop-check/<task_id>/nop.log
runs/reviews/<task_id>/review.json
runs/reviews/<task_id>/review.md
runs/claude-sessions/<phase>/<subject>/<run_id>/
runs/workspace/<phase>/<subject>/<run_id>/
runs/task-manifest.jsonl

generated/working/<seed_id>/<idea_id>/
generated/accepted/<task_id>/
generated/rejected/<task_id>/
```

`runs/` and `generated/` contents are ignored by git except skeleton
`.gitkeep` files. `seeds/` is an input directory; decide separately whether
your seed data should be committed.

## 7. Claude Workspace Model

Claude-backed phases use `src/taskgen/claude/runner.py` and
`src/taskgen/claude/workspace.py`.

Supported Claude phases:

```text
seed-brainstorm
skillnet-research
task-generation
task-review
task-repair
```

For each run, the runner creates:

```text
runs/claude-sessions/<phase>/<subject>/<run_id>/
runs/workspace/<phase>/<subject>/<run_id>/
```

The workspace receives the rendered prompt, project agents, project skills, and
phase-specific input artifacts. Claude writes outputs under `output/...`; after
a successful Claude exit, only declared output paths are copied back to project
runtime directories.

Session metadata includes:

| File | Purpose |
| --- | --- |
| `prompt.md` | Prompt copy used for the run. |
| `claude-code.txt` | Claude stream-json output and stderr. |
| `cost.json` | Parsed cost and token summary. |
| `status.json` | Run status, workspace paths, synced outputs, and cost summary. |

Claude runs with `--verbose`, `--output-format=stream-json`,
`--permission-mode bypassPermissions`, `--print`, `CLAUDE_CONFIG_DIR` scoped to
the run directory, and `IS_SANDBOX=1`.

## 8. Phase Contracts

### Summary

| Phase | Module | Scope | Main Output |
| --- | --- | --- | --- |
| `phase1` | `phase1_seed_brainstorm` | Seed-level Claude phase. | `runs/brainstorm/<seed_id>/seed_brainstorm.json` |
| `phase2` | `phase2_skillnet_research` | Seed-level Claude phase. | `runs/skillnet/<seed_id>/` |
| `phase3` | `phase3_task_generation` | Idea-level Claude phase. | `generated/working/<seed_id>/<idea_id>/` |
| `phase4` | `phase4_oracle_nop_check` | Idea-level Harbor phase. | `runs/oracle-nop-check/<task_id>/oracle-nop-status.json` |
| `phase5` | `phase5_task_review` | Idea-level Claude phase. | `runs/reviews/<task_id>/review.json` |
| `phase6` | `phase6_task_repair` | Idea-level Claude phase. | Updated `generated/working/<seed_id>/<idea_id>/` |
| `phase7` | `phase7_finalize` | Idea-level deterministic phase. | `generated/accepted/<task_id>/` or `generated/rejected/<task_id>/` |

### Phase 1: Seed Brainstorm

Inputs:

- `seeds/<seed_id>/` with `instruction.md`, `task.toml`, `environment/`,
  `solution/`, and `tests/`.
- `prompts/seed-brainstorm.md`.
- `cc-definitions/agents/seed-brainstormer.md`.
- `cc-definitions/skills/tb-harbor-task-generation/SKILL.md`.

Output JSON must contain `seed_id`, `source_path`, `task_understanding`,
`core_capabilities`, `avoid`, and a non-empty `ideas` list. Each idea must
include `idea_id`, `title`, `scenario`, `core_transfer`, `changed_dimensions`,
`expected_artifacts`, `verifier_sketch`, `risk_notes`, `difficulty_profile`,
and `skillnet_queries`.

`difficulty_profile.minimum_independent_subskills` must be at least `2`.

Manifest event: `brainstormed`.

### Phase 2: SkillNet Research

Inputs:

- Phase1 brainstorm JSON.
- `prompts/skillnet-research.md`.
- `cc-definitions/agents/skillnet-researcher.md`.
- Base generation skill.

Outputs:

- `runs/skillnet/<seed_id>/skillnet_index.json`.
- `runs/skillnet/<seed_id>/<idea_id>/skill_summary.json`.
- `runs/skillnet/<seed_id>/<idea_id>/skills/`.
- `runs/skillnet/<seed_id>/<idea_id>/raw/`.

Statuses are `ready`, `partial`, `no_strong_match`, and `failed`.

Skill package names must be path-safe and start with
`taskgen-<idea_id>-`. `ready` requires 3-5 selected skills; `partial` requires
1-5. `skill_summary.json` must include selected skills, notes, implementation
risks, `recommended_direction`, and `difficulty_hardening` with minimum
complexity, too-easy risks, hardening recommendations, and do-not-simplify
guidance.

Manifest event: `skillnet_done`.

### Phase 3: Task Generation

Inputs:

- Seed task.
- Phase1 brainstorm JSON.
- Phase2 SkillNet index.
- Idea `skill_summary.json`.
- Idea `skills/`.
- `prompts/task-generation.md`.
- `cc-definitions/agents/tb-harbor-task-generator.md`.
- Base generation skill.

Output task layout:

```text
generated/working/<seed_id>/<idea_id>/
├── instruction.md
├── task.toml
├── environment/Dockerfile
├── solution/solve.sh
├── tests/Dockerfile
└── tests/test.sh
```

Validation checks required layout, non-empty required directories, matching
phase1/phase2 inputs, and absence of runner artifacts. Generated tasks must not
contain workspace input directories, Claude run files, `.pyc`, `.log`, symlinks,
or local runner paths such as `runs/workspace`, `runs/claude-sessions`, and
`/shared/users/`.

Manifest event: `generated`.

### Phase 4: Harbor Oracle / Nop Check

Phase4 validates the phase3 working task, then runs:

```text
harbor run -p <task_path> -a oracle -o <jobs_dir> --job-name oracle -k 1 -y
harbor run -p <task_path> -a nop    -o <jobs_dir> --job-name nop    -k 1 -y
```

Harbor is resolved from `HARBOR_BIN`, then from `harbor` on `PATH`.

Status is written to:

```text
runs/oracle-nop-check/<task_id>/oracle-nop-status.json
```

The formal pass condition is:

- oracle exit code is `0` and reward is `1.0`.
- nop exit code is `0` and reward is `0.0`.

The phase runner records status even when rewards fail. Pipeline review can use
a failed but well-formed status to produce repair instructions.

Manifest event: `checked`.

### Phase 5: Task Review

Inputs:

- Working task.
- Reviewable phase4 oracle/nop status.
- `prompts/task-review.md`.

Outputs:

- `runs/reviews/<task_id>/review.json`.
- `runs/reviews/<task_id>/review.md`.

`review.json` must contain exactly:

```text
task_id
decision
summary
modification_items
blocking_reasons
```

Allowed decisions are `ready`, `needs_modification`, and `rejected`.

- `ready`: no modification items or blocking reasons.
- `needs_modification`: non-empty `modification_items`, no blocking reasons.
- `rejected`: non-empty `blocking_reasons`, no modification items.

Manifest event: `reviewed`.

### Phase 6: Task Repair

Phase6 can run only when phase5 validates and the latest review decision is
`needs_modification`.

Inputs:

- Working task.
- Review directory.
- Optional oracle/nop directory copied into the Claude workspace.
- `prompts/task-repair.md`.

Claude must sync `output/task` back to
`generated/working/<seed_id>/<idea_id>`. Validation then reuses phase3 task
validation and checks that the new Claude session synced the repaired task.

Manifest event: `repaired`.

### Phase 7: Finalize / Organize

Phase7 requires:

- phase5 validation passes.
- phase4 formal pass condition is true.
- review decision is `ready` or `rejected`.

For `ready`, the working task is copied to:

```text
generated/accepted/<task_id>/
```

For `rejected`, the working task is copied to:

```text
generated/rejected/<task_id>/
```

The counterpart final directory is removed, and the working task directory is
removed after finalization. Validation checks required task files in the final
directory and ensures the working directory no longer exists.

Manifest event: `accepted` or `rejected`.

## 9. Manifest

`runs/task-manifest.jsonl` is append-only. Each phase appends one event after a
successful run and then validation checks that a matching event exists.

| Event | Written By | Required References |
| --- | --- | --- |
| `brainstormed` | phase1 | `brainstorm_ref`, `claude_session_ref` |
| `skillnet_done` | phase2 | `brainstorm_ref`, `skillnet_ref`, `claude_session_ref` |
| `generated` | phase3 | `task_path`, `brainstorm_ref`, `skillnet_ref`, `skill_summary_ref`, `claude_session_ref` |
| `checked` | phase4 | `task_path`, `oracle_nop_ref`, `passed` |
| `reviewed` | phase5 | `review_ref`, `review_markdown_ref`, `oracle_nop_ref`, `decision`, `claude_session_ref` |
| `repaired` | phase6 | `task_path`, `review_ref`, `oracle_nop_ref`, `claude_session_ref` |
| `accepted` | phase7 | `task_path`, `source_task_ref`, `review_ref`, `oracle_nop_ref` |
| `rejected` | phase7 | `task_path`, `source_task_ref`, `review_ref`, `oracle_nop_ref` |

Manifest validation is strict enough to prove phase lineage, but it does not
deduplicate older events. Validators look for at least one matching valid event.

## 10. Cleanup and Git Hygiene

Intermediate cleanup:

```bash
scripts/clean-intermediate.sh
scripts/clean-intermediate.sh --apply
```

Without `--apply`, the command lists targets only. With `--apply`, it removes:

- `runs/prompts`
- `runs/brainstorm`
- `runs/skillnet`
- `runs/oracle-nop-check`
- `runs/reviews`
- `runs/workspace`
- `runs/claude-sessions`
- `runs/task-manifest.jsonl`
- Python `__pycache__` under `src/`, `scripts/`, and `tests/`

It then restores the `runs/` skeleton directories with `.gitkeep` files.

Current ignore rules keep local credentials, runtime artifacts, generated task
outputs, Python caches, and the local Claude binary out of git. `model.json`
still points to the expected local Claude binary path.

## 11. Development Checks

Run these after changing code or docs that describe behavior:

```bash
python3 -B -m compileall -q src tests
python3 -B -m unittest discover -s tests -v
bash -n scripts/*.sh
```

Use validation commands for behavior-specific checks:

```bash
scripts/taskgen.sh validate phase1 <seed_id> --json
scripts/taskgen.sh validate phase3 <seed_id> --idea-id <idea_id> --json
scripts/taskgen.sh validate phase7 <seed_id> --idea-id <idea_id> --json
```

Before documenting new behavior, verify the source module that owns it:

- CLI and pipeline: `src/taskgen/cli.py`.
- Phase run and validation behavior: `src/taskgen/phases/`.
- Claude workspace behavior: `src/taskgen/claude/`.
- Harbor check behavior: `src/taskgen/harbor/oracle_nop.py`.
- Cleanup behavior: `src/taskgen/maintenance/clean_intermediate.py`.
