<h1 align="center">TB-Harbor-Taskgen 开发者指南</h1>

<p align="center">
  <a href="TB_HARBOR_TASKGEN_MVP_SPEC.md">English</a>
  ·
  <strong>简体中文</strong>
</p>

本文档说明 TB-Harbor-Taskgen 当前代码实现，面向需要运行、调试或扩展项目的开发者。文档以代码为准；如果行为在代码中变化，应在同一改动中更新本文档。

## 1. 项目范围

TB-Harbor-Taskgen 将一个 Terminal-Bench Harbor seed task 转换为一个或多个生成的 TB3 Harbor task candidate。工作流按 phase 组织：

1. 读取 seed task 并生成任务思路。
2. 为每个思路检索 SkillNet 证据。
3. 生成 working Harbor task。
4. 运行 Harbor oracle/nop 检查。
5. 评审任务。
6. 当评审要求修改时修复任务。
7. 将任务移动到 accepted 或 rejected 输出目录。

流水线在本地运行。需要语言模型判断的 phase 始终使用 Claude Code 作为 agent。默认调用配置的 Anthropic-compatible backend；传入 `--openai` 时，改为通过临时 LiteLLM 网关调用 OpenAI-compatible backend。确定性 phase 使用 Python 和 Harbor。

## 2. 仓库结构

| 路径 | 作用 |
| --- | --- |
| `src/taskgen/cli.py` | 顶层 CLI、phase 注册表、完整流水线编排。 |
| `src/taskgen/phases/` | 各 phase 的 run 和 validate 实现。 |
| `src/taskgen/claude/` | Claude workspace 准备、执行包装和 cost 解析。 |
| `src/taskgen/openai_gateway.py` | 临时 LiteLLM 生命周期和 OpenAI-compatible 协议桥接。 |
| `src/taskgen/harbor/oracle_nop.py` | Harbor oracle/nop 检查运行器和状态写入。 |
| `src/taskgen/maintenance/clean_intermediate.py` | 运行中间产物清理。 |
| `prompts/` | 渲染到 `runs/prompts/` 的 prompt 模板。 |
| `cc-definitions/` | 复制到 Claude workspace 的 agents 和项目 skills。 |
| `scripts/` | Shell 入口，负责加载本地环境并调用 Python 模块。 |
| `tests/` | 配置、workspace、validation 和 phase 行为的单元测试。 |
| `runs/` | 运行产物、日志、渲染后的 prompts、workspaces 和 manifest。 |
| `generated/` | 生成中的任务和最终任务目录。 |
| `seeds/` | phase1 和 phase3 使用的输入 seed tasks。 |

## 3. ID 和路径规则

`seed_id` 和 `idea_id` 必须是单个路径安全片段，匹配：

```text
[A-Za-z0-9._-]+
```

它们不能为空，不能是 `.` 或 `..`，也不能包含保留分隔符 `__`。
`seed_id` 最长 128 个字符，`idea_id` 最长 120 个字符，从而保证组合后的
task id 不超过文件系统单个路径组件的长度限制。

phase4 之后使用的稳定 task id 是：

```text
<seed_id>__<idea_id>
```

该 id 用于 review 目录、oracle/nop 状态目录、最终任务目录和 manifest 事件。

## 4. 配置和入口

### `model.json`

`model.json` 配置 Claude binary、Claude 与 Harbor 外层超时、默认 Claude-compatible 模型设置和可选 OpenAI-compatible 模型设置：

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

支持的 effort 值是 `low`、`medium`、`high`、`xhigh`、`max`。

`claude_code_path` 如果不是绝对路径，会从项目根目录解析。该 binary 是本地文件，并被 git 忽略。如果删除该字段，runner 会先使用唯一一个可执行的 `cc-binary/claude-*`，再回退到 `PATH` 上的 `claude`。

`claude_code_timeout_sec` 设置每次 Claude Code 运行的超时时间，单位为秒，且必须为正数。默认值 `1800` 表示 30 分钟；运行达到该时限后，runner 会在 POSIX/Linux 环境终止其独立进程组，并记录退出码 `124` 和 `timed_out: true`。

`claude_code_phase_timeouts_sec` 是可选的按 phase 覆盖映射。key 支持与 `phase_efforts` 相同的 canonical name 和 alias，value 必须是正有限数；命中的覆盖值优先于 `claude_code_timeout_sec`。当前配置为 phase3 和 phase6 设置 `10800` 秒，其他 phase 保留全局 30 分钟兜底值。

`harbor_check_timeout_sec` 是每次 Harbor oracle 或 nop 调用的正有限外层超时，默认值为 `10800` 秒。检查超时后会在可 review 的 phase4 status 中记录退出码 `124` 和超时元数据。未知的 `model.json` 顶层字段会被拒绝，避免拼写错误静默回退默认值。

`phase_efforts` 支持 `src/taskgen/config.py` 中定义的 canonical phase key 和 alias。新增配置时优先使用 `phase1` 到 `phase7`。

可选的 `openai` 对象只在传入 `--openai` 时生效。显式 `--model` 优先于 `openai_default_model`，最终选中的模型名会原样使用。Effort 依次从显式 `--effort`、`openai_phase_efforts` 和 `openai_default_effort` 解析，不会回退到 Claude 配置。缺少必需值或包含未知 key 时，会在模型 phase 启动前失败。

### Shell 脚本

| 脚本 | 行为 |
| --- | --- |
| `scripts/taskgen.sh` | 选择本地环境并运行 `python3 -m taskgen.cli`。 |
| `scripts/run-claude-logged.sh` | 运行 Claude wrapper 并记录 session metadata。 |
| `scripts/run-harbor-oracle-nop.sh` | 运行 Harbor oracle/nop 检查。 |
| `scripts/clean-intermediate.sh` | 清理中间运行产物。 |
| `scripts/tool_init.sh` | 通过 `uv tool install` 安装 `harbor==0.13.2`、`skillnet-ai==0.0.18` 和 `litellm[proxy]==1.91.1`。 |

对应文件存在时，`scripts/taskgen.sh` 默认 source `scripts/env_init.sh`，传入 `--openai` 时改为 source `scripts/env_openai_init.sh`。两个文件都只用于本机并被 git 忽略，可从对应的 `.example.sh` 文件创建。嵌套 wrapper 会保留已启用的网关环境；所有 wrapper 都会把 `src/` 加入 `PYTHONPATH`。

## 5. CLI

console script `taskgen` 指向 `taskgen.cli:main`。本地开发通常使用 shell 入口：

```bash
scripts/taskgen.sh <command> ...
```

### 查看命令

```bash
scripts/taskgen.sh phases
scripts/taskgen.sh paths <seed_id> [--idea-id <idea_id>] [--task-id <task_id>]
scripts/taskgen.sh command <phase> <seed_id> [--idea-id <idea_id>]
scripts/taskgen.sh next <seed_id>
```

### 单个 Phase

```bash
scripts/taskgen.sh run <phase> <seed_id> [--idea-id <idea_id>] [--dry-run] \
  [--model <model>] [--effort <effort>] [--openai]
scripts/taskgen.sh validate <phase> <seed_id> [--idea-id <idea_id>] [--json]
```

`phase3` 到 `phase7` 必须传 `--idea-id`。`phase1` 和 `phase2` 不接受 `--idea-id`。单独运行 `phase1` 时也可以传 `--idea-count N` 来请求并校验精确的 brainstorm idea 数量。

Claude-backed phases 支持 `--model` 和 `--effort`：`phase1`、`phase2`、`phase3`、`phase5`、`phase6`。相同的 phases 支持 `--openai`，确定性 phases 会拒绝该参数。dry-run 会解析并打印 OpenAI-compatible 模型和 effort，但不会启动 LiteLLM。

### 完整 Pipeline

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

pipeline 先运行 phase1 和 phase2，然后处理指定 idea 或 phase1 输出中的全部 ideas。`--idea-count` 会请求并校验 phase1 brainstorm 的精确 idea 数量；如果已有 phase1 输出数量不同，phase1 会重跑。除此之外，已通过当前验证的 phase 会被跳过，除非传入 `--force`。如果 phase5 返回 `needs_modification`，pipeline 会运行 phase6，然后强制重跑 phase4 和 phase5，直到评审结果为 `ready`、`rejected`，或 repair budget 用完。若 dry-run 需要重跑 phase1 且未显式提供 `--idea-id`，后续按 idea 的计划尚无法推断，命令会返回非零。

### OpenAI-Compatible Claude Code Backend

`--openai` 不会替换 Claude Code agent，只改变 Claude Code 使用的模型传输链路：

```text
Claude Code -> Anthropic /v1/messages -> 临时 LiteLLM
            -> 上游 OpenAI-compatible /v1/responses
```

在 `scripts/env_openai_init.sh` 中设置 `OPENAI_BASE_URL` 和
`OPENAI_API_KEY`。上游必须支持上图所示的 `POST /v1/responses` 路径。

`run --openai` 为该 phase 启动一个回环网关；`pipeline --openai` 和
`run-all --openai` 在全部 Claude-backed phases 与 repair rounds 间共用一个网关。phase 会在本地网关通过就绪检查后开始；上游连通性由首次模型请求验证。正常结束、失败或中断后都会停止网关。

上游凭据只提供给 LiteLLM，不会传给 Claude Code 或其工具。网关使用 LiteLLM 标准的 Anthropic Messages 转换，不会主动禁用 thinking；所选 effort 会经 Claude Code 传递，并可能由 LiteLLM 按模型能力调整。Skills、subagents 和 Bash 仍由 Claude Code 实现；完整运行还要求上游支持 streaming 和 tool calling。

## 6. 产物布局

运行产物路径是确定的，并由各 phase 模块验证。

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

`runs/` 和 `generated/` 的内容默认被 git 忽略，只保留骨架 `.gitkeep`。`seeds/` 是输入目录；seed 数据是否提交需要按项目需要单独决定。

## 7. Claude Workspace 模型

Claude-backed phases 使用 `src/taskgen/claude/runner.py` 和 `src/taskgen/claude/workspace.py`。

支持的 Claude phases：

```text
seed-brainstorm
skillnet-research
task-generation
task-review
task-repair
```

每次运行都会创建：

```text
runs/claude-sessions/<phase>/<subject>/<run_id>/
runs/workspace/<phase>/<subject>/<run_id>/
```

workspace 中会放入渲染后的 prompt、项目 agents、项目 skills 和该 phase 所需输入。Claude 将输出写入 `output/...`；Claude 成功退出后，每个声明输出都必须存在。输出会先复制到目标旁的临时路径，再原子切换到项目运行目录，复制失败不会破坏旧产物。`runs/output-sync-transactions/` 中的小型事务日志可让下一次运行在进程被强制终止后恢复或完成未结束的 rename 序列。

会修改数据的 phase、runner 和独立 workspace 命令会按 subject 串行执行，并持有清理命令使用的共享 activity guard。phase 只会把实际持有锁的 subject/activity 文件描述符传给它直接启动的 runner 子进程，从而让嵌套调用保持串行、避免重复加锁死锁，并保证父进程异常退出后锁仍持续到 runner 结束。

session metadata：

| 文件 | 作用 |
| --- | --- |
| `prompt.md` | 本次运行使用的 prompt 副本。 |
| `claude-code.txt` | Claude stream-json 输出和 stderr。 |
| `cost.json` | 解析后的 cost 和 token 摘要。 |
| `status.json` | 运行状态、超时元数据、workspace 路径、同步输出和 cost 摘要。 |

stream log 采用逐行增量解析。可选的 OpenRouter generation metadata 补充查询
默认受 30 秒总 deadline、100 个 generation ID 和单请求 10 秒超时约束，可通过
`TASKGEN_OPENROUTER_DEADLINE_SECONDS`、`TASKGEN_OPENROUTER_MAX_GENERATIONS`、
`TASKGEN_OPENROUTER_QUERY_TIMEOUT_SECONDS` 调整。只有每个 generation 都返回
有限且非负的 cost 时，provider cost 才会覆盖 stream cost。

在 OpenAI-compatible 模式下仍会记录 token usage，但金额是 Claude Code 的估算值，不是 provider 账单。

Claude 运行参数包括 `--verbose`、`--output-format=stream-json`、`--permission-mode bypassPermissions`、`--print`。`CLAUDE_CONFIG_DIR` 绑定到本次 run 目录，环境里设置 `IS_SANDBOX=1`。在 POSIX/Linux 环境中，runner 会把 Claude 放入独立进程组，因此配置超时、中断或 runner 异常时也会终止其启动的工具子进程。

## 8. Phase 契约

### 汇总

| Phase | 模块 | 范围 | 主要输出 |
| --- | --- | --- | --- |
| `phase1` | `phase1_seed_brainstorm` | Seed 级 Claude phase。 | `runs/brainstorm/<seed_id>/seed_brainstorm.json` |
| `phase2` | `phase2_skillnet_research` | Seed 级 Claude phase。 | `runs/skillnet/<seed_id>/` |
| `phase3` | `phase3_task_generation` | Idea 级 Claude phase。 | `generated/working/<seed_id>/<idea_id>/` |
| `phase4` | `phase4_oracle_nop_check` | Idea 级 Harbor phase。 | `runs/oracle-nop-check/<task_id>/oracle-nop-status.json` |
| `phase5` | `phase5_task_review` | Idea 级 Claude phase。 | `runs/reviews/<task_id>/review.json` |
| `phase6` | `phase6_task_repair` | Idea 级 Claude phase。 | 更新 `generated/working/<seed_id>/<idea_id>/` |
| `phase7` | `phase7_finalize` | Idea 级确定性 phase。 | `generated/accepted/<task_id>/` 或 `generated/rejected/<task_id>/` |

### Phase 1: Seed Brainstorm

输入：

- `seeds/<seed_id>/`，包含 `instruction.md`、`task.toml`、`environment/`、`solution/`、`tests/`。
- `prompts/seed-brainstorm.md`。
- `cc-definitions/agents/seed-brainstormer.md`。
- `cc-definitions/skills/tb-harbor-task-generation/SKILL.md`。

输出 JSON 必须包含 `seed_id`、`source_path`、`task_understanding`、`core_capabilities`、`avoid` 和非空 `ideas` 列表。每个 idea 必须包含 `idea_id`、`title`、`scenario`、`core_transfer`、`changed_dimensions`、`expected_artifacts`、`verifier_sketch`、`risk_notes`、`difficulty_profile`、`skillnet_queries`。

`difficulty_profile.minimum_independent_subskills` 必须至少为 `2`。

Manifest event：`brainstormed`。

### Phase 2: SkillNet Research

输入：

- Phase1 brainstorm JSON。
- `prompts/skillnet-research.md`。
- `cc-definitions/agents/skillnet-researcher.md`。
- 基础 generation skill。

输出：

- `runs/skillnet/<seed_id>/skillnet_index.json`。
- `runs/skillnet/<seed_id>/<idea_id>/skill_summary.json`。
- `runs/skillnet/<seed_id>/<idea_id>/skills/`。
- `runs/skillnet/<seed_id>/<idea_id>/raw/`。

状态值是 `ready`、`partial`、`no_strong_match`、`failed`。

skill package 名称必须路径安全，并以 `taskgen-<idea_id>-` 开头。`ready` 需要 3-5 个 selected skills；`partial` 需要 1-5 个。`skill_summary.json` 必须包含 selected skills、notes、implementation risks、`recommended_direction`，以及带最小复杂度、太简单风险、加难建议和不要简化边界的 `difficulty_hardening`。

Manifest event：`skillnet_done`。

### Phase 3: Task Generation

输入：

- Seed task。
- Phase1 brainstorm JSON。
- Phase2 SkillNet index。
- idea 的 `skill_summary.json`。
- idea 的 `skills/`。
- `prompts/task-generation.md`。
- `cc-definitions/agents/tb-harbor-task-generator.md`。
- 基础 generation skill。

输出任务布局：

```text
generated/working/<seed_id>/<idea_id>/
├── instruction.md
├── task.toml
├── environment/Dockerfile
├── solution/solve.sh
├── tests/Dockerfile
└── tests/test.sh
```

validation 会检查必需布局、必需目录非空、phase1/phase2 输入一致性，并拒绝 runner artifacts。生成任务不能包含 workspace 输入目录、Claude 运行文件、`.pyc`、`.log`、symlink，或 `runs/workspace`、`runs/claude-sessions`、`/shared/users/` 等本地 runner 路径。

生成提示词中的尽早 oracle/nop 检查是 best-effort：Harbor 优先使用 `HARBOR_BIN`、否则使用 `harbor`，每次调用最多 900 秒。超时不会替代 phase4 的正式验证。

Manifest event：`generated`。

### Phase 4: Harbor Oracle / Nop Check

Phase4 先验证 phase3 working task，然后运行：

```text
harbor run -p <task_path> -a oracle -o <jobs_dir> --job-name oracle -k 1 -y
harbor run -p <task_path> -a nop    -o <jobs_dir> --job-name nop    -k 1 -y
```

Harbor 先从 `HARBOR_BIN` 解析，再回退到 `PATH` 上的 `harbor`。

状态写入：

```text
runs/oracle-nop-check/<task_id>/oracle-nop-status.json
```

正式通过条件：

- oracle exit code 为 `0` 且 reward 为 `1.0`。
- nop exit code 为 `0` 且 reward 为 `0.0`。

phase runner 即使 reward 失败或 Harbor 外层超时也会记录 status。status 与 manifest 会记录被检查任务树的摘要和 run id；任务被修改后旧检查结果会失效。pipeline 可以把格式正确但未通过的 status 交给 review 生成修复指令。

Manifest event：`checked`。

### Phase 5: Task Review

输入：

- Working task。
- 可 review 的 phase4 oracle/nop status。
- `prompts/task-review.md`。

输出：

- `runs/reviews/<task_id>/review.json`。
- `runs/reviews/<task_id>/review.md`。

`review.json` 必须且只能包含：

```text
task_id
decision
summary
modification_items
blocking_reasons
```

允许的 decision 是 `ready`、`needs_modification`、`rejected`。

- `ready`：无 modification items 和 blocking reasons。
- `needs_modification`：`modification_items` 非空，无 blocking reasons。
- `rejected`：`blocking_reasons` 非空，无 modification items。

`review.md` 必须和 `review.json` 保持相同 decision，并包含非空的 Summary、Modification Items 与 Blocking Reasons 章节。

Manifest event：`reviewed`。

### Phase 6: Task Repair

只有当 phase5 验证通过且最新 review decision 为 `needs_modification` 时，phase6 才能运行。

输入：

- Working task。
- Review directory。
- 可选 oracle/nop directory，会复制到 Claude workspace。
- `prompts/task-repair.md`。

Claude 必须把 `output/task` 同步回 `generated/working/<seed_id>/<idea_id>`。验证随后复用 phase3 task validation，并检查新的 Claude session 确实同步了 repaired task。

与 phase3 相同，修复提示词中的每次 best-effort Harbor 检查最多运行 900 秒，phase4 仍是正式验证。

Manifest event：`repaired`。

### Phase 7: Finalize / Organize

Phase7 要求：

- phase5 validation 通过。
- review decision 为 `ready` 或 `rejected`。
- `ready` 必须满足 phase4 formal pass condition。
- `rejected` 只要求 phase4 status 格式正确且可 review，可以未通过正式 gate。

当 decision 为 `ready`，working task 复制到：

```text
generated/accepted/<task_id>/
```

当 decision 为 `rejected`，working task 复制到：

```text
generated/rejected/<task_id>/
```

final task 会先复制并验证到同级 staging directory，再原子切换到目标；切换或验证失败时可恢复旧目标。通过验证的切换提交后，phase7 将 manifest append 作为独立的不可逆 commit point；若 append 中断，会保留有效 final destination，重跑 phase7 可补齐事件。`runs/finalization-transactions/` 也用于补完 backup 清理或回滚尚未完成的切换。

存在待恢复的 finalization journal 时，phase7 `--dry-run` 会完整验证 journal，并报告真实运行将提交还是回滚；它不会 rename 或删除路径、删除 journal、fsync directory，也不会追加 manifest event。

Manifest event：`accepted` 或 `rejected`。

## 9. Manifest

`runs/task-manifest.jsonl` 是 append-only。每个 phase 成功运行后追加一个事件，随后 validation 检查是否存在匹配事件。

| Event | 写入者 | 必需引用 |
| --- | --- | --- |
| `brainstormed` | phase1 | `brainstorm_ref`, `claude_session_ref` |
| `skillnet_done` | phase2 | `brainstorm_ref`, `skillnet_ref`, `claude_session_ref` |
| `generated` | phase3 | `task_path`, `brainstorm_ref`, `skillnet_ref`, `skill_summary_ref`, `claude_session_ref` |
| `checked` | phase4 | `task_path`, `oracle_nop_ref`, `passed`, `run_id`, `task_tree_sha256` |
| `reviewed` | phase5 | `review_ref`, `review_markdown_ref`, `oracle_nop_ref`, `decision`, `claude_session_ref`, `phase4_run_id`, `task_tree_sha256` |
| `repaired` | phase6 | `task_path`, `review_ref`, `oracle_nop_ref`, `claude_session_ref` |
| `accepted` | phase7 | `task_path`, `source_task_ref`, `review_ref`, `oracle_nop_ref`, `run_id`, `task_tree_sha256` |
| `rejected` | phase7 | `task_path`, `source_task_ref`, `review_ref`, `oracle_nop_ref`, `run_id`, `task_tree_sha256` |

Manifest validation 用来证明 phase lineage，但不去重旧事件。validators 只要求存在至少一个匹配且有效的事件。

## 10. Cleanup 和 Git Hygiene

清理中间产物：

```bash
scripts/clean-intermediate.sh
scripts/clean-intermediate.sh --apply
scripts/clean-intermediate.sh --apply --drop-manifest
scripts/clean-intermediate.sh --apply --discard-transactions
```

不带 `--apply` 时只列出目标。带 `--apply` 时会先确认没有活跃 pipeline/phase 持有 activity lock，且没有遗留的 Claude session marker，然后删除：

- `runs/prompts`
- `runs/brainstorm`
- `runs/skillnet`
- `runs/oracle-nop-check`
- `runs/reviews`
- `runs/workspace`
- `runs/output-sync-transactions`
- `runs/finalization-transactions`
- `runs/claude-sessions`
- `src/`、`scripts/`、`tests/` 下的 Python `__pycache__`

删除后会恢复 `runs/` 骨架目录和 `.gitkeep` 文件。
append-only 的 `runs/task-manifest.jsonl` 默认保留，只有显式传入 `--drop-manifest` 才会删除。`--force-active` 可在人工恢复时绕过活跃运行保护，但可能破坏正在执行的 run。存在 output-sync 或 finalization 待恢复日志时，清理会拒绝执行，以保留 crash recovery 能力；`--discard-transactions` 是显式的破坏性覆盖选项。

无论执行 dry-run 列表还是实际删除，清理都要求 project root 以及存在的 `runs/`、`src/`、`scripts/`、`tests/` container 是真实目录；目标若经过 symlink 或非目录 ancestor 会被拒绝，查找 Python cache 时也不会跟随 directory symlink。若 cleanup target 自身是 symlink，则只 unlink 该链接，外部目标保持不变。

当前 ignore 规则会让本地 credentials、运行产物、generated task outputs、Python caches 和本地 Claude binary 不进入 git。`model.json` 仍保留预期的本地 Claude binary 路径。

## 11. 开发检查

改动代码或描述行为的文档后运行：

```bash
python3 -B -m compileall -q src tests
python3 -B -m unittest discover -s tests -v
bash -n scripts/*.sh
```

针对具体行为使用 validation 命令：

```bash
scripts/taskgen.sh validate phase1 <seed_id> --json
scripts/taskgen.sh validate phase3 <seed_id> --idea-id <idea_id> --json
scripts/taskgen.sh validate phase7 <seed_id> --idea-id <idea_id> --json
```

记录新行为前，先确认对应源码：

- CLI 和 pipeline：`src/taskgen/cli.py`。
- Phase run 和 validation：`src/taskgen/phases/`。
- Claude workspace：`src/taskgen/claude/`。
- Harbor check：`src/taskgen/harbor/oracle_nop.py`。
- Cleanup：`src/taskgen/maintenance/clean_intermediate.py`。
