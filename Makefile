up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f backend

ollama-up:
	docker compose --profile local up -d ollama

test:
	cd backend && pytest -q

lint:
	cd backend && ruff check .

.PHONY: up down logs ollama-up test lint
