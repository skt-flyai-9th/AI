"""SFX 파이프라인 — intent → resolve → asset (구현 문서 20).

역할 분리:
  GPT: intent + 시간 + 강도만 출력
  Resolver: intent를 실제 자산으로 resolve (Provider 어댑터)
  실패: SILENT fallback (사용자 의도를 바꾸는 판단 아님 — 승인된 정책)
Mock Provider는 lavfi로 짧은 신호음을 합성한다. 실구현에서 Epidemic Sound
Partner API 어댑터로 교체하며 인터페이스는 동일하다.
"""
from __future__ import annotations
import pathlib
from dataclasses import dataclass

from .media import FFMPEG, run


@dataclass
class ResolvedSfx:
    intent_id: str
    asset_path: str
    duration_ms: int
    provider: str
    provider_asset_id: str
    license_ref: str


class SfxResolveError(Exception):
    pass


class SfxResolver:
    def resolve(self, intent_id: str, strength: str, workdir: str) -> ResolvedSfx:
        raise NotImplementedError


class MockSynthSfxResolver(SfxResolver):
    """합성 신호음 Provider — 라이선스 이슈 없는 데모/테스트 자산."""

    RECIPES = {
        # intent: (표현식, 길이 s) — 짧은 UI성 사운드
        "TEXT_POP":        ("sine=frequency=1245:duration=0.14", 0.14),
        "PRODUCT_REVEAL":  ("sine=frequency=660:duration=0.30", 0.30),
        "CTA_APPEAR":      ("sine=frequency=880:duration=0.16,asetpts=PTS-STARTPTS", 0.34),
        "FAST_TRANSITION": ("anoisesrc=color=white:duration=0.12:amplitude=0.25", 0.12),
        "RESULT_REVEAL":   ("sine=frequency=990:duration=0.25", 0.25),
    }
    GAIN = {"LIGHT": 0.35, "MEDIUM": 0.6, "STRONG": 0.85}

    def resolve(self, intent_id, strength, workdir):
        if intent_id not in self.RECIPES:
            raise SfxResolveError(f"resolve 불가 intent: {intent_id}")
        expr, dur = self.RECIPES[intent_id]
        out = pathlib.Path(workdir) / f"sfx_{intent_id.lower()}.wav"
        gain = self.GAIN.get(strength, 0.35)
        if intent_id == "CTA_APPEAR":   # 2음 상승
            filt = (f"sine=frequency=740:duration=0.15[a];"
                    f"sine=frequency=988:duration=0.18[b];"
                    f"[a][b]concat=n=2:v=0:a=1,"
                    f"volume={gain},afade=t=out:st=0.24:d=0.09,"
                    f"aresample=48000,aformat=channel_layouts=stereo[sfxout]")
            cmd = [FFMPEG, "-hide_banner", "-y",
                   "-filter_complex", filt, "-map", "[sfxout]",
                   "-t", "0.34", "-c:a", "pcm_s16le", str(out)]
        else:
            cmd = [FFMPEG, "-hide_banner", "-y", "-f", "lavfi", "-i", expr,
                   "-af", f"volume={gain},afade=t=in:st=0:d=0.01,"
                          f"afade=t=out:st={max(dur-0.05,0.01)}:d=0.05,"
                          f"aresample=48000,aformat=channel_layouts=stereo",
                   "-c:a", "pcm_s16le", str(out)]
        run(cmd, timeout=60)
        return ResolvedSfx(intent_id=intent_id, asset_path=str(out),
                           duration_ms=int(dur * 1000), provider="MOCK_SYNTH",
                           provider_asset_id=f"mock:{intent_id}",
                           license_ref="synthetic-internal-demo")


class FailingSfxResolver(SfxResolver):
    """Provider 장애 시뮬레이션 — SILENT fallback 경로 테스트용."""
    def resolve(self, intent_id, strength, workdir):
        raise SfxResolveError("provider unavailable (simulated)")
