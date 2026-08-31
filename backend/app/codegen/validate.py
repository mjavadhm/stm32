"""Checking a plan against the silicon before a single line is generated.

P2 refuses to invent an alternate-function number. This is where the numbers
come from instead, and it is the difference between `CubeMXPlan.validated`
being a flag a model set and a flag that means something.

The contract is narrow on purpose:

  * a pin that cannot carry the signal the plan gave it is an **error**, and
    the message names the pins that can -- the repair is then obvious to a
    model, to the panel, and to a person reading a build log;
  * a signal the plan left blank is filled in only when the pin leaves no
    choice, never by picking the most likely one;
  * an AF number the plan guessed and got wrong is corrected, with a warning,
    because the table is the authority and silence would hide the guess;
  * an interrupt vector shared with another peripheral is a warning, read
    from this part's own CMSIS header rather than from a list typed out for
    one family.

Nothing here raises: a plan with errors comes back with `validated` false and
the reasons attached, which is what a repair loop needs.
"""

from dataclasses import dataclass, field

from app.codegen import devicedata, peripherals
from app.codegen.devicedata import DeviceData
from app.codegen.devices import device_for
from app.codegen.errors import CodegenError
from app.codegen.select import DIRECTIONS, required_signals
from app.orchestrator.contracts import CubeMXPlan

ALTERNATE = "alternate"
# How many alternatives to list before a refusal turns into an essay.
MAX_SUGGESTIONS = 6


@dataclass
class Validation:
    """What was checked, and what did not survive it."""

    mcu: str = ""
    part: str = ""
    source: str = ""
    pins: int = 0  # alternate-function pins examined
    resolved: int = 0  # AF numbers filled in from the table
    dma: int = 0  # DMA routes checked
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _check_pins(plan: CubeMXPlan, data: DeviceData, report: Validation) -> None:
    owners: dict[str, str] = {}
    for assignment in plan.pins:
        try:
            port, number = peripherals.parse_pin(assignment.pin)
        except CodegenError as error:
            report.errors.append(str(error))
            continue
        pin = f"P{port}{number}"
        owner = str(assignment.signal or assignment.peripheral or assignment.mode).strip()
        if pin in owners:
            report.errors.append(
                f"{pin} is assigned more than once ({owners[pin]} and {owner or 'another use'})"
            )
        else:
            owners[pin] = owner or "unnamed use"
        offered = data.signals(pin)
        mode = str(assignment.mode or "").strip().lower()

        if mode != ALTERNATE:
            # A plain input or output needs no table entry, but a pin this
            # part does not have is worth saying out loud.
            if data.pins and not offered:
                report.warnings.append(
                    f"{pin}: this part's table does not list this pin; it may not exist "
                    f"on your package"
                )
            continue

        report.pins += 1
        peripheral = str(assignment.peripheral or "").strip().upper()
        signal = str(assignment.signal or "").strip().upper()

        if not signal:
            candidates = data.signals_of(pin, peripheral)
            if len(candidates) == 1:
                signal = candidates[0]
                assignment.signal = signal
            elif not candidates:
                report.errors.append(
                    f"{pin} carries nothing for {peripheral or 'that peripheral'}; "
                    f"it offers {', '.join(sorted(offered)) or 'no alternate function'}"
                )
                continue
            else:
                report.errors.append(
                    f"{pin} can serve {' or '.join(candidates)} and the plan does not say "
                    f"which; name the signal"
                )
                continue

        if signal not in offered:
            elsewhere = data.pins_for(signal)
            where = ", ".join(elsewhere[:MAX_SUGGESTIONS]) if elsewhere else "no pin on this part"
            report.errors.append(f"{pin} cannot carry {signal}; {signal} is available on {where}")
            continue

        number = offered[signal]
        if number is None:
            report.errors.append(
                f"{pin} carries {signal} without an alternate function; it is an analog "
                f"or direct connection, so the pin mode should not be 'alternate'"
            )
            continue

        if assignment.alternate is not None and assignment.alternate != number:
            report.warnings.append(
                f"{pin}: the plan said AF{assignment.alternate} for {signal}, the part's "
                f"table says AF{number}; used AF{number}"
            )
        assignment.alternate = number
        report.resolved += 1


def _check_peripherals(plan: CubeMXPlan, data: DeviceData, report: Validation) -> None:
    seen: set[str] = set()
    for config in plan.peripherals:
        name = str(config.peripheral or "").strip().upper()
        if not name:
            report.errors.append("a peripheral configuration has no peripheral name")
            continue
        if name in seen:
            report.errors.append(f"{name} is configured more than once")
            continue
        seen.add(name)
        if data.instances and not data.has(name):
            similar = [
                instance
                for instance in data.instances
                if instance.rstrip("0123456789") == name.rstrip("0123456789")
            ]
            have = ", ".join(similar) if similar else "none of that kind"
            report.errors.append(f"{name} is not on this part; it has {have}")
            continue
        if config.nvic_priority is not None and not 0 <= config.nvic_priority <= 15:
            report.errors.append(
                f"{name}: NVIC priority {config.nvic_priority} is outside 0..15"
            )
        if config.nvic_priority is None:
            continue
        vectors = data.vectors_for(name)
        if not vectors:
            report.warnings.append(
                f"{name}: the plan asks for interrupt priority {config.nvic_priority}, but "
                f"this part's vector table has no entry for it; no handler was generated"
            )
            continue
        for vector in vectors:
            shared = data.shares_vector(vector, name)
            if shared:
                report.warnings.append(
                    f"{name} shares the {vector} interrupt with {', '.join(shared)}; "
                    f"one handler serves both and has to tell them apart"
                )


def _check_required_signals(plan: CubeMXPlan, data: DeviceData, report: Validation) -> None:
    for config in plan.peripherals:
        name = str(config.peripheral or "").strip().upper()
        if data.instances and not data.has(name):
            continue
        assigned = {
            str(pin.signal or "").strip().upper()
            for pin in plan.pins
            if str(pin.peripheral or "").strip().upper() == name
        }
        for suffix in required_signals(name, config.mode):
            signal = f"{name}_{suffix}"
            if signal not in assigned:
                report.errors.append(f"{name} requires {signal}, but the plan has no pin for it")


def _check_dma(plan: CubeMXPlan, data: DeviceData, report: Validation) -> None:
    used: dict[str, str] = {}
    for config in plan.peripherals:
        peripheral = str(config.peripheral or "").strip().upper()
        for dma in config.dma:
            request = str(dma.request or "").strip().upper()
            direction = str(dma.direction or "").strip().lower()
            if not request:
                report.errors.append(f"{peripheral}: a DMA route has no request name")
                continue
            routes = data.routes_for(request)
            if not routes:
                report.errors.append(f"{peripheral}: {request} is not a DMA request on this part")
                continue
            if not request.startswith(f"{peripheral}_"):
                report.errors.append(
                    f"{peripheral}: DMA request {request} belongs to another peripheral"
                )
            expected = (
                DIRECTIONS["RX"] if request.endswith("_RX")
                else DIRECTIONS["TX"] if request.endswith("_TX")
                else ""
            )
            if expected and direction != expected:
                report.errors.append(
                    f"{peripheral}: {request} direction must be {expected}, not "
                    f"{direction or 'blank'}"
                )
            elif direction not in set(DIRECTIONS.values()):
                report.errors.append(
                    f"{peripheral}: DMA direction {direction or 'blank'} is not supported"
                )
            if not dma.stream or dma.channel is None:
                valid = ", ".join(
                    f"{route.stream_name}/Channel {route.channel}" for route in routes
                )
                report.errors.append(
                    f"{peripheral}: {request} has no complete DMA route; valid routes: {valid}"
                )
                continue
            if data.dma_route(request, dma.stream, dma.channel) is None:
                valid = ", ".join(
                    f"{route.stream_name}/Channel {route.channel}" for route in routes
                )
                report.errors.append(
                    f"{peripheral}: {request} cannot use {dma.stream}/Channel {dma.channel}; "
                    f"valid routes: {valid}"
                )
                continue
            stream = str(dma.stream).strip().upper()
            if stream in used:
                report.errors.append(
                    f"{dma.stream} is shared by {used[stream]} and {request}; choose another route"
                )
                continue
            used[stream] = request
            if dma.nvic_priority is not None and not 0 <= dma.nvic_priority <= 15:
                report.errors.append(
                    f"{request}: NVIC priority {dma.nvic_priority} is outside 0..15"
                )
            report.dma += 1


def validate_plan(plan: CubeMXPlan, *, data: DeviceData | None = None) -> Validation:
    """Check a plan against the part, filling in what can be looked up.

    Mutates the plan: `alternate` numbers are set from the table, blank
    signals are named when unambiguous, warnings are appended, and
    `validated` ends up true only if nothing was refused.
    """
    report = Validation(mcu=plan.mcu)
    try:
        device = device_for(plan.mcu)
        report.part = device.part
        if data is None:
            data = devicedata.load(device.part)
    except CodegenError as error:
        report.errors.append(str(error))
        plan.validated = False
        return report

    report.source = data.source
    _check_pins(plan, data, report)
    _check_peripherals(plan, data, report)
    _check_required_signals(plan, data, report)
    _check_dma(plan, data, report)

    plan.validated = report.ok
    for warning in report.warnings:
        if warning not in plan.warnings:
            plan.warnings.append(warning)
    return report
