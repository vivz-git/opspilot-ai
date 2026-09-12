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

.PHONY: lint
lint: ## Ruff + mypy
	cd backend && ruff check . && ruff format --check . && mypy app

.PHONY: fmt
fmt: ## Autoformat
	cd backend && ruff format . && ruff check --fix .

.PHONY: test
test: ## Backend test suite
	cd backend && pytest

.PHONY: test-unit
test-unit: ## Backend tests that need no database
	cd backend && pytest -m "unit or contract"

.PHONY: eval
eval: ## Run the deterministic evaluation suite
	cd backend && python -m app.evaluation.cli run --suite all

.PHONY: web-test
web-test: ## Frontend test suite
	cd frontend && npm test

.PHONY: secrets-scan
secrets-scan: ## Fail if a credential-shaped string is tracked in git
	@docker run --rm -v "$(PWD):/repo" zricethezav/gitleaks:latest detect \
	  --source=/repo --redact --no-banner

.PHONY: check
check: lint test ## Everything CI enforces on the backend
