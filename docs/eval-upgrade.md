# Eval 升级：轨迹级评估（Trajectory-Level Evaluation）

> ADR — Architecture Decision Record
> 状态：已实施 ✅ | 日期：2026-07-22 | 对齐 `vllm-optimization.md` 9 章节范本

---

## 1. 目标与约束

### 目标

把 `evals/` 从"答案级 LLM-as-Judge 雏形"升级为**轨迹级评估**体系：
不只看最终答案，还要评"工具选得对不对、参数准不准、有没有无意义循环、预算内 token/延迟"。

实现 **golden dataset + rubric + LLM-as-Judge + regression gate** 四层闭环，
让简历能讲"我用什么指标证明 agent 变好了"。

### 约束

- 零破坏答案级 eval（`Evaluator` 类不修改）
- CI 零 LLM 成本（回归 gate 全 mock）
- golden dataset 单一真源（`evals/golden/tasks.py`，不重复数据）
- 回滚零影响（轨迹功能全部在新增文件 + 子目录，删除即回滚）

---

## 2. 技术决策

### 决策 A — 轨迹 rubric 放 `evals/metrics/prompts/trajectory/` 子目录

`evals/metrics/__init__.py` 用 `os.listdir`（非递归）只扫顶层 `.md`，
子目录不污染答案级 loader。轨迹 rubric 由独立的 `load_trajectory_metrics()` 加载。

**验证：** answer-level metrics = 5，trajectory metrics = 4，互不干扰。

### 决策 B — 轨迹 rubric 输入 ≠ 答案级 Input/Output

答案级用 `(input, output)` 二元组；轨迹级用 `(requirement, trajectory)`，
其中 `trajectory` 是有序步骤序列（含 tool_call 名/参数/返回摘要）。
新增 `format_trajectory()`，不改动 `format_messages()`。

### 决策 C — `format_trajectory()` 放 `evals/trajectory_helpers.py`

保持 `helpers.py`（答案级）稳定，轨迹相关逻辑独立成模块：
`format_trajectory` / `get_trajectory_messages` / `estimate_tokens` /
`detect_redundant_loops` / `load_trajectory_metrics`。

### 决策 D — 回归 gate 用全新 `tests/test_eval_regression.py`，自包含 mock 基建

golden 任务工具集多样（write_file/edit_file/read_file/run_tests/mcp_*），
与现有 `test_coder_node_mcp_trajectory.py`（MCP 专用）mock 形状不同。
新文件自带 mock builder，不触碰现有通过的测试，零回归风险。

### 决策 E — ADR 文档 9 章节对齐 `vllm-optimization.md` 范本

并在 `docs/evaluation.md` 追加轨迹级章节（英文，与该文件风格一致）。

### 决策 F — golden 单一真源 `evals/golden/tasks.py`（Python dataclass 列表）

回归测试直接 import（无 DB 依赖）；`mcp_server/seed_db.py` 经
`sys.path` append import 它填充 sqlite `eval_golden` 表，不重复数据。
import 失败时 seed_db 降级为只填 `tasks` 表（eval_golden 留空），MCP server 仍可启动。

---

## 3. 配置变更

### `pyproject.toml` — 新增 2 个 pytest markers

```toml
markers = [
    # ... existing markers ...
    "regression: trajectory-level regression gate (CI-safe, mock LLM, no network)",
    "eval: LLM-as-Judge eval tests (opt-in, costs LLM tokens)",
]
```

### `Makefile` — 新增 2 个 targets

```makefile
test-regression:
	uv run pytest tests/test_eval_regression.py -v --tb=short -m "regression"

eval-trajectory:
	@$(call run_with_env,python -m evals.main --trajectory)
```

help 文本新增 Evaluation 段的 `eval-trajectory` 和独立 Testing 段（test / test-e2e / test-regression）。

### `mcp_server/seed_db.py` — 扩 eval_golden schema + import 单一真源

```sql
CREATE TABLE eval_golden (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type           TEXT,
    requirement         TEXT,
    expected_files      TEXT,
    expected_tools      TEXT,
    expected_trajectory TEXT,   -- JSON
    max_steps           INTEGER,
    max_tokens          INTEGER
);
```

填充逻辑从硬编码改为 `from evals.golden.tasks import GOLDEN_TASKS`，带 `try/except ImportError` 降级。

### `mcp_server/sqlite_tools.py` — `query_eval_golden` SELECT 加列

```python
"SELECT id, task_type, requirement, expected_files, expected_tools, "
"expected_trajectory, max_steps, max_tokens "
"FROM eval_golden ..."
```

### `.github/workflows/ci.yaml` — test job 注释补充

test job 已跑 `-m "not slow"`，regression 标记 ≠ slow → **自动纳入 CI**，无需改命令。
仅补充注释说明含 trajectory regression gate。

---

## 4. 验证结果

| 验证项 | 命令 | 结果 |
| --- | --- | --- |
| 回归 gate | `uv run pytest tests/test_eval_regression.py -m "regression"` | **34 passed** (4.52s) |
| Ruff lint | `uv run ruff check evals/ tests/test_eval_regression.py` | **All checks passed** |
| Ruff format | `uv run ruff format --check evals/ tests/test_eval_regression.py` | **11 files already formatted** |
| Pyright | `uv run pyright evals/` | **0 errors, 0 warnings** |
| 子目录隔离 | `from evals.metrics import metrics; from evals.trajectory_helpers import load_trajectory_metrics` | answer=5, traj=4 |
| seed_db | `python -m mcp_server.seed_db` → `SELECT count(*) FROM eval_golden` | **8 rows** |
| 全量测试 | `uv run pytest tests/ -m "not slow"` | 255+34 passed, 10 pre-existing failures (无关) |
| 导入验证 | `from evals.evaluator import TrajectoryEvaluator` | OK |

### 回归 gate 覆盖的 8 条 golden 任务

| task_type | expected_tools | max_steps | max_tokens |
| --- | --- | --- | --- |
| landing_page | write_file, edit_file | 6 | 4000 |
| script | write_file, run_tests | 4 | 3000 |
| bug_fix | read_file, edit_file, run_tests | 6 | 5000 |
| refactor | read_file, edit_file, run_tests | 6 | 5000 |
| test_addition | read_file, write_file, run_tests | 5 | 4000 |
| config_update | read_file, edit_file | 4 | 2500 |
| api_endpoint | read_file, edit_file, run_tests | 6 | 5000 |
| mcp_lookup | mcp_query_tasks, read_file, edit_file | 5 | 4000 |

### 回归 gate 断言维度（6 个测试函数，34 个断言）

1. `test_golden_tasks_complete` — 8 条都有 required 字段
2. `test_max_steps_respected[×8]` — trajectory 长度 ≤ max_steps
3. `test_no_redundant_loops[×8]` — 无连续相同 (tool, args) 重复
4. `test_max_tokens_budget[×8]` — token 估算 ≤ max_tokens
5. `test_expected_tools_are_known` — 所有工具名合法（已注册或 mcp_* 前缀）
6. `test_coder_trajectory_matches_expected[×8]` — mock LLM → coder_node → ToolMessage 序列匹配

---

## 5. 已知限制

1. **`query_eval_golden` 暴露 expected_trajectory 有"看答案"风险** —
   已在 `docs/mcp-integration.md` 记录，限 demo/introspection 场景使用。

2. **mock LLM 只能验证工具调用序列匹配，不能评真实工具选择质量** —
   回归 gate 验证的是"agent 不会漂移到错误的工具调用模式"，
   真实工具选择质量由离线 LLM-as-Judge（`TrajectoryEvaluator`）覆盖。

3. **`estimate_tokens` 是粗估（len(text)//4）** —
   非精确 tokenizer，用于预算断言的快速近似。
   后续可替换为 tiktoken 精确计数（但增加依赖）。

4. **TrajectoryEvaluator 需要真实 Langfuse trace** —
   离线 LLM-as-Judge 只在有真实 agent 运行轨迹时才有意义。
   无 trace 时 `_fetch_traces()` 返回空列表，不会报错。

---

## 6. 验收 Gate

```bash
# 1. CI 核心：回归 gate（零 LLM 成本）
make test-regression
# 期望：34 passed

# 2. Lint + Format
uv run ruff check evals/ tests/test_eval_regression.py
uv run ruff format --check evals/ tests/test_eval_regression.py
# 期望：All checks passed / already formatted

# 3. Typecheck
uv run pyright evals/
# 期望：0 errors

# 4. 离线 LLM-as-Judge（手动，需 Langfuse + OPENAI key）
make eval-trajectory
# 期望：fetch 真实 trace × 4 rubric → push score → JSON 报告
```

---

## 7. 回滚预案

轨迹功能全部在新增文件 + 子目录，删除即回滚，答案级 eval 零影响：

| 操作 | 文件 |
| --- | --- |
| 删除目录 | `evals/golden/`、`evals/metrics/prompts/trajectory/` |
| 删除文件 | `evals/trajectory_helpers.py`、`tests/test_eval_regression.py`、`docs/eval-upgrade.md` |
| 删除类 | `evals/evaluator.py` 中的 `TrajectoryEvaluator`（`Evaluator` 不受影响） |
| 删除分支 | `evals/main.py` 中的 `--trajectory` flag 和 `trajectory` 参数 |
| 回退 schema | `mcp_server/seed_db.py` 回退到硬编码 golden（或空） |
| 回退查询 | `mcp_server/sqlite_tools.py` 的 `query_eval_golden` SELECT 去掉 3 列 |
| 删除 markers | `pyproject.toml` 去掉 `regression` 和 `eval` markers |
| 删除 targets | `Makefile` 去掉 `test-regression` 和 `eval-trajectory` |

---

## 8. 后续路线

1. **真实 trace 标注** — 收集真实 agent 运行轨迹，人工标注工具选择质量，
   作为 LLM-as-Judge 的 ground truth 验证集。

2. **Rubric 自动调优** — 用标注数据自动优化 rubric prompt，
   提升 LLM-as-Judge 与人工标注的一致性（Cohen's Kappa）。

3. **预算 p95 而非硬上限** — `max_steps`/`max_tokens` 改为软阈值 + p95 统计，
   避免因单次超预算而硬失败。

4. **CI 可视化** — 在 GitHub Actions 中加入 regression gate 的历史趋势图，
   可视化 tool-selection 准确率随时间的变化。

5. **多 agent 轨迹** — 当前只评 coder_node 轨迹，后续扩展到 planner → supervisor → coder → reviewer 全链路。

---

## 9. 附录：简历叙事模板

```
工程化评估体系（Eval Engineering）：

设计了 golden dataset + rubric + LLM-as-Judge + CI regression gate 四层闭环，
从"会用框架"升级为"工程化评估"。

- Golden dataset：8 条多元场景（landing_page / bug_fix / refactor / mcp_lookup 等），
  覆盖 file tools + MCP tools 混合工具集
- 4 个轨迹级 rubric：tool_selection（工具选择）、tool_args_accuracy（参数准确）、
  no_redundant_loops（循环检测）、budget_adherence（预算遵守）
- CI regression gate：mock LLM 零成本跑 coder_node，34 个断言防止 agent 行为漂移
- 离线 LLM-as-Judge：TrajectoryEvaluator fetch 真实 Langfuse trace × 4 rubric，
  push score 到 Langfuse + 生成 JSON 报告

指标证明 agent 变好：
- tool-selection 准确率（回归 gate 通过率）
- loop 检出率（detect_redundant_loops 检出数趋近 0）
- 预算遵守率（steps ≤ max_steps 且 tokens ≤ max_tokens 的比例）
```

---

## 关键文件清单

**新增（11）：**

| 文件 | 说明 |
| --- | --- |
| `evals/golden/__init__.py` | 包标记 + 导出 |
| `evals/golden/tasks.py` | 单一真源，8 条 golden 任务 |
| `evals/golden/loader.py` | `load_golden_tasks()` / `get_golden_by_type()` |
| `evals/metrics/prompts/trajectory/tool_selection.md` | 工具选择 rubric |
| `evals/metrics/prompts/trajectory/tool_args_accuracy.md` | 参数准确 rubric |
| `evals/metrics/prompts/trajectory/no_redundant_loops.md` | 循环检测 rubric |
| `evals/metrics/prompts/trajectory/budget_adherence.md` | 预算遵守 rubric |
| `evals/trajectory_helpers.py` | 5 个轨迹 helper 函数 |
| `tests/test_eval_regression.py` | 6 测试函数 / 34 断言 |
| `docs/eval-upgrade.md` | 本 ADR |

**修改（8）：**

| 文件 | 变更 |
| --- | --- |
| `evals/evaluator.py` | + `TrajectoryEvaluator` 类（不动 `Evaluator`） |
| `evals/main.py` | + `--trajectory` flag |
| `mcp_server/seed_db.py` | 扩 schema + import 单一真源 |
| `mcp_server/sqlite_tools.py` | `query_eval_golden` SELECT +3 列 |
| `pyproject.toml` | + 2 markers |
| `Makefile` | + `test-regression` / `eval-trajectory` targets + help 文本 |
| `.github/workflows/ci.yaml` | test job 注释补充 |
| `docs/evaluation.md` | + 轨迹级章节 |
