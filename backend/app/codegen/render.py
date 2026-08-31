"""A template engine small enough to read in one sitting.

Adding jinja2 to requirements.txt would make everyone rebuild the backend
image for the sake of `{{ }}`, and templates that can branch and loop are
templates that end up holding logic no test ever reaches. So: literal
substitution, no logic, and every decision stays in Python where it can be
asserted on.

A placeholder left unfilled is an error rather than an empty string. A hole
in a Makefile becomes a compile failure three minutes later, inside a
container, with a much worse message than "you forgot C_SOURCES".
"""

import re
from pathlib import Path

from app.codegen.errors import CodegenError

TEMPLATE_DIR = Path(__file__).parent / "templates"

_PLACEHOLDER = re.compile(r"\{\{([A-Z0-9_]+)\}\}")

# CubeMX's marker, kept exactly: an engineer who has seen a generated project
# already knows what it means, and existing tooling looks for it.
_USER_CODE = re.compile(
    r"/\* USER CODE BEGIN (?P<name>[^*]+?) \*/"
    r"(?P<body>.*?)"
    r"/\* USER CODE END (?P=name) \*/",
    re.DOTALL,
)


def render(name: str, values: dict[str, str]) -> str:
    """Fill a template, refusing to leave a hole or accept a typo."""
    path = TEMPLATE_DIR / name
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise CodegenError(f"missing template {name}") from error

    used: set[str] = set()

    def substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise CodegenError(f"{name}: nothing to put in {{{{{key}}}}}")
        used.add(key)
        return values[key]

    filled = _PLACEHOLDER.sub(substitute, text)

    unused = sorted(set(values) - used)
    if unused:
        # Almost always a renamed placeholder, which would otherwise show up
        # as a missing section in a file nobody reads.
        raise CodegenError(f"{name}: {', '.join(unused)} is not in the template")
    return filled


def user_regions(text: str) -> dict[str, str]:
    """The contents of every USER CODE region, keyed by marker name."""
    return {match.group("name"): match.group("body") for match in _USER_CODE.finditer(text)}


def merge_user_code(previous: str, generated: str) -> str:
    """Regenerate a file without throwing away what was written into it.

    Regeneration happens on every repair attempt. If it wiped the USER CODE
    regions, the repair loop would delete the firmware it is trying to fix.
    Regions that disappeared from the plan are dropped with the code they
    contained; that is the same trade CubeMX makes.
    """
    kept = user_regions(previous)

    def restore(match: re.Match[str]) -> str:
        name = match.group("name")
        body = kept.get(name)
        if body is None or not body.strip():
            return match.group(0)
        return f"/* USER CODE BEGIN {name} */{body}/* USER CODE END {name} */"

    return _USER_CODE.sub(restore, generated)
