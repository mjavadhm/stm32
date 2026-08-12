"""M4-readiness tests: evidence linkage, schema versioning, contract retry.

Same offline setup as `test_m3`: a fake LLM, no PageVault, no database. These
cover the machinery M4 (CubeMX + firmware generation) will lean on, so a
regression here shows up before code generation starts producing confident,
unsupported firmware.
"""

import asyncio
import json

import pytest

from app.agents import architecture as architecture_module
from app.agents.architecture import _enforce_evidence, design_architecture
from app.agents.base import request_contract
from app.orchestrator.contracts import (
    SUPPORTED_SCHEMA_VERSIONS,
    Architecture,
    ContractError,
    HardwareFinding,
    HardwareFindings,
    ImplementationStep,
    Module,
    PeripheralPlan,
    Requirements,
    dump,
    parse_stored,
)

SPI_SOURCE = "stm32f4xx_hal_spi.c:1420-1490"
UART_SOURCE = "stm32f4xx_hal_uart.c:200-260"


class ScriptedLLM:
    """Returns each reply in turn, so a retry can be observed."""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        index = min(len(self.calls) - 1, len(self.replies) - 1)
        return self.replies[index]


def _hardware() -> HardwareFindings:
    return HardwareFindings(
        family="STM32F4",
        findings=[
            HardwareFinding(
                topic="SPI1",
                question="How is SPI1 configured with DMA?",
                answer=f"Enable TXDMAEN. See {SPI_SOURCE}.",
                citations=[SPI_SOURCE],
                cited=[SPI_SOURCE],
                grounded=True,
            ),
            HardwareFinding(
                topic="USART2",
                question="How is USART2 configured?",
                answer="Use HAL_UART_Transmit.",
                citations=[UART_SOURCE],
                grounded=True,
            ),
        ],
    )


# --------------------------------------------------------------------------
# Retrieved != used
# --------------------------------------------------------------------------


def test_a_finding_is_verified_only_when_the_answer_cites_a_source():
    hardware = _hardware()
    offered, used = hardware.findings[1], hardware.findings[0]

    assert used.verified is True
    assert offered.verified is False  # sources existed, the answer ignored them
    assert hardware.grounded is True
    assert hardware.verified is True
    assert hardware.coverage == 0.5


def test_coverage_of_an_empty_run_is_zero_not_an_error():
    assert HardwareFindings().coverage == 0.0


# --------------------------------------------------------------------------
# Citations belong to the topic they were retrieved for
# --------------------------------------------------------------------------


def test_a_citation_borrowed_from_another_topic_is_kept_but_flagged():
    architecture = Architecture(
        peripherals=[PeripheralPlan(peripheral="SPI1", citation=UART_SOURCE)]
    )

    enforced, warnings = _enforce_evidence(architecture, _hardware())

    # Shared HAL files legitimately cover several peripherals, so it stays...
    assert enforced.peripherals[0].citation == UART_SOURCE
    # ...but a reviewer gets told.
    assert any("another topic" in warning for warning in warnings)


def test_an_invented_citation_is_dropped_with_a_machine_readable_warning():
    architecture = Architecture(
        peripherals=[
            PeripheralPlan(
                peripheral="SPI1",
                citation="invented_source.c:1-2",
                rationale="1 kHz streaming",
            )
        ]
    )

    enforced, warnings = _enforce_evidence(architecture, _hardware())

    assert enforced.peripherals[0].citation == ""
    assert enforced.citations == []
    assert any("no supporting source" in a for a in enforced.assumptions)
    # The eval harness counts hallucinations by this exact phrase.
    assert any("unverifiable citation dropped" in w for w in warnings)


def test_module_citations_are_filtered_to_what_was_retrieved():
    architecture = Architecture(
        modules=[Module(name="spi_bus", citations=[SPI_SOURCE, "ghost.c:1-2"])]
    )

    enforced, _ = _enforce_evidence(architecture, _hardware())

    assert enforced.modules[0].citations == [SPI_SOURCE]


# --------------------------------------------------------------------------
# What M4 reads: per-step evidence
# --------------------------------------------------------------------------


def test_a_step_inherits_the_evidence_of_the_topic_it_names():
    architecture = Architecture(
        implementation_order=[
            ImplementationStep(order=9, title="Bring up SPI1 with DMA"),
            ImplementationStep(order=4, title="Blink the status LED"),
        ]
    )

    enforced, _ = _enforce_evidence(architecture, _hardware())
    first, second = enforced.implementation_order

    assert [first.order, second.order] == [1, 2]  # renumbered, as before
    assert first.citations == [SPI_SOURCE]
    assert second.citations == []  # nothing to cite, so nothing is invented


def test_the_design_carries_the_topic_to_source_map():
    enforced, _ = _enforce_evidence(Architecture(), _hardware())

    assert enforced.evidence == {"SPI1": [SPI_SOURCE], "USART2": [UART_SOURCE]}


def test_a_design_built_on_uncited_sources_is_flagged(monkeypatch):
    """Retrieval worked, the model ignored it: that must not read as grounded."""
    hardware = HardwareFindings(
        findings=[
            HardwareFinding(
                topic="SPI1",
                question="q",
                answer="Use DMA.",  # no reference in the text
                citations=[SPI_SOURCE],
                grounded=True,
            )
        ]
    )
    reply = json.dumps(
        {
            "overview": "HAL with DMA",
            "peripherals": [{"peripheral": "SPI1", "citation": SPI_SOURCE}],
            "implementation_order": [{"order": 1, "title": "SPI1 bring-up"}],
        }
    )
    llm = ScriptedLLM(reply)
    monkeypatch.setattr(architecture_module, "get_agent_llm", lambda name: llm)

    architecture, warnings = asyncio.run(
        design_architecture(Requirements(summary="stream an IMU"), hardware)
    )

    assert architecture.citations == [SPI_SOURCE]
    assert any("no hardware answer referenced them" in w for w in warnings)


# --------------------------------------------------------------------------
# Stored contracts
# --------------------------------------------------------------------------


def test_a_stored_contract_from_an_unknown_schema_version_is_rejected():
    payload = dump(Requirements(summary="blink an LED"))

    assert parse_stored(Requirements, payload).summary == "blink an LED"
    assert parse_stored(Requirements, None).summary == ""  # empty state is fine

    payload["schema_version"] = max(SUPPORTED_SCHEMA_VERSIONS) + 1
    with pytest.raises(ContractError):
        parse_stored(Requirements, payload)


def test_a_v1_row_still_parses_after_the_bump():
    legacy = {"schema_version": 1, "summary": "older run", "mcu": "STM32F407VG"}

    requirements = parse_stored(Requirements, legacy)

    assert requirements.mcu == "STM32F407VG"


# --------------------------------------------------------------------------
# Contract retry (docs/architecture.md decision #3)
# --------------------------------------------------------------------------


def test_a_malformed_reply_is_repaired_on_the_second_attempt():
    good = json.dumps({"summary": "blink an LED", "mcu": "STM32F407VG"})
    llm = ScriptedLLM("Sure! Here is the plan you asked for.", good)

    requirements, warnings, _reply = asyncio.run(
        request_contract(llm, Requirements, [{"role": "user", "content": "hi"}])
    )

    assert requirements.summary == "blink an LED"
    assert len(llm.calls) == 2
    assert any("repaired" in warning for warning in warnings)
    # The second attempt shows the model its own reply plus the parser error.
    repair_turn = llm.calls[1]
    assert repair_turn[-2]["role"] == "assistant"
    assert "could not be parsed" in repair_turn[-1]["content"]


def test_retries_are_bounded_and_then_the_agent_decides():
    llm = ScriptedLLM("no json", "still no json", "nope")

    with pytest.raises(ContractError):
        asyncio.run(
            request_contract(
                llm,
                Requirements,
                [{"role": "user", "content": "hi"}],
                retries=1,
            )
        )

    assert len(llm.calls) == 2  # one attempt, one repair, then give up


def test_a_clean_reply_costs_exactly_one_call():
    llm = ScriptedLLM(json.dumps({"summary": "ok"}))

    _requirements, warnings, _reply = asyncio.run(
        request_contract(llm, Requirements, [{"role": "user", "content": "hi"}])
    )

    assert len(llm.calls) == 1
    assert warnings == []
