from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from importlib import resources
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.editing_template import EditingTemplate
from app.models.template_source import TemplateSourceBundle, TemplateSourceRecord
from app.schemas.template_knowledge import (
    EditingTemplateContent,
    TemplateSourceBundleRead,
    TemplateSourceRecordRead,
    TemplateSourceStatus,
    TemplateType,
    TradeAreaSourceContextRead,
)
from app.template_knowledge.validation import TemplateCandidateValidator

_SOURCE_PACKAGE = "app.template_knowledge.sources"
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

    editing_result = _import_editing_templates(
        db,
        payloads[TemplateType.VIDEO_EDITING],
        bundles[TemplateType.VIDEO_EDITING],
        validator=validator or TemplateCandidateValidator(),
    )
    imported.extend(editing_result["created"])
    skipped.extend(editing_result["skipped"])
    return {
        "created": imported,
        "skipped": skipped,
        "source_bundles": {
            template_type.value: bundles[template_type].id for template_type in bundles
        },
        "editing_templates": editing_result,
        "trade_area": {
            "status": bundles[TemplateType.TRADE_AREA].status,
            "service_eligible": False,
            "reason": "The provided workbook marks the trade-area knowledge rows as draft.",
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
    status = (
        TemplateSourceStatus.ACTIVE
        if template_type == TemplateType.VIDEO_EDITING
        else TemplateSourceStatus.DRAFT
    )
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


def _import_editing_templates(
    db: Session,
    payload: dict[str, Any],
    bundle: TemplateSourceBundle,
    *,
    validator: TemplateCandidateValidator,
) -> dict[str, list[str]]:
    guide_rows = payload["datasets"]["03_GUIDE_TEMPLATES"]["records"]
    challenge_rows = payload["datasets"]["02_INPUT_GUIDES"]["records"]
    challenge_names = {row["id"]: row["name"] for row in challenge_rows}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in guide_rows:
        if row["validation_status"] != "PASS":
            continue
        groups[row["guide_template_id"]].append(row)

    created: list[str] = []
    skipped: list[str] = []
    for source_template_id, rows in groups.items():
        rows.sort(key=lambda item: int(item["guide_sequence_index"]))
        source_version = int(rows[0]["template_version"])
        template_id = re.sub(r"_v\d+$", "", source_template_id)
        marker = f"VIDEO_EDITING:{template_id}:v{source_version}"
        if db.get(EditingTemplate, (template_id, source_version)) is not None:
            skipped.append(marker)
            continue
        content = _editing_content(
            rows,
            challenge_name=challenge_names.get(rows[0]["challenge_id"], rows[0]["challenge_id"]),
        )
        errors = validator.validate(
            TemplateType.VIDEO_EDITING,
            content.model_dump(mode="json"),
            is_initial_version=True,
        )
        if errors:
            raise TemplateSourceImportError(
                f"Provided editing template {source_template_id} failed validation: {errors}"
            )
        for current in db.scalars(
            select(EditingTemplate).where(
                EditingTemplate.template_id == template_id,
                EditingTemplate.status == "ACTIVE",
            )
        ):
            current.status = "ARCHIVED"
        db.add(
            EditingTemplate(
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
                    }
                },
                activated_at=datetime.now(timezone.utc),
            )
        )
        created.append(marker)
    db.commit()
    return {"created": created, "skipped": skipped}


def _editing_content(
    rows: list[dict[str, Any]], *, challenge_name: str
) -> EditingTemplateContent:
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
    for order, row in enumerate(active_rows, start=1):
        duration = max(0.1, (float(row["end_ms"]) - float(row["start_ms"])) / 1000)
        description = _bounded(
            f"{row['scene_summary']} — {row['action_pattern']}",
            500,
        )
        on_screen_text = str(row.get("guide_on_screen_text") or "").strip() or None
        scenes.append(
            {
                "scene_order": order,
                "scene_role": _bounded(str(row["narrative_role"]), 80),
                "scene_description": description,
                "scene_dialogue": None,
                "scene_subtitle": _bounded(on_screen_text, 200) if on_screen_text else None,
                "shot_type": "가이드 구간 재현",
                "target_duration_sec": min(duration, 30),
            }
        )
        tasks.append(
            {
                "task_order": order,
                "description": _bounded(str(row["action_pattern"]), 500),
            }
        )
    concept = _bounded(
        " → ".join(str(row["scene_summary"]) for row in active_rows),
        2000,
    )
    max_duration = max(float(row["end_ms"]) for row in active_rows) / 1000
    return EditingTemplateContent.model_validate(
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
