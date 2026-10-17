"""Application settings.

Everything defaults to localhost. This project is never deployed — see the
"Deployment — LOCAL ONLY" section of claude.md.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "local"
    log_level: str = "INFO"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    frontend_origin: str = "http://localhost:3000"

    # bcrypt work factor. 12 is the production-grade default; the test suite
    # drops it so a run that registers dozens of users stays fast.
    bcrypt_rounds: int = 12

    jwt_secret: str = "dev-only-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 14
    password_reset_ttl_minutes: int = 30

    postgres_user: str = "gd"
    postgres_password: str = "gd_dev_password"
    postgres_db: str = "gd"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # Set directly to override the assembled Postgres URL (the test suite points
    # this at in-memory SQLite so it runs without Docker).
    database_url: str | None = None

    redis_url: str = "redis://localhost:6379/0"

    livekit_url: str = "ws://localhost:7880"
    livekit_api_key: str = "devkey"
    livekit_api_secret: str = "devsecret_local_only_change_never_shipped"

    minio_endpoint: str = "http://localhost:9000"
    minio_root_user: str = "gd_minio"
    minio_root_password: str = "gd_minio_dev_password"
    minio_bucket: str = "gd-recordings"
    # "minio" once the container is up; "filesystem" keeps everything on local
    # disk so the pipeline runs with nothing else started.
    object_store: str = "filesystem"

    # --- Speech (FR-13) ---
    # auto | elevenlabs | whisper | stub. `auto` prefers ElevenLabs Scribe when a
    # key is set, then Whisper, then the stub.
    stt_provider: str = "auto"
    whisper_model: str = "base"

    # --- ElevenLabs (practice mode voice) ---
    # Cloud vendor, kept behind the STT/TTS interfaces. Empty key = offline
    # fallbacks (Whisper/stub STT, browser speech for the personas).
    elevenlabs_api_key: str = ""
    elevenlabs_base_url: str = "https://api.elevenlabs.io"
    elevenlabs_tts_model: str = "eleven_turbo_v2_5"
    # Per-persona voice overrides as JSON, e.g. {"bhavana": "<voice_id>"}.
    persona_voices: dict[str, str] = Field(default_factory=dict)
    elevenlabs_stt_model: str = "scribe_v1"
    # auto | elevenlabs | edge | stub. `auto` uses ElevenLabs when a key is set, else the
    # browser voices; `edge` is the free Microsoft Edge neural voices (no key).
    tts_provider: str = "auto"

    # --- LLM layer (design §54) ---
    # anthropic | stub. `stub` keeps the agentic pipeline runnable offline.
    llm_provider: str = "stub"
    llm_model: str = "claude-sonnet-4-5"
    # Generic key, provider-agnostic. If unset, the adapters fall back to the
    # vendor's own conventional variable name below — someone who pastes
    # OPENAI_API_KEY into .env should not have to discover a second name.
    llm_api_key: str = ""
    openai_api_key: str = ""
    # Any OpenAI-compatible endpoint (Groq, Gemini, OpenRouter, Ollama...). Empty = OpenAI.
    openai_base_url: str = ""
    anthropic_api_key: str = ""
    # Faster model for the practice personas' short spoken lines (empty = LLM_MODEL).
    persona_llm_model: str = ""
    # Reasoning effort for models that think before answering (gpt-oss on Groq).
    # Without it they spend the whole token cap thinking and return nothing, so a
    # 150-token persona line comes back empty. low | medium | high.
    llm_reasoning_effort: str = "low"
    # Models to move to while one is rate-limited, comma-separated (same endpoint and
    # key; each model has its own per-minute bucket). e.g. openai/gpt-oss-20b.
    llm_fallback_models: str = ""
    llm_max_tokens: int = 2048
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 3
    # Max LLM requests in flight at once; 0 = unlimited. Set 1-2 on small free tiers.
    llm_max_concurrency: int = 0
    # Give simulated (AI) participants spoken voices when a TTS provider can make audio.
    sim_voices: bool = True

    # --- Orchestration engine ---
    # langgraph | native. `langgraph` runs the evaluation workflow as a
    # compiled state graph with dynamic fan-out and checkpointing; `native`
    # drives the same stages with plain asyncio. Identical agents, contracts
    # and aggregator either way.
    evaluation_engine: str = "langgraph"

    # --- Evaluation dispatch ---
    # per_dimension | per_candidate. `per_dimension` sends one call per
    # specialist covering every candidate (design §49); `per_candidate` sends
    # one call per pair. Both produce identical downstream data.
    evaluation_strategy: str = "per_dimension"
    # Specialists use evidence tools and a critic audits them (vs. one-shot prompts).
    evaluation_agentic: bool = True
    # Deterministic score penalty per interruption a candidate made, and its cap.
    interruption_penalty_points: float = 2.0
    interruption_penalty_cap: float = 15.0

    # --- Evaluation (design §40) ---
    prompt_version: str = "1.0"
    agent_version: str = "1.0"

    cors_origins: list[str] = Field(default_factory=list)

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def allowed_origins(self) -> list[str]:
        return self.cors_origins or [self.frontend_origin]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
