"""Tests for the PageVault client and the Datasheet Agent.

Every test uses an in-process transport: no PageVault container, no network.
The mock handler serves the three individual endpoints the client fans out
to -- POST /text/search, GET /text/symbols, POST /search -- the same way the
real PageVault serves them.
"""

import asyncio
import json

import httpx
import pytest

from app.agents.datasheet import (
    answer_hardware_question,
    build_messages,
    detect_family,
)
from app.rag.client import PageVaultClient, RagContext

# The question every "ok" test asks. Its identifier-shaped tokens are
# HAL_SPI_Transmit (scored highest) and STM32F407, in that order -- the
# symbol-channel tests below depend on that ordering.
QUESTION = "How do I use HAL_SPI_Transmit with DMA on STM32F407?"

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

TYPE = {
    "name": "SPI_HandleTypeDef",
    "kind": "struct",
    "signature": "typedef struct __SPI_HandleTypeDef",
    "path": "Drivers/STM32F4xx_HAL_Driver/Inc/stm32f4xx_hal_spi.h",
    "line_start": 300,
    "line_end": 340,
    "doc": "SPI handle structure definition.",
    "chunk_id": "c2",
    "collection": "stm32",
    "match": "exact",
    "matched_term": "",
}


def _client(handler) -> PageVaultClient:
    return PageVaultClient(
        base_url="http://pagevault-api:8000",
        timeout=1.0,
        transport=httpx.MockTransport(handler),
    )


def _record(captured: list[dict], request: httpx.Request) -> None:
    captured.append(
        {
            "method": request.method,
            "path": request.url.path,
            "params": dict(request.url.params),
            "body": json.loads(request.content) if request.content else None,
        }
    )


def _ok_handler(captured: list[dict] | None = None):
    """Serve every channel. Symbols answer for the identifiers in QUESTION
    plus the SPI_HandleTypeDef type; everything else matches nothing."""

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            _record(captured, request)

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
            if q == "HAL_SPI_Transmit":
                return httpx.Response(
                    200, json={"query": q, "match": "prefix", "results": [SYMBOL]}
                )
            if q == "SPI_HandleTypeDef":
                return httpx.Response(
                    200, json={"query": q, "match": "exact", "results": [TYPE]}
                )
            return httpx.Response(
                200, json={"query": q, "match": "none", "results": []}
            )
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


# --- client ----------------------------------------------------------------


def test_search_maps_every_channel():
    context = asyncio.run(_client(_ok_handler()).search(QUESTION))

    assert context.available is True
    assert [s.name for s in context.symbols] == ["HAL_SPI_Transmit_DMA"]
    assert [s.name for s in context.type_context] == ["SPI_HandleTypeDef"]
    assert [s.name for s in context.chunks] == ["SPI with DMA"]
    # Identifier-shaped tokens of the question, best-first.
    assert context.identifiers == ["HAL_SPI_Transmit", "STM32F407"]


def test_search_sends_the_family_filter_to_the_text_channel():
    captured: list[dict] = []
    asyncio.run(_client(_ok_handler(captured)).search("DMA setup", family="STM32F4"))

    text_requests = [r for r in captured if r["path"] == "/text/search"]
    assert len(text_requests) == 1
    body = text_requests[0]["body"]
    # Case variants, not one exact string: this KB ingests `stm32f4` while
    # family detection produces `STM32F4`, and MatchAny matches either.
    assert body["filters"] == {"family": ["STM32F4", "stm32f4"]}
    assert body["collection"] == "stm32"
    assert body["mode"] == "hybrid"
    # The symbol lookups filter by collection, not by family.
    for request in captured:
        if request["path"] == "/text/symbols":
            assert request["params"]["collection"] == "stm32"


def test_collection_overrides_reach_every_text_call():
    captured: list[dict] = []
    asyncio.run(
        _client(_ok_handler(captured)).search(
            QUESTION, text_collection="code", page_collection="manuals"
        )
    )

    text_requests = [r for r in captured if r["path"] == "/text/search"]
    assert text_requests[0]["body"]["collection"] == "code"
    visual_requests = [r for r in captured if r["path"] == "/search"]
    assert visual_requests[0]["body"]["collection"] == "manuals"
    for request in captured:
        if request["path"] == "/text/symbols":
            assert request["params"]["collection"] == "code"


def test_document_ids_scope_both_channels():
    captured: list[dict] = []
    asyncio.run(
        _client(_ok_handler(captured)).search(QUESTION, document_ids=["d1", "d2"])
    )

    text_requests = [r for r in captured if r["path"] == "/text/search"]
    assert text_requests[0]["body"]["filters"]["document_id"] == ["d1", "d2"]
    visual_requests = [r for r in captured if r["path"] == "/search"]
    assert visual_requests[0]["body"]["filters"]["document_id"] == ["d1", "d2"]


def test_collections_and_documents_can_be_listed():
    def listing_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/text/collections":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "c1",
                        "name": "code",
                        "document_count": 13,
                        "chunk_count": 800,
                        "symbol_count": 1500,
                    }
                ],
            )
        if request.url.path == "/collections":
            return httpx.Response(
                200, json=[{"id": "v1", "name": "default", "document_count": 4}]
            )
        if request.url.path == "/text/documents":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "d1",
                        "path": "hal/Src/stm32f4xx_hal_spi.c",
                        "status": "indexed",
                        "chunk_count": 105,
                        "symbol_count": 99,
                    },
                    # A failed ingest has nothing to ask against.
                    {
                        "id": "d2",
                        "path": "hal/Src/broken.c",
                        "status": "failed",
                        "chunk_count": 0,
                        "symbol_count": 0,
                    },
                ],
            )
        if request.url.path == "/documents":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "p1",
                        "kind": "pdf",
                        "filename": "RM0090.pdf",
                        "status": "indexed",
                        "page_count": 1130,
                    }
                ],
            )
        return httpx.Response(404, text="unknown route")

    client = _client(listing_handler)

    text, text_warning = asyncio.run(client.list_text_collections())
    assert text_warning is None
    assert text == [
        {
            "name": "code",
            "document_count": 13,
            "chunk_count": 800,
            "symbol_count": 1500,
        }
    ]

    visual, visual_warning = asyncio.run(client.list_visual_collections())
    assert visual_warning is None
    assert visual == [{"name": "default", "document_count": 4}]

    documents, document_warning = asyncio.run(client.list_documents("code"))
    assert document_warning is None
    assert [d["path"] for d in documents] == ["hal/Src/stm32f4xx_hal_spi.c"]

    pages, pages_warning = asyncio.run(
        client.list_documents("default", visual=True)
    )
    assert pages_warning is None
    assert pages == [{"id": "p1", "path": "RM0090.pdf", "status": "indexed", "pages": 1130}]


def test_document_chunks_can_be_fetched_for_a_citation():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/text/documents/d1/chunks":
            return httpx.Response(
                200,
                json={
                    "document_id": "d1",
                    "path": "hal/Src/spi.c",
                    "count": 2,
                    "chunks": [
                        {
                            "id": "c1",
                            "kind": "function",
                            "name": "HAL_SPI_Receive_DMA",
                            "lines": [1745, 1853],
                            "text": "HAL_StatusTypeDef HAL_SPI_Receive_DMA(...)",
                        },
                        {
                            "id": "c2",
                            "kind": "function",
                            "name": "HAL_SPI_Transmit_DMA",
                            "lines": [1643, 1743],
                            "text": "HAL_StatusTypeDef HAL_SPI_Transmit_DMA(...)",
                        },
                    ],
                },
            )
        return httpx.Response(404, text="unknown route")

    chunks, warning = asyncio.run(_client(handler).get_document_chunks("d1"))

    assert warning is None
    assert [c["name"] for c in chunks] == [
        "HAL_SPI_Receive_DMA",
        "HAL_SPI_Transmit_DMA",
    ]


def test_page_images_are_proxied():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/files/p1/pages/3.png":
            return httpx.Response(
                200, content=b"\x89PNG-bytes", headers={"content-type": "image/png"}
            )
        return httpx.Response(404, text="unknown route")

    content, media_type, warning = asyncio.run(
        _client(handler).fetch_page_image("p1", 3)
    )

    assert warning is None
    assert content == b"\x89PNG-bytes"
    assert media_type == "image/png"

    missing, _type, warning = asyncio.run(
        _client(handler).fetch_page_image("nope", 1)
    )
    assert missing is None and warning


def test_listings_degrade_to_empty_when_pagevault_is_down():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _client(boom)

    text, text_warning = asyncio.run(client.list_text_collections())
    assert text == [] and text_warning

    documents, document_warning = asyncio.run(client.list_documents("code"))
    assert documents == [] and document_warning


def test_symbol_lookups_share_one_budget(monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr("app.rag.client.settings.rag_symbol_top_k", 1)
    asyncio.run(_client(_ok_handler(captured)).search(QUESTION))

    # Budget of 1: the first identifier fills it, the second is never asked.
    # (Type expansion also queries /text/symbols, with limit=1.)
    symbol_queries = [
        r["params"]["q"] for r in captured if r["path"] == "/text/symbols"
    ]
    assert symbol_queries[0] == "HAL_SPI_Transmit"
    assert "STM32F407" not in symbol_queries
    context = asyncio.run(_client(_ok_handler()).search(QUESTION))
    assert len(context.symbols) == 1


def test_citations_are_stable_and_deduplicated():
    context = asyncio.run(_client(_ok_handler()).search(QUESTION))
    citations = context.citations()

    assert citations[0].endswith("stm32f4xx_hal_spi.c:1420-1490")
    assert len(citations) == len(set(citations))


def test_prompt_puts_exact_symbols_before_prose():
    context = asyncio.run(_client(_ok_handler()).search(QUESTION))
    prompt = context.as_prompt()

    assert prompt.index("HAL_SPI_Transmit_DMA") < prompt.index("TXDMAEN")
    assert "[RM0090.md:10-60]" in prompt


def test_prompt_respects_the_character_ceiling():
    context = asyncio.run(_client(_ok_handler()).search(QUESTION))
    assert len(context.as_prompt(max_chars=200)) <= 250


def test_an_unreachable_knowledge_base_degrades_instead_of_raising():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    context = asyncio.run(_client(boom).search("anything"))

    assert context.available is False
    assert context.is_empty
    assert context.warnings
    assert any("failed" in warning for warning in context.warnings)


def test_a_timeout_degrades_instead_of_raising():
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    context = asyncio.run(_client(slow).search("anything"))

    assert context.available is False
    assert any("timed out" in warning for warning in context.warnings)


def test_a_server_error_degrades_instead_of_raising():
    context = asyncio.run(
        _client(lambda request: httpx.Response(500, text="boom")).search("anything")
    )
    assert context.available is False


def test_one_dead_channel_does_not_sink_the_others():
    """Visual search down, text and symbols up: still available, with a warning."""

    captured: list[dict] = []

    def flaky_visual(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/search":
            raise httpx.ConnectError("visual down", request=request)
        return _ok_handler(captured)(request)

    context = asyncio.run(_client(flaky_visual).search(QUESTION))

    assert context.available is True
    assert context.chunks and context.symbols
    assert any("visual" in warning for warning in context.warnings)


def test_a_symbol_skip_does_not_mask_a_dead_knowledge_base():
    """No identifiers in the question AND every call fails -> unavailable.

    The skip itself is not a failure, so it must not be counted as the one
    channel that "answered".
    """

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    context = asyncio.run(_client(boom).search("How do I configure a timer?"))

    assert context.available is False
    assert context.warnings


def test_health_is_false_when_pagevault_is_down():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert asyncio.run(_client(boom).health()) is False


def test_a_symbol_skip_is_a_note_not_a_user_visible_warning():
    """A question naming no identifier is normal, not something wrong.

    The skip used to travel in the warning slot, so every natural-language
    question (and every Persian one) showed retrieval plumbing to the user.
    """
    captured: list[dict] = []
    context = asyncio.run(
        _client(_ok_handler(captured)).search("How do I configure a timer?")
    )

    assert context.available is True
    assert context.identifiers == []
    assert context.warnings == []
    assert any("symbol channel skipped" in note for note in context.notes)
    # And it really did skip: no symbol lookup was attempted.
    assert not [r for r in captured if r["path"] == "/text/symbols"]


def test_partial_symbol_results_survive_a_later_failure():
    """A term that fails must not discard what earlier terms found."""

    def flaky_symbols(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/text/symbols":
            if request.url.params.get("q") == "HAL_SPI_Transmit":
                return httpx.Response(
                    200, json={"match": "prefix", "results": [SYMBOL]}
                )
            raise httpx.ConnectError("symbols down", request=request)
        return _ok_handler()(request)

    context = asyncio.run(_client(flaky_symbols).search(QUESTION))

    assert context.available is True
    assert [s.name for s in context.symbols] == ["HAL_SPI_Transmit_DMA"]
    assert any("symbol" in warning for warning in context.warnings)


def test_repeated_channel_warnings_are_deduped():
    """One dead channel is one warning, however many calls hit it."""

    def dead_symbols(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/text/symbols":
            raise httpx.ConnectError("symbols down", request=request)
        return _ok_handler()(request)

    context = asyncio.run(_client(dead_symbols).search(QUESTION))

    assert len(context.warnings) == len(set(context.warnings))


def test_a_page_hit_carries_what_the_ui_needs_to_resolve_it():
    """A visual citation must be resolvable back to a page image."""
    page = {
        "document_id": "07c66e7d-879c-4fb3-a7a4-9a8df381c613",
        "page_number": 213,
        "score": 0.61,
        "image_url": "http://pagevault-api:8000/files/07c66e7d/pages/213.png",
        "filename": "rm0090.pdf",
    }

    def with_pages(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/search":
            return httpx.Response(200, json={"results": [page]})
        return _ok_handler()(request)

    context = asyncio.run(_client(with_pages).search(QUESTION))

    (hit,) = context.pages
    assert hit.citation == "07c66e7d-879c-4fb3-a7a4-9a8df381c613#p213"
    assert hit.metadata["document_id"] == "07c66e7d-879c-4fb3-a7a4-9a8df381c613"
    assert hit.metadata["page_number"] == 213
    assert hit.metadata["filename"] == "rm0090.pdf"


# --- datasheet agent -------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Read MPU6050 over SPI on STM32F407", "STM32F4"),
        ("stm32h743 clock tree", "STM32H7"),
        ("How do I configure a timer?", None),
    ],
)
def test_detect_family(text, expected):
    assert detect_family(text) == expected


def test_prompt_demands_citations_when_context_exists():
    context = asyncio.run(_client(_ok_handler()).search(QUESTION))
    messages = build_messages("How do I use SPI with DMA?", context)

    assert "Cite the source of every technical claim" in messages[0]["content"]
    assert "HAL_SPI_Transmit_DMA" in messages[1]["content"]


def test_prompt_switches_to_a_warning_mode_without_context():
    empty = RagContext(query="q", available=False, warnings=["down"])
    messages = build_messages("How do I use SPI with DMA?", empty)

    assert "knowledge base is currently unavailable" in messages[0]["content"]
    assert "unverified" in messages[0]["content"]


def test_agent_answers_and_reports_its_sources(monkeypatch):
    import app.agents.datasheet as datasheet

    captured: dict = {}

    class FakeLLM:
        async def chat(self, messages, **kwargs):
            captured["messages"] = messages
            return "Set TXDMAEN in SPI_CR2 [RM0090.md:10-60]."

    monkeypatch.setattr(datasheet, "get_rag_client", lambda: _client(_ok_handler()))
    monkeypatch.setattr(datasheet, "get_agent_llm", lambda _name: FakeLLM())

    result = asyncio.run(answer_hardware_question(QUESTION))

    assert result["grounded"] is True
    assert result["family"] == "STM32F4"
    assert result["sources_used"] == {"symbols": 1, "types": 1, "chunks": 1, "pages": 0}
    assert any("stm32f4xx_hal_spi.c" in c for c in result["citations"])
    assert "Retrieved context" in captured["messages"][1]["content"]
    json.dumps(result)  # must be storable in TaskRun.result


def test_agent_still_answers_when_the_knowledge_base_is_down(monkeypatch):
    import app.agents.datasheet as datasheet

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    class FakeLLM:
        async def chat(self, messages, **kwargs):
            return "Warning: answered without documentation sources. ..."

    monkeypatch.setattr(datasheet, "get_rag_client", lambda: _client(boom))
    monkeypatch.setattr(datasheet, "get_agent_llm", lambda _name: FakeLLM())

    result = asyncio.run(answer_hardware_question("DMA on STM32F407?"))

    assert result["grounded"] is False
    assert result["citations"] == []
    assert result["warnings"]


# --- /rag/source: both citation shapes -------------------------------------


def test_a_page_citation_resolves_to_a_proxied_image(monkeypatch):
    """`<document_id>#pN` used to 404: it was parsed as a file path.

    A visual hit has no text to return, so the answer is a pointer to the
    page image, proxied through this API (the browser cannot reach PageVault).
    """
    from app.api.routes import rag as rag_routes

    result = asyncio.run(
        rag_routes.rag_source(
            citation="07c66e7d-879c-4fb3-a7a4-9a8df381c613#p213", collection=None
        )
    )

    assert result["kind"] == "page"
    assert result["document_id"] == "07c66e7d-879c-4fb3-a7a4-9a8df381c613"
    assert result["page"] == 213
    assert result["chunks"] == []
    assert result["image_url"] == (
        "/rag/page?document_id=07c66e7d-879c-4fb3-a7a4-9a8df381c613&page=213"
    )


def test_the_page_proxy_rejects_a_traversing_document_id():
    """The id is interpolated into a PageVault URL path."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    response = client.get("/rag/page", params={"document_id": "../../health", "page": 1})
    assert response.status_code == 422
