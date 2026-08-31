"""Deterministic STM32CubeMX `.ioc` rendering.

CubeMX files are key/value text, not a stable public schema. The renderer keeps
the small subset needed by P3 explicit and ordered, which makes artifacts
reviewable and snapshot-testable while remaining openable by CubeMX.
"""

from app.build import workspace
from app.codegen.devicedata import DeviceData
from app.orchestrator.contracts import CubeMXPlan

MCU_METADATA = {
    "stm32f407xx": ("STM32F407VGTx", "STM32F407VGT6", "LQFP100"),
    "stm32f411xe": {
        "ce": ("STM32F411CEUx", "STM32F411CEU6", "UFQFPN48"),
        "re": ("STM32F411RETx", "STM32F411RET6", "LQFP64"),
    },
}


def mcu_metadata(mcu: str) -> tuple[str, str, str] | None:
    text = str(mcu or "").strip().lower().replace("-", "")
    if "stm32f407" in text:
        # VG is the only supported F407 P3 package. A bare family is not
        # sufficiently specific to claim a complete IOC.
        return MCU_METADATA["stm32f407xx"] if "vg" in text else None
    if "stm32f411" in text:
        if "ce" in text:
            return MCU_METADATA["stm32f411xe"]["ce"]
        if "re" in text:
            return MCU_METADATA["stm32f411xe"]["re"]
    return None


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _pin_signal_lines(plan: CubeMXPlan) -> list[str]:
    lines: list[str] = []
    assignments = sorted(
        plan.pins,
        key=lambda item: (str(item.pin).upper(), str(item.signal).upper()),
    )
    for index, assignment in enumerate(assignments):
        pin = str(assignment.pin or "").strip().upper()
        if not pin:
            continue
        lines.append(f"Mcu.Pin{index}={pin}")
        mode = str(assignment.mode or "").strip().lower()
        signal = str(assignment.signal or "").strip().upper()
        if mode == "alternate" and signal:
            lines.append(f"{pin}.Signal={signal}")
        elif mode == "input":
            lines.append(f"{pin}.Signal=GPIO_Input")
        elif mode.startswith("output"):
            lines.append(f"{pin}.Signal=GPIO_Output")
        elif mode == "analog":
            lines.append(f"{pin}.Signal=ADCx_INx")
        if mode != "alternate" and signal:
            lines.append(f"{pin}.GPIOParameters=GPIO_Label")
            lines.append(f"{pin}.GPIO_Label={signal}")
        if assignment.pull and assignment.pull.lower() != "none":
            lines.append(f"{pin}.GPIO_Pull={assignment.pull.upper()}")
        if assignment.speed:
            lines.append(f"{pin}.GPIO_Speed={assignment.speed.upper()}")
        if assignment.alternate is not None:
            lines.append(f"{pin}.GPIO_AF={assignment.alternate}")
    lines.append(f"Mcu.PinsNb={len(assignments)}")
    return lines


def render_ioc(plan: CubeMXPlan, *, data: DeviceData | None = None) -> str:
    metadata = mcu_metadata(plan.mcu)
    ips = ["NVIC", "RCC", "SYS", *sorted(
        {
            str(config.peripheral or "").strip().upper()
            for config in plan.peripherals
            if str(config.peripheral or "").strip()
        }
    )]
    lines = [
        "#MicroXplorer Configuration settings - do not modify",
        "File.Version=6",
        "KeepUserPlacement=false",
        "Mcu.Family=STM32F4",
    ]
    if metadata is None:
        lines += [
            "Mcu.CPN=",
            "Mcu.Name=",
            "Mcu.Package=",
            "Mcu.UserName=",
            "P3.Warning=MCU is unsupported or insufficiently specific",
        ]
    else:
        mcu_name, cpn, package = metadata
        lines += [
            f"Mcu.CPN={cpn}",
            f"Mcu.Name={mcu_name}",
            f"Mcu.Package={package}",
            f"Mcu.UserName={mcu_name}",
        ]

    for index, ip in enumerate(ips):
        lines.append(f"Mcu.IP{index}={ip}")
    lines += [f"Mcu.IPNb={len(ips)}", "Mcu.ThirdPartyNb=0", "Mcu.UserConstants="]

    lines += [
        (
            "ProjectManager.ProjectName="
            f"{str(plan.board or 'stm32-project').strip() or 'stm32-project'}"
        ),
        "ProjectManager.TargetToolchain=Makefile",
        "ProjectManager.MainLocation=Core/Src",
        "ProjectManager.CoupleFile=false",
        "ProjectManager.NoMain=false",
        "ProjectManager.DeviceId=STM32F4",
        "ProjectManager.ToolChainLocation=",
        "PinoutPanel.RotationAngle=0",
        "MxCube.Version=6.12.0",
        f"RCC.HSE_VALUE={plan.clock.hse_hz or 0}",
        f"RCC.SYSCLKFreq_VALUE={plan.clock.sysclk_hz or 0}",
        f"RCC.HCLKFreq_Value={plan.clock.hclk_hz or 0}",
        f"RCC.APB1CLKFreq_Value={plan.clock.apb1_hz or 0}",
        f"RCC.APB2CLKFreq_Value={plan.clock.apb2_hz or 0}",
        f"RCC.ClockSource={str(plan.clock.source or 'hsi').upper()}",
        f"RCC.PLLM={plan.clock.pll_m or 0}",
        f"RCC.PLLN={plan.clock.pll_n or 0}",
        f"RCC.PLLP={plan.clock.pll_p or 0}",
        f"RCC.PLLQ={plan.clock.pll_q or 0}",
        "SYS.Debug=Serial_Wire",
        "SYS.IPParameters=Debug",
    ]

    lines.extend(_pin_signal_lines(plan))
    for config in sorted(plan.peripherals, key=lambda item: str(item.peripheral).upper()):
        name = str(config.peripheral or "").strip().upper()
        if not name:
            continue
        parameters = dict(config.parameters)
        if name.startswith("SPI"):
            parameters.setdefault("Mode", "SPI_MODE_MASTER")
            parameters.setdefault("Direction", "SPI_DIRECTION_2LINES")
            parameters.setdefault("VirtualType", "VM_MASTER")
        elif name.startswith(("USART", "UART")):
            parameters.setdefault("VirtualMode", "VM_ASYNC")
        elif name.startswith("TIM"):
            parameters.setdefault("InternalClock", "TIM_CLOCKSOURCE_INTERNAL")
        keys = sorted(parameters)
        if keys:
            lines.append(f"{name}.IPParameters={','.join(keys)}")
            for key in keys:
                lines.append(f"{name}.{key}={parameters[key]}")
        for index, dma in enumerate(config.dma or []):
            request = str(dma.request or "").strip().upper()
            lines.append(f"{name}.DMAReq.{index}={request}")
            lines.append(f"{name}.DMAReq.{index}.Instance={str(dma.stream or '').strip()}")
            channel = dma.channel if dma.channel is not None else ""
            lines.append(f"{name}.DMAReq.{index}.Channel={channel}")
            lines.append(f"{name}.DMAReq.{index}.Direction={str(dma.direction or '').upper()}")
            if dma.nvic_priority is not None:
                lines.append(
                    f"NVIC.{str(dma.stream).strip()}_IRQn=true:{dma.nvic_priority}:0:false"
                )
        if config.nvic_priority is not None:
            lines.append(f"NVIC.{name}_IRQn=true:{config.nvic_priority}:0:false")

    lines += [
        "NVIC.PriorityGroup=NVIC_PRIORITYGROUP_4",
        "ProjectManager.GenerateUnderRoot=true",
        f"P3.Validated={_bool(plan.validated)}",
    ]
    return "\n".join(lines) + "\n"


def write_ioc(
    project_id: str,
    plan: CubeMXPlan,
    *,
    name: str = "project.ioc",
    data: DeviceData | None = None,
) -> str:
    if not str(name).endswith(".ioc"):
        name = f"{name}.ioc"
    workspace.write_file(project_id, name, render_ioc(plan, data=data))
    return name
