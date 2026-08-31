"""Board profiles and the clock solver (P3b).

The clock tests are the point of this file. A wrong PLL divider compiles,
links, flashes and runs -- at the wrong speed, which shows up as a UART that
prints garbage and a delay loop that lies. So the arithmetic is pinned against
frequencies taken from the vendor examples for these exact boards.
"""

import pytest

from app.codegen import boards
from app.codegen.devices import DEVICES, device_for
from app.codegen.errors import CodegenError
from app.codegen.peripherals import parse_pin


def test_the_discovery_clock_matches_the_hand_written_plan():
    """8 MHz -> 168 MHz is the one configuration everyone publishes.

    `scripts/build_scaffold.py` carries these four numbers written by hand from
    the reference manual, and that project is known to run. The solver has to
    arrive at the same answer on its own.
    """
    plan, warnings = boards.plan_for(boards.board_for("stm32f4-discovery"))
    clock = plan.clock
    assert (clock.pll_m, clock.pll_n, clock.pll_p, clock.pll_q) == (8, 336, 2, 7)
    assert clock.sysclk_hz == 168_000_000
    assert clock.apb1_hz == 42_000_000
    assert clock.apb2_hz == 84_000_000
    assert warnings == []


def test_usb_is_kept_exact_even_when_it_costs_speed():
    """On the F411 a 100 MHz core and a 48 MHz USB clock are exclusive."""
    board = boards.board_for("blackpill-f411")
    plan, warnings = boards.plan_for(board)
    assert board.usb is True
    assert plan.clock.sysclk_hz == 96_000_000
    vco = 25_000_000 // plan.clock.pll_m * plan.clock.pll_n
    assert vco // plan.clock.pll_q == boards.USB_CLOCK_HZ
    assert any("48 MHz" in warning for warning in warnings)


def test_the_usb_trade_can_be_declined():
    """Someone who is not using USB should get the full 100 MHz."""
    plan, warnings = boards.plan_for(
        boards.board_for("blackpill-f411"), require_usb=False
    )
    assert plan.clock.sysclk_hz == 100_000_000
    assert warnings == []


def test_a_board_without_usb_is_not_slowed_down():
    plan, _ = boards.plan_for(boards.board_for("nucleo-f411re"))
    assert plan.clock.sysclk_hz == 100_000_000


def test_the_apb_ceilings_are_respected_on_every_board():
    for name in boards.BOARDS:
        board = boards.board_for(name)
        plan, _ = boards.plan_for(board)
        device = device_for(board.part)
        assert plan.clock.apb1_hz <= device.apb1_max_hz
        assert plan.clock.apb2_hz <= device.apb2_max_hz
        assert plan.clock.hclk_hz % plan.clock.apb1_hz == 0
        assert plan.clock.hclk_hz % plan.clock.apb2_hz == 0


def test_the_vco_input_stays_in_range_on_every_board():
    """Outside 1..2 MHz the PLL is out of spec, whatever it produces."""
    for name in boards.BOARDS:
        board = boards.board_for(name)
        plan, _ = boards.plan_for(board)
        source = board.hse_hz if board.clock_source.startswith("hse") else 16_000_000
        vco_in = source / plan.clock.pll_m
        assert boards.VCO_INPUT_MIN_HZ <= vco_in <= boards.VCO_INPUT_MAX_HZ
        vco = vco_in * plan.clock.pll_n
        assert boards.VCO_OUTPUT_MIN_HZ <= vco <= boards.VCO_OUTPUT_MAX_HZ


def test_pll_p_is_one_the_hardware_has():
    for name in boards.BOARDS:
        plan, _ = boards.plan_for(boards.board_for(name))
        assert plan.clock.pll_p in boards.PLL_P
        assert plan.clock.pll_q in boards.PLL_Q
        assert plan.clock.pll_m in boards.PLL_M
        assert plan.clock.pll_n in boards.PLL_N


def test_asking_for_more_than_the_part_allows_is_refused():
    with pytest.raises(CodegenError) as error:
        boards.solve_clock(boards.board_for("blackpill-f411"), target_hclk_hz=168_000_000)
    assert "stm32f411xe" in str(error.value)


def test_an_unreachable_target_is_lowered_and_reported():
    """Better a clock that exists than a refusal, as long as it is said.

    An odd frequency like this is what arrives when a target is computed from
    a baud rate or copied out of a forum post. The PLL cannot produce it from
    an 8 MHz crystal, and the nearest whole megahertz below it can.
    """
    plan, warnings = boards.plan_for(
        boards.board_for("stm32f4-discovery"), target_hclk_hz=100_000_001
    )
    assert plan.clock.sysclk_hz == 100_000_000
    assert any("not reachable" in warning for warning in warnings)


def test_a_bypassed_oscillator_is_recorded_as_bypass():
    """The Nucleo has no crystal: HSE_ON would wait for one and time out."""
    board = boards.board_for("nucleo-f411re")
    assert board.clock_source == "hse_bypass"
    plan, _ = boards.plan_for(board)
    assert plan.clock.source == "hse_bypass"
    assert plan.clock.hse_hz == 8_000_000


def test_the_black_pill_led_is_recorded_as_active_low():
    board = boards.board_for("blackpill-f411")
    led = board.leds[0]
    assert (led.pin, led.active_low) is not None
    assert led.pin == "PC13"
    assert led.active_low is True
    assert any("low" in note for note in board.notes)


def test_the_discovery_button_is_the_other_way_round():
    """PA0 on the Discovery goes high when pressed; the others go low."""
    disco = boards.board_for("stm32f4-discovery").button
    pill = boards.board_for("blackpill-f411").button
    assert disco is not None and pill is not None
    assert (disco.pin, disco.active_low, disco.pull) == ("PA0", False, "down")
    assert (pill.pin, pill.active_low, pill.pull) == ("PA0", True, "up")


def test_a_board_is_found_by_the_names_people_type():
    wanted = boards.BOARDS["blackpill-f411"]
    for name in ("blackpill-f411", "BlackPill", "black pill", "weact", "blackpill_f411"):
        assert boards.board_for(name) is wanted


def test_an_unknown_board_is_refused_with_the_known_ones():
    with pytest.raises(CodegenError) as error:
        boards.board_for("arduino uno")
    message = str(error.value)
    for name in boards.BOARDS:
        assert name in message


def test_alternate_functions_are_left_for_the_pin_table():
    """A number typed here would be a second source of truth for one fact."""
    plan, _ = boards.plan_for(boards.board_for("blackpill-f411"))
    assert plan.pins
    assert all(pin.alternate is None for pin in plan.pins)
    assert plan.validated is False


def test_the_console_pins_name_their_peripheral():
    board = boards.board_for("nucleo-f411re")
    plan, _ = boards.plan_for(board)
    console = [pin for pin in plan.pins if pin.mode == "alternate"]
    assert {pin.pin for pin in console} == {"PA2", "PA3"}
    assert {pin.peripheral for pin in console} == {"USART2"}
    assert {pin.signal for pin in console} == {"USART2_TX", "USART2_RX"}
    assert [config.peripheral for config in plan.peripherals] == ["USART2"]


def test_every_board_pin_is_a_real_gpio_and_used_once():
    for name in boards.BOARDS:
        plan, _ = boards.plan_for(boards.board_for(name))
        seen = [pin.pin for pin in plan.pins]
        assert len(seen) == len(set(seen)), f"{name} assigns a pin twice"
        for pin in seen:
            parse_pin(pin)


def test_every_board_names_a_part_the_generator_supports():
    for board in boards.BOARDS.values():
        assert board.part in DEVICES
        assert device_for(board.mcu).part == board.part


def test_every_board_says_where_its_facts_came_from():
    """An unsourced board fact is indistinguishable from a guess."""
    for board in boards.BOARDS.values():
        assert board.source
        plan, _ = boards.plan_for(board)
        assert plan.citations == [board.source]
        assert plan.clock.citation == board.source
        assert all(pin.citation == board.source for pin in plan.pins)


def test_the_solver_reports_no_answer_rather_than_a_wrong_one():
    """Three ways to have no answer, none of which may return dividers.

    The first is a real mistake people make: the 32.768 kHz crystal next to
    the 25 MHz one is for the RTC, and it cannot drive the PLL -- PLLM only
    divides, so nothing brings that up into the 1..2 MHz VCO input window.
    """
    assert boards.pll_for(32_768, 168_000_000) is None
    assert boards.pll_for(8_000_000, 500_000_000) is None  # past the VCO ceiling
    assert boards.pll_for(0, 168_000_000) is None
