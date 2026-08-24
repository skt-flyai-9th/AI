"""editing agent runs

Revision ID: 20260824_0003
Revises: 20260824_0002
Create Date: 2026-08-24
"""
from alembic import op
import sqlalchemy as sa

revision = "20260824_0003"
down_revision = "20260824_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "editing_runs",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("parent_run_id", sa.String(length=48), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("celery_task_id", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("request_snapshot", sa.JSON(), nullable=False),
        sa.Column("video_context", sa.JSON(), nullable=False),
        sa.Column("recipe", sa.JSON(), nullable=True),
        sa.Column("render_result", sa.JSON(), nullable=True),
        sa.Column("publishing_result", sa.JSON(), nullable=True),
        sa.Column("missing_scene_roles", sa.JSON(), nullable=False),
        sa.Column("available_options", sa.JSON(), nullable=False),
        sa.Column("revision_action", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["parent_run_id"], ["editing_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_editing_runs_created_at"), "editing_runs", ["created_at"])
    op.create_index(op.f("ix_editing_runs_parent_run_id"), "editing_runs", ["parent_run_id"])
    op.create_index(op.f("ix_editing_runs_status"), "editing_runs", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_editing_runs_status"), table_name="editing_runs")
    op.drop_index(op.f("ix_editing_runs_parent_run_id"), table_name="editing_runs")
    op.drop_index(op.f("ix_editing_runs_created_at"), table_name="editing_runs")
    op.drop_table("editing_runs")
