"""purge removed ranked shortforms and their stored references

Revision ID: 20260830_0012
Revises: 20260830_0011
Create Date: 2026-08-30
"""

import json

import sqlalchemy as sa
from alembic import op


revision = "20260830_0012"
down_revision = "20260830_0011"
branch_labels = None
depends_on = None

_CHALLENGE_IDS = {
    "cafe_recommendation_reels",
    "donggeurio_challenge",
    "donggeurio_store_promotion",
}
_TEMPLATE_IDS = {
    "gt_cafe_recommendation",
    "gt_donggeurio_challenge",
    "gt_donggeurio_store_promotion",
}
_REFERENCE_MARKERS = (
    *_CHALLENGE_IDS,
    *_TEMPLATE_IDS,
    "카페 추천 리뷰 릴스",
    "동그리오(챌린지)",
    "동그리오(매장 홍보)",
    "https://www.youtube.com/shorts/OWnLiuJU8Ks",
    "https://www.youtube.com/shorts/6duJ3WOzeuQ",
)


def _contains_removed_reference(value: object) -> bool:
    if value is None:
        return False
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    return any(marker in value for marker in _REFERENCE_MARKERS)


def _table(bind: sa.engine.Connection, name: str) -> sa.Table | None:
    if not sa.inspect(bind).has_table(name):
        return None
    return sa.Table(name, sa.MetaData(), autoload_with=bind)


def _delete_rows_containing_references(
    bind: sa.engine.Connection,
    table_name: str,
    columns: tuple[str, ...],
) -> None:
    table = _table(bind, table_name)
    if table is None:
        return
    selected = [table.c.id, *(table.c[name] for name in columns)]
    ids = [
        row.id
        for row in bind.execute(sa.select(*selected)).mappings()
        if any(_contains_removed_reference(row[name]) for name in columns)
    ]
    if ids:
        bind.execute(sa.delete(table).where(table.c.id.in_(ids)))


def _delete_editing_runs(bind: sa.engine.Connection) -> None:
    table = _table(bind, "editing_runs")
    if table is None:
        return
    json_columns = (
        "request_snapshot",
        "video_context",
        "recipe",
        "render_result",
        "publishing_result",
        "missing_scene_roles",
        "available_options",
    )
    rows = list(
        bind.execute(
            sa.select(table.c.id, table.c.parent_run_id, *(table.c[name] for name in json_columns))
        ).mappings()
    )
    deleted = {
        row["id"]
        for row in rows
        if any(_contains_removed_reference(row[name]) for name in json_columns)
    }
    changed = True
    while changed:
        changed = False
        for row in rows:
            if row["id"] not in deleted and row["parent_run_id"] in deleted:
                deleted.add(row["id"])
                changed = True
    if deleted:
        bind.execute(
            sa.update(table).where(table.c.parent_run_id.in_(deleted)).values(parent_run_id=None)
        )
        bind.execute(sa.delete(table).where(table.c.id.in_(deleted)))


def _purge_template_source_records(bind: sa.engine.Connection) -> None:
    records = _table(bind, "template_source_records")
    if records is None:
        return
    ids = [
        row.id
        for row in bind.execute(
            sa.select(records.c.id, records.c.record_key, records.c.payload)
        ).mappings()
        if _contains_removed_reference(row.record_key) or _contains_removed_reference(row.payload)
    ]
    if ids:
        bind.execute(sa.delete(records).where(records.c.id.in_(ids)))

    bundles = _table(bind, "template_source_bundles")
    if bundles is None:
        return
    for row in bind.execute(
        sa.select(bundles.c.id, bundles.c.dataset_manifest).where(
            bundles.c.template_type == "VIDEO_EDITING"
        )
    ).mappings():
        manifest = dict(row.dataset_manifest or {})
        remaining_rows = bind.execute(
            sa.select(records.c.dataset_name, records.c.status).where(records.c.bundle_id == row.id)
        ).mappings()
        counts: dict[str, dict[str, int]] = {}
        for record in remaining_rows:
            dataset_counts = counts.setdefault(record.dataset_name, {})
            dataset_counts[record.status] = dataset_counts.get(record.status, 0) + 1
        for dataset_name, dataset in manifest.items():
            if not isinstance(dataset, dict):
                continue
            metadata_rows = dataset.get("metadata_rows")
            if isinstance(metadata_rows, list):
                dataset["metadata_rows"] = [
                    metadata
                    for metadata in metadata_rows
                    if not _contains_removed_reference(metadata)
                ]
            status_counts = counts.get(dataset_name, {})
            dataset["record_count"] = sum(status_counts.values())
            dataset["status_counts"] = dict(sorted(status_counts.items()))
        bind.execute(
            sa.update(bundles).where(bundles.c.id == row.id).values(dataset_manifest=manifest)
        )


def upgrade() -> None:
    bind = op.get_bind()

    video_records = _table(bind, "video_editing_db_records")
    if video_records is not None:
        bind.execute(sa.delete(video_records).where(video_records.c.template_id.in_(_TEMPLATE_IDS)))

    candidates = _table(bind, "template_update_candidates")
    if candidates is not None:
        bind.execute(sa.delete(candidates).where(candidates.c.template_id.in_(_TEMPLATE_IDS)))

    analyses = _table(bind, "template_video_analyses")
    if analyses is not None:
        bind.execute(sa.delete(analyses).where(analyses.c.trend_id.in_(_CHALLENGE_IDS)))

    _purge_template_source_records(bind)
    _delete_rows_containing_references(
        bind,
        "shortform_sessions",
        (
            "store_context",
            "project_state",
            "conversation",
            "shown_video_editing_db_ids",
            "current_recommendation",
        ),
    )
    _delete_editing_runs(bind)
    _delete_rows_containing_references(
        bind,
        "template_knowledge_runs",
        ("request_payload", "result", "error", "error_message"),
    )

    snapshots = _table(bind, "ranking_snapshots")
    if snapshots is not None:
        bind.execute(sa.delete(snapshots).where(snapshots.c.challenge_id.in_(_CHALLENGE_IDS)))
    challenges = _table(bind, "challenges")
    if challenges is not None:
        bind.execute(sa.delete(challenges).where(challenges.c.id.in_(_CHALLENGE_IDS)))


def downgrade() -> None:
    # Purged production records cannot be reconstructed without reintroducing the
    # deleted shortforms, so downgrade intentionally keeps them removed.
    pass
