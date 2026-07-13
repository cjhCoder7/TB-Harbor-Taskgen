<h1 align="center">TB-Harbor-Taskgen Developer Guide</h1>

<p align="center">
  <strong>English</strong>
  ·
  <a href="TB_HARBOR_TASKGEN_MVP_SPEC.zh-CN.md">简体中文</a>
</p>

This document describes the current implementation of TB-Harbor-Taskgen for
developers who need to run, debug, or extend the project. It is intentionally
implementation-aligned: when behavior changes in code, update this guide in the
same change.

## 1. Project Scope

TB-Harbor-Taskgen turns one Terminal-Bench Harbor seed task into one or more
generated TB3 Harbor task candidates. The workflow is phase-based:

1. Read a seed task and brainstorm task ideas.
2. Research SkillNet evidence for each idea.
3. Generate a working Harbor task.
4. Run Harbor oracle/nop checks.
5. Review the task.
6. Repair it when review asks for modification.
7. Move the task to accepted or rejected output.

The pipeline is local. Claude Code is the agent for phases that need
language-model work. By default it calls the configured Anthropic-compatible
backend; `--openai` instead routes it through a temporary LiteLLM gateway to an
OpenAI-compatible backend. Deterministic phases run in Python and Harbor.

## 2. Repository Map

| Path | Purpose |
| --- | --- |
| `src/taskgen/cli.py` | Top-level CLI, phase registry, full pipeline orchestration. |
| `src/taskgen/phases/` | Phase-specific run and validation modules. |
| `src/taskgen/claude/` | Claude workspace preparation, execution wrapper, and cost parsing. |
| `src/taskgen/openai_gateway.py` | Temporary LiteLLM lifecycle and OpenAI-compatible protocol bridge. |
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

They cannot be empty, `.`, `..`, or contain the reserved `__` separator.
`seed_id` is limited to 128 characters and `idea_id` to 120 characters, keeping
the combined task id below the filesystem's single-component limit.

The stable task id used by phase4 onward is:

```text
<seed_id>__<idea_id>
```

This id is used for review directories, oracle/nop status directories, final
task directories, and manifest events.

## 4. Configuration and Entry Points

### `model.json`

`model.json` configures the Claude binary, Claude and Harbor supervisor
timeouts, the default Claude-compatible model settings, and the optional
OpenAI-compatible model settings:

```json
{
  "claude_code_path": "cc-binary/claude-2.1.169-linux-x64",
  "claude_code_timeout_sec": 1800,
  "claude_code_phase_timeouts_sec": {
    "phase3": 10800,
    "phase6": 10800
  },
  "harbor_check_timeout_sec": 10800,
  "default_model": "claude-opus-4-8",
  "default_effort": "max",
  "phase_efforts": {
    "phase1": "max",
    "phase2": "medium",
    "phase3": "max",
    "phase5": "high",
    "phase6": "high"
  },
  "openai": {
    "openai_default_model": "provider-model-name",
    "openai_default_effort": "xhigh",
    "openai_phase_efforts": {}
  }
}
```

Supported efforts are `low`, `medium`, `high`, `xhigh`, and `max`.

`claude_code_path` is resolved relative to the project root when it is not
absolute. The referenced binary is local and is ignored by git. If this field is
removed, the runner uses exactly one executable `cc-binary/claude-*`, then
falls back to `claude` on `PATH`.

`claude_code_timeout_sec` sets the timeout for each Claude Code run in seconds
and must be positive. The default value `1800` is 30 minutes. When a run reaches
this limit, the runner terminates its isolated process group and records exit
code `124` with `timed_out: true`. Process-group cleanup is a POSIX/Linux
runtime behavior.

`claude_code_phase_timeouts_sec` is an optional per-phase override map. Keys
accept the same canonical names and aliases as `phase_efforts`; values must be
positive finite numbers. A matching override takes precedence over
`claude_code_timeout_sec`. This configuration gives phase3 and phase6 `10800`
seconds while retaining the 30-minute fallback for all other phases.

`harbor_check_timeout_sec` is the positive, finite supervisor timeout for each
oracle or nop Harbor invocation. The default is `10800` seconds. A timed-out
check records exit code `124` and timeout metadata in the reviewable phase4
status. Unknown top-level `model.json` fields are rejected so configuration
typos cannot silently fall back to defaults.

`phase_efforts` accepts canonical phase keys and aliases defined in
`src/taskgen/config.py`. Prefer canonical keys (`phase1` through `phase7`) for
new config.

The optional `openai` object applies only with `--openai`. An explicit `--model`
takes precedence over `openai_default_model`, and the selected name is used
unchanged. Effort resolves from explicit `--effort`, then
`openai_phase_efforts`, then `openai_default_effort`; it never falls back to the
Claude settings. Missing required values and unknown keys fail before a
model-backed phase starts.

### Shell Scripts

| Script | Behavior |
| --- | --- |
| `scripts/taskgen.sh` | Selects the local environment and runs `python3 -m taskgen.cli`. |
| `scripts/run-claude-logged.sh` | Runs the Claude wrapper and records session metadata. |
| `scripts/run-harbor-oracle-nop.sh` | Runs Harbor oracle/nop checks. |
| `scripts/clean-intermediate.sh` | Cleans intermediate runtime artifacts. |
| `scripts/tool_init.sh` | Installs `harbor==0.13.2`, `skillnet-ai==0.0.18`, and `litellm[proxy]==1.91.1` with `uv tool install`. |

When present, `scripts/taskgen.sh` sources `scripts/env_init.sh` by default or
`scripts/env_openai_init.sh` with `--openai`. Both files are local-only and
ignored by git; create them from the corresponding `.example.sh` file. Nested
wrappers preserve an active gateway environment, and all wrappers add `src/` to
`PYTHONPATH`.

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
scripts/taskgen.sh run <phase> <seed_id> [--idea-id <idea_id>] [--dry-run] \
  [--model <model>] [--effort <effort>] [--openai]
scripts/taskgen.sh validate <phase> <seed_id> [--idea-id <idea_id>] [--json]
```

`phase3` through `phase7` require `--idea-id`. `phase1` and `phase2` reject
`--idea-id`. `phase1` run commands also accept `--idea-count N` to request and
validate an exact brainstorm idea count.

Claude-backed phases accept `--model` and `--effort`: `phase1`, `phase2`,
`phase3`, `phase5`, `phase6`. Those same phases accept `--openai`; deterministic
phases reject it. A dry run resolves and prints the OpenAI-compatible model and
effort but does not start LiteLLM.

### Full Pipeline

```bash
scripts/taskgen.sh pipeline <seed_id> \
  [--idea-id <idea_id>] \
  [--idea-count N] \
  [--max-repairs N] \
  [--force] \
  [--continue-on-error] \
  [--dry-run] \
  [--model <model>] \
  [--effort <effort>] \
  [--openai]
```

The pipeline runs phase1 and phase2 first, then processes either the requested
idea or every idea listed in phase1 output. `--idea-count` requests and validates
an exact phase1 brainstorm idea count; when existing phase1 output has a
different count, phase1 is rerun. Existing valid phases are otherwise skipped
unless `--force` is set. When phase5 returns `needs_modification`, the pipeline
runs phase6 and then forces phase4 and phase5 again until the review decision is
`ready`, `rejected`, or the repair budget is exhausted. A dry run returns
non-zero when phase1 would need to run and no explicit `--idea-id` is available,
because the remaining per-idea plan cannot yet be determined.

### OpenAI-Compatible Claude Code Backend

`--openai` does not replace Claude Code as the agent. It changes only the model
transport used by Claude Code:

```text
Claude Code -> Anthropic /v1/messages -> temporary LiteLLM
            -> upstream OpenAI-compatible /v1/responses
```

Set `OPENAI_BASE_URL` and `OPENAI_API_KEY` in
`scripts/env_openai_init.sh`. The upstream must support the
`POST /v1/responses` route shown above.

`run --openai` starts one loopback gateway for the phase. `pipeline --openai`
and `run-all --openai` share one gateway across all Claude-backed phases and
repair rounds. Phases start after local gateway readiness checks; upstream
connectivity is exercised by the first model request. The gateway is stopped
after completion, failure, or interruption.

Upstream credentials are available to LiteLLM but not to Claude Code or its
tools. The gateway uses LiteLLM's standard Anthropic Messages translation.
It does not disable thinking; the selected effort passes through Claude Code
and may be normalized by LiteLLM to match model support. Skills, subagents, and
Bash remain Claude Code features; full operation requires upstream streaming
and tool calling support.

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
a successful Claude exit, every declared output must exist. Outputs are staged
next to their destinations and atomically switched into project runtime
directories so a failed copy does not destroy the previous artifact. A small
transaction journal under `runs/output-sync-transactions/` lets the next run
restore or complete an interrupted rename sequence after an abrupt process exit.
Mutating phase, runner, and standalone workspace commands serialize operations
for the same subject and hold the shared activity guard used by cleanup. A phase
passes its actual locked subject/activity descriptors only to its direct runner
child, so the nested invocation remains serialized without deadlocking and the
locks survive an abrupt parent exit until the runner itself finishes.

Session metadata includes:

| File | Purpose |
| --- | --- |
| `prompt.md` | Prompt copy used for the run. |
| `claude-code.txt` | Claude stream-json output and stderr. |
| `cost.json` | Parsed cost and token summary. |
| `status.json` | Run status, timeout metadata, workspace paths, synced outputs, and cost summary. |

Stream logs are parsed incrementally. Optional OpenRouter generation metadata
enrichment is bounded by a 30-second total deadline, 100 generation IDs, and a
10-second request timeout by default. These bounds can be adjusted with
`TASKGEN_OPENROUTER_DEADLINE_SECONDS`, `TASKGEN_OPENROUTER_MAX_GENERATIONS`, and
`TASKGEN_OPENROUTER_QUERY_TIMEOUT_SECONDS`. Provider cost replaces the stream
cost only when every generation has a finite, non-negative cost.

In OpenAI-compatible mode, token usage is still recorded, but dollar totals are
Claude Code estimates rather than provider billing.

Claude runs with `--verbose`, `--output-format=stream-json`,
`--permission-mode bypassPermissions`, `--print`, `CLAUDE_CONFIG_DIR` scoped to
the run directory, and `IS_SANDBOX=1`. On POSIX/Linux, the runner starts Claude
in an isolated process group so a configured timeout, interrupt, or runner-side
failure also stops its tool subprocesses.

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

The generation prompt's best-effort early oracle and nop runs resolve Harbor
from `HARBOR_BIN` (falling back to `harbor`) and cap each invocation at 900
seconds. A timeout does not replace the authoritative phase4 validation.

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

The phase runner records status even when rewards fail or a Harbor supervisor
timeout occurs. Status and manifest data include the checked task-tree digest
and run id, so editing the task invalidates stale check results. Pipeline review
can use a failed but well-formed status to produce repair instructions.

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

`review.md` must state the same decision as `review.json` and include non-empty
Summary, Modification Items, and Blocking Reasons sections.

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

As in phase3, each best-effort early Harbor check in the repair prompt is
capped at 900 seconds and phase4 remains authoritative.

Manifest event: `repaired`.

### Phase 7: Finalize / Organize

Phase7 requires:

- phase5 validation passes.
- review decision is `ready` or `rejected`.
- for `ready`, phase4 formal pass condition is true.
- for `rejected`, phase4 status is well formed and reviewable, but it may have
  failed the formal pass condition.

For `ready`, the working task is copied to:

```text
generated/accepted/<task_id>/
```

For `rejected`, the working task is copied to:

```text
generated/rejected/<task_id>/
```

The final task is first copied and validated in a sibling staging directory,
then atomically switched into place. The previous destination can be restored
if switching or validation fails. Once the validated switch commits, phase7
appends the manifest as a separate irreversible commit point. If that append is
interrupted, the valid final destination is retained and rerunning phase7 adds
the missing event. `runs/finalization-transactions/` also lets reruns finish
backup cleanup or roll back an incomplete switch.

When a finalization journal is pending, phase7 `--dry-run` fully validates the
journal and reports whether a real run would commit or roll back. It does not
rename or remove paths, delete the journal, fsync directories, or append a
manifest event.

Manifest event: `accepted` or `rejected`.

## 9. Manifest

`runs/task-manifest.jsonl` is append-only. Each phase appends one event after a
successful run and then validation checks that a matching event exists.

| Event | Written By | Required References |
| --- | --- | --- |
| `brainstormed` | phase1 | `brainstorm_ref`, `claude_session_ref` |
| `skillnet_done` | phase2 | `brainstorm_ref`, `skillnet_ref`, `claude_session_ref` |
| `generated` | phase3 | `task_path`, `brainstorm_ref`, `skillnet_ref`, `skill_summary_ref`, `claude_session_ref` |
| `checked` | phase4 | `task_path`, `oracle_nop_ref`, `passed`, `run_id`, `task_tree_sha256` |
| `reviewed` | phase5 | `review_ref`, `review_markdown_ref`, `oracle_nop_ref`, `decision`, `claude_session_ref`, `phase4_run_id`, `task_tree_sha256` |
| `repaired` | phase6 | `task_path`, `review_ref`, `oracle_nop_ref`, `claude_session_ref` |
| `accepted` | phase7 | `task_path`, `source_task_ref`, `review_ref`, `oracle_nop_ref`, `run_id`, `task_tree_sha256` |
| `rejected` | phase7 | `task_path`, `source_task_ref`, `review_ref`, `oracle_nop_ref`, `run_id`, `task_tree_sha256` |

Manifest validation is strict enough to prove phase lineage, but it does not
deduplicate older events. Validators look for at least one matching valid event.

## 10. Cleanup and Git Hygiene

Intermediate cleanup:

```bash
scripts/clean-intermediate.sh
scripts/clean-intermediate.sh --apply
scripts/clean-intermediate.sh --apply --drop-manifest
scripts/clean-intermediate.sh --apply --discard-transactions
```

Without `--apply`, the command lists targets only. With `--apply`, it refuses
to run while an active pipeline/phase holds the activity lock or a Claude
session marker remains, then removes:

- `runs/prompts`
- `runs/brainstorm`
- `runs/skillnet`
- `runs/oracle-nop-check`
- `runs/reviews`
- `runs/workspace`
- `runs/output-sync-transactions`
- `runs/finalization-transactions`
- `runs/claude-sessions`
- Python `__pycache__` under `src/`, `scripts/`, and `tests/`

It then restores the `runs/` skeleton directories with `.gitkeep` files.
The append-only `runs/task-manifest.jsonl` is preserved unless
`--drop-manifest` is explicitly supplied. `--force-active` bypasses the active
run guard for manual recovery and can disrupt a live run. Pending output-sync
or finalization journals block cleanup so crash recovery remains possible;
`--discard-transactions` is the explicit destructive override.

Before either a dry-run listing or deletion, cleanup requires the project root
and the `runs/`, `src/`, `scripts/`, and `tests/` containers (when present) to
be real directories. It also rejects targets reached through a symlink or
non-directory ancestor and does not follow directory symlinks while locating
Python caches. A cleanup target that is itself a symlink is only unlinked; its
external target is left untouched.

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
