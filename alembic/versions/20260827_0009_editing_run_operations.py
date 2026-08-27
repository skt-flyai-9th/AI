"""add bounded recovery, checkpoints, and LLM usage to editing runs

Revision ID: 20260827_0009
Revises: 20260826_0008
Create Date: 2026-08-27
"""

import sqlalchemy as sa
from alembic import op


revision = "20260827_0009"
down_revision = "20260826_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("editing_runs", sa.Column("recovery_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("editing_runs", sa.Column("stage_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("editing_runs", sa.Column("llm_request_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("editing_runs", sa.Column("llm_input_tokens", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("editing_runs", sa.Column("llm_output_tokens", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("editing_runs", sa.Column("llm_estimated_cost_usd", sa.Float(), nullable=False, server_default="0"))
    op.add_column("editing_runs", sa.Column("analysis_checkpoint", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("editing_runs", "analysis_checkpoint")
    op.drop_column("editing_runs", "llm_estimated_cost_usd")
    op.drop_column("editing_runs", "llm_output_tokens")
    op.drop_column("editing_runs", "llm_input_tokens")
    op.drop_column("editing_runs", "llm_request_count")
    op.drop_column("editing_runs", "stage_started_at")
    op.drop_column("editing_runs", "recovery_attempts")
