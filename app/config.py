from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Every value can be overridden with an environment variable."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://eve:eve@localhost:5432/eve"

    jwt_secret: str = "change-me-in-production-use-a-random-32-byte-value"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    bcrypt_rounds: int = 12

    # Shared secret used to sign webhooks from the (simulated) payment provider.
    webhook_secret: str = "dev-webhook-secret"

    # Simulated payment provider behaviour.
    payment_success_rate: float = 0.8
    allow_simulated_outcome: bool = True

    rate_limit_enabled: bool = True

    # Redis backs the catalogue cache and the Celery broker. Unset = both are off and the API runs on the
    # database alone, which is also what the fast test run uses.
    redis_url: str | None = None
    catalogue_cache_ttl_seconds: int = 300

    # Run Celery tasks inline instead of sending them to a worker (tests, or a setup without Redis).
    celery_task_always_eager: bool = False

    log_level: str = "INFO"
    log_format: str = "json"  # "json" (one object per line) or "text"


@lru_cache
def get_settings() -> Settings:
    return Settings()
