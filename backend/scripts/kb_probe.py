"""Prove the knowledge base is reachable *and* filterable.

`make kb-check` pings /health, which answers "is PageVault up?" -- not "does a
real query come back with anything?". The failure this catches is the quiet
one: if chunks were indexed without a `family` field, every filtered search
returns zero results, every agent falls back to model memory, and the health
check stays green the whole time.

Usage (inside the backend container):
    python -m scripts.kb_probe "SPI DMA transfer" STM32F4

Exit code 0 = healthy, 1 = something is wrong. Safe to run in CI.
"""

import asyncio
import sys
from typing import Any

from app.core.config import settings
from app.rag import close_rag_client, get_rag_client

DEFAULT_QUERY = "SPI with DMA transfer configuration"
DEFAULT_FAMILY = "STM32F4"


def _counts(context: Any) -> dict[str, int]:
    return {
        "symbols": len(context.symbols),
        "types": len(context.type_context),
        "chunks": len(context.chunks),
        "pages": len(context.pages),
    }


def _report(label: str, context: Any) -> int:
    counts = _counts(context)
    total = sum(counts.values())
    detail = "  ".join(f"{name}={value}" for name, value in counts.items())
    print(f"  {label:<22} total={total:<4} {detail}")
    for warning in context.warnings:
        print(f"      warning: {warning}")
    return total


async def probe(query: str, family: str) -> int:
    print(f"query   : {query!r}")
    print(f"family  : {family}")
    print(f"backend : {settings.pagevault_url}")

    if not settings.rag_enabled:
        print("\nFAIL  RAG_ENABLED=false -- agents run without retrieval.")
        return 1

    client = get_rag_client()
    try:
        try:
            health = await client.health()
        except Exception as exc:  # container down, wrong URL, no network
            print(f"\nFAIL  PageVault unreachable: {exc}")
            return 1
        print(f"health  : {health}\n")

        unfiltered = await client.search(query)
        filtered = await client.search(query, family=family)
    finally:
        await close_rag_client()

    total_unfiltered = _report("without family filter", unfiltered)
    total_filtered = _report(f"with family={family}", filtered)

    if total_unfiltered == 0 and total_filtered == 0:
        print(
            "\nFAIL  The knowledge base returned nothing at all.\n"
            "      Either the collections are empty or the names in\n"
            f"      RAG_TEXT_COLLECTION={settings.rag_text_collection!r} / "
            f"RAG_PAGE_COLLECTION={settings.rag_page_collection!r}\n"
            "      do not match what PageVault indexed. Re-run ingestion."
        )
        return 1

    if total_filtered == 0:
        print(
            "\nFAIL  Unfiltered search works, the family filter kills it.\n"
            "      The indexed chunks carry no `family` metadata, so every\n"
            "      agent query silently retrieves nothing. Either re-index\n"
            "      with the family field or drop the filter in\n"
            "      app/rag/client.py::search."
        )
        return 1

    print("\nOK    Filtered retrieval works. Sample citations:")
    for citation in filtered.citations()[:5]:
        print(f"      - {citation}")
    if not filtered.citations():
        print("      (none -- hits carry no path/line metadata to cite)")
    return 0


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUERY
    family = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_FAMILY
    return asyncio.run(probe(query, family))


if __name__ == "__main__":
    raise SystemExit(main())
