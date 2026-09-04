# Path to the PageVault checkout (the knowledge base, M2). Override with:
#   make up-all PAGEVAULT_DIR=/path/to/pagevault
PAGEVAULT_DIR ?= ../pagevault
PAGEVAULT_COMPOSE = docker compose \
	-f $(PAGEVAULT_DIR)/docker-compose.yml \
	-f $(PAGEVAULT_DIR)/docker-compose.textrag.yml \
	-f $(CURDIR)/deploy/pagevault-rag.override.yml

# Host ports, read from .env so `make` and `./run.sh` always agree. Defaults
# must match the ones in docker-compose.yml.
dotenv = $(shell sed -n 's/^[[:space:]]*$(1)[[:space:]]*=//p' $(CURDIR)/.env 2>/dev/null | tail -n1)
FRONTEND_PORT  ?= $(or $(call dotenv,FRONTEND_PORT),19300)
BACKEND_PORT   ?= $(or $(call dotenv,BACKEND_PORT),19800)
PAGEVAULT_PORT ?= $(or $(call dotenv,PAGEVAULT_PORT),19100)

# The PageVault stack is loaded with `-f $(PAGEVAULT_DIR)/...`, which makes that
# directory compose's project directory -- so it reads PageVault's .env and not
# ours. Exporting is what carries our port across.
export PAGEVAULT_PORT

# Guided first run: creates rag-net, builds the toolchain image before the
# backend needs it, imports the pin tables, and checks the settings that break
# silently when ports move. Idempotent -- safe as the everyday "start" command.
start:
	./run.sh

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
	@echo "STM32 API      http://localhost:$(BACKEND_PORT)"
	@echo "Dashboard      http://localhost:$(FRONTEND_PORT)"
	@echo "PageVault API  http://localhost:$(PAGEVAULT_PORT)"

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

# Does retrieval still work *with* the family filter applied? kb-check only
# proves PageVault is up; this proves a real query comes back with something.
kb-probe:
	docker compose exec backend python -m scripts.kb_probe

# Quality numbers for the design pipeline. Needs a live LLM and PageVault.
eval:
	docker compose exec backend python -m evals.run_eval

# Build the compile sandbox (arm-none-eabi toolchain, ~1 GB image). This is
# also the step that downloads ST's HAL/CMSIS sources, so it needs internet.
# Nothing after it does.
builder-image:
	docker compose build builder

# Which drivers did the image download, and can the backend read them?
sdk-check:
	docker compose exec backend python -m scripts.sdk_check

# Refresh the drivers and the pin tables after rebuilding the image. Docker
# only copies them into the volume while it is empty, so an old volume
# quietly hides a new SDK.
#
# The volume cannot be deleted while ANY container still mounts it, and the
# backend and the worker both do. They are stopped here for that reason. The
# deletion is deliberately not prefixed with `-`: a refresh that failed but
# reported success is how a stale volume goes unnoticed until generation.
sdk-refresh:
	docker compose rm -sf builder backend worker
	docker volume rm $$(docker compose config --format json | \
		python3 -c "import json,sys; print(json.load(sys.stdin)['name'])")_cube_sdk
	docker compose up -d builder backend worker
	@echo
	@echo "Refilling the volume from the image ..."
	@sleep 6
	-@docker compose exec -T builder sh -c \
		'ls /opt/stm32cube; printf "pin table files: "; \
		 ls /opt/stm32cube/modm-devices/stm32 2>/dev/null | wc -l'

# Compile a project we know is good. If this fails, the toolchain or the
# wiring is broken -- not the generated code. Runs in CI.
golden:
	docker compose exec backend python -m scripts.build_golden

# Generate a project from a hand-written plan and compile it: the same path
# the CubeMX agent will take, without the model. If this fails, the generator
# is broken. Runs in CI.
scaffold:
	docker compose exec backend python -m scripts.build_scaffold

# Generate a project for a real board and compile it: the crystal, the pins
# and the PLL all come from the board profile and the imported pin table, so
# nothing in the plan was typed by hand. BOARD picks the profile.
BOARD ?= blackpill-f411
board:
	docker compose exec backend python -m scripts.build_board $(BOARD)

test:
	cd backend && pytest -q

lint:
	cd backend && ruff check .

# Convert the vendor pin tables into the per-part tables the planner is
# validated against, and spot-check them against known datasheet rows. Run it
# once after `make builder-image`, and again after any `make sdk-refresh`.
devices:
	docker compose exec backend python -m scripts.build_devices

.PHONY: start up down logs ollama-up test lint net kb-up kb-down kb-logs up-all down-all kb-check kb-probe eval builder-image golden scaffold sdk-check sdk-refresh devices board
