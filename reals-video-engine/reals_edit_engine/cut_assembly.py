"""CUT_ASSEMBLY — READ-1: 분석 → 앞뒤 트림 → 순서 보존 결합 (구현 문서 12).

금지(12.6): 게시글·음원·최종 자막·SFX·개인화·순서 재배열·주요 동작 중간 절단.
Mock segmenter는 Pegasus/MediaPipe adapter와 동일 인터페이스를 갖는다.
"""
from __future__ import annotations
import pathlib
import uuid
from dataclasses import dataclass

from .contracts import (CutAssemblyRequest, CutDecision, CutItemDecision, CutManifest,
                        QcStatus)
from .ffmpeg_graph import build_concat_plan
from .media import media_ref, probe, run
from .qc import intermediate_qc
from .registries import ENGINE_VERSION, Registries


@dataclass
class SegmenterFinding:
    raw_cut_file_id: str
    mapped_guide_segment_id: str
    confidence: float
    lead_dead_ms: int          # 시작부 명백한 대기 구간
    tail_dead_ms: int
    evidence: str = ""


class SemanticSegmenter:
    """실구현: TwelveLabs Pegasus 1.5 + OpenCV + MediaPipe adapter."""
    name = "abstract"

    def analyze(self, req: CutAssemblyRequest) -> list[SegmenterFinding]:
        raise NotImplementedError


class MockSegmenter(SemanticSegmenter):
    """구성값 기반 결정론 Mock. findings 미지정 컷은 conf 0.9·트림 0."""
    name = "mock_segmenter/1.0"

    def __init__(self, findings: dict[str, dict] | None = None):
        self._cfg = findings or {}

    def analyze(self, req):
        out = []
        guide_ids = [g.guide_template_segment_id for g in req.guide_segments] or [""]
        for i, cut in enumerate(sorted(req.raw_cuts, key=lambda c: c.capture_sequence_index)):
            cfg = self._cfg.get(cut.raw_cut_file_id, {})
            out.append(SegmenterFinding(
                raw_cut_file_id=cut.raw_cut_file_id,
                mapped_guide_segment_id=cfg.get("guide_id", guide_ids[min(i, len(guide_ids) - 1)]),
                confidence=cfg.get("confidence", 0.90),
                lead_dead_ms=cfg.get("lead_dead_ms", 0),
                tail_dead_ms=cfg.get("tail_dead_ms", 0),
                evidence=cfg.get("evidence", "mock 판정"),
            ))
        return out


class CutAssemblyError(Exception):
    pass


def run_cut_assembly(req: CutAssemblyRequest, reg: Registries,
                     segmenter: SemanticSegmenter, workdir: str) -> CutManifest:
    pol = reg.edit_policies["cut_assembly"]
    if not req.flow_lock or req.policies.reorder_allowed:
        raise CutAssemblyError("flow_lock 필수·reorder 금지 — 요청 거부")

    cuts = sorted(req.raw_cuts, key=lambda c: c.capture_sequence_index)
    if not cuts:
        raise CutAssemblyError("raw_cuts가 비어 있습니다. 입력 촬영 클립이 필요합니다.")

    idx = [c.capture_sequence_index for c in cuts]
    if len(set(idx)) != len(idx):
        raise CutAssemblyError(f"capture_sequence_index 중복: {idx}")

    findings = {f.raw_cut_file_id: f for f in segmenter.analyze(req)}

    # 가이드 매핑 단조성 검사 (12.4)
    guide_order = {g.guide_template_segment_id: g.guide_sequence_index
                   for g in req.guide_segments}
    prev = -1
    for c in cuts:
        f = findings.get(c.raw_cut_file_id)
        gi = guide_order.get(f.mapped_guide_segment_id, prev) if f else prev
        if gi < prev:
            raise CutAssemblyError(
                f"가이드 매핑 순서 충돌: {c.raw_cut_file_id} — 자동 재배열 금지, 실패 처리")
        prev = gi

    items: list[CutItemDecision] = []
    concat_inputs: list[tuple[str, float, float]] = []
    warnings: list[str] = []
    expected_ms = 0

    for out_i, c in enumerate(cuts, start=1):
        f = findings[c.raw_cut_file_id]
        dur = c.file.duration_ms
        lead, tail = f.lead_dead_ms, f.tail_dead_ms

        if not req.policies.edge_trim_allowed:
            lead = tail = 0
        # 신뢰도 정책 (12.4)
        if f.confidence >= pol["confidence_auto"]:
            reason = "자동 트림 허용 구간"
        elif f.confidence >= pol["confidence_limited"]:
            lim = pol["limited_max_trim_ms"]
            if lead > lim or tail > lim:
                lead, tail = min(lead, lim), min(tail, lim)
                warnings.append(f"{c.raw_cut_file_id}: 중간 신뢰도 — 트림 {lim}ms로 제한")
            reason = "중간 신뢰도 — 명백한 앞뒤 대기만 제한 트림"
        else:
            lead = tail = 0
            reason = f"낮은 신뢰도({f.confidence:.2f}) — {pol['low_confidence_fallback']}"
            warnings.append(f"{c.raw_cut_file_id}: 신뢰도 {f.confidence:.2f} < "
                            f"{pol['confidence_limited']} — 원본 전체 유지")
        lead = min(lead, pol["max_edge_trim_ms"])
        tail = min(tail, pol["max_edge_trim_ms"])
        t_in, t_out = lead, dur - tail
        if t_out - t_in < 200:
            raise CutAssemblyError(f"{c.raw_cut_file_id}: 트림 후 길이 부족")

        decision = (CutDecision.KEEP_FULL_CUT if f.confidence < pol["confidence_limited"]
                    else CutDecision.TRIM if (lead or tail) else CutDecision.KEEP)
        items.append(CutItemDecision(
            raw_cut_file_id=c.raw_cut_file_id,
            capture_sequence_index=c.capture_sequence_index,
            decision=decision, trim_in_ms=t_in, trim_out_ms=t_out,
            output_sequence_index=out_i,
            mapped_guide_segment_id=f.mapped_guide_segment_id,
            confidence=f.confidence, decision_reason=reason))
        concat_inputs.append((c.file.path, t_in / 1000.0, t_out / 1000.0))
        expected_ms += t_out - t_in

    # 순서 보존 최종 검증 — 결합 직전 invariant
    seqs = [(it.capture_sequence_index, it.output_sequence_index) for it in items]
    if [s[1] for s in seqs] != sorted(s[1] for s in seqs) or \
       [s[0] for s in seqs] != sorted(s[0] for s in seqs):
        raise CutAssemblyError(f"순서 보존 위반: {seqs}")

    rp = reg.render_profile(req.output_profile_id)
    out_path = pathlib.Path(workdir) / f"assembled_{req.shoot_session_id}.mp4"
    cmds, temps = build_concat_plan(concat_inputs, str(out_path), rp, workdir,
                                    key=req.shoot_session_id)
    try:
        for cmd in cmds:
            run(cmd, timeout=900)
    finally:
        for t in temps:
            try:
                pathlib.Path(t).unlink(missing_ok=True)
            except Exception:
                pass

    qc = intermediate_qc(str(out_path), expected_ms, rp)
    return CutManifest(
        cut_manifest_id=f"cutman_{uuid.uuid4().hex[:8]}",
        job_id=req.job_id, shoot_session_id=req.shoot_session_id,
        flow_preserved=True, items=items,
        assembled_file=media_ref(f"asm_{req.shoot_session_id}", out_path),
        edit_engine_version=ENGINE_VERSION,
        segmenter_runs=[{"adapter": segmenter.name,
                         "cuts": len(cuts)}],
        warnings=warnings, qc_status=qc.status)
