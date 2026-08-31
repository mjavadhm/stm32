"""Turning vendor files into the table this generator trusts.

Two sources, both already on the machine and neither in this repository:

  * **modm-devices XML** -- which pin can carry which signal, and at which
    alternate-function number. This is the one fact in the whole system that
    exists nowhere in ST's own source drop: the HAL knows what `GPIO_AF7_USART2`
    means, but not that PA2 is allowed to use it. modm-devices publishes it
    machine-readably, extracted from the same vendor database STM32CubeMX
    unpacks to disk.
  * **the CMSIS device header** (`stm32f407xx.h`), already in the SDK volume --
    `enum IRQn_Type` is the exact interrupt vector list for this part, and the
    `((TIM_TypeDef *) TIM3_BASE)` defines are the exact peripheral instances.
    Both are per-part facts we would otherwise be typing out per family.

The import is deliberately one-directional and offline: the build image
downloads the XML once (`deploy/builder/fetch-devices.sh`), and this converts
it into `devicedata`'s compact JSON. Nothing here reaches the network.

The known limitation, stated rather than hidden: the vendor data is grouped
by part family, so pin *availability* is per family, not per package. A pin
missing from your 64-pin package will still be listed. The AF numbers, which
are what the generator cannot guess, are exact.
"""

import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from app.codegen import sdk
from app.codegen.devicedata import HINT, DeviceData, DmaRoute
from app.codegen.errors import CodegenError
from app.core.config import settings

_PART_RE = re.compile(r"^stm32(?P<family>[a-z]\d)(?P<name>\d{2})")
_IRQ_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*)_IRQn\s*=\s*(-?\d+)")
_INSTANCE_RE = re.compile(
    r"^#define\s+([A-Z][A-Z0-9]*)\s+\(\(\s*\w+_TypeDef\s*\*\s*\)\s*\w+\s*\)",
    re.M,
)
# modm narrows a signal to some of the devices described by one file.
_PACKAGE_FILTER_RE = re.compile(r"device-(pin|package|size|variant|core|temperature)=")

PACKAGE_NOTE = (
    "some signals in the vendor file are restricted to certain packages; they "
    "were kept, so check the pinout for your package before soldering"
)
FAMILY_NOTE = (
    "alternate-function numbers are exact; pin availability is per part "
    "family, not per package"
)


def xml_root() -> Path:
    """Read the setting on every call so tests can point it at a temp dir."""
    return Path(settings.device_xml_root)


def vendor_files() -> list[Path]:
    """The XML files the build image downloaded, flat layout or one deep."""
    root = xml_root()
    if not root.is_dir():
        return []
    return sorted(set(root.glob("*.xml")) | set(root.glob("*/*.xml")))


def vendor_version() -> str:
    """The ref fetch-devices.sh actually downloaded."""
    try:
        return " | ".join((xml_root() / "VERSION").read_text("utf-8").split())
    except OSError:
        return ""


def family_and_name(part: str) -> tuple[str, str]:
    """'stm32f407xx' -> ('f4', '07'), the two fields modm filters on."""
    match = _PART_RE.match(str(part).strip().lower())
    if not match:
        raise CodegenError(f"{part!r} is not a part number this importer understands")
    return match.group("family"), match.group("name")


def signal_name(element: ElementTree.Element) -> str:
    """<signal driver="usart" instance="2" name="tx"/> -> 'USART2_TX'."""
    driver = (element.get("driver") or "").strip().upper()
    instance = (element.get("instance") or "").strip()
    name = (element.get("name") or "").strip().upper()
    if not driver or not name:
        return ""
    return f"{driver}{instance}_{name}"


def _wanted(element: ElementTree.Element, name: str) -> bool:
    """Is this signal offered on *our* device, or only on its file-mates?

    One modm file describes a group of parts (`stm32f4-05_07_15_17.xml`), and
    a signal that only the F405 has carries `device-name="05"`. Keeping it
    would hand the planner a pin function this chip does not have.
    """
    allowed = element.get("device-name")
    if not allowed:
        return True
    return name in [value.strip() for value in allowed.split("|")]


def _matching_devices(root: ElementTree.Element, part: str):
    """Yield device elements whose expansion includes this exact part."""
    family, name = family_and_name(part)
    prefix = f"stm32{family}{name}"
    for device in root.iter("device"):
        valid = [(element.text or "").strip().lower() for element in device.iter("valid-device")]
        if valid:
            if any(entry.startswith(prefix) for entry in valid):
                yield device
            continue
        names = [value.strip() for value in (device.get("name") or "").split("|")]
        if device.get("family") == family and name in names:
            yield device


def parse_pins(text: str, part: str) -> dict[str, dict[str, int | None]]:
    """Read one vendor file, keeping only what applies to this part."""
    _family, name = family_and_name(part)
    root = ElementTree.fromstring(text)
    pins: dict[str, dict[str, int | None]] = {}

    for device in _matching_devices(root, part):
        for gpio in device.iter("gpio"):
            port = (gpio.get("port") or "").strip().upper()
            number = (gpio.get("pin") or "").strip()
            if not port or not number.isdigit():
                continue
            pin = f"P{port}{int(number)}"
            for signal in gpio.iter("signal"):
                if not _wanted(signal, name):
                    continue
                label = signal_name(signal)
                if not label:
                    continue
                raw = (signal.get("af") or "").strip()
                value = int(raw) if raw.isdigit() else None
                table = pins.setdefault(pin, {})
                # The same signal can be listed twice, once without an AF
                # number. Never let the empty one win.
                if label not in table or table[label] is None:
                    table[label] = value
    return pins


def parse_dma(text: str, part: str) -> dict[str, list[DmaRoute]]:
    """Read request -> controller/stream/channel routes for one exact part."""
    _family, name = family_and_name(part)
    root = ElementTree.fromstring(text)
    found: dict[str, set[tuple[int, int, int]]] = {}

    for device in _matching_devices(root, part):
        for driver in device.iter("driver"):
            if (driver.get("name") or "").strip().lower() != "dma":
                continue
            if (driver.get("type") or "").strip().lower() != "stm32-stream-channel":
                continue
            for streams in driver.findall("streams"):
                raw_controller = (streams.get("instance") or "").strip()
                if not raw_controller.isdigit() or not _wanted(streams, name):
                    continue
                controller = int(raw_controller)
                for stream in streams.findall("stream"):
                    raw_stream = (stream.get("position") or "").strip()
                    if not raw_stream.isdigit() or not _wanted(stream, name):
                        continue
                    stream_number = int(raw_stream)
                    for channel in stream.findall("channel"):
                        raw_channel = (channel.get("position") or "").strip()
                        if not raw_channel.isdigit() or not _wanted(channel, name):
                            continue
                        channel_number = int(raw_channel)
                        for signal in channel.findall("signal"):
                            if not _wanted(signal, name):
                                continue
                            request = signal_name(signal)
                            if not request:
                                continue
                            found.setdefault(request, set()).add(
                                (controller, stream_number, channel_number)
                            )

    return {
        request: [DmaRoute(*route) for route in sorted(routes)]
        for request, routes in sorted(found.items())
    }


def parse_vectors(text: str) -> dict[str, int]:
    """`enum IRQn_Type` from the CMSIS header: the part's real vector list."""
    end = text.find("IRQn_Type;")
    if end < 0:
        raise CodegenError("no IRQn_Type enum in the CMSIS header; is this the right file?")
    return {match.group(1): int(match.group(2)) for match in _IRQ_RE.finditer(text[:end])}


def parse_instances(text: str) -> list[str]:
    """`#define SPI1 ((SPI_TypeDef *) SPI1_BASE)` -> the instances that exist."""
    return sorted({match.group(1) for match in _INSTANCE_RE.finditer(text)})


def header_for(part: str) -> Path:
    return sdk.sdk_root() / sdk.DEVICE_DIR / "Include" / f"{str(part).strip().lower()}.h"


def build(part: str) -> DeviceData:
    """Import one part. Raises rather than returning a half-filled table."""
    name = str(part).strip().lower()
    family, device_name = family_and_name(name)
    prefix = f"stm32{family}{device_name}"

    header = header_for(name)
    try:
        header_text = header.read_text("utf-8", errors="ignore")
    except OSError as error:
        raise CodegenError(f"no CMSIS header at {header} -- {sdk.HINT}") from error
    vectors = parse_vectors(header_text)
    instances = parse_instances(header_text)

    pins: dict[str, dict[str, int | None]] = {}
    dma_routes: dict[str, list[DmaRoute]] = {}
    used: list[str] = []
    packaged = False
    for path in vendor_files():
        try:
            text = path.read_text("utf-8", errors="ignore")
        except OSError:
            continue
        # Cheap pre-filter: 350 F4 files, and only a couple mention this part.
        if prefix not in text.lower():
            continue
        try:
            found = parse_pins(text, name)
            found_dma = parse_dma(text, name)
        except ElementTree.ParseError as error:
            raise CodegenError(f"{path.name} is not readable as XML: {error}") from error
        if not found:
            continue
        used.append(path.name)
        packaged = packaged or bool(_PACKAGE_FILTER_RE.search(text))
        for pin, table in found.items():
            pins.setdefault(pin, {}).update(table)
        for request, routes in found_dma.items():
            existing = {
                (route.controller, route.stream, route.channel)
                for route in dma_routes.setdefault(request, [])
            }
            dma_routes[request].extend(
                route
                for route in routes
                if (route.controller, route.stream, route.channel) not in existing
            )
            dma_routes[request].sort(
                key=lambda route: (route.controller, route.stream, route.channel)
            )

    if not pins:
        raise CodegenError(
            f"no pin table for {name} under {xml_root()} "
            f"({len(vendor_files())} vendor files searched) -- {HINT}"
        )

    source = " | ".join(filter(None, [vendor_version(), ", ".join(used), header.name]))
    notes = [FAMILY_NOTE]
    if packaged:
        notes.append(PACKAGE_NOTE)
    return DeviceData(
        part=name,
        source=source,
        pins=pins,
        vectors=vectors,
        instances=instances,
        dma_routes=dma_routes,
        notes=notes,
    )
