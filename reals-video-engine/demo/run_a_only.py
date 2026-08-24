import pathlib, sys, uuid
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _d in (".work", "output"):
    (ROOT / _d).mkdir(exist_ok=True)   # _ensure_dirs
sys.path.insert(0, str(ROOT / "demo"))
from reals_edit_engine import VideoEditEngine
from reals_edit_engine.contracts import (AvoidMap, AvoidRegion, FinalRenderRequest,
                                          SourceMode)
from reals_edit_engine.media import media_ref
from reals_edit_engine.sfx import MockSynthSfxResolver
from reals_edit_engine.subtitle_layout import StaticAvoidMapProvider
from recipes import build_recipe_a

AVOID = AvoidMap(regions=[
    AvoidRegion(x=250, y=350, w=460, h=470, priority=100, label="FACE",
                start_ms=0, end_ms=18240),
    AvoidRegion(x=150, y=150, w=810, h=360, priority=90, label="EXISTING_TEXT_APT",
                start_ms=4000, end_ms=14500),
])
engine = VideoEditEngine(ROOT, sfx_resolver=MockSynthSfxResolver(),
                         avoid_provider=StaticAvoidMapProvider(AVOID))
produced = media_ref("prod_one_take_001", ROOT / ".work" / "produced_one_take.mp4")
res = engine.final_render(
    FinalRenderRequest(job_id=f"render_{uuid.uuid4().hex[:6]}",
                       produced_video=produced,
                       source_mode=SourceMode.ONE_TAKE_PASSTHROUGH,
                       edit_recipe=build_recipe_a(produced.duration_ms)),
    out_path=str(ROOT / "output" / "final_one_take_sfx.mp4"))
print(f"status={res.status} deliverable={res.deliverable}")
for c in (res.qc.checks if res.qc else []):
    print(f"  [{c.status.value:4}] {c.check_id}: {c.detail}")
if res.error: print("ERROR:", res.error[:600])
if res.render_manifest:
    (ROOT / "output" / "render_manifest_A.json").write_text(
        res.render_manifest.model_dump_json(indent=2))
    print("sfx_windows:", res.render_manifest.sfx_windows_ms)
