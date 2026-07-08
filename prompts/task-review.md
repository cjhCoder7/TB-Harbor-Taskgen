# Task Review

You are in an isolated workspace. Work only inside the current directory.

Inputs:

- Task directory: the single generated task under `task/<seed_id>/<idea_id>/`.
- Oracle/nop validation directory: `oracle-nop-check/<task_id>/`.

Goal:

Review one generated Terminal-Bench/Harbor task and decide whether it is ready, needs concrete changes, or should be rejected. The oracle/nop status may show either a successful gate or a failed gate with concrete fix evidence.

Write the review result to `output/review/`.

Review priority:

1. Difficulty calibration is a required gate, not an optional comment. Do not let TB3 formatting correctness hide a task that is too easy or too hard.
2. Oracle/nop failure is review evidence, not an automatic rejection. If the task concept is fixable, use `needs_modification` and point to the failed reward, exit code, log, or task file that should be fixed.
3. Use `needs_modification` with `area: "difficulty"` when a task is structurally valid but too easy for the intended benchmark.
4. A task is too easy when the visible environment and instruction reduce the solve to a direct single-tool command, a short obvious loop, a direct fixture lookup, or a reference solution with fewer than three meaningful independent stages.
5. Security, archive, credential, or forensics tasks are too easy when they use a tiny guaranteed-hit wordlist or fully pinned command parameters without requiring separate inference, triage, validation, normalization, or post-unlock processing.
6. Programming or type-system tasks are too easy when they only test shallow happy paths, can plausibly be hardcoded from visible examples, or rely on hidden fixture count instead of compositional requirements.
7. If a task appears too hard or likely to hit max turns despite oracle success, use `needs_modification` with `area: "difficulty"` and ask for a narrower grammar, smaller bounded data scale, clearer visible spec, or fewer interacting edge cases.

Required work:

1. Inspect the generated task files under `task/<seed_id>/<idea_id>/`.
2. Inspect `oracle-nop-status.json` and relevant oracle/nop logs under `oracle-nop-check/<task_id>/`.
3. Check TB3 structure, instruction quality, verifier quality, environment reproducibility, solution correctness, metadata consistency, difficulty realism, security, answer leakage, and artifact cleanliness.
4. Decide whether the task is `ready`, `needs_modification`, or `rejected`.
5. Write `output/review/review.json`.
6. Write `output/review/review.md`.
7. Stop.

Boundaries:

- Do not modify the task.
- Do not modify files under `task/` or `oracle-nop-check/`.
- Do not write outside `output/review/`.
- Only write the requested review outputs.
- Do not include chain-of-thought in any output.

Decision contract:

- Use `ready` only when oracle/nop passed and there are no required or recommended modifications and no blocking rejection reasons.
- Do not use `ready` when the task is likely too easy or too hard for the intended benchmark band; return `needs_modification` with a concrete `difficulty` modification item instead.
- Do not use `ready` when oracle reward is not exactly `1.0`, nop reward is not exactly `0.0`, or either check exited unsuccessfully.
- Use `needs_modification` when the task is worth fixing and has at least one concrete modification item.
- Use `rejected` only when the task is not worth continuing as a candidate.
- If a defect is fixable without replacing the task concept, use `needs_modification` instead of `rejected`.
- If `decision` is `ready`, `modification_items` must be empty and `blocking_reasons` must be empty.
- If `decision` is `needs_modification`, `modification_items` must be non-empty and `blocking_reasons` must be empty.
- If `decision` is `rejected`, `modification_items` must be empty and `blocking_reasons` must be non-empty.

Quality checklist:

- Benchmark suitability: the task is a benchmark item, not an agent prompt; it is realistic, fair, deterministic, and free of real credentials, API keys, private data, or unsafe instructions.
- TB3 structure: required files/directories exist; `task.toml` has top-level artifacts, complete metadata, separate verifier, integer-valued agent timeout, and environment resource fields; declared artifacts align with `instruction.md`.
- Instruction quality: concise, direct, human-edited, absolute-path based, outcome-focused, no solution steps/tool nudges/role prompts/thinking prompts, and exact TB3 suffix with `N == [agent].timeout_sec`.
- Instruction quality: do not accept a lengthy, highly polished Markdown description with formal `#` or `##` sectioning; treat it as a likely pure LLM synthesis artifact that should be shortened and made more natural before delivery.
- Instruction quality: do not accept prose that is hard-wrapped at 80 or 90 columns inside a paragraph; request natural paragraphs where each paragraph is one physical line, separated by blank lines.
- Environment and solution: agent image contains only the starting state, never copies `solution/` or `tests/`, is reproducible, and the reference solution solves from the same visible starting state with tools available in the agent image.
- Environment comments: visible environment files must not contain explanatory comments, docstrings, inline hints, known-defect notes, TODO/FIXME markers, or prose that helps the agent infer the intended fix.
- Verifier quality: separate verifier image, verifier-only files and dependencies stay in `tests/Dockerfile`, tests check externally visible outcomes and write reward, and reward hacking is resisted.
- Dynamic evidence: if oracle/nop validation passed, oracle reward is exactly `1.0` and nop reward is exactly `0.0`; if it failed, the review must explain the failure and choose `needs_modification` when the issue is fixable.
- Cleanliness: no generated/validation artifacts, caches, bytecode, transient logs, prompts, temporary workspace files, or leaked copied inputs remain in the task directory.

Output schema:

`review.json` must be valid JSON. It must contain exactly these top-level fields:

- `task_id`: string.
- `decision`: one of `ready`, `needs_modification`, or `rejected`.
- `summary`: string.
- `modification_items`: array.
- `blocking_reasons`: array.

Do not include any other top-level fields.

Each item in `modification_items` must contain:

- `area`: non-empty string.
- `priority`: non-empty string.
- `message`: concise description of what must be changed.
- `evidence`: array of file paths, field names, or observed facts supporting the item.
- `repair_direction`: concise guidance about what to change, without modifying files.

Each item in `blocking_reasons` must contain:

- `area`: non-empty string.
- `message`: concise description of why the task should not proceed.
- `evidence`: array of file paths, field names, or observed facts supporting the decision.

JSON shape:

```json
{
  "task_id": "<task_id>",
  "decision": "needs_modification",
  "summary": "<concise review summary>",
  "modification_items": [
    {
      "area": "instruction",
      "priority": "high",
      "message": "<what must be changed>",
      "evidence": ["<path-or-field-or-observed-fact>"],
      "repair_direction": "<repair direction>"
    }
  ],
  "blocking_reasons": []
}
```

Markdown report:

`review.md` must be short and human-readable. It must include:

- Final decision.
- Summary.
- Modification items, or a clear statement that there are no modification items.
- Blocking reasons, or a clear statement that there are no blocking reasons.

Final validation:

- Confirm `review.json` is valid JSON.
- Confirm `review.json` uses exactly the required top-level fields.
- Confirm the array requirements match the selected `decision`.
- Confirm `review.md` and `review.json` state the same decision.
