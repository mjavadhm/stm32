"""P3 CubeMX agent: model proposal, deterministic hardware completion, artifacts."""

import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from app.agents.base import request_contract
from app.build import workspace
from app.codegen import boards, devicedata, ioc
from app.codegen.errors import CodegenError
from app.codegen.scaffold import scaffold_project, slug
from app.codegen.select import complete_plan
from app.codegen.validate import validate_plan
from app.core.llm import get_agent_llm, is_agent_enabled
from app.orchestrator.contracts import (
    Architecture,
    ClockPlan,
    CubeMXPlan,
    DmaConfig,
    HardwareFindings,
    PeripheralConfig,
    PinAssignment,
    Requirements,
    dump,
    parse_stored,
)

logger = logging.getLogger(__name__)
AGENT_NAME = "cubemx"
SUPPORTED_GROUPS = frozenset({"SPI", "I2C", "USART", "UART", "TIM"})


class PeripheralProposal(BaseModel):
    peripheral: str
    mode: str = ""
    parameters: dict[str, str] = Field(default_factory=dict)
    nvic_priority: int | None = None
    # Used only by the opt-in llm pin policy. Keys are complete signal names
    # and values must be candidates supplied in the prompt.
    pins: dict[str, str] = Field(default_factory=dict)
    citation: str = ""


class CubeMXProposal(BaseModel):
    peripherals: list[PeripheralProposal] = Field(default_factory=list)


_SYSTEM_PROMPT = """You configure supported STM32F4 peripherals.

Propose ONLY peripheral modes and HAL parameters. Do not invent an MCU, clock,
alternate-function number, or DMA controller/stream/channel. When candidate
pins are supplied and the pin policy is llm, you may choose only exact entries
from those candidates. Citations must come from the matching hardware topic.

Supported P3 modes are SPI full-duplex master, I2C, asynchronous USART/UART,
and timer base. Reply with ONLY JSON:
{
  "peripherals": [
    {"peripheral":"SPI1","mode":"master_full_duplex","parameters":{},
     "nvic_priority":null,"pins":{},"citation":""}
  ]
}"""


def _hardware_evidence(hardware: HardwareFindings) -> str:
    rows: list[str] = []
    for finding in hardware.findings:
        sources = ", ".join(finding.citations) or "none"
        rows.append(f"- {finding.topic}: {finding.answer}\n  allowed citations: {sources}")
    return "\n".join(rows) or "None available"


def _candidate_map(requirements: Requirements) -> dict[str, list[str]]:
    try:
        from app.codegen.devices import device_for

        if requirements.board:
            device = devicedata.load(boards.board_for(requirements.board).part)
        else:
            device = devicedata.load(device_for(requirements.mcu).part)
    except CodegenError:
        try:
            device = devicedata.load(device_for(requirements.mcu).part)
        except CodegenError:
            return {}
    result: dict[str, list[str]] = {}
    for need in requirements.peripherals:
        name = need.name.strip().upper()
        group = re.sub(r"\d+$", "", name)
        suffixes = {
            "SPI": ("SCK", "MISO", "MOSI"),
            "I2C": ("SCL", "SDA"),
            "USART": ("TX", "RX"),
            "UART": ("TX", "RX"),
        }.get(group, ())
        for suffix in suffixes:
            signal = f"{name}_{suffix}"
            result[signal] = device.pins_for(signal)
    return result


def build_user_prompt(
    requirements: Requirements,
    hardware: HardwareFindings,
    architecture: Architecture,
    *,
    pin_policy: str,
) -> str:
    return (
        f"# Requirements\n{requirements.model_dump_json(indent=2)}\n\n"
        f"# Architecture\n{architecture.model_dump_json(indent=2)}\n\n"
        f"# Hardware evidence\n{_hardware_evidence(hardware)}\n\n"
        f"# Pin policy\n{pin_policy}\n\n"
        f"# Valid pin candidates\n{_candidate_map(requirements)}"
    )


def _fallback_proposal(requirements: Requirements, architecture: Architecture) -> CubeMXProposal:
    proposals: list[PeripheralProposal] = []
    architecture_by_name = {
        item.peripheral.strip().upper(): item for item in architecture.peripherals
    }
    names: list[str] = []
    names.extend(
        item.name.strip().upper()
        for item in requirements.peripherals
        if item.name.strip()
    )
    names.extend(
        item.peripheral.strip().upper()
        for item in architecture.peripherals
        if item.peripheral.strip()
    )
    for name in dict.fromkeys(names):
        item = architecture_by_name.get(name)
        group = re.sub(r"\d+$", "", name)
        mode = item.mode if item and item.mode else {
            "SPI": "master_full_duplex",
            "I2C": "i2c",
            "USART": "asynchronous",
            "UART": "asynchronous",
            "TIM": "time_base",
        }.get(group, "")
        proposals.append(
            PeripheralProposal(
                peripheral=name,
                mode=mode,
                citation=item.citation if item else "",
            )
        )
    return CubeMXProposal(peripherals=proposals)


def _base_plan(requirements: Requirements) -> tuple[CubeMXPlan, list[str]]:
    if requirements.board:
        try:
            return boards.plan_for(boards.board_for(requirements.board))
        except CodegenError as error:
            board_warning = f"No built-in board profile matched {requirements.board!r}: {error}"
        else:  # pragma: no cover - return above
            board_warning = ""
    else:
        board_warning = ""
    return (
        CubeMXPlan(
            mcu=requirements.mcu,
            board=requirements.board,
            clock=ClockPlan(
                source="hsi",
                sysclk_hz=16_000_000,
                hclk_hz=16_000_000,
                apb1_hz=16_000_000,
                apb2_hz=16_000_000,
            ),
            assumptions=[
                "Used the safe 16 MHz HSI baseline because no validated board "
                "clock was available."
            ],
            warnings=[board_warning] if board_warning else [],
        ),
        [board_warning] if board_warning else [],
    )


def _architecture_pins(architecture: Architecture) -> list[PinAssignment]:
    assignments: list[PinAssignment] = []
    for peripheral in architecture.peripherals:
        name = peripheral.peripheral.strip().upper()
        for raw in peripheral.pins:
            match = re.search(r"\bP[A-K](?:1[0-5]|\d)\b", raw.upper())
            if not match:
                continue
            suffix_match = re.search(r"\b(SCK|MISO|MOSI|SCL|SDA|TX|RX)\b", raw.upper())
            if not suffix_match:
                continue
            assignments.append(
                PinAssignment(
                    pin=match.group(0),
                    signal=f"{name}_{suffix_match.group(1)}",
                    peripheral=name,
                    mode="alternate",
                    citation=peripheral.citation,
                )
            )
    return assignments


def _transfer_modes(requirements: Requirements, architecture: Architecture) -> dict[str, str]:
    result = {
        item.name.strip().upper(): item.transfer_mode.strip().lower()
        for item in requirements.peripherals
    }
    for item in architecture.peripherals:
        if item.transfer_mode:
            result[item.peripheral.strip().upper()] = item.transfer_mode.strip().lower()
    return result


def _valid_citations(hardware: HardwareFindings, peripheral: str) -> set[str]:
    name = peripheral.lower()
    valid: set[str] = set()
    for finding in hardware.findings:
        if name in finding.topic.lower():
            valid.update(finding.citations)
    return valid


def _merge_proposal(
    plan: CubeMXPlan,
    proposal: CubeMXProposal,
    requirements: Requirements,
    hardware: HardwareFindings,
    architecture: Architecture,
    *,
    pin_policy: str,
) -> list[str]:
    warnings: list[str] = []
    by_name = {
        config.peripheral.strip().upper(): config for config in plan.peripherals
    }
    transfers = _transfer_modes(requirements, architecture)
    candidates = _candidate_map(requirements)
    proposed_names = {
        item.peripheral.strip().upper() for item in proposal.peripherals if item.peripheral.strip()
    }
    for fallback in _fallback_proposal(requirements, architecture).peripherals:
        if fallback.peripheral.strip().upper() not in proposed_names:
            proposal.peripherals.append(fallback)
    for item in proposal.peripherals:
        name = item.peripheral.strip().upper()
        if not name:
            continue
        group = re.sub(r"\d+$", "", name)
        if group not in SUPPORTED_GROUPS:
            warnings.append(
                f"{name}: P3 supports only GPIO, SPI, I2C, USART/UART, and timer base; "
                "the peripheral was not added"
            )
            continue
        citation = item.citation
        if citation and citation not in _valid_citations(hardware, name):
            warnings.append(
                f"{name}: citation {citation!r} does not belong to the matching "
                "hardware topic; moved to assumptions"
            )
            plan.assumptions.append(
                f"{name} mode/parameters were proposed without matching evidence."
            )
            citation = ""
        config = by_name.get(name)
        if config is None:
            config = PeripheralConfig(peripheral=name)
            plan.peripherals.append(config)
            by_name[name] = config
        config.mode = item.mode or config.mode
        config.parameters.update(item.parameters)
        if item.nvic_priority is not None:
            config.nvic_priority = item.nvic_priority
        config.citation = citation or config.citation

        if transfers.get(name) == "dma" and not config.dma:
            if group in {"SPI", "I2C", "USART", "UART"}:
                config.dma = [
                    DmaConfig(
                        request=f"{name}_RX",
                        direction="peripheral_to_memory",
                        nvic_priority=5,
                    ),
                    DmaConfig(
                        request=f"{name}_TX",
                        direction="memory_to_peripheral",
                        nvic_priority=5,
                    ),
                ]
        if pin_policy == "llm":
            for signal, pin in item.pins.items():
                signal_name = signal.strip().upper()
                pin_name = pin.strip().upper()
                if pin_name not in candidates.get(signal_name, []):
                    warnings.append(
                        f"{name}: ignored proposed {signal_name}={pin_name}; it was not "
                        "in the supplied candidates"
                    )
                    continue
                plan.pins.append(
                    PinAssignment(
                        pin=pin_name,
                        signal=signal_name,
                        peripheral=name,
                        mode="alternate",
                    )
                )
    if pin_policy in {"explicit", "deterministic"}:
        for assignment in _architecture_pins(architecture):
            if not any(
                pin.pin.upper() == assignment.pin.upper()
                and pin.signal.upper() == assignment.signal.upper()
                for pin in plan.pins
            ):
                plan.pins.append(assignment)
    return warnings


async def create_cubemx_plan(
    requirements: Requirements,
    hardware: HardwareFindings,
    architecture: Architecture,
    *,
    project_id: str,
    project_name: str,
    pin_policy: str = "deterministic",
) -> tuple[CubeMXPlan, dict[str, Any]]:
    plan, warnings = _base_plan(requirements)
    proposal: CubeMXProposal
    fallback_reason = ""
    if not is_agent_enabled(AGENT_NAME):
        proposal = _fallback_proposal(requirements, architecture)
        fallback_reason = "CubeMX agent is disabled; used the deterministic architecture fallback."
    else:
        try:
            proposal, repair_warnings, _reply = await request_contract(
                get_agent_llm(AGENT_NAME),
                CubeMXProposal,
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": build_user_prompt(
                            requirements, hardware, architecture, pin_policy=pin_policy
                        ),
                    },
                ],
            )
            warnings.extend(repair_warnings)
        except Exception as error:
            logger.warning("cubemx proposal failed: %s", error)
            proposal = _fallback_proposal(requirements, architecture)
            fallback_reason = (
                f"CubeMX agent unavailable or malformed ({error}); used the "
                "deterministic architecture fallback."
            )
    if fallback_reason:
        warnings.append(fallback_reason)
        if pin_policy == "llm":
            warnings.append(
                "The llm pin policy could not run, so deterministic pin selection "
                "was used for this fallback."
            )

    warnings.extend(
        _merge_proposal(
            plan,
            proposal,
            requirements,
            hardware,
            architecture,
            pin_policy=pin_policy,
        )
    )

    errors: list[str] = []
    data = None
    supported_device = True
    try:
        from app.codegen.devices import device_for

        device = device_for(plan.mcu)
        data = devicedata.load(device.part)
        selection = complete_plan(plan, data, pin_policy=pin_policy)
        warnings.extend(selection.warnings)
        errors.extend(selection.errors)
        report = validate_plan(plan, data=data)
        warnings.extend(report.warnings)
        errors.extend(report.errors)
    except CodegenError as error:
        errors.append(str(error))
        plan.validated = False
        supported_device = False

    complete_mcu = ioc.mcu_metadata(plan.mcu) is not None
    if not complete_mcu:
        errors.append(
            f"{plan.mcu or 'MCU'} is unsupported or insufficiently specific; "
            "supported CubeMX variants are STM32F407VGTx, STM32F411CEUx, "
            "and STM32F411RETx"
        )
        plan.validated = False

    for warning in warnings:
        if warning and warning not in plan.warnings:
            plan.warnings.append(warning)

    project_file = f"{slug(project_name, project_id)}.ioc"
    workspace.ensure_workspace(project_id, clean=True)
    ioc_path = ""
    if supported_device and complete_mcu:
        ioc_path = ioc.write_ioc(project_id, plan, name=project_file, data=data)
    else:
        warnings.append("The .ioc file was not written because the MCU is not fully supported.")
    scaffold_files: list[str] = []
    scaffold_warnings: list[str] = []
    if plan.validated:
        scaffold = scaffold_project(
            project_id,
            plan,
            clean=False,
            target=project_name,
            summary=requirements.summary,
        )
        scaffold_files = scaffold.files
        scaffold_warnings = scaffold.warnings
        for warning in scaffold_warnings:
            if warning not in plan.warnings:
                plan.warnings.append(warning)
    else:
        warnings.append("Scaffold generation was skipped because the CubeMX plan is not validated.")

    artifacts = {
        "validated": plan.validated,
        "ioc_path": ioc_path,
        "scaffold_files": scaffold_files,
        "warnings": list(dict.fromkeys([*warnings, *scaffold_warnings, *plan.warnings])),
        "errors": list(dict.fromkeys(errors)),
        "assumptions": plan.assumptions,
        "hardware_data": {
            "part": data.part if data else "",
            "source": data.source if data else "",
            "notes": data.notes if data else [],
        },
    }
    return plan, artifacts


async def cubemx_node(state: dict[str, Any]) -> dict[str, Any]:
    requirements = parse_stored(Requirements, state.get("requirements"))
    hardware = parse_stored(HardwareFindings, state.get("hardware"))
    architecture = parse_stored(Architecture, state.get("architecture"))
    plan, artifacts = await create_cubemx_plan(
        requirements,
        hardware,
        architecture,
        project_id=state.get("project_id", ""),
        project_name=state.get("project_name", "") or state.get("project_id", "project"),
        pin_policy=state.get("pin_selection_policy", "deterministic"),
    )
    return {"cubemx": dump(plan), "cubemx_artifacts": artifacts}
