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

    # Ceiling on generated tokens. 0 = leave it to the provider. Set it when
    # running locally: a runaway generation costs minutes, not cents.
    llm_max_tokens: int = 0
    # A contract reply that fails validation is retried this many times, with
    # the parser error fed back to the model, before the agent degrades
    # (docs/architecture.md decision #3).
    llm_contract_retries: int = 1

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
    # Drop retrieved text/page snippets below this score. 0 = keep everything;
    # scores are not comparable across collections until you calibrate them,
    # so this stays off until an eval run tells you where the noise floor is.
    rag_min_score: float = 0.0

    # --- Agentic document chat (agentic RAG) ---
    # The chat agent plans its own retrieval: per question it may run at most
    # this many searches before it has to answer with what it has collected.
    chat_max_searches: int = 3

    # --- Build sandbox (M4) ---
    # Compilation happens in a separate container that has no route to the
    # internet, the database, or this API. The backend and the builder share
    # the `workspaces` volume, so only a path crosses the wire, never source.
    builder_url: str = "http://builder:9000"
    build_timeout_seconds: float = 120.0
    workspace_root: str = "/workspaces"
    # ST's HAL and CMSIS sources. Downloaded into the build image (never
    # committed to git) and mounted here read-only, so the scaffold can copy
    # the drivers a project needs into the project itself -- the way
    # STM32CubeMX does, and the reason a downloaded zip compiles on a machine
    # that has never heard of this stack.
    cube_sdk_root: str = "/opt/stm32cube/f4"
    # Which pin can carry which signal, at which alternate-function number.
    # The one hardware fact ST does not ship with the HAL, downloaded into
    # the build image next to the drivers and read from the same volume.
    device_xml_root: str = "/opt/stm32cube/modm-devices"
    # The compact tables converted from it by `make devices`, plus anything
    # imported later from the admin panel. Writable, and on its own volume so
    # an imported chip survives an image rebuild.
    device_data_root: str = "/var/lib/stm32ai/devices"

    # --- Infrastructure ---
    database_url: str = "postgresql+psycopg://stm32ai:stm32ai@postgres:5432/stm32ai"
    redis_url: str = "redis://redis:6379/0"
    qdrant_url: str = "http://qdrant:6333"


settings = Settings()
