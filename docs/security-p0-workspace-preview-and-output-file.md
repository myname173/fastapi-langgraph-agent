# ADR: P0 安全工作 — 工作区预览鉴权 + 输出文件扩展名/大小校验

> 文档日期: 2026-07-23
> 文档人: AI Agent (P0 安全修复执行)
> 范围: 工作区产物预览端点鉴权、LangGraph `save_output_file` 工具输入校验
> 状态: ✅ 完成并通过验收 Gate

---

## 1. 目标与约束

| 维度 | 目标 | 实际 |
|------|------|------|
| 工作区预览信息泄露 | 任意知道 `thread_id` 的人不能读他人产物 | ✅ 改为带 JWT 鉴权路由 |
| 输出文件扩展名绕过 | agent 不能写出白名单外（如 `.exe`）的文件 | ✅ `save_output_file` 加扩展名 + 大小校验 |
| 改动面 | 仅收敛安全缺口，不破坏现有 UI 预览链路 | ✅ Streamlit `?token=` 双通道保留 |
| 测试 | 两处修复均有回归用例覆盖 | ✅ 10 个用例全绿 |

---

## 2. 技术决策

### 决策 A — 工作区预览由无鉴权 `StaticFiles` 挂载改为带鉴权路由

**原问题**：`app/main.py` 曾用 `app.mount("/workspace", StaticFiles(directory=...))`
直接暴露产物目录。任何拿到 `thread_id` 的请求都能遍历读取其他用户的
HTML/图片/产物，构成信息泄露（info-leak）。

**方案**：移除 `StaticFiles` 挂载，改为显式路由
`@app.get(f"{settings.WORKSPACE_PREVIEW_URL_PREFIX}/{{path:path}}")`
→ `serve_workspace_file`，每次请求强制校验：

1. `ENABLE_WORKSPACE_PREVIEW` 关闭时直接 `404`，不触碰文件系统；
2. token 从 `?token=` 查询参数或 `Authorization: Bearer` 头取其一；缺失 → `401`；
3. `verify_token` 按 `expected_type="session"` 再 `"user"` 两次校验，
   畸形 token 抛 `ValueError` 被捕获为 `401`（而非 500）；
4. 身份归属校验：
   - session token → `sub`（session id）必须等于 `thread_id`；
   - user token → 通过 `database_service.get_session(thread_id)` 确认该用户拥有此会话；
   不匹配 → `403`；
5. `get_workspace_path(thread_id, relative)` 做路径穿越（traversal）防护；
6. `is_allowed_extension(abs_path)` 二次滤网，禁止白名单外类型 → `403`。

> UI 侧（`ui/components/preview.py`）通过 `?token=` 透传 `session_token`，
> 浏览器 iframe / img / video 无法带 header，故保留查询参数通道；
> 程序化调用仍可走 `Authorization` 头。

### 决策 B — `save_output_file` 补齐扩展名 + 大小校验

**原问题**：`save_output_file`（LangGraph 工具，agent 用来落盘产物）此前
**未**走 `is_allowed_extension` / 大小上限，与同模块的 `write_file` /
`download_asset` 不一致，可被用来写出 `.exe`、超大文件等。

**方案**：在 `write_text` 之前插入两道校验，被拦截时返回
`{"file_path": "", "success": False, "error": ...}` **且不落盘**：

- 扩展名：`is_allowed_extension(file_path)` 失败 → 返回失败结果；
- 大小：`len(content.encode("utf-8")) > settings.SDLC_MAX_FILE_SIZE` → 返回失败结果。

至此 `save_output_file` 与 `write_file` / `download_asset` 的校验口径完全对齐。

---

## 3. 配置变更

本次为**纯代码行为修复**，未新增环境变量。涉及的既有安全配置项：

| 配置键 | 默认值 | 作用 |
|--------|--------|------|
| `ENABLE_WORKSPACE_PREVIEW` | `true` | 关闭则预览端点整体 `404` |
| `WORKSPACE_PREVIEW_URL_PREFIX` | `/workspace` | 预览路由前缀 |
| `SDLC_MAX_FILE_SIZE` | `10485760`（10 MB） | 单文件内容字节上限 |
| `SDLC_ALLOWED_EXTENSIONS` | 代码/文本/图片等白名单 | 允许写出的扩展名集合 |

---

## 4. 验证结果

| 测试文件 | 用例 | 覆盖 |
|----------|------|------|
| `tests/test_workspace_preview.py` | 8 个 | 无 token→401、畸形 token→401、session 拥有者命中、非拥有者→403、缺失文件→404、扩展名→403、user token 拥有者命中、非拥有者→403 |
| `tests/test_sdlc_tools.py` | 2 个 | `test_save_file_rejects_disallowed_extension`（拒绝 `.exe` 且不落盘）、`test_save_file_rejects_oversized_content`（`SDLC_MAX_FILE_SIZE` 小化 mock） |

结果：全部通过（EXIT=0），相关文件 lint 0 错误。

---

## 5. 已知限制

- user-token 路径每次需查库确认会话归属（`database_service.get_session`），
  高并发预览下有一次 DB 往返；当前预览为低频操作，可接受。
- `?token=` 出现在 URL 中，可能被访问日志/代理记录；因 iframe/img 无法带
  header 而保留，属可接受权衡。如需更强保护可后续引入短期一次性预览 token。
- 安全边界仍依赖 `SDLC_ALLOWED_EXTENSIONS` 白名单完整性；新增产物类型需同步维护。

---

## 6. 验收 Gate

```bash
# 1. 安全回归用例
.venv/bin/python -m pytest tests/test_workspace_preview.py tests/test_sdlc_tools.py -q
# 期望：全部 passed

# 2. Lint
pre-commit run --all-files
# 期望：All checks passed
```

---

## 7. 回滚预案

两处修复均为独立函数/路由，回滚即 `git revert` 对应提交，或：

- P0-1：将 `serve_workspace_file` 路由换回 `app.mount("/workspace", StaticFiles(...))`
  （**不推荐**，会重新引入信息泄露）；
- P0-2：移除 `save_output_file` 内两道校验（**不推荐**，会重新引入扩展名绕过）。

因修复无数据迁移、无配置新增，回滚风险为零。

---

## 8. 后续路线

- 引入短期一次性预览 token，避免 `?token=` 长期有效被日志留存；
- 预览响应补 `Content-Security-Policy` / `X-Content-Type-Options` 头，降低
  被预览 HTML 内联脚本攻击的风险；
- 将 `sdlc_security_issues_total` 指标接入拦截点，便于观测异常写入尝试。

---

## 9. 关键文件清单

- `app/main.py` — `serve_workspace_file` 路由（P0-1）
- `app/core/langgraph/tools/sdlc_tools.py` — `save_output_file` / `is_allowed_extension` / `get_workspace_path`（P0-2）
- `app/core/config.py` — `ENABLE_WORKSPACE_PREVIEW` / `WORKSPACE_PREVIEW_URL_PREFIX` / `SDLC_MAX_FILE_SIZE` / `SDLC_ALLOWED_EXTENSIONS`
- `ui/components/preview.py` — 预览 URL 透传 `session_token`
- `tests/test_workspace_preview.py` — P0-1 回归用例
- `tests/test_sdlc_tools.py` — P0-2 回归用例
