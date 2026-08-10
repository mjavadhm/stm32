"""Tests for the PageVault client and the Datasheet Agent.

Every test uses an in-process transport: no PageVault container, no network.
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

UNIFIED_RESPONSE = {
    "query": "How do I use HAL_SPI_Transmit with DMA?",
    "strategy": "quota",
    "symbols": [
        {
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
            "match": "exact",
            "matched_term": "HAL_SPI_Transmit",
        }
    ],
    "type_context": [
        {
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
    ],
    "chunks": [
        {
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
            "metadata": {"family": "STM32F4"},
        }
    ],
    "pages": [],
    "identifiers": ["HAL_SPI_Transmit"],
    "fused": [],
    "warnings": [],
}


def _client(handler) -> PageVaultClient:
    return PageVaultClient(
        base_url="http://pagevault-api:8000",
        timeout=1.0,
        transport=httpx.MockTransport(handler),
    )


def _ok_handler(captured: list[dict] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(json.loads(request.content))
        return httpx.Response(200, json=UNIFIED_RESPONSE)

    return handler


# --- client ----------------------------------------------------------------


def test_search_maps_every_channel():
    context = asyncio.run(_client(_ok_handler()).search("HAL_SPI_Transmit with DMA"))

    assert context.available is True
    assert [s.name for s in context.symbols] == ["HAL_SPI_Transmit_DMA"]
    assert [s.name for s in context.type_context] == ["SPI_HandleTypeDef"]
    assert [s.name for s in context.chunks] == ["SPI with DMA"]
    assert context.identifiers == ["HAL_SPI_Transmit"]


def test_search_hits_the_unified_endpoint_with_a_family_filter():
    captured: list[dict] = []
    asyncio.run(_client(_ok_handler(captured)).search("DMA setup", family="STM32F4"))

    assert len(captured) == 1
    payload = captured[0]
    assert payload["text_filters"] == {"family": "STM32F4"}
    assert payload["expand_types"] is True
    assert payload["strategy"] == "quota"


def test_citations_are_stable_and_deduplicated():
    context = asyncio.run(_client(_ok_handler()).search("spi dma"))
    citations = context.citations()

    assert citations[0].endswith("stm32f4xx_hal_spi.c:1420-1490")
    assert len(citations) == len(set(citations))


def test_prompt_puts_exact_symbols_before_prose():
    context = asyncio.run(_client(_ok_handler()).search("spi dma"))
    prompt = context.as_prompt()

    assert prompt.index("HAL_SPI_Transmit_DMA") < prompt.index("TXDMAEN")
    assert "[RM0090.md:10-60]" in prompt


def test_prompt_respects_the_character_ceiling():
    context = asyncio.run(_client(_ok_handler()).search("spi dma"))
    assert len(context.as_prompt(max_chars=200)) <= 250


def test_an_unreachable_knowledge_base_degrades_instead_of_raising():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    context = asyncio.run(_client(boom).search("anything"))

    assert context.available is False
    assert context.is_empty
    assert context.warnings and "unavailable" in context.warnings[0]


def test_a_timeout_degrades_instead_of_raising():
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    context = asyncio.run(_client(slow).search("anything"))

    assert context.available is False
    assert "timed out" in context.warnings[0]


def test_a_server_error_degrades_instead_of_raising():
    context = asyncio.run(
        _client(lambda request: httpx.Response(500, text="boom")).search("anything")
    )
    assert context.available is False


def test_health_is_false_when_pagevault_is_down():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert asyncio.run(_client(boom).health()) is False


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
    context = asyncio.run(_client(_ok_handler()).search("spi dma"))
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

    result = asyncio.run(
        answer_hardware_question("How do I use HAL_SPI_Transmit with DMA on STM32F407?")
    )

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
