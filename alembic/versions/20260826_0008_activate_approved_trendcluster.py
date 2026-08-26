"""activate and rank the three approved trendcluster rows

Revision ID: 20260826_0008
Revises: 20260826_0007
Create Date: 2026-08-26
"""

from alembic import op

revision = "20260826_0008"
down_revision = "20260826_0007"
branch_labels = None
depends_on = None

_APPROVED_RANKS = {
    "jujutsu_transition": 1,
    "cafe_recommendation_reels": 2,
    "otsukare_summer_challenge": 3,
}


def upgrade() -> None:
    for challenge_id, rank in _APPROVED_RANKS.items():
        op.execute(
            "UPDATE challenges "
            f"SET active = TRUE, automatic_rank = {rank} "
            f"WHERE id = '{challenge_id}'"
        )


def downgrade() -> None:
    pass
