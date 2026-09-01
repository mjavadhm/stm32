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

Retrieval is a fan-out over PageVault's three individual endpoints -- text
search, symbol lookup, visual page search -- orchestrated here, client-side.
There is deliberately no unified endpoint and no cross-channel re-ranking:
ColBERT MaxSim scores and cosine scores are not on the same scale, so a
merged ranking would be quietly wrong. Each channel keeps its own slot and
its own score, exactly as the agents consume them.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import httpx

from app.core.config import settings
from app.rag.query_terms import extract_identifiers, referenced_types

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


def _snippet_from_symbol(
    raw: dict[str, Any],
    channel: str = "symbol",
    match: str = "",
    matched_term: str = "",
) -> Snippet:
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
            "match": match or raw.get("match", ""),
            "matched_term": matched_term,
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
        text_collection: str | None = None,
        page_collection: str | None = None,
        document_ids: list[str] | None = None,
    ) -> RagContext:
        """Query every channel, one endpoint at a time.

        `family` (STM32F4, STM32F1 ...) is expressed as an ordinary metadata
        filter on the text channel rather than a PageVault feature, so the
        knowledge base stays domain-agnostic and reusable outside this
        project.

        `text_collection` / `page_collection` override the configured
        defaults for this call -- the chat UI lets the user pick which part
        of the knowledge base to ask against. `document_ids` narrows both
        channels to specific ingested documents ("ask only this file").

        Channels are independent: one failing channel becomes a warning and
        the rest still answer. Only when every enabled channel fails is the
        knowledge base reported as unavailable.
        """
        if not settings.rag_enabled:
            return RagContext(query=query, available=False, warnings=["RAG disabled"])

        collection = text_collection or settings.rag_text_collection
        page_col = page_collection or settings.rag_page_collection

        filters = dict(text_filters or {})
        if family:
            # The chunk payload carries the family as plain metadata, and
            # this KB ingested it lowercase while our detection produces
            # uppercase. A MatchAny of case variants matches either shape
            # without the KB having to agree on a convention.
            variants: list[str] = []
            for variant in (family, family.lower(), family.upper()):
                if variant not in variants:
                    variants.append(variant)
            filters["family"] = variants
        if document_ids:
            # Qdrant MatchAny on the payload key both channels index.
            filters["document_id"] = list(document_ids)
        page_filters: dict[str, Any] = (
            {"document_id": list(document_ids)} if document_ids else {}
        )

        warnings: list[str] = []
        # (name, enabled, warning, failed) per channel. `failed` only counts
        # when the channel actually ran and its call(s) did not answer; a
        # channel disabled by top_k=0 is neither a success nor a failure.
        outcomes: list[tuple[str, bool, str | None, bool]] = []
        text_on, page_on, symbol_on = self._channel_flags(
            text_top_k, page_top_k, symbol_top_k
        )

        # Chunks and pages both cost an embedding round trip on the
        # PageVault side, so they run concurrently; the symbol channel is a
        # fast indexed lookup and runs after, because type expansion needs
        # its results.
        (chunks, chunk_warning, chunk_failed), (pages, page_warning, page_failed) = (
            await asyncio.gather(
                self._search_chunks(query, filters, text_top_k, collection),
                self._search_pages(query, page_top_k, page_col, page_filters),
            )
        )
        outcomes.append(("text", text_on, chunk_warning, chunk_failed))
        outcomes.append(("visual", page_on, page_warning, page_failed))

        identifiers = extract_identifiers(query)
        symbols, symbol_warning, symbol_failed = await self._lookup_symbols(
            identifiers, symbol_top_k, collection
        )
        # The symbol channel "runs" only when the question actually names an
        # identifier; a skip makes zero HTTP calls and must not count as the
        # knowledge base having answered.
        symbol_attempted = symbol_on and bool(identifiers)
        outcomes.append(("symbol", symbol_attempted, symbol_warning, symbol_failed))

        type_context, type_warning, _type_failed = await self._expand_types(
            symbols, chunks, collection
        )
        # Dependent channel: a failure here is a warning, never a verdict on
        # the knowledge base itself.

        for _name, _on, warning, _failed in outcomes:
            if warning:
                warnings.append(warning)
        if type_warning:
            warnings.append(type_warning)

        attempted = [name for name, on, _w, _f in outcomes if on]
        succeeded = [name for name, on, _w, failed in outcomes if on and not failed]
        available = bool(succeeded) or not attempted
        if not available:
            logger.warning("every PageVault channel failed: %s", warnings)
            return RagContext(
                query=query,
                available=False,
                warnings=warnings or ["knowledge base unavailable"],
            )

        return RagContext(
            query=query,
            chunks=chunks,
            symbols=symbols,
            type_context=type_context,
            pages=pages,
            identifiers=identifiers,
            warnings=warnings,
            available=True,
        )

    @staticmethod
    def _channel_flags(
        text_top_k: int | None, page_top_k: int | None, symbol_top_k: int | None
    ) -> tuple[bool, bool, bool]:
        """Which channels will actually run, in outcome order."""
        return (
            (text_top_k if text_top_k is not None else settings.rag_text_top_k) > 0,
            (page_top_k if page_top_k is not None else settings.rag_page_top_k) > 0,
            (symbol_top_k if symbol_top_k is not None else settings.rag_symbol_top_k) > 0,
        )

    def _channel_error(self, channel: str, exc: Exception) -> str:
        """A warning a human can act on, with the timeout called out."""
        if isinstance(exc, httpx.TimeoutException):
            return f"{channel} channel timed out after {self.timeout}s"
        return f"{channel} channel failed: {type(exc).__name__}: {exc}"

    async def _search_chunks(
        self,
        query: str,
        filters: dict[str, Any],
        top_k: int | None,
        collection: str | None = None,
    ) -> tuple[list[Snippet], str | None, bool]:
        """Documentation and code excerpts: POST /text/search (hybrid)."""
        limit = top_k if top_k is not None else settings.rag_text_top_k
        if limit <= 0:
            return [], None, False
        try:
            response = await self._client.post(
                "/text/search",
                json={
                    "query": query,
                    "collection": collection or settings.rag_text_collection,
                    "top_k": limit,
                    "filters": filters or None,
                    "mode": "hybrid",
                },
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning("text channel failed: %s", exc)
            return [], self._channel_error("text", exc), True
        return (
            _above_score(
                [_snippet_from_chunk(item) for item in data.get("results", [])],
                settings.rag_min_score,
            ),
            None,
            False,
        )

    async def _search_pages(
        self,
        query: str,
        top_k: int | None,
        collection: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> tuple[list[Snippet], str | None, bool]:
        """Reference-manual pages (visual hits): POST /search."""
        limit = top_k if top_k is not None else settings.rag_page_top_k
        if limit <= 0:
            return [], None, False
        try:
            response = await self._client.post(
                "/search",
                json={
                    "query": query,
                    "collection": collection or settings.rag_page_collection,
                    "top_k": limit,
                    "filters": filters or {},
                },
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning("visual channel failed: %s", exc)
            return [], self._channel_error("visual", exc), True
        return (
            _above_score(
                [_snippet_from_page(item) for item in data.get("results", [])],
                settings.rag_min_score,
            ),
            None,
            False,
        )

    async def _lookup_symbols(
        self,
        identifiers: list[str],
        top_k: int | None,
        collection: str | None = None,
    ) -> tuple[list[Snippet], str | None, bool]:
        """Exact API definitions for the identifiers named in the question.

        The budget is shared across identifiers rather than granted to each,
        so a three-identifier question does not flood the prompt. Only the
        exact and prefix tiers are accepted -- the substring tier is noise
        when the caller never typed the term.
        """
        limit = top_k if top_k is not None else settings.rag_symbol_top_k
        if limit <= 0:
            return [], None, False
        if not identifiers:
            # Not a failure: the question simply names no identifier. An
            # empty symbol slot is better than substring noise.
            return [], "symbol channel skipped: no C-like identifier found in the query", False

        out: list[Snippet] = []
        seen: set[tuple[str, str]] = set()
        for term in identifiers:
            if len(out) >= limit:
                break
            try:
                response = await self._client.get(
                    "/text/symbols",
                    params={
                        "q": term,
                        "collection": collection or settings.rag_text_collection,
                        "limit": limit - len(out),
                    },
                )
                response.raise_for_status()
                data = response.json()
            except Exception as exc:
                logger.warning("symbol channel failed for %s: %s", term, exc)
                return [], self._channel_error("symbol", exc), True
            if data.get("match") not in ("exact", "prefix"):
                continue
            for raw in data.get("results", []):
                identity = (raw.get("name", ""), raw.get("path", ""))
                if identity in seen:
                    continue
                seen.add(identity)
                out.append(
                    _snippet_from_symbol(raw, match=data["match"], matched_term=term)
                )
                if len(out) >= limit:
                    break
        return out, None, False

    async def _expand_types(
        self,
        symbols: list[Snippet],
        chunks: list[Snippet],
        collection: str | None = None,
    ) -> tuple[list[Snippet], str | None, bool]:
        """Definitions of the types the matched signatures depend on.

        A generator handed HAL_SPI_TransmitReceive_DMA but not
        SPI_HandleTypeDef writes a call it cannot type-check. Those
        definitions are already in the symbol table, so pulling them in
        costs one indexed lookup per type and closes a real gap.
        """
        limit = settings.rag_type_top_k
        if limit <= 0 or not (symbols or chunks):
            return [], None, False

        already = {symbol.name for symbol in symbols}
        wanted: list[str] = []
        for signature in [s.signature for s in symbols] + [
            c.signature for c in chunks if c.signature
        ]:
            for type_name in referenced_types(signature):
                if type_name not in already and type_name not in wanted:
                    wanted.append(type_name)

        out: list[Snippet] = []
        for type_name in wanted:
            if len(out) >= limit:
                break
            try:
                response = await self._client.get(
                    "/text/symbols",
                    params={
                        "q": type_name,
                        "collection": collection or settings.rag_text_collection,
                        "limit": 1,
                    },
                )
                response.raise_for_status()
                data = response.json()
            except Exception as exc:
                logger.warning("type expansion failed for %s: %s", type_name, exc)
                return out, self._channel_error("type expansion", exc), True
            # Only an exact hit is context; a prefix hit is a different type.
            if data.get("match") != "exact":
                continue
            results = data.get("results", [])
            if results:
                out.append(_snippet_from_symbol(results[0], channel="type"))
        return out, None, False

    # ------------------------------------------------------------------
    # Knowledge-base browsing: what can the user choose to ask against?
    # These power the collection/document selectors in the chat UI.
    # ------------------------------------------------------------------

    async def list_text_collections(self) -> tuple[list[dict], str | None]:
        """Collections of code/docs chunks: GET /text/collections."""
        try:
            response = await self._client.get("/text/collections")
            response.raise_for_status()
            rows = response.json()
        except Exception as exc:
            logger.warning("listing text collections failed: %s", exc)
            return [], self._channel_error("collections", exc)
        return [
            {
                "name": row.get("name", ""),
                "document_count": row.get("document_count", 0),
                "chunk_count": row.get("chunk_count", 0),
                "symbol_count": row.get("symbol_count", 0),
            }
            for row in rows
        ], None

    async def list_visual_collections(self) -> tuple[list[dict], str | None]:
        """Collections of indexed PDF pages: GET /collections."""
        try:
            response = await self._client.get("/collections")
            response.raise_for_status()
            rows = response.json()
        except Exception as exc:
            logger.warning("listing visual collections failed: %s", exc)
            return [], self._channel_error("collections", exc)
        return [
            {
                "name": row.get("name", ""),
                "document_count": row.get("document_count", 0),
            }
            for row in rows
        ], None

    async def list_documents(
        self, collection: str, *, visual: bool = False
    ) -> tuple[list[dict], str | None]:
        """Documents of one collection: the "parts" a user can narrow to.

        Text documents come from GET /text/documents (path, chunk and
        symbol counts); visual ones from GET /documents?type=pdf (filename,
        page count). Only indexed documents are returned -- a failed or
        pending ingest has nothing to ask against.
        """
        try:
            if visual:
                response = await self._client.get(
                    "/documents",
                    params={"collection": collection, "type": "pdf"},
                )
                response.raise_for_status()
                rows = response.json()
                return [
                    {
                        "id": row.get("id", ""),
                        "path": row.get("filename", ""),
                        "status": row.get("status", ""),
                        "pages": row.get("page_count"),
                    }
                    for row in rows
                    if row.get("status") == "indexed"
                ], None
            response = await self._client.get(
                "/text/documents", params={"collection": collection, "limit": 500}
            )
            response.raise_for_status()
            rows = response.json()
            return [
                {
                    "id": row.get("id", ""),
                    "path": row.get("path", ""),
                    "status": row.get("status", ""),
                    "chunks": row.get("chunk_count", 0),
                    "symbols": row.get("symbol_count", 0),
                }
                for row in rows
                if row.get("status") == "indexed"
            ], None
        except Exception as exc:
            logger.warning("listing documents failed: %s", exc)
            return [], self._channel_error("documents", exc)


@lru_cache(maxsize=1)
def get_rag_client() -> PageVaultClient:
    """Shared client (one connection pool for the whole process)."""
    return PageVaultClient()


async def close_rag_client() -> None:
    """Called from the FastAPI lifespan on shutdown."""
    if get_rag_client.cache_info().currsize:
        await get_rag_client().aclose()
        get_rag_client.cache_clear()
