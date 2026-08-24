from app.db.session import Base, engine
from app.models import (  # noqa: F401
    challenge,
    editing_run,
    editing_template,
    pipeline_run,
    ranking_snapshot,
    shortform_session,
    template_update_candidate,
    template_video_analysis,
    template_knowledge_run,
    trade_area_analysis,
    trade_area_template,
)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
