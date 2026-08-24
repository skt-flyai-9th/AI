from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.agents.editing.reals import RealsRecipeAdapter
from app.core.config import Settings
from app.renderer import service as renderer_service_module
from app.renderer.service import NativeRealsModules, RealsRendererService
from tests.test_editing_agent import _recipe, _request
from tests.test_reals_editing_integration import _contexts, _template


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


class _StreamResponse:
    headers = {"content-length": "5"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @staticmethod
    def raise_for_status() -> None:
        return None

    @staticmethod
    def iter_bytes():
        yield b"video"


class _HttpClient:
    def __init__(self, captured: list[str], **_: Any) -> None:
        self.captured = captured

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def stream(self, method: str, url: str):
        assert method == "GET"
        self.captured.append(url)
        return _StreamResponse()


def test_renderer_service_downloads_assembles_and_invokes_final_render(tmp_path, monkeypatch):
    captured_downloads: list[str] = []
    monkeypatch.setattr(
        renderer_service_module.httpx,
        "Client",
        lambda **kwargs: _HttpClient(captured_downloads, **kwargs),
    )

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

    def build_concat_plan(inputs, output_path, profile, workdir, key):
        assert len(inputs) == 2
        assert profile["width"] == 1080
        assert workdir
        assert key
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
        template=_template(),
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
    assert {"/renders", "/files", "/health/live", "/health/ready"} <= paths


def test_renderer_builds_the_native_reals_final_render_contract(tmp_path):
    engine_root = Path("reals-video-engine").resolve()
    native = renderer_service_module._load_native_reals(engine_root)
    facade = RealsRecipeAdapter().build_request(
        run_id="native-contract-test",
        recipe=_recipe(),
        videos=_request().videos,
        video_contexts=_contexts(),
        template=_template(),
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
