from __future__ import annotations

from typing import Any

from app.core.config import get_settings

settings = get_settings()

try:
    from celery import Celery
    from celery.schedules import crontab
except ImportError:  # pragma: no cover - minimal local/test environments
    Celery = None  # type: ignore[assignment]
    crontab = None  # type: ignore[assignment]


class _UnavailableCelery:
    conf: dict[str, Any] = {}

    def task(self, *args: Any, **kwargs: Any):
        def decorator(func):
            return func

        return decorator


if Celery is None:
    celery_app = _UnavailableCelery()
else:
    celery_app = Celery(
        "challenge_ranker",
        broker=settings.celery_broker_url,
        backend=settings.celery_result_backend,
        include=["app.workers.tasks"],
    )
    celery_app.conf.update(
        task_track_started=True,
        task_time_limit=settings.pipeline_timeout_seconds,
        task_soft_time_limit=max(60, settings.pipeline_timeout_seconds - 30),
        task_always_eager=settings.celery_task_always_eager,
        task_eager_propagates=True,
        timezone="Asia/Seoul",
        enable_utc=True,
        beat_schedule={
            "daily-challenge-ranking": {
                "task": "app.workers.tasks.run_ranking_pipeline",
                "schedule": crontab(
                    hour=settings.ranking_schedule_hour_kst,
                    minute=settings.ranking_schedule_minute_kst,
                ),
                "args": (),
            },
            "daily-history-cleanup": {
                "task": "app.workers.tasks.cleanup_history",
                "schedule": crontab(
                    hour=settings.cleanup_schedule_hour_kst,
                    minute=settings.cleanup_schedule_minute_kst,
                ),
                "args": (),
            },
            "weekly-database-maintenance": {
                "task": "app.workers.tasks.run_database_maintenance",
                "schedule": crontab(
                    day_of_week=settings.database_maintenance_weekday,
                    hour=settings.database_maintenance_hour_kst,
                    minute=settings.database_maintenance_minute_kst,
                ),
                "args": (),
            },
        },
    )
