"""Structured contracts exchanged between agents (M3).

Agents do not pass prose to each other. Each design agent emits one of these
models, it is validated here, and the next agent consumes typed fields.

Two rules make this survive later milestones:

* **Versioned.** Every contract carries `schema_version`. `TaskRun.result`
  rows outlive the code that wrote them, so a reader must be able to tell an
  old payload from a new one instead of silently mis-parsing it.
* **Tolerant.** `parse_model()` accepts raw JSON, fenced JSON, or JSON buried
  in prose, because local models wrap output in markdown no matter what the
  prompt says. A reply that cannot be salvaged raises `ContractError`, and the
  calling agent decides whether to degrade or fail.
"""

import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, Field, ValidationError

SCHEMA_VERSION = 1


class ContractError(ValueError):
    """An LLM reply could not be turned into a valid contract."""


class Contract(BaseModel):
    """Base for every agent-to-agent payload."""

    schema_version: int = SCHEMA_VERSION


# --------------------------------------------------------------------------
# Requirements Agent
# --------------------------------------------------------------------------


class PeripheralNeed(BaseModel):
    """One peripheral the firmware has to drive."""

    name: str  # SPI1, I2C2, TIM3, USART2 ...
    role: str = ""  # "read MPU6050", "debug console"
    protocol: str = ""  # SPI | I2C | UART | CAN | ADC ...
    transfer_mode: str = ""  # polling | interrupt | dma | "" if undecided


class Requirements(Contract):
    """What the user asked for, made explicit.

    `assumptions` and `open_questions` exist so the pipeline never blocks on
    ambiguity: an unstated detail is recorded and execution continues, instead
    of the agent quietly inventing a value.
    """

    summary: str = ""
    mcu: str = ""  # STM32F407VG
    family: str = ""  # STM32F4
    board: str = ""
    peripherals: list[PeripheralNeed] = Field(default_factory=list)
    external_components: list[str] = Field(default_factory=list)  # MPU6050 ...
    rtos: str = ""  # none | freertos
    constraints: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Datasheet Agent
# --------------------------------------------------------------------------


class HardwareFinding(BaseModel):
    """One answered hardware question, with its sources."""

    topic: str  # "SPI1 + DMA"
    question: str
    answer: str
    citations: list[str] = Field(default_factory=list)
    grounded: bool = False  # False => answered without retrieved sources


class HardwareFindings(Contract):
    family: str = ""
    findings: list[HardwareFinding] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def citations(self) -> list[str]:
        seen: list[str] = []
        for finding in self.findings:
            for citation in finding.citations:
                if citation not in seen:
                    seen.append(citation)
        return seen

    @property
    def grounded(self) -> bool:
        return any(finding.grounded for finding in self.findings)


# --------------------------------------------------------------------------
# Architecture Agent
# --------------------------------------------------------------------------


class Module(BaseModel):
    name: str  # mpu6050
    path: str = ""  # Core/Src/mpu6050.c
    layer: str = ""  # driver | service | app | hal
    responsibility: str = ""
    depends_on: list[str] = Field(default_factory=list)


class PeripheralPlan(BaseModel):
    """A concrete peripheral assignment, with the reason it was chosen.

    `rationale` and `citation` are what let a reviewer (or the M5 review agent)
    tell an evidence-based decision from a plausible guess.
    """

    peripheral: str  # SPI1
    mode: str = ""  # master, full-duplex, 8-bit
    transfer_mode: str = ""  # polling | interrupt | dma
    dma_stream: str = ""  # DMA2_Stream3 / Channel 3
    pins: list[str] = Field(default_factory=list)  # PA5 (SCK) ...
    clock_hint: str = ""
    rationale: str = ""
    citation: str = ""


class ImplementationStep(BaseModel):
    order: int
    title: str
    detail: str = ""
    modules: list[str] = Field(default_factory=list)


class Architecture(Contract):
    overview: str = ""
    driver_layer: str = ""  # hal | ll | mixed
    rtos: str = "none"
    modules: list[Module] = Field(default_factory=list)
    peripherals: list[PeripheralPlan] = Field(default_factory=list)
    file_tree: list[str] = Field(default_factory=list)
    implementation_order: list[ImplementationStep] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

TContract = TypeVar("TContract", bound=BaseModel)


def extract_json(text: str) -> dict[str, Any]:
    """Find the JSON object in an LLM reply.

    Three shapes are handled, in order of preference: a bare object, a fenced
    block, and an object embedded in commentary. The last case uses brace
    matching rather than a regex because nested objects are the normal case
    here, not the exception.
    """
    if not text or not text.strip():
        raise ContractError("empty reply")

    candidates: list[str] = []
    stripped = text.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    candidates.extend(match.strip() for match in _FENCE_RE.findall(text))

    start = text.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : index + 1])
                    break

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise ContractError("no JSON object found in reply")


def parse_model(model: type[TContract], text: str) -> TContract:
    """Parse an LLM reply into a contract model."""
    data = extract_json(text)
    data.setdefault("schema_version", SCHEMA_VERSION)
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ContractError(f"{model.__name__} validation failed: {exc}") from exc


def dump(contract: BaseModel) -> dict[str, Any]:
    """JSON-safe dict for `TaskRun.result` and the API."""
    return contract.model_dump(mode="json")
