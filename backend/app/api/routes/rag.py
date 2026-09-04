"""Knowledge-base endpoints (PageVault).

These exist so retrieval can be exercised and debugged on its own, without
running a whole pipeline -- which is what you want while tuning chunking,
collections and filters. The collections/documents listings also power the
chat UI's scope selectors ("ask against this collection, or only this
document").
"""

import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.agents.datasheet import answer_hardware_question, detect_family
from app.core.config import settings
from app.rag import get_rag_client

router = APIRouter(prefix="/rag", tags=["rag"])


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    family: str | None = None
    text_top_k: int | None = Field(default=None, ge=1, le=50)
    symbol_top_k: int | None = Field(default=None, ge=0, le=50)
    page_top_k: int | None = Field(default=None, ge=0, le=20)
    # Scope overrides: which part of the knowledge base to search.
    text_collection: str | None = None
    page_collection: str | None = None
    document_ids: list[str] | None = Field(default=None, max_length=20)


class AskRequest(BaseModel):
    question: str = Field(min_length=1)
    family: str | None = None


@router.get("/health")
async def rag_health() -> dict:
    """Is the knowledge base reachable? Never raises."""
    reachable = await get_rag_client().health()
    return {
        "enabled": settings.rag_enabled,
        "url": settings.pagevault_url,
        "reachable": reachable,
        "text_collection": settings.rag_text_collection,
        "page_collection": settings.rag_page_collection,
    }


@router.post("/search")
async def rag_search(payload: SearchRequest) -> dict:
    """Raw retrieval, no LLM. Use this to judge whether recall is the problem."""
    context = await get_rag_client().search(
        payload.query,
        family=payload.family or detect_family(payload.query),
        text_top_k=payload.text_top_k,
        symbol_top_k=payload.symbol_top_k,
        page_top_k=payload.page_top_k,
        text_collection=payload.text_collection,
        page_collection=payload.page_collection,
        document_ids=payload.document_ids,
    )
    return {
        "query": context.query,
        "available": context.available,
        "identifiers": context.identifiers,
        "citations": context.citations(),
        "symbols": [
            {
                "name": s.name,
                "signature": s.signature,
                "citation": s.citation,
                "match": s.metadata.get("match", ""),
            }
            for s in context.symbols
        ],
        "types": [
            {"name": s.name, "signature": s.signature, "citation": s.citation}
            for s in context.type_context
        ],
        "chunks": [
            {
                "name": s.name,
                "citation": s.citation,
                "score": s.score,
                "text": s.text[:600],
            }
            for s in context.chunks
        ],
        "pages": [
            {"citation": s.citation, "score": s.score, "image_url": s.metadata.get("image_url")}
            for s in context.pages
        ],
        "warnings": context.warnings,
    }


@router.post("/ask")
async def rag_ask(payload: AskRequest) -> dict:
    """Datasheet Agent: retrieval + a cited answer."""
    return await answer_hardware_question(payload.question, family=payload.family)


@router.get("/collections")
async def rag_collections() -> dict:
    """What can the user choose to ask against? Never raises."""
    rag = get_rag_client()
    text, text_warning = await rag.list_text_collections()
    visual, visual_warning = await rag.list_visual_collections()
    warnings = [w for w in (text_warning, visual_warning) if w]
    return {
        "available": not warnings,
        # The configured defaults, so the UI can label its "default" option.
        "default_text_collection": settings.rag_text_collection,
        "default_page_collection": settings.rag_page_collection,
        "text": text,
        "visual": visual,
        "warnings": warnings,
    }


# A visual citation is `<document_id>#p<page>`; a text one is `path:a-b`.
_PAGE_CITATION_RE = re.compile(r"^(?P<document>[^#\s]+)#p(?P<page>\d+)$")


@router.get("/source")
async def rag_source(
    citation: str = Query(min_length=1, description="path:line_start-line_end or id#pN"),
    collection: str | None = None,
) -> dict:
    """The source behind a citation, so an answer can be checked.

    Two citation shapes exist, because two channels produce them:

    * `path:start-end` -- a text chunk. The document is found by path in the
      collection, then the chunks overlapping that line range are returned:
      exactly the excerpt the model was shown.
    * `<document_id>#p<page>` -- a reference-manual page. There is no text to
      return (the model was shown an image), so the answer is a pointer to
      the proxied page image.
    """
    page_match = _PAGE_CITATION_RE.match(citation.strip())
    if page_match:
        document_id = page_match["document"]
        page = int(page_match["page"])
        return {
            "citation": citation,
            "kind": "page",
            "collection": collection or settings.rag_page_collection,
            "document_id": document_id,
            "path": f"{document_id} · page {page}",
            "page": page,
            "lines": None,
            # Relative on purpose: the browser already talks to this API and
            # cannot reach PageVault directly.
            "image_url": f"/rag/page?document_id={quote(document_id)}&page={page}",
            "chunks": [],
        }

    path, _, lines = citation.rpartition(":")
    start = end = 0
    if path and "-" in lines:
        first, _, last = lines.partition("-")
        if first.isdigit() and last.isdigit():
            start, end = int(first), int(last)
    if not start:
        # No parsable line range: treat the whole citation as a path.
        path, start, end = citation, 0, 0

    rag = get_rag_client()
    target = collection or settings.rag_text_collection
    documents, warning = await rag.list_documents(target)
    if warning:
        raise HTTPException(status_code=503, detail=warning)

    document = next((d for d in documents if d.get("path") == path), None)
    if document is None:
        raise HTTPException(
            status_code=404, detail=f"no indexed document with path {path!r}"
        )

    chunks, warning = await rag.get_document_chunks(document["id"])
    if warning:
        raise HTTPException(status_code=503, detail=warning)

    def overlaps(chunk: dict) -> bool:
        chunk_lines = chunk.get("lines") or [0, 0]
        if not start:
            return True
        return not (chunk_lines[1] < start or chunk_lines[0] > end)

    matched = [c for c in chunks if overlaps(c)]
    return {
        "citation": citation,
        "kind": "text",
        "collection": target,
        "document_id": document["id"],
        "path": path,
        "lines": [start, end] if start else None,
        "chunks": [
            {
                "name": c.get("name", ""),
                "kind": c.get("kind", ""),
                "lines": c.get("lines"),
                "text": c.get("text", ""),
            }
            for c in matched
        ],
    }


@router.get("/page")
async def rag_page(
    # Constrained rather than free-form: this value is interpolated into a
    # PageVault URL path, and a `../` in it would let a caller aim this
    # proxy at any endpoint of an internal service.
    document_id: str = Query(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._-]+$"),
    page: int = Query(ge=1, le=10000),
) -> Response:
    """Proxy one indexed PDF page image (the browser cannot reach PageVault)."""
    content, media_type, warning = await get_rag_client().fetch_page_image(
        document_id, page
    )
    if content is None:
        raise HTTPException(status_code=503, detail=warning or "page unavailable")
    return Response(
        content=content,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/documents")
async def rag_documents(
    collection: str = Query(min_length=1),
    kind: str = Query(default="text", pattern="^(text|visual)$"),
) -> dict:
    """Documents ("parts") of one collection, for the chat UI's scope selector."""
    documents, warning = await get_rag_client().list_documents(
        collection, visual=(kind == "visual")
    )
    documents.sort(key=lambda d: d.get("path", ""))
    return {
        "available": warning is None,
        "collection": collection,
        "kind": kind,
        "documents": documents,
        "warning": warning,
    }
