.PHONY: install fmt lint typecheck test check diagrams docker-build \
	docker-up-dev docker-up-prod docker-down-dev docker-down-prod

install:
	uv sync

fmt:
	uv run ruff format src/ tests/

lint:
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/

typecheck:
	uv run pyright

test:
	uv run pytest

check: lint typecheck test

# Regenerate the architecture & domain diagrams under docs/generated/.
# Run after changing code/models and commit the result; CI fails if the
# committed diagrams drift from the code (see .github/workflows/ci.yml).
diagrams:
	uv run pytest tests/guardrail/ -q

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
