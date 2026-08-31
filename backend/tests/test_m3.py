"""M3 tests: contracts, requirements, datasheet fan-out, architecture, routing.

No live services. The LLM is a fake object and PageVault is an in-process
`httpx.MockTransport`, so the whole design pipeline is exercised offline.
"""

import asyncio
import json

import httpx
import pytest

from app.agents import architecture as architecture_module
from app.agents import datasheet as datasheet_module
from app.agents import requirements as requirements_module
from app.agents.architecture import design_architecture
from app.agents.datasheet import build_questions, gather_hardware_findings
from app.agents.requirements import analyze_requirements
from app.db.models import RequestType
from app.orchestrator.contracts import (
    SCHEMA_VERSION,
    Architecture,
    ContractError,
    HardwareFinding,
    HardwareFindings,
    PeripheralNeed,
    Requirements,
    dump,
    extract_json,
    parse_model,
)
from app.orchestrator.graph import ROUTER_NODE, agent_sequence_for, pipeline_for
from app.rag.client import PageVaultClient


class FakeLLM:
    """Returns canned replies and records what it was asked."""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls: list[list[dict]] = []

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        return self.reply


REQUIREMENTS_REPLY = json.dumps(
    {
        "summary": "Read an MPU6050 over SPI using DMA.",
        "mcu": "STM32F407VG",
        "family": "",
        "peripherals": [
            {
                "name": "SPI1",
                "role": "read MPU6050",
                "protocol": "spi",
                "transfer_mode": "DMA",
            }
        ],
        "external_components": ["MPU6050"],
        "rtos": "",
        "deliverables": ["firmware source"],
        "assumptions": [],
        "open_questions": ["Which SPI pins are wired on the board?"],
    }
)

ARCHITECTURE_REPLY = json.dumps(
    {
        "overview": "HAL-based driver with DMA streaming.",
        "driver_layer": "hal",
        "rtos": "none",
        "modules": [
            {
                "name": "mpu6050",
                "path": "Core/Src/mpu6050.c",
                "layer": "driver",
                "responsibility": "sensor driver",
                "depends_on": ["spi_bus"],
            }
        ],
        "peripherals": [
            {
                "peripheral": "SPI1",
                "transfer_mode": "dma",
                "rationale": "continuous sensor streaming",
                "citation": "stm32f4xx_hal_spi.c:1420-1490",
            },
            {
                "peripheral": "USART2",
                "transfer_mode": "polling",
                "rationale": "debug console",
                "citation": "invented_source.c:1-2",
            },
        ],
        "file_tree": ["Core/Src/main.c"],
        "implementation_order": [
            {"order": 7, "title": "clock setup"},
            {"order": 7, "title": "SPI init"},
        ],
        "risks": [],
        "assumptions": [],
    }
)

UNIFIED_RESPONSE = {
    "query": "q",
    "strategy": "quota",
    "symbols": [
        {
            "name": "HAL_SPI_Transmit_DMA",
            "kind": "function",
            "signature": "HAL_StatusTypeDef HAL_SPI_Transmit_DMA(...)",
            "path": "stm32f4xx_hal_spi.c",
            "line_start": 1420,
            "line_end": 1490,
            "doc": "Transmit with DMA.",
            "chunk_id": "c1",
            "collection": "stm32",
            "match": "exact",
            "matched_term": "HAL_SPI_Transmit",
        }
    ],
    "type_context": [],
    "chunks": [],
    "pages": [],
    "identifiers": [],
    "fused": [],
    "warnings": [],
}


def _rag_client(handler=None) -> PageVaultClient:
    handler = handler or (lambda request: httpx.Response(200, json=UNIFIED_RESPONSE))
    return PageVaultClient(
        base_url="http://pagevault-api:8000",
        timeout=1.0,
        transport=httpx.MockTransport(handler),
    )


# --- contracts -------------------------------------------------------------


def test_contracts_round_trip_through_json():
    requirements = Requirements(mcu="STM32F407VG", family="STM32F4")
    restored = Requirements.model_validate(json.loads(json.dumps(dump(requirements))))

    assert restored == requirements
    assert restored.schema_version == SCHEMA_VERSION


@pytest.mark.parametrize(
    "reply",
    [
        '{"mcu": "STM32F407VG"}',
        '```json\n{"mcu": "STM32F407VG"}\n```',
        'Sure! Here it is:\n{"mcu": "STM32F407VG", "peripherals": []}\nHope that helps.',
    ],
)
def test_json_is_recovered_from_however_the_model_wrapped_it(reply):
    assert extract_json(reply)["mcu"] == "STM32F407VG"


def test_nested_objects_survive_extraction():
    reply = 'text {"a": {"b": {"c": 1}}, "d": "}"} trailing'
    assert extract_json(reply) == {"a": {"b": {"c": 1}}, "d": "}"}


def test_an_unparseable_reply_raises_contract_error():
    with pytest.raises(ContractError):
        parse_model(Requirements, "I cannot help with that.")


# --- requirements agent ----------------------------------------------------


def test_requirements_are_extracted_and_normalised(monkeypatch):
    llm = FakeLLM(REQUIREMENTS_REPLY)
    monkeypatch.setattr(requirements_module, "get_agent_llm", lambda _n: llm)

    requirements, warnings = asyncio.run(
        analyze_requirements("روی STM32F407 سنسور MPU6050 را با SPI و DMA بخوان")
    )

    # The model left "family" empty; it is derived from the MCU locally,
    # because retrieval filters on it.
    assert requirements.family == "STM32F4"
    assert requirements.rtos == "none"
    assert requirements.peripherals[0].protocol == "SPI"
    assert requirements.peripherals[0].transfer_mode == "dma"
    assert warnings == []


def test_ambiguity_is_recorded_rather_than_resolved(monkeypatch):
    monkeypatch.setattr(
        requirements_module, "get_agent_llm", lambda _n: FakeLLM(REQUIREMENTS_REPLY)
    )
    requirements, _ = asyncio.run(analyze_requirements("read a sensor"))

    assert requirements.open_questions


def test_a_missing_mcu_is_reported_not_invented(monkeypatch):
    reply = json.dumps({"summary": "blink an LED", "peripherals": []})
    monkeypatch.setattr(requirements_module, "get_agent_llm", lambda _n: FakeLLM(reply))

    requirements, warnings = asyncio.run(analyze_requirements("blink an LED"))

    assert requirements.mcu == ""
    assert any("MCU" in warning for warning in warnings)
    assert any("family" in warning for warning in warnings)


def test_a_malformed_reply_degrades_instead_of_failing(monkeypatch):
    monkeypatch.setattr(
        requirements_module, "get_agent_llm", lambda _n: FakeLLM("no JSON here")
    )

    requirements, warnings = asyncio.run(
        analyze_requirements("SPI on STM32F407 please")
    )

    assert requirements.family == "STM32F4"  # still recovered by regex
    assert requirements.open_questions
    assert any("degraded" in warning for warning in warnings)


# --- datasheet fan-out -----------------------------------------------------


def test_one_question_is_asked_per_peripheral_and_component():
    requirements = Requirements(
        mcu="STM32F407VG",
        peripherals=[
            PeripheralNeed(name="SPI1", role="read MPU6050", transfer_mode="dma"),
            PeripheralNeed(name="USART2", role="console"),
        ],
        external_components=["MPU6050"],
    )

    topics = [topic for topic, _q in build_questions(requirements)]
    questions = [question for _t, question in build_questions(requirements)]

    assert len(topics) == 3
    assert "with DMA" in questions[0]
    assert "with DMA" not in questions[1]


def test_findings_carry_citations_from_retrieval(monkeypatch):
    monkeypatch.setattr(datasheet_module, "get_rag_client", _rag_client)
    monkeypatch.setattr(
        datasheet_module, "get_agent_llm", lambda _n: FakeLLM("Use HAL_SPI_Transmit_DMA.")
    )

    findings = asyncio.run(
        gather_hardware_findings(
            Requirements(
                mcu="STM32F407VG",
                family="STM32F4",
                peripherals=[PeripheralNeed(name="SPI1", transfer_mode="dma")],
            )
        )
    )

    assert findings.grounded is True
    assert findings.citations == ["stm32f4xx_hal_spi.c:1420-1490"]
    json.dumps(dump(findings))


def test_an_empty_knowledge_base_still_produces_findings(monkeypatch):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(datasheet_module, "get_rag_client", lambda: _rag_client(down))
    monkeypatch.setattr(
        datasheet_module, "get_agent_llm", lambda _n: FakeLLM("Warning: no sources.")
    )

    findings = asyncio.run(
        gather_hardware_findings(
            Requirements(peripherals=[PeripheralNeed(name="SPI1")])
        )
    )

    assert findings.findings  # the run continues
    assert findings.grounded is False
    assert findings.warnings


# --- architecture agent ----------------------------------------------------


def _design(monkeypatch, hardware: HardwareFindings, reply=ARCHITECTURE_REPLY):
    llm = FakeLLM(reply)
    monkeypatch.setattr(architecture_module, "get_agent_llm", lambda _n: llm)
    architecture, warnings = asyncio.run(
        design_architecture(
            Requirements(mcu="STM32F407VG", family="STM32F4"), hardware
        )
    )
    return architecture, warnings, llm


def _hardware() -> HardwareFindings:
    return HardwareFindings(
        family="STM32F4",
        findings=[
            HardwareFinding(
                topic="SPI1",
                question="?",
                answer="Enable TXDMAEN.",
                citations=["stm32f4xx_hal_spi.c:1420-1490"],
                grounded=True,
            )
        ],
    )


def test_a_verified_citation_is_kept(monkeypatch):
    architecture, _warnings, _llm = _design(monkeypatch, _hardware())
    spi = next(p for p in architecture.peripherals if p.peripheral == "SPI1")

    assert spi.citation == "stm32f4xx_hal_spi.c:1420-1490"
    assert architecture.citations == ["stm32f4xx_hal_spi.c:1420-1490"]


def test_an_invented_citation_is_demoted_to_an_assumption(monkeypatch):
    architecture, _warnings, _llm = _design(monkeypatch, _hardware())
    uart = next(p for p in architecture.peripherals if p.peripheral == "USART2")

    assert uart.citation == ""
    assert any("USART2" in assumption for assumption in architecture.assumptions)


def test_the_implementation_order_is_renumbered(monkeypatch):
    architecture, _warnings, _llm = _design(monkeypatch, _hardware())
    assert [step.order for step in architecture.implementation_order] == [1, 2]


def test_findings_reach_the_prompt(monkeypatch):
    _architecture, _warnings, llm = _design(monkeypatch, _hardware())
    prompt = llm.calls[0][1]["content"]

    assert "Enable TXDMAEN." in prompt
    assert "stm32f4xx_hal_spi.c:1420-1490" in prompt


def test_designing_without_sources_is_flagged(monkeypatch):
    _architecture, warnings, llm = _design(monkeypatch, HardwareFindings())

    assert any("unverified" in warning for warning in warnings)
    assert "None available" in llm.calls[0][1]["content"]


def test_a_malformed_architecture_reply_degrades(monkeypatch):
    architecture, warnings, _llm = _design(monkeypatch, _hardware(), reply="sorry")

    assert isinstance(architecture, Architecture)
    assert architecture.risks
    assert any("degraded" in warning for warning in warnings)


# --- routing ---------------------------------------------------------------


def test_the_router_runs_before_anything_else():
    assert agent_sequence_for(RequestType.full_project)[0] == ROUTER_NODE
    assert agent_sequence_for() == [ROUTER_NODE]


def test_full_project_runs_the_design_pipeline():
    assert agent_sequence_for(RequestType.full_project) == [
        "router",
        "requirements",
        "datasheet",
        "architecture",
        "cubemx",
    ]


@pytest.mark.parametrize(
    "request_type",
    [RequestType.debug, RequestType.optimize, RequestType.test],
)
def test_copilot_modes_skip_the_design_pipeline(request_type):
    assert pipeline_for(request_type) == ["mock_copilot"]


def test_every_request_type_has_a_pipeline():
    for request_type in RequestType:
        assert pipeline_for(request_type), f"no pipeline for {request_type}"


def test_the_graph_compiles_with_the_real_nodes():
    from app.orchestrator.graph import build_graph

    assert build_graph() is not None
