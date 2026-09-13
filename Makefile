# ---------------------------------------------------------------------------
# OpsPilot AI — developer entrypoints. Every check CI runs is runnable here.
# ---------------------------------------------------------------------------
.DEFAULT_GOAL := help
SHELL := /bin/bash
COMPOSE := docker compose

.PHONY: help
help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: env
env: ## Create .env from .env.example if missing
	@test -f .env || (cp .env.example .env && echo "created .env — fill in secrets")

.PHONY: up
up: env ## Start database + API
	$(COMPOSE) up -d db api

.PHONY: up-full
up-full: env ## Start database + API + frontend dev server
	$(COMPOSE) --profile frontend up -d

.PHONY: down
down: ## Stop the stack (volumes preserved)
	$(COMPOSE) down

.PHONY: logs
logs: ## Tail API logs
	$(COMPOSE) logs -f api

.PHONY: migrate
migrate: ## Apply database migrations
	$(COMPOSE) exec api alembic upgrade head

.PHONY: seed
seed: ## Load the mock CRM fixture dataset
	$(COMPOSE) exec api python -m app.integrations.mock.seed

.PHONY: install
install: ## Install backend deps exactly as locked in uv.lock
	cd backend && uv sync --locked --extra dev

.PHONY: lint
lint: install ## Ruff + mypy
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy app

.PHONY: fmt
fmt: install ## Autoformat
	cd backend && uv run ruff format . && uv run ruff check --fix .

.PHONY: test
test: install ## Backend test suite
	cd backend && uv run pytest

.PHONY: test-unit
test-unit: install ## Backend tests that need no database
	cd backend && uv run pytest -m "unit or contract"

.PHONY: eval
eval: install ## Run the deterministic evaluation suite
	cd backend && uv run python -m app.evaluation.cli run --suite all

.PHONY: web-test
web-test: ## Frontend test suite
	cd frontend && npm test

.PHONY: secrets-scan
secrets-scan: ## Fail if a credential-shaped string is tracked in git
	@docker run --rm -v "$(PWD):/repo" zricethezav/gitleaks:latest detect \
	  --source=/repo --redact --no-banner

.PHONY: check
check: lint test ## Everything CI enforces on the backend
