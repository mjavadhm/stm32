"""Bounded repair loop (M4 P5): build errors back into the firmware agent.

    firmware -> build --(failed, repairable, attempt <= N)--> firmware(patch) -> build
                  `----(ok, or nothing left to try)--------> END

Rules from docs/m4-plan.md, P5:

* Bounded. `FIRMWARE_BUILD_RETRIES` (default 2) repairs at most, so at most
  N+1 builds. An unbounded loop burns tokens on an error the model cannot fix.
* Small input. At most `MAX_ERRORS` diagnostics, and only the lines around
  each error (plus the include block) -- never whole files.
* Only model-written files. An error in a template (startup, linker script,
  HAL config) is our bug; the model is never asked to "fix" it.

The model answers with line-range edits, not a rewritten file. An edit must
stay inside an excerpt it was shown, and a patch that loses a
`USER CODE BEGIN/END` marker is rejected: the scaffold regenerates around
those markers and silently dropping one would cost the user their code.
"""

import logging
from typing import Any

from pydantic import BaseModel, Field

from app.agents.base import request_contract
from app.build import workspace
from app.codegen import checks
from app.codegen.ownership import strip_owned
from app.core.config import settings
from app.core.llm import get_agent_llm, is_agent_enabled
from app.orchestrator.contracts import (
    BUILD_FAILED,
    BUILD_INCOMPLETE,
    BuildResult,
    ContractError,
    Diagnostic,
    FirmwareBundle,
    SourceFile,
)

logger = logging.getLogger(__name__)

AGENT_NAME = "firmware"  # a repair is another firmware attempt
MAX_ERRORS = 5
CONTEXT_LINES = 6  # lines shown before and after each error line
HEAD_LINES = 20  # the include block, so a missing #include can be added
NO_LINE_WINDOW = 60  # errors without a line number (linker) see the file head
# A patch may not quietly delete code. Shrinking a file by more than this
# share, or dropping a call, needs a stated reason (`removed`).
MAX_SHRINK = 0.3
GUARD_RETRIES = 1  # one more try, in the same attempt, after a rejected patch

_SYSTEM_PROMPT = """You fix STM32 HAL C firmware so it builds AND does its job.

You are shown diagnostics for ONE file and numbered excerpts of that file.
Diagnostics come from the compiler, or from the project checker (codes
starting with `check-`): those mean the code compiles but is unfinished --
a module main() never calls, a declared function nobody defined, a
placeholder comment, a commented-out call. Fix the cause in code.

Rules:
1. Edit only lines that appear in the excerpts. `start_line`..`end_line` are
   inclusive and are replaced by `replacement` (whole lines, no line numbers,
   use \\n between lines). To delete lines use an empty replacement. To insert,
   replace one shown line with itself plus the new lines.
2. Never remove or rename `/* USER CODE BEGIN ... */` or `/* USER CODE END ... */`.
3. Use only the peripheral handles listed in the configuration (e.g. `hspi1`)
   and only project functions, types and fields declared in the project headers
   shown. A function that is declared nowhere does not exist: call one that is
   declared, or implement it as a `static` helper in this file. Never call a
   function you have not been shown. Do not invent HAL functions.
4. Never make an error disappear by deleting or commenting out the code that
   has it, and never write "omitted", "TODO" or a placeholder. If removing a
   call really is the correct fix, list it in "removed" with the reason; a
   removal without a reason is rejected.
5. Make the smallest change that fixes the error. Do not reformat other code.
6. Reply with ONLY a JSON object:
{
  "path": "Core/Src/example.c",
  "edits": [{"start_line": 12, "end_line": 12, "replacement": "  HAL_SPI_Init(&hspi1);"}],
  "removed": [{"name": "old_call", "reason": "why removing it is right"}],
  "notes": ["what was wrong"]
}"""


class LineEdit(BaseModel):
    start_line: int
    end_line: int
    replacement: str = ""


class Removal(BaseModel):
    name: str
    reason: str = ""


class FilePatch(BaseModel):
    path: str = ""
    edits: list[LineEdit] = Field(default_factory=list)
    # Calls the model says it removed on purpose, each with a reason.
    removed: list[Removal] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Deciding whether to repair
# --------------------------------------------------------------------------


def repairable_errors(
    result: BuildResult, bundle: FirmwareBundle, limit: int = MAX_ERRORS
) -> list[Diagnostic]:
    """Errors located in files the model wrote, first `limit` of them.

    main.c is the exception: the checker reports an uncalled module there,
    and wiring a module into USER CODE is the model's job even when no step
    listed main.c among the files to write.
    """
    generated = {source.path for source in bundle.files if source.generated}
    return [
        d
        for d in result.errors
        if d.file in generated or (d.tool == checks.TOOL and d.file == checks.MAIN)
    ][:limit]


def should_repair(result: BuildResult, bundle: FirmwareBundle, attempt: int) -> bool:
    """One rule, read by both the graph edge and the worker's progress rows."""
    return (
        result.status in (BUILD_FAILED, BUILD_INCOMPLETE)
        and attempt <= settings.firmware_build_retries
        and bool(repairable_errors(result, bundle))
    )


# --------------------------------------------------------------------------
# Excerpts and edits
# --------------------------------------------------------------------------


def error_windows(line_count: int, errors: list[Diagnostic]) -> list[tuple[int, int]]:
    """Merged 1-based inclusive line ranges the model is allowed to see/edit."""
    if line_count <= 0:
        return []
    ranges = [(1, min(line_count, HEAD_LINES))]
    for diagnostic in errors:
        if diagnostic.line > 0:
            ranges.append(
                (
                    max(1, diagnostic.line - CONTEXT_LINES),
                    min(line_count, diagnostic.line + CONTEXT_LINES),
                )
            )
        else:
            ranges.append((1, min(line_count, NO_LINE_WINDOW)))
    ranges.sort()
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def render_excerpts(lines: list[str], windows: list[tuple[int, int]]) -> str:
    width = len(str(len(lines)))
    blocks = []
    for start, end in windows:
        body = "\n".join(f"{n:>{width}}| {lines[n - 1]}" for n in range(start, end + 1))
        blocks.append(f"--- lines {start}-{end} ---\n{body}")
    return "\n".join(blocks)


def apply_edits(
    lines: list[str], edits: list[LineEdit], windows: list[tuple[int, int]]
) -> tuple[list[str], int, list[str]]:
    """Apply edits bottom-up. Returns (new_lines, applied, rejection messages)."""
    rejected: list[str] = []
    accepted: list[LineEdit] = []
    for edit in edits:
        if edit.start_line < 1 or edit.end_line < edit.start_line:
            rejected.append(f"invalid range {edit.start_line}-{edit.end_line}")
            continue
        if not any(s <= edit.start_line and edit.end_line <= e for s, e in windows):
            rejected.append(f"lines {edit.start_line}-{edit.end_line} were not shown")
            continue
        if any(
            edit.start_line <= other.end_line and other.start_line <= edit.end_line
            for other in accepted
        ):
            rejected.append(f"lines {edit.start_line}-{edit.end_line} overlap another edit")
            continue
        accepted.append(edit)

    result = list(lines)
    for edit in sorted(accepted, key=lambda e: e.start_line, reverse=True):
        replacement = edit.replacement.split("\n") if edit.replacement else []
        result[edit.start_line - 1 : edit.end_line] = replacement
    return result, len(accepted), rejected


def _markers(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if "USER CODE BEGIN" in line or "USER CODE END" in line
    ]


# --------------------------------------------------------------------------
# The guard: a repair may not delete its way to a clean build
# --------------------------------------------------------------------------


def _significant(text: str) -> int:
    return sum(1 for line in text.split("\n") if line.strip())


def guard_patch(
    path: str,
    original: str,
    patched: str,
    errors: list[Diagnostic],
    removed: list[Removal],
) -> tuple[list[str], list[str]]:
    """(problems, accepted removals) for one patched file.

    A call that disappears is fine when an error named it (replacing a call
    to an undeclared `spi_bus_select` *is* the fix) or when the model listed
    it under `removed` with a reason. Anything else is code deleted to make
    an error go away, and so is a new placeholder or a file that shrank by
    more than MAX_SHRINK.
    """
    named = " ".join(d.message for d in errors)
    justified = {
        item.name.strip(): item.reason.strip()
        for item in removed
        if item.name.strip() and len(item.reason.strip()) >= 10
    }
    lost = sorted(checks.calls(original) - checks.calls(patched))
    unexplained = [name for name in lost if name not in named and name not in justified]
    problems: list[str] = []
    if unexplained:
        problems.append(
            f"the patch removed {', '.join(f'`{n}`' for n in unexplained)} without fixing "
            "the cause"
        )
    if checks.unfinished_markers(path, patched) > checks.unfinished_markers(path, original):
        problems.append("the patch added a placeholder comment or commented-out code")
    before, after = _significant(original), _significant(patched)
    if before >= 20 and after < before * (1 - MAX_SHRINK) and not justified:
        problems.append(f"the patch deleted {before - after} of {before} lines")
    accepted = [f"`{name}`: {justified[name]}" for name in lost if name in justified]
    return problems, accepted


def _guard_feedback(problems: list[str]) -> str:
    return (
        "Rejected: "
        + "; ".join(problems)
        + ". The code must still do what it did. Fix the error itself: declare, include, "
        "implement or correct the call. If removing a call really is the right fix, "
        'list it under "removed" with the reason. Reply with the corrected JSON only.'
    )


# --------------------------------------------------------------------------
# The repair step
# --------------------------------------------------------------------------


def build_repair_prompt(path: str, errors: list[Diagnostic], excerpts: str, context: str) -> str:
    return "\n".join(
        [
            f"# File: `{path}`",
            "\n# Diagnostics",
            *(f"- {d.as_prompt()}" for d in errors),
            "\n# Project context",
            context or "(none)",
            "\n# Excerpts (line numbers are for reference only)",
            excerpts,
        ]
    )


def _read_current(project_id: str, bundle: FirmwareBundle, path: str) -> str | None:
    """What the compiler actually saw: the workspace copy, not the bundle.

    The scaffold refresh rewrites main.c around the model's USER CODE, so the
    bundle's copy can be a few lines off from the line numbers gcc reports.
    """
    if project_id:
        try:
            if workspace.exists(project_id):
                return workspace.read_file(project_id, path)
        except workspace.WorkspaceError:
            pass
    source = bundle.file(path)
    return source.contents if source is not None else None


async def repair_firmware(
    bundle: FirmwareBundle,
    result: BuildResult,
    *,
    project_id: str = "",
    context: str = "",
    llm: Any = None,
) -> tuple[FirmwareBundle, list[str], dict[str, Any]]:
    """Patch the files named by the build's first errors.

    Returns the updated bundle, warnings and a report for the progress view.
    Never raises: a failed repair simply leaves the files as they were, and
    the next build reports the same errors.
    """
    errors = repairable_errors(result, bundle)
    report: dict[str, Any] = {
        "from_attempt": result.attempt,
        "errors": [d.as_prompt() for d in errors],
        "files": {},
        "rejected": [],
        "removed": [],
        "notes": [],
    }
    warnings: list[str] = []
    if not errors:
        warnings.append("repair: no errors in generated files to fix")
        return bundle, warnings, report

    if llm is None:
        if not is_agent_enabled(AGENT_NAME):
            warnings.append("repair: firmware agent is disabled; nothing patched")
            return bundle, warnings, report
        llm = get_agent_llm(AGENT_NAME)

    by_file: dict[str, list[Diagnostic]] = {}
    for diagnostic in errors:
        by_file.setdefault(diagnostic.file, []).append(diagnostic)

    updated = bundle.model_copy(deep=True)
    for path, file_errors in by_file.items():
        original = _read_current(project_id, updated, path)
        if original is None:
            warnings.append(f"repair: {path} not found")
            continue
        lines = original.split("\n")
        windows = error_windows(len(lines), file_errors)
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": build_repair_prompt(
                    path, file_errors, render_excerpts(lines, windows), context
                ),
            },
        ]
        patched = ""
        applied = 0
        for round_ in range(GUARD_RETRIES + 1):
            try:
                patch, repair_warnings, reply = await request_contract(
                    llm, FilePatch, messages, temperature=0.0
                )
                warnings.extend(repair_warnings)
            except ContractError as exc:
                warnings.append(f"repair: no usable patch for {path}: {exc}")
                applied = 0
                break

            new_lines, applied, rejected = apply_edits(lines, patch.edits, windows)
            report["rejected"].extend(f"{path}: {message}" for message in rejected)
            report["notes"].extend(f"{path}: {note}" for note in patch.notes)
            if not applied:
                break
            patched = "\n".join(new_lines)
            if _markers(patched) != _markers(original):
                report["rejected"].append(f"{path}: patch changed USER CODE markers")
                applied = 0
                break

            problems, removals = guard_patch(path, original, patched, file_errors, patch.removed)
            if not problems:
                for removal in removals:
                    report["removed"].append(f"{path}: {removal}")
                    warnings.append(f"repair removed {removal} from {path}")
                break
            report["rejected"].append(f"{path}: {'; '.join(problems)}")
            applied = 0
            if round_ < GUARD_RETRIES:
                messages = [
                    *messages,
                    {"role": "assistant", "content": reply},
                    {"role": "user", "content": _guard_feedback(problems)},
                ]
        if not applied:
            report["files"][path] = 0
            continue
        # A patch must not bring back what the scaffold owns.
        owned_contents, owned = strip_owned(path, patched)
        if owned and owned_contents is not None:
            patched = owned_contents
            report["removed"].append(f"{path}: scaffold-owned {', '.join(owned)}")

        if project_id:
            try:
                workspace.write_file(project_id, path, patched)
            except workspace.WorkspaceError as exc:
                warnings.append(f"repair: could not write {path}: {exc}")
                continue
        for index, source in enumerate(updated.files):
            if source.path == path:
                updated.files[index] = source.model_copy(update={"contents": patched})
                break
        else:
            updated.files.append(SourceFile(path=path, contents=patched, purpose="repair"))
        report["files"][path] = applied

    if not any(report["files"].values()):
        warnings.append("repair: no edit could be applied; the next build will repeat")
    return updated, warnings, report
