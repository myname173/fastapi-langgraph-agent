# MCP server image — pure Python, no node, no GPU.
#
# Built from the same python:3.13-slim base as the main app to keep the
# toolchain consistent. Only installs the ``mcp`` package (ships FastMCP);
# langchain-mcp-adapters lives in the *client* (main app) image, not here.
FROM python:3.13-slim

WORKDIR /app

# Install the MCP SDK only — keeps the image ~50MB.
RUN pip install --no-cache-dir "mcp>=1.2.0"

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
