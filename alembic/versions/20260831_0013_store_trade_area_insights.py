"""persist store trade-area insight results

Revision ID: 20260831_0013
Revises: 20260830_0012
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from alembic import op


revision = "20260831_0013"
down_revision = "20260830_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "store_trade_area_insights",
        sa.Column("normalized_address", sa.String(length=320), nullable=False),
        sa.Column("address", sa.String(length=255), nullable=False),
        sa.Column("store_name", sa.String(length=200), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("district_name", sa.String(length=200), nullable=True),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("normalized_address"),
    )
    op.create_index(
        "ix_store_trade_area_insights_district_name",
        "store_trade_area_insights",
        ["district_name"],
    )
    op.create_index(
        "ix_store_trade_area_insights_analyzed_at",
        "store_trade_area_insights",
        ["analyzed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_store_trade_area_insights_analyzed_at",
        table_name="store_trade_area_insights",
    )
    op.drop_index(
        "ix_store_trade_area_insights_district_name",
        table_name="store_trade_area_insights",
    )
    op.drop_table("store_trade_area_insights")
