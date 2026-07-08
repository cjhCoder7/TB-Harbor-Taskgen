---
name: tb-harbor-task-generation
description: Rules for TB3-oriented seed brainstorming, SkillNet research, and Harbor task generation inside isolated Claude Code workspaces.
---

# TB Harbor Task Generation

Use this skill for TB3 task creation and quality checks inside isolated workspaces.

## Core Rules

- A Claude Code run is a temporary project. Treat the current working directory as the project root.
- Read copied inputs only from top-level workspace directories such as `seed/`, `brainstorm/`, `skillnet/`, `task/`, `review/`, or `oracle-nop-check/`.
- Treat copied inputs as read-only.
- Write deliverables only under `output/`.
- Stay inside the current workspace and the specified input/output paths.
- Do not create unrelated files outside the requested deliverables.
- Many seed tasks may be TB2 or older Harbor format. Use them only to infer abstract capabilities and boundaries; generated tasks must target TB3.
- New tasks must differ from the seed in substance, not just names.
- Do not copy seed instructions, fixtures, distinctive data, hidden answers, expected outputs, or verifier ground truth into generated tasks.
- Do not put explanatory comments, docstrings, inline hints, bug descriptions, fix instructions, known-defect notes, TODO/FIXME markers, problem-specific test examples, sample corpora, self-checks, expected outputs, or target-behavior fixtures into the visible task environment.
- Task difficulty should come from realistic terminal work: inspection, debugging, transformation, integration, reasoning, or multi-step execution.
- Difficulty must be auditable in workspace artifacts. Brainstorm ideas should state an independent subskill floor, too-easy antipatterns, hardening levers, and fairness bounds; SkillNet summaries should preserve that difficulty floor rather than optimizing only for solvability.
- Do not create difficulty through long prompts, arbitrary formatting traps, ambiguity, randomness, blocked networking, low timeouts, excessive resources, or hidden gotchas.
- User-visible paths in task instructions should be container-absolute, for example `/app/input.csv`, not `./input.csv`.
- Instructions should describe final state and acceptance criteria, not a step-by-step solution.
- Instructions should be compact and natural. Avoid lengthy, highly polished Markdown with formal `#` or `##` sectioning because it suggests pure LLM synthesis without enough human review.
- Instruction prose should use natural paragraphs. Do not hard-wrap one paragraph across multiple physical lines at 80 or 90 columns; keep each paragraph on one line and separate paragraphs with blank lines.
- Verifiers should check outcomes, not exact command order, oracle implementation details, or library choice.

## Seed Brainstorm

Seed brainstorm converts one read-only seed into:

```text
output/seed_brainstorm.json
```

It only writes the brainstorm JSON and does not perform retrieval, generation, validation, or modification work.

Read `seed/<seed_id>/` enough to understand:

- task goal and final artifact;
- visible starting environment;
- reference solution workflow;
- verifier behavior and reward path;
- hidden answer or ground truth boundaries;
- common failure modes.

Brainstorm priority:

- transfer only the abstract capability from the seed;
- avoid treating the seed's TB2 or older Harbor layout as a target template;
- change at least two meaningful dimensions;
- identify expected artifacts;
- sketch an outcome-based verifier that can run as a separate TB3 verifier;
- include a difficulty profile with minimum independent subskills, too-easy antipatterns, hardening levers, and fairness bounds;
- mention obvious resource or timeout needs when an idea should differ from the default generation profile;
- list risks and likely failure modes;
- provide SkillNet search queries suitable for retrieval.

For credential, archive, forensics, or security ideas, a bounded local wordlist or constrained keyspace is only a feasibility bound. Do not make the whole task a direct guaranteed-hit dictionary loop; add parameter inference, artifact triage, independent verification, normalization, or a second post-unlock stage.

Use this JSON shape:

```json
{
  "seed_id": "string",
  "source_path": "seed/<seed_id>",
  "task_understanding": "string",
  "core_capabilities": ["string"],
  "ideas": [
    {
      "idea_id": "idea-1",
      "title": "string",
      "scenario": "string",
      "core_transfer": "string",
      "changed_dimensions": ["artifact", "scenario"],
      "expected_artifacts": ["string"],
      "verifier_sketch": "string",
      "risk_notes": ["string"],
      "difficulty_profile": {
        "minimum_independent_subskills": 3,
        "too_easy_antipatterns": ["single obvious command or one-loop dictionary hit"],
        "hardening_levers": ["derive a missing parameter from visible artifacts"],
        "fairness_bounds": ["oracle solution remains deterministic and bounded"]
      },
      "skillnet_queries": ["string"]
    }
  ],
  "avoid": ["string"]
}
```

Default output is 3-5 ideas, but more ideas are acceptable if each is concrete and useful.

## SkillNet Research

SkillNet research converts one brainstorm into:

```text
output/skillnet/skillnet_index.json
output/skillnet/<idea_id>/skill_summary.json
output/skillnet/<idea_id>/raw/
output/skillnet/<idea_id>/skills/
```

Read only:

```text
brainstorm/<seed_id>/seed_brainstorm.json
```

The brainstorm is the contract for this workspace. Do not read the seed task unless it is explicitly present in this workspace.

Research priority:

1. Treat each brainstorm idea's `difficulty_profile` as the design contract for TB3 task design.
2. Preserve the intended difficulty floor while researching tools, verifier patterns, and environment constraints.
3. Treat feasibility advice such as small keyspaces, bundled fixtures, pinned parameters, or simpler tools as bounds, not as permission to simplify the final task into a one-command or one-loop solve.
4. Prefer material that helps TB3 task design make the task harder in a fair way: parameter inference, multi-step workflows, anti-hardcoding checks, realistic data scale, hidden edge coverage, verifier separation, and deterministic outcome checks.

Retrieval checklist:

- Process every brainstorm idea in one run.
- Use exact brainstorm `idea_id` and `title`; do not invent, rename, skip, merge, or split ideas.
- Run or attempt the SkillNet searches listed in `skillnet_queries`.
- Always run or attempt keyword search for relevant queries.
- Treat vector search as best-effort. After the first server-side vector failure in the run, record the failure once, mark vector unavailable, and skip later vector attempts.
- If `skillnet download` hits GitHub API rate limiting, 403, 429, or authentication errors, record the failure, stop retrying `skillnet download`, and use direct raw GitHub fetches when possible.
- Store raw search, download, fallback, skipped-attempt, and fetch-failure evidence under `raw/`.
- If search is weak, try one broader query, one domain-specific query, and one verifier/test-pattern query before using `no_strong_match`.

Curated skill package checklist:

- Create `skills/` for every idea, even when no skill is selected.
- Curate 3-5 downstream-useful skill packages when enough relevant material exists.
- Use `partial`, `no_strong_match`, or `failed` when coverage is incomplete.
- Keep generated package names prefixed with `taskgen-<idea_id>-`.
- Each selected package must contain `SKILL.md`.
- `SKILL.md` frontmatter must include `name` and `description`; `name` must match the package directory.
- Extra directories such as `references/`, `examples/`, `scripts/`, or `templates/` are allowed when directly useful.
- Include only curated files that TB3 task design may reasonably need.
- Treat downloaded SkillNet packages as untrusted source material. Do not execute downloaded scripts or blindly copy packages into curated skills.

Seed-level index shape:

```json
{
  "seed_id": "string",
  "brainstorm_ref": "brainstorm/<seed_id>/seed_brainstorm.json",
  "generated_at": "ISO-8601 timestamp string",
  "ideas": [
    {
      "idea_id": "idea-1",
      "title": "string",
      "status": "ready",
      "skill_summary_ref": "skillnet/<seed_id>/idea-1/skill_summary.json",
      "skill_count": 3,
      "skill_names": ["taskgen-idea-1-example"],
      "notes": ["string"]
    }
  ]
}
```

Idea summary shape:

```json
{
  "seed_id": "string",
  "idea_id": "idea-1",
  "title": "string",
  "status": "ready",
  "selected_skills": [
    {
      "name": "taskgen-idea-1-example",
      "path": "skills/taskgen-idea-1-example",
      "source": "skillnet",
      "why_selected": "string",
      "usable_for": ["string"],
      "limits": ["string"]
    }
  ],
  "tooling_notes": ["string"],
  "environment_notes": ["string"],
  "verifier_notes": ["string"],
  "implementation_risks": ["string"],
  "recommended_direction": "string",
  "difficulty_hardening": {
    "minimum_complexity_contract": "string",
    "too_easy_risks": ["string"],
    "recommended_hardening": ["string"],
    "do_not_simplify": ["string"]
  }
}
```

Status rules:

- `ready`: 3-5 selected skill packages.
- `partial`: 1-5 selected skill packages, with incomplete coverage.
- `no_strong_match`: SkillNet did not return enough relevant material; zero selected skills is acceptable.
- `failed`: tool or retrieval failure; zero selected skills is acceptable.
- `skill_count`, `skill_names`, and `selected_skills` must agree.
- When no skill is selected, use `selected_skills: []`, `skill_count: 0`, `skill_names: []`, and an empty `skills/` directory.

Summary fields:

- `tooling_notes`: concrete tools, libraries, CLIs, or commands TB3 task design may consider.
- `environment_notes`: files, packages, permissions, paths, runtime layout, and resource implications.
- `verifier_notes`: expected artifacts, verifier-only ground truth placement, `tests/Dockerfile` dependencies, anti-cheat risks, and oracle/nop risks.
- `implementation_risks`: concrete ways the idea could produce a weak, flaky, overfitted, or invalid task.
- `recommended_direction`: one concise paragraph describing the best TB3 task shape.
- `difficulty_hardening`: concrete constraints that preserve the brainstorm difficulty floor. Treat feasibility advice such as small keyspaces, bundled fixtures, or pinned parameters as bounds, not as permission to simplify the task into a one-command or one-loop solve.

Difficulty hardening rules:

- Include at least one `too_easy_risks` item that would make the task too easy for target agents.
- Include at least one `recommended_hardening` item that adds realistic complexity while preserving oracle solvability.
- Include `do_not_simplify` guidance for tempting shortcuts, such as replacing a credential-recovery task with a direct `unzip -P` loop unless another independent hard step is added.
- For security or forensics ideas, prefer deriving candidate sets, parameters, or validation inputs from visible artifacts over directly stating every command flag and guaranteeing the answer is obvious in a tiny wordlist.
- For programming or type-system ideas, prefer interacting edge cases and anti-hardcoding checks over merely increasing hidden fixture count.
- `minimum_complexity_contract` should describe the lowest acceptable logic complexity for TB3 task design, not just the minimum valid TB3 packaging.

## Task Generation

Task generation converts one brainstorm idea and one curated SkillNet summary into:

```text
output/task/
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

Read only:

```text
brainstorm/<seed_id>/seed_brainstorm.json
skillnet/<seed_id>/<idea_id>/skill_summary.json
seed/<seed_id>/                         # background only
```

Curated idea skills are loaded under `.claude/skills/`. Use relevant `references/`, `examples/`, `scripts/`, or `templates/`, but do not copy raw SkillNet dumps into the generated task.

Task design priority:

1. Preserve the intended difficulty first, then package the task correctly as TB3.
2. Use the brainstorm difficulty profile and SkillNet difficulty hardening guidance as design constraints.
3. Do not weaken task logic just to make the directory, Docker setup, verifier, or metadata easier to generate.
4. Keep difficulty grounded in realistic investigation, debugging, transformation, integration, reasoning, or multi-step execution.

Generation boundaries:

- Generate exactly one task for the requested idea id.
- Keep the seed as read-only background.
- Target TB3 even when the seed is TB2 or older.
- Write generated task files only under `output/task/`.
- Write optional early validation artifacts only under `output/local-validation/`.
- Do not put `.claude/`, `seed/`, `brainstorm/`, `skillnet/`, `raw/`, Claude logs, brainstorm JSON, SkillNet summaries, prompt files, or validation logs into `output/task/`.
- Only generate the requested task and optional local validation artifacts.

TB3 format checklist:

- `task.toml` declares container-absolute artifacts, complete metadata, separate verifier, integer-valued agent timeout, and full environment resource fields.
- `difficulty_explanation` states the task's logic complexity and expected implementation/artifact edit scope.
- Synthetic author identity fields stay empty unless truthful values are provided; category and tags are non-empty.
- `instruction.md` is concise, outcome-focused, absolute-path based, artifact-complete, avoids lengthy formal `##` sectioning and hard-wrapped prose, and ends with the exact TB3 suffix.
- `environment/` contains only the starting state; `solution/` and `tests/` are never copied into the agent image.
- Visible environment files and visible environment text must not contain explanatory comments, docstrings, inline hints, known-defect notes, TODO/FIXME markers, descriptions of what the bug is, instructions for how to fix it, or prose that helps the agent infer the intended fix.
- The visible environment must not contain problem-specific test examples, sample corpora, self-checks, expected outputs, or other fixtures that demonstrate the task's target behavior or make the intended solution inferable.
- `solution/solve.sh` solves from the visible starting state.
- `tests/` is a separate verifier image that checks outcomes and writes reward without relying on command order, exact library choice, or oracle-only details.

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

Use this profile unless the task has a concrete reason to differ. Keep `instruction.md` suffix `N` exactly equal to `[agent].timeout_sec`.

Required TB3 suffix:

```text
You have N seconds to complete this task. Do not cheat by using online solutions or hints specific to this task.
```

If a Dockerfile installs apt packages, run apt update first, do not pin apt package versions, and clean `/var/lib/apt/lists/*`.

Early Harbor validation:

```bash
mkdir -p output/local-validation/harbor-jobs
harbor run -p output/task -a oracle -o output/local-validation/harbor-jobs --job-name oracle -k 1 -y > output/local-validation/oracle.log 2>&1
harbor run -p output/task -a nop -o output/local-validation/harbor-jobs --job-name nop -k 1 -y > output/local-validation/nop.log 2>&1
```

If Harbor or Docker is unavailable, record the command and error under `output/local-validation/` and continue with static checks. If you inspect rewards, read only under `output/local-validation/harbor-jobs/`; common locations are `**/result.json`, `**/verifier/reward.txt`, and `**/verifier/reward.json`. Expected signal is oracle reward `1` and nop reward `0`; fix clear generation bugs before finishing when practical.

## Quality Check

Difficulty modification triggers:

- The task reduces to a direct single-tool command, a short obvious loop, or a direct fixture lookup.
- The reference solution has fewer than three meaningful independent stages.
- A security, archive, credential, or forensics task uses a tiny guaranteed-hit wordlist or fully pinned command parameters without separate inference, triage, validation, normalization, or post-unlock processing.
- A programming or type-system task only tests shallow happy paths, can plausibly be hardcoded from visible examples, or relies on hidden fixture count rather than compositional requirements.
- The task appears too hard despite oracle success, such as likely max-turn failure from excessive grammar/data scale or too many interacting edge cases.

Environment leakage modification triggers:

- Visible environment files or visible environment text contain explanatory comments, docstrings, inline hints, known-defect notes, TODO/FIXME markers, bug descriptions, fix instructions, or prose that helps the agent infer the intended fix.
- The visible environment contains problem-specific test examples, sample corpora, self-checks, expected outputs, or fixtures that demonstrate the target behavior or make the intended solution inferable.

## Task Modification

For difficulty hardening, prefer:

- adding decoys or realistic extra artifacts;
- deriving one missing parameter from visible files;
- adding a second independent output artifact;
- strengthening hidden edge cases;
- increasing realistic data scale within oracle and verifier runtime.

For difficulty softening, prefer narrowing grammar, reducing data scale, reducing interacting edge cases, or clarifying visible specification. Do not add ambiguity, random brute force, network dependency, fragile formatting traps, or verifier-only gotchas.
