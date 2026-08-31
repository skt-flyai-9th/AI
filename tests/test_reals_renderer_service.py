from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from app.agents.editing.reals import RealsRecipeAdapter
from app.core.config import Settings
from app.renderer import service as renderer_service_module
from app.renderer.service import (
    NativeRealsModules,
    RealsRendererService,
    RendererServiceError,
    _assembly_duration_tolerance_ms,
    _fit_recipe_to_produced_duration,
    _verify_media_metadata,
)
from tests.test_editing_agent import _recipe, _request
from tests.test_reals_editing_integration import _contexts, _video_editing_db


class _MediaRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: str
    path: str
    sha256: str
    duration_ms: int
    width: int
    height: int
    fps: float


class _FinalRequest(BaseModel):
    job_id: str
    execution_mode: str
    idempotency_key: str
    produced_video: _MediaRef
    source_mode: str
    edit_recipe: dict[str, Any]
    template_bundle_id: str


class _FakeRegistry:
    font = {"fonts": {"PRETENDARD": {"files": {}}}}

    @staticmethod
    def render_profile(_: str) -> dict:
        return {"width": 1080, "height": 1920, "fps": 30}


class _FakeEngine:
    def __init__(self, _: Path) -> None:
        self.reg = _FakeRegistry()
        self.request = None
        self.input_existed = False

    def final_render(self, request: _FinalRequest, out_path: str):
        self.request = request
        self.input_existed = Path(request.produced_video.path).exists()
        Path(out_path).write_bytes(b"rendered-video")
        output = _MediaRef(
            file_id="final-output",
            path=out_path,
            sha256="sha256:output",
            duration_ms=4000,
            width=1080,
            height=1920,
            fps=30,
        )
        return _FakeEngineResult(output)


class _FakeEngineResult:
    def __init__(self, output: _MediaRef) -> None:
        self.deliverable = True
        self.render_manifest = SimpleNamespace(output_file=output)
        self._output = output

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return {
            "job_id": "renderer-test",
            "execution_mode": "FINAL_RENDER",
            "status": "COMPLETED",
            "deliverable": True,
            "render_manifest": {"output_file": self._output.model_dump(mode="json")},
            "qc": {"status": "PASS", "checks": []},
        }


def test_renderer_service_downloads_assembles_and_invokes_final_render(tmp_path, monkeypatch):
    captured_downloads: list[str] = []

    def download(url, target, **_kwargs):
        captured_downloads.append(url)
        target.write_bytes(b"video")

    monkeypatch.setattr(renderer_service_module, "download_source_asset", download)

    def media_ref(file_id: str, path: str | Path) -> _MediaRef:
        duration = 4000 if file_id.endswith("_produced") else 5000
        return _MediaRef(
            file_id=file_id,
            path=str(path),
            sha256=f"sha256:{file_id}",
            duration_ms=duration,
            width=1080,
            height=1920,
            fps=30,
        )

    def build_concat_plan(inputs, output_path, profile, workdir, key, *, keep_audio=True):
        assert len(inputs) == 2
        assert all(len(item) == 5 for item in inputs)
        assert [(item[3], item[4]) for item in inputs] == [(0.5, 0.5), (0.5, 0.5)]
        assert profile["width"] == 1080
        assert workdir
        assert key
        assert keep_audio is False
        Path(output_path).write_bytes(b"assembled-video")
        return [], []

    native = NativeRealsModules(
        VideoEditEngine=_FakeEngine,
        FinalRenderRequest=_FinalRequest,
        build_concat_plan=build_concat_plan,
        media_ref=media_ref,
        run=lambda *args, **kwargs: None,
    )
    settings = Settings(
        editing_reals_engine_path=Path("reals-video-engine"),
        renderer_output_dir=tmp_path / "output",
        renderer_work_dir=tmp_path / "work",
        renderer_public_base_url="https://media.example",
    )
    service = RealsRendererService(settings=settings, native=native)
    request = RealsRecipeAdapter().build_request(
        run_id="renderer-test",
        recipe=_recipe(),
        videos=_request().videos,
        video_contexts=_contexts(),
        video_editing_db=_video_editing_db(),
    )

    result = service.render(request)

    assert result["deliverable"] is True
    assert result["render"]["output_video_url"].startswith("https://media.example/files/")
    assert result["render"]["resolution"] == "1080x1920"
    assert result["render"]["duration_sec"] == 4
    assert captured_downloads == [video.footage_url for video in _request().videos]
    assert service.engine.input_existed is True
    assert service.engine.request.execution_mode == "FINAL_RENDER"
    assert service.engine.request.produced_video.path.endswith("assembled.mp4")
    assert service.engine.request.edit_recipe["bgm_policy"] == "NONE"


def test_renderer_app_exposes_real_render_route():
    from app.renderer.main import app

    paths = {route.path for route in app.routes}
    assert {"/renders", "/files/{filename:path}", "/health/live", "/health/ready"} <= paths


def test_renderer_files_require_internal_auth():
    from fastapi.testclient import TestClient

    from app.renderer import main as renderer_main
    from app.core.security import sign_renderer_file

    target = renderer_main.output_dir / "protected-test.mp4"
    target.write_bytes(b"video")
    try:
        with TestClient(renderer_main.app) as client:
            assert client.get("/files/protected-test.mp4").status_code == 401
            response = client.get(
                "/files/protected-test.mp4",
                headers={"X-Internal-API-Key": "test-token"},
            )
            assert response.status_code == 200
            expires = 4_102_444_800
            signature = sign_renderer_file("protected-test.mp4", expires)
            signed = client.get(
                f"/files/protected-test.mp4?expires={expires}&signature={signature}"
            )
            assert signed.status_code == 200
    finally:
        target.unlink(missing_ok=True)


def test_renderer_clamps_only_frame_sized_produced_duration_drift():
    facade = RealsRecipeAdapter().build_request(
        run_id="duration-drift-test",
        recipe=_recipe(),
        videos=_request().videos,
        video_contexts=_contexts(),
        video_editing_db=_video_editing_db(),
    )
    recipe = facade.final_render.edit_recipe
    original_end = recipe.segments[-1].trim_out_ms

    fitted = _fit_recipe_to_produced_duration(
        recipe,
        duration_ms=original_end - 4,
        fps=30,
    )

    assert fitted.segments[-1].trim_out_ms == original_end - 4
    assert recipe.segments[-1].trim_out_ms == original_end
    assert all(
        overlay.end_ms <= original_end - 4
        for overlay in fitted.overlays
        if overlay.produced_segment_id == fitted.segments[-1].produced_segment_id
    )


def test_renderer_leaves_large_duration_overrun_for_native_validation():
    facade = RealsRecipeAdapter().build_request(
        run_id="invalid-duration-test",
        recipe=_recipe(),
        videos=_request().videos,
        video_contexts=_contexts(),
        video_editing_db=_video_editing_db(),
    )
    recipe = facade.final_render.edit_recipe
    original_end = recipe.segments[-1].trim_out_ms

    fitted = _fit_recipe_to_produced_duration(
        recipe,
        duration_ms=original_end - 100,
        fps=30,
    )

    assert fitted.segments[-1].trim_out_ms == original_end


def test_renderer_clamps_accumulated_multi_cut_frame_drift():
    facade = RealsRecipeAdapter().build_request(
        run_id="multi-cut-duration-drift-test",
        recipe=_recipe(),
        videos=_request().videos,
        video_contexts=_contexts(),
        video_editing_db=_video_editing_db(),
    )
    recipe = facade.final_render.edit_recipe
    original_end = recipe.segments[-1].trim_out_ms

    fitted = _fit_recipe_to_produced_duration(
        recipe,
        duration_ms=original_end - 300,
        fps=30,
        assembly_segment_count=6,
    )

    assert fitted.segments[-1].trim_out_ms == original_end - 300
    assert all(
        overlay.end_ms <= original_end - 300
        for overlay in fitted.overlays
        if overlay.produced_segment_id == fitted.segments[-1].produced_segment_id
    )


def test_assembly_metadata_tolerance_scales_with_cut_boundaries():
    tolerance = _assembly_duration_tolerance_ms(
        duration_ms=12_000,
        fps=30,
        segment_count=6,
    )

    assert tolerance == 476

    expected = _MediaRef(
        file_id="assembled",
        path="expected.mp4",
        sha256="",
        duration_ms=12_000,
        width=1080,
        height=1920,
        fps=30,
    )
    actual = expected.model_copy(
        update={"path": "actual.mp4", "sha256": "sha256:actual", "duration_ms": 11_650}
    )

    _verify_media_metadata(expected, actual, duration_tolerance_ms=tolerance)


def test_assembly_metadata_still_rejects_non_frame_metadata_changes():
    expected = _MediaRef(
        file_id="assembled",
        path="expected.mp4",
        sha256="",
        duration_ms=12_000,
        width=1080,
        height=1920,
        fps=30,
    )
    actual = expected.model_copy(
        update={"path": "actual.mp4", "sha256": "sha256:actual", "width": 720}
    )

    with pytest.raises(RendererServiceError) as error:
        _verify_media_metadata(expected, actual, duration_tolerance_ms=476)

    exc = error.value
    assert exc.code == "REALS_ASSET_METADATA_MISMATCH"
    assert exc.details == [{"field": "width", "expected": 1080, "actual": 720}]
    assert "width expected=1080 actual=720" in str(exc)


def test_assembly_metadata_rejects_duration_drift_above_frame_tolerance():
    expected = _MediaRef(
        file_id="assembled",
        path="expected.mp4",
        sha256="",
        duration_ms=12_000,
        width=1080,
        height=1920,
        fps=30,
    )
    actual = expected.model_copy(
        update={"path": "actual.mp4", "sha256": "sha256:actual", "duration_ms": 11_500}
    )

    with pytest.raises(RendererServiceError) as error:
        _verify_media_metadata(expected, actual, duration_tolerance_ms=476)

    assert error.value.details == [
        {
            "field": "duration_ms",
            "expected": 12_000,
            "actual": 11_500,
            "tolerance": 476,
        }
    ]


def test_renderer_builds_the_native_reals_final_render_contract(tmp_path):
    engine_root = Path("reals-video-engine").resolve()
    native = renderer_service_module._load_native_reals(engine_root)
    facade = RealsRecipeAdapter().build_request(
        run_id="native-contract-test",
        recipe=_recipe(),
        videos=_request().videos,
        video_contexts=_contexts(),
        video_editing_db=_video_editing_db(),
    )
    produced_path = tmp_path / "assembled.mp4"
    produced_path.write_bytes(b"contract-only")

    native_request = native.FinalRenderRequest.model_validate(
        {
            "job_id": facade.job_id,
            "execution_mode": "FINAL_RENDER",
            "idempotency_key": facade.idempotency_key,
            "produced_video": {
                **facade.final_render.produced_video.model_dump(mode="json"),
                "path": str(produced_path),
                "sha256": "sha256:contract",
            },
            "source_mode": facade.final_render.source_mode,
            "edit_recipe": facade.final_render.edit_recipe.model_dump(mode="json"),
            "template_bundle_id": facade.final_render.template_bundle_id,
        }
    )

    assert native_request.execution_mode.value == "FINAL_RENDER"
    assert native_request.produced_video.path == str(produced_path)
    assert native_request.edit_recipe.recipe_id == "native-contract-test_recipe"


def test_cpu_encoder_honors_free_tier_preset_override(monkeypatch):
    renderer_service_module._load_native_reals(Path("reals-video-engine").resolve())
    from reals_edit_engine.ffmpeg_graph import video_encode_args

    monkeypatch.setenv("REALS_FFMPEG_PRESET_OVERRIDE", "veryfast")
    args = video_encode_args(
        {
            "video_codec": "libx264",
            "crf": 20,
            "preset": "medium",
            "x264_profile": "high",
            "level": "4.1",
            "gop": 60,
        }
    )

    assert args[args.index("-preset") + 1] == "veryfast"


def test_native_engine_artifact_key_is_ffmpeg_path_safe():
    renderer_service_module._load_native_reals(Path("reals-video-engine").resolve())
    from reals_edit_engine.engine import _artifact_key

    value = _artifact_key("editing:edit_123:recipe/hash with spaces")

    assert len(value) == 32
    assert value.isalnum()
    assert value == _artifact_key("editing:edit_123:recipe/hash with spaces")
