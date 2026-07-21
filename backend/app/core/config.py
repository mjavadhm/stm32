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

    # --- Embeddings (kept separate from LLM on purpose) ---
    # Decide before M2: changing the embedding model after building Qdrant
    # collections requires a full re-index (vector dimensions differ).
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"

    # --- Infrastructure ---
    database_url: str = "postgresql+psycopg://stm32ai:stm32ai@postgres:5432/stm32ai"
    redis_url: str = "redis://redis:6379/0"
    qdrant_url: str = "http://qdrant:6333"


settings = Settings()
