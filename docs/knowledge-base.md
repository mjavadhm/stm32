# Knowledge base (M2) — PageVault

Retrieval is **not** implemented in this repo. It is provided by
[PageVault](https://github.com/mjavadhm/pagevault), a standalone open-source
service with its own database, migrations and release cycle. This project is
one of its consumers.

## Why two stacks instead of one\n
A single `docker compose` file for both projects looks convenient and breaks
quickly:

| Resource | STM32 | PageVault | Conflict |
|---|---|---|---|
| host port 8000 | `backend` | `api` | direct collision |
| `postgres` | `16-alpine`, user `stm32ai` | `16`, user `pagevault` | same service name, different credentials |
| `redis` | `redis://redis:6379/0` | `redis://redis:6379/0` | **shared Celery broker** |
| `qdrant` | pinned `v1.12.4` | `latest` | version drift |
| volumes | `pgdata`, `qdrant_data` | `pgdata`, `qdrant_data` | same names |
| `.env` | `POSTGRES_USER=stm32ai` | `POSTGRES_USER=pagevault` | one file, two meanings |

The Redis row is the dangerous one. Both projects run a Celery worker on the
default `celery` queue. Sharing one broker means the STM32 worker picks up
PageVault tasks and dies with `Received unregistered task`, intermittently and
in both directions. (TextRAG's worker uses `-Q textrag` and is unaffected;
this repo's worker now also states `-Q celery` explicitly.)

Beyond that: PageVault must stay independently usable, its visual worker
competes with Ollama for GPU memory, and a shared database would let a
PageVault migration break this project.

**So: two stacks, one network, HTTP only.**

```
┌──────────────────────────┐         ┌───────────────────────────┐
│ stm32-ai-assistant       │         │ pagevault                 │
│  backend  :19800 → 8000  │         │  api      :19100 → 8000   │
│  worker                  │ ──────▶ │  textrag-worker           │
│  postgres redis qdrant   │ rag-net │  embeddings :7997         │
│  frontend :19300 → 3000  │  HTTP   │  postgres redis qdrant    │
└──────────────────────────┘         └───────────────────────────┘
```

Only the host side moved to 19xxx (see the "Host ports" block in
`.env.example`). Container-internal ports are untouched, so `PAGEVAULT_URL`
stays `http://pagevault-api:8000` regardless of what the host publishes.

## Running both

```bash
./run.sh                          # creates rag-net, starts PageVault, then this stack
# or, by hand:
make up-all                       # knowledge base, then this project
# or: make up-all PAGEVAULT_DIR=/path/to/pagevault
```

`deploy/pagevault-rag.override.yml` is applied as the last `-f` layer. It only
adds the network, the `pagevault-api` alias and a host port remap, so the
PageVault repo itself is never modified and still runs standalone.

Because that first `-f` points into the PageVault checkout, Compose treats *that*
directory as the project directory and reads *its* `.env` — not ours. So
`PAGEVAULT_PORT` has to reach it through the environment; both the Makefile and
`run.sh` export it for exactly that reason.

Verify the link:

```bash
make kb-check
curl localhost:19800/rag/health
```

## Using it from an agent

```python
from app.rag import get_rag_client

context = await get_rag_client().search(
    "How do I use HAL_SPI_Transmit with DMA?",
    family="STM32F4",
)
prompt = context.as_prompt()     # symbols first, then types, then prose
sources = context.citations()    # ["stm32f4xx_hal_spi.c:1420-1490", ...]
```

`app/agents/datasheet.py` is the reference implementation. Three rules every
agent should copy from it:

1. **Retrieve before prompting.** Never let the model answer a hardware
   question from memory.
2. **Carry citations through.** Every snippet keeps its path and line range;
   the prompt shows them and the model is told to quote them back.
3. **Degrade, never fail.** If PageVault is down or slow, `search()` returns
   an empty context with `available=False`, the agent switches to a prompt
   that labels its own output unverified, and the pipeline keeps running.

Try it without any pipeline:

```bash
curl -X POST localhost:19800/rag/search -H 'content-type: application/json' \
  -d '{"query": "DMA registers on STM32F407"}'

curl -X POST localhost:19800/rag/ask -H 'content-type: application/json' \
  -d '{"question": "How do I use HAL_SPI_Transmit with DMA on STM32F407?"}'
```

`/rag/search` runs retrieval with no LLM — use it to tell a retrieval problem
apart from a generation problem.

## Chip-family filtering

An F4 question must not be answered from an F1 reference manual. The family is
detected from the request (`detect_family`) and passed as an ordinary metadata
filter (`text_filters: {"family": "STM32F4"}`).

This is deliberately a plain metadata filter rather than a PageVault feature:
the knowledge base stays domain-agnostic and reusable, and the STM32-specific
knowledge lives here. When ingesting documents into PageVault, set `family` in
their metadata or the filter silently matches nothing.

## Ingesting documents

Ingestion happens on the PageVault side, against `RAG_TEXT_COLLECTION`
(default `stm32`) and `RAG_PAGE_COLLECTION` (default `stm32-manuals`). See the
PageVault README. Required metadata for this project: `family`, and `doc_type`
(`reference_manual`, `datasheet`, `app_note`, `hal_source`, `example`).

## Before scaling this up

The embedding model must be final before large-scale indexing — changing it
later means re-indexing everything, because vector dimensions differ. Build a
small evaluation set (10–20 questions with known-correct sections, the M2
acceptance criterion) and measure recall@k before committing.
