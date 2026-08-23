from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Challenge Ranker API"
    app_env: str = "local"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    admin_api_token: str = "change-me-before-production"

    database_url: str = "sqlite:///./runtime-data/challenge-ranker.db"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    celery_task_always_eager: bool = False

    pipeline_config_path: Path = Path("config/pipeline.yaml")
    ranker_data_dir: Path = Path("runtime-data")
    export_dir: Path = Path("exports")
    pipeline_timeout_seconds: int = Field(default=3600, ge=60, le=14400)
    ranking_schedule_hour_kst: int = Field(default=6, ge=0, le=23)
    ranking_schedule_minute_kst: int = Field(default=0, ge=0, le=59)

    apify_api_token: str = ""
    gemini_api_key: str = ""
    youtube_api_key: str = ""
    naver_api_hub_client_id: str = ""
    naver_api_hub_client_secret: str = ""

    @property
    def required_api_key_status(self) -> dict[str, bool]:
        return {
            "apify": bool(self.apify_api_token.strip()),
            "gemini": bool(self.gemini_api_key.strip()),
            "youtube": bool(self.youtube_api_key.strip()),
            "naver_api_hub": bool(
                self.naver_api_hub_client_id.strip()
                and self.naver_api_hub_client_secret.strip()
            ),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
