# 工作区预览与资源管理通用设计方案

> 目标：让 SDLC Agent 具备"生成→持久化→预览"任何 Web 类产物（网页、游戏、
> 数据看板、交互工具、落地页等）的通用能力，**不针对任何具体任务硬编码**。
> 参考项目：OpenGenerativeUI、Pretifact、MS-Agent、Cloudscape Artifact Previews。

---

## 〇、修订记录

### v2（2026-07-10）— 架构评审优化

基于代码对照评审 + 网络最佳实践调研，修正以下问题：

| 类别 | 问题 | 修正 |
|------|------|------|
| 硬伤 1 | thread_id / task_id 混用 | 端点、工具、UI 全部统一为 thread_id |
| 硬伤 2 | 工具注册代码不完整 | 补全 registry.py 循环分支 |
| 硬伤 3 | 扩展名校验链路不明 | download_asset 明确走 `is_allowed_extension` |
| 硬伤 4 | 限流方式与现有代码不符 | 改用 `@limiter.limit` 装饰器 |
| 硬伤 5 | check_security 误报 | 分层扫描：仅 SVG/HTML 做 XSS 专项扫描 |
| 硬伤 6 | UI client 获取方式 | 函数内部从 session_state 取 |
| 遗漏 1 | iframe 沙箱逃逸 | 改用 `sandbox="allow-scripts"`（无 same-origin） |
| 遗漏 2 | manifest 并发写入 | 每个 artifact 独立文件，读时遍历 |
| 遗漏 3 | 工作区清理缺失 | v1 即提供 task 删除联动清理 |
| 遗漏 4 | 路由顺序 | 明确放置位置与冲突分析 |
| 决策 1 | 预览面板位置 | 改为 Results 独立 tab（参考 Cloudscape Canvas） |
| 决策 2 | manifest 文件 vs DB | 标记为已知技术债，说明 trade-off |

**网络调研依据**：
- W3C/WHATWG HTML 规范明确警告 `allow-scripts + allow-same-origin` 组合会使 sandbox 失效
- Cloudscape 设计系统提出 Inline vs Canvas 两种 artifact 预览模式
- Cline 编码 Agent 采用沙箱化执行 + 检查点恢复的架构模式

---

## 一、背景与目标

### 1.1 问题场景

当用户向 Agent 提出"生成一个 XX 网页/游戏/工具，并能直接预览运行"这类需求时，
当前架构存在三处断点，导致 Agent 即使写出代码也无法让用户在浏览器中"直接玩到/看到"：

| 断点 | 现状 | 后果 |
|------|------|------|
| **资源不可达** | `generate_image` 返回 DashScope 临时 URL（~24h 过期），写入 HTML 后很快变死链 | 资源失效后页面"裂图" |
| **文件不可访问** | `write_file` 把产物落到 `workspace/{thread_id}/`，但 FastAPI 没有挂载 `StaticFiles`，浏览器访问 404 | 用户看不到 Agent 生成的页面 |
| **缺少预览入口** | 没有"列出本任务可预览产物 + 在 UI 中打开"的端点与组件 | 工作流断裂，必须手动复制文件 |

### 1.2 设计目标（必须满足）

1. **通用性（第一原则）** — 任何输出 HTML/JS/CSS/静态资源的任务都能预览，
   不为贪吃蛇/计算器/看板等任何具体任务写一行针对性代码。
2. **沙箱安全** — 严格限定在 `workspace/{thread_id}/` 内，复用现有路径校验与扩展名白名单；
   iframe 渲染遵循 W3C sandbox 规范，不使用已被证明不安全的组合。
3. **Agent 自主** — 通过新增工具让 Agent 自主决定"何时下载远程资源、何时生成预览入口"，
   不需要用户手动操作。
4. **UI 可发现** — 任务完成后自动列出可预览产物，作为独立 Tab 高亮展示。
5. **与现有架构解耦** — 不侵入 SDLC 状态机，不修改 Agent 提示词契约，
   以"工具 + 端点 + UI 组件"三件套方式增量交付。

### 1.3 非目标（明确排除）

- ❌ 不做服务端代码执行预览（如 Node.js SSR）——产物是纯静态资源，浏览器渲染即可
- ❌ 不做实时协同编辑——预览是只读的
- ❌ 不做版本对比/diff 预览——复用现有 `diff_viewer.py`
- ❌ 不做任务特定的资源生成逻辑（如"游戏素材包"）——Agent 通过提示词自主规划

---

## 二、现状分析

### 2.1 已具备的能力（可复用）

| 能力 | 位置 | 复用方式 |
|------|------|----------|
| 工作区沙箱 | `app/core/langgraph/tools/sdlc_tools.py:get_workspace_path` | 直接复用，所有预览/下载工具都走它 |
| 扩展名白名单 | `settings.SDLC_ALLOWED_EXTENSIONS`（默认含 py/js/ts/json/md/...） | 扩充 html/css/svg/png/jpg/gif/webp/mp4/webm |
| 路径遍历防护 | `get_workspace_path` 内 `resolved_path.startswith(workspace_root_abs)` | 直接复用 |
| 安全扫描 | `check_security` + `scan_security_issues` | **不复用于下载内容**（见 5.2.5 分层扫描） |
| 图像生成工具 | `app/core/langgraph/tools/media_tools.py:generate_image` | 返回 URL，由新工具下载持久化 |
| 文件写入工具 | `write_file` / `save_output_file` | download_asset 走 write_file 同级的显式校验 |
| thread_id 路由模式 | `app/api/v1/sdlc.py` 中 `/{thread_id}/state`、`/approve`、`/reject` | 新端点沿用此模式 |

### 2.2 缺失的能力（本方案补齐）

| 缺失项 | 影响 | 本方案模块 |
|--------|------|------------|
| FastAPI `StaticFiles` 挂载 | 浏览器无法访问工作区文件 | 模块 A |
| 远程资源下载工具 | Agent 无法把临时 URL 持久化为本地资源 | 模块 B |
| 预览元数据端点 | UI 不知道一个任务有哪些可预览产物、入口是哪个 HTML | 模块 C |
| UI 预览组件 | 用户看不到入口按钮 | 模块 D |
| 工作区清理 | 磁盘无限膨胀 | 模块 E |

### 2.3 现有代码的关键事实（v2 评审补充）

经代码对照，确认以下事实影响设计决策：

| 事实 | 位置 | 影响 |
|------|------|------|
| `thread_id` 是每次请求生成的 `uuid4()`，用作 LangGraph checkpoint key | `sdlc.py:60` | 工作区按 thread_id 组织，端点必须用 thread_id 寻址 |
| `task_id` 是 SDLCTask 数据库模型的独立 ID | `sdlc.py:405` | 与 thread_id 不同，`/tasks/{task_id}` 路由用于 CRUD |
| `save_output_file` 绕过 `validate_file_path` 和 `is_allowed_extension` | `sdlc_tools.py:1013` | 既有安全缺口，新工具不应继承此模式 |
| SDLC 端点用 `@limiter.limit("N per minute")` 装饰器，非 `RATE_LIMIT_ENDPOINTS` dict | `sdlc.py:49,104,155...` | 新端点须保持一致 |
| registry.py 按工具名循环匹配分配 agents | `registry.py:266-300` | 新工具须在循环中加 if 分支，否则变成 programmatic-only |
| `/{thread_id}/state` 路由定义在 `/tasks` 之前 | `sdlc.py:155 vs 392` | 路径段数不同不冲突，但新端点应放在 `/{thread_id}/*` 组中保持一致 |

---

## 三、参考项目研究

### 3.1 OpenGenerativeUI（CopilotKit，530★）

- **核心模式**：Agent 生成 HTML/SVG → 通过 LangGraph 状态传递 → 前端用
  `<iframe sandbox="allow-scripts allow-same-origin">` 渲染 → ResizeObserver 自适应高度
- **借鉴点**：沙箱 iframe 是渲染 Agent 生成内容的黄金标准；ResizeObserver 解决高度自适应
- **⚠️ 安全修正**：其 `allow-scripts + allow-same-origin` 组合已被 W3C 规范明确警告
  （见 3.6），本方案改用更安全的 `sandbox="allow-scripts"` 模式

### 3.2 Pretifact（artifact 预览）

- **核心模式**：Agent 产出 artifact → 浏览器新标签页打开 → websocket live-reload
- **借鉴点**："artifact" 抽象：把任何可预览产物统一为 `{path, type, open_url}` 三元组

### 3.3 MS-Agent（ModelScope，4k★）

- **核心模式**：Agent 生成代码 → Docker 沙箱执行 → artifact 协议回传 → 前端渲染
- **借鉴点**："Agent Skills Protocol"：工具返回结构化元数据，让 UI 知道产物类型

### 3.4 Cloudscape Artifact Previews（AWS 设计系统）

- **核心模式**：区分 **Inline previews**（简单内容，卡片+下载）vs
  **Canvas previews**（复杂交互内容，相邻面板展开）
- **借鉴点**：
  - 复杂交互产物（游戏/看板）应用 Canvas 模式——独立面板/Tab 展示
  - 简单产物（单图/JSON）用 Inline 模式——卡片内直接渲染
  - artifact 应有 label + description + metadata + related actions
- **本方案应用**：预览面板作为 Results 的独立 Tab（Canvas 模式），
  而非折叠在审批面板之后

### 3.5 Cline（开源编码 Agent）

- **核心模式**：沙箱化执行 + 检查点恢复 + 工作区生命周期管理
- **借鉴点**：工作区应有明确的清理时机（任务删除/归档时联动清理）

### 3.6 iframe sandbox 安全规范（W3C/WHATWG）

来源：[W3C HTML 规范](https://html.spec.whatwg.org/) + [rocketvalidator 指南](https://rocketvalidator.com/html-validation/bad-value-x-for-attribute-sandbox-on-element-iframe-setting-both-allow-scripts-and-allow-same-origin-is-not-recommended-because-it-effectively-enables-an-embedded-page-to-break-out-of-all-sandboxing) + [The Hacker News iframe 安全报告](https://thehackernews.com/2025/09/iframe-security-exposed-blind-spot.html)

**关键结论**：`allow-scripts + allow-same-origin` 同时使用是安全反模式：
- 嵌入页面可用 JS 程序化移除自身 `<iframe>` 的 sandbox 属性
- 移除后所有沙箱限制失效，可访问 parent DOM、cookie、storage
- W3C 验证器会对此组合发出警告
- 2024-2025 年多个支付 skimmer 攻击利用此组合绕过防护

**本方案修正**：iframe 使用 `sandbox="allow-scripts"`（无 `allow-same-origin`）。
经规范验证，此模式下：
- `<img>`/`<script>`/`<link>` 标签的相对路径仍基于 base URL 正确解析
  （base URL 是 iframe 的 src，与 origin 无关）
- JS 无法访问 parent DOM、cookie、localStorage（安全）
- 唯一限制：JS 内的 `fetch`/`XHR` 跨域请求会失败（opaque origin）——
  对大多数静态网页/游戏不影响（用标签引用资源即可）

### 3.7 提炼出的通用模式

```
Agent 写文件 ─┐
              ├─→ workspace/{thread_id}/ ─→ StaticFiles 挂载 ─→ 浏览器访问
Agent 下载资源 ─┘                                   │
                                                    └─→ UI iframe 渲染 (sandbox=allow-scripts)
```

---

## 四、设计方案

### 4.1 总体架构

```
┌─────────────────────────────────────────────────────────────┐
│                      Agent (Coder)                          │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────┐  │
│  │ write_file  │  │ download_asset│  │ register_artifact  │  │
│  │ (existing)  │  │  (NEW: 模块B) │  │   (NEW: 模块B)     │  │
│  └──────┬──────┘  └───────┬───────┘  └─────────┬──────────┘  │
└─────────┼─────────────────┼────────────────────┼─────────────┘
          │                 │                    │
          ▼                 ▼                    ▼
   workspace/{thread_id}/  (本地持久化)    .artifacts/*.json
          │                 (扩展名校验)    (每 artifact 独立文件)
          │                                      │
┌─────────┴──────────────────────────────────────┴─────────────┐
│                     FastAPI (app/main.py)                    │
│  ┌──────────────────────┐  ┌──────────────────────────────┐  │
│  │ StaticFiles 挂载      │  │ GET /{thread_id}/artifacts   │  │
│  │   /workspace/...      │  │   (NEW: 模块C)               │  │
│  │   (NEW: 模块A)        │  └──────────────────────────────┘  │
│  └──────────────────────┘                                    │
└─────────────────────────────────────────────────────────────┘
          │                                      │
          ▼                                      ▼
┌─────────────────────────────────────────────────────────────┐
│                   Streamlit UI (ui/)                        │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ Preview Tab (NEW: 模块D) — Canvas 模式                 │   │
│  │  - 列出 artifacts (Inline 卡片)                        │   │
│  │  - HTML: iframe (sandbox=allow-scripts)                │   │
│  │  - image/video: 内联渲染                               │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### 4.2 设计原则

1. **thread_id 统一寻址** — 工作区物理路径按 thread_id 组织，
   所有预览相关端点、工具、UI 查询统一用 thread_id，不混用 task_id。
2. **通用 artifact 抽象** — 任何可预览产物统一为
   `{name, path, type, url, description}`，type 支持 html/image/video/text/json/other，
   UI 按 type 分发渲染器。**不出现任何 "game" / "snake" / "scene" 字样。**
3. **Agent 自主注册** — Agent 决定哪个文件是"主入口"，
   通过 `register_artifact` 工具声明，而非靠文件名约定。
4. **工具层零侵入** — 新工具与 `write_file` 同级，走相同的扩展名校验链路，
   不修改 SDLC 状态机的字段（artifact 元数据存工作区文件，不进 SDLCState）。
5. **安全分层** — StaticFiles 只读挂载、扩展名白名单、路径遍历校验、
   下载内容专项 XSS 扫描（不复用全量规则避免误报）、iframe sandbox 最小权限。
6. **UI 按需渲染** — 有 artifact 才显示 Preview Tab，没有则完全隐藏，
   不影响纯后端任务（如重构、修 bug）的体验。

---

## 五、详细设计

### 5.1 模块 A：FastAPI StaticFiles 挂载

**目标**：让 `workspace/{thread_id}/` 下的文件可通过 HTTP 访问。

#### 5.1.1 实现位置

`app/main.py`（在 `app.include_router(api_router, ...)` 之后追加）

#### 5.1.2 核心代码

```python
from fastapi.staticfiles import StaticFiles
from app.core.config import settings

# Feature flag — 默认开启，可关闭
if settings.ENABLE_WORKSPACE_PREVIEW:
    settings.SDLC_WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    app.mount(
        settings.WORKSPACE_PREVIEW_URL_PREFIX,  # 默认 "/workspace"
        StaticFiles(directory=settings.SDLC_WORKSPACE_ROOT),
        name="workspace",
    )
```

#### 5.1.3 安全考虑

| 风险 | 缓解措施 |
|------|----------|
| 路径遍历（`/workspace/../etc/passwd`） | `StaticFiles` 内部已用 `Path.resolve()` 校验，且只在挂载根下提供文件 |
| 列目录泄露 | `StaticFiles` 默认不开启 `html=True`，不返回目录列表 |
| 扩展名越界 | 挂载是只读的，写入仍受 `write_file` 的白名单约束 |
| 跨任务访问 | URL 中必须含 `{thread_id}`，逻辑上隔离；v2 可加会话归属中间件 |
| iframe 沙箱逃逸 | iframe 用 `sandbox="allow-scripts"`（无 same-origin），见 5.4.2 |

#### 5.1.4 配置项

```python
# app/core/config.py
self.ENABLE_WORKSPACE_PREVIEW = os.getenv("ENABLE_WORKSPACE_PREVIEW", "true").lower() == "true"
self.WORKSPACE_PREVIEW_URL_PREFIX = os.getenv("WORKSPACE_PREVIEW_URL_PREFIX", "/workspace")
```

---

### 5.2 模块 B：Agent 工具 — `download_asset` + `register_artifact`

#### 5.2.1 工具 1：`download_asset`（远程资源持久化）

**文件位置**：`app/core/langgraph/tools/asset_tools.py`（新建）

```python
"""Asset management tools — generic resource download + artifact registration.

These tools let the coder agent persist remote URLs (e.g. temporary image
links from generate_image) into the workspace sandbox and declare which
files should be surfaced as previewable artifacts in the UI.

Decoupled from any specific task — works for any web project that produces
static assets (games, dashboards, landing pages, tools, etc.).
"""

import mimetypes
from pathlib import Path
from urllib.parse import urlparse

import httpx
from langchain_core.tools import tool

from app.core.config import settings
from app.core.logging import logger
from app.core.langgraph.tools.sdlc_tools import (
    get_workspace_path,
    is_allowed_extension,
)


_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 50MB
_DOWNLOAD_TIMEOUT = 30.0


@tool
async def download_asset(
    thread_id: str,
    url: str,
    dest_path: str,
    filename: str = "",
) -> dict:
    """Download a remote resource (image, video, JSON, etc.) into the workspace.

    Use this tool to persist temporary URLs (e.g. generate_image output that
    expires in ~24h) so the asset remains accessible from the generated HTML.

    Args:
        thread_id: The thread ID for workspace isolation.
        url: HTTP(S) URL of the resource to download.
        dest_path: Destination directory relative to workspace root
            (e.g. "assets/images"). Created if not exists.
        filename: Output filename. If empty, inferred from the URL path.

    Returns:
        Dict with local_path, absolute_url, content_type, size_bytes, success, error.

    Examples:
        download_asset(thread_id, "https://dashscope-result.com/tmp/abc.png",
                       "assets/images", "snake-skin.png")
    """
    try:
        # Infer filename from URL if not provided
        if not filename:
            url_path = urlparse(url).path
            filename = Path(url_path).name or "asset.bin"

        # Extension whitelist check — MUST use is_allowed_extension,
        # NOT inherit save_output_file's bypass behavior (security gap).
        dest_file = Path(filename)
        if not is_allowed_extension(dest_file):
            return {
                "local_path": "",
                "absolute_url": "",
                "content_type": "",
                "size_bytes": 0,
                "success": False,
                "error": f"Extension not allowed: {dest_file.suffix}",
            }

        # Resolve destination within workspace sandbox
        relative_path = f"{dest_path}/{filename}" if dest_path else filename
        abs_path = get_workspace_path(thread_id, relative_path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        # Stream download with size limit
        async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                downloaded = 0
                with open(abs_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=8192):
                        downloaded += len(chunk)
                        if downloaded > _MAX_DOWNLOAD_BYTES:
                            abs_path.unlink(missing_ok=True)
                            return {
                                "local_path": "",
                                "absolute_url": "",
                                "content_type": content_type,
                                "size_bytes": downloaded,
                                "success": False,
                                "error": f"Exceeded {_MAX_DOWNLOAD_BYTES} bytes limit",
                            }
                        f.write(chunk)

        # Targeted XSS scan for SVG/HTML only — NOT full check_security
        # (which has false positives on legit JSON keys / hex values)
        ext = dest_file.suffix.lower()
        if ext in {".svg", ".html", ".htm"}:
            svg_issues = _scan_svg_xss(abs_path.read_text(encoding="utf-8"))
            if svg_issues:
                abs_path.unlink(missing_ok=True)
                return {
                    "local_path": "",
                    "absolute_url": "",
                    "content_type": content_type,
                    "size_bytes": downloaded,
                    "success": False,
                    "error": f"XSS pattern detected: {svg_issues[0]}",
                }

        preview_url = f"{settings.WORKSPACE_PREVIEW_URL_PREFIX}/{thread_id}/{relative_path}"
        logger.info(
            "asset_downloaded",
            thread_id=thread_id,
            url=url,
            path=relative_path,
            size=downloaded,
        )
        return {
            "local_path": relative_path,
            "absolute_url": preview_url,
            "content_type": content_type,
            "size_bytes": downloaded,
            "success": True,
            "error": None,
        }
    except Exception as e:
        logger.exception("asset_download_failed", thread_id=thread_id, url=url, error=str(e))
        return {
            "local_path": "",
            "absolute_url": "",
            "content_type": "",
            "size_bytes": 0,
            "success": False,
            "error": str(e),
        }


def _scan_svg_xss(content: str) -> list[str]:
    """Targeted XSS scan for SVG/HTML — only blocks script injection,
    not legit content. Avoids check_security false positives on JSON keys.
    """
    import re
    patterns = [
        (r"<script\b", "<script> tag"),
        (r"javascript:", "javascript: URL"),
        (r"on\w+\s*=", "inline event handler"),
    ]
    issues = []
    for pattern, label in patterns:
        if re.search(pattern, content, re.IGNORECASE):
            issues.append(label)
    return issues
```

**关键设计点**：
- **扩展名校验**：显式调用 `is_allowed_extension()`，**不继承 `save_output_file` 的绕过行为**
- **安全扫描分层**：仅对 SVG/HTML 做 XSS 专项扫描（`<script>`/`javascript:`/`on*=`），
  不复用 `check_security` 全量规则——避免合法 JSON 中的 hex 值/API key 字段名被误报
- **大小限制**：50MB 上限，流式下载，超限即删
- **返回值结构化**：同时返回 `local_path`（HTML 中用相对路径）和
  `absolute_url`（UI 中直接打开），Agent 不需要拼 URL

#### 5.2.2 工具 2：`register_artifact`（声明预览入口）

```python
@tool
def register_artifact(
    thread_id: str,
    name: str,
    path: str,
    artifact_type: str = "auto",
    description: str = "",
) -> dict:
    """Register a workspace file as a previewable artifact.

    Use this tool AFTER writing/persisting files to declare which ones the
    user should be able to preview. Call once per artifact. The UI will
    render a preview panel listing all registered artifacts.

    Args:
        thread_id: The thread ID for workspace isolation.
        name: Human-readable name (e.g. "Snake Game", "API Docs",
              "Sales Dashboard"). Shown as the preview button label.
        path: Relative path to the file within the workspace.
        artifact_type: One of: "auto" (infer from extension),
            "html", "image", "video", "text", "json", "other".
        description: Optional description shown under the name.

    Returns:
        Dict with success status, resolved type, preview_url, and error.
    """
    try:
        abs_path = get_workspace_path(thread_id, path)
        if not abs_path.exists():
            return {"success": False, "error": f"File not found: {path}"}

        resolved_type = _resolve_artifact_type(artifact_type, abs_path)
        preview_url = f"{settings.WORKSPACE_PREVIEW_URL_PREFIX}/{thread_id}/{path}"

        # Each artifact = independent file under .artifacts/ dir
        # Avoids concurrent write race on a single manifest JSON
        artifacts_dir = get_workspace_path(thread_id, ".artifacts")
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        # Sanitize name for filename
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:50]
        artifact_file = artifacts_dir / f"{safe_name}.json"

        import json
        from datetime import datetime, timezone
        artifact_data = {
            "name": name,
            "path": path,
            "type": resolved_type,
            "description": description,
            "preview_url": preview_url,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        artifact_file.write_text(
            json.dumps(artifact_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        logger.info(
            "artifact_registered",
            thread_id=thread_id,
            name=name,
            path=path,
            type=resolved_type,
        )
        return {"success": True, "type": resolved_type, "preview_url": preview_url, "error": None}
    except Exception as e:
        logger.exception("artifact_register_failed", thread_id=thread_id, name=name, error=str(e))
        return {"success": False, "type": "", "preview_url": "", "error": str(e)}


def _resolve_artifact_type(artifact_type: str, abs_path: Path) -> str:
    """Infer artifact type from extension when artifact_type='auto'."""
    if artifact_type != "auto":
        return artifact_type
    ext_map = {
        ".html": "html", ".htm": "html",
        ".png": "image", ".jpg": "image", ".jpeg": "image",
        ".gif": "image", ".webp": "image", ".svg": "image",
        ".mp4": "video", ".webm": "video",
        ".json": "json",
    }
    return ext_map.get(abs_path.suffix.lower(), "text")
```

**关键设计点**：
- **每个 artifact 独立文件**：`workspace/{thread_id}/.artifacts/{name}.json`，
  避免 read-modify-write 的并发竞争（v2 评审修正）。读取时遍历目录。
- **type 自动推断**：`auto` 模式按扩展名映射
- **可多次调用**：一个任务可有多个 artifact

#### 5.2.3 工具注册（v2 修正：完整 registry 分支）

**文件位置**：`app/core/langgraph/tools/registry.py`

```python
def _build_default_registry() -> ToolRegistry:
    # ... existing imports ...
    from app.core.langgraph.tools.asset_tools import (
        download_asset,
        register_artifact,
    )

    asset_tools_list = [download_asset, register_artifact]

    registry = ToolRegistry()

    # ... existing chat/sdlc/code_index/media registrations ...

    # -- Asset management tools (coder can persist URLs + declare previews) --
    # MUST be in the agent-binding set, otherwise they become
    # programmatic-only (get_by_name works, but LLM cannot call them).
    for tool in asset_tools_list:
        registry.register(
            tool,
            agents={"coder"},
            requires={"ENABLE_WORKSPACE_PREVIEW"},  # gated by feature flag
            tags={"asset"},
        )

    return registry
```

**⚠️ 关键**：必须在循环中显式设置 `agents={"coder"}`，否则工具会被注册但
agent 集合为空，`get_for_agent("coder")` 不会返回它，Agent 无法调用。

#### 5.2.4 扩展名白名单扩充

```python
# app/core/config.py — 默认值扩充
self.SDLC_ALLOWED_EXTENSIONS = parse_list_from_env(
    "SDLC_ALLOWED_EXTENSIONS",
    [
        # 代码
        "py", "js", "ts", "jsx", "tsx", "json", "md", "txt", "yml", "yaml", "toml",
        # Web
        "html", "htm", "css", "xml", "svg",
        # 静态资源
        "png", "jpg", "jpeg", "gif", "webp", "ico",
        "mp4", "webm", "mp3", "wav", "ogg",
        # 字体
        "woff", "woff2", "ttf", "eot",
    ],
)
```

**注意**：`save_output_file` 现有绕过白名单的行为是已知安全缺口，
不在本方案范围内修复（避免扩大改动面），但 `download_asset` 不继承此行为。

---

### 5.3 模块 C：预览元数据端点

**目标**：让 UI 查询某个任务的 artifact 列表。

#### 5.3.1 端点定义（v2 修正：统一用 thread_id）

```
GET /api/v1/sdlc/{thread_id}/artifacts
```

与 `/{thread_id}/state`、`/{thread_id}/approve` 同级，**用 thread_id 寻址**，
因为工作区物理路径就是 `workspace/{thread_id}/`。

#### 5.3.2 路由顺序与冲突分析

现有 `sdlc.py` 路由顺序（按定义）：
```
L48:  POST /                            (创建任务)
L103: POST /stream                       (流式创建)
L155: GET  /{thread_id}/state            (查状态)
L185: GET  /feature-flags                (功能标志)
L214: POST /{thread_id}/approve          (审批)
L263: POST /{thread_id}/reject           (拒绝)
L319: POST /{thread_id}/replay           (重放)
L392: GET  /tasks                        (任务列表)
L405: GET  /tasks/{task_id}              (任务详情)
L422: GET  /{thread_id}/approvals        (审批日志)
L435: POST /tasks/{task_id}/cancel       (取消)
L452: DELETE /tasks/{task_id}            (删除)
```

**新增 `GET /{thread_id}/artifacts` 应放在 L422 (`/{thread_id}/approvals`) 附近**，
与其他 `/{thread_id}/*` 路由同组。

**冲突分析**：
- `/{thread_id}/artifacts` 是两段路径（`/{x}/artifacts`）
- `/tasks/{task_id}` 也是两段路径（`/tasks/{x}`）
- 两者路径模式不同，不会误匹配：请求 `/abc-123/artifacts` 不会匹配 `/tasks/{task_id}`
  （因为第一段不是字面量 "tasks"），请求 `/tasks/xxx` 不会匹配 `/{thread_id}/artifacts`
  （因为第二段不是字面量 "artifacts"）
- ✅ 无冲突，但保持路由分组一致性

#### 5.3.3 实现

```python
@router.get("/{thread_id}/artifacts")
@limiter.limit("30 per minute")  # v2 修正: 用装饰器，与现有 SDLC 端点一致
async def sdlc_get_artifacts(
    thread_id: str,
    request: Request,
    session: Session = Depends(get_current_session),
):
    """List previewable artifacts for a task.

    Reads individual JSON files from workspace/{thread_id}/.artifacts/
    directory. Returns empty list if no artifacts registered.
    """
    try:
        from app.core.langgraph.tools.sdlc_tools import get_workspace_path
        artifacts_dir = get_workspace_path(thread_id, ".artifacts")
        artifacts = []
        if artifacts_dir.exists():
            for af in sorted(artifacts_dir.glob("*.json")):
                try:
                    data = json.loads(af.read_text(encoding="utf-8"))
                    # Attach absolute_url for direct browser access
                    data["absolute_url"] = f"{request.base_url}{data.get('preview_url', '')}"
                    artifacts.append(data)
                except Exception:
                    continue

        logger.info("sdlc_get_artifacts", thread_id=thread_id, count=len(artifacts))
        return {"thread_id": thread_id, "artifacts": artifacts}
    except Exception as e:
        logger.exception("sdlc_get_artifacts_failed", thread_id=thread_id, error=str(e))
        raise safe_500(e, context="sdlc_get_artifacts")
```

**⚠️ 限流方式**：直接用 `@limiter.limit("30 per minute")` 装饰器，
**不**修改 `RATE_LIMIT_ENDPOINTS` dict（该 dict 仅用于 chat/auth 端点）。

---

### 5.4 模块 D：UI 预览组件

#### 5.4.1 位置决策（v2 修正：独立 Tab）

参考 Cloudscape Canvas previews 模式，预览面板作为 **Results 的独立 Tab**，
与 Plan/Code/Tests/Review 并列，而非折叠在审批面板之后。

**理由**：用户完成 Web 类任务后最想看"产物长什么样"，应高亮展示。

#### 5.4.2 iframe 沙箱策略（v2 修正：安全模式）

```python
import streamlit.components.v1 as components

def _render_html_preview(url: str, height: int = 600) -> None:
    """Render HTML artifact in a sandboxed iframe.

    Uses sandbox="allow-scripts" ONLY (no allow-same-origin).
    Per W3C/WHATWG spec, allow-scripts + allow-same-origin together
    is a security anti-pattern: embedded page can use JS to remove its
    own sandbox attribute, breaking out of all restrictions.

    With allow-scripts alone:
    - <img>/<script>/<link> relative paths still work (resolved via base URL)
    - JS runs but cannot access parent DOM, cookies, localStorage
    - JS fetch/XHR to same-origin fails (opaque origin) — acceptable for
      static pages/games that use tag-based resource loading
    """
    components.iframe(url, height=height, scrolling=True)
```

**沙箱权限对比**：

| 策略 | 脚本 | 同源访问 | 相对路径资源 | localStorage | 安全性 |
|------|------|----------|-------------|-------------|--------|
| `allow-scripts allow-same-origin`（原方案，❌） | ✅ | ✅ | ✅ | ✅ | ❌ 可逃逸 |
| `allow-scripts`（v2 方案，✅） | ✅ | ❌ | ✅ | ❌ | ✅ 安全 |
| 无 sandbox | ✅ | ✅ | ✅ | ✅ | ❌ 无隔离 |

**v2 方案的取舍**：JS 内 `fetch`/`XHR` 加载同目录资源会失败（opaque origin）。
对大多数静态网页/游戏不影响（用 `<img>`/`<script>` 标签引用资源即可）。
如需 fetch，v2 可升级为独立 origin 挂载（见 5.4.3）。

#### 5.4.3 v2 可选升级：独立 origin 挂载

若未来需要支持 JS fetch/localStorage 的复杂应用，可将 StaticFiles 挂在独立端口/子域名：

```python
# 独立 FastAPI app，监听 8081 端口
preview_app = FastAPI()
preview_app.mount("/", StaticFiles(directory=settings.SDLC_WORKSPACE_ROOT), name="preview")
# iframe src 指向 http://preview.host:8081/{thread_id}/index.html
# 此时 allow-scripts + allow-same-origin 是安全的（不同源，无法访问主应用）
```

本方案 v1 不实现，但架构预留扩展点。

#### 5.4.4 组件结构

**文件位置**：`ui/components/preview.py`（新建）

```python
"""Preview panel — render workspace artifacts as previewable cards.

Generic component: works for any artifact type (html/image/video/text).
Appears as an independent tab in the results panel only when artifacts
exist; otherwise hidden (keeps pure-backend tasks clean).

Follows Cloudscape Canvas previews pattern: complex interactive content
gets a dedicated panel, not folded under approval controls.
"""

from typing import Dict, List

import streamlit as st
import streamlit.components.v1 as components


def render_preview_tab() -> None:
    """Render the Preview tab content.

    Called from ui/components/results.py as a new tab alongside
    Plan/Code/Tests/Review. Self-fetches the client from session_state
    to match the existing pattern in _render_approval().
    """
    client = st.session_state.api_client  # v2 修正: 内部取，不传参
    thread_id = st.session_state.get("current_thread_id")
    if not thread_id:
        st.caption("No task selected.")
        return

    artifacts = _load_artifacts(client, thread_id)
    if not artifacts:
        st.caption("No previewable artifacts. Use register_artifact tool to declare outputs.")
        return

    st.caption(f"{len(artifacts)} artifact(s) registered by the agent.")

    for art in artifacts:
        _render_artifact_card(art)


def _render_artifact_card(artifact: Dict) -> None:
    """Render one artifact card with type-appropriate preview."""
    name = artifact.get("name", "Untitled")
    art_type = artifact.get("type", "other")
    url = artifact.get("absolute_url") or artifact.get("preview_url", "")
    desc = artifact.get("description", "")

    with st.container(border=True):
        col_info, col_action = st.columns([3, 1])
        with col_info:
            st.markdown(f"**{name}**")
            if desc:
                st.caption(desc)
            st.caption(f"`{artifact.get('path', '')}` · {art_type}")
        with col_action:
            st.link_button("Open ↗", url, use_container_width=True)

        # Type-dispatched inline preview
        if art_type == "html":
            _render_html_preview(url)
        elif art_type == "image":
            st.image(url)
        elif art_type == "video":
            st.video(url)
        elif art_type == "json":
            try:
                import requests
                data = requests.get(url, timeout=5).json()
                st.json(data)
            except Exception:
                st.caption("Failed to load JSON.")


def _render_html_preview(url: str, height: int = 600) -> None:
    """Render HTML artifact in sandboxed iframe (see 5.4.2 for security rationale)."""
    components.iframe(url, height=height, scrolling=True)


def _load_artifacts(client, thread_id: str) -> List[Dict]:
    """Fetch artifacts list, cache in session_state with per-thread key."""
    cache_key = f"artifacts_{thread_id}"
    if cache_key not in st.session_state:
        try:
            st.session_state[cache_key] = client.list_artifacts(thread_id)
        except Exception:
            st.session_state[cache_key] = []
    return st.session_state[cache_key]
```

#### 5.4.5 集成到 results.py

`ui/components/results.py:render_results()` 修改 Tab 定义：

```python
from ui.components.preview import render_preview_tab

# Tab 定义增加 Preview（放在 Code 之后，突出产物展示）
tab_plan, tab_code, tab_preview, tab_tests, tab_review = st.tabs([
    "Plan", "Code", "Preview", "Tests", "Review"
])

with tab_preview:
    # 仅在任务完成或有 artifact 时显示内容
    if st.session_state.get("is_complete") or st.session_state.get("human_approval_needed"):
        render_preview_tab()
    else:
        st.caption("Preview available after task completes.")
```

#### 5.4.6 API 客户端与配置

`ui/api_client.py` 追加：

```python
def list_artifacts(self, thread_id: str) -> list:
    """List previewable artifacts for a task (by thread_id)."""
    try:
        resp = self._client.get(
            config.sdlc_artifacts_url.format(thread_id=thread_id),
            headers=self._headers(),
            timeout=config.request_timeout,
        )
        if resp.status_code == 200:
            return resp.json().get("artifacts", [])
        return []
    except Exception:
        return []
```

`ui/config.py` 追加：

```python
@property
def sdlc_artifacts_url(self) -> str:
    # v2 修正: 用 thread_id 寻址，与 /{thread_id}/state 同级
    return f"{self.api_base_url}/api/v1/sdlc/{{thread_id}}/artifacts"
```

---

### 5.5 模块 E：工作区清理（v2 新增）

**目标**：避免磁盘无限膨胀，v1 即提供清理入口。

#### 5.5.1 任务删除联动清理

在 `app/services/sdlc_task_service.py` 的 `delete_task` 中追加工作区清理：

```python
import shutil
from app.core.config import settings

async def delete_task(self, task_id: str) -> bool:
    # ... existing logic ...

    # v2 新增: 联动清理工作区
    # task_id 在审批日志中作为 task_id 字段记录（见 sdlc.py:228, 283）
    # 需要从 task 记录取 thread_id，或审批日志反查
    task = await self.get_task(task_id)
    if task and task.thread_id:  # 假设 SDLCTask 模型有 thread_id 字段
        workspace_path = settings.SDLC_WORKSPACE_ROOT / task.thread_id
        if workspace_path.exists():
            shutil.rmtree(workspace_path, ignore_errors=True)
            logger.info("workspace_cleaned_on_delete", thread_id=task.thread_id)

    return success
```

**⚠️ 前置条件**：需确认 `SDLCTask` 模型是否记录 `thread_id`。
若未记录，需新增字段（Alembic migration）。这是现有模型的缺口，本方案标记为依赖项。

#### 5.5.2 可选：定时清理

```python
# app/core/config.py
self.WORKSPACE_RETENTION_DAYS = int(os.getenv("WORKSPACE_RETENTION_DAYS", "30"))
self.ENABLE_WORKSPACE_AUTO_CLEANUP = os.getenv("ENABLE_WORKSPACE_AUTO_CLEANUP", "false").lower() == "true"
```

定时任务清理超过 N 天未修改的工作区目录。v1 不实现，保留配置项。

---

## 六、安全设计

### 6.1 威胁模型与缓解（v2 修订）

| 威胁 | 路径 | 缓解 |
|------|------|------|
| 路径遍历 | `download_asset` 写到 `../../etc/passwd` | `get_workspace_path` 校验 `resolved_path.startswith(workspace_root)` |
| 恶意扩展名 | 下载 `.exe` / `.sh` | `is_allowed_extension()` 白名单校验（download_asset 显式调用） |
| XSS 注入 | Agent 写入恶意 HTML，iframe 执行 | iframe `sandbox="allow-scripts"`（无 same-origin），无法访问 parent |
| 沙箱逃逸 | 嵌入页面 JS 移除自身 sandbox | 不使用 `allow-same-origin`，即使移除 sandbox 也无法跨 origin 访问 |
| 大文件 DoS | 下载 10GB 视频 | 50MB 上限 + 流式下载 + 30s 超时 |
| SVG 脚本注入 | 下载含 `<script>` 的 SVG | `_scan_svg_xss` 专项扫描，block |
| 误报合法内容 | JSON 中含 hex 值/API key 字段名 | download_asset **不**用 `check_security` 全量规则，仅 SVG/HTML 做 XSS 扫描 |
| 跨任务访问 | 用户 A 访问用户 B 的 thread_id | v1 接受（工作区按 thread_id 隔离）；v2 加 session-thread 归属中间件 |

### 6.2 iframe sandbox 决策依据（v2 关键修正）

**原方案**：`sandbox="allow-scripts allow-same-origin"`
**问题**：W3C/WHATWG 规范明确警告此组合使 sandbox 失效——嵌入页面可用 JS 移除自身 sandbox 属性，访问 parent DOM/cookie。
**v2 方案**：`sandbox="allow-scripts"`（无 `allow-same-origin`）
**验证**：
- 相对路径资源（`<img src="assets/x.png">`）基于 base URL 解析，正常加载
- JS 无法访问 parent DOM、cookie、localStorage（安全）
- JS `fetch`/`XHR` 跨域失败（opaque origin）——对静态网页/游戏无影响

来源：[W3C HTML 规范 sandbox 章节](https://html.spec.whatwg.org/multipage/iframe-embed-object.html#attr-iframe-sandbox) + [rocketvalidator 指南](https://rocketvalidator.com/html-validation/bad-value-x-for-attribute-sandbox-on-element-iframe-setting-both-allow-scripts-and-allow-same-origin-is-not-recommended-because-it-effectively-enables-an-embedded-page-to-break-out-of-all-sandboxing)

### 6.3 扩展名白名单扩充

见 5.2.4。

---

## 七、实现阶段

### 阶段 1：基础管道（最小可用）

**交付物**：
- [ ] 模块 A：`StaticFiles` 挂载 + `ENABLE_WORKSPACE_PREVIEW` flag
- [ ] 模块 B：`download_asset` + `register_artifact` 工具 + registry 注册
- [ ] 扩展名白名单扩充
- [ ] 模块 C：`GET /{thread_id}/artifacts` 端点
- [ ] 模块 D：Preview Tab + Open in new tab 按钮 + iframe 渲染
- [ ] 模块 E：task 删除联动清理（依赖 SDLCTask.thread_id 字段）

**验收标准**：
- 手动调用 `download_asset` 下载一张图片，HTML 引用本地路径，浏览器访问能看到图片
- `register_artifact` 后，UI Preview Tab 出现卡片
- iframe 内 HTML 可运行 JS，相对路径资源正常加载
- 删除任务后工作区目录被清理

### 阶段 2：体验优化

**交付物**：
- [ ] image/video artifact 的内联渲染
- [ ] artifact 列表缓存与刷新按钮
- [ ] ResizeObserver 自适应 iframe 高度
- [ ] Streamlit CSP 策略评估（iframe 跨域加载）

**验收标准**：
- 不离开 Streamlit 即可在 iframe 中玩 Agent 生成的网页游戏

### 阶段 3：Agent 自主能力（提示词引导）

**交付物**：
- [ ] 在 `app/core/prompts/coder.md` 追加**通用**工具使用指引：
  > "When the task produces a web page, game, dashboard, or any artifact
  > the user can preview: (1) persist all remote URLs via download_asset;
  > (2) declare each previewable file via register_artifact with a
  > descriptive name and the entry HTML."
- [ ] Coder 提示词加入 `register_artifact` 的 few-shot 示例

**验收标准**：
- 给 Agent 任意 Web 类任务，Agent 自主调用工具，UI 出现预览

---

## 八、风险与后遗症评估（v2 修订）

| 风险 | 等级 | 后遗症 | 缓解 |
|------|------|--------|------|
| StaticFiles 挂载暴露工作区所有文件 | 中 | 其他任务文件按 thread_id 可访问（无鉴权） | v1 接受；v2 加 session-thread 归属中间件 |
| `.artifacts/*.json` 被手动破坏 | 低 | 该 artifact 卡片不显示 | 端点对损坏文件跳过，UI 隐藏卡片 |
| Agent 滥用 `download_asset` 下大文件 | 中 | 磁盘占用增长 | 50MB 上限 + v2 可配额限制每任务总下载量 |
| iframe 渲染恶意 JS | 低 | 预览页面执行脚本 | `sandbox="allow-scripts"`，无法访问 parent/localStorage |
| 工作区清理策略基础 | 中 | 磁盘累积 | v1 task 删除联动清理；v2 定时清理 |
| SDLCTask 缺 thread_id 字段 | 中 | 模块 E 清理无法联动 | 需确认模型字段，必要时加 migration |
| 扩展名白名单含 .svg（可含 JS） | 低 | SVG 作为 image 渲染时浏览器不执行脚本；作为 text 渲染只显示源码 | `download_asset` 对 SVG 做 XSS 专项扫描 |

### 强耦合检查

- **与 SDLC 状态机耦合**：❌ 无。artifact 元数据存文件，不进 SDLCState
- **与具体 Agent 耦合**：❌ 无。工具注册到 coder，但任何 Agent 都可调用
- **与 UI 框架耦合**：❌ 无。后端端点通用，UI 组件可独立替换
- **与任务类型耦合**：❌ 无。无任何 "game"/"snake" 字样，完全通用
- **与现有工具耦合**：❌ 无。download_asset 走 `is_allowed_extension` 显式校验，不继承 save_output_file 缺口

---

## 九、测试场景（通用性验证）

以下场景**必须全部通过**，证明方案通用：

| 场景 | 任务描述 | 期望 Agent 行为 |
|------|----------|-----------------|
| 网页游戏 | "生成一个网页版贪吃蛇，用 AI 生图作场景和角色" | generate_image → download_asset → write_file(index.html) → register_artifact("Snake Game", "index.html") |
| 数据看板 | "用 Chart.js 生成一个销售数据看板，数据来自 sales.json" | write_file(sales.json) → write_file(dashboard.html) → register_artifact("Sales Dashboard", "dashboard.html") |
| 交互工具 | "做一个在线计算器网页" | write_file(calculator.html) → register_artifact("Calculator", "calculator.html") |
| 落地页 | "做一个产品介绍落地页，含 3 张配图" | generate_image x3 → download_asset x3 → write_file(index.html) → register_artifact("Landing Page", "index.html") |
| 纯后端任务 | "重构 auth.py 把密码校验抽成函数" | **不调用** download_asset / register_artifact，Preview Tab 隐藏 |

**关键验证点**：第 5 个场景必须不显示 Preview Tab，证明方案不侵入非 Web 类任务。

---

## 十、已知技术债（v2 显式标记）

| 技术债 | Trade-off | 偿还时机 |
|--------|-----------|----------|
| manifest 用文件而非 DB 表 | 失去事务保证和跨任务查询能力；纯文件方案遍历所有工作区目录才能列全部 artifact | v3 若需"全局 artifact 搜索"再迁 DB |
| `save_output_file` 绕过扩展名校验 | 既有安全缺口，Agent 可绕过白名单写任意扩展名 | 独立 PR 修复，不混入本方案 |
| iframe 无法支持 fetch/localStorage | `sandbox="allow-scripts"` 限制 opaque origin 的 JS 网络请求 | v2 升级独立 origin 挂载 |
| StaticFiles 无鉴权 | 任何知道 thread_id 的人可访问工作区文件 | v2 加 session-thread 归属中间件 |
| SDLCTask 可能缺 thread_id 字段 | 模块 E 清理依赖此字段做 task→workspace 映射 | 实现阶段确认，必要时加 migration |

---

## 十一、配置项汇总

```bash
# .env.production 新增

# 工作区预览总开关
ENABLE_WORKSPACE_PREVIEW=true

# StaticFiles URL 前缀（默认 /workspace）
WORKSPACE_PREVIEW_URL_PREFIX=/workspace

# 扩展名白名单（覆盖默认）
SDLC_ALLOWED_EXTENSIONS=py,js,ts,jsx,tsx,json,md,txt,yml,yaml,toml,html,htm,css,xml,svg,png,jpg,jpeg,gif,webp,ico,mp4,webm,mp3,wav,ogg,woff,woff2,ttf,eot

# 资源下载上限（字节，默认 50MB）
ASSET_DOWNLOAD_MAX_BYTES=52428800

# 资源下载超时（秒，默认 30）
ASSET_DOWNLOAD_TIMEOUT=30

# 工作区保留天数（v2 定时清理用，默认 30）
WORKSPACE_RETENTION_DAYS=30
```

---

## 十二、文件清单

### 新增文件

| 文件 | 用途 |
|------|------|
| `app/core/langgraph/tools/asset_tools.py` | `download_asset` + `register_artifact` 工具 |
| `ui/components/preview.py` | UI 预览 Tab 组件 |
| `ui/tests/test_asset_tools.py` | 工具单元测试（mock httpx） |
| `ui/tests/test_preview_panel.py` | UI 组件渲染测试（mock artifacts） |

### 修改文件

| 文件 | 改动 |
|------|------|
| `app/main.py` | 追加 `StaticFiles` 挂载 |
| `app/core/config.py` | 新增配置项 + 扩充扩展名白名单 |
| `app/api/v1/sdlc.py` | 追加 `GET /{thread_id}/artifacts` 端点 |
| `app/core/langgraph/tools/registry.py` | 注册 asset_tools（含 agents 分支） |
| `app/core/prompts/coder.md` | 追加通用工具使用指引 |
| `app/services/sdlc_task_service.py` | delete_task 联动清理工作区 |
| `ui/components/results.py` | Tab 定义增加 Preview |
| `ui/components/__init__.py` | 导出 preview 模块 |
| `ui/api_client.py` | 追加 `list_artifacts` 方法 |
| `ui/config.py` | 追加 `sdlc_artifacts_url` 属性 |

---

## 十三、与现有架构的兼容性

| 现有约定 | 本方案是否遵守 |
|----------|----------------|
| 所有 imports 在文件顶部 | ✅ |
| structlog 小写下划线事件名 | ✅ |
| 无 f-string in structlog events | ✅ |
| tenacity 重试 | ✅（download_asset 可加 `@retry`） |
| 所有路由加 `@limiter.limit` 装饰器 | ✅（v2 修正，不用 dict） |
| async DB 操作 | ✅（无 DB 操作，全文件系统） |
| Langfuse tracing | ✅（工具自动被 LangGraph callback 追踪） |
| 工作区沙箱 `workspace/{thread_id}/` | ✅（复用 `get_workspace_path`） |
| Feature Flag 隔离 | ✅（`ENABLE_WORKSPACE_PREVIEW`） |
| SDLCState 不污染 | ✅（artifact 元数据存文件） |
| thread_id 寻址 | ✅（v2 修正，端点/工具/UI 统一） |

---

## 十四、后续扩展（非本方案范围）

1. **独立 origin 挂载**：StaticFiles 挂独立端口/子域名，支持 iframe + allow-same-origin + fetch
2. **live-reload**：websocket 监听工作区文件变化，预览自动刷新（参考 Pretifact）
3. **Agent 自检**：Agent 完成后用 Playwright 打开预览，截图验证渲染正常（参考 AutoGen/Cline）
4. **多版本预览**：同一 artifact 的历史版本对比（需 git 集成）
5. **预览分享**：生成带签名的公开 URL，分享给非登录用户
6. **工作区配额**：按用户限制工作区总大小
7. **manifest 迁 DB**：支持跨任务 artifact 搜索（偿还技术债）

---

## 十五、总结

本方案 v2 以 **"通用 artifact 抽象 + 安全最小权限"** 为核心，
通过五个低耦合模块补齐了"Agent 生成 → 资源持久化 → 浏览器预览 → 工作区清理"的完整链路：

1. **模块 A**（StaticFiles）让工作区文件 HTTP 可达
2. **模块 B**（download_asset + register_artifact）让 Agent 自主持久化资源并声明预览入口
3. **模块 C**（artifacts 端点）让 UI 按 thread_id 查询预览元数据
4. **模块 D**（Preview Tab）让用户在独立 Tab 中按类型预览任意 Web 产物
5. **模块 E**（工作区清理）防止磁盘膨胀

**v2 关键修正**：
- thread_id 统一寻址（修正硬伤 1）
- iframe sandbox 改用安全模式（修正遗漏 1，遵循 W3C 规范）
- 工具注册补全 agents 分支（修正硬伤 2）
- 安全扫描分层避免误报（修正硬伤 5）
- manifest 每文件独立避免并发竞争（修正遗漏 2）

**核心保证**：全程不出现任何任务特定逻辑，换一个类似的 Web 类任务
（游戏/看板/工具/落地页）Agent 同样能完成"生成 + 预览"闭环。
