# Task Repair

You are in an isolated workspace. Work only inside the current directory.

Inputs:

- Task directory: the generated task under `task/<seed_id>/<idea_id>/`.
- Review directory: `review/<task_id>/`.
- Optional oracle/nop validation directory: `oracle-nop-check/<task_id>/`. It may contain a failed check run that review used as repair evidence.

Goal:

Repair one generated Terminal-Bench/Harbor task according to `review/<task_id>/review.json`.

Write the complete repaired task to `output/task/`.

Required work:

1. Read `review/<task_id>/review.json` first.
2. Confirm the review `decision` is `needs_modification`.
3. Read the task under `task/<seed_id>/<idea_id>/`.
4. Apply only the repairs needed to address `modification_items`.
5. Preserve the same task concept unless the review explicitly says a narrow concept adjustment is required.
6. Write a complete repaired TB3 Harbor task under `output/task/`.
7. If Harbor is available, run one workspace-local oracle check and one workspace-local nop check against `output/task/`, capped at 900 seconds each. Fix clear repair bugs before finishing when practical.
8. Stop.

Minimum required output skeleton:

The repaired task may and should also contain task-specific source code, data,
fixtures, configuration, and other files required by the task. Preserve such
files from the input task unless a review item explicitly requires changing or
removing them.

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

Boundaries:

- Do not modify files under `task/`, `review/`, or `oracle-nop-check/`.
- Do not write outside `output/`.
- Put optional validation logs and Harbor jobs only under `output/local-validation/`.
- Keep `output/task/` limited to the complete repaired task: the minimum
  skeleton above plus task-specific files required for the task. Do not include
  runner inputs, prompts, validation output, caches, or transient artifacts.
- Use only the local validation commands described here when validating.
- Do not include chain-of-thought in any output.

TB3 repair checklist:

- Keep the task tree complete and TB3-compatible.
- Keep `instruction.md` concise, outcome-focused, absolute-path based, artifact-complete, and free of solution steps, command sequences, tool nudges, role prompts, or thinking prompts.
- Keep `instruction.md` prose natural: do not hard-wrap paragraphs at 80 or 90 columns; each paragraph should be one physical line, with blank lines between paragraphs and bullets only when genuinely useful.
- Keep the required TB3 suffix, with `N` exactly equal to `[agent].timeout_sec`.
- Keep `task.toml` artifacts, metadata, verifier mode, agent timeout, and environment resources aligned with the repaired task.
- Keep `environment/` as the agent-visible starting state only; never copy `solution/` or `tests/` into the agent image.
- Keep `solution/solve.sh` as a real reference solution from the visible starting state; every external command must exist in the agent image or be installed before use.
- Keep verifier dependencies in `tests/Dockerfile`; `tests/test.sh` verifies outcomes and writes reward.
- Do not leak answers, verifier ground truth, hidden expected outputs, temporary workspace files, or copied inputs into the repaired task.
- Keep metadata, especially `difficulty_explanation`, consistent with any difficulty repair.
- For difficulty hardening, prefer adding decoys, deriving one missing parameter from visible artifacts, adding a second independent output artifact, strengthening hidden edge cases, or increasing realistic data scale within oracle/verifier runtime. Do not add ambiguity, random brute force, network dependency, fragile formatting traps, or verifier-only gotchas.
- For difficulty softening, reduce interacting edge cases, narrow grammar/data scale, or clarify visible specification while keeping the task outcome-based and nontrivial.

Optional Harbor validation:

```bash
mkdir -p output/local-validation/harbor-jobs
HARBOR_COMMAND="${HARBOR_BIN:-harbor}"
timeout -k 30s 870s "$HARBOR_COMMAND" run -p output/task -a oracle -o output/local-validation/harbor-jobs --job-name oracle -k 1 -y > output/local-validation/oracle.log 2>&1
timeout -k 30s 870s "$HARBOR_COMMAND" run -p output/task -a nop -o output/local-validation/harbor-jobs --job-name nop -k 1 -y > output/local-validation/nop.log 2>&1
```

Each early Harbor invocation has a 900-second wall-clock budget: `timeout` sends `TERM` at 870 seconds and allows a 30-second grace period before `KILL`. Exit code `124`, or `137` when the hard kill is needed, indicates a timeout. If Harbor, Docker, or `timeout` is unavailable, or a check times out, record the failed command and error under `output/local-validation/` and continue with static checks. Expected signal is oracle reward `1` and nop reward `0`. The pipeline's phase4 check remains the authoritative validation.

Final validation:

- Confirm every `modification_items` entry has been addressed or explicitly made obsolete by the repair.
- Confirm `output/task/` contains only the final task files.
- Confirm `instruction.md`, `task.toml`, `environment/Dockerfile`, `solution/solve.sh`, and verifier expectations describe the same repaired behavior.
- Confirm optional validation artifacts, if any, are outside `output/task/`.
