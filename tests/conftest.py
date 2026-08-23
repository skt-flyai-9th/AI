from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "sqlite:///./runtime-data/test.db")
os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "true")
os.environ.setdefault("INTERNAL_API_KEY", "test-token")
os.environ.setdefault("ADMIN_API_TOKEN", "")

import pytest
from fastapi.testclient import TestClient

from app.db.session import Base, engine
from app.main import app


@pytest.fixture(autouse=True)
def clean_db():
    Path("runtime-data").mkdir(exist_ok=True)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-Internal-API-Key": "test-token"}


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client
