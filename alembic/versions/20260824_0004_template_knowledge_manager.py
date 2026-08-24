"""template knowledge manager

Revision ID: 20260824_0004
Revises: 20260824_0003
Create Date: 2026-08-24
"""
from alembic import op
import sqlalchemy as sa

revision = "20260824_0004"
down_revision = "20260824_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "editing_templates",
        sa.Column("evidence_summary", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "editing_templates",
        sa.Column("source_candidate_id", sa.String(length=48), nullable=True),
    )
    op.add_column(
        "editing_templates",
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f("ix_editing_templates_source_candidate_id"),
        "editing_templates",
        ["source_candidate_id"],
    )

    op.create_table(
        "trade_area_templates",
        sa.Column("template_id", sa.String(length=160), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("industry_categories", sa.JSON(), nullable=False),
        sa.Column("area_types", sa.JSON(), nullable=False),
        sa.Column("analysis_dimensions", sa.JSON(), nullable=False),
        sa.Column("inference_rules", sa.JSON(), nullable=False),
        sa.Column("recommendation_hints", sa.JSON(), nullable=False),
        sa.Column("prompt_context", sa.Text(), nullable=False),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("evidence_summary", sa.JSON(), nullable=False),
        sa.Column("source_candidate_id", sa.String(length=48), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("template_id", "version"),
    )
    op.create_index(
        op.f("ix_trade_area_templates_status"), "trade_area_templates", ["status"]
    )
    op.create_index(
        op.f("ix_trade_area_templates_source_candidate_id"),
        "trade_area_templates",
        ["source_candidate_id"],
    )

    op.create_table(
        "template_update_candidates",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("template_type", sa.String(length=32), nullable=False),
        sa.Column("template_id", sa.String(length=160), nullable=False),
        sa.Column("base_version", sa.Integer(), nullable=True),
        sa.Column("proposed_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("source_evidence", sa.JSON(), nullable=False),
        sa.Column("proposed_payload", sa.JSON(), nullable=False),
        sa.Column("diff", sa.JSON(), nullable=False),
        sa.Column("validation_errors", sa.JSON(), nullable=False),
        sa.Column("requires_human_approval", sa.Boolean(), nullable=False),
        sa.Column("generation_model", sa.String(length=160), nullable=False),
        sa.Column("approved_by", sa.String(length=160), nullable=True),
        sa.Column("approval_note", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_by", sa.String(length=160), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_template_update_candidates_template_type"),
        "template_update_candidates",
        ["template_type"],
    )
    op.create_index(
        op.f("ix_template_update_candidates_template_id"),
        "template_update_candidates",
        ["template_id"],
    )
    op.create_index(
        op.f("ix_template_update_candidates_status"),
        "template_update_candidates",
        ["status"],
    )
    op.create_index(
        op.f("ix_template_update_candidates_created_at"),
        "template_update_candidates",
        ["created_at"],
    )

    op.create_table(
        "template_video_analyses",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("trend_id", sa.String(length=160), nullable=False),
        sa.Column("youtube_url", sa.Text(), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("insights", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_fingerprint"),
    )
    op.create_index(
        op.f("ix_template_video_analyses_trend_id"),
        "template_video_analyses",
        ["trend_id"],
    )
    op.create_index(
        op.f("ix_template_video_analyses_source_fingerprint"),
        "template_video_analyses",
        ["source_fingerprint"],
        unique=True,
    )
    op.create_index(
        op.f("ix_template_video_analyses_status"),
        "template_video_analyses",
        ["status"],
    )
    op.create_index(
        op.f("ix_template_video_analyses_created_at"),
        "template_video_analyses",
        ["created_at"],
    )

    op.create_table(
        "trade_area_analyses",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("template_id", sa.String(length=160), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("evidence_snapshot", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_trade_area_analyses_template_id"),
        "trade_area_analyses",
        ["template_id"],
    )
    op.create_index(
        op.f("ix_trade_area_analyses_created_at"),
        "trade_area_analyses",
        ["created_at"],
    )

    op.create_table(
        "template_knowledge_runs",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("operation", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("celery_task_id", sa.String(length=64), nullable=True),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_template_knowledge_runs_operation"),
        "template_knowledge_runs",
        ["operation"],
    )
    op.create_index(
        op.f("ix_template_knowledge_runs_status"),
        "template_knowledge_runs",
        ["status"],
    )
    op.create_index(
        op.f("ix_template_knowledge_runs_created_at"),
        "template_knowledge_runs",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_template_knowledge_runs_created_at"), table_name="template_knowledge_runs"
    )
    op.drop_index(
        op.f("ix_template_knowledge_runs_status"), table_name="template_knowledge_runs"
    )
    op.drop_index(
        op.f("ix_template_knowledge_runs_operation"), table_name="template_knowledge_runs"
    )
    op.drop_table("template_knowledge_runs")
    op.drop_index(op.f("ix_trade_area_analyses_created_at"), table_name="trade_area_analyses")
    op.drop_index(op.f("ix_trade_area_analyses_template_id"), table_name="trade_area_analyses")
    op.drop_table("trade_area_analyses")
    op.drop_index(
        op.f("ix_template_video_analyses_created_at"), table_name="template_video_analyses"
    )
    op.drop_index(
        op.f("ix_template_video_analyses_status"), table_name="template_video_analyses"
    )
    op.drop_index(
        op.f("ix_template_video_analyses_source_fingerprint"),
        table_name="template_video_analyses",
    )
    op.drop_index(
        op.f("ix_template_video_analyses_trend_id"), table_name="template_video_analyses"
    )
    op.drop_table("template_video_analyses")
    op.drop_index(
        op.f("ix_template_update_candidates_created_at"),
        table_name="template_update_candidates",
    )
    op.drop_index(
        op.f("ix_template_update_candidates_status"), table_name="template_update_candidates"
    )
    op.drop_index(
        op.f("ix_template_update_candidates_template_id"),
        table_name="template_update_candidates",
    )
    op.drop_index(
        op.f("ix_template_update_candidates_template_type"),
        table_name="template_update_candidates",
    )
    op.drop_table("template_update_candidates")
    op.drop_index(
        op.f("ix_trade_area_templates_source_candidate_id"),
        table_name="trade_area_templates",
    )
    op.drop_index(op.f("ix_trade_area_templates_status"), table_name="trade_area_templates")
    op.drop_table("trade_area_templates")
    op.drop_index(
        op.f("ix_editing_templates_source_candidate_id"), table_name="editing_templates"
    )
    op.drop_column("editing_templates", "activated_at")
    op.drop_column("editing_templates", "source_candidate_id")
    op.drop_column("editing_templates", "evidence_summary")
