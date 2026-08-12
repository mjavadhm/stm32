"""Compiler output in, structured diagnostics out.

The repair loop cannot act on a 4000-line log: it needs a file, a line and a
message, and it needs the *cause* rather than the cascade behind it. This is
the single place in the system that reads compiler prose, so the build
sandbox stays a plain toolchain and this parser is unit tested against real
captured logs (`tests/fixtures/gcc_errors.txt`) with no cross compiler in
sight.
"""

import re

from app.orchestrator.contracts import BuildSize, Diagnostic

# Core/Src/main.c:42:5: error: 'x' undeclared (first use in this function)
_GCC_RE = re.compile(
    r"^(?P<file>[^\s:][^:]*):(?P<line>\d+):(?:(?P<column>\d+):)?\s*"
    r"(?P<severity>fatal error|error|warning|note):\s*(?P<message>.*)$"
)
# trailing " [-Wunused-variable]"
_CODE_RE = re.compile(r"\s*\[(?P<code>-W[\w+=-]+)\]\s*$")
# arm-none-eabi-ld: main.o:(.text+0x8): undefined reference to `foo'
_LD_REF_RE = re.compile(r"(?P<file>[^\s:]+):\((?P<section>[^)]*)\):\s*(?P<message>.+)$")
_LD_LINE_RE = re.compile(r"\bld(?:\.exe)?:\s*(?P<message>.+)$")
# ld repeats a severity inside its own message: "ld: foo.o: warning: ...".
# Without this, newlib's four "note:" lines on a *successful* link are stored
# as errors and the repair loop wakes up to fix a build that is already green.
_LD_SEVERITY_RE = re.compile(
    r"^(?P<location>.*?):\s*(?P<severity>fatal error|error|warning|note):\s*(?P<message>.+)$"
)
# Core/Src/mpu6050.c:31: undefined reference to `HAL_I2C_Mem_Write'
# (ld prints the location on its own line, with no severity word on it)
_LD_LOCATION_RE = re.compile(
    r"^(?P<file>[^\s:][^:]*):(?P<line>\d+):\s*"
    r"(?P<message>(?:undefined reference|multiple definition|relocation truncated).*)$"
)
# region `RAM' overflowed by 4096 bytes
_OVERFLOW_RE = re.compile(r"region `?(?P<region>\w+)'? overflowed by (?P<bytes>\d+) bytes")
# make[1]: *** [Makefile:52: build/main.o] Error 1
_MAKE_RE = re.compile(r"^make(?:\[\d+\])?:\s*\*\*\*\s*(?P<message>.+)$")
# the "[Makefile:52: ...]" part of that line
_MAKE_TARGET_RE = re.compile(r"\[(?P<file>[^\s:\]]+):(?P<line>\d+):")
# mkdir: cannot create directory 'build': Permission denied
# A recipe that dies before the compiler starts. Not a cascade -- the cause.
_SHELL_RE = re.compile(
    r"^(?P<tool>[\w.+-]+):\s*(?P<message>[^:].*?"
    r"(?:Permission denied|No such file or directory|command not found|Read-only file system)"
    r".*)$"
)
#    text	   data	    bss	    dec	    hex	filename
_SIZE_RE = re.compile(r"^\s*(?P<text>\d+)\s+(?P<data>\d+)\s+(?P<bss>\d+)\s+(?P<dec>\d+)\b")

_SEVERITIES = {"fatal error": "error", "error": "error", "warning": "warning", "note": "note"}


def _relative(path: str, root: str) -> str:
    """Strip the workspace prefix.

    The model wrote `Core/Src/main.c`; it must be told the error is in
    `Core/Src/main.c`, not in `/workspaces/7f3a.../Core/Src/main.c`. Absolute
    paths also leak the server layout into stored results.
    """
    cleaned = path.strip()
    if root:
        prefix = root.rstrip("/") + "/"
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    return cleaned.removeprefix("./")


def _split_code(message: str) -> tuple[str, str]:
    match = _CODE_RE.search(message)
    if not match:
        return message.strip(), ""
    return message[: match.start()].strip(), match.group("code")


def parse_log(log: str, *, root: str = "", limit: int = 200) -> list[Diagnostic]:
    """Every actionable message in a build log, in the order gcc printed them.

    `make: *** [...] Error 1` lines are kept only when nothing else explains
    the failure: they are a summary of someone else's error, and feeding them
    to a model as if they were the problem is how a repair attempt starts
    editing the Makefile instead of the code.
    """
    diagnostics: list[Diagnostic] = []
    make_failures: list[Diagnostic] = []
    seen: set[tuple[str, int, int, str, str]] = set()

    def add(diagnostic: Diagnostic, bucket: list[Diagnostic]) -> None:
        key = (
            diagnostic.file,
            diagnostic.line,
            diagnostic.column,
            diagnostic.severity,
            diagnostic.message,
        )
        if key in seen:
            return
        seen.add(key)
        bucket.append(diagnostic)

    for raw_line in (log or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue

        gcc = _GCC_RE.match(line)
        if gcc:
            message, code = _split_code(gcc.group("message"))
            add(
                Diagnostic(
                    file=_relative(gcc.group("file"), root),
                    line=int(gcc.group("line")),
                    column=int(gcc.group("column") or 0),
                    severity=_SEVERITIES.get(gcc.group("severity"), "error"),
                    code=code,
                    message=message,
                    tool="gcc",
                ),
                diagnostics,
            )
            continue

        ld_location = _LD_LOCATION_RE.match(line)
        if ld_location:
            add(
                Diagnostic(
                    file=_relative(ld_location.group("file"), root),
                    line=int(ld_location.group("line")),
                    severity="error",
                    message=ld_location.group("message").strip(),
                    tool="ld",
                ),
                diagnostics,
            )
            continue

        overflow = _OVERFLOW_RE.search(line)
        if overflow:
            add(
                Diagnostic(
                    severity="error",
                    message=(
                        f"region {overflow.group('region')} overflowed by "
                        f"{overflow.group('bytes')} bytes"
                    ),
                    tool="ld",
                ),
                diagnostics,
            )
            continue

        ld_line = _LD_LINE_RE.search(line)
        if ld_line:
            body = ld_line.group("message")
            # "...: in function `foo':" is context, not a finding -- the real
            # error is on the following line, and keeping both would show the
            # repair loop the same problem twice.
            if body.rstrip().endswith("':"):
                continue
            severity = "error"
            location = ""
            inner = _LD_SEVERITY_RE.match(body)
            if inner:
                severity = _SEVERITIES.get(inner.group("severity"), "error")
                location = inner.group("location").strip()
                body = inner.group("message").strip()
            reference = _LD_REF_RE.search(location or body)
            if reference:
                location = reference.group("file")
                if not inner:
                    body = reference.group("message")
            location = _relative(location.split(":(", 1)[0], root)
            add(
                Diagnostic(
                    # A path inside the toolchain's own libc says nothing to a
                    # model that can only edit files in the workspace.
                    file="" if location.startswith("/") else location,
                    severity=severity,
                    message=body.strip(),
                    tool="ld",
                ),
                diagnostics,
            )
            continue

        make_failure = _MAKE_RE.match(line)
        if make_failure:
            message = make_failure.group("message").strip()
            location = _MAKE_TARGET_RE.search(message)
            add(
                Diagnostic(
                    file=_relative(location.group("file"), root) if location else "",
                    line=int(location.group("line")) if location else 0,
                    severity="error",
                    message=message,
                    tool="make",
                ),
                make_failures,
            )
            continue

        shell = _SHELL_RE.match(line)
        if shell:
            add(
                Diagnostic(
                    severity="error",
                    message=f"{shell.group('tool')}: {shell.group('message').strip()}",
                    tool="shell",
                ),
                diagnostics,
            )

    if not any(diagnostic.severity == "error" for diagnostic in diagnostics):
        diagnostics.extend(make_failures)
    return diagnostics[:limit]


def parse_size(output: str, *, flash_total: int = 0, ram_total: int = 0) -> BuildSize:
    """Read `arm-none-eabi-size` output.

    Flash is text+data (initialisers are stored in flash and copied at
    startup) and RAM is data+bss -- the numbers people actually care about,
    which the raw columns do not give you directly.
    """
    for line in (output or "").splitlines():
        match = _SIZE_RE.match(line)
        if not match:
            continue
        return BuildSize(
            text=int(match.group("text")),
            data=int(match.group("data")),
            bss=int(match.group("bss")),
            flash_total=flash_total,
            ram_total=ram_total,
        )
    return BuildSize(flash_total=flash_total, ram_total=ram_total)


def summarise(diagnostics: list[Diagnostic]) -> str:
    """One line for logs and for the run status."""
    errors = [d for d in diagnostics if d.severity == "error"]
    warnings = [d for d in diagnostics if d.severity == "warning"]
    if not errors and not warnings:
        return "no diagnostics"
    summary = f"{len(errors)} error(s), {len(warnings)} warning(s)"
    if errors:
        summary += f" -- first: {errors[0].as_prompt()}"
    return summary
