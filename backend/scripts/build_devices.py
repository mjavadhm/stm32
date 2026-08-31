"""Convert the vendor pin tables into the form the validator reads.

    make devices

`make sdk-check` proves ST's drivers arrived. This proves the *pin tables*
arrived and mean what we think: for every supported part it imports the
vendor data, spot-checks a handful of pin functions that are true on real
silicon, and writes the compact table the planner will use.

The spot checks are the point. An importer that reads the wrong element, or
vendor data whose layout moved, produces a table that is empty or subtly
shifted -- and a subtly shifted alternate-function number compiles perfectly
and then does nothing on the board. Three known-true rows catch that here,
where the message can name the file, instead of on someone's desk.
"""

import sys

from app.codegen import devicedata, deviceimport, sdk
from app.codegen.devices import DEVICES
from app.codegen.errors import CodegenError

# pin, signal, alternate function -- from the datasheet pin definition tables.
SPOT_CHECKS: dict[str, tuple[tuple[str, str, int], ...]] = {
    "stm32f407xx": (
        ("PA2", "USART2_TX", 7),
        ("PA5", "SPI1_SCK", 5),
        ("PB9", "I2C1_SDA", 4),
    ),
    "stm32f411xe": (
        ("PA9", "USART1_TX", 7),
        ("PA5", "SPI1_SCK", 5),
        ("PB6", "I2C1_SCL", 4),
    ),
}


def show(label: str, value: object) -> None:
    print(f"{label:<11}: {value}")


def diagnose() -> list[str]:
    """With no vendor files, say which of the two causes it actually is.

    Both look identical from here -- an empty directory -- but the fixes are
    different, and guessing costs a rebuild. If ST's drivers are present and
    the pin tables are not, the shared volume was filled by an older image:
    Docker copies an image's contents into a named volume only while that
    volume is still empty.
    """
    xml = deviceimport.xml_root()
    if xml.is_dir() and deviceimport.vendor_files():
        return []
    lines: list[str] = []
    if sdk.sdk_root().is_dir():
        lines.append("ST's drivers are here but the pin tables are not, so this shared")
        lines.append("volume was filled by an older image and never refreshed.")
        lines.append("Docker refuses to delete a volume while any container mounts it,")
        lines.append("and the backend and worker both do, so they have to stop first:")
        lines.append("")
        lines.append("    docker compose rm -sf builder backend worker")
        lines.append("    docker volume rm <project>_cube_sdk")
        lines.append("    docker compose up -d")
        lines.append("")
        lines.append("`make sdk-refresh` now does exactly that, and no longer hides a")
        lines.append("failed deletion.")
    else:
        lines.append(f"nothing is mounted at {sdk.sdk_root().parent}: start the builder so")
        lines.append("Docker fills the shared volume from its image.")
    lines.append("")
    lines.append("If it is still empty after that, the image itself has no pin tables:")
    lines.append("    docker compose run --rm builder ls /opt/stm32cube/modm-devices/stm32")
    return lines


def check(data: devicedata.DeviceData) -> list[str]:
    """Known-true rows, compared against what we just imported."""
    problems: list[str] = []
    for pin, signal, expected in SPOT_CHECKS.get(data.part, ()):
        offered = data.signals(pin)
        if signal not in offered:
            problems.append(f"{pin} should carry {signal}; the table offers {sorted(offered)}")
        elif offered[signal] != expected:
            problems.append(
                f"{pin} {signal} should be AF{expected}, the table says AF{offered[signal]}"
            )
    return problems


def main() -> int:
    files = deviceimport.vendor_files()
    show("vendor", deviceimport.vendor_version() or "no VERSION file")
    show("xml", f"{len(files)} files under {deviceimport.xml_root()}")
    show("tables", devicedata.data_root())

    trouble = diagnose()
    if trouble:
        print()
        for line in trouble:
            print(f"  {line}")

    failed: list[str] = []
    for part in sorted(DEVICES):
        print()
        try:
            data = deviceimport.build(part)
        except CodegenError as error:
            print(f"FAIL  {part}: {error}")
            failed.append(part)
            continue

        problems = check(data)
        signals = sum(len(table) for table in data.pins.values())
        show("device", part)
        show("source", data.source)
        show("pins", f"{len(data.pins)} with {signals} signals")
        show("vectors", f"{len(data.vectors)} from the CMSIS header")
        show("instances", f"{len(data.instances)} peripherals")
        for pin, signal, _ in SPOT_CHECKS.get(part, ())[:1]:
            names = ", ".join(f"{key}" for key in sorted(data.signals(pin)))
            show("sample", f"{pin} -> {names or 'nothing'}")
        for note in data.notes:
            print(f"  note    : {note}")

        if problems:
            for problem in problems:
                print(f"  FAIL    : {problem}")
            failed.append(part)
            continue
        try:
            show("written", devicedata.store(data))
        except OSError as error:
            print(f"FAIL  {part}: cannot write the table: {error}")
            failed.append(part)

    print()
    if failed:
        print(f"FAIL  {len(failed)} part(s) without a usable table: {', '.join(failed)}")
        print(f"      {devicedata.HINT}")
        return 1
    print(f"OK    {len(DEVICES)} part(s) have a pin table the generator can trust")
    return 0


if __name__ == "__main__":
    sys.exit(main())
