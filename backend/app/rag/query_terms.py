"""Pulling C identifiers out of a natural-language question.

The symbol channel answers "what is the exact signature of X". To do that it
first has to know what X is. Taking the last word of the query is wrong in
both directions:

- "how do I use HAL_SPI_Transmit with DMA?" -> last word is "DMA?" and the
  identifier the user actually named is dropped;
- for a Persian question the last word is a Persian verb, which matches
  nothing, so the channel silently contributes nothing;
- for "configure spi with dma" the substring tier then returns whatever
  happens to contain "dma", which is noise presented as exact fact.

So: find every token that *looks* like a C identifier, rank them, and look up
the best few. If none look like identifiers, return nothing at all -- an
empty symbol slot is far better than a confidently wrong one.

Ported from PageVault's `app/textrag/query_terms.py` (stdlib only there, stdlib
only here): the retrieval fan-out lives in this client now, so the query-term
logic has to live here too.

Standard library only.
"""

from __future__ import annotations

import re

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_CAMEL_RE = re.compile(r"[a-z][A-Z]")

# Words that pass the shape tests but are never symbols people ask about.
# Kept deliberately small: the shape filter does most of the work.
_STOPWORDS = frozenset(
    """
    the and for with what how why when where which this that these those from
    into your yours our ours their about does did done use uses using used
    make makes making need needs needed want wants please help error errors
    code codes file files line lines value values function functions example
    examples work works working write writes writing read reads reading
    should would could must can cannot not but you are was were have has had
    give gives given show shows tell tells find finds fix fixes fixed
    generate generates create creates creating add adds added set sets
    setting explain explains difference between
    """.split()
)

# Bare words that are real symbol *fragments* but far too broad on their own.
_TOO_BROAD = frozenset({"hal", "ll", "ll_", "stm", "stm32", "cmsis", "gpio", "dma"})


def looks_like_identifier(token: str) -> bool:
    """True when a token has the *shape* of a C identifier, not of a word.

    Three accepted shapes, matching how ST names things:
      snake / screaming snake  HAL_SPI_Transmit, SPI_CR1_BR_Msk
      CamelCase                SPI_HandleTypeDef, TransmitReceive
      SHOUTY                   HAL_OK, ENABLE (length-guarded)
    A plain lowercase English word matches none of them.
    """
    if token.lower() in _STOPWORDS or token.lower() in _TOO_BROAD:
        return False
    if "_" in token:
        return True
    if _CAMEL_RE.search(token):
        return True
    if token.isupper() and len(token) > 3:
        return True
    return False


def _score(token: str) -> int:
    """Longer and more structured tokens are more likely to be the subject."""
    score = min(len(token), 48)
    if "_" in token:
        score += 20
    if _CAMEL_RE.search(token):
        score += 10
    if token.isupper() and len(token) > 3:
        score += 6
    if any(char.isdigit() for char in token):
        score += 4
    return score


def extract_identifiers(query: str, limit: int = 4) -> list[str]:
    """Best-first list of identifier-shaped tokens found in ``query``.

    Empty when the question names no identifier -- callers must treat that as
    "skip the symbol channel", not as "fall back to something".
    """
    seen: set[str] = set()
    scored: list[tuple[int, int, str]] = []
    for order, match in enumerate(_IDENT_RE.finditer(query)):
        token = match.group(0)
        lowered = token.lower()
        if lowered in seen or not looks_like_identifier(token):
            continue
        seen.add(lowered)
        # -order keeps the earliest mention first on a score tie.
        scored.append((_score(token), -order, token))
    scored.sort(reverse=True)
    return [token for _, _, token in scored[:limit]]


# Types that appear in every HAL signature and add nothing as context.
_TRIVIAL_TYPES = frozenset(
    {
        "void",
        "char",
        "short",
        "int",
        "long",
        "float",
        "double",
        "signed",
        "unsigned",
        "const",
        "volatile",
        "static",
        "extern",
        "inline",
        "struct",
        "union",
        "enum",
        "typedef",
        "return",
        "uint8_t",
        "uint16_t",
        "uint32_t",
        "uint64_t",
        "int8_t",
        "int16_t",
        "int32_t",
        "int64_t",
        "size_t",
        "bool",
    }
)


def referenced_types(signature: str, limit: int = 6) -> list[str]:
    """Type names a signature depends on, e.g. ``SPI_HandleTypeDef``.

    Retrieving ``HAL_SPI_TransmitReceive_DMA`` without ``SPI_HandleTypeDef``
    and ``HAL_StatusTypeDef`` gives a generator a call it cannot type-check.
    The symbol table already holds those definitions, so pulling them in is
    nearly free and removes a whole class of non-compiling output.
    """
    out: list[str] = []
    seen: set[str] = set()
    for match in _IDENT_RE.finditer(signature):
        token = match.group(0)
        lowered = token.lower()
        if lowered in _TRIVIAL_TYPES or lowered in seen:
            continue
        # Parameter names are lowercase or camelCase starting lowercase;
        # ST type names are CamelCase or SHOUTY and usually end in _t/Def.
        if not (token[0].isupper() or "_" in token):
            continue
        seen.add(lowered)
        out.append(token)
        if len(out) >= limit:
            break
    return out
