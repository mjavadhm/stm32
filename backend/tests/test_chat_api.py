"""Tests for the chat API: conversations, SSE streaming, persistence.

The Chat Agent itself is faked here (it has its own test module); these
tests care about HTTP semantics and what lands in the database.
"""

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlmodel import Session

import app.api.routes.chat as chat_module
from app.db.session import get_session, upgrade_database
from app.main import app


async def _fake_answer(question: str, *, history=None, family=None):
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

    async def fake_answer(question, *, history=None, family=None):
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
