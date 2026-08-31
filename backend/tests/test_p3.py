import asyncio
from pathlib import Path

from app.agents import cubemx as cubemx_module
from app.codegen.devicedata import DeviceData, DmaRoute
from app.codegen.ioc import mcu_metadata, render_ioc
from app.codegen.select import complete_plan
from app.codegen.validate import validate_plan
from app.orchestrator.contracts import (
    Architecture,
    CubeMXPlan,
    DmaConfig,
    HardwareFinding,
    HardwareFindings,
    PeripheralConfig,
    PeripheralNeed,
    PeripheralPlan,
    PinAssignment,
    Requirements,
)


def device_data() -> DeviceData:
    return DeviceData(
        part="stm32f407xx",
        source="fixture",
        pins={
            "PA2": {"USART2_TX": 7},
            "PA3": {"USART2_RX": 7},
            "PA5": {"SPI1_SCK": 5},
            "PA6": {"SPI1_MISO": 5},
            "PA7": {"SPI1_MOSI": 5},
            "PB3": {"SPI1_SCK": 5},
            "PB4": {"SPI1_MISO": 5},
            "PB5": {"SPI1_MOSI": 5},
            "PD12": {},
        },
        vectors={"SPI1": 35, "USART2": 38},
        instances=["SPI1", "USART2", "TIM3"],
        dma_routes={
            "SPI1_RX": [DmaRoute(2, 0, 3), DmaRoute(2, 2, 3)],
            "SPI1_TX": [DmaRoute(2, 3, 3), DmaRoute(2, 5, 3)],
        },
    )


def spi_dma_plan() -> CubeMXPlan:
    return CubeMXPlan(
        mcu="STM32F407VGT6",
        pins=[PinAssignment(pin="PD12", signal="LED_GREEN", mode="output")],
        peripherals=[
            PeripheralConfig(
                peripheral="SPI1",
                mode="master_full_duplex",
                dma=[
                    DmaConfig(request="SPI1_RX", direction="peripheral_to_memory", nvic_priority=5),
                    DmaConfig(request="SPI1_TX", direction="memory_to_peripheral", nvic_priority=5),
                ],
            )
        ],
    )


def test_selection_is_repeatable_and_avoids_reserved_board_pins():
    first = spi_dma_plan()
    second = spi_dma_plan()
    first.pins.append(PinAssignment(pin="PA5", signal="BOARD_LED", mode="output"))
    second.pins.append(PinAssignment(pin="PA5", signal="BOARD_LED", mode="output"))

    complete_plan(first, device_data())
    complete_plan(second, device_data())

    assert [(pin.signal, pin.pin) for pin in first.pins] == [
        (pin.signal, pin.pin) for pin in second.pins
    ]
    assert next(pin.pin for pin in first.pins if pin.signal == "SPI1_SCK") == "PB3"
    assert [dma.stream for dma in first.peripherals[0].dma] == [
        "DMA2_Stream0",
        "DMA2_Stream3",
    ]


def test_explicit_policy_names_valid_missing_choices():
    plan = spi_dma_plan()
    result = complete_plan(plan, device_data(), pin_policy="explicit")

    assert result.errors
    assert "PA5" in result.errors[0]
    assert not plan.pins[-1].signal.startswith("SPI1")


def test_validation_rejects_pin_and_dma_collisions():
    plan = spi_dma_plan()
    plan.pins += [
        PinAssignment(pin="PA5", signal="SPI1_SCK", peripheral="SPI1"),
        PinAssignment(pin="PA5", signal="SPI1_MISO", peripheral="SPI1"),
        PinAssignment(pin="PA7", signal="SPI1_MOSI", peripheral="SPI1"),
    ]
    plan.peripherals[0].dma[0].stream = "DMA2_Stream0"
    plan.peripherals[0].dma[0].channel = 3
    plan.peripherals[0].dma[1].stream = "DMA2_Stream0"
    plan.peripherals[0].dma[1].channel = 3
    plan.peripherals[0].dma[1].request = "SPI1_RX"
    plan.peripherals[0].dma[1].direction = "peripheral_to_memory"

    report = validate_plan(plan, data=device_data())

    assert any("assigned more than once" in error for error in report.errors)
    assert any("shared by" in error for error in report.errors)
    assert plan.validated is False


def test_ioc_uses_exact_supported_identifiers_and_stable_dma_rows():
    plan = spi_dma_plan()
    complete_plan(plan, device_data())
    validate_plan(plan, data=device_data())
    text = render_ioc(plan, data=device_data())

    assert mcu_metadata("STM32F407VGT6") == (
        "STM32F407VGTx",
        "STM32F407VGT6",
        "LQFP100",
    )
    assert mcu_metadata("STM32F411CEU6") == (
        "STM32F411CEUx",
        "STM32F411CEU6",
        "UFQFPN48",
    )
    assert mcu_metadata("STM32F411RET6") == (
        "STM32F411RETx",
        "STM32F411RET6",
        "LQFP64",
    )
    assert mcu_metadata("STM32F411") is None
    assert "Mcu.Name=STM32F407VGTx" in text
    assert "Mcu.IP3=SPI1" in text
    assert "SPI1.DMAReq.0=SPI1_RX" in text
    assert "SPI1.DMAReq.1=SPI1_TX" in text


def test_disabled_agent_uses_architecture_fallback(monkeypatch):
    monkeypatch.setattr(cubemx_module, "is_agent_enabled", lambda _name: False)
    fake_data = type(
        "Data",
        (),
        {"load": staticmethod(lambda _part: device_data())},
    )
    monkeypatch.setattr(cubemx_module, "devicedata", fake_data)
    monkeypatch.setattr(
        cubemx_module,
        "validate_plan",
        lambda plan, data=None: validate_plan(plan, data=data),
    )
    monkeypatch.setattr(
        cubemx_module.ioc,
        "write_ioc",
        lambda *args, **kwargs: "demo.ioc",
    )
    fake_scaffold = type("S", (), {"files": ["Makefile"], "warnings": []})
    monkeypatch.setattr(
        cubemx_module,
        "scaffold_project",
        lambda *args, **kwargs: fake_scaffold(),
    )
    monkeypatch.setattr(
        cubemx_module.workspace,
        "ensure_workspace",
        lambda *args, **kwargs: Path("/tmp"),
    )

    plan, artifacts = asyncio.run(
        cubemx_module.create_cubemx_plan(
            Requirements(
                mcu="STM32F407VGT6",
                peripherals=[PeripheralNeed(name="SPI1", transfer_mode="dma")],
            ),
            HardwareFindings(),
            Architecture(),
            project_id="p3test",
            project_name="demo",
        )
    )

    assert plan.validated is True
    assert artifacts["ioc_path"] == "demo.ioc"
    assert any(
        "deterministic architecture fallback" in warning
        for warning in artifacts["warnings"]
    )


class BadLLM:
    async def chat(self, _messages, **_kwargs):
        return "not json"


def _mock_artifact_dependencies(monkeypatch):
    fake_data = type(
        "Data",
        (),
        {"load": staticmethod(lambda _part: device_data())},
    )
    monkeypatch.setattr(cubemx_module, "devicedata", fake_data)
    monkeypatch.setattr(
        cubemx_module,
        "validate_plan",
        lambda plan, data=None: validate_plan(plan, data=data),
    )
    monkeypatch.setattr(
        cubemx_module.ioc,
        "write_ioc",
        lambda *args, **kwargs: "demo.ioc",
    )
    fake_scaffold = type("S", (), {"files": ["Makefile"], "warnings": []})
    monkeypatch.setattr(
        cubemx_module,
        "scaffold_project",
        lambda *args, **kwargs: fake_scaffold(),
    )
    monkeypatch.setattr(
        cubemx_module.workspace,
        "ensure_workspace",
        lambda *args, **kwargs: Path("/tmp"),
    )


def test_malformed_agent_reply_falls_back(monkeypatch):
    _mock_artifact_dependencies(monkeypatch)
    monkeypatch.setattr(cubemx_module, "is_agent_enabled", lambda _name: True)
    monkeypatch.setattr(cubemx_module, "get_agent_llm", lambda _name: BadLLM())

    plan, artifacts = asyncio.run(
        cubemx_module.create_cubemx_plan(
            Requirements(
                mcu="STM32F407VGT6",
                peripherals=[PeripheralNeed(name="SPI1")],
            ),
            HardwareFindings(),
            Architecture(),
            project_id="p3test",
            project_name="demo",
        )
    )

    assert plan.validated is True
    assert any("unavailable or malformed" in warning for warning in artifacts["warnings"])


def test_invalid_citation_is_demoted_to_an_assumption(monkeypatch):
    _mock_artifact_dependencies(monkeypatch)
    monkeypatch.setattr(cubemx_module, "is_agent_enabled", lambda _name: False)
    hardware = HardwareFindings(
        findings=[
            HardwareFinding(
                topic="USART2 console",
                question="console?",
                answer="use USART2",
                citations=["usart-source"],
            )
        ]
    )
    architecture = Architecture(
        peripherals=[PeripheralPlan(peripheral="SPI1", citation="usart-source")]
    )

    plan, artifacts = asyncio.run(
        cubemx_module.create_cubemx_plan(
            Requirements(mcu="STM32F407VGT6"),
            hardware,
            architecture,
            project_id="p3test",
            project_name="demo",
        )
    )

    assert plan.peripherals[0].citation == ""
    assert any("does not belong" in warning for warning in artifacts["warnings"])
    assert any("without matching evidence" in item for item in plan.assumptions)


def test_unsupported_mcu_returns_no_ioc_or_scaffold(monkeypatch):
    monkeypatch.setattr(cubemx_module, "is_agent_enabled", lambda _name: False)
    monkeypatch.setattr(
        cubemx_module.workspace,
        "ensure_workspace",
        lambda *args, **kwargs: Path("/tmp"),
    )

    plan, artifacts = asyncio.run(
        cubemx_module.create_cubemx_plan(
            Requirements(mcu="STM32H743ZI"),
            HardwareFindings(),
            Architecture(),
            project_id="p3test",
            project_name="demo",
        )
    )

    assert plan.validated is False
    assert artifacts["ioc_path"] == ""
    assert artifacts["scaffold_files"] == []
    assert artifacts["errors"]
