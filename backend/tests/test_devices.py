"""The hardware table (M4, P3), tested against a vendor file we wrote by hand.

The real vendor data is downloaded into the build image and is not in git, so
these tests carry a miniature of it: two devices in one file, a signal that
belongs to only one of them, an analog signal with no alternate function, and
a pin that can serve two functions of the same timer. Every one of those is a
shape the real files contain, and every one of them is a way an importer can
be quietly wrong.

What is being pinned down is not "the parser works" but the promise the rest
of the generator leans on: **an alternate-function number is looked up, never
guessed, and when it cannot be looked up the plan is refused with the pins
that would have worked.**
"""

import contextlib
import tempfile
from pathlib import Path

import pytest

from app.codegen import devicedata, deviceimport, sdk
from app.codegen.errors import CodegenError
from app.codegen.validate import validate_plan
from app.core.config import settings
from app.orchestrator.contracts import CubeMXPlan, PeripheralConfig, PinAssignment

PART = "stm32f407xx"
MCU = "STM32F407VGT6"

# Shaped like modm-devices: one file per group of parts, signals filtered down
# to the members that have them, alternate functions as an attribute.
FAKE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<modm>
  <device platform="stm32" family="f4" name="05|07" pin="v|z|i" size="e|g">
    <naming-schema>{platform}{family}{name}{pin}{size}{package}{temperature}</naming-schema>
    <valid-device>stm32f405vgt6</valid-device>
    <valid-device>stm32f407vgt6</valid-device>
    <valid-device>stm32f407zgt6</valid-device>
    <driver name="gpio" type="stm32">
      <gpio port="a" pin="0">
        <signal af="1" driver="tim" instance="2" name="ch1"/>
        <signal af="1" driver="tim" instance="2" name="etr"/>
        <signal driver="adc" instance="1" name="in0"/>
      </gpio>
      <gpio port="a" pin="2">
        <signal af="7" driver="usart" instance="2" name="tx"/>
        <signal af="1" driver="tim" instance="2" name="ch3"/>
        <signal driver="adc" instance="1" name="in2"/>
      </gpio>
      <gpio port="a" pin="3">
        <signal af="7" driver="usart" instance="2" name="rx"/>
      </gpio>
      <gpio port="a" pin="5">
        <signal af="5" driver="spi" instance="1" name="sck"/>
      </gpio>
      <gpio port="b" pin="3">
        <signal af="5" driver="spi" instance="1" name="sck"/>
      </gpio>
      <gpio port="b" pin="8">
        <signal af="4" driver="i2c" instance="1" name="scl"/>
        <signal device-name="05" af="9" driver="can" instance="1" name="rx"/>
      </gpio>
      <gpio port="b" pin="9">
        <signal af="4" driver="i2c" instance="1" name="sda"/>
      </gpio>
      <gpio port="d" pin="12">
        <signal af="2" driver="tim" instance="4" name="ch1"/>
      </gpio>
    </driver>
    <driver name="dma" type="stm32-stream-channel">
      <instance value="1"/>
      <instance value="2"/>
      <streams instance="2">
        <stream position="0">
          <channel position="3"><signal driver="spi" instance="1" name="rx"/></channel>
        </stream>
        <stream position="2">
          <channel position="3"><signal driver="spi" instance="1" name="rx"/></channel>
        </stream>
        <stream position="3">
          <channel position="3"><signal driver="spi" instance="1" name="tx"/></channel>
        </stream>
        <stream position="5">
          <channel position="3"><signal driver="spi" instance="1" name="tx"/></channel>
        </stream>
        <stream position="6">
          <channel device-name="05" position="4">
            <signal driver="tim" instance="1" name="ch1"/>
          </channel>
        </stream>
      </streams>
    </driver>
  </device>
  <device platform="stm32" family="f4" name="11" pin="c" size="e">
    <valid-device>stm32f411ceu6</valid-device>
    <driver name="gpio" type="stm32">
      <gpio port="a" pin="9">
        <signal af="7" driver="usart" instance="1" name="tx"/>
      </gpio>
    </driver>
  </device>
</modm>
"""

# Shaped like ST's CMSIS header: core exceptions first, then the part's own
# vectors, then the peripheral instances as casts onto their base addresses.
FAKE_HEADER = """/* stm32f407xx.h */
typedef enum
{
  NonMaskableInt_IRQn         = -14,
  SysTick_IRQn                = -1,
  WWDG_IRQn                   = 0,
  TIM3_IRQn                   = 29,
  I2C1_EV_IRQn                = 31,
  I2C1_ER_IRQn                = 32,
  SPI1_IRQn                   = 35,
  USART2_IRQn                 = 38,
  TIM8_BRK_TIM12_IRQn         = 43,
} IRQn_Type;

#define TIM3                ((TIM_TypeDef *) TIM3_BASE)
#define TIM8                ((TIM_TypeDef *) TIM8_BASE)
#define TIM12               ((TIM_TypeDef *) TIM12_BASE)
#define SPI1                ((SPI_TypeDef *) SPI1_BASE)
#define I2C1                ((I2C_TypeDef *) I2C1_BASE)
#define USART2              ((USART_TypeDef *) USART2_BASE)
#define GPIOA               ((GPIO_TypeDef *) GPIOA_BASE)
"""


@contextlib.contextmanager
def sandbox():
    """A machine with vendor data on it, and an empty table directory."""
    before = (settings.device_xml_root, settings.device_data_root, settings.cube_sdk_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        vendor = root / "vendor"
        (vendor / "stm32").mkdir(parents=True)
        (vendor / "stm32" / "stm32f4-05_07.xml").write_text(FAKE_XML, encoding="utf-8")
        (vendor / "VERSION").write_text("modm-devices refs/heads/develop\n", encoding="utf-8")
        include = root / "sdk" / sdk.DEVICE_DIR / "Include"
        include.mkdir(parents=True)
        (include / f"{PART}.h").write_text(FAKE_HEADER, encoding="utf-8")
        settings.device_xml_root = str(vendor)
        settings.device_data_root = str(root / "tables")
        settings.cube_sdk_root = str(root / "sdk")
        try:
            yield root
        finally:
            settings.device_xml_root, settings.device_data_root, settings.cube_sdk_root = before


def plan_for(pins, peripherals=()):
    return CubeMXPlan(
        mcu=MCU,
        board="fixture",
        pins=list(pins),
        peripherals=list(peripherals),
    )


def test_the_alternate_function_number_comes_from_the_vendor_table():
    with sandbox():
        data = deviceimport.build(PART)

    assert data.signals("PA2")["USART2_TX"] == 7
    assert data.signals("PB9")["I2C1_SDA"] == 4
    assert data.alternate("PA5", "SPI1_SCK") == 5


def test_a_signal_meant_for_another_device_in_the_file_is_not_offered():
    # PB8 carries CAN1_RX on the F405 only; the file says so and we obey it.
    with sandbox():
        data = deviceimport.build(PART)

    assert "I2C1_SCL" in data.signals("PB8")
    assert "CAN1_RX" not in data.signals("PB8")


def test_a_pin_from_another_part_in_the_same_file_does_not_leak_in():
    with sandbox():
        data = deviceimport.build(PART)

    assert data.signals("PA9") == {}


def test_an_analog_signal_is_stored_without_an_alternate_function():
    # None means "no AF number exists", which is a different answer from the
    # signal being unknown -- and only one of the two is a refusal.
    with sandbox():
        data = deviceimport.build(PART)

    assert "ADC1_IN2" in data.signals("PA2")
    assert data.alternate("PA2", "ADC1_IN2") is None


def test_the_interrupt_vectors_come_from_the_cmsis_header():
    with sandbox():
        data = deviceimport.build(PART)

    assert data.vectors["TIM3"] == 29
    assert data.vectors_for("USART2") == ["USART2"]
    assert data.vectors_for("I2C1") == ["I2C1_ER", "I2C1_EV"]


def test_a_timer_that_shares_its_vector_is_visible_in_the_table():
    # The fact that used to be a hand-written set of timer names.
    with sandbox():
        data = deviceimport.build(PART)

    assert data.vectors_for("TIM12") == ["TIM8_BRK_TIM12"]
    assert data.shares_vector("TIM8_BRK_TIM12", "TIM12") == ["TIM8"]
    assert data.shares_vector("TIM3", "TIM3") == []
    assert data.shares_vector("I2C1_EV", "I2C1") == []


def test_the_peripheral_instances_come_from_the_header():
    with sandbox():
        data = deviceimport.build(PART)

    assert data.has("SPI1")
    assert data.has("USART2")
    assert not data.has("SPI4")


def test_dma_requests_are_part_filtered_and_stably_ordered():
    with sandbox():
        data = deviceimport.build(PART)

    assert [route.stream_name for route in data.routes_for("SPI1_RX")] == [
        "DMA2_Stream0",
        "DMA2_Stream2",
    ]
    assert [route.channel for route in data.routes_for("SPI1_TX")] == [3, 3]
    assert data.routes_for("TIM1_CH1") == []


def test_the_table_says_where_its_numbers_came_from():
    with sandbox():
        data = deviceimport.build(PART)

    assert "modm-devices" in data.source
    assert "stm32f4-05_07.xml" in data.source
    assert f"{PART}.h" in data.source


def test_a_part_with_no_vendor_data_says_how_to_get_it():
    with sandbox():
        settings.device_xml_root = "/nowhere/at/all"
        with pytest.raises(CodegenError) as error:
            deviceimport.build(PART)

    assert "make devices" in str(error.value)


def test_the_table_is_written_once_and_read_back():
    # Parsing vendor XML on every generation would be a tax on every project.
    with sandbox():
        first = devicedata.load(PART)
        assert devicedata.available() == [PART]
        settings.device_xml_root = "/nowhere/at/all"
        second = devicedata.load(PART)

    assert second.pins == first.pins
    assert second.vectors == first.vectors
    assert second.to_dict()["dma_routes"] == first.to_dict()["dma_routes"]


def test_a_table_from_a_future_format_is_reported_not_misread():
    with pytest.raises(CodegenError) as error:
        devicedata.DeviceData.from_dict({"format": 99, "part": PART, "pins": {}})

    assert "make devices" in str(error.value)


def test_a_plan_the_table_agrees_with_is_marked_validated():
    plan = plan_for(
        [
            PinAssignment(pin="PA2", signal="USART2_TX", peripheral="USART2"),
            PinAssignment(pin="PA3", signal="USART2_RX", peripheral="USART2"),
        ]
    )
    with sandbox():
        report = validate_plan(plan)

    assert report.errors == []
    assert report.resolved == 2
    assert plan.validated is True
    assert [pin.alternate for pin in plan.pins] == [7, 7]


def test_a_pin_that_cannot_carry_the_signal_is_refused_with_the_pins_that_can():
    plan = plan_for([PinAssignment(pin="PA4", signal="SPI1_SCK", peripheral="SPI1")])
    with sandbox():
        report = validate_plan(plan)

    assert len(report.errors) == 1
    assert "PA5" in report.errors[0]
    assert "PB3" in report.errors[0]
    assert plan.validated is False


def test_the_signal_is_inferred_when_the_pin_leaves_no_choice():
    plan = plan_for([PinAssignment(pin="PA3", peripheral="USART2")])
    with sandbox():
        report = validate_plan(plan)

    assert report.errors == []
    assert plan.pins[0].signal == "USART2_RX"
    assert plan.pins[0].alternate == 7


def test_a_pin_that_could_serve_two_functions_is_not_guessed():
    # PA0 is TIM2_CH1 and TIM2_ETR. Picking one would be a coin toss that
    # compiles.
    plan = plan_for([PinAssignment(pin="PA0", peripheral="TIM2")])
    with sandbox():
        report = validate_plan(plan)

    assert len(report.errors) == 1
    assert "TIM2_CH1" in report.errors[0]
    assert "TIM2_ETR" in report.errors[0]
    assert plan.validated is False


def test_an_alternate_function_the_plan_got_wrong_is_corrected():
    guessed = PinAssignment(pin="PA2", signal="USART2_TX", peripheral="USART2", alternate=8)
    plan = plan_for([guessed])
    with sandbox():
        report = validate_plan(plan)

    assert report.errors == []
    assert plan.pins[0].alternate == 7
    assert any("AF8" in warning and "AF7" in warning for warning in report.warnings)
    assert report.warnings[0] in plan.warnings


def test_a_signal_with_no_alternate_function_is_not_an_alternate_pin():
    plan = plan_for([PinAssignment(pin="PA2", signal="ADC1_IN2", peripheral="ADC1")])
    with sandbox():
        report = validate_plan(plan)

    assert len(report.errors) == 1
    assert "analog" in report.errors[0]


def test_a_peripheral_this_part_does_not_have_is_refused():
    plan = plan_for([], [PeripheralConfig(peripheral="SPI4", mode="master_full_duplex")])
    with sandbox():
        report = validate_plan(plan)

    assert len(report.errors) == 1
    assert "SPI1" in report.errors[0]


def test_a_shared_interrupt_is_reported_against_the_plan():
    plan = plan_for([], [PeripheralConfig(peripheral="TIM12", mode="time_base", nvic_priority=5)])
    with sandbox():
        report = validate_plan(plan)

    assert report.errors == []
    assert any("TIM8_BRK_TIM12" in warning for warning in report.warnings)


def test_a_plain_output_pin_needs_no_entry_in_the_table():
    plan = plan_for(
        [
            PinAssignment(pin="PD12", signal="LED_GREEN", mode="output"),
            PinAssignment(pin="PD15", signal="LED_BLUE", mode="output"),
        ]
    )
    with sandbox():
        report = validate_plan(plan)

    assert report.errors == []
    assert report.pins == 0
    assert plan.validated is True


def test_a_plan_for_a_part_with_no_table_is_not_quietly_approved():
    plan = plan_for([PinAssignment(pin="PA2", signal="USART2_TX", peripheral="USART2")])
    plan.validated = True
    with sandbox():
        settings.device_xml_root = "/nowhere/at/all"
        report = validate_plan(plan)

    assert report.errors
    assert plan.validated is False
