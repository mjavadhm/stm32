"""Board profiles: the facts a schematic decides and no datasheet knows.

The MCU table says an STM32F411 can run at 100 MHz. It cannot say that the
crystal soldered next to it is 25 MHz, that the LED hangs off PC13 and lights
when the pin goes *low*, or that there is no crystal at all and the clock
arrives from the debugger. Those are board facts, and getting one wrong
produces firmware that compiles, flashes, and does nothing -- the worst
failure mode available, because there is no error message to read.

So each board is written down once, with the document it came from, and the
clock tree is *solved* rather than copied. Given a crystal and a target
frequency there is exactly one set of PLL dividers worth using, and finding it
is a dozen lines of arithmetic. Asking a language model for PLLM/PLLN/PLLP is
asking it to do arithmetic it has no way to check, to produce a number no
compiler will object to and every UART baud rate depends on.
"""

from dataclasses import dataclass, field

from app.codegen.devices import Device, device_for
from app.codegen.errors import CodegenError
from app.codegen.peripherals import APB_DIVIDERS, HSI_HZ
from app.orchestrator.contracts import (
    ClockPlan,
    CubeMXPlan,
    PeripheralConfig,
    PinAssignment,
)

# PLL limits for STM32F4 (RM0090 / RM0383, "PLL configuration"). The VCO input
# has to land between 1 and 2 MHz; ST recommends the high end to limit jitter,
# while every vendor example for these boards divides down to exactly 1 MHz.
# The examples win: matching them means the numbers here can be compared
# against a known-good configuration, which is worth more than the jitter.
VCO_INPUT_MIN_HZ = 1_000_000
VCO_INPUT_MAX_HZ = 2_000_000
PREFERRED_VCO_INPUT_HZ = 1_000_000
VCO_OUTPUT_MIN_HZ = 100_000_000
VCO_OUTPUT_MAX_HZ = 432_000_000
PLL_M = range(2, 64)
PLL_N = range(50, 433)
PLL_P = (2, 4, 6, 8)
PLL_Q = range(2, 16)

# The OTG FS peripheral needs this exactly, not approximately.
USB_CLOCK_HZ = 48_000_000

# When the requested frequency is not reachable, walk down in these steps
# rather than declaring failure: 96 MHz that works beats 100 MHz that does not.
CLOCK_SEARCH_STEP_HZ = 1_000_000
MIN_USABLE_HCLK_HZ = 24_000_000

CONSOLE_BAUD = "115200"


@dataclass(frozen=True)
class Led:
    """An LED, and which way round it is wired.

    `active_low` is not a detail: a blink program written for the wrong
    polarity still blinks, so the mistake survives testing and shows up as an
    inverted status light in the field.
    """

    pin: str
    name: str
    active_low: bool = False


@dataclass(frozen=True)
class Button:
    pin: str
    name: str = "BUTTON"
    active_low: bool = True
    # "none" when the board already has the resistor: enabling the internal
    # one as well is harmless, claiming one exists when it does not is not.
    pull: str = "up"


@dataclass(frozen=True)
class Board:
    """One board that someone can actually put on a desk."""

    name: str
    mcu: str  # STM32F411CEU6, as printed on the package
    part: str  # the key into the device table
    hse_hz: int  # 0 when the board has no external clock at all
    clock_source: str  # hse | hse_bypass | hsi
    usb: bool  # a USB socket wired to the MCU's OTG FS pins
    console: str  # USART1, or "" when nothing is broken out
    console_pins: tuple[tuple[str, str], ...] = ()  # (pin, signal)
    leds: tuple[Led, ...] = ()
    button: Button | None = None
    source: str = ""  # the document these facts came from
    aliases: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


BOARDS: dict[str, Board] = {
    "blackpill-f411": Board(
        name="blackpill-f411",
        mcu="STM32F411CEU6",
        part="stm32f411xe",
        hse_hz=25_000_000,
        clock_source="hse",
        usb=True,
        console="USART1",
        console_pins=(("PA9", "USART1_TX"), ("PA10", "USART1_RX")),
        leds=(Led(pin="PC13", name="LED_BLUE", active_low=True),),
        # KEY sits between PA0 and ground with a pull-up on the board.
        button=Button(pin="PA0", name="KEY", active_low=True, pull="up"),
        source="WeAct Black Pill V3.0 schematic",
        aliases=("blackpill", "weact", "blackpillf411", "stm32f411blackpill"),
        notes=(
            "the LED on PC13 sinks current: the pin has to be driven low to "
            "light it",
            "PA9 also carries USB VBUS sensing on this part; the console and "
            "USB device mode cannot both own that pin",
        ),
    ),
    "nucleo-f411re": Board(
        name="nucleo-f411re",
        mcu="STM32F411RET6",
        part="stm32f411xe",
        # No crystal is fitted. The 8 MHz comes from the ST-LINK's MCO output,
        # so the oscillator has to be started in bypass mode -- HSE_ON here
        # waits for a crystal that is not on the board and times out.
        hse_hz=8_000_000,
        clock_source="hse_bypass",
        usb=False,
        console="USART2",
        console_pins=(("PA2", "USART2_TX"), ("PA3", "USART2_RX")),
        leds=(Led(pin="PA5", name="LED_GREEN"),),
        button=Button(pin="PC13", name="USER_BUTTON", active_low=True, pull="up"),
        source="UM1724 (STM32 Nucleo-64 boards)",
        aliases=("nucleof411re", "nucleo411", "nucleof411"),
        notes=(
            "USART2 is wired to the ST-LINK virtual COM port, so the console "
            "needs no extra adapter",
        ),
    ),
    "stm32f4-discovery": Board(
        name="stm32f4-discovery",
        mcu="STM32F407VGT6",
        part="stm32f407xx",
        hse_hz=8_000_000,
        clock_source="hse",
        usb=False,
        console="USART2",
        console_pins=(("PA2", "USART2_TX"), ("PA3", "USART2_RX")),
        leds=(
            Led(pin="PD12", name="LED_GREEN"),
            Led(pin="PD13", name="LED_ORANGE"),
            Led(pin="PD14", name="LED_RED"),
            Led(pin="PD15", name="LED_BLUE"),
        ),
        # B1 pulls PA0 high when pressed, the opposite of the other two boards.
        button=Button(pin="PA0", name="USER_BUTTON", active_low=False, pull="down"),
        source="UM1472 (STM32F4DISCOVERY)",
        aliases=("discovery", "disco", "stm32f4disco", "f4discovery", "stm32f407disco"),
        notes=(
            "USART2 is not connected to the on-board debugger: the console "
            "needs a USB-serial adapter on PA2/PA3",
        ),
    ),
}


def _key(name: str) -> str:
    return "".join(c for c in str(name or "").lower() if c.isalnum())


def board_for(name: str) -> Board:
    """Resolve whatever someone typed into one profile.

    "Black Pill", "blackpill_f411" and "WeAct" are the same board, and a
    refusal to build because of a hyphen is a refusal nobody learns from.
    """
    wanted = _key(name)
    if not wanted:
        raise CodegenError(f"no board profile named {name!r}; known: {', '.join(BOARDS)}")
    for board in BOARDS.values():
        if wanted == _key(board.name) or wanted in {_key(a) for a in board.aliases}:
            return board
    for board in BOARDS.values():
        if wanted in _key(board.name) or _key(board.name) in wanted:
            return board
    raise CodegenError(f"no board profile named {name!r}; known: {', '.join(BOARDS)}")


@dataclass(frozen=True)
class Pll:
    """One set of PLL dividers, with what it actually produces."""

    m: int
    n: int
    p: int
    q: int
    sysclk_hz: int
    usb_hz: int  # 0 when no legal Q gives exactly 48 MHz

    @property
    def usb_ok(self) -> bool:
        return self.usb_hz == USB_CLOCK_HZ


def _q_for(vco_hz: int) -> tuple[int, int]:
    """(Q, usb_hz). Exactly 48 MHz when the VCO allows it, else the safe Q.

    PLLQ has to be programmed either way, so when USB cannot be exact the
    divider is chosen to stay *under* 48 MHz rather than over it, and usb_hz
    is reported as 0 so nobody downstream believes USB will work.
    """
    if vco_hz % USB_CLOCK_HZ == 0:
        exact = vco_hz // USB_CLOCK_HZ
        if exact in PLL_Q:
            return exact, USB_CLOCK_HZ
    ceil = -(-vco_hz // USB_CLOCK_HZ)
    return min(max(ceil, PLL_Q.start), PLL_Q.stop - 1), 0


def pll_for(source_hz: int, sysclk_hz: int) -> Pll | None:
    """The dividers that turn `source_hz` into exactly `sysclk_hz`, or None.

    Exhaustive rather than clever: the search space is a few hundred
    combinations, and an exhaustive search is one that can be read and
    believed. Ties are broken towards a 1 MHz VCO input, which is what the
    vendor examples for these parts use.
    """
    if source_hz <= 0 or sysclk_hz <= 0:
        return None
    best: Pll | None = None
    best_rank: tuple[int, int, int] | None = None
    for m in PLL_M:
        if source_hz % m:
            continue
        vco_in = source_hz // m
        if not VCO_INPUT_MIN_HZ <= vco_in <= VCO_INPUT_MAX_HZ:
            continue
        for p in PLL_P:
            vco = sysclk_hz * p
            if not VCO_OUTPUT_MIN_HZ <= vco <= VCO_OUTPUT_MAX_HZ:
                continue
            if vco % vco_in:
                continue
            n = vco // vco_in
            if n not in PLL_N:
                continue
            q, usb = _q_for(vco)
            candidate = Pll(m=m, n=n, p=p, q=q, sysclk_hz=sysclk_hz, usb_hz=usb)
            rank = (
                0 if candidate.usb_ok else 1,
                abs(vco_in - PREFERRED_VCO_INPUT_HZ),
                m,
            )
            if best_rank is None or rank < best_rank:
                best, best_rank = candidate, rank
    return best


def _highest_below(source_hz: int, target_hz: int, *, usb: bool) -> Pll | None:
    """Fastest reachable clock at or below the target.

    The walk starts on a whole-megahertz boundary. Stepping down by 1 MHz from
    a target of 100_000_001 Hz would otherwise only ever visit frequencies
    ending in 1 Hz -- none of which the PLL can produce -- and report that a
    perfectly ordinary crystal is unusable.
    """
    hz = (target_hz // CLOCK_SEARCH_STEP_HZ) * CLOCK_SEARCH_STEP_HZ
    while hz >= MIN_USABLE_HCLK_HZ:
        found = pll_for(source_hz, hz)
        if found is not None and (found.usb_ok or not usb):
            return found
        hz -= CLOCK_SEARCH_STEP_HZ
    return None


def _bus_hz(hclk_hz: int, ceiling_hz: int) -> int:
    """The fastest APB frequency the bus is allowed to run at."""
    for divider in sorted(APB_DIVIDERS):
        if hclk_hz % divider:
            continue
        candidate = hclk_hz // divider
        if ceiling_hz <= 0 or candidate <= ceiling_hz:
            return candidate
    return hclk_hz // max(APB_DIVIDERS)


def solve_clock(
    board: Board,
    *,
    target_hclk_hz: int = 0,
    require_usb: bool | None = None,
) -> tuple[ClockPlan, list[str]]:
    """Work out the clock tree for a board, and say what was traded away.

    Defaults to the part's ceiling, and to keeping USB exact on a board that
    has a USB socket -- which on the F411 costs 4 MHz, because 100 MHz and an
    exact 48 MHz cannot come out of the same VCO. That trade is made here,
    once, with a warning, instead of being discovered later as a device that
    enumerates only sometimes.
    """
    device: Device = device_for(board.part)
    ceiling = device.max_hclk_hz
    target = int(target_hclk_hz or ceiling)
    if target > ceiling:
        raise CodegenError(
            f"clock: {target} Hz is above the {ceiling} Hz ceiling for {device.part}"
        )

    uses_hse = board.clock_source.startswith("hse")
    source_hz = board.hse_hz if uses_hse else HSI_HZ
    if source_hz <= 0:
        raise CodegenError(
            f"board {board.name}: clock source is {board.clock_source} but no "
            "frequency is recorded for it"
        )

    want_usb = board.usb if require_usb is None else bool(require_usb)
    warnings: list[str] = []
    chosen = pll_for(source_hz, target)

    if want_usb and (chosen is None or not chosen.usb_ok):
        exact = _highest_below(source_hz, target, usb=True)
        if exact is not None:
            if exact.sysclk_hz != target:
                warnings.append(
                    f"clock: {target} Hz and an exact 48 MHz USB clock cannot come "
                    f"from the same PLL, so {exact.sysclk_hz} Hz was used instead"
                )
            chosen = exact
        else:
            warnings.append(
                "clock: no PLL setting gives exactly 48 MHz from this crystal, so "
                "USB will not work reliably on this board"
            )

    if chosen is None:
        chosen = _highest_below(source_hz, target, usb=False)
        if chosen is None:
            raise CodegenError(
                f"clock: a {source_hz} Hz source cannot drive this part through the "
                f"PLL; check the crystal recorded for {board.name}"
            )
        warnings.append(
            f"clock: {target} Hz is not reachable from a {source_hz} Hz source, so "
            f"{chosen.sysclk_hz} Hz was used instead"
        )

    hclk = chosen.sysclk_hz
    plan = ClockPlan(
        source=board.clock_source,
        hse_hz=board.hse_hz if uses_hse else 0,
        pll_m=chosen.m,
        pll_n=chosen.n,
        pll_p=chosen.p,
        pll_q=chosen.q,
        sysclk_hz=hclk,
        hclk_hz=hclk,
        apb1_hz=_bus_hz(hclk, device.apb1_max_hz),
        apb2_hz=_bus_hz(hclk, device.apb2_max_hz),
        citation=board.source,
    )
    return plan, warnings


def board_pins(board: Board) -> list[PinAssignment]:
    """The pins the board itself dictates.

    Alternate-function numbers are deliberately left empty: they are looked up
    from the part's table by `validate_plan`, and a number typed here would be
    a second, unchecked source of truth for the same fact.
    """
    pins: list[PinAssignment] = []
    for led in board.leds:
        pins.append(
            PinAssignment(
                pin=led.pin,
                signal=led.name,
                mode="output",
                # A pin driving an LED has no reason to slew fast, and a slow
                # pin radiates less.
                speed="low",
                pull="none",
                citation=board.source,
            )
        )
    if board.button is not None:
        pins.append(
            PinAssignment(
                pin=board.button.pin,
                signal=board.button.name,
                mode="input",
                pull=board.button.pull,
                citation=board.source,
            )
        )
    for pin, signal in board.console_pins:
        pins.append(
            PinAssignment(
                pin=pin,
                signal=signal,
                peripheral=board.console,
                mode="alternate",
                citation=board.source,
            )
        )
    return pins


def console_config(board: Board) -> list[PeripheralConfig]:
    if not board.console:
        return []
    return [
        PeripheralConfig(
            peripheral=board.console,
            mode="asynchronous",
            parameters={"BaudRate": CONSOLE_BAUD},
            nvic_priority=5,
            citation=board.source,
        )
    ]


def plan_for(
    board: Board,
    *,
    target_hclk_hz: int = 0,
    require_usb: bool | None = None,
) -> tuple[CubeMXPlan, list[str]]:
    """A minimal but real plan for a board: clock, LEDs, button, console.

    This is the floor the CubeMX agent starts from rather than the ceiling it
    has to reach. Everything in it is a board fact or arithmetic, so a model
    that adds an SPI sensor on top can be wrong about the sensor without also
    being wrong about the crystal.

    `validated` stays False: the plan has not seen the part's pin table yet.
    """
    clock, warnings = solve_clock(
        board, target_hclk_hz=target_hclk_hz, require_usb=require_usb
    )
    plan = CubeMXPlan(
        mcu=board.mcu,
        board=board.name,
        clock=clock,
        pins=board_pins(board),
        peripherals=console_config(board),
        validated=False,
        warnings=list(warnings),
        assumptions=list(board.notes),
        citations=[board.source] if board.source else [],
    )
    return plan, warnings
