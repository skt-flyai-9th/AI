"""enforce a single ACTIVE version per template

Revision ID: 20260830_0011
Revises: 20260828_0010
Create Date: 2026-08-30
"""

from alembic import op


revision = "20260830_0011"
down_revision = "20260828_0010"
branch_labels = None
depends_on = None

_TABLES = ("trade_area_db_records", "video_editing_db_records")


def upgrade() -> None:
    for table in _TABLES:
        # Application code always archives the previous ACTIVE row before
        # activating a new version, but nothing at the database level enforced
        # it. Archive any historical duplicates (keeping the highest version)
        # so the partial unique index below can be created safely.
        op.execute(
            f"""
            UPDATE {table} AS current
            SET status = 'ARCHIVED'
            WHERE status = 'ACTIVE'
              AND version < (
                SELECT MAX(other.version)
                FROM {table} AS other
                WHERE other.template_id = current.template_id
                  AND other.status = 'ACTIVE'
              )
            """
        )
        op.execute(
            f"CREATE UNIQUE INDEX uq_{table}_one_active "
            f"ON {table} (template_id) WHERE status = 'ACTIVE'"
        )


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP INDEX IF EXISTS uq_{table}_one_active")
    # Rows archived by upgrade() stay archived: they were invariant violations,
    # and un-archiving them would recreate the ambiguity this migration removed.
