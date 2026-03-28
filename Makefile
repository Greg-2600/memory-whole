.PHONY: help install dev test lint format docker-up docker-down run dashboard clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## Install runtime dependencies
	pip install -r requirements.txt

dev: ## Install all dependencies (runtime + dev)
	pip install -r requirements.txt -r requirements-dev.txt

test: ## Run the test suite
	python -m pytest tests/ -v

lint: ## Run linters (ruff + black --check)
	python -m ruff check .
	python -m black --check .

format: ## Auto-format code with black
	python -m black .

run: ## Run the full pipeline (fetch → cluster → dashboard)
	python rss_reader.py --max-items 250

dashboard: ## Regenerate dashboard from existing DB
	python rss_reader.py --dashboard-only

docker-up: ## Build and start the Docker container
	docker compose up -d --build

docker-down: ## Stop the Docker container
	docker compose down

clean: ## Remove caches and temp files
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache
