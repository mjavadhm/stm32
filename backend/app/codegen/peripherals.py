"""The initialisation code CubeMX would have written, from the plan.

This is the part a language model should not be writing. Not because it is
hard, but because it is *mechanical and unforgiving*: `SPI_BAUDRATEPRESCALER_16`
is a real macro and `SPI_BAUDRATE_PRESCALER_16` is not, an alternate-function
number is a table lookup, and forgetting `__HAL_RCC_SPI1_CLK_ENABLE()` gives
you code that compiles, links, runs, and does nothing at all -- with the
peripheral registers reading back as zero and no error anywhere.

So the plan decides *what* (SPI1 as master at prescaler 16 on PA5/6/7) and
this module decides *how it is spelled*. The model is left with the part it is
good at: the application on top.

Every emitted fragment is plain text so it can be asserted on in a test
without a compiler, and the compiler still has the last word in CI.
"""

import re
from dataclasses import dataclass, field

from app.codegen.devices import Device
from app.codegen.errors import CodegenError
from app.codegen.sdk import family
from app.orchestrator.contracts import ClockPlan, CubeMXPlan, PeripheralConfig, PinAssignment

_PIN_RE = re.compile(r"^P(?P<port>[A-K])(?P<number>\d{1,2})$")
_IDENTIFIER_RE = re.compile(r"[^A-Za-z0-9]+")
_OPTION_RE = re.compile(r"[A-Z0-9_]+")

# The namespace of a HAL option is normally everything before the last word of
# the default (SPI_BAUDRATEPRESCALER + _16). These are the defaults where that
# reading is wrong: UART_MODE_TX_RX is UART_MODE plus TX_RX, so a plan asking
# for "TX" would otherwise get UART_MODE_TX_TX.
ENUM_PREFIXES = {
    "UART_MODE_TX_RX": "UART_MODE",
}

GPIO_MODES = {
    "output": "GPIO_MODE_OUTPUT_PP",
    "output_od": "GPIO_MODE_OUTPUT_OD",
    "input": "GPIO_MODE_INPUT",
    "analog": "GPIO_MODE_ANALOG",
    "alternate": "GPIO_MODE_AF_PP",
    "event": "GPIO_MODE_IT_RISING",
    "interrupt": "GPIO_MODE_IT_RISING",
}
GPIO_PULLS = {"none": "GPIO_NOPULL", "up": "GPIO_PULLUP", "down": "GPIO_PULLDOWN"}
GPIO_SPEEDS = {
    "low": "GPIO_SPEED_FREQ_LOW",
    "medium": "GPIO_SPEED_FREQ_MEDIUM",
    "high": "GPIO_SPEED_FREQ_HIGH",
    "very_high": "GPIO_SPEED_FREQ_VERY_HIGH",
}

# Peripheral families this module can configure. Anything else is reported,
# not guessed at: half-written init code is worse than none, because it
# compiles.
HANDLE_TYPES = {
    "SPI": "SPI_HandleTypeDef",
    "I2C": "I2C_HandleTypeDef",
    "USART": "UART_HandleTypeDef",
    "UART": "UART_HandleTypeDef",
    "TIM": "TIM_HandleTypeDef",
}
INIT_FUNCTIONS = {
    "SPI": "HAL_SPI_Init",
    "I2C": "HAL_I2C_Init",
    "USART": "HAL_UART_Init",
    "UART": "HAL_UART_Init",
    "TIM": "HAL_TIM_Base_Init",
}
MSP_FUNCTIONS = {
    "SPI": ("HAL_SPI_MspInit", "SPI_HandleTypeDef", "spiHandle"),
    "I2C": ("HAL_I2C_MspInit", "I2C_HandleTypeDef", "i2cHandle"),
    "USART": ("HAL_UART_MspInit", "UART_HandleTypeDef", "uartHandle"),
    "UART": ("HAL_UART_MspInit", "UART_HandleTypeDef", "uartHandle"),
    "TIM": ("HAL_TIM_Base_MspInit", "TIM_HandleTypeDef", "timHandle"),
}

_UART_FIELDS = (
    ("BaudRate", "115200"),
    ("WordLength", "UART_WORDLENGTH_8B"),
    ("StopBits", "UART_STOPBITS_1"),
    ("Parity", "UART_PARITY_NONE"),
    ("Mode", "UART_MODE_TX_RX"),
    ("HwFlowCtl", "UART_HWCONTROL_NONE"),
    ("OverSampling", "UART_OVERSAMPLING_16"),
)

# Field order matters only for readability; the defaults are what CubeMX
# offers for the common mode of each peripheral, so a plan that says nothing
# still produces a working, conventional configuration.
FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "SPI": (
        ("Mode", "SPI_MODE_MASTER"),
        ("Direction", "SPI_DIRECTION_2LINES"),
        ("DataSize", "SPI_DATASIZE_8BIT"),
        ("CLKPolarity", "SPI_POLARITY_LOW"),
        ("CLKPhase", "SPI_PHASE_1EDGE"),
        ("NSS", "SPI_NSS_SOFT"),
        ("BaudRatePrescaler", "SPI_BAUDRATEPRESCALER_16"),
        ("FirstBit", "SPI_FIRSTBIT_MSB"),
        ("TIMode", "SPI_TIMODE_DISABLE"),
        ("CRCCalculation", "SPI_CRCCALCULATION_DISABLE"),
        ("CRCPolynomial", "10"),
    ),
    "I2C": (
        ("ClockSpeed", "100000"),
        ("DutyCycle", "I2C_DUTYCYCLE_2"),
        ("OwnAddress1", "0"),
        ("AddressingMode", "I2C_ADDRESSINGMODE_7BIT"),
        ("DualAddressMode", "I2C_DUALADDRESS_DISABLE"),
        ("OwnAddress2", "0"),
        ("GeneralCallMode", "I2C_GENERALCALL_DISABLE"),
        ("NoStretchMode", "I2C_NOSTRETCH_DISABLE"),
    ),
    "USART": _UART_FIELDS,
    "UART": _UART_FIELDS,
    "TIM": (
        ("Prescaler", "0"),
        ("CounterMode", "TIM_COUNTERMODE_UP"),
        ("Period", "65535"),
        ("ClockDivision", "TIM_CLOCKDIVISION_DIV1"),
        ("AutoReloadPreload", "TIM_AUTORELOAD_PRELOAD_DISABLE"),
    ),
}

# Which HAL entry point each interrupt vector belongs to. I2C has two.
IRQ_VECTORS: dict[str, tuple[tuple[str, str], ...]] = {
    "SPI": (("", "HAL_SPI_IRQHandler"),),
    "I2C": (("_EV", "HAL_I2C_EV_IRQHandler"), ("_ER", "HAL_I2C_ER_IRQHandler")),
    "USART": (("", "HAL_UART_IRQHandler"),),
    "UART": (("", "HAL_UART_IRQHandler"),),
    "TIM": (("", "HAL_TIM_IRQHandler"),),
}

# On F4 only these timers own their vector outright. TIM1, TIM6 and TIM8..14
# share one with another timer or with the DAC (TIM1_UP_TIM10_IRQn,
# TIM6_DAC_IRQn ...), so the obvious TIMn_IRQn does not exist and the plan
# has to say which shared vector it wants. Refusing beats emitting a name
# that fails to compile -- or worse, one that compiles for a different timer.
# Peripherals that cannot do anything without pins. A time-base timer has
# none by design, and warning about it teaches people to skim past the
# warnings that do mean something.
NEEDS_PINS = {"SPI", "I2C", "USART", "UART"}

TIM_OWN_VECTOR = {"TIM2", "TIM3", "TIM4", "TIM5", "TIM7"}

FLASH_LATENCY_STEP_HZ = 30_000_000
AHB_DIVIDERS = {1: "RCC_SYSCLK_DIV1", 2: "RCC_SYSCLK_DIV2", 4: "RCC_SYSCLK_DIV4"}
APB_DIVIDERS = {
    1: "RCC_HCLK_DIV1",
    2: "RCC_HCLK_DIV2",
    4: "RCC_HCLK_DIV4",
    8: "RCC_HCLK_DIV8",
    16: "RCC_HCLK_DIV16",
}
HSI_HZ = 16_000_000


@dataclass
class Generated:
    """Text fragments, ready to drop into the templates."""

    handles: str = ""
    dma_handles: str = ""
    prototypes: str = ""
    init_calls: str = ""
    clock_config: str = ""
    gpio_init: str = ""
    init_functions: str = ""
    msp_functions: str = ""
    msp_externs: str = ""
    irq_handlers: str = ""
    irq_prototypes: str = ""
    externs: str = ""
    pin_defines: str = ""
    configured: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _name(peripheral: str) -> str:
    return str(peripheral or "").strip().upper()


def _dma_suffix(dma) -> str:
    return (
        "rx"
        if str(dma.request or "").upper().endswith("_RX")
        or str(dma.direction).lower() == "peripheral_to_memory"
        else "tx"
    )


def _dma_complete(dma) -> bool:
    return bool(dma.request and dma.stream and dma.channel is not None and dma.direction)


def _dma_stream(value: str) -> str:
    match = re.fullmatch(r"DMA([12])_STREAM([0-7])", str(value or "").strip().upper())
    if not match:
        return str(value or "").strip()
    return f"DMA{match.group(1)}_Stream{match.group(2)}"


def parse_pin(pin: str) -> tuple[str, int]:
    """'PA5' -> ('A', 5)."""
    text = str(pin or "").strip().upper()
    match = _PIN_RE.match(text)
    if not match:
        raise CodegenError(f"pin {pin!r} is not a port and number like PA5")
    number = int(match.group("number"))
    if number > 15:
        raise CodegenError(f"pin {pin!r}: a GPIO port has 16 pins")
    return match.group("port"), number


def pin_mask(numbers: list[int]) -> str:
    return "|".join(f"GPIO_PIN_{number}" for number in sorted(set(numbers)))


def enum_value(default: str, given: str) -> str:
    """Accept both `SPI_BAUDRATEPRESCALER_8` and a bare `8` from the plan.

    A model asked for a prescaler answers "8". A model asked for the HAL macro
    answers with one that is plausible and occasionally imaginary. Letting the
    plan carry the short form and completing it against the default here
    removes a whole class of build failure -- and a macro the plan spells out
    in full is passed through untouched, so the compiler still gets the last
    word on it.
    """
    text = str(given or "").strip()
    if not text:
        return default
    # A numeric field (BaudRate, Period, ClockSpeed) takes the value as given.
    if "_" not in default or not default[:1].isalpha():
        return text
    prefix = ENUM_PREFIXES.get(default) or default.rsplit("_", 1)[0]
    if text.startswith(f"{prefix}_"):
        return text
    upper = text.upper()
    if not _OPTION_RE.fullmatch(upper):
        return text
    head = upper.split("_", 1)[0]
    if upper.count("_") >= 2 and any(character.isalpha() for character in head):
        # Already a full macro, from a family we did not expect. Passing it
        # through gives a compile error naming the macro; prefixing it would
        # give one naming a macro nobody wrote.
        return text
    return f"{prefix}_{upper}"


def _fields_for(config: PeripheralConfig, warnings: list[str]) -> list[tuple[str, str]]:
    name = _name(config.peripheral)
    table = FIELDS[family(name)]
    given = {str(key).strip(): str(value) for key, value in (config.parameters or {}).items()}
    lines: list[tuple[str, str]] = []
    for key, default in table:
        lines.append((key, enum_value(default, given.pop(key, ""))))
    for key in sorted(given):
        # Not silently dropped: an ignored parameter is a plan the generated
        # project does not implement, and the report has to say so.
        warnings.append(f"{name}: parameter {key} is not one this generator knows; ignored")
    return lines


def flash_latency(hclk_hz: int) -> str:
    """Wait states at 3.3 V, from the flash-latency table in the reference manual."""
    steps = max(0, (max(int(hclk_hz), 1) - 1) // FLASH_LATENCY_STEP_HZ)
    return f"FLASH_LATENCY_{min(steps, 5)}"


def _divider(name: str, table: dict[int, str], numerator: int, denominator: int,
             warnings: list[str], fallback: str) -> str:
    if numerator <= 0 or denominator <= 0:
        return fallback
    ratio, remainder = divmod(numerator, denominator)
    if remainder or ratio not in table:
        warnings.append(
            f"clock: {name} {numerator} Hz from {denominator} Hz is not a divider "
            f"the hardware has; used {fallback}"
        )
        return fallback
    return table[ratio]


def clock_config(clock: ClockPlan, device: Device, warnings: list[str]) -> str:
    """SystemClock_Config, generated from the numbers in the plan."""
    source = str(clock.source or "hsi").strip().lower()
    use_hse = source.startswith("hse")
    use_pll = clock.pll_n > 0 and clock.pll_m > 0 and clock.pll_p > 0

    sysclk = clock.sysclk_hz or (clock.hse_hz if use_hse else HSI_HZ)
    hclk = clock.hclk_hz or sysclk
    if hclk > device.max_hclk_hz:
        warnings.append(
            f"clock: {hclk} Hz is above the {device.max_hclk_hz} Hz ceiling for "
            f"{device.define}; generated anyway, but the part will not run there"
        )
    if use_hse and clock.hse_hz <= 0:
        raise CodegenError("clock: source is HSE but hse_hz is 0; the PLL cannot be computed")

    oscillator = "RCC_OSCILLATORTYPE_HSE" if use_hse else "RCC_OSCILLATORTYPE_HSI"
    lines = [
        "void SystemClock_Config(void)",
        "{",
        "  RCC_OscInitTypeDef RCC_OscInitStruct = {0};",
        "  RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};",
        "",
        "  /* The voltage regulator has to be told about the target frequency",
        "   * before the frequency changes, not after. */",
        "  __HAL_RCC_PWR_CLK_ENABLE();",
        "  __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE1);",
        "",
        f"  RCC_OscInitStruct.OscillatorType = {oscillator};",
    ]
    if use_hse:
        state = "RCC_HSE_BYPASS" if source == "hse_bypass" else "RCC_HSE_ON"
        lines.append(f"  RCC_OscInitStruct.HSEState = {state};")
    else:
        lines.append("  RCC_OscInitStruct.HSIState = RCC_HSI_ON;")
        lines.append(
            "  RCC_OscInitStruct.HSICalibrationValue = RCC_HSICALIBRATION_DEFAULT;"
        )

    if use_pll:
        pll_source = "RCC_PLLSOURCE_HSE" if use_hse else "RCC_PLLSOURCE_HSI"
        lines += [
            "  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;",
            f"  RCC_OscInitStruct.PLL.PLLSource = {pll_source};",
            f"  RCC_OscInitStruct.PLL.PLLM = {clock.pll_m};",
            f"  RCC_OscInitStruct.PLL.PLLN = {clock.pll_n};",
            f"  RCC_OscInitStruct.PLL.PLLP = RCC_PLLP_DIV{clock.pll_p};",
            f"  RCC_OscInitStruct.PLL.PLLQ = {clock.pll_q or 4};",
        ]
    else:
        lines.append("  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_NONE;")

    sysclk_source = (
        "RCC_SYSCLKSOURCE_PLLCLK"
        if use_pll
        else ("RCC_SYSCLKSOURCE_HSE" if use_hse else "RCC_SYSCLKSOURCE_HSI")
    )
    ahb = _divider("HCLK", AHB_DIVIDERS, sysclk, hclk, warnings, "RCC_SYSCLK_DIV1")
    apb1 = _divider("PCLK1", APB_DIVIDERS, hclk, clock.apb1_hz, warnings, "RCC_HCLK_DIV4")
    apb2 = _divider("PCLK2", APB_DIVIDERS, hclk, clock.apb2_hz, warnings, "RCC_HCLK_DIV2")

    lines += [
        "  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)",
        "  {",
        "    Error_Handler();",
        "  }",
        "",
        "  RCC_ClkInitStruct.ClockType = RCC_CLOCKTYPE_HCLK|RCC_CLOCKTYPE_SYSCLK",
        "                              |RCC_CLOCKTYPE_PCLK1|RCC_CLOCKTYPE_PCLK2;",
        f"  RCC_ClkInitStruct.SYSCLKSource = {sysclk_source};",
        f"  RCC_ClkInitStruct.AHBCLKDivider = {ahb};",
        f"  RCC_ClkInitStruct.APB1CLKDivider = {apb1};",
        f"  RCC_ClkInitStruct.APB2CLKDivider = {apb2};",
        "",
        f"  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, {flash_latency(hclk)}) != HAL_OK)",
        "  {",
        "    Error_Handler();",
        "  }",
        "}",
    ]
    return "\n".join(lines)


def _grouped(pins: list[PinAssignment]) -> dict[tuple[str, str, str, str, str], list[int]]:
    groups: dict[tuple[str, str, str, str, str], list[int]] = {}
    for assignment in pins:
        port, number = parse_pin(assignment.pin)
        mode = str(assignment.mode or "output").strip().lower()
        key = (
            port,
            mode,
            str(assignment.pull or "none").strip().lower(),
            str(assignment.speed or "high").strip().lower(),
            "" if assignment.alternate is None else str(assignment.alternate),
        )
        groups.setdefault(key, []).append(number)
    return groups


def _gpio_mode(mode: str, peripheral: str) -> str:
    if mode not in GPIO_MODES:
        raise CodegenError(f"pin mode {mode!r} is not one of {sorted(GPIO_MODES)}")
    # I2C is open drain, always. Push-pull SDA and SCL lines fight the other
    # devices on the bus, and the symptom is an intermittently dead bus rather
    # than a compile error.
    if mode == "alternate" and family(peripheral) == "I2C":
        return "GPIO_MODE_AF_OD"
    return GPIO_MODES[mode]


def _struct_lines(
    port: str,
    numbers: list[int],
    *,
    mode: str,
    pull: str,
    speed: str,
    alternate: str,
    peripheral: str,
    indent: str,
) -> list[str]:
    hal_mode = _gpio_mode(mode, peripheral)
    if pull not in GPIO_PULLS:
        raise CodegenError(f"pin pull {pull!r} is not one of {sorted(GPIO_PULLS)}")
    if speed not in GPIO_SPEEDS:
        raise CodegenError(f"pin speed {speed!r} is not one of {sorted(GPIO_SPEEDS)}")

    lines = [
        f"{indent}GPIO_InitStruct.Pin = {pin_mask(numbers)};",
        f"{indent}GPIO_InitStruct.Mode = {hal_mode};",
        f"{indent}GPIO_InitStruct.Pull = {GPIO_PULLS[pull]};",
    ]
    if "OUTPUT" in hal_mode or "AF" in hal_mode:
        lines.append(f"{indent}GPIO_InitStruct.Speed = {GPIO_SPEEDS[speed]};")
    if mode == "alternate":
        if not alternate:
            raise CodegenError(
                f"{peripheral or 'pin'} on P{port}{numbers[0]}: no alternate-function "
                "number. The plan has to be validated against the MCU's pin table "
                "before code can be generated from it"
            )
        lines.append(
            f"{indent}GPIO_InitStruct.Alternate = GPIO_AF{alternate}_{_name(peripheral)};"
        )
    lines.append(f"{indent}HAL_GPIO_Init(GPIO{port}, &GPIO_InitStruct);")
    return lines


def gpio_init(plan: CubeMXPlan, warnings: list[str]) -> str:
    """MX_GPIO_Init: port clocks, plus every pin that is not a peripheral's."""
    plain = [
        assignment
        for assignment in plan.pins
        if str(assignment.mode or "output").strip().lower() != "alternate"
    ]
    ports = sorted({parse_pin(assignment.pin)[0] for assignment in plan.pins})

    lines = [
        "/* Port clocks for every pin in the plan, and the pins that are not",
        " * owned by a peripheral. A GPIO write to a port whose clock is off is",
        " * silently discarded, so this runs before anything else. */",
        "static void MX_GPIO_Init(void)",
        "{",
    ]
    if plain:
        lines.append("  GPIO_InitTypeDef GPIO_InitStruct = {0};")
        lines.append("")
    for port in ports:
        lines.append(f"  __HAL_RCC_GPIO{port}_CLK_ENABLE();")

    resets = [
        assignment
        for assignment in plain
        if str(assignment.mode or "").strip().lower().startswith("output")
    ]
    if resets:
        lines.append("")
        for assignment in resets:
            port, number = parse_pin(assignment.pin)
            lines.append(
                f"  HAL_GPIO_WritePin(GPIO{port}, GPIO_PIN_{number}, GPIO_PIN_RESET);"
            )

    for (port, mode, pull, speed, alternate), numbers in _grouped(plain).items():
        lines.append("")
        lines += _struct_lines(
            port,
            numbers,
            mode=mode,
            pull=pull,
            speed=speed,
            alternate=alternate,
            peripheral="",
            indent="  ",
        )

    lines += [
        "",
        "  /* USER CODE BEGIN MX_GPIO_Init */",
        "",
        "  /* USER CODE END MX_GPIO_Init */",
        "}",
    ]
    return "\n".join(lines)


def _init_function(plan: CubeMXPlan, config: PeripheralConfig, warnings: list[str]) -> str:
    name = _name(config.peripheral)
    group = family(name)
    handle = plan.handle(name)
    lines = [f"static void MX_{name}_Init(void)", "{"]
    if group == "TIM":
        lines += [
            "  TIM_ClockConfigTypeDef sClockSourceConfig = {0};",
            "  TIM_MasterConfigTypeDef sMasterConfig = {0};",
            "",
        ]
    lines += [
        f"  /* USER CODE BEGIN {name}_Init 0 */",
        "",
        f"  /* USER CODE END {name}_Init 0 */",
        "",
        f"  {handle}.Instance = {name};",
    ]
    for key, value in _fields_for(config, warnings):
        lines.append(f"  {handle}.Init.{key} = {value};")
    lines += [
        f"  if ({INIT_FUNCTIONS[group]}(&{handle}) != HAL_OK)",
        "  {",
        "    Error_Handler();",
        "  }",
    ]
    if group == "TIM":
        lines += [
            "",
            "  sClockSourceConfig.ClockSource = TIM_CLOCKSOURCE_INTERNAL;",
            f"  if (HAL_TIM_ConfigClockSource(&{handle}, &sClockSourceConfig) != HAL_OK)",
            "  {",
            "    Error_Handler();",
            "  }",
            "  sMasterConfig.MasterOutputTrigger = TIM_TRGO_RESET;",
            "  sMasterConfig.MasterSlaveMode = TIM_MASTERSLAVEMODE_DISABLE;",
            f"  if (HAL_TIMEx_MasterConfigSynchronization(&{handle}, &sMasterConfig) != HAL_OK)",
            "  {",
            "    Error_Handler();",
            "  }",
        ]
    lines += [
        "",
        f"  /* USER CODE BEGIN {name}_Init 1 */",
        "",
        f"  /* USER CODE END {name}_Init 1 */",
        "}",
    ]
    return "\n".join(lines)


def _irq_names(config: PeripheralConfig, warnings: list[str]) -> list[tuple[str, str]]:
    """[(IRQn, HAL handler)] for a peripheral that asked for an interrupt."""
    name = _name(config.peripheral)
    group = family(name)
    if config.nvic_priority is None:
        return []
    if group == "TIM" and name not in TIM_OWN_VECTOR:
        warnings.append(
            f"{name}: its interrupt shares a vector with another timer on this "
            "part, so no handler was generated; add the shared one by hand"
        )
        return []
    return [(f"{name}{suffix}_IRQn", handler) for suffix, handler in IRQ_VECTORS[group]]


def af_pins(plan: CubeMXPlan, peripheral: str) -> list[PinAssignment]:
    """The plan's alternate-function pins for one peripheral."""
    name = _name(peripheral)
    return [
        assignment
        for assignment in plan.pins
        if _name(assignment.peripheral) == name
        and str(assignment.mode or "").strip().lower() == "alternate"
    ]


def _msp_body(plan: CubeMXPlan, config: PeripheralConfig, warnings: list[str]) -> list[str]:
    name = _name(config.peripheral)
    argument = MSP_FUNCTIONS[family(name)][2]
    pins = af_pins(plan, name)
    lines = [f"    __HAL_RCC_{name}_CLK_ENABLE();"]
    dma_controllers = {
        match.group(1)
        for dma in config.dma or []
        if _dma_complete(dma)
        for match in [re.match(r"DMA([12])_Stream\d+", _dma_stream(dma.stream))]
        if match
    }
    for controller in sorted(dma_controllers):
        lines.append(f"    __HAL_RCC_DMA{controller}_CLK_ENABLE();")
    for port in sorted({parse_pin(assignment.pin)[0] for assignment in pins}):
        lines.append(f"    __HAL_RCC_GPIO{port}_CLK_ENABLE();")
    if not pins and family(name) in NEEDS_PINS:
        warnings.append(
            f"{name}: no pins in the plan carry this peripheral, so none were "
            "configured for it"
        )
    for (port, mode, pull, speed, alternate), numbers in _grouped(pins).items():
        lines.append("")
        lines += _struct_lines(
            port,
            numbers,
            mode=mode,
            pull=pull,
            speed=speed,
            alternate=alternate,
            peripheral=name,
            indent="    ",
        )
    irqs = _irq_names(config, warnings)
    if irqs:
        lines.append("")
        priority = max(0, min(int(config.nvic_priority or 0), 15))
        for irqn, _handler in irqs:
            lines.append(f"    HAL_NVIC_SetPriority({irqn}, {priority}, 0);")
            lines.append(f"    HAL_NVIC_EnableIRQ({irqn});")
    for index, dma in enumerate(config.dma or []):
        if not _dma_complete(dma):
            warnings.append(
                f"{name}: DMA route {dma.stream or index + 1} is incomplete (request, "
                "stream, channel, and direction are required); it was not emitted"
            )
            continue
        suffix = _dma_suffix(dma)
        handle_name = f"hdma_{name.lower()}_{suffix}"
        stream = _dma_stream(dma.stream)
        channel = dma.channel if dma.channel is not None else 0
        if not stream:
            warnings.append(f"{name}: DMA route {index + 1} has no stream; it was not emitted")
            continue
        direction = str(dma.direction or "").strip().lower()
        direction_macro = {
            "peripheral_to_memory": "DMA_PERIPH_TO_MEMORY",
            "memory_to_peripheral": "DMA_MEMORY_TO_PERIPH",
        }.get(direction)
        if direction_macro is None:
            warnings.append(
                f"{name}: DMA route {stream} has no supported direction; it was not emitted"
            )
            continue
        lines += [
            "",
            f"    {handle_name}.Instance = {stream};",
            f"    {handle_name}.Init.Channel = DMA_CHANNEL_{channel};",
            f"    {handle_name}.Init.Direction = {direction_macro};",
            f"    {handle_name}.Init.PeriphInc = DMA_PINC_DISABLE;",
            f"    {handle_name}.Init.MemInc = DMA_MINC_ENABLE;",
            f"    {handle_name}.Init.PeriphDataAlignment = DMA_PDATAALIGN_BYTE;",
            f"    {handle_name}.Init.MemDataAlignment = DMA_MDATAALIGN_BYTE;",
            f"    {handle_name}.Init.Mode = DMA_{str(dma.mode or 'normal').strip().upper()};",
            (
                f"    {handle_name}.Init.Priority = "
                f"DMA_PRIORITY_{str(dma.priority or 'low').strip().upper()};"
            ),
            (
                f"    {handle_name}.Init.FIFOMode = "
                f"{'DMA_FIFOMODE_ENABLE' if dma.fifo else 'DMA_FIFOMODE_DISABLE'};"
            ),
            f"    {handle_name}.Init.FIFOThreshold = DMA_FIFO_THRESHOLD_FULL;",
            f"    {handle_name}.Init.MemBurst = DMA_MBURST_SINGLE;",
            f"    {handle_name}.Init.PeriphBurst = DMA_PBURST_SINGLE;",
            f"    if (HAL_DMA_Init(&{handle_name}) != HAL_OK)",
            "    {",
            "      Error_Handler();",
            "    }",
            f"    __HAL_LINKDMA({argument}, hdma{suffix}, {handle_name});",
        ]
        if dma.nvic_priority is not None:
            try:
                dma_priority = max(0, min(int(dma.nvic_priority), 15))
            except (TypeError, ValueError):
                dma_priority = 5
                warnings.append(f"{name}: invalid DMA NVIC priority {dma.nvic_priority!r}; used 5")
            irq = f"{stream}_IRQn"
            lines += [
                f"    HAL_NVIC_SetPriority({irq}, {dma_priority}, 0);",
                f"    HAL_NVIC_EnableIRQ({irq});",
            ]
    return lines


def _pin_defines(plan: CubeMXPlan) -> str:
    """Names for the pins the application will touch, so firmware code can
    say LED_GPIO_Port instead of repeating a port letter it might get wrong."""
    seen: set[str] = set()
    lines: list[str] = []
    for assignment in plan.pins:
        mode = str(assignment.mode or "").strip().lower()
        label = _IDENTIFIER_RE.sub("_", str(assignment.signal or "")).strip("_").upper()
        if mode == "alternate" or not label or label in seen:
            continue
        seen.add(label)
        port, number = parse_pin(assignment.pin)
        lines.append(f"#define {label}_Pin GPIO_PIN_{number}")
        lines.append(f"#define {label}_GPIO_Port GPIO{port}")
    return "\n".join(lines) + ("\n" if lines else "")


def generate(plan: CubeMXPlan, device: Device) -> Generated:
    """Every fragment the templates need, from one plan."""
    result = Generated()
    warnings = result.warnings

    configs: list[PeripheralConfig] = []
    for config in plan.peripherals:
        name = _name(config.peripheral)
        if not name:
            continue
        if family(name) not in FIELDS:
            warnings.append(
                f"{name}: no initialisation template for this peripheral, so it is "
                "left unconfigured; write its init in a USER CODE block"
            )
            continue
        configs.append(config)
    result.configured = [_name(config.peripheral) for config in configs]

    handles = [
        f"{HANDLE_TYPES[family(_name(config.peripheral))]} {plan.handle(config.peripheral)};"
        for config in configs
    ]
    result.handles = "\n".join(handles) + ("\n" if handles else "")
    dma_handles = []
    for config in configs:
        name = _name(config.peripheral)
        for dma in config.dma or []:
            if not _dma_complete(dma):
                continue
            suffix = _dma_suffix(dma)
            dma_handles.append(f"DMA_HandleTypeDef hdma_{name.lower()}_{suffix};")
    result.dma_handles = "\n".join(dict.fromkeys(dma_handles)) + ("\n" if dma_handles else "")
    result.handles += result.dma_handles
    result.msp_externs = "\n".join(
        f"extern {declaration}" for declaration in dict.fromkeys(dma_handles)
    ) + ("\n\n" if dma_handles else "")
    result.prototypes = "".join(
        f"static void MX_{_name(config.peripheral)}_Init(void);\n" for config in configs
    )
    result.init_calls = "".join(
        f"  MX_{_name(config.peripheral)}_Init();\n" for config in configs
    )
    result.clock_config = clock_config(plan.clock, device, warnings)
    result.gpio_init = gpio_init(plan, warnings)
    result.init_functions = "".join(
        f"{_init_function(plan, config, warnings)}\n\n" for config in configs
    )

    # One MspInit per handle type, with a branch per instance: that is the
    # function the HAL calls, and two definitions of it would not link.
    by_group: dict[str, list[PeripheralConfig]] = {}
    for config in configs:
        by_group.setdefault(MSP_FUNCTIONS[family(_name(config.peripheral))][0], []).append(config)

    msp: list[str] = []
    for config_group in by_group.values():
        function, handle_type, argument = MSP_FUNCTIONS[family(_name(config_group[0].peripheral))]
        block = [f"void {function}({handle_type}* {argument})", "{"]
        # Declared only when it is used: -Wextra reports an unused local, and a
        # warning in code we generated is indistinguishable from one in code
        # the model generated until someone reads the file.
        if any(af_pins(plan, config.peripheral) for config in config_group):
            block.append("  GPIO_InitTypeDef GPIO_InitStruct = {0};")
        for config in config_group:
            name = _name(config.peripheral)
            block.append("")
            block.append(f"  if ({argument}->Instance == {name})")
            block.append("  {")
            block += _msp_body(plan, config, warnings)
            block.append("  }")
        block.append("}")
        msp.append("\n".join(block))
    result.msp_functions = "\n\n".join(msp) + ("\n\n" if msp else "")

    handlers: list[str] = []
    prototypes: list[str] = []
    externs: list[str] = []
    for config in configs:
        irqs = _irq_names(config, warnings)
        dma_irqs = [
            dma
            for dma in config.dma or []
            if dma.nvic_priority is not None and _dma_complete(dma)
        ]
        if not irqs and not dma_irqs:
            continue
        handle = plan.handle(config.peripheral)
        handle_type = HANDLE_TYPES[family(_name(config.peripheral))]
        externs.append(f"extern {handle_type} {handle};")
        for irqn, hal_handler in irqs:
            vector = f"{irqn[:-4]}IRQHandler"
            prototypes.append(f"void {vector}(void);")
            handlers.append(
                f"void {vector}(void)\n{{\n  {hal_handler}(&{handle});\n}}"
            )
        for dma in dma_irqs:
            suffix = _dma_suffix(dma)
            dma_handle = f"hdma_{_name(config.peripheral).lower()}_{suffix}"
            externs.append(f"extern DMA_HandleTypeDef {dma_handle};")
            dma_vector = f"{_dma_stream(dma.stream)}_IRQHandler"
            prototypes.append(f"void {dma_vector}(void);")
            handlers.append(
                f"void {dma_vector}(void)\n{{\n  HAL_DMA_IRQHandler(&{dma_handle});\n}}"
            )
    result.externs = "\n".join(externs) + ("\n\n" if externs else "")
    result.irq_prototypes = "".join(f"{line}\n" for line in prototypes)
    result.irq_handlers = "\n\n".join(handlers) + ("\n\n" if handlers else "")
    result.pin_defines = _pin_defines(plan)
    return result
