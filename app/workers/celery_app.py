from __future__ import annotations

from typing import Any

from app.core.config import get_settings

settings = get_settings()

try:
    from celery import Celery
except ImportError:  # pragma: no cover - minimal local/test environments
    Celery = None  # type: ignore[assignment]


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
        worker_prefetch_multiplier=1,
        task_time_limit=settings.pipeline_timeout_seconds,
        task_soft_time_limit=max(60, settings.pipeline_timeout_seconds - 30),
        task_always_eager=settings.celery_task_always_eager,
        task_eager_propagates=True,
        timezone="Asia/Seoul",
        enable_utc=True,
        # Recurring automation is intentionally disabled until its product
        # policy, approval flow, and failure handling are redesigned.
        beat_schedule={},
    )
