from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from importlib import resources
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.video_editing_db_record import VideoEditingDBRecord
from app.models.template_source import TemplateSourceBundle, TemplateSourceRecord
from app.schemas.template_knowledge import (
    VideoEditingDBContent,
    TemplateSourceBundleRead,
    TemplateSourceRecordRead,
    TemplateSourceStatus,
    TemplateType,
    TradeAreaSourceContextRead,
)
from app.template_knowledge.validation import TemplateCandidateValidator

_SOURCE_PACKAGE = "app.template_knowledge.sources"
_SHOOTING_INTERVALS_FILE = "video_editing_task_intervals.json"
_SOURCE_FILES = {
    TemplateType.VIDEO_EDITING: ("video_editing.json", "영상편집DB.xlsx"),
    TemplateType.TRADE_AREA: ("trade_area.json", "상권분석DB.xlsx"),
}
_SERVICE_ELIGIBLE_STATUSES = {
    "ACTIVE",
    "APPROVED",
    "CURRENT",
    "INGESTED",
    "PASS",
    "UNSPECIFIED",
}
_TRADE_AREA_APPROVAL_DATASETS = {
    "regions",
    "categories",
    "concepts",
    "region_category_map",
    "official_trade_area_profiles",
    "content_region_trade_area_map",
    "official_category_map",
}

_EDITING_SCOPE_ADAPTERS: dict[str, dict[str, Any]] = {
    "jujutsu_transition": {
        "supported_subject_types": ["MENU", "PRODUCT"],
        "supported_objectives": ["awareness", "new_customer", "visit", "sales"],
        "supported_filming_times": ["within_10m", "within_20m", "30m_plus"],
        "supported_face_modes": ["allowed", "not_allowed"],
        "minimum_filming_time": "within_10m",
        "requires_face": False,
    },
    "otsukare_summer_challenge": {
        "supported_subject_types": ["STORE", "SERVICE"],
        "supported_objectives": ["awareness", "new_customer", "visit", "trust"],
        "supported_filming_times": ["within_10m", "within_20m", "30m_plus"],
        "supported_face_modes": ["allowed"],
        "minimum_filming_time": "within_10m",
        "requires_face": True,
    },
    "cafe_recommendation_reels": {
        "supported_subject_types": ["MENU", "STORE"],
        "supported_objectives": ["awareness", "new_customer", "visit", "trust"],
        "supported_filming_times": ["within_20m", "30m_plus"],
        "supported_face_modes": ["allowed", "not_allowed"],
        "minimum_filming_time": "within_20m",
        "requires_face": False,
    },
}


class TemplateSourceImportError(RuntimeError):
    pass


class TemplateSourceService:
    def list_bundles(
        self,
        db: Session,
        *,
        template_type: TemplateType | None = None,
    ) -> list[TemplateSourceBundleRead]:
        query = select(TemplateSourceBundle).order_by(TemplateSourceBundle.imported_at.desc())
        if template_type is not None:
            query = query.where(TemplateSourceBundle.template_type == template_type.value)
        return [TemplateSourceBundleRead.model_validate(row) for row in db.scalars(query)]

    def list_records(
        self,
        db: Session,
        bundle_id: str,
        *,
        dataset_name: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[TemplateSourceRecordRead]:
        query = (
            select(TemplateSourceRecord)
            .where(TemplateSourceRecord.bundle_id == bundle_id)
            .order_by(
                TemplateSourceRecord.dataset_name,
                TemplateSourceRecord.source_row_number,
            )
            .limit(limit)
        )
        if dataset_name:
            query = query.where(TemplateSourceRecord.dataset_name == dataset_name)
        if status:
            query = query.where(TemplateSourceRecord.status == status.upper())
        return [TemplateSourceRecordRead.model_validate(row) for row in db.scalars(query)]

    def resolve_trade_area_context(
        self,
        db: Session,
        *,
        region_id: str | None = None,
        category_id: str | None = None,
        official_trade_area_code: str | None = None,
        include_draft: bool = False,
    ) -> TradeAreaSourceContextRead:
        bundle = db.scalar(
            select(TemplateSourceBundle)
            .where(TemplateSourceBundle.template_type == TemplateType.TRADE_AREA.value)
            .order_by(TemplateSourceBundle.imported_at.desc())
        )
        if bundle is None:
            raise TemplateSourceImportError("Trade-area source bundle is not imported.")

        rows = list(
            db.scalars(
                select(TemplateSourceRecord).where(
                    TemplateSourceRecord.bundle_id == bundle.id
                )
            )
        )
        if not include_draft:
            rows = [row for row in rows if row.status in _SERVICE_ELIGIBLE_STATUSES]
        grouped: dict[str, list[TemplateSourceRecord]] = defaultdict(list)
        for row in rows:
            grouped[row.dataset_name].append(row)

        region = _find(grouped["regions"], region_id=region_id)
        category = _find(grouped["categories"], category_id=category_id)
        fit = _find(
            grouped["region_category_map"],
            region_id=region_id,
            category_id=category_id,
        )
        official = _find(
            grouped["official_trade_areas"], current_trdar_cd=official_trade_area_code
        )
        profile = _find(
            grouped["official_trade_area_profiles"],
            current_trdar_cd=official_trade_area_code,
        )
        mapping = _find(
            grouped["content_region_trade_area_map"],
            current_trdar_cd=official_trade_area_code,
            content_region_id=region_id,
        )
        selected = [item for item in (region, category, fit, official, profile, mapping) if item]
        return TradeAreaSourceContextRead(
            bundle_id=bundle.id,
            region=region.payload if region else None,
            category=category.payload if category else None,
            region_category_fit=fit.payload if fit else None,
            official_trade_area=official.payload if official else None,
            official_profile=profile.payload if profile else None,
            mapping=mapping.payload if mapping else None,
            source_ids=[item.id for item in selected],
            draft_data_included=include_draft,
        )


def import_provided_template_library(
    db: Session,
    *,
    validator: TemplateCandidateValidator | None = None,
) -> dict[str, Any]:
    imported: list[str] = []
    skipped: list[str] = []
    bundles: dict[TemplateType, TemplateSourceBundle] = {}
    payloads: dict[TemplateType, dict[str, Any]] = {}

    for template_type in (TemplateType.VIDEO_EDITING, TemplateType.TRADE_AREA):
        payload = _load_source_payload(template_type)
        payloads[template_type] = payload
        bundle, was_created = _import_bundle(db, payload)
        bundles[template_type] = bundle
        marker = f"SOURCE_BUNDLE:{template_type.value}:{bundle.schema_version}"
        (imported if was_created else skipped).append(marker)

    editing_result = _import_video_editing_db(
        db,
        payloads[TemplateType.VIDEO_EDITING],
        bundles[TemplateType.VIDEO_EDITING],
        validator=validator or TemplateCandidateValidator(),
    )
    imported.extend(editing_result["created"])
    skipped.extend(editing_result["skipped"])
    trade_area_eligible = _trade_area_service_eligible(
        payloads[TemplateType.TRADE_AREA]
    )
    return {
        "created": imported,
        "skipped": skipped,
        "source_bundles": {
            template_type.value: bundles[template_type].id for template_type in bundles
        },
        "video_editing_db": editing_result,
        "trade_area": {
            "status": bundles[TemplateType.TRADE_AREA].status,
            "service_eligible": trade_area_eligible,
            "reason": (
                "The provided trade-area DB is provisionally approved pending the next research cycle."
                if trade_area_eligible
                else "The provided trade-area DB contains unapproved core knowledge rows."
            ),
        },
    }


def _load_source_payload(template_type: TemplateType) -> dict[str, Any]:
    json_name, workbook_name = _SOURCE_FILES[template_type]
    package = resources.files(_SOURCE_PACKAGE)
    payload = json.loads(package.joinpath(json_name).read_text(encoding="utf-8"))
    workbook_bytes = package.joinpath(workbook_name).read_bytes()
    actual_sha = hashlib.sha256(workbook_bytes).hexdigest().upper()
    expected_sha = str(payload["source"]["sha256"]).upper()
    if actual_sha != expected_sha:
        raise TemplateSourceImportError(
            f"Source checksum mismatch for {workbook_name}: {actual_sha} != {expected_sha}"
        )
    if payload.get("template_type") != template_type.value:
        raise TemplateSourceImportError(f"Unexpected template type in {json_name}.")
    return payload


@lru_cache(maxsize=1)
def _load_shooting_task_intervals() -> dict[str, Any]:
    package = resources.files(_SOURCE_PACKAGE)
    payload = json.loads(
        package.joinpath(_SHOOTING_INTERVALS_FILE).read_text(encoding="utf-8")
    )
    source = payload["source"]
    workbook_name = str(source["bundled_filename"])
    actual_sha = hashlib.sha256(package.joinpath(workbook_name).read_bytes()).hexdigest().upper()
    expected_sha = str(source["sha256"]).upper()
    if actual_sha != expected_sha:
        raise TemplateSourceImportError(
            f"Shooting interval source checksum mismatch for {workbook_name}: "
            f"{actual_sha} != {expected_sha}"
        )

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for interval in payload["intervals"]:
        grouped[str(interval["challenge_id"])].append(interval)
    for challenge_id, intervals in grouped.items():
        intervals.sort(key=lambda item: int(item["display_order"]))
        expected_orders = list(range(1, len(intervals) + 1))
        actual_orders = [int(item["display_order"]) for item in intervals]
        if actual_orders != expected_orders:
            raise TemplateSourceImportError(
                f"Shooting intervals must be contiguous for {challenge_id}: {actual_orders}"
            )
        if any(float(item["end_ms"]) <= float(item["start_ms"]) for item in intervals):
            raise TemplateSourceImportError(
                f"Shooting intervals must have positive durations for {challenge_id}."
            )
    return {"source": source, "by_challenge": dict(grouped)}


def _import_bundle(
    db: Session, payload: dict[str, Any]
) -> tuple[TemplateSourceBundle, bool]:
    source = payload["source"]
    source_sha = str(source["sha256"]).upper()
    existing = db.scalar(
        select(TemplateSourceBundle).where(
            TemplateSourceBundle.source_sha256 == source_sha
        )
    )
    if existing is not None:
        return existing, False

    template_type = TemplateType(payload["template_type"])
    status = TemplateSourceStatus.ACTIVE
    if (
        template_type == TemplateType.TRADE_AREA
        and not _trade_area_service_eligible(payload)
    ):
        status = TemplateSourceStatus.DRAFT
    if status == TemplateSourceStatus.ACTIVE:
        for current in db.scalars(
            select(TemplateSourceBundle).where(
                TemplateSourceBundle.template_type == template_type.value,
                TemplateSourceBundle.status == TemplateSourceStatus.ACTIVE.value,
            )
        ):
            current.status = TemplateSourceStatus.ARCHIVED.value
    bundle_id = f"tsb_{template_type.value.lower()}_{source_sha[:20].lower()}"
    manifest: dict[str, Any] = {}
    rows: list[TemplateSourceRecord] = []
    for dataset_name, dataset in payload["datasets"].items():
        status_counts = Counter()
        records = dataset.get("records", [])
        for index, raw in enumerate(records, start=1):
            item = dict(raw)
            source_row = int(item.pop("_source_row_number", index))
            record_status = str(item.pop("_record_status", "UNSPECIFIED")).upper()
            status_counts[record_status] += 1
            record_key = _record_key(item, source_row)
            digest = hashlib.sha256(
                f"{bundle_id}|{dataset_name}|{source_row}".encode()
            ).hexdigest()[:24]
            rows.append(
                TemplateSourceRecord(
                    id=f"tsr_{digest}",
                    bundle_id=bundle_id,
                    dataset_name=dataset_name,
                    record_key=record_key,
                    source_row_number=source_row,
                    status=record_status,
                    payload=item,
                )
            )
        manifest[dataset_name] = {
            "columns": dataset.get("columns", []),
            "metadata_rows": dataset.get("metadata_rows", []),
            "record_count": len(records),
            "status_counts": dict(sorted(status_counts.items())),
        }
    bundle = TemplateSourceBundle(
        id=bundle_id,
        template_type=template_type.value,
        schema_version=str(payload["schema_version"]),
        source_filename=str(source["filename"]),
        source_sha256=source_sha,
        status=status.value,
        dataset_manifest=manifest,
    )
    db.add(bundle)
    db.add_all(rows)
    db.commit()
    db.refresh(bundle)
    return bundle, True


def _import_video_editing_db(
    db: Session,
    payload: dict[str, Any],
    bundle: TemplateSourceBundle,
    *,
    validator: TemplateCandidateValidator,
) -> dict[str, list[str]]:
    guide_rows = payload["datasets"]["03_GUIDE_TEMPLATES"]["records"]
    challenge_rows = payload["datasets"]["02_INPUT_GUIDES"]["records"]
    challenge_names = {row["id"]: row["name"] for row in challenge_rows}
    challenges_by_id = {row["id"]: row for row in challenge_rows}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in guide_rows:
        if row["validation_status"] != "PASS":
            continue
        groups[row["guide_template_id"]].append(row)

    created: list[str] = []
    skipped: list[str] = []
    interval_source = _load_shooting_task_intervals()
    for source_template_id, rows in groups.items():
        rows.sort(key=lambda item: int(item["guide_sequence_index"]))
        active_rows = [row for row in rows if row["template_status"] == "ACTIVE"]
        if not active_rows:
            continue
        source_version = int(rows[0]["template_version"])
        template_id = re.sub(r"_v\d+$", "", source_template_id)
        marker = f"VIDEO_EDITING:{template_id}:v{source_version}"
        task_intervals = interval_source["by_challenge"].get(rows[0]["challenge_id"])
        if not task_intervals:
            raise TemplateSourceImportError(
                f"Original shooting intervals are missing for {rows[0]['challenge_id']}."
            )
        content = _editing_content(
            rows,
            challenge_name=challenge_names.get(rows[0]["challenge_id"], rows[0]["challenge_id"]),
            task_intervals=task_intervals,
        )
        errors = validator.validate(
            TemplateType.VIDEO_EDITING,
            content.model_dump(mode="json"),
            is_initial_version=True,
        )
        if errors:
            raise TemplateSourceImportError(
                f"Provided video-editing DB record {source_template_id} failed validation: {errors}"
            )
        interval_evidence = {
            **interval_source["source"],
            "source_rows": [int(item["source_row_number"]) for item in task_intervals],
            "task_count": len(task_intervals),
        }
        existing = db.get(VideoEditingDBRecord, (template_id, source_version))
        if existing is not None:
            evidence_summary = dict(existing.evidence_summary or {})
            existing.shooting_guide = content.shooting_guide.model_dump(mode="json")
            evidence_summary["shooting_task_intervals"] = interval_evidence
            existing.evidence_summary = evidence_summary
            db.commit()
            skipped.append(marker)
            continue
        for current in db.scalars(
            select(VideoEditingDBRecord).where(
                VideoEditingDBRecord.template_id == template_id,
                VideoEditingDBRecord.status == "ACTIVE",
            )
        ):
            current.status = "ARCHIVED"
        db.add(
            VideoEditingDBRecord(
                template_id=template_id,
                version=source_version,
                status="ACTIVE",
                name=content.name,
                recommendation_title=content.recommendation_title,
                recommendation_concept=content.recommendation_concept,
                recommendation_metadata=content.recommendation_metadata.model_dump(mode="json"),
                shooting_guide=content.shooting_guide.model_dump(mode="json"),
                editing_rules=content.editing_rules.model_dump(mode="json"),
                trend_ids=content.trend_ids,
                evidence_summary={
                    "provided_source": {
                        "bundle_id": bundle.id,
                        "source_file": bundle.source_filename,
                        "source_sha256": bundle.source_sha256,
                        "source_template_id": source_template_id,
                        "source_template_version": source_version,
                        "source_rows": [row["_source_row_number"] for row in rows],
                    },
                    "input_guide": challenges_by_id.get(rows[0]["challenge_id"]),
                    "reference_segments": active_rows,
                    "shooting_task_intervals": interval_evidence,
                },
                activated_at=datetime.now(timezone.utc),
            )
        )
        created.append(marker)
    db.commit()
    return {"created": created, "skipped": skipped}


def _editing_content(
    rows: list[dict[str, Any]],
    *,
    challenge_name: str,
    task_intervals: list[dict[str, Any]],
) -> VideoEditingDBContent:
    challenge_id = str(rows[0]["challenge_id"])
    active_rows = [row for row in rows if row["template_status"] == "ACTIVE"]
    if not active_rows:
        raise TemplateSourceImportError(f"No ACTIVE segment exists for {challenge_id}.")
    adapter = _EDITING_SCOPE_ADAPTERS.get(challenge_id)
    if adapter is None:
        raise TemplateSourceImportError(
            f"Recommendation scope adapter is required for provided challenge {challenge_id}."
        )
    production_minutes = max(int(row["estimated_production_minutes"]) for row in active_rows)
    scenes = []
    tasks = []
    for order, interval in enumerate(task_intervals, start=1):
        duration = max(
            0.1,
            (float(interval["end_ms"]) - float(interval["start_ms"])) / 1000,
        )
        instructions = [
            _bounded(str(item), 500) for item in interval.get("instructions", []) if str(item).strip()
        ]
        if not instructions:
            raise TemplateSourceImportError(
                f"Shooting interval {challenge_id}:{order} has no instructions."
            )
        description = _bounded(
            f"{interval['task_title']} — {' '.join(instructions)}",
            500,
        )
        scenes.append(
            {
                "scene_order": order,
                "scene_role": _bounded(str(interval["scene_role"]), 80),
                "scene_description": description,
                "scene_dialogue": None,
                "scene_subtitle": None,
                "shot_type": "가이드 구간 재현",
                "target_duration_sec": min(duration, 30),
            }
        )
        tasks.append(
            {
                "display_order": order,
                "task_title": _bounded(str(interval["task_title"]), 200),
                "scene_index": order - 1,
                "guide": {
                    "instructions": instructions,
                },
            }
        )
    concept = _bounded(
        " → ".join(str(row["scene_summary"]) for row in active_rows),
        2000,
    )
    max_duration = max(float(row["end_ms"]) for row in active_rows) / 1000
    return VideoEditingDBContent.model_validate(
        {
            "name": challenge_name,
            "recommendation_title": challenge_name,
            "recommendation_concept": concept,
            "recommendation_metadata": {
                **adapter,
                "requires_tts": False,
                "requires_photo_input": False,
                "renderer_supported": True,
                "source_type": "VIDEO_ONLY",
                "difficulty": "중" if production_minutes <= 10 else "상",
            },
            "shooting_guide": {
                "estimated_shooting_sec": production_minutes * 60,
                "required_people": 1,
                "props": [],
                "difficulty": "중" if production_minutes <= 10 else "상",
                "scenes": scenes,
                "tasks": tasks,
            },
            "editing_rules": {
                "source_type": "VIDEO_ONLY",
                "render_profile_id": "INSTAGRAM_REELS_V1",
                "assembly_profile_id": "INTERMEDIATE_VERTICAL_V1",
                "safe_area_profile_id": "INSTAGRAM_REELS_2026_V1",
                "audio_policy": "SILENT_V1",
                "min_cut_duration_ms": 300,
                "max_duration_sec": max_duration,
                "allowed_effect_ids": [],
                "allowed_transition_ids": ["CUT", "HARD_CUT"],
            },
            "trend_ids": [challenge_id],
        }
    )


def _record_key(payload: dict[str, Any], source_row: int) -> str:
    for key, value in payload.items():
        if value is not None and str(value).strip():
            return _bounded(str(value), 255)
    return f"row_{source_row}"


def _trade_area_service_eligible(payload: dict[str, Any]) -> bool:
    datasets = payload.get("datasets", {})
    for dataset_name in _TRADE_AREA_APPROVAL_DATASETS:
        records = datasets.get(dataset_name, {}).get("records", [])
        if not records or any(
            str(record.get("_record_status") or "").upper() != "APPROVED"
            for record in records
        ):
            return False
    return True


def _find(
    records: list[TemplateSourceRecord], **conditions: str | None
) -> TemplateSourceRecord | None:
    expected = {key: str(value) for key, value in conditions.items() if value is not None}
    if not expected:
        return None
    for record in records:
        if all(str(record.payload.get(key)) == value for key, value in expected.items()):
            return record
    return None


def _bounded(value: str | None, limit: int) -> str:
    if value is None:
        return ""
    return value if len(value) <= limit else f"{value[: limit - 1]}…"
