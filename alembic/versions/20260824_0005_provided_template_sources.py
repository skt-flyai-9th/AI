"""provided template source bundles

Revision ID: 20260824_0005
Revises: 20260824_0004
Create Date: 2026-08-24
"""

import sqlalchemy as sa
from alembic import op

revision = "20260824_0005"
down_revision = "20260824_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "template_source_bundles",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("template_type", sa.String(length=32), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("source_filename", sa.String(length=255), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("dataset_manifest", sa.JSON(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_sha256"),
    )
    op.create_index(
        op.f("ix_template_source_bundles_template_type"),
        "template_source_bundles",
        ["template_type"],
    )
    op.create_index(
        op.f("ix_template_source_bundles_source_sha256"),
        "template_source_bundles",
        ["source_sha256"],
        unique=True,
    )
    op.create_index(
        op.f("ix_template_source_bundles_status"),
        "template_source_bundles",
        ["status"],
    )

    op.create_table(
        "template_source_records",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("bundle_id", sa.String(length=48), nullable=False),
        sa.Column("dataset_name", sa.String(length=120), nullable=False),
        sa.Column("record_key", sa.String(length=255), nullable=False),
        sa.Column("source_row_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["bundle_id"], ["template_source_bundles.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "bundle_id",
            "dataset_name",
            "source_row_number",
            name="uq_template_source_record_location",
        ),
    )
    op.create_index(
        op.f("ix_template_source_records_bundle_id"),
        "template_source_records",
        ["bundle_id"],
    )
    op.create_index(
        op.f("ix_template_source_records_dataset_name"),
        "template_source_records",
        ["dataset_name"],
    )
    op.create_index(
        op.f("ix_template_source_records_record_key"),
        "template_source_records",
        ["record_key"],
    )
    op.create_index(
        op.f("ix_template_source_records_status"),
        "template_source_records",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_template_source_records_status"), table_name="template_source_records"
    )
    op.drop_index(
        op.f("ix_template_source_records_record_key"),
        table_name="template_source_records",
    )
    op.drop_index(
        op.f("ix_template_source_records_dataset_name"),
        table_name="template_source_records",
    )
    op.drop_index(
        op.f("ix_template_source_records_bundle_id"), table_name="template_source_records"
    )
    op.drop_table("template_source_records")
    op.drop_index(
        op.f("ix_template_source_bundles_status"), table_name="template_source_bundles"
    )
    op.drop_index(
        op.f("ix_template_source_bundles_source_sha256"),
        table_name="template_source_bundles",
    )
    op.drop_index(
        op.f("ix_template_source_bundles_template_type"),
        table_name="template_source_bundles",
    )
    op.drop_table("template_source_bundles")
