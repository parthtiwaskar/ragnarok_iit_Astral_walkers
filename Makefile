.PHONY: setup dev up down test lint ingest e5

setup:
	cd backend && python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"
	cd frontend && npm install

dev:
	cd backend && .venv/Scripts/python -m uvicorn app.main:app --reload

up:
	docker compose up --build

down:
	docker compose down

test:
	cd backend && .venv/Scripts/python -m pytest -q

lint:
	cd backend && .venv/Scripts/python -m ruff check app tests && .venv/Scripts/python -m mypy --strict app/trial app/tools app/memory
	cd frontend && npm run build

ingest:
	cd backend && .venv/Scripts/python cli.py ingest

e5:
	cd backend && .venv/Scripts/python cli.py --llm fake --embeddings hash ask "$$(cat ../data/runemark/sample_question.txt)" --as-of 2027-01-25 --ingest
