from app.db.session import Base, engine
from app.models import challenge, pipeline_run, ranking_snapshot  # noqa: F401


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
