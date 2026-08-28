from __future__ import annotations

import hashlib
import math
import sys
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable
from urllib.parse import quote, urlencode, urlparse

from app.agents.editing.reals import RealsRenderJobRequest
from app.core.config import Settings, get_settings
from app.core.security import sign_renderer_file
from app.services.source_assets import (
    SourceAssetDownloadError,
    SourceAssetTooLargeError,
    download_source_asset,
)


class RendererServiceError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int,
        retryable: bool = False,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.details = details or []

    def payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "validation_errors": self.details,
        }


@dataclass(frozen=True)
class NativeRealsModules:
    VideoEditEngine: type
    FinalRenderRequest: type
    build_concat_plan: Callable
    media_ref: Callable
    run: Callable


class RealsRendererService:
    """Resolve remote assets and execute the bundled REALS engine end to end."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        native: NativeRealsModules | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.engine_root = _resolve_engine_root(self.settings.editing_reals_engine_path)
        self.native = native or _load_native_reals(self.engine_root)
        self.engine = self.native.VideoEditEngine(self.engine_root)
        self.output_dir = _resolve_runtime_path(self.settings.renderer_output_dir)
        self.work_dir = _resolve_runtime_path(self.settings.renderer_work_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def ready(self) -> dict[str, Any]:
        font_files = self.engine.reg.font.get("fonts", {}).get("PRETENDARD", {}).get(
            "files", {}
        )
        font_errors: list[str] = [] if font_files else ["PRETENDARD registry entry missing"]
        for weight, value in font_files.items():
            try:
                self.engine.reg.resolve_font("PRETENDARD", weight)
            except Exception:
                font_errors.append(str(value.get("path", weight)))
        return {
            "ready": not font_errors,
            "engine_root": str(self.engine_root),
            "font_errors": font_errors,
        }

    def render(self, request: RealsRenderJobRequest) -> dict[str, Any]:
        self._verify_registry(request)
        output_name = f"{hashlib.sha256(request.job_id.encode()).hexdigest()[:32]}.mp4"
        output_path = self.output_dir / output_name

        with self._lock, TemporaryDirectory(
            prefix="reals-job-", dir=self.work_dir
        ) as temporary:
            job_dir = Path(temporary)
            sources = self._download_sources(request, job_dir)
            produced = self._prepare_produced_video(request, sources, job_dir)
            edit_recipe = _fit_recipe_to_produced_duration(
                request.final_render.edit_recipe,
                duration_ms=produced.duration_ms,
                fps=produced.fps,
                assembly_segment_count=(
                    len(request.source_assembly.segments)
                    if request.source_assembly is not None
                    else None
                ),
            )
            final_request = self.native.FinalRenderRequest.model_validate(
                {
                    "job_id": request.job_id,
                    "execution_mode": "FINAL_RENDER",
                    "idempotency_key": request.idempotency_key,
                    "produced_video": produced.model_dump(mode="json"),
                    "source_mode": request.final_render.source_mode,
                    "edit_recipe": edit_recipe.model_dump(mode="json"),
                    "template_bundle_id": request.final_render.template_bundle_id,
                }
            )
            candidate_output = job_dir / "final.mp4"
            result = self.engine.final_render(
                final_request,
                out_path=str(candidate_output),
            )
            if result.deliverable and result.render_manifest is not None:
                candidate_output.replace(output_path)
                result.render_manifest.output_file.path = str(output_path)

        payload = result.model_dump(mode="json")
        if not result.deliverable or result.render_manifest is None:
            payload.update(
                {
                    "code": (
                        "REALS_RENDER_FAILED"
                        if str(result.status) == "FAILED"
                        else "REALS_QC_NOT_DELIVERABLE"
                    ),
                    "retryable": str(result.status) == "FAILED",
                    "validation_errors": _qc_details(payload),
                }
            )
            return payload

        rendered = result.render_manifest.output_file
        expires = int(time.time()) + self.settings.renderer_file_url_ttl_seconds
        signature = sign_renderer_file(output_name, expires)
        payload["render"] = {
            "output_video_url": (
                f"{self.settings.renderer_public_base_url.rstrip('/')}/files/"
                f"{quote(output_name)}?{urlencode({'expires': expires, 'signature': signature})}"
            ),
            "resolution": f"{rendered.width}x{rendered.height}",
            "duration_sec": rendered.duration_ms / 1000,
            "cover_image_url": None,
        }
        return payload

    def _verify_registry(self, request: RealsRenderJobRequest) -> None:
        manifest = self.engine_root / "registry" / "manifest.json"
        if not manifest.is_file():
            raise RendererServiceError(
                "The REALS registry manifest is missing.",
                code="REALS_REGISTRY_MISSING",
                status_code=503,
                retryable=True,
            )
        actual = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
        if actual != request.registry_manifest_sha256:
            raise RendererServiceError(
                "The AI worker and renderer use different REALS registry bundles.",
                code="REALS_REGISTRY_MISMATCH",
                status_code=409,
            )

    def _download_sources(
        self,
        request: RealsRenderJobRequest,
        job_dir: Path,
    ) -> dict[str, Any]:
        sources: dict[str, Any] = {}
        for asset in request.source_assets:
            parsed = urlparse(asset.asset_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise RendererServiceError(
                    f"Invalid source asset URL for {asset.file_id}.",
                    code="REALS_ASSET_URL_INVALID",
                    status_code=422,
                )
            target = job_dir / (
                hashlib.sha256(asset.file_id.encode()).hexdigest()[:24] + ".mp4"
            )
            try:
                download_source_asset(
                    asset.asset_url,
                    target,
                    max_bytes=self.settings.renderer_max_download_bytes,
                    timeout_seconds=self.settings.renderer_download_timeout_seconds,
                )
            except SourceAssetTooLargeError as exc:
                raise RendererServiceError(
                    f"Source asset {asset.file_id} exceeds the download limit.",
                    code="REALS_ASSET_TOO_LARGE",
                    status_code=413,
                ) from exc
            except (SourceAssetDownloadError, OSError) as exc:
                raise RendererServiceError(
                    f"Could not download source asset {asset.file_id}.",
                    code="REALS_ASSET_DOWNLOAD_FAILED",
                    status_code=502,
                    retryable=True,
                ) from exc

            try:
                media = self.native.media_ref(asset.file_id, target)
            except Exception as exc:
                raise RendererServiceError(
                    f"Source asset {asset.file_id} is not a readable video.",
                    code="REALS_ASSET_INVALID",
                    status_code=422,
                ) from exc
            _verify_media_metadata(asset, media)
            sources[asset.file_id] = media
        return sources

    def _prepare_produced_video(
        self,
        request: RealsRenderJobRequest,
        sources: dict[str, Any],
        job_dir: Path,
    ) -> Any:
        plan = request.source_assembly
        expected = request.final_render.produced_video
        if plan is None:
            try:
                produced = sources[expected.file_id]
            except KeyError as exc:
                raise RendererServiceError(
                    "ONE_TAKE_PASSTHROUGH does not reference a downloaded source asset.",
                    code="REALS_SOURCE_REFERENCE_INVALID",
                    status_code=422,
                ) from exc
            _verify_media_metadata(expected, produced)
            return produced

        ordered = sorted(plan.segments, key=lambda segment: segment.sequence_index)
        indices = [segment.sequence_index for segment in ordered]
        if indices != list(range(1, len(ordered) + 1)):
            raise RendererServiceError(
                "source_assembly sequence_index must be consecutive from 1.",
                code="REALS_ASSEMBLY_ORDER_INVALID",
                status_code=422,
            )

        inputs: list[tuple[str, float, float]] = []
        for segment in ordered:
            try:
                source = sources[segment.source_file_id]
            except KeyError as exc:
                raise RendererServiceError(
                    f"Assembly source {segment.source_file_id} was not downloaded.",
                    code="REALS_SOURCE_REFERENCE_INVALID",
                    status_code=422,
                ) from exc
            if segment.trim_out_ms > source.duration_ms:
                raise RendererServiceError(
                    f"Assembly trim exceeds {segment.source_file_id} duration.",
                    code="REALS_ASSEMBLY_TRIM_INVALID",
                    status_code=422,
                )
            inputs.append(
                (
                    source.path,
                    segment.trim_in_ms / 1000,
                    segment.trim_out_ms / 1000,
                )
            )

        try:
            profile = self.engine.reg.render_profile(plan.output_profile_id)
            output = job_dir / "assembled.mp4"
            commands, temporary_files = self.native.build_concat_plan(
                inputs,
                str(output),
                profile,
                str(job_dir),
                key=hashlib.sha256(request.job_id.encode()).hexdigest()[:16],
            )
            try:
                for command in commands:
                    self.native.run(command, timeout=900)
            finally:
                for path in temporary_files:
                    Path(path).unlink(missing_ok=True)
            produced = self.native.media_ref(plan.output_file_id, output)
        except RendererServiceError:
            raise
        except Exception as exc:
            raise RendererServiceError(
                "REALS source assembly failed.",
                code="REALS_ASSEMBLY_FAILED",
                status_code=500,
                retryable=True,
            ) from exc
        _verify_media_metadata(
            expected,
            produced,
            duration_tolerance_ms=_assembly_duration_tolerance_ms(
                duration_ms=expected.duration_ms,
                fps=expected.fps,
                segment_count=len(ordered),
            ),
        )
        return produced


def _verify_media_metadata(
    expected: Any,
    actual: Any,
    *,
    duration_tolerance_ms: int | None = None,
) -> None:
    expected_sha256 = str(getattr(expected, "sha256", ""))
    if expected_sha256 and expected_sha256 != actual.sha256:
        raise RendererServiceError(
            f"Source checksum mismatch for {expected.file_id}.",
            code="REALS_ASSET_CHECKSUM_MISMATCH",
            status_code=409,
        )
    duration_tolerance = (
        max(250, int(expected.duration_ms * 0.02))
        if duration_tolerance_ms is None
        else duration_tolerance_ms
    )
    mismatches: list[dict[str, Any]] = []
    if abs(expected.duration_ms - actual.duration_ms) > duration_tolerance:
        mismatches.append(
            {
                "field": "duration_ms",
                "expected": expected.duration_ms,
                "actual": actual.duration_ms,
                "tolerance": duration_tolerance,
            }
        )
    if expected.width != actual.width:
        mismatches.append(
            {"field": "width", "expected": expected.width, "actual": actual.width}
        )
    if expected.height != actual.height:
        mismatches.append(
            {"field": "height", "expected": expected.height, "actual": actual.height}
        )
    if abs(expected.fps - actual.fps) > 0.1:
        mismatches.append(
            {
                "field": "fps",
                "expected": expected.fps,
                "actual": actual.fps,
                "tolerance": 0.1,
            }
        )
    if mismatches:
        summary = ", ".join(
            f"{item['field']} expected={item['expected']} actual={item['actual']}"
            for item in mismatches
        )
        raise RendererServiceError(
            (
                f"Source metadata changed for {expected.file_id} after video analysis: "
                f"{summary}."
            ),
            code="REALS_ASSET_METADATA_MISMATCH",
            status_code=409,
            details=mismatches,
        )


def _assembly_duration_tolerance_ms(
    *,
    duration_ms: int,
    fps: float,
    segment_count: int,
) -> int:
    """Allow only the frame quantization introduced by CFR source assembly."""

    frame_ms = max(1, math.ceil(1000 / fps))
    boundary_drift_ms = (max(1, segment_count) * 2 + 2) * frame_ms
    return max(250, int(duration_ms * 0.02), boundary_drift_ms)


def _fit_recipe_to_produced_duration(
    recipe: Any,
    *,
    duration_ms: int,
    fps: float,
    assembly_segment_count: int | None = None,
) -> Any:
    """Clamp frame-sized assembly drift before the native recipe validator runs.

    FFmpeg encodes an assembled CFR asset on frame boundaries, so its probed
    duration can be a few milliseconds shorter than the sum of the requested
    source trims. Larger overruns remain untouched and are rejected by the
    native validator as genuine recipe errors.
    """
    tolerance_ms = (
        max(1, math.ceil(1000 / fps))
        if assembly_segment_count is None
        else _assembly_duration_tolerance_ms(
            duration_ms=duration_ms,
            fps=fps,
            segment_count=assembly_segment_count,
        )
    )
    fitted_segments = []
    adjusted_ends: dict[str, int] = {}

    for segment in recipe.segments:
        overrun_ms = segment.trim_out_ms - duration_ms
        if 0 < overrun_ms <= tolerance_ms and segment.trim_in_ms < duration_ms:
            segment = segment.model_copy(update={"trim_out_ms": duration_ms})
            adjusted_ends[segment.produced_segment_id] = duration_ms
        fitted_segments.append(segment)

    fitted_overlays = []
    for overlay in recipe.overlays:
        segment_end_ms = adjusted_ends.get(overlay.produced_segment_id)
        if (
            segment_end_ms is not None
            and overlay.end_ms > segment_end_ms
            and overlay.start_ms < segment_end_ms
        ):
            overlay = overlay.model_copy(update={"end_ms": segment_end_ms})
        fitted_overlays.append(overlay)

    return recipe.model_copy(
        update={"segments": fitted_segments, "overlays": fitted_overlays}
    )


def _qc_details(payload: dict[str, Any]) -> list[dict[str, Any]]:
    qc = payload.get("qc")
    checks = qc.get("checks") if isinstance(qc, dict) else []
    return [item for item in checks if isinstance(item, dict)]


def _resolve_engine_root(configured: Path) -> Path:
    if configured.is_absolute():
        root = configured
    else:
        repository_root = Path(__file__).resolve().parents[2]
        candidates = (Path.cwd() / configured, repository_root / configured)
        root = next((candidate for candidate in candidates if candidate.is_dir()), candidates[-1])
    if not (root / "reals_edit_engine").is_dir():
        raise RendererServiceError(
            f"REALS engine package is missing: {root}",
            code="REALS_ENGINE_MISSING",
            status_code=503,
            retryable=True,
        )
    return root.resolve()


def _resolve_runtime_path(configured: Path) -> Path:
    return configured if configured.is_absolute() else (Path.cwd() / configured).resolve()


@lru_cache(maxsize=4)
def _load_native_reals(engine_root: Path) -> NativeRealsModules:
    root = str(engine_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from reals_edit_engine import VideoEditEngine
        from reals_edit_engine.contracts import FinalRenderRequest
        from reals_edit_engine.ffmpeg_graph import build_concat_plan
        from reals_edit_engine.media import media_ref, run
    except ImportError as exc:
        raise RendererServiceError(
            "REALS engine dependencies are not installed.",
            code="REALS_ENGINE_DEPENDENCY_MISSING",
            status_code=503,
            retryable=True,
        ) from exc
    return NativeRealsModules(
        VideoEditEngine=VideoEditEngine,
        FinalRenderRequest=FinalRenderRequest,
        build_concat_plan=build_concat_plan,
        media_ref=media_ref,
        run=run,
    )


@lru_cache(maxsize=1)
def get_renderer_service() -> RealsRendererService:
    return RealsRendererService()
