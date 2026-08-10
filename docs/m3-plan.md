# M3 — Analysis & Design: build plan

**Goal.** A request like *"Read MPU6050 over SPI with DMA on STM32F407"* produces a
reviewed, structured **design** — requirements, hardware facts with citations, and a
firmware architecture — with no code generation yet. M4 consumes that design.

**Definition of done.** `POST /projects` → `GET /projects/{id}` returns four completed
tasks (`router`, `requirements`, `datasheet`, `architecture`), each with a
JSON `result`, and the architecture names modules, peripherals, DMA streams, file
layout and an implementation order.

**Not in scope.** Code generation (M4), CubeMX `.ioc` generation (M4), a real
document corpus (deferred, see `knowledge-base.md`).

---

## Step 1 — Freeze the state contract

`orchestrator/contracts.py` holds Pydantic models that agents exchange;
`orchestrator/state.py` becomes typed rather than a bag of strings.

| Model | Producer | Consumer |
|---|---|---|
| `Requirements` | requirements | datasheet, architecture, M4 |
| `HardwareFindings` | datasheet | architecture |
| `Architecture` | architecture | M4 |

Every model carries `schema_version`. When M4 changes a field, old rows in
`TaskRun.result` stay readable instead of silently mis-parsing.

**Why first:** two agents written against different assumptions is the most
expensive mistake available here, and it only shows up at integration time.

## Step 2 — Requirements Agent

Free text (Persian or English) → `Requirements`: MCU and family, peripherals,
protocols, RTOS, constraints, deliverables, **assumptions**, **open questions**.

Rules:
- Never invent a part number. Unknown → an assumption or an open question.
- Ambiguity is recorded, not resolved. The pipeline does not stall on it.
- Structured output is parsed defensively; a malformed reply degrades to a
  minimal requirements object rather than failing the run.

## Step 3 — Datasheet Agent in the graph

Already written and tested (M2). Here it stops being an isolated endpoint:
it takes the peripherals from `Requirements`, asks one focused question per
peripheral, and returns `HardwareFindings` with citations.

With an empty knowledge base it returns zero findings and the run continues —
that is the designed degradation, not a bug.

## Step 4 — Architecture Agent

`Requirements` + `HardwareFindings` → `Architecture`: layers, modules, HAL vs LL,
transfer mode (polling / interrupt / DMA) with a stated reason, peripheral and DMA
stream assignments, file tree, implementation order, risks.

Rules:
- Every hardware claim reuses a datasheet citation. No citation → mark it an
  assumption.
- The implementation order must be buildable step by step; M4 walks it directly.

## Step 5 — Router becomes the first graph node

Today classification happens inside the `POST /projects` handler, so a slow
provider holds the HTTP request open, and the router is invisible in progress.

After: the API stores the request and returns immediately; the router runs as node
one and writes `request_type` into state. `_route_entry` and `agent_sequence_for`
collapse into a single source of truth — right now they encode the same routing
decision twice, which is a bug waiting for the first divergence.

Since the request type is unknown at enqueue time, the API pre-creates only the
`router` task; the worker adds the rest once the type is known.

## Step 6 — Tests, no live services

Fake LLM + `MockTransport` PageVault, as in `test_rag.py`.

- contract round-trips, including `schema_version`
- requirements parsing: clean JSON, fenced JSON, prose (degradation)
- datasheet fan-out; empty knowledge base still completes
- architecture consumes citations; uncited claims become assumptions
- routing table: one entry point per request type
- full graph over a stubbed pipeline: four nodes, correct order

---

## Order and rationale

1. contracts → 2. requirements → 3. datasheet wiring → 4. architecture →
5. router/graph → 6. tests

Contracts first because everything references them. Router last because it is a
refactor of working code, and refactoring while the shape of the pipeline is still
moving means doing it twice.

## Carried debt (tracked, deliberately not done here)

- Knowledge-base smoke test with real documents — **blocks M4 sign-off**, not M3.
- Retrieval eval set and embedding-model choice — before the corpus is bulk ingested.
- `attempt` column on `TaskRun` — needed by the M5 repair loop.
- Alembic migrations — needed before there is data worth keeping.
