"""purge deprecated shooting-element data

Revision ID: 20260828_0010
Revises: 20260827_0009
Create Date: 2026-08-28
"""

from alembic import op


revision = "20260828_0010"
down_revision = "20260827_0009"
branch_labels = None
depends_on = None


_JSON_COLUMNS = (
    ("video_editing_db_records", "shooting_guide"),
    ("video_editing_db_records", "evidence_summary"),
    ("editing_runs", "request_snapshot"),
    ("editing_runs", "video_context"),
    ("template_source_bundles", "dataset_manifest"),
    ("template_source_records", "payload"),
)


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION pg_temp.remove_shooting_element_data(input jsonb)
        RETURNS jsonb
        LANGUAGE sql
        IMMUTABLE
        AS $function$
            SELECT CASE jsonb_typeof(input)
                WHEN 'object' THEN COALESCE(
                    (
                        SELECT jsonb_object_agg(
                            item.key,
                            pg_temp.remove_shooting_element_data(item.value)
                        )
                        FROM jsonb_each(input) AS item
                        WHERE item.key NOT IN (
                            'shooting_element_id',
                            'shooting_elements',
                            '03A_SHOOTING_ELEMENTS'
                        )
                        AND NOT (
                            jsonb_typeof(item.value) = 'string'
                            AND item.value #>> '{}' ~ '^ELEMENT_[0-9]{2}$'
                        )
                    ),
                    '{}'::jsonb
                )
                WHEN 'array' THEN COALESCE(
                    (
                        SELECT jsonb_agg(
                            pg_temp.remove_shooting_element_data(item.value)
                            ORDER BY item.ordinality
                        )
                        FROM jsonb_array_elements(input) WITH ORDINALITY
                            AS item(value, ordinality)
                        WHERE item.value NOT IN (
                            '"shooting_element_id"'::jsonb,
                            '"shooting_elements"'::jsonb,
                            '"03A_SHOOTING_ELEMENTS"'::jsonb
                        )
                        AND NOT (
                            jsonb_typeof(item.value) = 'string'
                            AND item.value #>> '{}' ~ '^ELEMENT_[0-9]{2}$'
                        )
                    ),
                    '[]'::jsonb
                )
                ELSE input
            END
        $function$;
        """
    )

    op.execute(
        "DELETE FROM template_source_records "
        "WHERE dataset_name = '03A_SHOOTING_ELEMENTS'"
    )
    for table, column in _JSON_COLUMNS:
        op.execute(
            f"UPDATE {table} "
            f"SET {column} = pg_temp.remove_shooting_element_data({column}::jsonb)::json "
            f"WHERE {column}::text ~* "
            "'(shooting_element|03A_SHOOTING_ELEMENTS|ELEMENT_0[0-9])'"
        )

    op.execute(
        """
        UPDATE template_source_records
        SET payload = jsonb_set(
            payload::jsonb,
            '{raw_markdown_line}',
            to_jsonb(replace(payload::jsonb ->> 'raw_markdown_line', '촬영 요소', '촬영 컷'))
        )::json
        WHERE payload::jsonb ->> 'raw_markdown_line' LIKE '%촬영 요소%'
        """
    )

    op.execute(
        """
        DO $block$
        DECLARE
            remaining bigint;
        BEGIN
            SELECT
                (SELECT count(*) FROM template_source_records
                 WHERE dataset_name = '03A_SHOOTING_ELEMENTS')
                + (SELECT count(*) FROM template_source_records
                   WHERE payload::text ~* '(shooting_element|03A_SHOOTING_ELEMENTS|ELEMENT_0[0-9])'
                      OR payload::text LIKE '%촬영 요소%')
                + (SELECT count(*) FROM template_source_bundles
                   WHERE dataset_manifest::text ~* '(shooting_element|03A_SHOOTING_ELEMENTS|ELEMENT_0[0-9])')
                + (SELECT count(*) FROM video_editing_db_records
                   WHERE shooting_guide::text ~* '(shooting_element|03A_SHOOTING_ELEMENTS|ELEMENT_0[0-9])'
                      OR evidence_summary::text ~* '(shooting_element|03A_SHOOTING_ELEMENTS|ELEMENT_0[0-9])')
                + (SELECT count(*) FROM editing_runs
                   WHERE request_snapshot::text ~* '(shooting_element|03A_SHOOTING_ELEMENTS|ELEMENT_0[0-9])'
                      OR video_context::text ~* '(shooting_element|03A_SHOOTING_ELEMENTS|ELEMENT_0[0-9])')
            INTO remaining;

            IF remaining <> 0 THEN
                RAISE EXCEPTION 'shooting-element cleanup left % matching rows', remaining;
            END IF;
        END
        $block$;
        """
    )


def downgrade() -> None:
    # The removed records were deprecated and may contain historical user data.
    # Recreating them during downgrade would invent data, so this cleanup is irreversible.
    pass
