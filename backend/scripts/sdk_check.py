"""Can the backend see the ST drivers, and do they copy into a project?

`make golden` proves the toolchain compiles. This proves the *drivers*
arrived: that the build image downloaded them and that the shared volume
really reaches this container. Every generated project depends on both, and
both fail in ways that are unreadable from inside a compile log.

It does a real copy into a throwaway workspace rather than stat-ing a few
paths, because that is the code path generation will use.

Run with: make sdk-check
"""

import sys

from app.build import workspace
from app.codegen import sdk

PROJECT_ID = "sdkcheck"
PERIPHERALS = ("SPI1", "USART2", "I2C1", "TIM3")


def show(label: str, value: object) -> None:
    print(f"{label:<12}: {value}")


def main() -> int:
    show("sdk root", sdk.sdk_root())
    show("sdk", sdk.sdk_version() or "no VERSION file")

    try:
        sdk.require_sdk()
        copied = sdk.copy_into(PROJECT_ID, peripherals=PERIPHERALS)
    except (sdk.SdkError, workspace.WorkspaceError) as error:
        print(f"FAIL  {error}")
        return 1

    root = workspace.workspace_path(PROJECT_ID)
    total = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())

    show("peripherals", ", ".join(PERIPHERALS))
    show("modules", ", ".join(copied.modules))
    show("sources", f"{len(copied.sources)} files to compile")
    show("headers", f"{copied.headers} copied")
    show("size", f"{total / 1_000_000:.1f} MB per project")
    if copied.unsupported:
        show("no driver", ", ".join(copied.unsupported))
    for name in copied.sources[-2:]:
        print(f"              {name}")

    workspace.remove_workspace(PROJECT_ID)
    print("OK    the drivers are readable and copy into a project")
    return 0


if __name__ == "__main__":
    sys.exit(main())
