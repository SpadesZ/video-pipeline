.PHONY: up down logs api worker smoke lint

up:
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f

api:
	docker compose up --build api

worker:
	docker compose up --build worker

smoke:
	docker compose run --rm api python scripts/smoke_test.py

lint:
	python -m compileall apps pipeline scripts

