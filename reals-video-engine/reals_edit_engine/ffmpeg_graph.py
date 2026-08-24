"""FFmpeg filter graph 빌더 (구현 문서 21).

- 모델이 만든 값은 이미 Validator를 통과한 뒤에만 여기 도달한다.
- argv 배열 생성만 한다. shell 문자열 조립 금지.
- 렌더 순서(21.3): trim → concat → speed → zoom → color → caption →
  원음 제거(입력 자체를 안 씀) → SFX mix/silent → encode.
"""
from __future__ import annotations
import os
import pathlib

from .contracts import ColorTone, EditRecipe, FinalAudioPolicy
from .media import FFMPEG
from .sfx import ResolvedSfx


def video_encode_args(rp: dict) -> list[str]:
    """CPU(libx264) / GPU(h264_nvenc) 인코더 인자 분기."""
    codec = rp["video_codec"]
    if codec.endswith("_nvenc"):
        return [
            "-c:v", codec, "-preset", rp.get("preset", "p6"),
            "-rc", rp.get("rc", "vbr"), "-cq", str(rp.get("cq", 20)),
            "-b:v", rp.get("bitrate", "8M"), "-maxrate", rp.get("maxrate", "12M"),
            "-bufsize", rp.get("bufsize", "16M"),
            "-profile:v", rp["x264_profile"], "-level", rp["level"],
            "-g", str(rp["gop"]), "-bf", "3", "-spatial-aq", "1",
            *rp.get("extra_args", []),
        ]
    preset = os.environ.get("REALS_FFMPEG_PRESET_OVERRIDE", rp["preset"]).strip()
    return [
        "-c:v", codec, "-crf", str(rp["crf"]), "-preset", preset or rp["preset"],
        "-profile:v", rp["x264_profile"], "-level", rp["level"], "-g", str(rp["gop"]),
    ]

COLOR_EQ = {
    ColorTone.NATURAL: None,
    ColorTone.WARM: "eq=saturation=1.06:gamma_r=1.02:gamma_b=0.98",
    ColorTone.COOL: "eq=saturation=1.04:gamma_r=0.98:gamma_b=1.02",
    ColorTone.VIVID: "eq=contrast=1.06:saturation=1.14:brightness=0.01",
}


def _filter_path(p) -> str:
    """filter 옵션 값용 경로 이스케이프 (Windows 드라이브 콜론 포함)."""
    return str(p).replace("\\", "/").replace(":", "\\:")


def map_produced_to_output_ms(recipe: EditRecipe, produced_segment_id: str, t_ms: int) -> int:
    """produced video 타임스탬프 → 출력 타임라인 (트림·속도 반영)."""
    offset = 0.0
    for s in recipe.segments:
        seg_out = (s.trim_out_ms - s.trim_in_ms) / s.speed_multiplier
        if s.produced_segment_id == produced_segment_id:
            t = min(max(t_ms, s.trim_in_ms), s.trim_out_ms)
            return int(round(offset + (t - s.trim_in_ms) / s.speed_multiplier))
        offset += seg_out
    raise KeyError(produced_segment_id)


def _segment_filters(s, rp) -> list[str]:
    """구간 하나에 적용할 필터 체인 (마감 필터·자막 제외)."""
    fps = rp["fps"]
    chain = ["setpts=PTS-STARTPTS"]
    if s.transition_id.value == "FLASH_WHITE":
        chain.append("fade=t=in:st=0:d=0.10:color=white")
    if s.speed_multiplier != 1.0:
        chain.append(f"setpts=PTS/{s.speed_multiplier:.6f}")
    dur = (s.trim_out_ms - s.trim_in_ms) / 1000.0
    for eff in s.effects:
        if eff.effect_id == "SMOOTH_ZOOM":
            z = float(eff.params.get("scale_end", 1.08))
            n_frames = max(int(dur / s.speed_multiplier * fps), 1)
            chain.append(
                f"zoompan=z='1+({z}-1)*on/{n_frames}'"
                f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":d=1:s={rp['width']}x{rp['height']}:fps={fps}")
        elif eff.effect_id == "PUNCH_ZOOM":
            z = float(eff.params.get("scale_end", 1.08))
            cw = int(rp["width"] / z / 2) * 2
            ch = int(rp["height"] / z / 2) * 2
            chain.append(f"crop={cw}:{ch}:(iw-{cw})/2:(ih-{ch})/2,"
                         f"scale={rp['width']}:{rp['height']}:flags=lanczos")
    eq = COLOR_EQ[s.color_tone]
    if eq:
        chain.append(eq)
    chain += [f"fps={fps}", f"format={rp['pix_fmt']}", "setsar=1"]
    return chain


def _intermediate_profile(rp: dict) -> dict:
    """구간 중간 파일용 고품질 프로파일 (최종보다 한 단계 높게)."""
    inter = dict(rp)
    inter.pop("finishing_vf", None)
    if rp["video_codec"].endswith("_nvenc"):
        inter["cq"] = max(int(rp.get("cq", 20)) - 3, 12)
        inter["bitrate"] = "0"
        inter["extra_args"] = ["-temporal-aq", "1"]
    else:
        inter["crf"] = max(int(rp.get("crf", 20)) - 3, 12)
        inter["preset"] = "veryfast"      # 중간 파일은 속도 우선
    return inter


def build_render_plan(recipe: EditRecipe, input_path: str, ass_path: str | None,
                      sfx_items: list[tuple[int, ResolvedSfx, float]],
                      out_path: str, rp: dict, amix: dict, expected_ms: int,
                      fonts_dir: str, workdir: str,
                      key: str = "job") -> tuple[list[list[str]], list[str]]:
    """메모리 안전 렌더 계획 → (명령 목록, 정리할 임시파일 목록).

    단일 입력을 split→trim 하거나 여러 입력을 concat **필터**로 묶으면,
    앞 구간이 인코딩되는 동안 뒤 구간 프레임이 필터 그래프에 무한정 쌓인다
    (1080x1920 ≈ 6MB/frame → 수 GB, 실측 14.5GB OOM).

    따라서 구간을 각각 파일로 렌더한 뒤 concat **디먹서**로 잇는다.
    디먹서는 한 번에 파일 하나만 열므로 메모리가 누적되지 않는다.
    구간이 하나뿐이면 중간 파일 없이 한 번에 처리한다.
    """
    wd = pathlib.Path(workdir)
    dur_s = expected_ms / 1000.0
    cmds: list[list[str]] = []
    temps: list[str] = []

    # ── 최종 패스 공통: 마감 필터 → 자막 → 오디오 → 인코드 ──
    def finish_cmd(video_inputs: list[str]) -> list[str]:
        inputs = [FFMPEG, "-hide_banner", "-y"] + video_inputs
        parts: list[str] = []
        vfinal = "0:v"
        if rp.get("finishing_vf"):
            parts.append(f"[0:v]{rp['finishing_vf']}[vfin]")
            vfinal = "vfin"
        if ass_path:
            parts.append(f"[{vfinal}]ass=filename={_filter_path(ass_path)}"
                         f":fontsdir={_filter_path(fonts_dir)}[vsub]")
            vfinal = "vsub"
        if not parts:
            parts.append("[0:v]null[vout]")
            vfinal = "vout"

        if recipe.final_audio_policy == FinalAudioPolicy.SFX_ONLY and sfx_items:
            labels = []
            for j, (delay_ms, asset, vol_db) in enumerate(sfx_items):
                inputs += ["-i", asset.asset_path]
                parts.append(f"[{j+1}:a]volume={vol_db}dB,"
                             f"adelay={delay_ms}|{delay_ms}[sfx{j}]")
                labels.append(f"[sfx{j}]")
            parts.append("".join(labels) +
                         f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0,"
                         f"alimiter=limit={10 ** (amix['true_peak_limit_db'] / 20):.4f},"
                         f"aresample={amix['sample_rate']},"
                         f"apad,atrim=end={dur_s:.4f},asetpts=PTS-STARTPTS[aout]")
        else:
            inputs += ["-f", "lavfi", "-t", f"{dur_s:.4f}",
                       "-i", f"anullsrc=r={amix['sample_rate']}:cl=stereo"]
            parts.append(f"[1:a]atrim=end={dur_s:.4f},asetpts=PTS-STARTPTS[aout]")

        return inputs + [
            "-filter_complex", ";".join(parts),
            "-map", f"[{vfinal}]", "-map", "[aout]",
            *video_encode_args(rp),
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            "-c:a", rp["audio_codec"], "-b:a", rp["audio_bitrate"],
            "-ar", str(rp["audio_sample_rate"]), "-ac", "2",
            "-t", f"{dur_s:.4f}", "-max_muxing_queue_size", "1024",
            "-movflags", rp["movflags"], "-map_metadata", "-1", str(out_path)]

    # ── 구간이 하나면 중간 파일 불필요 ──
    if len(recipe.segments) == 1:
        s0 = recipe.segments[0]
        t0 = s0.trim_in_ms / 1000.0
        d0 = (s0.trim_out_ms - s0.trim_in_ms) / 1000.0
        vf = ",".join(_segment_filters(s0, rp))
        seg_out = wd / f"_seg_{key}_0.mp4"
        temps.append(str(seg_out))
        inter = _intermediate_profile(rp)
        cmds.append([FFMPEG, "-hide_banner", "-y",
                     "-ss", f"{t0:.6f}", "-t", f"{d0:.6f}", "-i", str(input_path),
                     "-vf", vf, "-an", *video_encode_args(inter),
                     "-movflags", "+faststart", "-map_metadata", "-1", str(seg_out)])
        cmds.append(finish_cmd(["-i", str(seg_out)]))
        return cmds, temps

    # ── 1단계: 구간별 독립 렌더 (입력 1개씩 → 스트리밍) ──
    inter = _intermediate_profile(rp)
    seg_files: list[pathlib.Path] = []
    for i, seg in enumerate(recipe.segments):
        t0 = seg.trim_in_ms / 1000.0
        d0 = (seg.trim_out_ms - seg.trim_in_ms) / 1000.0
        vf = ",".join(_segment_filters(seg, rp))
        seg_out = wd / f"_seg_{key}_{i}.mp4"
        seg_files.append(seg_out)
        temps.append(str(seg_out))
        cmds.append([FFMPEG, "-hide_banner", "-y",
                     "-ss", f"{t0:.6f}", "-t", f"{d0:.6f}", "-i", str(input_path),
                     "-vf", vf, "-an", *video_encode_args(inter),
                     "-movflags", "+faststart", "-map_metadata", "-1", str(seg_out)])

    # ── 2단계: concat 디먹서 목록 (한 번에 파일 하나만 연다) ──
    list_path = wd / f"_concat_{key}.txt"
    temps.append(str(list_path))
    list_path.write_text(
        "".join(f"file '{p.as_posix()}'\n" for p in seg_files), encoding="utf-8")

    # ── 3단계: 마감 + 자막 + 오디오 + 최종 인코드 ──
    cmds.append(finish_cmd(["-f", "concat", "-safe", "0", "-i", str(list_path)]))
    return cmds, temps


def build_concat_plan(cut_files: list[tuple[str, float, float]], out_path: str,
                      rp: dict, workdir: str,
                      key: str = "asm") -> tuple[list[list[str]], list[str]]:
    """CUT_ASSEMBLY 결합 계획 — (명령 목록, 임시파일 목록).

    FINAL_RENDER와 같은 이유로 concat 필터를 쓰지 않는다. 컷마다 정규화된
    중간 파일을 만든 뒤 concat 디먹서로 잇는다. 원음은 유지한다
    (제거는 FINAL_RENDER 책임 — 중간 산출물은 검수용).
    """
    wd = pathlib.Path(workdir)
    cmds: list[list[str]] = []
    temps: list[str] = []
    seg_files: list[pathlib.Path] = []

    inter = _intermediate_profile(rp)
    vf = (f"scale={rp['width']}:{rp['height']}:"
          f"force_original_aspect_ratio=increase:flags=lanczos,"
          f"crop={rp['width']}:{rp['height']},"
          f"fps={rp['fps']},format={rp['pix_fmt']},setsar=1")

    for i, (path, t0, t1) in enumerate(cut_files):
        seg_out = wd / f"_cut_{key}_{i}.mp4"
        seg_files.append(seg_out)
        temps.append(str(seg_out))
        cmds.append([
            FFMPEG, "-hide_banner", "-y",
            "-ss", f"{t0:.6f}", "-t", f"{max(t1 - t0, 0.001):.6f}", "-i", str(path),
            "-vf", vf, "-af", f"aresample={rp['audio_sample_rate']},"
                              f"aformat=channel_layouts=stereo",
            *video_encode_args(inter),
            "-c:a", rp["audio_codec"], "-b:a", rp["audio_bitrate"],
            "-ar", str(rp["audio_sample_rate"]), "-ac", "2",
            "-movflags", "+faststart", "-map_metadata", "-1", str(seg_out)])

    list_path = wd / f"_concat_{key}.txt"
    temps.append(str(list_path))
    list_path.write_text(
        "".join(f"file '{p.as_posix()}'\n" for p in seg_files), encoding="utf-8")

    cmds.append([
        FFMPEG, "-hide_banner", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_path),
        *video_encode_args(rp),
        "-c:a", rp["audio_codec"], "-b:a", rp["audio_bitrate"],
        "-ar", str(rp["audio_sample_rate"]), "-ac", "2",
        "-max_muxing_queue_size", "1024",
        "-movflags", rp["movflags"], "-map_metadata", "-1", str(out_path)])
    return cmds, temps
