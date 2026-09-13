"""Tests for the Firmware Agent (M4, Phase P4).

Tests are completely offline with ScriptedLLM -- no real LLM calls and no network.
Verifies:
- Step-by-step file generation along implementation_order
- Context propagation (header signatures visible to subsequent steps)
- USER CODE merging and scaffold preservation in main.c
- Citation validation and demotion to assumptions
- Workspace materialization and dynamic Makefile update for user .c sources
- LangGraph firmware_node integration
"""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from app.agents.firmware import (
    _headers_context,
    _merge_scaffold_file,
    firmware_node,
    generate_firmware,
)
from app.build import workspace
from app.codegen.scaffold import scaffold_project
from app.core.config import settings
from app.orchestrator.contracts import (
    Architecture,
    ClockPlan,
    CubeMXPlan,
    HardwareFinding,
    HardwareFindings,
    ImplementationStep,
    Module,
    PeripheralConfig,
    PinAssignment,
    Requirements,
    SourceFile,
    dump,
)

from tests.test_scaffold import make_sdk


class ScriptedLLM:
    """Returns scripted responses in sequence."""

    def __init__(self, *replies: str | dict):
        self.replies = [
            json.dumps(r) if isinstance(r, dict) else r for r in replies
        ]
        self.calls: list[list[dict]] = []

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        index = min(len(self.calls) - 1, len(self.replies) - 1)
        return self.replies[index]


def _sample_requirements() -> Requirements:
    return Requirements(
        mcu="STM32F407VGTx",
        board="STM32F407G-DISC1",
        summary="Blink LED and read sensor over SPI",
    )


def _sample_hardware() -> HardwareFindings:
    return HardwareFindings(
        family="STM32F4",
        findings=[
            HardwareFinding(
                topic="SPI1",
                question="How to configure SPI1?",
                answer="Use HAL_SPI_TransmitReceive with hspi1.",
                citations=["stm32f4xx_hal_spi.c:100-150"],
                cited=["stm32f4xx_hal_spi.c:100-150"],
                grounded=True,
            )
        ],
    )


def _sample_cubemx_plan() -> CubeMXPlan:
    return CubeMXPlan(
        mcu="STM32F407VGTx",
        board="STM32F407G-DISC1",
        clock=ClockPlan(
            source="hse",
            hse_hz=8000000,
            sysclk_hz=168000000,
            hclk_hz=168000000,
            apb1_hz=42000000,
            apb2_hz=84000000,
        ),
        pins=[
            PinAssignment(
                pin="PA5", signal="SPI1_SCK", peripheral="SPI1", mode="alternate", alternate=5
            ),
            PinAssignment(
                pin="PA7", signal="SPI1_MOSI", peripheral="SPI1", mode="alternate", alternate=5
            ),
            PinAssignment(pin="PD13", signal="GPIO_Output", peripheral="GPIO", mode="output"),
        ],
        peripherals=[
            PeripheralConfig(
                peripheral="SPI1",
                mode="master_full_duplex",
                parameters={"BaudRatePrescaler": "16"},
            )
        ],
        validated=True,
    )


def _sample_architecture() -> Architecture:
    return Architecture(
        overview="Two-layer architecture with LED driver and main application loop.",
        driver_layer="hal",
        modules=[
            Module(
                name="led",
                path="Core/Src/led.c",
                layer="driver",
                responsibility="Controls status LED on PD13",
            )
        ],
        implementation_order=[
            ImplementationStep(
                order=1,
                title="LED Driver",
                detail="Implement LED driver header and source",
                modules=["led"],
                files=["Core/Inc/led.h", "Core/Src/led.c"],
                citations=[],
            ),
            ImplementationStep(
                order=2,
                title="Main Application",
                detail="Integrate LED blink in main loop",
                modules=[],
                files=["Core/Src/main.c"],
                citations=["stm32f4xx_hal_spi.c:100-150"],
            ),
        ],
        file_tree=["Core/Inc/led.h", "Core/Src/led.c", "Core/Src/main.c"],
    )


def test_headers_context_extracts_previous_headers():
    files = [
        SourceFile(
            path="Core/Inc/led.h",
            contents="void LED_Init(void);\nvoid LED_Toggle(void);",
            step_order=1,
        ),
        SourceFile(path="Core/Src/led.c", contents="void LED_Init(void) {}", step_order=1),
    ]
    ctx = _headers_context(files)
    assert "Core/Inc/led.h" in ctx
    assert "void LED_Toggle(void);" in ctx
    assert "Core/Src/led.c" not in ctx


def test_merge_scaffold_file_preserves_system_init():
    scaffold = (
        "/* Header */\n"
        "#include \"main.h\"\n"
        "/* USER CODE BEGIN Includes */\n"
        "/* USER CODE END Includes */\n"
        "int main(void) {\n"
        "  HAL_Init();\n"
        "  SystemClock_Config();\n"
        "  /* USER CODE BEGIN 2 */\n"
        "  /* USER CODE END 2 */\n"
        "  while (1) {\n"
        "    /* USER CODE BEGIN WHILE */\n"
        "    /* USER CODE END WHILE */\n"
        "  }\n"
        "}\n"
    )

    proposed = (
        "/* USER CODE BEGIN Includes */\n"
        "#include \"led.h\"\n"
        "/* USER CODE END Includes */\n"
        "/* USER CODE BEGIN 2 */\n"
        "LED_Init();\n"
        "/* USER CODE END 2 */\n"
        "/* USER CODE BEGIN WHILE */\n"
        "LED_Toggle();\n"
        "HAL_Delay(500);\n"
        "/* USER CODE END WHILE */\n"
    )

    merged = _merge_scaffold_file("Core/Src/main.c", proposed, scaffold)

    # User code is present
    assert "#include \"led.h\"" in merged
    assert "LED_Init();" in merged
    assert "LED_Toggle();" in merged
    # System init is untouched
    assert "HAL_Init();" in merged
    assert "SystemClock_Config();" in merged


def test_step_by_step_generation_and_context_propagation():
    llm = ScriptedLLM(
        # Step 1 - led.h
        {
            "path": "Core/Inc/led.h",
            "purpose": "LED driver header",
            "contents": "#ifndef LED_H\n#define LED_H\nvoid LED_Init(void);\n#endif\n",
            "citations": [],
        },
        # Step 1 - led.c
        {
            "path": "Core/Src/led.c",
            "purpose": "LED driver implementation",
            "contents": (
                '#include "led.h"\n#include "main.h"\n'
                "void LED_Init(void) { HAL_GPIO_WritePin(GPIOD, GPIO_PIN_13, GPIO_PIN_SET); }\n"
            ),
            "citations": [],
        },
        # Step 2 - main.c
        {
            "path": "Core/Src/main.c",
            "purpose": "Main loop calling LED_Init",
            "contents": "/* USER CODE BEGIN 2 */\nLED_Init();\n/* USER CODE END 2 */\n",
            "citations": ["stm32f4xx_hal_spi.c:100-150", "fake_manual.pdf:12"],
        },
    )

    req = _sample_requirements()
    hw = _sample_hardware()
    arch = _sample_architecture()
    plan = _sample_cubemx_plan()

    bundle, warnings = asyncio.run(
        generate_firmware(req, hw, arch, plan, llm=llm)
    )

    # 3 files generated
    assert len(bundle.files) == 3
    paths = [f.path for f in bundle.files]
    assert paths == ["Core/Inc/led.h", "Core/Src/led.c", "Core/Src/main.c"]

    # Step orders are preserved
    assert bundle.files[0].step_order == 1
    assert bundle.files[1].step_order == 1
    assert bundle.files[2].step_order == 2

    # Verify context propagation: call for main.c should see led.h declarations
    main_c_call = llm.calls[2]
    user_prompt_content = main_c_call[1]["content"]
    assert "Core/Inc/led.h" in user_prompt_content
    assert "void LED_Init(void);" in user_prompt_content

    # Citation validation: fake_manual.pdf:12 dropped, stm32f4xx_hal_spi.c kept
    main_file = bundle.file("Core/Src/main.c")
    assert main_file is not None
    assert "stm32f4xx_hal_spi.c:100-150" in main_file.citations
    assert "fake_manual.pdf:12" not in main_file.citations
    assert any("fake_manual.pdf:12" in w for w in warnings)


def test_firmware_materialization_and_makefile_rescan():
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        orig_roots = (settings.workspace_root, settings.cube_sdk_root)
        settings.workspace_root = str(base / "workspaces")
        settings.cube_sdk_root = str(base / "sdk")
        make_sdk(Path(settings.cube_sdk_root))
        project_id = "test_p4_proj"

        try:
            plan = _sample_cubemx_plan()
            req = _sample_requirements()
            arch = _sample_architecture()
            hw = _sample_hardware()

            # First scaffold project as cubemx agent does
            scaffold_project(project_id, plan, clean=True)
            initial_makefile = workspace.read_file(project_id, "Makefile")
            assert "Core/Src/led.c" not in initial_makefile

            llm = ScriptedLLM(
                {
                    "path": "Core/Inc/led.h",
                    "contents": "#ifndef LED_H\n#define LED_H\nvoid LED_Init(void);\n#endif\n",
                },
                {
                    "path": "Core/Src/led.c",
                    "contents": '#include "led.h"\n#include "main.h"\nvoid LED_Init(void) {}\n',
                },
                {
                    "path": "Core/Src/main.c",
                    "contents": "/* USER CODE BEGIN 2 */\nLED_Init();\n/* USER CODE END 2 */\n",
                },
            )

            bundle, warnings = asyncio.run(
                generate_firmware(
                    req,
                    hw,
                    arch,
                    plan,
                    project_id=project_id,
                    project_name="test_p4_proj",
                    llm=llm,
                )
            )

            # Check that files exist on disk
            assert workspace.safe_join(project_id, "Core/Inc/led.h").is_file()
            assert workspace.safe_join(project_id, "Core/Src/led.c").is_file()
            assert workspace.safe_join(project_id, "Core/Src/main.c").is_file()

            # Check that Makefile was refreshed and now contains Core/Src/led.c in C_SOURCES
            updated_makefile = workspace.read_file(project_id, "Makefile")
            assert "Core/Src/led.c" in updated_makefile

            # Check that main.c has the user code and preserved system init
            main_on_disk = workspace.read_file(project_id, "Core/Src/main.c")
            assert "LED_Init();" in main_on_disk
            assert "SystemClock_Config();" in main_on_disk

        finally:
            settings.workspace_root, settings.cube_sdk_root = orig_roots


def test_firmware_node_returns_state_update():
    llm = ScriptedLLM(
        {
            "path": "Core/Src/main.c",
            "contents": "/* USER CODE BEGIN 2 */\n/* setup */\n/* USER CODE END 2 */\n",
        }
    )

    req = _sample_requirements()
    hw = _sample_hardware()
    arch = Architecture(
        overview="Single file project",
        driver_layer="hal",
        implementation_order=[
            ImplementationStep(order=1, title="Main", files=["Core/Src/main.c"])
        ],
    )
    plan = _sample_cubemx_plan()

    state = {
        "project_id": "state_test",
        "requirements": dump(req),
        "hardware": dump(hw),
        "architecture": dump(arch),
        "cubemx": dump(plan),
    }

    with patch("app.agents.firmware.get_agent_llm", return_value=llm):
        update = asyncio.run(firmware_node(state))

    assert "firmware" in update
    assert "firmware_artifacts" in update
    assert update["firmware_artifacts"]["file_count"] == 1
    assert update["firmware_artifacts"]["files"] == ["Core/Src/main.c"]
