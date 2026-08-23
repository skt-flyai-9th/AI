"""initial schema

Revision ID: 20260823_0001
Revises:
Create Date: 2026-08-23
"""
from alembic import op
import sqlalchemy as sa

revision = "20260823_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("stage", sa.String(64), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("celery_task_id", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("source_status", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_pipeline_runs_status", "pipeline_runs", ["status"])
    op.create_index("ix_pipeline_runs_created_at", "pipeline_runs", ["created_at"])

    op.create_table(
        "challenges",
        sa.Column("id", sa.String(160), primary_key=True),
        sa.Column("automatic_name", sa.String(255), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("category", sa.String(80), nullable=False),
        sa.Column("automatic_rank", sa.Integer(), nullable=True),
        sa.Column("automatic_score", sa.Float(), nullable=False),
        sa.Column("lifecycle", sa.String(32), nullable=False),
        sa.Column("kr_affinity", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("automatic_representative_youtube_url", sa.Text(), nullable=True),
        sa.Column("automatic_guide_youtube_url", sa.Text(), nullable=True),
        sa.Column("representative_video_metadata", sa.JSON(), nullable=False),
        sa.Column("guide_video_metadata", sa.JSON(), nullable=False),
        sa.Column("override_rank", sa.Integer(), nullable=True),
        sa.Column("override_name", sa.String(255), nullable=True),
        sa.Column("override_representative_youtube_url", sa.Text(), nullable=True),
        sa.Column("override_guide_youtube_url", sa.Text(), nullable=True),
        sa.Column("rank_overridden", sa.Boolean(), nullable=False),
        sa.Column("name_overridden", sa.Boolean(), nullable=False),
        sa.Column("representative_video_overridden", sa.Boolean(), nullable=False),
        sa.Column("guide_video_overridden", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("latest_run_id", sa.String(36), nullable=True),
        sa.Column("raw_details", sa.JSON(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_challenges_automatic_name", "challenges", ["automatic_name"])
    op.create_index("ix_challenges_automatic_rank", "challenges", ["automatic_rank"])
    op.create_index("ix_challenges_active", "challenges", ["active"])
    op.create_index("ix_challenges_latest_run_id", "challenges", ["latest_run_id"])
    op.create_index("ix_challenges_last_seen_at", "challenges", ["last_seen_at"])

    op.create_table(
        "ranking_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("challenge_id", sa.String(160), sa.ForeignKey("challenges.id", ondelete="CASCADE"), nullable=False),
        sa.Column("automatic_rank", sa.Integer(), nullable=False),
        sa.Column("automatic_score", sa.Float(), nullable=False),
        sa.Column("row_data", sa.JSON(), nullable=False),
        sa.Column("source_metrics", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "challenge_id", name="uq_run_challenge"),
    )
    op.create_index("ix_ranking_snapshots_run_id", "ranking_snapshots", ["run_id"])
    op.create_index("ix_ranking_snapshots_challenge_id", "ranking_snapshots", ["challenge_id"])
    op.create_index("ix_ranking_snapshots_created_at", "ranking_snapshots", ["created_at"])


def downgrade() -> None:
    op.drop_table("ranking_snapshots")
    op.drop_table("challenges")
    op.drop_table("pipeline_runs")
