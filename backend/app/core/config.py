from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central app configuration.

    All values can be overridden via environment variables or `.env`.
    Agents must NEVER hardcode model names or URLs — always read from here,
    so switching to Ollama later is a pure `.env` change.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "STM32 Engineering AI Assistant"
    environment: str = "development"
    cors_origins: list[str] = ["http://localhost:3000"]

    # --- LLM provider (OpenAI-compatible) ---
    # Later for Ollama: LLM_BASE_URL=http://ollama:11434/v1, LLM_API_KEY=ollama
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"

    # The OpenAI SDK defaults to a 600s timeout, which is absurd for a
    # 10-token router call and turns an unreachable provider into a hung
    # HTTP request. Agents that legitimately need longer pass their own.
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2
    # Hard ceiling on request classification specifically; on timeout the
    # router falls back to deterministic keyword rules.
    router_timeout_seconds: float = 8.0

    # agent_settings rows are read on every LLM call. Cache them briefly so
    # a pipeline of ten agents does not issue ten queries per step. The
    # cache is invalidated immediately when the settings API writes.
    agent_settings_cache_ttl: float = 30.0

    # --- Embeddings (kept separate from LLM on purpose) ---
    # Decide before M2: changing the embedding model after building Qdrant
    # collections requires a full re-index (vector dimensions differ).
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"

    # --- Knowledge base (PageVault, M2) ---
    # PageVault runs as its own stack and is reached over the shared
    # `rag-net` Docker network. Moving it to a dedicated GPU machine later
    # means changing this URL and nothing else.
    pagevault_url: str = "http://pagevault-api:8000"
    rag_enabled: bool = True
    # Retrieval must not dominate an agent step; on timeout the agent
    # continues without citations rather than failing.
    rag_timeout_seconds: float = 20.0
    rag_text_collection: str = "stm32"
    rag_page_collection: str = "stm32-manuals"
    rag_text_top_k: int = 6
    rag_symbol_top_k: int = 5
    # Visual page hits cost GPU time on the PageVault side; keep small.
    rag_page_top_k: int = 2
    # Ceiling on the retrieved context injected into a prompt. Local models
    # have far smaller context windows than hosted ones.
    rag_context_max_chars: int = 12000

    # --- Infrastructure ---
    database_url: str = "postgresql+psycopg://stm32ai:stm32ai@postgres:5432/stm32ai"
    redis_url: str = "redis://redis:6379/0"
    qdrant_url: str = "http://qdrant:6333"


settings = Settings()
