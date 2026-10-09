# 部署指南 (DEPLOY)

> 零构建静态 SPA 由 FastAPI app 容器同域托管。前端文件在 `frontend/`，由
> Dockerfile 的 `COPY . .` 烤进镜像，无需 Node 构建链。

## 1. 架构

```
浏览器 ──https──> cloudflared 隧道 ──> app:8000 (FastAPI)
                                         ├─ /api/v1/*   后端 API（auth/chatbot/sdlc）
                                         ├─ /docs /redoc /openapi.json
                                         ├─ /health
                                         ├─ /workspace_preview/*  工作区产物预览
                                         └─ /* (回退)  frontend/index.html + 静态资源
```

同域托管 = 前端与 API 同源，**无需 CORS 配置**（`ALLOWED_ORIGINS` 不用改）。

## 2. 本地开发

```bash
make dev   # uvicorn app.main:app --reload --port 8000
```

浏览器打开 `http://localhost:8000/` → 注册/登录 → 选择 Investor 或 Coder 模式。

> 前端纯静态，改 `frontend/` 下文件后刷新浏览器即可（service worker 启用时
> 需在 DevTools → Application → Service Workers → Update 或 unregister）。

## 3. 生产部署（docker-compose）

```bash
# 启动 app + db（Streamlit UI 已退场为可选 profile，默认不启动）
docker compose --env-file .env.production up -d db app

# 验证
curl -s http://localhost:8000/health
curl -sI http://localhost:8000/        # 应返回 index.html (200, text/html)
```

## 4. 公网 HTTPS（cloudflared 隧道，已配）

两条通道，**指向的后端不同**，别搞混：

| 通道 | 启动方式 | 指向 | 说明 |
|---|---|---|---|
| 桌面快捷隧道（生产，一键） | 双击 `tunnel\start-tunnel-prod.bat` | `http://localhost:8010` | **会先把 db + app 拉起来**（若未运行），等 `/health` 变绿后再开隧道 |
| 桌面快捷隧道（dev） | 双击 `tunnel\start-tunnel.bat` | `http://localhost:8000` | 只开隧道，暴露 `make dev` 的 uvicorn |
| Docker profile | `docker compose --profile tunnel up cloudflared` | 容器内 `http://app:8000` | 全容器化，不需要本地 exe |

`start-tunnel-prod.bat` 的流程：探测 8010 → 没起来就
`docker compose --env-file .env.production up -d db app` → 轮询 `/health` 最多 180s →
交给 `start-tunnel.bat` 拿到公网 URL。它**只起 db + app**（app 仅依赖 db）；
vllm / prometheus / grafana / cadvisor / mcp 等较重的服务不会被拉起，需要时自行启动。
需要 Docker Desktop 已在运行。

> **端口提醒**：`docker compose ps` 里 app 容器发布在宿主 **127.0.0.1:8010**，
> 而 `start-tunnel.bat` 默认是 **8000**（那是 `make dev` 的 uvicorn 监听口）。
> 想暴露生产容器就显式传端口：`start-tunnel.bat 8010`。

桌面隧道首次运行会自动下载 `cloudflared.exe`（约 53MB，走 gh-proxy 镜像，速度约
150–240 KB/s，请耐心等）。脚本会用 `cloudflared.exe --version` 校验二进制是否可用，
损坏会自动删除并重下；下载过程请勿关闭窗口。

```bash
# 快速隧道（随机域名，重启失效）：
docker compose --env-file .env.production --profile tunnel up cloudflared
docker compose logs cloudflared | grep trycloudflare   # 拿到 https://*.trycloudflare.com

# 命名隧道（持久域名）：
# 1. https://one.dash.cloudflare.com → Networks → Tunnels 创建隧道，拿 token
# 2. .env.production 设置 CLOUDFLARE_TUNNEL_TOKEN=<token>
# 3. 编辑 docker-compose.yml cloudflared.command 为：
#    tunnel run --token ${CLOUDFLARE_TUNNEL_TOKEN}
# 4. Cloudflare 控制台绑定子域名 → http://app:8000
# 5. docker compose --profile tunnel up -d cloudflared
```

> ⚠️ **`tunnel/*.bat` 必须保持 CRLF 换行。** cmd.exe 解析 LF-only 的 .bat 会逐行吃掉
> 开头字符（`setlocal` → `tlocal`、`echo` → `ho`），含 `if (...) else (...)` 括号块的
> 脚本会整体失效，报一片 "不是内部或外部命令"。`.gitattributes` 已强制
> `*.bat text eol=crlf`；用编辑器或 AI 改写后，请确认换行仍是 CRLF。
>
> 另外：**请双击运行，不要在 Git Bash 里跑**。Git Bash 的 `find` / `timeout` 会盖过
> Windows 同名命令，导致 `find: '/i': No such file or directory` 之类噪声，等待循环也会
> 退化（`timeout /t 3` 报 invalid time interval）。在 Explorer 里双击不受影响。

同源访问，`ALLOWED_ORIGINS` 无需加入隧道域名。

## 5. Streamlit UI（可选回退）

旧 Streamlit UI 已标记为 `profiles: ["streamlit"]`，默认不启动。如需启用：

```bash
docker compose --env-file .env.production --profile streamlit up -d ui
# 访问 http://localhost:8501
```

## 6. 已知限制 / 后续

- **PWA 图标**：当前仅提供 `icon.svg`；部分浏览器要求 PNG（192/512）才允许"安装到主屏幕"。补 PNG 后在 `manifest.json` `icons` 追加条目。
- **token 存储**：session token 存 localStorage（MVP 取舍，存在 XSS 风险）。后续可升级为 httpOnly cookie + CSRF token。
- **markdown 渲染**：`vendor/md.js` 为最小实现；如需完整 CommonMark，替换为 `marked.min.js`。
- **路由**：使用 hash 路由（`/#/investor`）；如需 history API 路由，需改用 `SPAStaticFiles` 回退。
