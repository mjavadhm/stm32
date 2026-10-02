"""What the CubeMX scaffold owns, and keeping generated files off it.

The scaffold writes peripheral init (`MX_*_Init`), the MSP hooks, the clock
tree, the IRQ handlers and the HAL handle definitions. A model that writes
them again -- typically as `spi1.c`/`spi1.h` copied from CubeMX output --
produces duplicate symbols and, worse, a file the repair loop keeps patching
because nothing tells it the file should not exist. The rules here are
deterministic: planned files named after a peripheral are dropped before any
code is generated, and scaffold-owned definitions are cut out of whatever the
model writes anyway. A file left with nothing of its own is dropped, and so
are the includes that pointed at it.
"""

import posixpath
import re

# Files the scaffold writes itself (main.c/main.h are merged, not owned).
_SCAFFOLD_FILE_RE = re.compile(
    r"^(?:stm32\w*_it|stm32\w*_hal_msp|stm32\w*_hal_conf|system_stm32\w*"
    r"|startup_stm32\w*|syscalls|sysmem)$",
    re.IGNORECASE,
)
# A module named after a peripheral instance is CubeMX's own init file. The
# instance number is required: `uart.c` or `spi.c` may be an app-level wrapper.
_PERIPHERAL_FILE_RE = re.compile(
    r"^(?:gpio|dma|spi\d+|i2c\d+|i2s\d+|u?s?art\d+|lpuart\d+|tim\d+|adc\d+|dac\d*"
    r"|can\d+|rtc|crc|rng|iwdg|wwdg|sdio|fsmc)$",
    re.IGNORECASE,
)
RESERVED_FUNCTION_RE = re.compile(
    r"^(?:MX_\w+_Init|HAL_\w*MspInit|HAL_\w*MspDeInit|SystemClock_Config|SystemInit"
    r"|Error_Handler|assert_failed|main|\w+_IRQHandler)$"
)
_HANDLE_DEF_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?:static\s+)?(?P<type>\w+_HandleTypeDef)\s+"
    r"(?P<name>h\w+)\s*(?:=[^;]*)?;",
    re.MULTILINE,
)
# A declaration has a return type before the name; a call does not.
_PROTOTYPE_RE = re.compile(
    r"^[ \t]*(?:\w+[ \t\*]+)+\**(?P<name>\w+)\s*\([^;{()]*\)\s*;[ \t]*\n?",
    re.MULTILINE,
)
_INCLUDE_RE = re.compile(r'^[ \t]*#\s*include\s+"(?P<name>[^"]+)"[^\n]*\n?', re.MULTILINE)
# `extern "C" {` once its literal is masked; the block is still file scope.
_EXTERN_C_RE = re.compile(r"\bextern\s*\{")
_HEAD_RE = re.compile(r"\b(?P<name>\w+)\s*\([^{};]*\)\s*$", re.DOTALL)
_SCAFFOLD_MERGED = {"Core/Src/main.c", "Core/Inc/main.h"}


def _stem(path: str) -> str:
    return posixpath.splitext(posixpath.basename(path))[0]


def scaffold_owned_path(path: str) -> bool:
    """True for a planned file the model must not write at all."""
    if path in _SCAFFOLD_MERGED:
        return False
    stem = _stem(path)
    return bool(_SCAFFOLD_FILE_RE.match(stem) or _PERIPHERAL_FILE_RE.match(stem))


def scaffold_file(path: str) -> bool:
    """True for a file the scaffold itself writes (its includes stay valid)."""
    return bool(_SCAFFOLD_FILE_RE.match(_stem(path)))


def _mask(code: str) -> str:
    """Same length as `code`, with comments and literals blanked out."""
    out = list(code)
    i, n = 0, len(code)
    while i < n:
        two = code[i : i + 2]
        if two == "/*":
            end = code.find("*/", i + 2)
            end = n if end < 0 else end + 2
        elif two == "//":
            end = code.find("\n", i)
            end = n if end < 0 else end
        elif code[i] in "\"'":
            quote, end = code[i], i + 1
            while end < n and code[end] not in (quote, "\n"):
                end += 2 if code[end] == "\\" else 1
            end = min(end + 1, n)
        else:
            i += 1
            continue
        for k in range(i, end):
            if out[k] != "\n":
                out[k] = " "
        i = end
    return "".join(out)


def _top_level_functions(code: str) -> list[tuple[int, int, str]]:
    """(start, end, name) of each function definition at file scope.

    An unbalanced body runs to the end of the file: it is broken either way,
    and cutting it out is what lets the rest compile.
    """
    masked = _mask(code)
    found: list[tuple[int, int, str]] = []
    depth, head_start, i = 0, 0, 0
    while i < len(masked):
        char = masked[i]
        if depth == 0 and char in ";}":
            head_start = i + 1
        elif depth == 0 and char == "#":
            # A preprocessor line (with its continuations) ends a head too.
            end = masked.find("\n", i)
            while end > 0 and masked[:end].rstrip(" \t").endswith("\\"):
                end = masked.find("\n", end + 1)
            i = len(masked) if end < 0 else end
            head_start = i
            continue
        if char == "{":
            if depth == 0 and re.search(r"\bextern\s*$", masked[head_start:i]):
                head_start = i + 1
                i += 1
                continue
            if depth == 0:
                head = masked[head_start:i]
                match = _HEAD_RE.search(head)
                if match and not re.search(r"[=\[]", head):
                    end = _matching_brace(masked, i)
                    start = head_start + len(head) - len(head.lstrip())
                    found.append((start, end, match.group("name")))
                    i = end
                    head_start = end
                    continue
            depth += 1
        elif char == "}":
            depth = max(depth - 1, 0)
        i += 1
    return found


def _matching_brace(masked: str, open_at: int) -> int:
    depth = 0
    for index in range(open_at, len(masked)):
        if masked[index] == "{":
            depth += 1
        elif masked[index] == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return len(masked)


def _depth(masked: str, at: int) -> int:
    """Brace depth at an offset; only file-scope declarations are cut."""
    head = masked[:at]
    wrappers = len(_EXTERN_C_RE.findall(head))
    return head.count("{") - head.count("}") - wrappers


def _substantive(code: str) -> bool:
    """Anything left beyond includes, guards, externs and C++ wrappers."""
    text = _mask(re.sub(r'extern\s+"C"\s*\{', "", code))
    text = re.sub(r"^[ \t]*#[^\n]*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\bextern\b[^;{]*;", "", text)
    return bool(re.sub(r"[\s{};]", "", text))


def _keeps_own_code(path: str, code: str) -> bool:
    """A source keeps a function of its own; a header keeps any declaration.

    A source whose every function belonged to the scaffold is a CubeMX copy;
    whatever statements survive the cut are debris, not a module.
    """
    if path.endswith(".c"):
        return any(
            not RESERVED_FUNCTION_RE.match(name)
            for _, _, name in _top_level_functions(code)
        )
    return _substantive(code)


def strip_owned(path: str, contents: str) -> tuple[str | None, list[str]]:
    """Cut scaffold-owned definitions out of a generated file.

    Returns the new contents (None when nothing of the file's own is left)
    and the names removed.
    """
    if path in _SCAFFOLD_MERGED:
        return contents, []
    removed: list[str] = []
    code = contents
    for start, end, name in reversed(_top_level_functions(code)):
        if RESERVED_FUNCTION_RE.match(name):
            code = code[:start] + code[end:].lstrip("\n")
            removed.append(name)

    masked = _mask(code)
    cuts = [
        (m.start(), m.end(), m.group("name"))
        for m in _PROTOTYPE_RE.finditer(masked)
        if RESERVED_FUNCTION_RE.match(m.group("name")) and _depth(masked, m.start()) <= 0
    ]
    for start, end, name in reversed(cuts):
        code = code[:start] + code[end:]
        removed.append(name)

    def _extern(match: re.Match[str]) -> str:
        removed.append(match.group("name"))
        return f"{match.group('indent')}extern {match.group('type')} {match.group('name')};"

    # Handles are defined by main.c; a second definition is a link error.
    code = _HANDLE_DEF_RE.sub(_extern, code)
    removed = sorted(set(removed))
    if not removed:
        return contents, []
    if not _keeps_own_code(path, code):
        return None, removed
    return code, removed


def drop_includes(contents: str, dropped: set[str]) -> str:
    """Remove `#include "x.h"` lines for headers that were dropped."""
    names = {posixpath.basename(path) for path in dropped}
    return _INCLUDE_RE.sub(
        lambda m: "" if posixpath.basename(m.group("name")) in names else m.group(0),
        contents,
    )


OWNERSHIP_RULE = (
    "The CubeMX scaffold already owns peripheral and clock init: MX_*_Init, "
    "HAL_*_MspInit/MspDeInit, SystemClock_Config, Error_Handler, every "
    "*_IRQHandler and the handle definitions (hspi1, hi2c1, hdma_*). Never write "
    "them, and never create a file for them (no spi1.c, i2c1.c, gpio.c, dma.c, "
    "stm32*_it.c). Use the handles through `extern` / main.h."
)
