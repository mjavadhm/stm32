"""Tests for the chat API: conversations, SSE streaming, persistence.

The Chat Agent itself is faked here (it has its own test module); these
tests care about HTTP semantics and what lands in the database.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlmodel import Session

import app.api.routes.chat as chat_module
from app.db.session import get_session, upgrade_database
from app.main import app


async def _fake_answer(question: str, *, history=None, family=None, **kwargs):
    yield {"type": "search", "query": question, "index": 1, "max": 3}
    yield {
        "type": "search_result",
        "query": question,
        "available": True,
        "citations": ["RM0090.md:10-60"],
        "sources": {"symbols": 1, "types": 0, "chunks": 1, "pages": 0},
        "warnings": [],
    }
    yield {"type": "delta", "text": "Set TXDMAEN "}
    yield {"type": "delta", "text": "[RM0090.md:10-60]."}
    yield {
        "type": "done",
        "answer": "Set TXDMAEN [RM0090.md:10-60].",
        "citations": ["RM0090.md:10-60"],
        "cited": ["RM0090.md:10-60"],
        "grounded": True,
        "verified": True,
        "searches": [question],
        "scope": {
            "text_collection": kwargs.get("text_collection"),
            "page_collection": kwargs.get("page_collection"),
            "document_ids": kwargs.get("document_ids"),
        },
        "warnings": [],
        "failed": False,
    }


def _setup(tmp_path: Path, monkeypatch) -> TestClient:
    url = f"sqlite:///{tmp_path / 'chat.db'}"
    engine = create_engine(url)
    upgrade_database(engine, url)
    monkeypatch.setattr(chat_module, "engine", engine)
    monkeypatch.setattr(chat_module, "answer_with_search", _fake_answer)

    client = TestClient(app)

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    return client


def test_migration_creates_chat_tables(tmp_path: Path):
    url = f"sqlite:///{tmp_path / 'chat-migration.db'}"
    engine = create_engine(url)
    upgrade_database(engine, url)

    tables = set(inspect(engine).get_table_names())
    assert {"conversation", "message"} <= tables
    message_columns = {c["name"] for c in inspect(engine).get_columns("message")}
    assert {"id", "conversation_id", "role", "content", "payload"} <= message_columns


def test_a_full_turn_streams_and_persists(tmp_path: Path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)

    created = client.post("/chat/conversations", json={"title": None})
    assert created.status_code == 200
    conversation_id = created.json()["id"]
    assert created.json()["title"] == ""

    with client.stream(
        "POST",
        f"/chat/conversations/{conversation_id}/messages",
        json={"content": "How does SPI DMA work on STM32F407?"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    events = [
        json.loads(frame.removeprefix("data: "))
        for frame in body.strip().split("\n\n")
        if frame.startswith("data: ")
    ]
    assert [e["type"] for e in events] == [
        "search",
        "search_result",
        "delta",
        "delta",
        "done",
    ]

    detail = client.get(f"/chat/conversations/{conversation_id}").json()
    assert detail["title"] == "How does SPI DMA work on STM32F407?"
    roles = [m["role"] for m in detail["messages"]]
    assert roles == ["user", "assistant"]

    assistant = detail["messages"][1]
    assert assistant["content"] == "Set TXDMAEN [RM0090.md:10-60]."
    payload: dict[str, Any] = assistant["payload"]
    assert payload["cited"] == ["RM0090.md:10-60"]
    assert payload["grounded"] is True

    listed = client.get("/chat/conversations").json()
    assert [c["id"] for c in listed] == [conversation_id]

    deleted = client.delete(f"/chat/conversations/{conversation_id}")
    assert deleted.status_code == 200
    assert client.get(f"/chat/conversations/{conversation_id}").status_code == 404


def test_history_reaches_the_agent(tmp_path: Path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    conversation_id = client.post("/chat/conversations", json={}).json()["id"]

    seen: dict[str, Any] = {}

    async def fake_answer(question, *, history=None, family=None, **kwargs):
        seen["question"] = question
        seen["history"] = history
        yield {
            "type": "done",
            "answer": "ok",
            "citations": [],
            "cited": [],
            "grounded": False,
            "verified": False,
            "searches": [question],
            "scope": {
                "text_collection": kwargs.get("text_collection"),
                "page_collection": kwargs.get("page_collection"),
                "document_ids": kwargs.get("document_ids"),
            },
            "warnings": [],
            "failed": False,
        }

    monkeypatch.setattr(chat_module, "answer_with_search", fake_answer)

    for content in ("first question", "second question"):
        with client.stream(
            "POST",
            f"/chat/conversations/{conversation_id}/messages",
            json={"content": content},
        ) as response:
            assert response.status_code == 200
            "".join(response.iter_text())

    assert seen["question"] == "second question"
    assert [m["content"] for m in seen["history"]] == ["first question", "ok"]


def test_the_selected_scope_reaches_the_agent_and_is_persisted(
    tmp_path: Path, monkeypatch
):
    client = _setup(tmp_path, monkeypatch)
    conversation_id = client.post("/chat/conversations", json={}).json()["id"]

    seen: dict[str, Any] = {}

    async def fake_answer(question, *, history=None, family=None, **kwargs):
        seen.update(kwargs)
        yield {
            "type": "done",
            "answer": "ok",
            "citations": [],
            "cited": [],
            "grounded": False,
            "verified": False,
            "searches": [question],
            "scope": {
                "text_collection": kwargs.get("text_collection"),
                "page_collection": kwargs.get("page_collection"),
                "document_ids": kwargs.get("document_ids"),
            },
            "warnings": [],
            "failed": False,
        }

    monkeypatch.setattr(chat_module, "answer_with_search", fake_answer)

    with client.stream(
        "POST",
        f"/chat/conversations/{conversation_id}/messages",
        json={
            "content": "spi dma?",
            "text_collection": "code",
            "page_collection": "default",
            "document_ids": ["d1"],
        },
    ) as response:
        assert response.status_code == 200
        "".join(response.iter_text())

    assert seen == {
        "text_collection": "code",
        "page_collection": "default",
        "document_ids": ["d1"],
    }
    detail = client.get(f"/chat/conversations/{conversation_id}").json()
    assert detail["messages"][1]["payload"]["scope"] == {
        "text_collection": "code",
        "page_collection": "default",
        "document_ids": ["d1"],
    }


def test_messages_to_a_missing_conversation_404(tmp_path: Path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    response = client.post(
        "/chat/conversations/nope/messages", json={"content": "hello"}
    )
    assert response.status_code == 404


def test_empty_content_is_rejected(tmp_path: Path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    conversation_id = client.post("/chat/conversations", json={}).json()["id"]
    response = client.post(
        f"/chat/conversations/{conversation_id}/messages", json={"content": "   "}
    )
    assert response.status_code == 422


async def _stream_turn(conversation_id: str, content: str):
    """Start a turn and hand back its raw SSE iterator.

    `TestClient.stream` buffers the whole response before returning, so it
    cannot express "read two frames, then hang up". Calling the route gives
    the real `StreamingResponse` whose `body_iterator` can be abandoned
    exactly the way Starlette abandons it on a disconnect.
    """
    generator = app.dependency_overrides[get_session]()
    session = next(generator)
    response = await chat_module.send_message(
        conversation_id,
        chat_module.MessageCreate(content=content),
        session=session,
    )
    return response.body_iterator


def test_a_partial_answer_is_persisted_when_the_client_hangs_up(
    tmp_path: Path, monkeypatch
):
    """A turn cut short mid-stream must not lose what it already generated.

    The provider call is already paid for; dropping it meant a closed tab
    silently threw away a minute of generation.
    """
    client = _setup(tmp_path, monkeypatch)
    conversation_id = client.post("/chat/conversations", json={}).json()["id"]

    async def never_finishes(question, *, history=None, family=None, **kwargs):
        yield {"type": "delta", "text": "first half"}
        yield {"type": "delta", "text": " and more"}
        # The client goes away here: no `done` event will ever arrive.
        await asyncio.sleep(30)
        yield {"type": "done", "answer": "unreachable"}  # pragma: no cover

    monkeypatch.setattr(chat_module, "answer_with_search", never_finishes)

    async def read_two_frames_then_hang_up():
        stream = await _stream_turn(conversation_id, "explain SPI")
        frames = [await stream.__anext__(), await stream.__anext__()]
        # Starlette throws GeneratorExit into the body iterator when the
        # client disconnects; `aclose` is that, exactly.
        await stream.aclose()
        return frames

    frames = asyncio.run(read_two_frames_then_hang_up())
    assert "first half" in frames[0]

    detail = client.get(f"/chat/conversations/{conversation_id}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assistant = detail["messages"][1]
    assert assistant["content"] == "first half and more"
    assert assistant["payload"]["partial"] is True
    assert assistant["payload"]["failed"] is True


def test_a_quiet_agent_still_sends_heartbeats(tmp_path: Path, monkeypatch):
    """Nothing may be silent long enough for a proxy to close the connection."""
    client = _setup(tmp_path, monkeypatch)
    conversation_id = client.post("/chat/conversations", json={}).json()["id"]

    monkeypatch.setattr(chat_module.settings, "chat_heartbeat_seconds", 1.0)

    async def slow_planner(question, *, history=None, family=None, **kwargs):
        await asyncio.sleep(2.5)  # planning, no output
        yield {
            "type": "done",
            "answer": "done at last",
            "citations": [],
            "cited": [],
            "grounded": False,
            "verified": False,
            "searches": [],
            "scope": None,
            "warnings": [],
            "failed": False,
        }

    monkeypatch.setattr(chat_module, "answer_with_search", slow_planner)

    async def drain():
        stream = await _stream_turn(conversation_id, "slow one")
        return [frame async for frame in stream]

    frames = asyncio.run(drain())
    body = "".join(frames)

    assert body.count(": ping\n\n") >= 2
    # The comment frames must not be mistaken for events by a parser.
    events = [
        json.loads(frame.removeprefix("data: "))
        for frame in body.split("\n\n")
        if frame.startswith("data: ")
    ]
    assert [e["type"] for e in events] == ["done"]

    detail = client.get(f"/chat/conversations/{conversation_id}").json()
    assert detail["messages"][1]["content"] == "done at last"
    assert detail["messages"][1]["payload"]["partial"] is False


def test_an_agent_crash_persists_what_was_streamed(tmp_path: Path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    conversation_id = client.post("/chat/conversations", json={}).json()["id"]

    async def crashes(question, *, history=None, family=None, **kwargs):
        yield {"type": "delta", "text": "partial thought"}
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(chat_module, "answer_with_search", crashes)

    with client.stream(
        "POST",
        f"/chat/conversations/{conversation_id}/messages",
        json={"content": "boom"},
    ) as response:
        body = "".join(response.iter_text())

    assert '"type": "error"' in body
    detail = client.get(f"/chat/conversations/{conversation_id}").json()
    assert detail["messages"][1]["content"] == "partial thought"
    assert detail["messages"][1]["payload"]["partial"] is True


def test_a_turn_that_generated_nothing_stores_no_empty_message(
    tmp_path: Path, monkeypatch
):
    client = _setup(tmp_path, monkeypatch)
    conversation_id = client.post("/chat/conversations", json={}).json()["id"]

    async def silent(question, *, history=None, family=None, **kwargs):
        raise RuntimeError("died before the first token")
        yield {}  # pragma: no cover - unreachable, makes this a generator

    monkeypatch.setattr(chat_module, "answer_with_search", silent)

    with client.stream(
        "POST",
        f"/chat/conversations/{conversation_id}/messages",
        json={"content": "nothing comes back"},
    ) as response:
        "".join(response.iter_text())

    detail = client.get(f"/chat/conversations/{conversation_id}").json()
    assert [m["role"] for m in detail["messages"]] == ["user"]


def test_an_unreadable_payload_does_not_break_the_conversation(
    tmp_path: Path, monkeypatch
):
    client = _setup(tmp_path, monkeypatch)
    conversation_id = client.post("/chat/conversations", json={}).json()["id"]

    with Session(chat_module.engine) as session:
        session.add(
            chat_module.Message(
                conversation_id=conversation_id,
                role="assistant",
                content="an answer",
                payload="{not json at all",
            )
        )
        session.commit()

    detail = client.get(f"/chat/conversations/{conversation_id}")
    assert detail.status_code == 200
    assert detail.json()["messages"][0]["payload"] is None
