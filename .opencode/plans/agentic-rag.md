# Agentic RAG Q&A — implementation plan

Branch: `agentic-rag` (already created from master).
Goal: a conversational, agentic Q&A feature over the existing PageVault knowledge base — the model plans its own retrieval (bounded search loop), then streams a cited answer. Backend + minimal Next.js chat UI. SSE delivery.

Decisions already made with the user:
- Documents: existing PageVault KB (no upload feature).
- Agentic depth: true LLM tool loop (query planning + iterative retrieval).
- Scope: backend API + frontend chat UI.
- Delivery: SSE streaming.
- Tool protocol: JSON action protocol (text-based, like `request_contract`), NOT native function-calling — works with any OpenAI-compatible provider incl. local Ollama models, and reuses the repo's repair machinery.

## Architecture

Per turn, two phases:

1. **Decision loop** (non-streaming, `AgentLLM.chat` via `request_contract`): the model sees the question + compact summaries of what was retrieved so far, and replies either `{"action": "search", "query": "..."}` or `{"action": "ready"}`. Bounded by `chat_max_searches`. If the KB goes down mid-loop, stop searching. If the model plans zero searches, fall back to one direct search with the raw question (retrieve-before-prompting doctrine).
2. **Answer phase** (streaming, new `AgentLLM.stream`): plain-text answer with inline `[path:line-line]` citations, grounded in the accumulated contexts; falls back to an "unverified" prompt variant when nothing was retrieved.

Agent never raises: KB down / LLM failure / malformed protocol all degrade to a `done` event with warnings.

## Changes (7 files new, 6 edited)

### 1. `backend/app/core/config.py` (edit)

Add after the RAG section:

```python
# --- Agentic document chat (agentic RAG) ---
# The chat agent plans its own retrieval: per question it may run at most
# this many searches before it has to answer with what it has collected.
chat_max_searches: int = 3
```

### 2. `backend/app/core/llm.py` (edit)

- Add `from collections.abc import AsyncIterator` to imports.
- Add `stream()` method to `AgentLLM` (additive; `chat()` untouched):

```python
    async def stream(self, messages: list[dict], **kwargs) -> AsyncIterator[str]:
        """Yield the reply token by token.

        Only the final answer of a chat turn is streamed; decision steps
        stay on chat() so a malformed JSON action never reaches the UI as
        half-rendered text.
        """
        if settings.llm_max_tokens and "max_tokens" not in kwargs:
            kwargs["max_tokens"] = settings.llm_max_tokens
        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
            **kwargs,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
```

### 3. `backend/app/db/models.py` (edit)

Append two tables:

```python
class Conversation(SQLModel, table=True):
    """A multi-turn chat session with the knowledge base (agentic RAG)."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    # Set from the first user message when the client sends no title.
    title: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Message(SQLModel, table=True):
    """One chat turn. `payload` carries agent metadata (citations, warnings,
    searches) as JSON for assistant messages; NULL for user messages."""

    # Integer PK on purpose: chat turns are read in insertion order and a
    # uuid does not give that.
    id: int | None = Field(default=None, primary_key=True)
    conversation_id: str = Field(foreign_key="conversation.id", index=True)
    role: str  # user | assistant
    content: str
    payload: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
```

### 4. `backend/alembic/versions/0003_chat.py` (new)

```python
"""Agentic RAG chat: conversations and messages."""

from alembic import op
import sqlalchemy as sa

revision = "0003_chat"
down_revision = "0002_p3_cubemx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "message",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("payload", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversation.id"]),
    )
    op.create_index("ix_message_conversation_id", "message", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_message_conversation_id", table_name="message")
    op.drop_table("message")
    op.drop_table("conversation")
```

### 5. `backend/app/agents/__init__.py` (edit)

Add `"chat"` to `KNOWN_AGENTS` (after `"datasheet"`) so per-agent model override/enable works for free.

### 6. `backend/app/agents/qa.py` (new — the core agent)

- `AGENT_NAME = "chat"`, `MAX_HISTORY_MESSAGES = 10`.
- `_SearchAction(BaseModel)`: `action: Literal["search", "ready"]`, `query: str = ""` — parsed via `request_contract` (repair-once for free).
- Prompts:
  - `_DECISION_SYSTEM_PROMPT`: retrieval planner; one focused topic per query; prefer concrete identifiers; search again only when evidence is insufficient; reply ONLY the JSON action.
  - `_ANSWER_SYSTEM_PROMPT`: adapted from datasheet's — cite every claim with exact bracketed reference, copy registers verbatim, say what's missing instead of guessing, reply in the user's language.
  - `_NO_CONTEXT_PROMPT`: adapted unverified-answer variant for when nothing was retrieved.
- `detect_family` over question + recent history text (follow-up questions like "that timer" resolve the family from earlier turns).
- `_summarize_results(query, context)`: compact citation/name list for the decision loop (full text only goes to the answer phase).
- `answer_with_search(question, *, history=None, family=None) -> AsyncIterator[dict]` yields events in order:
  - `{"type": "search", "query", "index", "max"}`
  - `{"type": "search_result", "query", "available", "citations", "sources": {symbols, types, chunks, pages}, "warnings"}`
  - `{"type": "delta", "text"}` (answer tokens)
  - `{"type": "done", "answer", "citations", "cited", "grounded", "verified", "searches", "warnings", "failed"}`
- Loop details:
  - Decision conversation = `[system, *trimmed_history, user question]`; after each search append `assistant: {"action": "search", "query": ...}` + `user: _summarize_results(...)` so the planner sees what came back.
  - `context.available == False` → break (further searches will fail identically).
  - `ContractError` after retries → warning, break.
  - Zero searches planned → one fallback search with the raw question.
  - Answer phase: contexts rendered via `as_prompt()` with a per-context share of `rag_context_max_chars` (split evenly) so 3 searches don't triple the prompt; no-context variant when nothing grounded; `llm.stream(..., temperature=0)`, accumulate + re-yield deltas.
  - Answer-phase exception → warning + `failed: True`, partial answer kept.
  - `cited` = citations appearing in the answer; "sources retrieved but none cited" warning (same as datasheet agent).

### 7. `backend/app/api/routes/chat.py` (new)

`router = APIRouter(prefix="/chat", tags=["chat"])`:

- `POST /chat/conversations` — create (optional title).
- `GET /chat/conversations` — list, ordered by `updated_at desc`.
- `GET /chat/conversations/{id}` — summary + messages ordered by `Message.id`.
- `DELETE /chat/conversations/{id}` — delete messages + conversation.
- `POST /chat/conversations/{id}/messages` — body `{content, family?}`:
  - 404 if conversation missing; persist user Message + set title from first message + bump `updated_at` **before** streaming (the question survives an agent failure).
  - Returns `StreamingResponse(media_type="text/event-stream", headers={Cache-Control: no-cache, X-Accel-Buffering: no})`.
  - SSE frames: `data: {json}\n\n` per agent event; unexpected agent exception → `{"type": "error", ...}` frame.
  - On `done`: persist assistant Message (content = answer, payload = JSON of citations/cited/grounded/verified/searches/warnings) + bump conversation `updated_at`, using a **fresh `Session(engine)`** inside the generator (request dependency may close before a long stream ends).

### 8. `backend/app/main.py` (edit)

Import and include `chat.router`.

### 9. `backend/tests/test_p3_api.py` (edit)

`test_existing_pre_alembic_database_is_stamped_then_upgraded`: head moved — change `assert revision == "0002_p3_cubemx"` → `"0003_chat"`.

### 10. `backend/tests/test_chat_agent.py` (new)

FakeLLM with scripted `chat` replies + `stream` chunks (records calls); MockTransport PageVault handler with a canned unified-search response (compact local copy, not imported from test_rag). Plain `asyncio.run` style. Cases:

1. Model plans one search then ready → event order `[search, search_result, delta..., done]`; `done.cited == ["RM0090.md:10-60"]`, `grounded` True, searches recorded; decision conversation got the summary feedback.
2. Model says ready immediately → fallback single search with the raw question.
3. Model always wants to search → bounded (`monkeypatch.setattr(settings, "chat_max_searches", 2)` → exactly 2 searches) and still answers.
4. KB unreachable → `available: False` search_result, unverified answer (answer-phase system prompt is the no-context variant), warnings, `grounded` False.
5. Malformed decision reply then valid one → repair works, warning recorded.
6. Decision always malformed → planning-failure warning, fallback search, done still emitted.

### 11. `backend/tests/test_chat_api.py` (new)

- Fresh SQLite via `upgrade_database` → `conversation` + `message` tables exist.
- Full flow with `TestClient(app)` + dependency override for `get_session` + `monkeypatch` of `chat.answer_with_search` (fake async generator) and `chat.engine` (sqlite engine):
  - create conversation → POST message via `client.stream(...)` → SSE body contains search/delta/done frames → GET conversation shows user + assistant messages with citation payload → list reflects updated_at; DELETE works.

### 12. Frontend

- `frontend/app/chat/page.tsx` (new): "use client" page, RTL Persian, matching dashboard conventions (plain `fetch`, no libs):
  - Sidebar: conversations list + "گفتگوی جدید" button + delete per conversation.
  - Message list: user/assistant bubbles (`white-space: pre-wrap`), citation chips (from payload or done event), warnings in error style.
  - In-flight turn: search chips ("جستجو: …") as they arrive, answer text growing from deltas.
  - SSE via `fetch` POST + `response.body.getReader()`, buffer split on `\n\n`, parse `data: ` lines.
  - On done: append final assistant message, clear live state, refresh conversation list (title/updated_at).
- `frontend/app/page.tsx` (edit): add nav link `گفتگو با مستندات` → `/chat` in the header.
- `frontend/app/globals.css` (edit): append chat styles (`.chat-columns`, `.conversation-item`, `.message-list`, `.bubble-user`/`.bubble-assistant`, `.search-chip`, `.citation-chip`, `.chat-form`, `.nav-link`) reusing existing color tokens (#171a21, #2a2f3a, #4a6cf7, …).

## Verification

1. `cd backend && .venv/bin/python -m pytest tests -q` — all green (except the 2 pre-existing environmental proxy failures in test_fixes.py).
2. Frontend: `npm run lint` / `npm run build` if scripts exist in `frontend/package.json`.
3. Manual smoke (optional, user-run): `docker compose up backend` + `make kb-up`, then POST to `/chat/conversations` and stream a message.

## Commit

Single commit on `agentic-rag`, message matching repo style:
`agentic rag: chat agent with planned retrieval, SSE chat API + UI`
Not pushed unless asked.
