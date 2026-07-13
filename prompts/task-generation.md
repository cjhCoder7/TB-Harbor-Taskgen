# Task Generation

Use the `tb-harbor-task-generator` subagent in the current workspace and the `tb-harbor-task-generation` skill. When invoking `Agent`, omit the `isolation` field. Do not create or enter a Git worktree.

You are already in an isolated workspace. Work only inside the current directory.

Inputs:

- Seed id: `{{SEED_ID}}`
- Idea id: `{{IDEA_ID}}`
- Brainstorm: `{{BRAINSTORM_PATH}}`
- Skill summary: `{{SKILL_SUMMARY_PATH}}`
- Seed background: `{{SEED_PATH}}`
- Generated task output: `{{OUTPUT_PATH}}`
- Optional validation output: `output/local-validation/`

Goal:

Generate one complete TB3 Harbor task for exactly idea `{{IDEA_ID}}` under `{{OUTPUT_PATH}}/`.

Design priority:

1. First choose a task concept that is as hard as possible within realistic, fair, verifier-checkable, and oracle-solvable bounds.
2. Difficulty should come from task logic complexity and meaningful implementation or artifact edits, not from packaging, verbose instructions, formatting traps, randomness, blocked networking, low timeouts, or hidden gotchas.
3. Use the brainstorm `difficulty_profile` and SkillNet `difficulty_hardening` as design guidance. Do not simplify the task just to make TB3 packaging easier.
4. For code-generation or code-editing tasks, prefer concepts requiring coordination across multiple functions, types, modules, edge cases, or invariants. Avoid tasks where a one-line constant, thin wrapper, or direct hardcoded lookup is likely enough. Code change size is only a signal for real logic complexity; do not pad line count.
5. For non-code tasks, use the equivalent artifact-edit scope: multi-file investigation, parameter derivation, validation, transformation, and normalization. Avoid concepts where one obvious command or one short loop completes the entire job.
6. After the hard task concept is chosen, encode it as a valid TB3 Harbor task using the checklist below.

Required work:

1. Read the brainstorm and use exactly idea `{{IDEA_ID}}`.
2. Read the skill summary before designing files.
3. Use loaded curated skills when useful.
4. Inspect the seed only as read-only background. Many seeds are TB2 or older Harbor tasks; use them for capability boundaries, not as task-format templates.
5. Preserve only abstract capability from the seed. Do not copy seed wording, assets, hidden answers, expected outputs, verifier ground truth, or old `task.toml` structure.
6. Generate one complete TB3 task under `{{OUTPUT_PATH}}/` without weakening the intended difficulty.
7. In `task.toml` `difficulty_explanation`, explicitly describe the main logic complexity and the expected implementation/edit scope that make the task sufficiently difficult.
8. Check directory layout, `task.toml`, path consistency, instruction quality, reward writing, verifier separation, and absence of copied input artifacts.
9. If Harbor is available, run one early workspace-local oracle check and one early nop check, capped at 900 seconds each. Fix clear generation bugs before finishing when practical.

Required task tree:

```text
{{OUTPUT_PATH}}/
  instruction.md
  task.toml
  environment/
    Dockerfile
  solution/
    solve.sh
  tests/
    Dockerfile
    test.sh
```

Boundaries:

- Generated task files go only under `{{OUTPUT_PATH}}/`.
- Optional validation logs and Harbor jobs go only under `output/local-validation/`.
- Apart from the allowed output roots above, do not write other workspace paths.
- Do not copy `.claude/`, `seed/`, `brainstorm/`, `skillnet/`, `raw/`, prompt files, or validation files into `{{OUTPUT_PATH}}/`.
- Only perform the required task generation work and optional local validation described here.
- Do not remove or prune host-global Docker images, containers, volumes, networks, or build cache. Host resource cleanup belongs to the pipeline operator, not the generation agent.

TB3 format checklist:

- `task.toml`: top-level `artifacts = [...]` before any table; every artifact is container-absolute and mentioned in `instruction.md`.
- `task.toml`: `[metadata]` contains `author_name`, `author_email`, `author_organization`, `difficulty_explanation`, `solution_explanation`, `verification_explanation`, `category`, `tags`, `expert_time_estimate_hours`, and `relevant_experience`.
- `task.toml`: leave synthetic author identity and relevant experience fields empty unless truthful values are explicitly provided; choose non-empty `category` and `tags` from the task content.
- `task.toml`: `[verifier] environment_mode = "separate"` with positive `timeout_sec`; `[agent] timeout_sec` is integer-valued; `[environment]` has `build_timeout_sec`, `cpus`, `memory_mb`, `storage_mb`, `gpus`, and `allow_internet = true`.
- `instruction.md`: concise, human-edited, absolute paths only, mentions every artifact, states final observable outcome, and does not describe solution steps, command order, role prompts, thinking prompts, or tool nudges.
- `instruction.md`: avoid polished long-form Markdown structure. Do not write a lengthy task description with formal `#` or `##` sectioning; that style looks purely LLM-synthesized and insufficiently human-checked.
- `instruction.md`: write prose as natural paragraphs. Do not hard-wrap a paragraph at 80 or 90 columns; each paragraph should stay on one physical line, with blank lines between paragraphs and bullets only when genuinely useful.
- `instruction.md`: ends with exactly one blank line plus the required TB3 suffix, where `N` equals `[agent].timeout_sec`.
- `environment/`: only the agent-visible starting state; never copies `solution/` or `tests/`.
- `environment/`: visible files and visible text must not contain explanatory comments, docstrings, inline hints, known-defect notes, TODO/FIXME markers, descriptions of what the bug is, instructions for how to fix it, or prose that helps the agent infer the intended fix.
- `environment/`: do not include problem-specific test examples, sample corpora, self-checks, expected outputs, or other fixtures that demonstrate the task's target behavior or make the intended solution inferable.
- `solution/solve.sh`: real oracle solution from the same visible starting state; every external command exists in the agent image or is installed before use.
- `tests/`: separate verifier image; verifier-only files and dependencies stay in `tests/Dockerfile`; `tests/test.sh` checks outcomes and writes reward. Tests must not depend on command order, exact library choice, oracle-only knowledge, or copied seed ground truth.

Default `task.toml` generation profile:

```toml
[verifier]
timeout_sec = 600.0
environment_mode = "separate"

[agent]
timeout_sec = 7200.0

[environment]
build_timeout_sec = 900.0
cpus = 2
memory_mb = 4096
storage_mb = 10240
gpus = 0
allow_internet = true
```

Use this profile unless the task has a concrete reason to differ. Reduce only for genuinely lightweight tasks; increase for heavy builds, slow verifiers, larger datasets, services, or GPU workloads. Keep the `instruction.md` suffix `N` exactly equal to `[agent].timeout_sec`.

Required TB3 suffix:

```text
You have N seconds to complete this task. Do not cheat by using online solutions or hints specific to this task.
```

`N` is the integer value of `[agent].timeout_sec`.

Early Harbor validation:

```bash
mkdir -p output/local-validation/harbor-jobs
HARBOR_COMMAND="${HARBOR_BIN:-harbor}"
timeout -k 30s 870s "$HARBOR_COMMAND" run -p output/task -a oracle -o output/local-validation/harbor-jobs --job-name oracle -k 1 -y > output/local-validation/oracle.log 2>&1
timeout -k 30s 870s "$HARBOR_COMMAND" run -p output/task -a nop -o output/local-validation/harbor-jobs --job-name nop -k 1 -y > output/local-validation/nop.log 2>&1
```

Each early Harbor invocation has a 900-second wall-clock budget: `timeout` sends `TERM` at 870 seconds and allows a 30-second grace period before `KILL`. Exit code `124`, or `137` when the hard kill is needed, indicates a timeout. If Harbor, Docker, or `timeout` is unavailable, or a check times out, record the failed command and error under `output/local-validation/` and continue with static checks. If you inspect rewards, read only under `output/local-validation/harbor-jobs/`; common locations are `**/result.json`, `**/verifier/reward.txt`, and `**/verifier/reward.json`. Expected signal: oracle reward `1`, nop reward `0`. The pipeline's phase4 check remains the authoritative validation.

Final consistency checks:

- Confirm every command used by `solution/solve.sh` exists in the agent image or is installed by `solve.sh` before first use.
- Confirm the workspace `output/` tree matches the required layout: generated task files live only under `{{OUTPUT_PATH}}/`, optional validation logs and Harbor jobs live only under `output/local-validation/`, and no nested `output/`, validation logs, Harbor jobs, prompts, temporary workspace files, cache directories, `.pyc` files, or transient `.log` files are present inside `{{OUTPUT_PATH}}/`.
- Confirm `environment/Dockerfile`, `solution/solve.sh`, `instruction.md`, `task.toml` metadata, and verifier expectations describe the same toolchain and task behavior. If you change implementation tooling during validation, update metadata and explanations before finishing.
- Leave any Docker resources created by Harbor validation in place for the pipeline operator to clean up safely after the run.
