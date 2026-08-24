"""shortform agent tables

Revision ID: 20260824_0002
Revises: 20260823_0001
Create Date: 2026-08-24
"""
from alembic import op
import sqlalchemy as sa

revision = "20260824_0002"
down_revision = "20260823_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "editing_templates",
        sa.Column("template_id", sa.String(length=160), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("recommendation_title", sa.String(length=255), nullable=False),
        sa.Column("recommendation_concept", sa.Text(), nullable=False),
        sa.Column("recommendation_metadata", sa.JSON(), nullable=False),
        sa.Column("shooting_guide", sa.JSON(), nullable=False),
        sa.Column("editing_rules", sa.JSON(), nullable=False),
        sa.Column("trend_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("template_id", "version"),
    )
    op.create_index(
        op.f("ix_editing_templates_status"),
        "editing_templates",
        ["status"],
        unique=False,
    )

    op.create_table(
        "shortform_sessions",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("store_id", sa.String(length=160), nullable=False),
        sa.Column("store_context", sa.JSON(), nullable=False),
        sa.Column("project_state", sa.JSON(), nullable=False),
        sa.Column("conversation", sa.JSON(), nullable=False),
        sa.Column("shown_template_ids", sa.JSON(), nullable=False),
        sa.Column("current_recommendation", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_shortform_sessions_created_at"),
        "shortform_sessions",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_shortform_sessions_status"),
        "shortform_sessions",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_shortform_sessions_store_id"),
        "shortform_sessions",
        ["store_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_shortform_sessions_store_id"), table_name="shortform_sessions")
    op.drop_index(op.f("ix_shortform_sessions_status"), table_name="shortform_sessions")
    op.drop_index(op.f("ix_shortform_sessions_created_at"), table_name="shortform_sessions")
    op.drop_table("shortform_sessions")
    op.drop_index(op.f("ix_editing_templates_status"), table_name="editing_templates")
    op.drop_table("editing_templates")
