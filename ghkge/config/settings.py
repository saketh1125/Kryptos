from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "GHKGE_", "env_file": ".env", "env_file_encoding": "utf-8"}

    # Postgres (Supabase)
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/ghkge"

    # Neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"

    # LLM
    openai_api_key: str = ""
    openai_base_url: str = "https://openrouter.ai/api/v1"
    extraction_model: str = "google/gemini-flash-2.0"
    planner_model: str = "google/gemini-flash-2.0"

    # Embeddings
    ollama_base_url: str = "http://localhost:11434"
    embedding_model: str = "nomic-embed-text"
    embedding_dim: int = 768

    # Compliance
    user_agent: str = "HyperlocalKnowledgeGraphEngine/1.0 (+https://github.com/saketh1125/ghkge)"
    rate_limit_interval_s: float = 2.0
    robots_cache_ttl_hours: int = 24

    # Harvesting
    max_concurrent_browsers: int = 3
    max_concurrent_doc_parsers: int = 1
    chunk_size_words: int = 800
    chunk_overlap_words: int = 150

    # Entity Resolution
    entity_match_threshold: int = 85
    safety_corroboration_min: int = 2

    # Domain configuration (YAML ontology + strategies)
    domain_config_path: str = "config/default_domain.yaml"
    grid_precision: int = 6  # geohash precision for grid cells (~1.2km x ~0.6km)

    # Workers / task bus
    workers_enabled: bool = True
    worker_poll_interval_s: float = 1.0
    worker_batch_size: int = 5
    stale_task_takeover_minutes: int = 10

    # Scheduler
    scheduler_enabled: bool = True
    gap_eval_interval_hours: int = 24
    scheduled_acquisition_enabled: bool = False
    acquisition_interval_hours: int = 24
    scheduler_default_domain: str = ""

    # API
    json_logging: bool = False
    api_host: str = "0.0.0.0"
    api_port: int = 8000


settings = Settings()
