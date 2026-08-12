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

# v2 (M4 prep): hardware findings gained `cited`, modules and implementation
# steps gained `citations`, and Architecture gained `evidence`. Every new field
# has a default, so a v1 row still parses -- the bump exists so a reader can
# tell whether evidence linkage is expected to be present.
SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2})


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
    # Sources retrieval *offered* for this question.
    citations: list[str] = Field(default_factory=list)
    # Sources the answer actually referenced. The gap between the two is the
    # honest measure of grounding.
    cited: list[str] = Field(default_factory=list)
    grounded: bool = False  # False => answered without retrieved sources

    @property
    def verified(self) -> bool:
        """The answer leaned on retrieval, not just co-existed with it.

        `grounded` only says the knowledge base returned something -- with a
        fixed top-k it almost always does. M4 turns these answers into
        register-level code, so "the model quoted a real source" has to be a
        separate, stricter signal.
        """
        return bool(self.cited)


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

    @property
    def verified(self) -> bool:
        """At least one answer actually referenced a retrieved source."""
        return any(finding.verified for finding in self.findings)

    @property
    def coverage(self) -> float:
        """Share of topics whose answer cited a source (0..1). Eval metric."""
        if not self.findings:
            return 0.0
        verified = sum(1 for finding in self.findings if finding.verified)
        return round(verified / len(self.findings), 3)

    def citations_for(self, topic: str) -> list[str]:
        """Sources retrieved for one topic.

        The architecture agent validates each decision against its own topic:
        a reference pulled for USART2 does not support an SPI1 claim.
        """
        key = topic.strip().lower()
        for finding in self.findings:
            if finding.topic.strip().lower() == key:
                return list(finding.citations)
        return []

    def evidence_map(self) -> dict[str, list[str]]:
        """topic -> sources, for the design that M4 will build from."""
        return {
            finding.topic: list(finding.citations)
            for finding in self.findings
            if finding.citations
        }


# --------------------------------------------------------------------------
# Architecture Agent
# --------------------------------------------------------------------------


class Module(BaseModel):
    name: str  # mpu6050
    path: str = ""  # Core/Src/mpu6050.c
    layer: str = ""  # driver | service | app | hal
    responsibility: str = ""
    depends_on: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)


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
    """One buildable step of the plan.

    M4 walks this list and generates code step by step, so a step carries the
    files it touches and the sources that justify it: the firmware agent then
    prompts with exactly that evidence instead of retrieving again blindly.
    """

    order: int
    title: str
    detail: str = ""
    modules: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)


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
    # topic -> sources retrieved for it. Lets M4 (and a reviewer) walk the
    # chain requirement -> evidence -> decision -> generated file.
    evidence: dict[str, list[str]] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# CubeMX Agent (M4)
# --------------------------------------------------------------------------


class PinAssignment(BaseModel):
    """One pin and the job it was given."""

    pin: str  # PA5
    signal: str = ""  # SPI1_SCK
    peripheral: str = ""  # SPI1
    mode: str = "alternate"  # alternate | input | output | analog | event
    pull: str = "none"  # none | up | down
    speed: str = "high"  # low | medium | high | very_high
    # Filled in by validation against the MCU table, not by the model: an
    # alternate-function number is a fact to look up, not a design choice.
    alternate: int | None = None
    citation: str = ""


class ClockPlan(BaseModel):
    """The clock tree, in Hz. Never "84 MHz" as prose."""

    source: str = "hsi"  # hsi | hse | hse_bypass
    hse_hz: int = 0
    pll_m: int = 0
    pll_n: int = 0
    pll_p: int = 0
    pll_q: int = 0
    sysclk_hz: int = 0
    hclk_hz: int = 0
    apb1_hz: int = 0
    apb2_hz: int = 0
    citation: str = ""


class DmaConfig(BaseModel):
    stream: str = ""  # DMA2_Stream3
    channel: int | None = None
    direction: str = ""  # peripheral_to_memory | memory_to_peripheral
    priority: str = "low"
    mode: str = "normal"  # normal | circular
    fifo: bool = False


class PeripheralConfig(BaseModel):
    """A peripheral the way CubeMX would configure it."""

    peripheral: str  # SPI1
    mode: str = ""  # master_full_duplex | asynchronous | ...
    parameters: dict[str, str] = Field(default_factory=dict)  # BaudRatePrescaler: "16"
    dma: list[DmaConfig] = Field(default_factory=list)
    nvic_priority: int | None = None
    citation: str = ""


class CubeMXPlan(Contract):
    """Everything needed to emit a `.ioc` file and the peripheral init code.

    `validated` is the same idea as `HardwareFinding.verified`: a plan that was
    never checked against the MCU's pin and DMA tables must not look like one
    that was. A model will assign SPI1_MOSI to a pin that cannot carry it with
    complete confidence, so the flag -- not the prose -- is what downstream
    code trusts.
    """

    mcu: str = ""
    board: str = ""
    clock: ClockPlan = Field(default_factory=ClockPlan)
    pins: list[PinAssignment] = Field(default_factory=list)
    peripherals: list[PeripheralConfig] = Field(default_factory=list)
    middlewares: list[str] = Field(default_factory=list)  # FREERTOS, FATFS ...
    rtos: str = "none"
    validated: bool = False
    warnings: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)

    def handle(self, peripheral: str) -> str:
        """CubeMX handle name: SPI1 -> hspi1, USART2 -> huart2.

        The firmware agent is given these names explicitly. Left to itself a
        model invents `spi1_handle` or `hSpi1`, and the code stops compiling
        against the init file we generated.
        """
        name = peripheral.strip().upper()
        if name.startswith(("USART", "UART")):
            return f"huart{name.removeprefix('USART').removeprefix('UART')}"
        return f"h{name.lower()}"


# --------------------------------------------------------------------------
# Firmware Agent (M4)
# --------------------------------------------------------------------------


class SourceFile(BaseModel):
    """One file of the generated project."""

    path: str  # Core/Src/mpu6050.c
    purpose: str = ""
    contents: str = ""
    step_order: int = 0  # the implementation step that produced it
    citations: list[str] = Field(default_factory=list)
    # False => rendered from a template. The repair loop only ever asks the
    # model to fix files the model actually wrote; linker scripts and startup
    # code are ours, and a compiler error in them is our bug to fix once.
    generated: bool = True


class FirmwareBundle(Contract):
    files: list[SourceFile] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    evidence: dict[str, list[str]] = Field(default_factory=dict)

    @property
    def paths(self) -> list[str]:
        return [source.path for source in self.files]

    def file(self, path: str) -> SourceFile | None:
        for source in self.files:
            if source.path == path:
                return source
        return None


# --------------------------------------------------------------------------
# Build (M4)
# --------------------------------------------------------------------------

BUILD_OK = "ok"
BUILD_FAILED = "failed"
BUILD_TIMEOUT = "timeout"
BUILD_UNAVAILABLE = "unavailable"  # the sandbox itself could not be reached


class Diagnostic(BaseModel):
    """One compiler or linker message, with coordinates.

    Stored structured rather than as a log blob because two different
    consumers need it: the M4 repair loop, which must point the model at a
    file and a line, and the M5 debug agent. Parsing the same prose in two
    places is how two readings of the same error start disagreeing.
    """

    file: str = ""
    line: int = 0
    column: int = 0
    severity: str = "error"  # error | warning | note
    code: str = ""  # -Wunused-variable
    message: str = ""
    tool: str = "gcc"  # gcc | ld | make

    def as_prompt(self) -> str:
        where = self.file or "<unknown>"
        if self.line:
            where += f":{self.line}"
        code = f" [{self.code}]" if self.code else ""
        return f"{where}: {self.severity}: {self.message}{code}"


class BuildSize(BaseModel):
    """`arm-none-eabi-size` output, and what it means for this device."""

    text: int = 0
    data: int = 0
    bss: int = 0
    flash_total: int = 0  # device capacity; 0 = unknown
    ram_total: int = 0

    @property
    def flash_bytes(self) -> int:
        return self.text + self.data

    @property
    def ram_bytes(self) -> int:
        return self.data + self.bss

    @property
    def flash_pct(self) -> float:
        if not self.flash_total:
            return 0.0
        return round(100.0 * self.flash_bytes / self.flash_total, 1)

    @property
    def ram_pct(self) -> float:
        if not self.ram_total:
            return 0.0
        return round(100.0 * self.ram_bytes / self.ram_total, 1)


class BuildResult(Contract):
    """The outcome of one compile attempt.

    A failed build is a result, not an exception: the project is still
    delivered, with its errors attached, and M5 picks them up.
    """

    status: str = BUILD_OK  # ok | failed | timeout | unavailable
    exit_code: int = 0
    duration_ms: int = 0
    toolchain: str = ""  # arm-none-eabi-gcc 12.2.0
    command: str = ""
    attempt: int = 1
    artifacts: dict[str, str] = Field(default_factory=dict)  # elf|bin|hex|map -> path
    size: BuildSize = Field(default_factory=BuildSize)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    log_tail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == BUILD_OK

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "warning"]

    def first_errors(self, limit: int = 5) -> list[Diagnostic]:
        """What the repair loop is allowed to see.

        One root cause usually produces a cascade; feeding the model fifty
        messages buys nothing but tokens.
        """
        return self.errors[:limit]


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


def parse_stored(model: type[TContract], data: dict[str, Any] | None) -> TContract:
    """Validate a contract that was produced earlier, not by an LLM.

    Used when a node reads what a previous node wrote (and, from M4 on, when
    a run reads a `TaskRun.result` row written by older code). An unknown
    `schema_version` fails loudly here instead of quietly validating into an
    empty object that costs the design every citation it had.
    """
    payload = dict(data or {})
    version = payload.get("schema_version", SCHEMA_VERSION)
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ContractError(
            f"{model.__name__}: unsupported schema_version {version!r} "
            f"(this build supports {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
        )
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ContractError(f"{model.__name__} validation failed: {exc}") from exc


def dump(contract: BaseModel) -> dict[str, Any]:
    """JSON-safe dict for `TaskRun.result` and the API."""
    return contract.model_dump(mode="json")
