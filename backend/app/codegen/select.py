"""Deterministic pin and DMA selection for a CubeMX plan.

The language model may choose modes and parameters, but it never gets to
invent a pin mux or DMA route. This module completes a proposal from the
per-part device table, using stable ordering and a small backtracking search
so an early pin choice cannot hide a valid complete assignment.
"""

from dataclasses import dataclass, field
from itertools import product

from app.codegen import peripherals
from app.codegen.devicedata import DeviceData, DmaRoute
from app.orchestrator.contracts import CubeMXPlan, DmaConfig, PinAssignment

PIN_POLICIES = frozenset({"deterministic", "explicit", "llm"})

REQUIRED_SIGNALS: dict[str, tuple[str, ...]] = {
    "SPI": ("SCK", "MISO", "MOSI"),
    "I2C": ("SCL", "SDA"),
    "USART": ("TX", "RX"),
    "UART": ("TX", "RX"),
    "TIM": (),
}

DIRECTIONS = {
    "RX": "peripheral_to_memory",
    "TX": "memory_to_peripheral",
}


@dataclass
class Selection:
    selected_pins: list[str] = field(default_factory=list)
    selected_dma: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def required_signals(peripheral: str, mode: str = "") -> tuple[str, ...]:
    """Required GPIO signal suffixes for the supported P3 modes."""
    group = peripherals.family(peripheral)
    if group == "SPI":
        normalised = str(mode or "").strip().lower()
        if "receive_only" in normalised or "rx_only" in normalised:
            return ("SCK", "MISO")
        if "transmit_only" in normalised or "tx_only" in normalised:
            return ("SCK", "MOSI")
    return REQUIRED_SIGNALS.get(group, ())


def _pin_key(pin: str) -> tuple[str, int]:
    try:
        return peripherals.parse_pin(pin)
    except Exception:
        return (str(pin), 0)


def _assigned_signals(plan: CubeMXPlan, peripheral: str) -> dict[str, PinAssignment]:
    name = str(peripheral or "").strip().upper()
    return {
        str(assignment.signal or "").strip().upper(): assignment
        for assignment in plan.pins
        if str(assignment.peripheral or "").strip().upper() == name
        and str(assignment.signal or "").strip()
    }


def _pin_candidates(
    plan: CubeMXPlan,
    data: DeviceData,
) -> tuple[list[tuple[str, str, list[str]]], list[str]]:
    missing: list[tuple[str, str, list[str]]] = []
    errors: list[str] = []
    for config in plan.peripherals:
        name = str(config.peripheral or "").strip().upper()
        assigned = _assigned_signals(plan, name)
        for suffix in required_signals(name, config.mode):
            signal = f"{name}_{suffix}"
            if signal in assigned:
                continue
            candidates = data.pins_for(signal)
            if not candidates:
                errors.append(f"{name} requires {signal}, but this part offers no pin for it")
            missing.append((name, signal, candidates))
    return missing, errors


def select_pins(
    plan: CubeMXPlan,
    data: DeviceData,
    *,
    policy: str = "deterministic",
) -> Selection:
    result = Selection()
    policy = str(policy or "deterministic").strip().lower()
    if policy not in PIN_POLICIES:
        result.errors.append(f"unknown pin selection policy {policy!r}")
        return result

    missing, errors = _pin_candidates(plan, data)
    result.errors.extend(errors)
    missing = [item for item in missing if item[2]]
    if not missing:
        return result

    if policy == "explicit":
        for name, signal, candidates in missing:
            result.errors.append(
                f"{name} requires an explicit pin for {signal}; valid choices: "
                f"{', '.join(candidates)}"
            )
        return result

    if policy == "llm":
        for name, signal, candidates in missing:
            result.errors.append(
                f"{name} has no proposed pin for {signal}; the agent may choose only from "
                f"{', '.join(candidates)}"
            )
        return result

    occupied = {
        str(assignment.pin or "").strip().upper()
        for assignment in plan.pins
        if str(assignment.pin or "").strip()
    }
    choices = [candidates for _name, _signal, candidates in missing]
    selected: tuple[str, ...] | None = None
    for combination in product(*choices):
        normalised = tuple(pin.upper() for pin in combination)
        if len(set(normalised)) != len(normalised):
            continue
        if any(pin in occupied for pin in normalised):
            continue
        selected = combination
        break

    if selected is None:
        detail = "; ".join(
            f"{signal}: {', '.join(candidates)}"
            for _name, signal, candidates in missing
        )
        result.errors.append(
            "no complete conflict-free pin assignment exists with the pins already in use; "
            f"valid alternatives are {detail}"
        )
        return result

    for (name, signal, _candidates), pin in zip(missing, selected, strict=True):
        plan.pins.append(
            PinAssignment(
                pin=pin,
                signal=signal,
                peripheral=name,
                mode="alternate",
                pull="up" if peripherals.family(name) == "I2C" else "none",
                speed="very_high" if peripherals.family(name) == "SPI" else "high",
            )
        )
        result.selected_pins.append(f"{signal}={pin}")
    plan.pins.sort(key=lambda assignment: (_pin_key(assignment.pin), assignment.signal))
    return result


def _normalise_dma(config: DmaConfig, peripheral: str) -> None:
    request = str(config.request or "").strip().upper()
    direction = str(config.direction or "").strip().lower()
    if not request:
        suffix = ""
        if direction == DIRECTIONS["RX"]:
            suffix = "RX"
        elif direction == DIRECTIONS["TX"]:
            suffix = "TX"
        if suffix:
            request = f"{peripheral}_{suffix}"
            config.request = request
    if request.endswith("_RX") and not direction:
        config.direction = DIRECTIONS["RX"]
    elif request.endswith("_TX") and not direction:
        config.direction = DIRECTIONS["TX"]


def _route_description(route: DmaRoute) -> str:
    return f"{route.stream_name}/Channel {route.channel}"


def select_dma(plan: CubeMXPlan, data: DeviceData) -> Selection:
    result = Selection()
    used: set[str] = set()

    # Preserve complete explicit assignments first, so deterministic choices
    # for later requests route around resources the user already owns.
    for peripheral_config in plan.peripherals:
        name = str(peripheral_config.peripheral or "").strip().upper()
        for dma in peripheral_config.dma:
            _normalise_dma(dma, name)
            if dma.stream and dma.channel is not None:
                used.add(str(dma.stream).strip().upper())

    for peripheral_config in plan.peripherals:
        name = str(peripheral_config.peripheral or "").strip().upper()
        for dma in peripheral_config.dma:
            request = str(dma.request or "").strip().upper()
            if not request:
                result.errors.append(
                    f"{name}: DMA direction {dma.direction or 'is missing'} does not "
                    "identify a request"
                )
                continue
            routes = data.routes_for(request)
            if not routes:
                result.errors.append(f"{name}: {request} is not a DMA request on this part")
                continue
            if dma.stream and dma.channel is not None:
                if data.dma_route(request, dma.stream, dma.channel) is None:
                    valid = ", ".join(_route_description(route) for route in routes)
                    result.errors.append(
                        f"{name}: {request} cannot use {dma.stream}/Channel {dma.channel}; "
                        f"valid routes: {valid}"
                    )
                continue

            # A named stream without a channel is constrained to that stream;
            # an entirely blank route takes the first stable free choice.
            candidates = [
                route
                for route in routes
                if (not dma.stream or route.stream_name.upper() == dma.stream.upper())
                and route.stream_name.upper() not in used
            ]
            if not candidates:
                valid = ", ".join(_route_description(route) for route in routes)
                result.errors.append(
                    f"{name}: no free DMA stream for {request}; valid routes: {valid}"
                )
                continue
            route = candidates[0]
            dma.stream = route.stream_name
            dma.channel = route.channel
            used.add(route.stream_name.upper())
            result.selected_dma.append(f"{request}={_route_description(route)}")
    return result


def complete_plan(
    plan: CubeMXPlan,
    data: DeviceData,
    *,
    pin_policy: str = "deterministic",
) -> Selection:
    """Complete pins and DMA routes without validating any guessed fact."""
    pins = select_pins(plan, data, policy=pin_policy)
    dma = select_dma(plan, data)
    return Selection(
        selected_pins=pins.selected_pins,
        selected_dma=dma.selected_dma,
        errors=[*pins.errors, *dma.errors],
        warnings=[*pins.warnings, *dma.warnings],
    )
