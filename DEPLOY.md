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
