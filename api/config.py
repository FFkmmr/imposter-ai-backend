from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    api_key: str
    admin_api_key: str

    openai_api_key: str
    openai_model: str = "gpt-4.1-mini"

    postgres_host: str = "db"
    postgres_port: int = 5432
    postgres_db: str = "imposter_ai"
    postgres_user: str = "imposter"
    postgres_password: str = "changeme"

    jwt_private_key_path: str = "./keys/private.pem"
    jwt_public_key_path: str = "./keys/public.pem"
    jwt_algorithm: str = "RS256"
    jwt_expire_days: int = 365

    redis_host: str = "redis"
    redis_port: int = 6379

    adapty_webhook_secret: str = ""

    # === Subscription tiers (Adapty webhook token economy, ADR-002) ===
    # SKU маппинг vendor_product_id → грант токенов. product_id по умолчанию пустые
    # (должны быть заданы в проде), гранты имеют разумные дефолты.
    subscription_product_weekly: str = ""
    subscription_product_yearly: str = ""
    subscription_tokens_weekly: int = 100
    subscription_tokens_yearly: int = 1500
    subscription_tokens_grant: int = 50

    # === AI token spend (ADR-003) ===
    # Стоимость одной фактической выдачи AI-слова в POST /ai/generate-theme.
    ai_theme_token_cost: int = 1

    debug: bool = False
    log_level: str = "INFO"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def sync_database_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    class Config:
        env_file = ".env"


@lru_cache
def get_settings() -> Settings:
    return Settings()
