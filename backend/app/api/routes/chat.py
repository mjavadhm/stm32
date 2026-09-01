"""Conversational Q&A over the knowledge base (agentic RAG).

Conversations and messages are persisted; each turn runs the Chat Agent
(`app/agents/qa.py`), which plans its own retrieval, and the answer is
delivered as Server-Sent Events so the UI can render searches and answer
tokens as they happen.

The user message is committed *before* streaming begins: a question must
survive an agent failure, not vanish with the connection.
"""

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.agents.qa import answer_with_search
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


def _message_out(message: Message) -> dict:
    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "payload": json.loads(message.payload) if message.payload else None,
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
        # The request's session may close before a long stream ends; use a
        # fresh one for the writes that happen on completion.
        done_event: dict | None = None
        try:
            async for event in answer_with_search(
                question, history=history, family=family, **scope_kwargs
            ):
                if event["type"] == "done":
                    done_event = event
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:  # the agent never raises; belt and braces
            logger.exception("chat turn failed unexpectedly")
            yield f'data: {json.dumps({"type": "error", "detail": str(exc)})}\n\n'

        if done_event is None:
            yield 'data: {"type": "error", "detail": "stream ended without a done event"}\n\n'
            return

        with Session(engine) as write_session:
            conversation = write_session.get(Conversation, conversation_id)
            if conversation is not None:
                conversation.updated_at = datetime.now(UTC)
                write_session.add(conversation)
            write_session.add(
                Message(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=done_event["answer"],
                    payload=json.dumps(
                        {
                            "citations": done_event["citations"],
                            "cited": done_event["cited"],
                            "grounded": done_event["grounded"],
                            "verified": done_event["verified"],
                            "searches": done_event["searches"],
                            "scope": done_event.get("scope"),
                            "warnings": done_event["warnings"],
                            "failed": done_event["failed"],
                        },
                        ensure_ascii=False,
                    ),
                )
            )
            write_session.commit()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
