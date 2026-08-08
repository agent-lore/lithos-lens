.PHONY: install fmt lint typecheck test check diagrams metrics-history metrics-diff \
	run-fake e2e docker-build \
	docker-up-dev docker-up-prod docker-down-dev docker-down-prod

install:
	uv sync

fmt:
	uv run ruff format src/ tests/ scripts/

lint:
	uv run ruff check src/ tests/ scripts/
	uv run ruff format --check src/ tests/ scripts/

typecheck:
	uv run pyright

test:
	uv run pytest

check: lint typecheck test

# Regenerate the architecture docs, metrics, and per-component pages under
# docs/generated/. Run after changing code/models and commit the result; CI
# fails if the committed artifacts drift from the code (.github/workflows/ci.yml).
diagrams:
	uv run pytest tests/guardrail/ -q

# Print the architecture-metrics trend mined from the git history of
# docs/generated/metrics.json. FORMAT=csv|mermaid (default csv).
metrics-history:
	uv run python scripts/metrics_history.py --format $(or $(FORMAT),csv)

# Show the metrics delta between BASE (default origin/main) and the working tree.
# `set -e` + `trap` so the recipe exits with metrics_diff.py's status, not rm's.
metrics-diff:
	@set -e; tmp=$$(mktemp); trap 'rm -f $$tmp' EXIT; \
	git show $(or $(BASE),origin/main):docs/generated/metrics.json > $$tmp 2>/dev/null || echo '{}' > $$tmp; \
	uv run python scripts/metrics_diff.py $$tmp docs/generated/metrics.json

# Run the app in fake-Lithos app mode: the real UI served against in-memory
# fixtures, no Lithos server required. Open http://127.0.0.1:8000/tasks
# (override the port with LENS_PORT).
run-fake:
	LITHOS_LENS_FAKE_LITHOS=1 LITHOS_LENS_CONFIG=lithos-lens.example.toml uv run lithos-lens

# Playwright end-to-end smoke suite (see e2e/). Installs Node deps + Chromium,
# then drives the app in fake-Lithos mode. Not part of `make test`/CI's pytest
# gate — it needs Node and a browser.
e2e:
	cd e2e && npm install && npm run install-browsers && npm test

docker-build:
	docker build -t lithos-lens:dev -f docker/Dockerfile .

docker-up-dev:
	./docker/run.sh dev up

docker-up-prod:
	./docker/run.sh prod up

docker-down-dev:
	./docker/run.sh dev down

docker-down-prod:
	./docker/run.sh prod down
