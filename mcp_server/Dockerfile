# MCP server image — pure Python, no node, no GPU.
#
# Built from the same python:3.13-slim base as the main app to keep the
# toolchain consistent. Only installs the ``mcp`` package (ships FastMCP);
# langchain-mcp-adapters lives in the *client* (main app) image, not here.
FROM python:3.13-slim

WORKDIR /app

# Install git (hard runtime dep) + the MCP SDK.
# git_tools.py shells out to `git -C <repo> ...` via subprocess; without the
# git binary, every mcp_git_* tool fails at the subprocess layer
# ("git: command not found") even though /workspace is a valid repo — the
# MCP protocol layer still reports status=success, masking the semantic
# failure. --no-install-recommends keeps the image lean (~+35MB for git).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "mcp>=1.27.2,<1.28"

# Copy the server package.
COPY mcp_server/ ./mcp_server/

# Default runtime configuration (overridable via compose environment).
ENV MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8002 \
    MCP_WORKSPACE_ROOT=/workspace \
    MCP_DB_PATH=/data/mcp.db

EXPOSE 8002

# Seed the demo database on every start (idempotent: seed_db drops +
# recreates), then exec the server so it becomes PID 1 and receives
# SIGTERM cleanly during `docker compose down`.
CMD ["sh", "-c", "python -m mcp_server.seed_db && exec python -m mcp_server.server"]
