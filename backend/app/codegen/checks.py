"""Checks on model-written C that a compiler does not make (M4 P5).

"It compiled" was the only gate, and a weak model learned the shortest way
through it: delete the call that failed, leave `/* Sensor read omitted */` in
its place, and ship a main loop that does nothing. Nothing here needs gcc --
the code has already compiled when these run -- so they are text checks over
comment- and string-masked C, deliberately simple and deliberately strict:

* **signatures**: every function a project header declares is defined, with
  the same return and parameter types (`int32_t SPI_Bus_Init` against a
  `void SPI_Bus_Init(void)` prototype is a compile error in one file and a
  silent mismatch in every other one);
* **reachability**: every model-written module is reached from `main()`, an
  interrupt handler or a HAL callback. A driver nobody calls is dead weight
  that makes the build look finished;
* **placeholders**: "omitted", "TODO", "adjust as needed" and commented-out
  calls are unfinished work, not comments;
* **ASCII**: a stray non-ASCII character in code (outside comments and
  strings) is a model artefact, and in an unused macro it even compiles.

Each finding is a `Diagnostic` with a file and a line, so the repair loop can
show the model exactly where the job is unfinished.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.orchestrator.contracts import Diagnostic

TOOL = "check"

MAIN = "Core/Src/main.c"
# Where execution enters the project: main(), the vector table, the MSP hooks.
ENTRY_FILES = (MAIN, "Core/Src/stm32f4xx_it.c", "Core/Src/stm32f4xx_hal_msp.c")
# Rendered from templates; the model only owns their USER CODE regions.
MANAGED = frozenset({*ENTRY_FILES, "Core/Inc/main.h", "Core/Inc/stm32f4xx_it.h"})
# Pure template output, never checked.
TEMPLATES = frozenset({"Core/Src/system_stm32f4xx.c", "Core/Inc/stm32f4xx_hal_conf.h"})

KEYWORDS = frozenset(
    {
        "if", "for", "while", "switch", "return", "sizeof", "do", "else", "case",
        "defined", "__attribute__", "_Static_assert", "typeof", "__typeof__", "asm",
        "__asm", "__asm__", "_Alignof", "alignof", "__volatile__", "volatile",
    }
)
_STORAGE = frozenset(
    {"static", "inline", "extern", "__inline", "__STATIC_INLINE", "__weak", "__WEAK"}
)
_TYPE_WORDS = frozenset(
    {
        "void", "char", "short", "int", "long", "float", "double", "signed",
        "unsigned", "const", "volatile", "struct", "enum", "union", "_Bool",
    }
)

_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_HEAD_RE = re.compile(
    r"^(?P<ret>[\w\s\*]*?[\w\*])\s*\b(?P<name>[A-Za-z_]\w*)\s*\((?P<params>.*)\)$", re.DOTALL
)
_TOKEN_RE = re.compile(r"[A-Za-z_]\w*|\*|\[|\]|\.\.\.")
PLACEHOLDER_RE = re.compile(
    r"\b(?:omitted|adapt(?:ed)?\s+as\s+needed|adjust\s+as\s+needed|placeholder|"
    r"not\s+(?:yet\s+)?implemented|to\s+be\s+implemented|implement\s+(?:this|me|here|later)|"
    r"stub(?:bed)?|left\s+as\s+an\s+exercise)\b|\b(?:TODO|FIXME|XXX)\b",
    re.IGNORECASE,
)
# A statement inside a comment: `HAL_GPIO_WritePin(GPIOA, GPIO_PIN_4, GPIO_PIN_RESET);`
COMMENTED_CALL_RE = re.compile(r"\b[A-Za-z_]\w*\s*\([^;{}]*\)\s*;")
_REGION_RE = re.compile(
    r"/\*\s*USER CODE BEGIN (?P<name>[^*]*?)\s*\*/(?P<body>.*?)/\*\s*USER CODE END (?P=name)\s*\*/",
    re.DOTALL,
)


@dataclass
class Function:
    name: str
    signature: str  # normalised "ret name(types)"
    path: str
    line: int
    static: bool = False
    calls: set[str] = field(default_factory=set)
    offset: int = 0


@dataclass
class Parsed:
    path: str
    masked: str
    comments: list[tuple[int, int, str]]  # (start offset, line, text)
    declarations: list[Function] = field(default_factory=list)
    definitions: list[Function] = field(default_factory=list)


# --------------------------------------------------------------------------
# Lexing
# --------------------------------------------------------------------------


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def mask(
    text: str, *, keep_preprocessor: bool = False
) -> tuple[str, list[tuple[int, int, str]]]:
    """Blank comments, string/char literals and (by default) preprocessor lines.

    Offsets and newlines are preserved, so a line number found in the masked
    text is the line number in the file. Comments are returned separately.
    """
    out = list(text)
    comments: list[tuple[int, int, str]] = []
    i, n = 0, len(text)
    line_start = True
    while i < n:
        ch = text[i]
        if line_start and ch in " \t":
            i += 1
            continue
        if line_start and ch == "#" and not keep_preprocessor:
            # Preprocessor line, with backslash continuations.
            j = i
            while j < n:
                if text[j] == "\n" and text[j - 1] != "\\":
                    break
                j += 1
            _mask_range(text, out, i, j, comments)
            i = j
            continue
        line_start = ch == "\n"
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            comments.append((i, line_of(text, i), text[i:end]))
            _blank(out, i, end)
            i = end
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end == -1 else end
            comments.append((i, line_of(text, i), text[i:end]))
            _blank(out, i, end)
            i = end
            continue
        if ch in "\"'":
            j = i + 1
            while j < n and text[j] != ch and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            _blank(out, i + 1, min(j, n))
            i = j + 1
            continue
        i += 1
    return "".join(out), comments


def _blank(out: list[str], start: int, end: int) -> None:
    for k in range(start, min(end, len(out))):
        if out[k] != "\n":
            out[k] = " "


def _mask_range(
    text: str, out: list[str], start: int, end: int, comments: list[tuple[int, int, str]]
) -> None:
    """A preprocessor line is blanked, but its comments are still comments."""
    k = start
    while k < end:
        if text.startswith("/*", k):
            close = text.find("*/", k + 2)
            close = len(text) if close == -1 else close + 2
            comments.append((k, line_of(text, k), text[k:close]))
            k = close
            continue
        if text.startswith("//", k):
            comments.append((k, line_of(text, k), text[k:end]))
            break
        k += 1
    _blank(out, start, end)


# --------------------------------------------------------------------------
# Top-level parsing
# --------------------------------------------------------------------------


def _types_of(params: str) -> str:
    params = " ".join(params.split())
    if not params or params == "void":
        return "void"
    types = []
    for param in params.split(","):
        tokens = _TOKEN_RE.findall(param)
        idents = [i for i, t in enumerate(tokens) if re.match(r"[A-Za-z_]", t)]
        if len(idents) >= 2 and tokens[idents[-1]] not in _TYPE_WORDS:
            del tokens[idents[-1]]
        types.append(" ".join(tokens))
    return ", ".join(types)


def _signature(ret: str, name: str, params: str) -> tuple[str, bool]:
    tokens = _TOKEN_RE.findall(ret)
    static = "static" in tokens
    kept = [t for t in tokens if t not in _STORAGE]
    return f"{' '.join(kept)} {name}({_types_of(params)})", static


def _matching(text: str, start: int, open_ch: str, close_ch: str) -> int:
    depth = 0
    for k in range(start, len(text)):
        if text[k] == open_ch:
            depth += 1
        elif text[k] == close_ch:
            depth -= 1
            if depth == 0:
                return k
    return len(text) - 1


def parse(path: str, text: str) -> Parsed:
    masked, comments = mask(text)
    parsed = Parsed(path=path, masked=masked, comments=comments)
    seg_start = 0
    i, n = 0, len(masked)
    while i < n:
        ch = masked[i]
        if ch == ";":
            head = " ".join(masked[seg_start:i].split())
            _declaration(parsed, text, head, seg_start)
            seg_start = i + 1
        elif ch == "}":
            # The close of an `extern "C" {` block.
            seg_start = i + 1
        elif ch == "{":
            head = " ".join(masked[seg_start:i].split())
            if re.fullmatch(r'extern\s*"\s*"', head):
                seg_start = i + 1
                i += 1
                continue
            end = _matching(masked, i, "{", "}")
            match = _HEAD_RE.match(head) if head.endswith(")") else None
            if match and "=" not in head and match.group("name") not in KEYWORDS:
                signature, static = _signature(
                    match.group("ret"), match.group("name"), match.group("params")
                )
                offset = seg_start + len(masked[seg_start:i]) - len(masked[seg_start:i].lstrip())
                body = masked[i + 1 : end]
                parsed.definitions.append(
                    Function(
                        name=match.group("name"),
                        signature=signature,
                        path=path,
                        line=line_of(text, offset),
                        static=static,
                        calls={c for c in _CALL_RE.findall(body) if c not in KEYWORDS},
                    )
                )
                seg_start = end + 1
            # struct/enum/union bodies and initialisers: skip, keep the segment.
            i = end
        i += 1
    return parsed


def _declaration(parsed: Parsed, text: str, head: str, offset: int) -> None:
    if not head or "typedef" in head.split() or "=" in head or not head.endswith(")"):
        return
    match = _HEAD_RE.match(head)
    if not match or match.group("name") in KEYWORDS or not match.group("ret").strip():
        return
    signature, static = _signature(match.group("ret"), match.group("name"), match.group("params"))
    start = offset + len(parsed.masked[offset:]) - len(parsed.masked[offset:].lstrip())
    parsed.declarations.append(
        Function(
            name=match.group("name"),
            signature=signature,
            path=parsed.path,
            line=line_of(text, start),
            static=static,
            offset=start,
        )
    )


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------


def _diag(path: str, line: int, code: str, message: str) -> Diagnostic:
    return Diagnostic(
        file=path, line=line, severity="error", code=code, message=message, tool=TOOL
    )


def _regions(path: str, text: str) -> list[tuple[int, int]]:
    """Offsets the model owns: USER CODE bodies in managed files, else all."""
    if path not in MANAGED:
        return [(0, len(text))]
    return [(m.start("body"), m.end("body")) for m in _REGION_RE.finditer(text)]


def _inside(offset: int, regions: list[tuple[int, int]]) -> bool:
    return any(start <= offset < end for start, end in regions)


def placeholder_findings(path: str, text: str, parsed: Parsed | None = None) -> list[Diagnostic]:
    parsed = parsed or parse(path, text)
    regions = _regions(path, text)
    found: list[Diagnostic] = []
    for offset, line, comment in parsed.comments:
        if not _inside(offset, regions):
            continue
        body = comment.strip("/*").strip()
        placeholder = PLACEHOLDER_RE.search(comment)
        if placeholder:
            found.append(
                _diag(
                    path,
                    line,
                    "check-placeholder",
                    f"unfinished code: the comment says \"{placeholder.group(0)}\" "
                    f"(`{_short(body)}`). Write the code it describes; a comment does not run",
                )
            )
            continue
        call = COMMENTED_CALL_RE.search(comment)
        if call:
            found.append(
                _diag(
                    path,
                    line,
                    "check-commented-code",
                    f"commented-out call `{_short(call.group(0))}` does not run. Make it "
                    "real code (pins and handles are in main.h)",
                )
            )
    return found


def ascii_findings(path: str, text: str) -> list[Diagnostic]:
    # #define lines included: `#define REG 0x6B\u793e` compiles as long as nobody uses it.
    code, _ = mask(text, keep_preprocessor=True)
    regions = _regions(path, text)
    found: list[Diagnostic] = []
    seen_lines: set[int] = set()
    for offset, char in enumerate(code):
        if ord(char) < 128 or not _inside(offset, regions):
            continue
        line = line_of(text, offset)
        if line in seen_lines:
            continue
        seen_lines.add(line)
        found.append(
            _diag(
                path,
                line,
                "check-ascii",
                f"non-ASCII character {char!r} in code (outside comments and strings); "
                "remove it",
            )
        )
    return found


def _short(text: str, limit: int = 70) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def signature_findings(
    parsed: dict[str, Parsed], texts: dict[str, str], owned: set[str]
) -> list[Diagnostic]:
    definitions: dict[str, list[Function]] = {}
    for item in parsed.values():
        for function in item.definitions:
            definitions.setdefault(function.name, []).append(function)
    found: list[Diagnostic] = []
    reported: set[str] = set()
    for path, item in parsed.items():
        if not path.endswith(".h") or path not in owned:
            continue
        regions = _regions(path, texts[path])
        for declaration in item.declarations:
            if declaration.name in reported or not _inside(declaration.offset, regions):
                continue
            defined = definitions.get(declaration.name, [])
            if not defined:
                reported.add(declaration.name)
                source = path.replace("Core/Inc/", "Core/Src/").removesuffix(".h") + ".c"
                found.append(
                    _diag(
                        path,
                        declaration.line,
                        "check-undefined",
                        f"`{declaration.signature}` is declared here but defined in no "
                        f"source file. Implement it (in {source}), or if another declared "
                        "function already does this job, delete this declaration",
                    )
                )
                continue
            for definition in defined:
                if definition.signature != declaration.signature:
                    reported.add(declaration.name)
                    found.append(
                        _diag(
                            definition.path,
                            definition.line,
                            "check-signature",
                            f"`{declaration.name}` is declared in {path}:{declaration.line} "
                            f"as `{declaration.signature}` but defined here as "
                            f"`{definition.signature}`. Make this definition match the "
                            "declaration",
                        )
                    )
                    break
    return found


def _main_lines(text: str) -> tuple[int, int]:
    """Lines of USER CODE BEGIN 2 and BEGIN WHILE (or of main) in main.c."""
    begin2 = re.search(r"USER CODE BEGIN 2\b", text)
    loop = re.search(r"USER CODE BEGIN WHILE\b", text) or re.search(r"\bwhile\s*\(", text)
    main = re.search(r"\bint\s+main\s*\(", text)
    first = begin2 or main
    return (
        line_of(text, first.start()) if first else 1,
        line_of(text, loop.start()) if loop else 0,
    )


def reachability_findings(
    parsed: dict[str, Parsed], texts: dict[str, str], owned: set[str]
) -> list[Diagnostic]:
    graph: dict[str, set[str]] = {}
    entries: set[str] = set()
    for path, item in parsed.items():
        for function in item.definitions:
            graph.setdefault(function.name, set()).update(function.calls)
            if (
                path in ENTRY_FILES
                or function.name == "main"
                or function.name.endswith(("Callback", "_IRQHandler"))
            ):
                entries.add(function.name)
    reached: set[str] = set()
    pending = list(entries)
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name)
        pending.extend(graph.get(name, ()))

    unreached: list[tuple[str, list[str]]] = []
    for path, item in sorted(parsed.items()):
        if not path.endswith(".c") or path in ENTRY_FILES or path not in owned:
            continue
        public = [f.name for f in item.definitions if not f.static]
        if public and not any(f.name in reached for f in item.definitions):
            unreached.append((path, public))
    if not unreached or MAIN not in texts:
        return []
    init_line, loop_line = _main_lines(texts[MAIN])
    names = "; ".join(f"{path} ({', '.join(public[:4])})" for path, public in unreached)
    found = [
        _diag(
            MAIN,
            init_line,
            "check-unreachable",
            f"main() never reaches {names}. The firmware does nothing with it: call "
            "its init function here, after the MX_*_Init() calls",
        )
    ]
    if loop_line and loop_line != init_line:
        found.append(
            _diag(
                MAIN,
                loop_line,
                "check-unreachable",
                f"and call its work function (read/update/process) inside the main loop: {names}",
            )
        )
    return found


def check_sources(files: dict[str, str], owned: Iterable[str] | None = None) -> list[Diagnostic]:
    """Every check over a project's sources. `owned` = files the model wrote.

    Files outside `owned` still count as definitions and callers (a template
    calls into model code and defines what a model header may declare), but
    are never blamed.
    """
    texts = {path: text for path, text in files.items() if path not in TEMPLATES}
    owned_set = set(texts) if owned is None else {p for p in owned if p in texts}
    parsed = {path: parse(path, text) for path, text in texts.items()}
    # Most consequential first: the repair loop only shows the first few.
    found = reachability_findings(parsed, texts, owned_set)
    found += signature_findings(parsed, texts, owned_set)
    for path in sorted(owned_set):
        found += placeholder_findings(path, texts[path], parsed[path])
    for path in sorted(owned_set):
        found += ascii_findings(path, texts[path])
    return found


# --------------------------------------------------------------------------
# Used by the repair guard
# --------------------------------------------------------------------------


def calls(text: str) -> set[str]:
    """Every name called anywhere in a file (comments and strings excluded)."""
    masked, _ = mask(text)
    return {name for name in _CALL_RE.findall(masked) if name not in KEYWORDS}


def unfinished_markers(path: str, text: str) -> int:
    """How many placeholder comments / commented-out calls a file carries."""
    return len(placeholder_findings(path, text))


# --------------------------------------------------------------------------
# A source against its own header (generation and repair)
# --------------------------------------------------------------------------

_CONFLICT_RE = re.compile(r"conflicting types for '(?P<name>\w+)'")


def _declaration_text(text: str, parsed: Parsed, declaration: Function) -> str:
    end = parsed.masked.find(";", declaration.offset)
    return " ".join(text[declaration.offset : end].split())


def header_contract(header_path: str, header: str, source_path: str, source: str) -> list[str]:
    """What a source breaks of its own header, one sentence per problem.

    The header is the contract: it is generated first and every caller is
    written against it, so the fix is always on the definition side.
    """
    head = parse(header_path, header)
    body = parse(source_path, source)
    defined = {function.name: function for function in body.definitions}
    problems: list[str] = []
    for declaration in head.declarations:
        wanted = _declaration_text(header, head, declaration)
        definition = defined.get(declaration.name)
        if definition is None:
            problems.append(
                f"{header_path} declares `{wanted}` but {source_path} does not define it"
            )
        elif definition.signature != declaration.signature:
            problems.append(
                f"{source_path}:{definition.line} defines `{definition.signature}` but "
                f"{header_path} declares `{wanted}`; use the header's signature"
            )
    return problems


def align_definitions(header_path: str, header: str, source: str) -> tuple[str, list[str]]:
    """Give a `(void)` definition the parameter list its header declares.

    Only when the return types already agree and the definition takes no
    parameters: then the body cannot refer to them, and copying the header's
    list is a fix that cannot change what the body does.
    """
    head = parse(header_path, header)
    declared = {d.name: d for d in head.declarations}
    fixed: list[str] = []
    for match in reversed(list(_DEF_HEAD_RE.finditer(mask(source)[0]))):
        declaration = declared.get(match.group("name"))
        if declaration is None or " ".join(match.group("params").split()) not in ("", "void"):
            continue
        signature, _ = _signature(match.group("ret"), match.group("name"), "void")
        wanted_ret = declaration.signature.split(f" {declaration.name}(", 1)[0]
        if signature.split(f" {declaration.name}(", 1)[0] != wanted_ret:
            continue
        if declaration.signature == signature:
            continue
        wanted = _declaration_text(header, head, declaration)
        params = wanted[wanted.index("(") :]
        source = source[: match.start("open")] + params + source[match.end("close") :]
        fixed.append(declaration.name)
    return source, sorted(fixed)


_DEF_HEAD_RE = re.compile(
    r"^(?P<ret>[A-Za-z_][\w \t\*]*?[\w\*])[ \t]*\b(?P<name>[A-Za-z_]\w*)[ \t]*"
    r"(?P<open>\()(?P<params>[^()]*)(?P<close>\))\s*\{",
    re.MULTILINE,
)


def explain_conflicts(errors: list[Diagnostic], files: dict[str, str]) -> list[Diagnostic]:
    """Add the header's declaration to gcc's "conflicting types" errors.

    gcc names the clash; the declaration it clashes with sits in a note the
    repair prompt never shows, so the model guesses which side to change.
    """
    declarations: dict[str, str] = {}
    for path, text in files.items():
        if path.endswith(".h"):
            parsed = parse(path, text)
            for declaration in parsed.declarations:
                declarations.setdefault(
                    declaration.name,
                    f"{path}:{declaration.line} `"
                    f"{_declaration_text(text, parsed, declaration)}`",
                )
    explained = []
    for error in errors:
        match = _CONFLICT_RE.search(error.message)
        where = declarations.get(match.group("name")) if match else None
        if where and "declared in" not in error.message:
            error = error.model_copy(
                update={
                    "message": f"{error.message}; declared in {where}, which every caller "
                    "uses: change this definition to match it"
                }
            )
        explained.append(error)
    return explained
