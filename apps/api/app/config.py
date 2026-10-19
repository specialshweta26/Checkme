from __future__ import annotations

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../../.env"), extra="ignore")

    public_web_url: str = "http://localhost:3000"

    openai_api_key: str = ""
    openai_model: str = "gpt-5.6-luna"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_reasoning_effort: str = "none"
    embedding_dims: int = 1536

    google_client_id: str = ""
    google_client_secret: str = ""
    microsoft_client_id: str = Field(default="", validation_alias=AliasChoices("MICROSOFT_CLIENT_ID", "OUTLOOK_CLIENT_ID"))
    microsoft_client_secret: str = Field(
        default="", validation_alias=AliasChoices("MICROSOFT_CLIENT_SECRET", "OUTLOOK_CLIENT_SECRET")
    )
    microsoft_tenant: str = "common"

    session_secret: str = "dev-only-change-me"
    token_encryption_key: str = ""
    session_ttl_days: int = 14

    database_url: str = "postgresql+asyncpg://assistant:assistant@localhost:5432/assistant"
    database_hostport: str = ""
    postgres_user: str = "assistant"
    postgres_password: str = ""
    postgres_db: str = "assistant"
    redis_url: str = "redis://localhost:6379/0"

    letta_base_url: str = ""
    letta_api_key: str = ""
    letta_agent_model: str = ""
    letta_embedding: str = "openai/text-embedding-3-small"

    file_storage: str = "s3"
    storage_disk_path: str = "/var/data/attachments"
    s3_endpoint_url: str = "http://localhost:9000"
    s3_public_endpoint_url: str = "http://localhost:9000"
    s3_bucket: str = "attachments"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    attachment_max_bytes: int = 25 * 1024 * 1024

    @property
    def use_disk_storage(self) -> bool:
        return (self.file_storage or "").strip().lower() in {"disk", "local", "fs", "filesystem"}

    allow_private_mcp_urls: bool = False

    # Routing knobs, tuned by the routing eval.
    route_margin: float = 0.08
    max_tool_schemas: int = 12
    chat_rate_limit_per_minute: int = 30
    history_messages: int = 8
    max_agent_steps: int = 12

    @property
    def secure_cookies(self) -> bool:
        return self.public_web_url.startswith("https://")

    @property
    def async_database_url(self) -> str:
        if self.database_hostport and self.postgres_password:
            user = quote_plus(self.postgres_user)
            password = quote_plus(self.postgres_password)
            return f"postgresql+asyncpg://{user}:{password}@{self.database_hostport}/{self.postgres_db}"
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        if url.startswith("postgresql://") and "+asyncpg" not in url:
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
