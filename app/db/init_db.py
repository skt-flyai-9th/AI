from app.db.session import Base, engine
from app.models import (  # noqa: F401
    challenge,
    editing_run,
    video_editing_db_record,
    pipeline_run,
    ranking_snapshot,
    shortform_session,
    template_update_candidate,
    template_video_analysis,
    template_knowledge_run,
    template_source,
    trade_area_analysis,
    trade_area_db_record,
)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
