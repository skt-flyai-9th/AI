.PHONY: install test lint migrate up down logs run sync check-apis export

install:
	python -m pip install -e ".[dev]"

migrate:
	alembic upgrade head

run:
	uvicorn app.main:app --reload

sync:
	python -m app.cli run-ranking

check-apis:
	python scripts/check_apis.py

export:
	python -m app.cli export-ranking

test:
	pytest

lint:
	ruff check .

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f api worker beat
