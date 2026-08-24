from app.db.session import Base, engine
from app.models import (  # noqa: F401
    challenge,
    editing_run,
    editing_template,
    pipeline_run,
    ranking_snapshot,
    shortform_session,
)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
