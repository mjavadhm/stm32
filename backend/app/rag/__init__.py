"""Knowledge-base access (M2).

Retrieval is provided by PageVault, a separate open-source service that runs
in its own Docker stack and is reached over HTTP. This package holds only the
client: no ingestion, no chunking, no Qdrant code lives here.

    from app.rag import get_rag_client

    context = await get_rag_client().search("DMA registers on STM32F407")
    prompt = context.as_prompt()

Running the two stacks: see `docs/knowledge-base.md`.
"""

from app.rag.client import (
    PageVaultClient,
    RagContext,
    Snippet,
    close_rag_client,
    get_rag_client,
)

__all__ = [
    "PageVaultClient",
    "RagContext",
    "Snippet",
    "close_rag_client",
    "get_rag_client",
]
