.DEFAULT_GOAL := help

DOCKER_COMPOSE ?= docker-compose
ENV            ?= production
VALID_ENVS     := development staging production test

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
define check_env
	@$(if $(filter $(ENV),$(VALID_ENVS)),,echo Invalid ENV=$(ENV). Must be one of: $(VALID_ENVS) && exit 1)
endef

define load_env_file
	$(call check_env)
	@$(if $(wildcard .env.$(ENV)),,echo Environment file .env.$(ENV) not found. Please create it. && exit 1)
endef

# Shorthand: source env vars then run a command
run_with_env = bash -c "source scripts/set_env.sh $(ENV) && $(1)"

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
install:
	pip install uv
	uv sync
	uv run pre-commit install

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
dev:
	@$(call run_with_env,uv run uvicorn app.main:app --reload --port 8000)

staging:
	@$(call run_with_env,$(MAKE) _serve ENV=staging)

prod:
	@$(call run_with_env,$(MAKE) _serve ENV=production)

_serve:
	@$(call run_with_env,./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --loop uvloop)

# ---------------------------------------------------------------------------
# Database migrations
# ---------------------------------------------------------------------------
migrate:
	@$(call run_with_env,uv run alembic upgrade head)

migration:
	@if [ -z "$(MSG)" ]; then \
		echo "Usage: make migration MSG=\"describe your change\""; exit 1; \
	fi
	@$(call run_with_env,uv run alembic revision --autogenerate -m '$(MSG)')

migrate-downgrade:
	@$(call run_with_env,uv run alembic downgrade -1)

migrate-history:
	@$(call run_with_env,uv run alembic history --verbose)

# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
eval:
	@$(call run_with_env,python -m evals.main --interactive)

eval-quick:
	@$(call run_with_env,python -m evals.main --quick)

eval-no-report:
	@$(call run_with_env,python -m evals.main --no-report)

# ---------------------------------------------------------------------------
# Code quality
# ---------------------------------------------------------------------------
lint:
	uv run ruff check .

format:
	uv run ruff format .

typecheck:
	uv run pyright

check: lint typecheck
	@echo "All checks passed"

test:
	uv run pytest tests/ -v --tb=short

test-e2e:
	@$(call run_with_env,python evals/sdlc_evals.py)

ui:
	uv run streamlit run ui/app.py --server.port 8501

# Build and run only the UI container (requires `make docker-up` first so the
# backend API service is reachable at http://app:8000 inside the compose network).
ui-up:
	$(call load_env_file)
	@$(DOCKER_COMPOSE) --env-file .env.$(ENV) up -d --build ui

ui-down:
	$(call load_env_file)
	@$(DOCKER_COMPOSE) --env-file .env.$(ENV) stop ui

ui-logs:
	$(call load_env_file)
	@$(DOCKER_COMPOSE) --env-file .env.$(ENV) logs -f ui

pre-commit:
	uv run pre-commit run --all-files

pre-commit-update:
	uv run pre-commit autoupdate

# ---------------------------------------------------------------------------
# Docker — single service (API + DB)
# ---------------------------------------------------------------------------
docker-build:
	$(call check_env)
	@./scripts/build-docker.sh $(ENV)

docker-up:
	$(call load_env_file)
	@$(DOCKER_COMPOSE) --env-file .env.$(ENV) up -d --build db app

docker-down:
	$(call load_env_file)
	@$(DOCKER_COMPOSE) --env-file .env.$(ENV) down

docker-logs:
	$(call load_env_file)
	@$(DOCKER_COMPOSE) --env-file .env.$(ENV) logs -f app db

# Run Alembic migrations inside the running app container (where "db" resolves).
# Requires the stack to be up first (make docker-up).
docker-migrate:
	$(call load_env_file)
	@$(DOCKER_COMPOSE) --env-file .env.$(ENV) exec -T app /app/.venv/bin/alembic upgrade head

# Roll back the last migration inside the running app container.
docker-migrate-downgrade:
	$(call load_env_file)
	@$(DOCKER_COMPOSE) --env-file .env.$(ENV) exec -T app /app/.venv/bin/alembic downgrade -1

# Show migration history inside the running app container.
docker-migrate-history:
	$(call load_env_file)
	@$(DOCKER_COMPOSE) --env-file .env.$(ENV) exec -T app /app/.venv/bin/alembic history --verbose

# ---------------------------------------------------------------------------
# Docker — full stack (API + DB + Prometheus + Grafana)
# ---------------------------------------------------------------------------
stack-up:
	$(call load_env_file)
	@$(DOCKER_COMPOSE) --env-file .env.$(ENV) up -d

stack-down:
	$(call load_env_file)
	@$(DOCKER_COMPOSE) --env-file .env.$(ENV) down

stack-logs:
	$(call load_env_file)
	@$(DOCKER_COMPOSE) --env-file .env.$(ENV) logs -f

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
clean:
	rm -rf .venv __pycache__ .pytest_cache

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------
help:
	@echo "Usage: make <target> [ENV=production|staging|development|test] (default: production)"
	@echo ""
	@echo "Setup:"
	@echo "  install              Install deps, set up pre-commit hooks"
	@echo ""
	@echo "Server:"
	@echo "  dev                  Dev server with hot reload (port 8000)"
	@echo "  staging              Staging server"
	@echo "  prod                 Production server"
	@echo ""
	@echo "Database:"
	@echo "  migrate              Run migrations to latest (default ENV=development)"
	@echo "  migration MSG=...    Generate migration from model changes"
	@echo "  migrate-downgrade    Rollback last migration"
	@echo "  migrate-history      Show migration history"
	@echo ""
	@echo "Evaluation:"
	@echo "  eval                 Run evals (interactive)"
	@echo "  eval-quick           Run evals (default settings)"
	@echo "  eval-no-report       Run evals without report"
	@echo ""
	@echo "Code quality:"
	@echo "  lint                 Ruff lint check"
	@echo "  format               Ruff format"
	@echo "  typecheck            Pyright static type check"
	@echo "  check                Run lint + typecheck"
	@echo "  pre-commit           Run all pre-commit hooks"
	@echo "  pre-commit-update    Update pre-commit hook versions"
	@echo ""
	@echo "Docker (API + DB):"
	@echo "  docker-build         Build Docker image"
	@echo "  docker-up            Start API + DB containers"
	@echo "  docker-down          Stop containers"
	@echo "  docker-logs          Tail container logs"
	@echo "  docker-migrate       Run migrations inside the app container"
	@echo "  docker-migrate-downgrade  Roll back last migration (in container)"
	@echo "  docker-migrate-history    Show migration history (in container)"
	@echo ""
	@echo "Docker (full stack — includes Prometheus + Grafana):"
	@echo "  stack-up             Start entire stack"
	@echo "  stack-down           Stop entire stack"
	@echo "  stack-logs           Tail all service logs"
	@echo ""
	@echo "Misc:"
	@echo "  clean                Remove .venv, __pycache__, .pytest_cache"

.PHONY: install dev staging prod _serve \
        migrate migration migrate-downgrade migrate-history \
        eval eval-quick eval-no-report \
        lint format typecheck check pre-commit pre-commit-update \
        docker-build docker-up docker-down docker-logs docker-migrate \
        docker-migrate-downgrade docker-migrate-history \
        stack-up stack-down stack-logs \
        clean help
