"""READ-1 의미 구간 분석 — 하이브리드 어댑터 (구현 문서 12.3).

동일 인터페이스(SemanticSegmenter.analyze → list[SegmenterFinding])로:
  MotionQualitySegmenter : OpenCV 결정론 분석. 항상 사용 가능한 기반층
  LocalVlmSegmenter      : Qwen3-VL (4090 로컬). 가이드 구간 의미 매핑
  PegasusSegmenter       : TwelveLabs Pegasus 클라우드
  HybridSemanticSegmenter: 우선순위 체인 + Motion 신호 병합

의미 매핑(어느 컷이 가이드의 몇 번 장면인가)은 VLM/Pegasus가,
대기 구간·품질 신뢰도는 Motion 분석이 담당한다. 둘을 곱해 최종 confidence를 낸다.
"""
from __future__ import annotations
import json, os
from dataclasses import dataclass

from ..cut_assembly import SegmenterFinding, SemanticSegmenter
from .device import cuda_available, device_str, free_cuda
from .frames import sample_frames
from .quality import analyze_motion, dead_edges_ms, quality_confidence


class SegmenterUnavailable(RuntimeError):
    pass


# ── 1. 기반층: OpenCV 모션·품질 (항상 동작) ──────────────────────────
class MotionQualitySegmenter(SemanticSegmenter):
    name = "opencv/motion-quality"

    def __init__(self, max_edge_ms: int = 1500):
        self.max_edge_ms = max_edge_ms

    def analyze(self, req) -> list[SegmenterFinding]:
        guide_ids = [g.guide_template_segment_id for g in req.guide_segments] or [""]
        out = []
        cuts = sorted(req.raw_cuts, key=lambda c: c.capture_sequence_index)
        for i, cut in enumerate(cuts):
            mp_ = analyze_motion(cut.file.path)
            lead, tail = dead_edges_ms(mp_, max_edge_ms=self.max_edge_ms)
            conf = quality_confidence(mp_)
            out.append(SegmenterFinding(
                raw_cut_file_id=cut.raw_cut_file_id,
                mapped_guide_segment_id=guide_ids[min(i, len(guide_ids) - 1)],
                confidence=conf, lead_dead_ms=lead, tail_dead_ms=tail,
                evidence=(f"motion: lead {lead}ms/tail {tail}ms 정지, "
                          f"sharp={mp_.mean_sharpness:.0f} "
                          f"bright={mp_.mean_brightness:.2f} shake={mp_.shake:.4f}")))
        return out


# ── 2. 로컬 VLM (Qwen3-VL on 4090) ───────────────────────────────────
VLM_SYSTEM = (
    "너는 숏폼 촬영본 분석기다. 주어진 프레임들은 사용자가 촬영한 하나의 컷이다. "
    "가이드 장면 목록 중 이 컷이 어떤 장면인지 고르고, 실제 동작이 시작되기 전 "
    "대기 시간과 끝난 뒤 여유 시간을 밀리초로 추정하라. "
    "반드시 JSON만 출력한다: "
    '{"guide_segment_id":str,"confidence":0~1,"lead_dead_ms":int,'
    '"tail_dead_ms":int,"evidence":str}')


class LocalVlmSegmenter(SemanticSegmenter):
    """Qwen3-VL 계열 로컬 추론. 설치: pip install transformers accelerate qwen-vl-utils

    16GB(4090 Laptop)에서 실제로 뜨는 조합 — AWQ 변형은 공식 배포에 없다:
      기본  Qwen/Qwen3-VL-4B-Instruct       bf16 ≈8GB   가장 안전
      상위  Qwen/Qwen3-VL-8B-Instruct-FP8   ≈9GB        Ada(sm_89) FP8 지원 필요
      불가  Qwen/Qwen3-VL-8B-Instruct       bf16 ≈17GB  16GB에 안 올라감
    """
    name = "qwen3-vl/local"

    def __init__(self, model_id: str = "Qwen/Qwen3-VL-4B-Instruct",
                 frames_per_cut: int = 8, max_new_tokens: int = 256,
                 dtype: str = "auto"):
        self.model_id = model_id
        self.frames_per_cut = frames_per_cut
        self.max_new_tokens = max_new_tokens
        self.dtype = dtype
        self._m = self._p = None

    def _lazy(self):
        if self._m is None:
            if not cuda_available():
                raise SegmenterUnavailable("로컬 VLM은 CUDA 필요")
            try:
                from transformers import (AutoProcessor,
                                          AutoModelForImageTextToText)
            except Exception as e:
                raise SegmenterUnavailable(f"transformers 미설치: {e}")
            self._p = AutoProcessor.from_pretrained(self.model_id)
            self._m = AutoModelForImageTextToText.from_pretrained(
                self.model_id, dtype=self.dtype, device_map="cuda").eval()
        return self._m, self._p

    def analyze(self, req) -> list[SegmenterFinding]:
        import PIL.Image
        import cv2
        model, proc = self._lazy()
        guide_desc = [
            {"id": g.guide_template_segment_id, "seq": g.guide_sequence_index,
             "summary": g.scene_summary, "required": g.required_for_challenge}
            for g in req.guide_segments]
        out = []
        for cut in sorted(req.raw_cuts, key=lambda c: c.capture_sequence_index):
            dur = cut.file.duration_ms
            frames = sample_frames(cut.file.path, [(0, dur)],
                                   per_window=self.frames_per_cut,
                                   preview_long_side=448)
            imgs = [PIL.Image.fromarray(cv2.cvtColor(f.bgr, cv2.COLOR_BGR2RGB))
                    for f in frames]
            stamps = [f.t_ms for f in frames]
            user = (f"컷 길이: {dur}ms\n프레임 시각(ms): {stamps}\n"
                    f"가이드 장면 목록: {json.dumps(guide_desc, ensure_ascii=False)}")
            msgs = [{"role": "system", "content": [{"type": "text", "text": VLM_SYSTEM}]},
                    {"role": "user", "content": [{"type": "image", "image": im}
                                                 for im in imgs]
                                                + [{"type": "text", "text": user}]}]
            inputs = proc.apply_chat_template(
                msgs, add_generation_prompt=True, tokenize=True,
                return_dict=True, return_tensors="pt").to(model.device)
            ids = model.generate(**inputs, max_new_tokens=self.max_new_tokens,
                                 do_sample=False)
            text = proc.batch_decode(ids[:, inputs["input_ids"].shape[1]:],
                                     skip_special_tokens=True)[0]
            out.append(_parse_finding(cut, text, dur))
        return out

    def close(self):
        self._m = self._p = None
        free_cuda()


# ── 3. TwelveLabs Pegasus (클라우드) ─────────────────────────────────
class PegasusSegmenter(SemanticSegmenter):
    """설치: pip install twelvelabs. TWELVELABS_API_KEY 환경변수 필요."""
    name = "twelvelabs/pegasus"

    def __init__(self, index_id: str | None = None, api_key: str | None = None):
        self.index_id = index_id or os.environ.get("TWELVELABS_INDEX_ID")
        self.api_key = api_key or os.environ.get("TWELVELABS_API_KEY")
        self._c = None

    def _lazy(self):
        if self._c is None:
            if not self.api_key:
                raise SegmenterUnavailable("TWELVELABS_API_KEY 미설정")
            try:
                from twelvelabs import TwelveLabs
            except Exception as e:
                raise SegmenterUnavailable(f"twelvelabs SDK 미설치: {e}")
            self._c = TwelveLabs(api_key=self.api_key)
        return self._c

    def analyze(self, req) -> list[SegmenterFinding]:
        client = self._lazy()
        guide_desc = [{"id": g.guide_template_segment_id,
                       "seq": g.guide_sequence_index,
                       "summary": g.scene_summary} for g in req.guide_segments]
        out = []
        for cut in sorted(req.raw_cuts, key=lambda c: c.capture_sequence_index):
            task = client.task.create(index_id=self.index_id,
                                      file=cut.file.path)
            task.wait_for_done(sleep_interval=3)
            prompt = (VLM_SYSTEM + f"\n컷 길이: {cut.file.duration_ms}ms\n"
                      f"가이드 장면: {json.dumps(guide_desc, ensure_ascii=False)}")
            res = client.generate.text(video_id=task.video_id, prompt=prompt)
            out.append(_parse_finding(cut, res.data, cut.file.duration_ms))
        return out


def _parse_finding(cut, text: str, dur_ms: int) -> SegmenterFinding:
    body = text.strip()
    if "```" in body:
        body = body.split("```")[1].lstrip("json").strip()
    start, end = body.find("{"), body.rfind("}")
    try:
        d = json.loads(body[start:end + 1])
    except Exception:
        d = {}
    return SegmenterFinding(
        raw_cut_file_id=cut.raw_cut_file_id,
        mapped_guide_segment_id=str(d.get("guide_segment_id", "")),
        confidence=float(d.get("confidence", 0.0)),
        lead_dead_ms=int(max(0, min(d.get("lead_dead_ms", 0), dur_ms // 3))),
        tail_dead_ms=int(max(0, min(d.get("tail_dead_ms", 0), dur_ms // 3))),
        evidence=str(d.get("evidence", ""))[:300])


# ── 4. 하이브리드 라우터 ─────────────────────────────────────────────
@dataclass
class HybridSemanticSegmenter(SemanticSegmenter):
    """의미 매핑은 primary 체인, 대기·품질은 Motion. confidence는 곱."""
    primary: SemanticSegmenter | None = None
    fallback: SemanticSegmenter | None = None
    motion: SemanticSegmenter | None = None
    warnings: list[str] = None
    name: str = "hybrid/semantic"

    @classmethod
    def default(cls, prefer: str = "auto", **kw) -> "HybridSemanticSegmenter":
        """prefer: 'local' | 'pegasus' | 'motion' | 'auto'"""
        primary = fallback = None
        if prefer in ("local", "auto") and cuda_available():
            primary = LocalVlmSegmenter()
            fallback = PegasusSegmenter() if os.environ.get("TWELVELABS_API_KEY") else None
        elif prefer == "pegasus":
            primary = PegasusSegmenter()
        return cls(primary=primary, fallback=fallback,
                   motion=MotionQualitySegmenter(), warnings=[], **kw)

    def analyze(self, req) -> list[SegmenterFinding]:
        self.warnings = []
        base = {f.raw_cut_file_id: f for f in (self.motion or
                                               MotionQualitySegmenter()).analyze(req)}
        sem: dict[str, SegmenterFinding] = {}
        for cand in (self.primary, self.fallback):
            if cand is None:
                continue
            try:
                sem = {f.raw_cut_file_id: f for f in cand.analyze(req)}
                self.warnings.append(f"semantic={cand.name}")
                break
            except Exception as e:
                self.warnings.append(f"{cand.name} 실패 → 폴백: {type(e).__name__}: {e}")
            finally:
                if hasattr(cand, "close"):
                    try:
                        cand.close()
                    except Exception:
                        pass
        if not sem:
            # 비판적 수정: 모션·품질 신뢰도는 "가이드 장면과 맞는가"를 말해주지
            # 않는다. 의미 매핑 없이는 자동 트림 티어(>=0.80)에 못 들어가게
            # 상한을 걸어 제한 트림(<=1s)까지만 허용한다.
            self.warnings.append("semantic=motion-only → confidence 상한 0.79 적용")
            capped = []
            for f in base.values():
                f.confidence = min(f.confidence, 0.79)
                capped.append(f)
            return capped

        merged = []
        for cid, b in base.items():
            s = sem.get(cid)
            if s is None:
                merged.append(b); continue
            # 대기 구간은 두 신호의 보수적 교집합(작은 쪽), 신뢰도는 곱
            merged.append(SegmenterFinding(
                raw_cut_file_id=cid,
                mapped_guide_segment_id=s.mapped_guide_segment_id or b.mapped_guide_segment_id,
                confidence=round(min(1.0, s.confidence * (0.5 + 0.5 * b.confidence)), 3),
                lead_dead_ms=min(s.lead_dead_ms, b.lead_dead_ms),
                tail_dead_ms=min(s.tail_dead_ms, b.tail_dead_ms),
                evidence=f"[semantic] {s.evidence} | [motion] {b.evidence}"))
        return merged
