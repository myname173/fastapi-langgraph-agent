# 前端 UI 设计优化方案

> 参考 Claude Code 桌面版（2026-04 重设计）与 OpenAI Codex 桌面版的交互范式，
> 针对当前 Streamlit UI 进行结构化优化。

---

## 一、现状分析

### 1.1 当前架构

| 维度 | 现状 |
|------|------|
| 框架 | Streamlit（Python Web 框架） |
| 布局 | `wide` 模式，左侧边栏 + 主内容区 |
| 导航 | 2 个 Tab：`SDLC Tasks` / `Media Studio` |
| 侧边栏 | 账户信息 → 设置 → 功能标志 → 任务历史 → 登出 |
| 主流程 | 任务输入 → 流式执行日志 → 结果展示（垂直堆叠） |
| 审批 | 两按钮 `Approve` / `Reject` |
| 媒体 | 图像/视频生成 + 历史记录（3 个子 Tab） |

### 1.2 核心问题

| 编号 | 问题 | 影响 |
|------|------|------|
| P-1 | **单列垂直堆叠** — 输入、日志、结果全部上下排列，长任务需要反复滚动 | 上下文丢失，认知负担高 |
| P-2 | **无实时 Diff 查看** — 代码变更只能看最终结果，看不到逐行修改过程 | 审查效率低，信任感不足 |
| P-3 | **任务历史交互薄弱** — 只能点击加载状态，无法对比、搜索、筛选 | 历史任务利用率低 |
| P-4 | **缺少加载/状态指示** — 网络请求期间无骨架屏或 spinner | 用户以为卡死 |
| P-5 | **审批信息不足** — 只有两个按钮，不展示变更摘要、影响范围 | 决策盲目 |
| P-6 | **无视图模式切换** — 所有日志细节全部展示，无法按需收起 | 信息过载 |
| P-7 | **无键盘快捷键** — 所有操作依赖鼠标 | 效率低 |
| P-8 | **流式日志可读性差** — 纯文本追加，无节点分区、无时间戳 | 调试困难 |
| P-9 | **媒体与 SDLC 割裂** — 两个 Tab 完全独立，媒体生成结果无法嵌入 SDLC | 工作流断裂 |

---

## 二、设计参考提炼

### 2.1 Claude Code 桌面版（2026-04 重设计）

来源：[Anthropic 官方博客](https://claude.com/blog/claude-code-desktop-redesign) + [社区评测](https://claudelab.net/en/articles/claude-code/claude-code-desktop-parallel-agents)

| 设计亮点 | 核心思想 | 可借鉴度 |
|----------|----------|----------|
| **多会话侧边栏** | 左侧按项目分组管理多个并行会话，状态一目了然 | ★★★★★ |
| **拖拽式分栏布局** | 终端、预览、Diff、聊天面板可自由拖拽排列 | ★★★☆☆（Streamlit 限制） |
| **集成终端** | 应用内终端，无需切换窗口 | ★★☆☆☆（架构改动大） |
| **快速 Diff 查看器** | 性能优化的差异查看，支持逐行批注 | ★★★★★ |
| **Side Chat** | 侧边聊天面板，不打断主流程 | ★★★★☆ |
| **三种视图模式** | Verbose / Normal / Summary 三档信息密度 | ★★★★★ |
| **实时预览** | HTML/应用预览嵌入主界面 | ★★★★☆ |
| **会话归档** | PR 合并后自动归档，侧边栏保持聚焦 | ★★★★☆ |

### 2.2 OpenAI Codex 桌面版

来源：[Codex 桌面应用全景](https://cloud.tencent.cn/developer/article/2700743) + [社区评测](https://prommer.net/en/tech/guides/codex-desktop-app-review/)

| 设计亮点 | 核心思想 | 可借鉴度 |
|----------|----------|----------|
| **三栏布局** | 左导航 → 中对话执行 → 右结果面板（Diff/终端/预览） | ★★★★★ |
| **项目+线程组织** | 左侧栏按项目分组线程，线程保留完整上下文 | ★★★★☆ |
| **可视化审阅** | 逐行批注、按块接受/回滚、直接提交 PR | ★★★★★ |
| **Actions 快捷按钮** | 常用操作一键触达 | ★★★★☆ |
| **线程状态徽章** | active/idle/done/error 彩色标识 | ★★★★★ |
| **分块 Diff 接受** | 不再全量接受/拒绝，可按代码块粒度选择 | ★★★★★ |

### 2.3 关键设计原则提炼

综合两者，提炼出适用于本项目的 **5 条设计原则**：

1. **分栏优于堆叠** — 左右分栏让"输入/执行"与"结果/审查"同屏可见
2. **状态优于猜测** — 每个任务、节点、步骤都有明确的视觉状态
3. **粒度优于全量** — Diff 审查支持按块接受，而非全量通过
4. **聚焦优于全量** — 视图模式让用户控制信息密度
5. **上下文优于切换** — 相关信息同屏展示，减少跳转

---

## 三、优化方案

### 3.1 总体架构：三栏布局

```
┌─────────────┬──────────────────────────────┬─────────────────┐
│             │                              │                 │
│  左侧栏      │       主工作区                │   右侧面板       │
│  (会话/任务) │   (输入 + 执行日志)           │  (结果/Diff)    │
│             │                              │                 │
│  280px      │       flex-1                 │   420px         │
│             │                              │  (可折叠)        │
│             │                              │                 │
└─────────────┴──────────────────────────────┴─────────────────┘
```

**Streamlit 实现方式**：使用 `st.columns([1, 3, 1.5])` 三列布局，配合 `st.container()` 实现分区。

### 3.2 左侧栏优化：会话管理器

#### 现状
```
账户信息
设置
功能标志
任务历史（列表）
登出
```

#### 优化后
```
┌─────────────────────────┐
│ 📁 项目分组              │  ← 按 project_context 分组
│   ├─ 🟢 task-abc (done) │  ← 状态徽章
│   ├─ 🔵 task-def (run)  │
│   └─ ⚪ task-ghi (idle) │
│                         │
│ 🔍 [搜索框]              │  ← 新增：任务搜索
│                         │
│ 🏷️ [状态筛选]            │  ← 新增：done/run/error 筛选
│                         │
│ ────────────────────── │
│ 👤 user@email.com       │
│ ⚙️ 设置 | 🚪 登出        │
└─────────────────────────┘
```

**关键改进**：
- 任务卡片化：每个任务显示 `id前8位 + 状态徽章 + 简短描述`
- 状态徽章：🟢 done / 🔵 running / 🔴 error / ⚪ idle / 🟡 pending_approval
- 搜索筛选：支持按状态、关键词筛选
- 当前任务高亮：正在查看的任务左侧加蓝色竖线

### 3.3 主工作区优化：分阶段进度 + 结构化日志

#### 现状
```
[任务输入表单]
────────────────
Execution Log
[进度条]
[节点状态]
[纯文本日志追加]
[工具调用容器]
```

#### 优化后
```
┌──────────────────────────────────────────┐
│ 📝 任务输入                               │  ← 可折叠区域
│ [需求描述] [项目上下文] [图片上传]        │
│ [Submit]                                 │
└──────────────────────────────────────────┘
┌──────────────────────────────────────────┐
│ 🔄 执行进度                               │
│                                          │
│  ✅ Planner  ──  ✅ Coder  ──  🔄 Tester │  ← 阶段时间线
│     0.8s          2.1s         进行中... │
│                                          │
│ [视图模式: Verbose | Normal | Summary]   │  ← 新增：信息密度切换
└──────────────────────────────────────────┘
┌──────────────────────────────────────────┐
│ 📋 执行日志                               │
│                                          │
│ [10:32:01] [planner] 分析需求...         │  ← 时间戳 + 节点名
│ [10:32:03] [planner] 生成计划: 3 步      │
│ [10:32:05] [coder]   写入文件: app.py    │
│ [10:32:08] [coder]   🔧 工具调用: edit   │  ← 工具调用高亮
│ [10:32:10] [tester]  运行测试...         │
│                                          │
│ [📋 复制日志]                             │
└──────────────────────────────────────────┘
```

**关键改进**：

1. **阶段时间线** — 替代单进度条，显示每个 SDLC 阶段的完成状态和耗时
2. **视图模式** — 三档信息密度：
   - `Verbose`：完整日志 + 工具调用 + 中间状态
   - `Normal`：节点切换 + 关键事件（默认）
   - `Summary`：只显示最终结果摘要
3. **结构化日志** — 每行带时间戳 + 节点名 + 级别图标
4. **工具调用卡片** — 不再混在日志里，独立卡片显示：工具名 + 参数 + 结果

### 3.4 右侧面板优化：结果展示 + 分块 Diff 审查

#### 现状
```
Results
├─ Plan（列表）
├─ Code Changes（代码块）
├─ Test Results
├─ Feedback
├─ Security Issues
└─ Final Messages
```

#### 优化后
```
┌─────────────────────────────────┐
│ 📊 结果面板                      │
│                                 │
│ [Plan] [Code] [Tests] [Review] │  ← 子 Tab 切换
│                                 │
│ ─── Code Changes ───           │
│                                 │
│ 📄 app/main.py                  │  ← 文件级分组
│ ┌─────────────────────────────┐│
│ │  - old line                 ││  ← Diff 视图
│ │  + new line                 ││
│ └─────────────────────────────┘│
│ [✅ 接受此块] [❌ 拒绝此块]     │  ← 分块接受
│                                 │
│ 📄 app/utils.py                 │
│ ┌─────────────────────────────┐│
│ │  + new function             ││
│ └─────────────────────────────┘│
│ [✅ 接受此块] [❌ 拒绝此块]     │
│                                 │
│ ─── 审批操作 ───               │
│ 变更摘要: 2 文件, +15 -3 行     │
│ [✅ 全部接受] [❌ 全部拒绝]     │
│ [💬 添加批注]                   │  ← 新增：批注
└─────────────────────────────────┘
```

**关键改进**：

1. **分块 Diff 审查** — 每个文件的变更独立展示，可单独接受/拒绝
2. **变更摘要** — 审批前显示文件数、增删行数、影响范围
3. **子 Tab 导航** — Plan/Code/Tests/Review 之间快速切换，不必滚动
4. **Diff 语法高亮** — `+` 绿色背景，`-` 红色背景，支持代码语法高亮

### 3.5 审批流程优化

#### 现状
```
⚠️ Human Approval Required
[Approve]  [Reject]
```

#### 优化后
```
┌──────────────────────────────────────────┐
│ ⚠️ 需要人工审批                           │
│                                          │
│ 📋 变更摘要                               │
│ ├─ 修改文件: 2 个                         │
│ ├─ 新增代码: 15 行                        │
│ ├─ 删除代码: 3 行                         │
│ ├─ 安全检查: ✅ 通过                      │
│ └─ 测试结果: 5/5 通过                     │
│                                          │
│ 📄 变更预览                               │
│ [查看 Diff 详情 →]                        │
│                                          │
│ 💬 审批意见（可选）                        │
│ [文本输入框]                              │
│                                          │
│ [✅ 批准并继续]  [❌ 拒绝并回退]           │
└──────────────────────────────────────────┘
```

**关键改进**：
- 审批前展示完整变更摘要（文件数、行数、安全检查、测试结果）
- 支持填写审批意见（传递给 reviewer 节点）
- 拒绝时支持选择回退方式（修改/重新规划）

### 3.6 媒体工作室优化

#### 现状
3 个独立子 Tab（图像/视频/历史），与 SDLC 完全隔离。

#### 优化后
```
┌──────────────────────────────────────────┐
│ 🎨 Media Studio                           │
│                                          │
│ [生成图像] [生成视频] [历史记录]          │
│                                          │
│ ─── 生成图像 ───                          │
│ 提示词: [________________]                │
│ 尺寸:   [1024x1024 ▼]                    │
│ 数量:   [1] [2] [4]                      │
│ [✨ 生成]                                 │
│                                          │
│ ─── 结果预览 ───                          │
│ ┌────────┐ ┌────────┐                   │
│ │  img1  │ │  img2  │                   │
│ └────────┘ └────────┘                   │
│ [📥 下载] [📎 插入到 SDLC 任务]           │  ← 新增：跨 Tab 联动
│                                          │
│ ─── 配额 ───                              │
│ 今日已用: 3/10 | 剩余: 7                 │  ← 新增：配额展示
└──────────────────────────────────────────┘
```

**关键改进**：
- 生成结果可"插入到 SDLC 任务"（跨 Tab 联动）
- 配额展示（当日已用/剩余）
- 结果卡片化（悬停放大、一键下载）

---

## 四、视觉设计规范

### 4.1 色彩系统

| 用途 | 颜色 | Streamlit 主题参数 |
|------|------|-------------------|
| 主色（Primary） | `#0066FF` | `primaryColor` |
| 背景色 | `#FFFFFF` | `backgroundColor` |
| 次要背景 | `#F8F9FA` | `secondaryBackgroundColor` |
| 文字主色 | `#1A1A1A` | `textColor` |
| 成功 | `#28A745` | — |
| 警告 | `#FFC107` | — |
| 错误 | `#DC3545` | — |
| 信息 | `#17A2B8` | — |

### 4.2 间距规范

| 用途 | 值 |
|------|-----|
| 组件内间距 | `8px` |
| 组件间间距 | `16px` |
| 区域间间距 | `24px` |
| 卡片内边距 | `16px` |
| 卡片圆角 | `8px` |

### 4.3 状态徽章规范

| 状态 | 图标 | 颜色 | 含义 |
|------|------|------|------|
| done | ✅ | 绿色 | 任务完成 |
| running | 🔄 | 蓝色 | 正在执行 |
| error | ❌ | 红色 | 执行出错 |
| idle | ⚪ | 灰色 | 空闲/未开始 |
| pending_approval | 🟡 | 黄色 | 等待审批 |
| cancelled | ⛔ | 深灰 | 已取消 |

---

## 五、实施计划

### 阶段一：布局重构（低风险，高收益）

**目标**：从单列堆叠改为三栏布局

**改动范围**：
- `ui/app.py` — `main()` 函数布局重构
- 新增 `ui/components/sidebar.py` — 会话管理器组件
- 新增 `ui/components/workspace.py` — 主工作区组件
- 新增 `ui/components/result_panel.py` — 右侧结果面板

**关键实现**：
```python
# main() 布局骨架
col_sidebar, col_main, col_result = st.columns([1, 3, 1.5])

with col_sidebar:
    render_session_manager()  # 新：会话管理器

with col_main:
    render_task_input()       # 可折叠
    render_execution_timeline()  # 新：阶段时间线
    render_structured_log()   # 新：结构化日志

with col_result:
    render_result_panel()     # 新：分 Tab 结果展示
```

**预估改动**：~300 行新增，~150 行重构

### 阶段二：Diff 审查增强（中风险，高收益）

**目标**：实现分块 Diff 查看和按文件接受/拒绝

**改动范围**：
- `ui/components/diff_viewer.py`（新增）— Diff 渲染组件
- `ui/api_client.py` — 新增 `approve_partial()` 方法
- `app/api/v1/sdlc.py` — 新增部分审批端点

**关键实现**：
```python
# Diff 渲染
def render_diff_viewer(code_changes: list[dict]):
    """按文件分组渲染 Diff，支持分块接受。"""
    for change in code_changes:
        filename = change.get("file", "unknown")
        content = change.get("content", "")
        with st.container(border=True):
            st.markdown(f"📄 **{filename}**")
            render_diff_content(content)  # 语法高亮 Diff
            col1, col2 = st.columns(2)
            with col1:
                if st.button(f"✅ 接受", key=f"accept_{filename}"):
                    st.session_state[f"accepted_{filename}"] = True
            with col2:
                if st.button(f"❌ 拒绝", key=f"reject_{filename}"):
                    st.session_state[f"accepted_{filename}"] = False
```

### 阶段三：视图模式 + 结构化日志（低风险，中收益）

**目标**：三档信息密度 + 带时间戳的结构化日志

**改动范围**：
- `ui/app.py` — `render_streaming_output()` 重构
- 新增 `ui/components/log_viewer.py` — 结构化日志组件

**关键实现**：
```python
# 视图模式
view_mode = st.radio("视图模式", ["Verbose", "Normal", "Summary"],
                     horizontal=True, key="view_mode")

# 结构化日志
log_entries = []  # [{timestamp, node, level, message}]

def append_log(node: str, message: str, level: str = "info"):
    entry = {
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "node": node,
        "level": level,
        "message": message,
    }
    log_entries.append(entry)
    render_log_entry(entry, view_mode)
```

### 阶段四：会话管理增强（中风险，中收益）

**目标**：任务搜索、筛选、状态徽章

**改动范围**：
- `ui/components/sidebar.py` — 增强
- `ui/api_client.py` — 可能需要分页支持

### 阶段五：审批流程增强（低风险，中收益）

**目标**：变更摘要 + 审批意见输入

**改动范围**：
- `ui/app.py` — 审批区域重构
- `ui/api_client.py` — `approve_task()` 支持注释参数

---

## 六、技术约束与风险

### 6.1 Streamlit 框架限制

| 限制 | 影响 | 缓解方案 |
|------|------|----------|
| 无法真正拖拽分栏 | 三栏布局是固定比例 | 用 `st.columns()` 固定比例，配合可折叠容器 |
| 重渲染机制 | 每次交互全页刷新 | 用 `st.session_state` + `st.fragment`（Streamlit 1.33+）局部刷新 |
| 无法自定义 CSS 组件 | 状态徽章等需 hack | 用 emoji + `st.markdown()` 模拟，或注入自定义 CSS |
| 无原生 Diff 组件 | 需自己实现 | 用 `st.code()` + 文本 Diff 库（如 `difflib`）渲染 |

### 6.2 风险评估

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| 三栏布局在窄屏下挤压 | 高 | 中 | 响应式：窄屏自动折叠右侧面板 |
| 分块 Diff 增加后端复杂度 | 中 | 高 | 先做前端展示，后端部分审批作为 P2 |
| 结构化日志改变现有流式逻辑 | 中 | 中 | 保留 `log_text` 兼容，新增结构化层 |
| 会话管理器增加 API 调用 | 低 | 低 | TTL 缓存（已有模式） |

### 6.3 不建议实施项

以下 Claude Code/Codex 特性在 Streamlit 中**不建议实施**（成本过高）：

- ❌ 拖拽式自由分栏 — Streamlit 无原生支持
- ❌ 集成终端 — 架构改动过大，需 WebSocket 改造
- ❌ 应用内文件编辑器 — 与项目定位不符
- ❌ Side Chat — 需要并行会话支持，当前架构为单会话

---

## 七、预期效果

### 7.1 用户体验提升

| 指标 | 现状 | 优化后 |
|------|------|--------|
| 任务审查所需滚动次数 | ~8-10 次 | ~2-3 次（分栏同屏） |
| 审批决策信息完整度 | 20%（只有按钮） | 90%（摘要+Diff+测试） |
| 日志定位效率 | 纯文本搜索 | 时间戳+节点筛选 |
| 历史任务查找 | 线性浏览 | 搜索+筛选 |

### 7.2 信息密度可控

| 视图模式 | 适用场景 | 展示内容 |
|----------|----------|----------|
| Verbose | 调试/开发 | 完整日志 + 工具调用 + 中间状态 |
| Normal | 日常使用 | 节点切换 + 关键事件 + 最终结果 |
| Summary | 快速审查 | 仅结果摘要 + 变更统计 |

---

## 八、附录

### 8.1 文件结构规划

```
ui/
├── app.py                      # 主入口（精简）
├── api_client.py               # API 客户端（增强）
├── config.py                   # 配置（增强）
├── components/                 # 新增：UI 组件库
│   ├── __init__.py
│   ├── sidebar.py              # 会话管理器
│   ├── workspace.py            # 主工作区
│   ├── result_panel.py         # 结果面板
│   ├── diff_viewer.py          # Diff 查看器
│   ├── log_viewer.py           # 结构化日志
│   ├── timeline.py             # 阶段时间线
│   └── approval.py             # 审批组件
└── styles/
    └── theme.css               # 自定义主题
```

### 8.2 Streamlit 主题配置

```toml
# .streamlit/config.toml
[theme]
primaryColor = "#0066FF"
backgroundColor = "#FFFFFF"
secondaryBackgroundColor = "#F8F9FA"
textColor = "#1A1A1A"
font = "sans serif"
```

### 8.3 参考来源

- [Claude Code 桌面端重设计](https://claude.com/blog/claude-code-desktop-redesign) — Anthropic 官方
- [Claude Code Desktop Overhaul](https://claudelab.net/en/articles/claude-code/claude-code-desktop-parallel-agents) — 社区评测
- [Codex 桌面应用全景](https://cloud.tencent.cn/developer/article/2700743) — 中文社区
- [Codex Desktop App Review](https://prommer.net/en/tech/guides/codex-desktop-app-review/) — 设计批评
