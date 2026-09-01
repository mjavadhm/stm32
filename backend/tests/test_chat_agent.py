"""Tests for the Chat Agent (agentic RAG).

Every test runs fully offline: the knowledge base is an in-process
httpx MockTransport (serving the individual PageVault endpoints) and the
LLM is a fake with scripted replies.
"""

import asyncio
import json

import httpx
import pytest

import app.agents.qa as qa
from app.rag.client import PageVaultClient

CHUNK = {
    "chunk_id": "c3",
    "document_id": "d1",
    "score": 0.82,
    "kind": "section",
    "name": "SPI with DMA",
    "path": "RM0090.md",
    "line_start": 10,
    "line_end": 60,
    "signature": "",
    "heading_path": "SPI > DMA",
    "text": "Set the TXDMAEN bit in SPI_CR2 to enable DMA transmission.",
    "expanded_from_parent": False,
    "metadata": {"family": "STM32F4"},
}

SYMBOL = {
    "name": "HAL_SPI_Transmit_DMA",
    "kind": "function",
    "signature": (
        "HAL_StatusTypeDef HAL_SPI_Transmit_DMA"
        "(SPI_HandleTypeDef *hspi, uint8_t *pData, uint16_t Size)"
    ),
    "path": "Drivers/STM32F4xx_HAL_Driver/Src/stm32f4xx_hal_spi.c",
    "line_start": 1420,
    "line_end": 1490,
    "doc": "Transmit an amount of data in non-blocking mode with DMA.",
    "chunk_id": "c1",
    "collection": "stm32",
    "match": "prefix",
    "matched_term": "HAL_SPI_Transmit",
}


def _rag(handler) -> PageVaultClient:
    return PageVaultClient(
        base_url="http://pagevault-api:8000",
        timeout=1.0,
        transport=httpx.MockTransport(handler),
    )


def _ok_handler(captured: list[dict] | None = None):
    """Serve every channel: one chunk, one symbol, no types, no pages.

    Only the identifier HAL_SPI_Transmit resolves; every other term (the
    STM32F407 part number, the types referenced by the signature) matches
    nothing, which keeps the channel counts in the assertions stable.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "params": dict(request.url.params),
                    "body": json.loads(request.content) if request.content else None,
                }
            )
        if request.method == "POST" and request.url.path == "/text/search":
            return httpx.Response(
                200,
                json={
                    "query": json.loads(request.content)["query"],
                    "collection": "stm32",
                    "mode": "hybrid",
                    "results": [CHUNK],
                },
            )
        if request.method == "GET" and request.url.path == "/text/symbols":
            q = request.url.params.get("q", "")
            if q == "HAL_SPI_Transmit_DMA":
                return httpx.Response(
                    200, json={"query": q, "match": "exact", "results": [SYMBOL]}
                )
            if q == "HAL_SPI_Transmit":
                return httpx.Response(
                    200, json={"query": q, "match": "prefix", "results": [SYMBOL]}
                )
            return httpx.Response(200, json={"query": q, "match": "none", "results": []})
        if request.method == "POST" and request.url.path == "/search":
            return httpx.Response(
                200,
                json={
                    "query": json.loads(request.content)["query"],
                    "collection": "stm32-manuals",
                    "results": [],
                },
            )
        return httpx.Response(404, text="unknown route")

    return handler


def _down_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


class FakeLLM:
    """Scripted LLM: `replies` feed the planning calls in order, `chunks`
    are streamed as the answer."""

    def __init__(self, replies: list[str], chunks: list[str]):
        self.replies = list(replies)
        self.chunks = list(chunks)
        self.chat_calls: list[list[dict]] = []
        self.stream_calls: list[list[dict]] = []

    async def chat(self, messages, **kwargs):
        self.chat_calls.append(messages)
        return self.replies.pop(0)

    async def stream(self, messages, **kwargs):
        self.stream_calls.append(messages)
        for chunk in self.chunks:
            yield chunk


def _run(agent_iter):
    async def collect():
        return [event async for event in agent_iter]

    return asyncio.run(collect())


def _install(monkeypatch, llm: FakeLLM, rag: PageVaultClient):
    monkeypatch.setattr(qa, "get_agent_llm", lambda _name: llm)
    monkeypatch.setattr(qa, "get_rag_client", lambda: rag)


QUESTION = "How do I use HAL_SPI_Transmit with DMA on STM32F407?"


def test_a_planned_search_then_ready_streams_a_cited_answer(monkeypatch):
    captured: list[dict] = []
    llm = FakeLLM(
        replies=[
            '{"action": "search", "query": "HAL_SPI_Transmit_DMA function"}',
            '{"action": "ready"}',
        ],
        chunks=["Set TXDMAEN in SPI_CR2 ", "[RM0090.md:10-60]."],
    )
    _install(monkeypatch, llm, _rag(_ok_handler(captured)))

    events = _run(qa.answer_with_search(QUESTION))
    types = [e["type"] for e in events]

    assert types == ["search", "search_result", "delta", "delta", "done"]

    search, result = events[0], events[1]
    assert search["query"] == "HAL_SPI_Transmit_DMA function"
    assert search["index"] == 1
    assert result["available"] is True
    assert result["sources"] == {"symbols": 1, "types": 0, "chunks": 1, "pages": 0}

    done = events[-1]
    assert done["answer"] == "Set TXDMAEN in SPI_CR2 [RM0090.md:10-60]."
    assert done["cited"] == ["RM0090.md:10-60"]
    assert done["grounded"] is True
    assert done["verified"] is True
    assert done["searches"] == ["HAL_SPI_Transmit_DMA function"]
    assert done["failed"] is False
    json.dumps(done)  # must stay JSON-serialisable for the API layer

    # Family detected from the question narrows retrieval (case-insensitive).
    text_requests = [r for r in captured if r["path"] == "/text/search"]
    assert len(text_requests) == 1
    assert text_requests[0]["body"]["filters"]["family"] == ["STM32F4", "stm32f4"]

    # The planner saw what its search returned before deciding again.
    assert len(llm.chat_calls) == 2
    feedback = llm.chat_calls[1][-1]["content"]
    assert 'Search "HAL_SPI_Transmit_DMA function" returned:' in feedback
    assert "stm32f4xx_hal_spi.c:1420-1490" in feedback

    # The answer phase got the full context, not the summary.
    answer_messages = llm.stream_calls[0]
    assert "Cite the source" in answer_messages[0]["content"]
    assert "Set the TXDMAEN bit in SPI_CR2" in answer_messages[-1]["content"]


def test_ready_without_planning_still_retrieves_the_raw_question(monkeypatch):
    llm = FakeLLM(
        replies=['{"action": "ready"}'],
        chunks=["Answer ", "[RM0090.md:10-60]."],
    )
    _install(monkeypatch, llm, _rag(_ok_handler()))

    events = _run(qa.answer_with_search(QUESTION))
    done = events[-1]

    assert [e["type"] for e in events[:2]] == ["search", "search_result"]
    assert done["searches"] == [QUESTION]
    assert done["grounded"] is True


def test_an_endless_planner_is_bounded_by_the_search_cap(monkeypatch):
    monkeypatch.setattr(qa.settings, "chat_max_searches", 2)
    llm = FakeLLM(
        replies=[f'{{"action": "search", "query": "query {i}"}}' for i in range(5)],
        chunks=["answer [RM0090.md:10-60]"],
    )
    _install(monkeypatch, llm, _rag(_ok_handler()))

    events = _run(qa.answer_with_search(QUESTION))
    searches = [e for e in events if e["type"] == "search"]

    assert len(searches) == 2
    assert events[-1]["searches"] == ["query 0", "query 1"]


def test_an_unreachable_knowledge_base_degrades_to_an_unverified_answer(monkeypatch):
    llm = FakeLLM(
        replies=[
            '{"action": "search", "query": "spi dma"}',
            '{"action": "ready"}',
        ],
        chunks=["Warning: answered without documentation sources. Set TXDMAEN."],
    )
    _install(monkeypatch, llm, _rag(_down_handler))

    events = _run(qa.answer_with_search(QUESTION))
    result = events[1]
    done = events[-1]

    assert result["available"] is False
    assert done["grounded"] is False
    assert done["citations"] == []
    assert done["verified"] is False
    assert done["warnings"]
    # The answer prompt switched to its no-context variant.
    assert "unavailable" in llm.stream_calls[0][0]["content"]


def test_a_malformed_planning_reply_is_repaired_once(monkeypatch):
    llm = FakeLLM(
        replies=[
            "I will search the documentation now!",  # malformed
            '{"action": "search", "query": "spi dma"}',  # repaired
            '{"action": "ready"}',
        ],
        chunks=["answer [RM0090.md:10-60]"],
    )
    _install(monkeypatch, llm, _rag(_ok_handler()))

    events = _run(qa.answer_with_search(QUESTION))
    done = events[-1]

    assert done["searches"] == ["spi dma"]
    assert done["failed"] is False
    # malformed + repaired + ready = three planning calls
    assert len(llm.chat_calls) == 3


def test_a_broken_planner_falls_back_to_one_direct_search(monkeypatch):
    llm = FakeLLM(
        replies=["no json here", "still no json"],
        chunks=["Warning: answered without documentation sources."],
    )
    _install(monkeypatch, llm, _rag(_ok_handler()))

    events = _run(qa.answer_with_search(QUESTION))
    done = events[-1]

    assert any("retrieval planning failed" in w for w in done["warnings"])
    assert done["searches"] == [QUESTION]
    assert done["grounded"] is True


def test_follow_up_history_resolves_the_family(monkeypatch):
    captured: list[dict] = []
    llm = FakeLLM(
        replies=['{"action": "ready"}'],
        chunks=["answer [RM0090.md:10-60]"],
    )
    _install(monkeypatch, llm, _rag(_ok_handler(captured)))

    history = [
        {"role": "user", "content": "Configure a timer on STM32F407"},
        {"role": "assistant", "content": "Use TIM2 ..."},
    ]
    _run(qa.answer_with_search("What about its prescaler?", history=history))

    text_requests = [r for r in captured if r["path"] == "/text/search"]
    assert text_requests[0]["body"]["filters"]["family"] == ["STM32F4", "stm32f4"]
    # History reached the answer phase.
    assert any("prescaler" in m["content"] for m in llm.stream_calls[0])


def test_scope_narrows_every_search_the_agent_runs(monkeypatch):
    captured: list[dict] = []
    llm = FakeLLM(
        replies=[
            '{"action": "search", "query": "HAL_SPI_Transmit_DMA"}',
            '{"action": "ready"}',
        ],
        chunks=["answer [RM0090.md:10-60]"],
    )
    _install(monkeypatch, llm, _rag(_ok_handler(captured)))

    events = _run(
        qa.answer_with_search(
            QUESTION,
            text_collection="code",
            document_ids=["d1"],
        )
    )
    done = events[-1]

    text_requests = [r for r in captured if r["path"] == "/text/search"]
    assert text_requests[0]["body"]["collection"] == "code"
    assert text_requests[0]["body"]["filters"]["document_id"] == ["d1"]
    # The scope is echoed back so the UI can show what was searched.
    assert done["scope"] == {
        "text_collection": "code",
        "page_collection": None,
        "document_ids": ["d1"],
    }


def test_history_is_trimmed_and_normalised(monkeypatch):
    llm = FakeLLM(
        replies=['{"action": "ready"}'],
        chunks=["answer"],
    )
    _install(monkeypatch, llm, _rag(_ok_handler()))

    noisy = [{"role": "system", "content": "junk"}] + [
        {"role": "user", "content": f"turn {i}"} for i in range(20)
    ]
    _run(qa.answer_with_search("question", history=noisy))

    planning_messages = llm.chat_calls[0]
    # system(decision) + trimmed history + question
    assert len(planning_messages) == 1 + qa.MAX_HISTORY_MESSAGES + 1
    assert planning_messages[0]["role"] == "system"
    assert planning_messages[-1]["content"] == "question"
