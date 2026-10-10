.PHONY: setup up down migrate seed test lint typecheck eval bench demo

setup:  ## Install dependencies
	uv sync

up:  ## Start Postgres (port 5435)
	docker compose up -d

down:  ## Stop the services (data volume is kept)
	docker compose down

migrate:  ## Apply database migrations (as the migrator role)
	uv run python -m app.db.migrate

test:  ## Unit and integration tests (no live API calls, no cost)
	uv run pytest -q

lint:  ## Style checks
	uv run ruff check .
	uv run ruff format --check .

typecheck:  ## Basic type checking
	uv run mypy

eval:  ## Scenario evals (Phase 5)
	@echo "not implemented yet: Phase 5"

bench:  ## Benchmarks (Phase 5)
	@echo "not implemented yet: Phase 5"

demo:  ## Scripted demo (Phase 2)
	@echo "not implemented yet: Phase 2"
