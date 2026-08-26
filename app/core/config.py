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

    app_name: str = "FLY AI Service"
    app_env: str = "local"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"

    internal_api_key: str = ""
    admin_api_token: str = ""

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

    history_cleanup_enabled: bool = True
    run_retention_days: int = Field(default=90, ge=1, le=3650)
    failed_run_retention_days: int = Field(default=14, ge=1, le=3650)
    min_successful_runs_to_keep: int = Field(default=10, ge=1, le=1000)
    cleanup_schedule_hour_kst: int = Field(default=4, ge=0, le=23)
    cleanup_schedule_minute_kst: int = Field(default=30, ge=0, le=59)

    apify_api_token: str = ""
    gemini_api_key: str = ""
    youtube_api_key: str = ""
    naver_api_hub_client_id: str = ""
    naver_api_hub_client_secret: str = ""

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    shortform_openai_model: str = "gpt-5.4-mini-2026-03-17"
    shortform_request_timeout_seconds: int = Field(default=12, ge=2, le=60)
    shortform_max_output_tokens: int = Field(default=1800, ge=256, le=10000)
    shortform_max_photo_inputs: int = Field(default=4, ge=0, le=10)

    database_openai_model: str = "gpt-5.4-mini-2026-03-17"
    database_request_timeout_seconds: int = Field(default=60, ge=5, le=300)
    database_max_output_tokens: int = Field(default=6000, ge=512, le=30000)
    database_gemini_model: str = "auto"
    database_video_analysis_timeout_seconds: int = Field(default=300, ge=30, le=900)
    database_max_reference_videos: int = Field(default=5, ge=1, le=10)
    database_require_human_approval: bool = True
    database_maintenance_enabled: bool = False
    database_maintenance_weekday: int = Field(default=0, ge=0, le=6)
    database_maintenance_hour_kst: int = Field(default=5, ge=0, le=23)
    database_maintenance_minute_kst: int = Field(default=0, ge=0, le=59)

    # Editing Agent. Defaults are deliberately CPU-only friendly for the
    # current 2-vCPU / 8-GiB AI host: source frames are retained as compact
    # 360px JPEGs, then sampled into a bounded set of temporal VLM batches.
    editing_openai_model: str = "gpt-5.4-mini-2026-03-17"
    editing_request_timeout_seconds: int = Field(default=30, ge=5, le=180)
    editing_max_output_tokens: int = Field(default=5000, ge=512, le=20000)
    editing_llm_max_request_attempts: int = Field(default=3, ge=1, le=5)
    editing_rate_limit_retry_base_seconds: float = Field(default=20.0, ge=1.0, le=120.0)
    editing_max_repair_attempts: int = Field(default=2, ge=0, le=5)
    editing_max_keyframes_per_video: int = Field(default=3, ge=1, le=12)
    editing_analysis_frame_width: int = Field(default=360, ge=240, le=720)
    editing_analysis_jpeg_quality: int = Field(default=7, ge=2, le=15)
    editing_analysis_batch_frames: int = Field(default=24, ge=6, le=40)
    editing_analysis_max_frames_per_video: int = Field(default=48, ge=6, le=240)
    editing_analysis_max_total_frames: int = Field(default=120, ge=12, le=720)
    editing_orphan_recovery_enabled: bool = True
    editing_orphan_stale_seconds: int = Field(default=900, ge=60, le=14400)
    editing_orphan_recovery_interval_seconds: int = Field(default=300, ge=60, le=3600)
    editing_max_videos_per_run: int = Field(default=6, ge=1, le=20)
    editing_max_output_duration_seconds: int = Field(default=15, ge=1, le=60)
    editing_max_source_duration_seconds: int = Field(default=30, ge=1, le=300)
    editing_disabled_effect_ids: str = "SMOOTH_ZOOM"
    editing_ffprobe_path: str = "ffprobe"
    editing_ffmpeg_path: str = "ffmpeg"
    editing_probe_timeout_seconds: int = Field(default=45, ge=5, le=300)
    editing_renderer_url: str = ""
    editing_renderer_timeout_seconds: int = Field(default=1800, ge=30, le=7200)
    editing_renderer_health_timeout_seconds: int = Field(default=3, ge=1, le=30)
    editing_reals_registry_path: Path = Path("reals-video-engine/registry")

    renderer_public_base_url: str = "http://localhost:8080"
    renderer_work_dir: Path = Path("runtime-data/renderer/work")
    renderer_output_dir: Path = Path("runtime-data/renderer/output")
    renderer_max_download_bytes: int = Field(
        default=268_435_456, ge=1_048_576, le=10_737_418_240
    )
    renderer_download_timeout_seconds: int = Field(default=300, ge=10, le=3600)
    editing_reals_engine_path: Path = Path("reals-video-engine")

    @property
    def effective_internal_api_key(self) -> str:
        return (self.internal_api_key or self.admin_api_token).strip()

    @property
    def shortform_llm_ready(self) -> bool:
        return bool(self.openai_api_key.strip() and self.shortform_openai_model.strip())

    @property
    def editing_runtime_ready(self) -> bool:
        return bool(
            self.openai_api_key.strip()
            and self.editing_openai_model.strip()
            and self.editing_renderer_url.strip()
        )

    @property
    def database_knowledge_runtime(self) -> dict[str, bool]:
        return {
            "candidate_generation": bool(
                self.openai_api_key.strip() and self.database_openai_model.strip()
            ),
            "trade_area_analysis": bool(
                self.openai_api_key.strip() and self.database_openai_model.strip()
            ),
            "reference_video_analysis": bool(
                self.gemini_api_key.strip() and self.database_gemini_model.strip()
            ),
        }

    @property
    def editing_disabled_effect_ids_set(self) -> set[str]:
        return {
            effect_id.strip().upper()
            for effect_id in self.editing_disabled_effect_ids.split(",")
            if effect_id.strip()
        }

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
