"""prune trendcluster to the three approved video guides

Revision ID: 20260826_0007
Revises: 20260824_0006
Create Date: 2026-08-26
"""

from alembic import op

revision = "20260826_0007"
down_revision = "20260824_0006"
branch_labels = None
depends_on = None

_APPROVED_CHALLENGE_IDS = (
    "jujutsu_transition",
    "cafe_recommendation_reels",
    "otsukare_summer_challenge",
)


def upgrade() -> None:
    placeholders = ", ".join(f"'{challenge_id}'" for challenge_id in _APPROVED_CHALLENGE_IDS)
    op.execute(f"DELETE FROM challenges WHERE id NOT IN ({placeholders})")


def downgrade() -> None:
    # 삭제한 자동 발굴 결과는 소스 파이프라인에서 다시 만들 수 있지만, 정확한 과거
    # 스냅샷을 추측해서 복원하지는 않는다.
    pass
