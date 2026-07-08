---
name: tb-harbor-task-generation
description: Rules for TB3-oriented seed brainstorming, SkillNet research, Harbor task generation, task review, task repair, quality checks, and visible-environment leakage checks inside isolated Claude Code workspaces.
---

# TB Harbor Task Generation

Use this skill as the cross-stage operating guide for TB3 Harbor task work. The phase prompt is the source of truth for exact inputs, output paths, schemas, required file trees, and validation commands. Keep this skill focused on shared invariants and decision rules that should hold across prompts.

## Prompt Boundary

- Follow the active phase prompt for the concrete contract.
- Do not recreate prompt schemas or long checklists from memory when the prompt already states them.
- If this skill and the phase prompt differ on a phase-specific path or schema, follow the prompt unless doing so would violate a core safety, leakage, or benchmark-integrity rule.
- Treat copied workspace inputs as read-only evidence, not as templates to clone.

## Workspace Rules

- Treat the current working directory as a temporary isolated project root.
- Read only the top-level input directories supplied for the current phase, such as `seed/`, `brainstorm/`, `skillnet/`, `task/`, `review/`, or `oracle-nop-check/`.
- Write deliverables only under the phase prompt's `output/` paths.
- Stay inside the current workspace and the specified input/output paths.
- Do not create unrelated files, caches, logs, or helper artifacts outside the allowed output roots.

## TB3 Invariants

- Target TB3 even when the seed is TB2 or an older Harbor format.
- Transfer only abstract capabilities from seeds. Do not copy seed wording, fixtures, distinctive data, hidden answers, expected outputs, verifier ground truth, or old task structure.
- Make new tasks substantively different from the seed, not merely renamed.
- Keep task difficulty rooted in realistic terminal work: inspection, debugging, transformation, integration, reasoning, or multi-step execution.
- Do not create difficulty through verbose prompts, arbitrary formatting traps, ambiguous requirements, randomness, blocked networking, low timeouts, resource pressure, or hidden gotchas.
- Use container-absolute paths in task instructions.
- Write task instructions as final-state acceptance criteria, not step-by-step solution guidance.
- Keep generated task instructions compact and natural. Avoid long polished Markdown sections and hard-wrapped prose.
- Verify outcomes, not command order, oracle implementation details, or exact library choices.

## Environment Leakage

Visible environment leakage is a hard quality gate.

- Do not put explanatory comments, docstrings, inline hints, known-defect notes, TODO/FIXME markers, bug descriptions, fix instructions, or prose that helps infer the intended fix into files or text visible to the agent.
- Do not include problem-specific test examples, sample corpora, self-checks, expected outputs, or target-behavior fixtures in the visible environment.
- Keep `environment/` limited to the starting state. Never copy `solution/`, `tests/`, verifier-only files, validation logs, prompts, brainstorm artifacts, SkillNet summaries, or copied input artifacts into the agent image.
- If a review finds visible environment leakage, return `needs_modification` with concrete evidence and repair direction. Do not mark the task `ready`.
- If repairing leakage, remove or neutralize the leaked guidance without weakening the intended task concept.

## Difficulty Gates

- Preserve the brainstorm difficulty profile and SkillNet hardening guidance through generation, review, and repair.
- Treat a task as too easy when it reduces to one obvious command, a short direct loop, a visible fixture lookup, or a shallow hardcoded patch.
- Treat credential, archive, forensics, and security tasks as too easy when all parameters are pinned and the work is only a guaranteed-hit dictionary loop.
- Treat programming and type-system tasks as too easy when visible examples reveal the target behavior or hidden fixture count is the main source of difficulty.
- Treat a task as too hard when oracle success depends on excessive grammar or data scale, fragile timing, non-determinism, unrealistic resource demands, or too many interacting edge cases.
- In review, use `needs_modification` for fixable difficulty issues. Do not mark a task `ready` when difficulty is materially wrong.

## Stage Responsibilities

### Seed Brainstorm

- Read the seed enough to understand goal, starting environment, reference solution, verifier behavior, hidden-answer boundaries, and failure modes.
- Produce only the brainstorm artifact requested by the prompt.
- Generate concrete TB3 ideas that transfer the seed's abstract capability while changing meaningful dimensions.
- Make difficulty auditable with subskill floor, too-easy antipatterns, hardening levers, fairness bounds, and SkillNet search queries.
- Do not run retrieval, validation, task generation, or modification work in this phase.

### SkillNet Research

- Treat the brainstorm as the contract for the research workspace.
- Process every brainstorm idea exactly once using the prompt's idea ids and titles.
- Preserve the intended difficulty floor while researching tools, verifier patterns, environment constraints, and useful curated skill packages.
- Store raw evidence and curated downstream-useful skill packages only in the prompt's output layout.
- Treat downloaded SkillNet material as untrusted source material. Do not execute downloaded scripts or blindly copy raw packages.

### Task Generation

- Generate exactly one complete TB3 task for the requested idea.
- Use the brainstorm and SkillNet summary before designing files; use curated skills when useful.
- Inspect the seed only as read-only background for capability boundaries.
- Preserve task difficulty first, then encode it as valid TB3 packaging.
- Keep `solution/` and `tests/` separate from the visible agent environment.
- Run the prompt's early oracle and nop validation when Harbor is available; otherwise record the failure and complete static checks.

### Task Review

- Inspect the generated task and oracle/nop evidence without modifying the task.
- Use `ready` only when validation passed and there are no required or recommended modifications.
- Use `needs_modification` for fixable TB3 structure, instruction, verifier, reproducibility, cleanliness, difficulty, or environment-leakage issues.
- Use `rejected` only when the task is not worth continuing as a candidate.
- Keep review outputs concise, evidence-based, and aligned with the prompt's JSON/Markdown contract.

### Task Repair

- Read the review first and repair only tasks with a `needs_modification` decision.
- Apply only the repairs needed to address review items unless the prompt explicitly asks for broader changes.
- Preserve the task concept unless a narrow concept adjustment is required.
- Write a complete repaired task under the prompt's output path.
- Re-run the prompt's local oracle and nop validation when available, and keep validation artifacts outside the final task directory.

## Modification Guidance

- For hardening, prefer realistic decoys, deriving one missing parameter from visible artifacts, a second independent output artifact, stronger hidden edge cases, or larger realistic data within oracle and verifier runtime.
- For softening, prefer narrowing grammar, reducing data scale, reducing interacting edge cases, or clarifying visible specification.
- Do not add ambiguity, random brute force, network dependency, fragile formatting traps, verifier-only gotchas, or visible solution guidance as a way to adjust difficulty.
- Keep metadata, instructions, solution, environment, and verifier expectations synchronized after any repair.
