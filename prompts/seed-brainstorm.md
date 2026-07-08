# Seed Brainstorm

Use the `seed-brainstormer` subagent and the `tb-harbor-task-generation` skill.

You are in an isolated workspace. Work only inside the current directory.

Inputs:

- Seed id: `{{SEED_ID}}`
- Seed path: `{{SEED_PATH}}`
- Idea count requirement: {{IDEA_COUNT_REQUIREMENT}}

Goal:

Create one concise brainstorm artifact at `output/seed_brainstorm.json`. The brainstorm should turn the seed's abstract terminal-work capability into substantially different TB3 task ideas.

Brainstorm priority:

- Preserve only the seed's abstract capability.
- Change at least two meaningful dimensions, such as artifact type, domain scenario, output shape, toolchain, verifier design, data scale, environment complexity, or failure mode.
- Make every idea substantially harder than the seed. Within realistic, verifiable, fair, and oracle-solvable bounds, make each idea as hard as possible.
- Make the difficulty auditable. Each idea must state a minimum number of independent subskills, concrete too-easy antipatterns to avoid, hardening levers, and fairness bounds.
- Aim at TB3-quality tasks: realistic paid terminal work, outcome verification, separate-verifier feasibility, declared container-absolute artifacts, and open-internet assumptions.
- Make difficulty come from investigation, debugging, transformation, integration, reasoning, or multi-step execution.
- Avoid difficulty from long prompts, arbitrary formatting traps, ambiguous requirements, randomness, blocked network access, low timeouts, resource pressure, or hidden gotchas.
- For credential, archive, forensics, or security ideas, a bounded local wordlist or constrained keyspace is only a feasibility bound. Do not make the whole task a direct guaranteed-hit dictionary loop; add parameter inference, artifact triage, independent verification, normalization, or a second post-unlock stage.
- For programming or type-system ideas, difficulty should come from compositional edge cases and interaction between requirements, not simply from many hidden fixtures or compiler timeouts.
- If an idea clearly needs more or less than the default generation profile (`agent.timeout_sec = 7200.0`, `verifier.timeout_sec = 600.0`, `cpus = 2`, `memory_mb = 4096`), mention it in `verifier_sketch` or `risk_notes`.

Required work:

1. Read `{{SEED_PATH}}` as read-only input.
2. Understand the seed goal, visible starting environment, reference solution, verifier behavior, reward path, hidden ground truth boundaries, and likely failure modes.
3. Treat the seed as a capability source only. Many seeds are TB2 or older Harbor tasks; do not copy their task format, story, wording, fixtures, answers, verifier logic, or distinctive surface assets.
4. {{IDEA_COUNT_REQUIREMENT}}
5. Write only `output/seed_brainstorm.json`.
6. Stop.

Boundaries:

- Only complete the required brainstorm artifact.
- Do not create task directories or any other output files.
- Do not run validation or container checks.
- Do not modify anything under `{{SEED_PATH}}`.
- Stay inside the current workspace and the specified input/output paths.

Output schema:

```json
{
  "seed_id": "string",
  "source_path": "{{SEED_PATH}}",
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

Keep the JSON concrete and short. It is an internal design artifact, not the final task instruction.
