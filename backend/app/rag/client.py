"""Thin HTTP client for PageVault (the knowledge base, M2).

PageVault is a separate open-source service with its own repo, database and
release cycle. This module is deliberately the *only* place that knows its
wire format: agents receive plain dataclasses, so replacing or upgrading the
knowledge base never reaches into agent code.

Design rules:
  * No shared database, no shared Python package -- HTTP only.
  * Retrieval failure is never fatal. A knowledge base that is down degrades
    an agent to "answers without citations", it does not fail the pipeline.
  * Every snippet keeps `path` and line numbers so answers can cite sources.
"""

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Snippet:
    """One retrieved piece of evidence, normalised across channels."""

    channel: str  # chunk | symbol | type | page
    name: str
    path: str
    text: str
    line_start: int = 0
    line_end: int = 0
    score: float = 0.0
    signature: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def citation(self) -> str:
        if self.line_start and self.line_end:
            return f"{self.path}:{self.line_start}-{self.line_end}"
        return self.path or self.name


@dataclass(frozen=True)
class RagContext:
    """Everything retrieved for one question."""

    query: str
    chunks: list[Snippet] = field(default_factory=list)
    symbols: list[Snippet] = field(default_factory=list)
    type_context: list[Snippet] = field(default_factory=list)
    pages: list[Snippet] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    available: bool = True

    @property
    def is_empty(self) -> bool:
        return not (self.chunks or self.symbols or self.type_context or self.pages)

    def citations(self) -> list[str]:
        """Unique source references, in the order the model will see them."""
        seen: list[str] = []
        for snippet in [*self.symbols, *self.type_context, *self.chunks, *self.pages]:
            if snippet.citation and snippet.citation not in seen:
                seen.append(snippet.citation)
        return seen

    def as_prompt(self, max_chars: int | None = None) -> str:
        """Render the context block that gets prepended to an agent prompt.

        Symbols come first on purpose: an exact signature match is stronger
        evidence than a semantically similar paragraph, and the model tends
        to follow whatever it reads first.
        """
        limit = max_chars or settings.rag_context_max_chars
        sections: list[str] = []

        def add(title: str, snippets: list[Snippet]) -> None:
            if not snippets:
                return
            lines = [f"## {title}"]
            for snippet in snippets:
                header = f"[{snippet.citation}]"
                if snippet.signature:
                    header += f" {snippet.signature}"
                elif snippet.name:
                    header += f" {snippet.name}"
                lines.append(header)
                if snippet.text:
                    lines.append(snippet.text.strip())
                lines.append("")
            sections.append("\n".join(lines))

        add("Symbols (exact API definitions)", self.symbols)
        add("Types referenced by those symbols", self.type_context)
        add("Documentation and code excerpts", self.chunks)
        add("Reference-manual pages", self.pages)

        rendered = "\n".join(sections).strip()
        if len(rendered) > limit:
            # Truncate at a section boundary when possible so the model never
            # sees half a code block.
            cut = rendered.rfind("\n## ", 0, limit)
            rendered = rendered[: cut if cut > 0 else limit].rstrip()
            rendered += "\n\n[context truncated]"
        return rendered


def _above_score(snippets: list[Snippet], min_score: float) -> list[Snippet]:
    """Drop low-confidence hits.

    A snippet with score 0.0 is kept: PageVault omits the score on exact
    lookups, and "no score reported" must not be read as "scored zero".
    """
    if min_score <= 0:
        return snippets
    return [s for s in snippets if s.score == 0.0 or s.score >= min_score]


def _snippet_from_chunk(raw: dict[str, Any], channel: str = "chunk") -> Snippet:
    return Snippet(
        channel=channel,
        name=raw.get("name") or raw.get("heading_path") or "",
        path=raw.get("path", ""),
        text=raw.get("text", ""),
        line_start=int(raw.get("line_start") or 0),
        line_end=int(raw.get("line_end") or 0),
        score=float(raw.get("score") or 0.0),
        signature=raw.get("signature", ""),
        metadata=raw.get("metadata") or {},
    )


def _snippet_from_symbol(raw: dict[str, Any], channel: str = "symbol") -> Snippet:
    return Snippet(
        channel=channel,
        name=raw.get("name", ""),
        path=raw.get("path", ""),
        text=raw.get("doc", ""),
        line_start=int(raw.get("line_start") or 0),
        line_end=int(raw.get("line_end") or 0),
        signature=raw.get("signature", ""),
        metadata={
            "kind": raw.get("kind", ""),
            "match": raw.get("match", ""),
            "matched_term": raw.get("matched_term", ""),
        },
    )


def _snippet_from_page(raw: dict[str, Any]) -> Snippet:
    page_number = raw.get("page_number")
    return Snippet(
        channel="page",
        name=f"page {page_number}",
        path=f"{raw.get('document_id', 'document')}#p{page_number}",
        text="",  # Visual hit: the page image, not text.
        score=float(raw.get("score") or 0.0),
        metadata={"image_url": raw.get("image_url", ""), **(raw.get("metadata") or {})},
    )


class PageVaultClient:
    """Async client for PageVault's unified search."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or settings.pagevault_url).rstrip("/")
        self.timeout = timeout or settings.rag_timeout_seconds
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> bool:
        try:
            response = await self._client.get("/health")
            return response.status_code == 200
        except Exception:
            return False

    async def search(
        self,
        query: str,
        *,
        family: str | None = None,
        text_top_k: int | None = None,
        symbol_top_k: int | None = None,
        page_top_k: int | None = None,
        text_filters: dict[str, Any] | None = None,
    ) -> RagContext:
        """Query every channel at once.

        `family` (STM32F4, STM32F1 ...) is expressed as an ordinary metadata
        filter rather than a PageVault feature, so the knowledge base stays
        domain-agnostic and reusable outside this project.
        """
        if not settings.rag_enabled:
            return RagContext(query=query, available=False, warnings=["RAG disabled"])

        filters = dict(text_filters or {})
        if family:
            filters["family"] = family

        payload: dict[str, Any] = {
            "query": query,
            "text_collection": settings.rag_text_collection,
            "text_top_k": text_top_k if text_top_k is not None else settings.rag_text_top_k,
            "symbol_top_k": (
                symbol_top_k if symbol_top_k is not None else settings.rag_symbol_top_k
            ),
            "page_collection": settings.rag_page_collection,
            "page_top_k": page_top_k if page_top_k is not None else settings.rag_page_top_k,
            "expand_types": True,
            "strategy": "quota",
        }
        if filters:
            payload["text_filters"] = filters

        try:
            response = await self._client.post("/text/unified-search", json=payload)
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException:
            logger.warning("PageVault timed out after %ss", self.timeout)
            return RagContext(
                query=query,
                available=False,
                warnings=[f"knowledge base timed out after {self.timeout}s"],
            )
        except Exception as exc:  # network error, 5xx, malformed JSON
            logger.warning("PageVault query failed: %s", exc)
            return RagContext(
                query=query,
                available=False,
                warnings=[f"knowledge base unavailable: {exc}"],
            )

        return RagContext(
            query=query,
            chunks=_above_score(
                [_snippet_from_chunk(item) for item in data.get("chunks", [])],
                settings.rag_min_score,
            ),
            symbols=[_snippet_from_symbol(item) for item in data.get("symbols", [])],
            type_context=[
                _snippet_from_symbol(item, channel="type")
                for item in data.get("type_context", [])
            ],
            pages=_above_score(
                [_snippet_from_page(item) for item in data.get("pages", [])],
                settings.rag_min_score,
            ),
            identifiers=list(data.get("identifiers", [])),
            warnings=list(data.get("warnings", [])),
            available=True,
        )


@lru_cache(maxsize=1)
def get_rag_client() -> PageVaultClient:
    """Shared client (one connection pool for the whole process)."""
    return PageVaultClient()


async def close_rag_client() -> None:
    """Called from the FastAPI lifespan on shutdown."""
    if get_rag_client.cache_info().currsize:
        await get_rag_client().aclose()
        get_rag_client.cache_clear()
