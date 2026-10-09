#!/bin/bash
set -e

# Print initial environment values (before loading .env)
echo "Starting with these environment variables:"
echo "APP_ENV: ${APP_ENV:-development}"
echo "Initial Database Host: $( [[ -n ${POSTGRES_HOST:-${DB_HOST:-}} ]] && echo 'set' || echo 'Not set' )"
echo "Initial Database Port: $( [[ -n ${POSTGRES_PORT:-${DB_PORT:-}} ]] && echo 'set' || echo 'Not set' )"
echo "Initial Database Name: $( [[ -n ${POSTGRES_DB:-${DB_NAME:-}} ]] && echo 'set' || echo 'Not set' )"
echo "Initial Database User: $( [[ -n ${POSTGRES_USER:-${DB_USER:-}} ]] && echo 'set' || echo 'Not set' )"

# Load environment variables from the appropriate .env file
if [ -f ".env.${APP_ENV}" ]; then
    echo "Loading environment from .env.${APP_ENV}"
    while IFS= read -r line || [[ -n "$line" ]]; do
        # Skip comments and empty lines
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "$line" ]] && continue

        # Extract the key
        key=$(echo "$line" | cut -d '=' -f 1)

        # Only set if not already set in environment
        if [[ -z "${!key}" ]]; then
            export "$line"
        else
            echo "Keeping existing value for $key"
        fi
    done <".env.${APP_ENV}"
elif [ -f ".env" ]; then
    echo "Loading environment from .env"
    while IFS= read -r line || [[ -n "$line" ]]; do
        # Skip comments and empty lines
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "$line" ]] && continue

        # Extract the key
        key=$(echo "$line" | cut -d '=' -f 1)

        # Only set if not already set in environment
        if [[ -z "${!key}" ]]; then
            export "$line"
        else
            echo "Keeping existing value for $key"
        fi
    done <".env"
else
    echo "Warning: No .env file found. Using system environment variables."
fi

# Check required sensitive environment variables.
#
# 2026-10-06: the single OPENAI_API_KEY was split into two role-named keys
# (see .env.production "LLM Settings — 双 provider 拆分"):
#   CHAT_API_KEY      -> chat / LLM provider        (e.g. DeepSeek)
#   EMBEDDING_API_KEY -> /embeddings provider       (e.g. Alibaba Bailian)
# OPENAI_API_KEY is kept as a legacy single-provider fallback: if it is set,
# it implicitly covers both (config.py falls back the same way).
required_vars=("JWT_SECRET_KEY" "CHAT_API_KEY" "EMBEDDING_API_KEY")

# Support Docker secrets _FILE convention: if VAR is empty but VAR_FILE
# points to a readable file, load VAR from that file. OPENAI_API_KEY is
# included so its legacy _FILE form still resolves.
file_backed_vars=("CHAT_API_KEY" "EMBEDDING_API_KEY" "OPENAI_API_KEY")
for var in "${file_backed_vars[@]}"; do
    file_var="${var}_FILE"
    if [[ -z "${!var}" && -n "${!file_var}" && -f "${!file_var}" ]]; then
        export "$var=$(cat "${!file_var}")"
        echo "Loaded $var from ${!file_var}"
    fi
done

missing_vars=()

for var in "${required_vars[@]}"; do
    if [[ -z "${!var}" ]]; then
        missing_vars+=("$var")
    fi
done

# Legacy single-provider fallback: OPENAI_API_KEY covers both role-named keys.
if [[ -n "${OPENAI_API_KEY:-}" ]]; then
    missing_vars=()
fi

if [[ ${#missing_vars[@]} -gt 0 ]]; then
    echo "ERROR: The following required environment variables are missing:"
    for var in "${missing_vars[@]}"; do
        echo "  - $var"
    done
    echo "Provide them via Docker secrets (CHAT_API_KEY_FILE / EMBEDDING_API_KEY_FILE),"
    echo "the CHAT_API_KEY / EMBEDDING_API_KEY env vars, or the legacy OPENAI_API_KEY."
    exit 1
fi

# Print final environment info
echo -e "\nFinal environment configuration:"
echo "Environment: ${APP_ENV:-development}"

echo "Database Host: $( [[ -n ${POSTGRES_HOST:-${DB_HOST:-}} ]] && echo 'set' || echo 'Not set' )"
echo "Database Port: $( [[ -n ${POSTGRES_PORT:-${DB_PORT:-}} ]] && echo 'set' || echo 'Not set' )"
echo "Database Name: $( [[ -n ${POSTGRES_DB:-${DB_NAME:-}} ]] && echo 'set' || echo 'Not set' )"
echo "Database User: $( [[ -n ${POSTGRES_USER:-${DB_USER:-}} ]] && echo 'set' || echo 'Not set' )"

echo "LLM Model: ${DEFAULT_LLM_MODEL:-Not set}"
echo "Chat provider base URL: ${CHAT_BASE_URL:-${OPENAI_BASE_URL:-Not set}}"
echo "Embedding provider base URL: ${EMBEDDING_BASE_URL:-${OPENAI_BASE_URL:-Not set}}"
echo "Debug Mode: ${DEBUG:-false}"

# Run database migrations on every boot. `alembic upgrade head` is
# idempotent (a no-op when already at head), so this is safe for
# restarts. Fail-fast: if migrations fail, `set -e` exits the entrypoint
# and the container restarts (restart: on-failure) instead of starting
# uvicorn against a stale/empty schema. Set SKIP_MIGRATIONS=1 to bypass
# (e.g. for a sidecar one-off command, or when a DBA applied migrations
# out-of-band).
if [ "${SKIP_MIGRATIONS:-0}" != "1" ]; then
    echo "Running database migrations (alembic upgrade head)..."
    /app/.venv/bin/alembic upgrade head
    echo "Database migrations completed successfully."
else
    echo "SKIP_MIGRATIONS=1 set, skipping database migrations."
fi

# Execute the CMD
exec "$@"
