.PHONY: setup up down migrate seed test lint typecheck scan eval eval-rules bench demo api scheduler bot

setup:  ## Install dependencies
	uv sync

up:  ## Start Postgres only (port 5435)
	docker compose up -d postgres

down:  ## Stop the services (data volume is kept)
	docker compose down

migrate:  ## Apply database migrations (as the migrator role) and create the checkpoint tables
	uv run python -m app.db.migrate
	uv run python -c "from app.agent.run import setup_checkpointer; setup_checkpointer()"

test:  ## Unit and integration tests (no live API calls, no cost)
	uv run pytest -q

lint:  ## Style checks
	uv run ruff check .
	uv run ruff format --check .

typecheck:  ## Basic type checking
	uv run mypy app evals

scan:  ## Secrets and personal-data scan (add --history before any push)
	uv run python scripts/secrets_scan.py

eval-rules:  ## The scenarios decided by code (emergency, STOP): no model, $0
	uv run python -m evals.run --rules-only --yes

eval:  ## All 22 scenarios x 3 runs with the real model (~$0.20; asks before spending above $0.50)
	uv run python -m evals.run --runs 3 --label eval

bench:  ## The experiments in docs/benchmarks.md (E2 caching, E4 compaction, E5 supervisor); ~$0.60 in total
	AGENT_MODE=supervisor uv run python -m evals.run --runs 3 --yes --label E5-supervisor
	LLM_PROMPT_CACHING=true uv run python -m evals.run --runs 3 --yes --label E2-caching
	AGENT_COMPACTION_ENABLED=false uv run python -m evals.run --runs 3 --yes --only long_thread_compaction --label E4-no-compaction
	uv run python -m evals.run --runs 3 --yes --only long_thread_compaction --label E4-compaction

demo:  ## Real server, signed webhooks, a restart mid-conversation, approval, emergency (~$0.01, dry-run)
	uv run python -m scripts.demo

api:  ## The API on :8003
	uv run uvicorn app.api.main:create_app --factory --port 8003

scheduler:  ## Follow-up tick, message delivery, CRM sync
	uv run python -m app.scheduler.main

bot:  ## Telegram owner bot (long polling; sends only if TELEGRAM_LIVE=true)
	uv run python -m app.scheduler.telegram_bot
