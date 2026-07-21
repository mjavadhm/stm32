"""RAG package (M2).

Will contain: document ingestion, chunking, embeddings, Qdrant collections
and the semantic search API with chip-family/document-type filters.

IMPORTANT: fix the embedding model (EMBEDDING_MODEL in .env) before creating
Qdrant collections — changing it later requires a full re-index.
"""
