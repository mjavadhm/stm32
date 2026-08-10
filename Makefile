# Path to the PageVault checkout (the knowledge base, M2). Override with:
#   make up-all PAGEVAULT_DIR=/path/to/pagevault
PAGEVAULT_DIR ?= ../pagevault
PAGEVAULT_COMPOSE = docker compose \
	-f $(PAGEVAULT_DIR)/docker-compose.yml \
	-f $(PAGEVAULT_DIR)/docker-compose.textrag.yml \
	-f $(CURDIR)/deploy/pagevault-rag.override.yml

up:
	docker compose up -d --build

# Create the shared network. Safe to run repeatedly.
net:
	@docker network inspect rag-net >/dev/null 2>&1 || docker network create rag-net

kb-up: net
	$(PAGEVAULT_COMPOSE) up -d

kb-down:
	$(PAGEVAULT_COMPOSE) down

kb-logs:
	$(PAGEVAULT_COMPOSE) logs -f api textrag-worker

# Both stacks, knowledge base first.
up-all: net kb-up up
	@echo "STM32 API      http://localhost:8000"
	@echo "Dashboard      http://localhost:3000"
	@echo "PageVault API  http://localhost:8100"

down-all: down kb-down

# Is the knowledge base reachable from inside the backend container?
kb-check:
	docker compose exec backend python -c "import httpx,os; \
print(httpx.get(os.environ.get('PAGEVAULT_URL','http://pagevault-api:8000')+'/health', timeout=5).text)"

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

.PHONY: up down logs ollama-up test lint net kb-up kb-down kb-logs up-all down-all kb-check
