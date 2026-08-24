"""unify video-editing and trade-area database naming

Revision ID: 20260824_0006
Revises: 20260824_0005
Create Date: 2026-08-24
"""

from alembic import op

revision = "20260824_0006"
down_revision = "20260824_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_editing_templates_status", table_name="editing_templates")
    op.drop_index(
        "ix_editing_templates_source_candidate_id", table_name="editing_templates"
    )
    op.rename_table("editing_templates", "video_editing_db_records")
    op.create_index(
        "ix_video_editing_db_records_status",
        "video_editing_db_records",
        ["status"],
    )
    op.create_index(
        "ix_video_editing_db_records_source_candidate_id",
        "video_editing_db_records",
        ["source_candidate_id"],
    )

    op.drop_index("ix_trade_area_templates_status", table_name="trade_area_templates")
    op.drop_index(
        "ix_trade_area_templates_source_candidate_id",
        table_name="trade_area_templates",
    )
    op.rename_table("trade_area_templates", "trade_area_db_records")
    op.create_index(
        "ix_trade_area_db_records_status",
        "trade_area_db_records",
        ["status"],
    )
    op.create_index(
        "ix_trade_area_db_records_source_candidate_id",
        "trade_area_db_records",
        ["source_candidate_id"],
    )

    with op.batch_alter_table("shortform_sessions") as batch_op:
        batch_op.alter_column(
            "shown_template_ids",
            new_column_name="shown_video_editing_db_ids",
        )


def downgrade() -> None:
    with op.batch_alter_table("shortform_sessions") as batch_op:
        batch_op.alter_column(
            "shown_video_editing_db_ids",
            new_column_name="shown_template_ids",
        )

    op.drop_index(
        "ix_trade_area_db_records_source_candidate_id",
        table_name="trade_area_db_records",
    )
    op.drop_index(
        "ix_trade_area_db_records_status", table_name="trade_area_db_records"
    )
    op.rename_table("trade_area_db_records", "trade_area_templates")
    op.create_index(
        "ix_trade_area_templates_status", "trade_area_templates", ["status"]
    )
    op.create_index(
        "ix_trade_area_templates_source_candidate_id",
        "trade_area_templates",
        ["source_candidate_id"],
    )

    op.drop_index(
        "ix_video_editing_db_records_source_candidate_id",
        table_name="video_editing_db_records",
    )
    op.drop_index(
        "ix_video_editing_db_records_status", table_name="video_editing_db_records"
    )
    op.rename_table("video_editing_db_records", "editing_templates")
    op.create_index(
        "ix_editing_templates_status", "editing_templates", ["status"]
    )
    op.create_index(
        "ix_editing_templates_source_candidate_id",
        "editing_templates",
        ["source_candidate_id"],
    )
