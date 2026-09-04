"""Conversational Q&A over the knowledge base (agentic RAG).

Conversations and messages are persisted; each turn runs the Chat Agent
(`app/agents/qa.py`), which plans its own retrieval, and the answer is
delivered as Server-Sent Events so the UI can render searches and answer
tokens as they happen.

The user message is committed *before* streaming begins, and the assistant
message is committed on *every* exit path -- normal completion, agent
failure, or the client hanging up mid-stream. A turn costs real provider
time; none of it may be lost because a browser tab closed.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.agents.qa import answer_with_search
from app.core.config import settings
from app.db.models import Conversation, Message, utcnow
from app.db.session import engine, get_session

router = APIRouter(prefix="/chat", tags=["chat"])

logger = logging.getLogger(__name__)


def _conversation_summary(conversation: Conversation) -> dict:
    return {
        "id": conversation.id,
        "title": conversation.title,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
    }


def _payload_out(message: Message) -> dict | None:
    """Decode a stored payload, tolerating a corrupt one.

    A single unreadable row must not 500 the whole conversation: the
    message text is the part the user came for, the metadata is a bonus.
    """
    if not message.payload:
        return None
    try:
        return json.loads(message.payload)
    except (ValueError, TypeError):
        logger.warning("message %s has an unreadable payload", message.id)
        return None


def _message_out(message: Message) -> dict:
    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "payload": _payload_out(message),
        "created_at": message.created_at,
    }


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class MessageCreate(BaseModel):
    content: str = Field(min_length=1)
    family: str | None = None
    # Retrieval scope, from the chat UI's selectors: which collections to
    # search and, optionally, which specific documents ("parts") only.
    text_collection: str | None = None
    page_collection: str | None = None
    document_ids: list[str] | None = Field(default=None, max_length=20)


@router.post("/conversations")
def create_conversation(
    payload: ConversationCreate,
    session: Session = Depends(get_session),
) -> dict:
    conversation = Conversation(title=(payload.title or "").strip())
    session.add(conversation)
    session.commit()
    session.refresh(conversation)
    return _conversation_summary(conversation)


@router.get("/conversations")
def list_conversations(session: Session = Depends(get_session)) -> list[dict]:
    conversations = session.exec(
        select(Conversation).order_by(Conversation.updated_at.desc())  # type: ignore[union-attr]
    ).all()
    return [_conversation_summary(c) for c in conversations]


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str,
    session: Session = Depends(get_session),
) -> dict:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = session.exec(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.id)  # type: ignore[union-attr]
    ).all()
    return {
        **_conversation_summary(conversation),
        "messages": [_message_out(m) for m in messages],
    }


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: str,
    session: Session = Depends(get_session),
) -> dict:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    for message in session.exec(
        select(Message).where(Message.conversation_id == conversation_id)
    ).all():
        session.delete(message)
    session.delete(conversation)
    session.commit()
    return {"deleted": conversation_id}


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: str,
    payload: MessageCreate,
    session: Session = Depends(get_session),
) -> StreamingResponse:
    """Run one chat turn and stream the agent's events as SSE."""
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="Message content is empty")

    history = [
        {"role": m.role, "content": m.content}
        for m in session.exec(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id)  # type: ignore[union-attr]
        ).all()
    ]

    # Persist the user message (and a title for the first one) before any
    # streaming starts, so it survives an agent failure or a dropped
    # connection.
    now = utcnow()
    session.add(Message(conversation_id=conversation_id, role="user", content=content))
    if not conversation.title:
        conversation.title = content[:80]
    conversation.updated_at = now
    session.add(conversation)
    session.commit()

    question = content
    family = payload.family
    scope_kwargs = {
        "text_collection": payload.text_collection,
        "page_collection": payload.page_collection,
        "document_ids": payload.document_ids,
    }

    async def event_stream() -> AsyncIterator[str]:
        # The request's session may close before a long stream ends; the
        # writes below use a fresh one.
        state: dict[str, Any] = {"done": None, "answer": "", "aborted": False}
        try:
            async for event in _with_heartbeat(
                answer_with_search(
                    question, history=history, family=family, **scope_kwargs
                ),
                state,
            ):
                yield event
        except (asyncio.CancelledError, GeneratorExit):
            # The client hung up. Everything generated so far is already
            # paid for, so record it and re-raise: swallowing a
            # CancelledError leaves the task in an undefined state.
            state["aborted"] = True
            _persist_turn(conversation_id, state)
            raise
        except Exception as exc:  # the agent never raises; belt and braces
            logger.exception("chat turn failed unexpectedly")
            state["aborted"] = True
            yield _frame({"type": "error", "detail": str(exc)})
            _persist_turn(conversation_id, state)
            return

        if state["done"] is None:
            state["aborted"] = True
            yield _frame(
                {"type": "error", "detail": "stream ended without a done event"}
            )
        _persist_turn(conversation_id, state)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _frame(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _with_heartbeat(
    events: AsyncIterator[dict], state: dict[str, Any]
) -> AsyncIterator[str]:
    """Serialise agent events, emitting a comment while the agent is quiet.

    Retrieval planning on a reasoning model can take a minute before the
    first token, and a connection that carries no bytes for that long is
    closed by proxies (nginx `proxy_read_timeout` defaults to 60s). A
    `: ping` comment is ignored by every SSE parser but resets the clock.

    Answer text is accumulated in `state` as it passes, so a turn cut short
    can still be persisted.
    """
    interval = max(1.0, settings.chat_heartbeat_seconds)
    iterator = events.__aiter__()
    pending: asyncio.Future | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            # `wait` leaves the task running on timeout, so the same step is
            # still in flight across however many pings it needs.
            finished, _ = await asyncio.wait({pending}, timeout=interval)
            if not finished:
                yield ": ping\n\n"
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            finally:
                pending = None
            # Record before yielding: the yield is where a disconnect
            # raises, and a delta already seen must not be lost with it.
            if event["type"] == "delta":
                state["answer"] += event.get("text", "")
            elif event["type"] == "done":
                state["done"] = event
            yield _frame(event)
    finally:
        # Stop the in-flight step and let the agent generator unwind, so a
        # disconnect never leaves an orphaned provider call behind.
        if pending is not None:
            pending.cancel()
            try:
                await pending
            except BaseException:  # noqa: BLE001 - cleanup, any outcome is fine
                pass
        with suppress(BaseException):  # noqa: BLE001
            await iterator.aclose()  # type: ignore[attr-defined]


def _persist_turn(conversation_id: str, state: dict[str, Any]) -> None:
    """Write the assistant message, whether or not the turn finished.

    An aborted turn is stored with `partial: true` so the UI can mark it as
    incomplete rather than presenting a truncated answer as final.
    """
    done = state["done"]
    answer = done["answer"] if done else state["answer"]
    if not answer.strip():
        # Nothing was generated: a bare "" message would only add noise to
        # the transcript and to the next turn's history.
        return

    metadata = {
        "citations": done["citations"] if done else [],
        "cited": done["cited"] if done else [],
        "grounded": done["grounded"] if done else False,
        "verified": done["verified"] if done else False,
        "searches": done["searches"] if done else [],
        "scope": done.get("scope") if done else None,
        "warnings": done["warnings"] if done else ["turn ended before completion"],
        "failed": done["failed"] if done else True,
        "partial": done is None,
    }
    try:
        with Session(engine) as write_session:
            conversation = write_session.get(Conversation, conversation_id)
            if conversation is not None:
                conversation.updated_at = datetime.now(UTC)
                write_session.add(conversation)
            write_session.add(
                Message(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=answer,
                    payload=json.dumps(metadata, ensure_ascii=False),
                )
            )
            write_session.commit()
    except Exception:  # pragma: no cover - a failed write must not mask the turn
        logger.exception("persisting the assistant message failed")
