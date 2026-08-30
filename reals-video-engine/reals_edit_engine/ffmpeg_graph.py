"""FFmpeg filter graph builder for REALS.

The renderer is intentionally deterministic and CPU-safe. LLM/VLM stages decide
what to do; this module only translates validated recipe values into argv.
"""
from __future__ import annotations

import math
import os
import pathlib

from .contracts import ColorTone, EditRecipe, FinalAudioPolicy
from .media import FFMPEG
from .sfx import ResolvedSfx


def video_encode_args(rp: dict) -> list[str]:
    """CPU(libx264) / GPU(h264_nvenc) encoder arguments."""
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

TIMED_COLOR_EQ = {
    "NATURAL": None,
    "WARM": "saturation=1.06:gamma_r=1.02:gamma_b=0.98",
    "COOL": "saturation=1.04:gamma_r=0.98:gamma_b=1.02",
    "VIVID": "contrast=1.06:saturation=1.14:brightness=0.01",
}


def _filter_path(p) -> str:
    """Escape paths used inside FFmpeg filter options."""
    return str(p).replace("\\", "/").replace(":", "\\:")


def map_produced_to_output_ms(recipe: EditRecipe, produced_segment_id: str, t_ms: int) -> int:
    """Map produced-video timestamp to final output timestamp."""
    offset = 0.0
    for s in recipe.segments:
        seg_out = (s.trim_out_ms - s.trim_in_ms) / s.speed_multiplier
        if s.produced_segment_id == produced_segment_id:
            t = min(max(t_ms, s.trim_in_ms), s.trim_out_ms)
            return int(round(offset + (t - s.trim_in_ms) / s.speed_multiplier))
        offset += seg_out
    raise KeyError(produced_segment_id)


def _normalize_filter(s, rp: dict) -> str:
    """Normalize every path, including ONE_TAKE, to the render profile."""
    width, height = int(rp["width"]), int(rp["height"])
    if s.crop_mode.value == "CENTER_9_16":
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={width}:{height}:"
            f"x='(in_w-{width})*{s.crop_center_x:.6f}':"
            f"y='(in_h-{height})*{s.crop_center_y:.6f}'"
        )
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
    )


def _effect_window(params: dict, duration_sec: float) -> tuple[float, float, float]:
    start = max(0.0, float(params.get("start_ms", 0)) / 1000.0)
    end = min(duration_sec, float(params.get("end_ms", duration_sec * 1000)) / 1000.0)
    if end <= start:
        end = min(duration_sec, start + 0.001)
    return start, end, max(end - start, 0.001)


def _active(start: float, end: float) -> str:
    return f"between(t,{start:.6f},{end:.6f})"


def _phase(start: float, duration: float) -> str:
    return f"((t-{start:.6f})/{duration:.6f})"


def _transform_motion_filters(effect_id: str, params: dict, rp: dict, duration_sec: float) -> list[str]:
    """Build CPU-only frame transforms from validated effect parameters."""
    width, height = int(rp["width"]), int(rp["height"])
    start, end, window = _effect_window(params, duration_sec)
    active = _active(start, end)
    phase = _phase(start, window)

    if effect_id in {"SHAKE", "VIBRATION"}:
        ax = abs(float(params.get("amplitude_x_pct", 0.012 if effect_id == "SHAKE" else 0.006)))
        ay = abs(float(params.get("amplitude_y_pct", 0.006 if effect_id == "SHAKE" else 0.004)))
        freq = float(params.get("frequency_hz", 12.0 if effect_id == "SHAKE" else 20.0))
        rotation = float(params.get("rotation_deg", 0.4 if effect_id == "SHAKE" else 0.15))
        requested_scale = float(params.get("scale", 1.018 if effect_id == "SHAKE" else 1.012))
        damping = bool(params.get("damping", True))
        envelope = f"(1-{phase})" if damping else "1"
        overscan = max(requested_scale, 1.08, 1.0 + 2.2 * max(ax, ay))
        angle = rotation * math.pi / 180.0
        return [
            f"scale=ceil(iw*{overscan:.6f}/2)*2:ceil(ih*{overscan:.6f}/2)*2:flags=lanczos",
            (
                "rotate="
                f"'if({active},{angle:.9f}*sin(2*PI*{freq:.6f}*(t-{start:.6f}))*{envelope},0)'"
                ":ow=iw:oh=ih:c=black"
            ),
            (
                f"crop={width}:{height}:"
                f"x='(iw-{width})/2+if({active},{ax*width:.6f}*"
                f"sin(2*PI*{freq:.6f}*(t-{start:.6f}))*{envelope},0)':"
                f"y='(ih-{height})/2+if({active},{ay*height:.6f}*"
                f"cos(2*PI*{freq:.6f}*(t-{start:.6f}))*{envelope},0)'"
            ),
        ]

    if effect_id == "ROTATION":
        rotation = float(params.get("rotation_deg", 0.8))
        requested_scale = float(params.get("scale", 1.02))
        overscan = max(requested_scale, 1.08)
        angle = rotation * math.pi / 180.0
        return [
            f"scale=ceil(iw*{overscan:.6f}/2)*2:ceil(ih*{overscan:.6f}/2)*2:flags=lanczos",
            (
                "rotate="
                f"'if({active},{angle:.9f}*sin(PI*{phase}),0)'"
                ":ow=iw:oh=ih:c=black"
            ),
            f"crop={width}:{height}:(iw-{width})/2:(ih-{height})/2",
        ]

    if effect_id == "POSITION_MOVE":
        tx = float(params.get("translate_x_pct", 0.0))
        ty = float(params.get("translate_y_pct", 0.0))
        requested_scale = float(params.get("scale", 1.02))
        overscan = max(requested_scale, 1.0 + 2.2 * max(abs(tx), abs(ty)))
        return [
            f"scale=ceil(iw*{overscan:.6f}/2)*2:ceil(ih*{overscan:.6f}/2)*2:flags=lanczos",
            (
                f"crop={width}:{height}:"
                f"x='(iw-{width})/2+if({active},{tx*width:.6f}*sin(PI*{phase}),0)':"
                f"y='(ih-{height})/2+if({active},{ty*height:.6f}*sin(PI*{phase}),0)'"
            ),
        ]

    if effect_id == "ZOOM":
        scale_start = float(params.get("scale_start", 1.0))
        scale_end = float(params.get("scale_end", 1.08))
        fps = float(rp["fps"])
        start_frame = max(0, int(round(start * fps)))
        end_frame = max(start_frame + 1, int(round(end * fps)))
        frame_span = max(1, end_frame - start_frame)
        zexpr = (
            f"if(lt(on,{start_frame}),{scale_start:.6f},"
            f"if(lte(on,{end_frame}),"
            f"{scale_start:.6f}+({scale_end:.6f}-{scale_start:.6f})*"
            f"(on-{start_frame})/{frame_span},{scale_end:.6f}))"
        )
        return [
            (
                f"zoompan=z='{zexpr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":d=1:s={width}x{height}:fps={fps}"
            )
        ]

    if effect_id == "FLASH":
        opacity = float(params.get("opacity", 0.85))
        return [
            (
                f"drawbox=x=0:y=0:w=iw:h=ih:color=white@{opacity:.4f}:t=fill:"
                f"enable='{active}'"
            )
        ]

    if effect_id == "COLOR":
        tone = str(params.get("tone", "NATURAL"))
        values = TIMED_COLOR_EQ.get(tone)
        if not values:
            return []
        return [f"eq={values}:enable='{active}'"]

    return []


def _segment_filters(s, rp) -> list[str]:
    """Filters for one segment; subtitles/final finishing are applied later."""
    fps = rp["fps"]
    chain = ["setpts=PTS-STARTPTS", _normalize_filter(s, rp)]
    if s.transition_id.value == "FLASH_WHITE":
        chain.append("fade=t=in:st=0:d=0.10:color=white")
    if s.speed_multiplier != 1.0:
        chain.append(f"setpts=PTS/{s.speed_multiplier:.6f}")

    duration_sec = (s.trim_out_ms - s.trim_in_ms) / 1000.0 / s.speed_multiplier
    for eff in s.effects:
        params = eff.params
        if eff.effect_id == "SMOOTH_ZOOM":
            z = float(params.get("scale_end", 1.08))
            n_frames = max(int(duration_sec * fps), 1)
            chain.append(
                f"zoompan=z='1+({z}-1)*on/{n_frames}'"
                f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":d=1:s={rp['width']}x{rp['height']}:fps={fps}"
            )
        elif eff.effect_id == "PUNCH_ZOOM":
            z = float(params.get("scale_end", 1.08))
            cw = int(rp["width"] / z / 2) * 2
            ch = int(rp["height"] / z / 2) * 2
            chain.append(
                f"crop={cw}:{ch}:(iw-{cw})/2:(ih-{ch})/2,"
                f"scale={rp['width']}:{rp['height']}:flags=lanczos"
            )
        else:
            chain.extend(_transform_motion_filters(eff.effect_id, params, rp, duration_sec))

    eq = COLOR_EQ[s.color_tone]
    if eq:
        chain.append(eq)
    chain += [f"fps={fps}", f"format={rp['pix_fmt']}", "setsar=1"]
    return chain


def _intermediate_profile(rp: dict) -> dict:
    """Higher-quality intermediate profile, biased toward CPU throughput."""
    inter = dict(rp)
    inter.pop("finishing_vf", None)
    if rp["video_codec"].endswith("_nvenc"):
        inter["cq"] = max(int(rp.get("cq", 20)) - 3, 12)
        inter["bitrate"] = "0"
        inter["extra_args"] = ["-temporal-aq", "1"]
    else:
        inter["crf"] = max(int(rp.get("crf", 20)) - 3, 12)
        inter["preset"] = "veryfast"
    return inter


def build_render_plan(recipe: EditRecipe, input_path: str, ass_path: str | None,
                      sfx_items: list[tuple[int, ResolvedSfx, float]],
                      out_path: str, rp: dict, amix: dict, expected_ms: int,
                      fonts_dir: str, workdir: str,
                      key: str = "job") -> tuple[list[list[str]], list[str]]:
    """Build a memory-safe render plan.

    Each segment is rendered independently before concat. This prevents the
    multi-gigabyte frame accumulation seen with a monolithic concat filter graph.
    """
    wd = pathlib.Path(workdir)
    dur_s = expected_ms / 1000.0
    cmds: list[list[str]] = []
    temps: list[str] = []

    def finish_cmd(video_inputs: list[str]) -> list[str]:
        inputs = [FFMPEG, "-hide_banner", "-y"] + video_inputs
        parts: list[str] = []
        vfinal = "0:v"
        if rp.get("finishing_vf"):
            parts.append(f"[0:v]{rp['finishing_vf']}[vfin]")
            vfinal = "vfin"
        if ass_path:
            parts.append(
                f"[{vfinal}]ass=filename={_filter_path(ass_path)}"
                f":fontsdir={_filter_path(fonts_dir)}[vsub]"
            )
            vfinal = "vsub"
        if not parts:
            parts.append("[0:v]null[vout]")
            vfinal = "vout"

        if recipe.final_audio_policy == FinalAudioPolicy.SFX_ONLY and sfx_items:
            labels = []
            for j, (delay_ms, asset, vol_db) in enumerate(sfx_items):
                inputs += ["-i", asset.asset_path]
                parts.append(
                    f"[{j+1}:a]volume={vol_db}dB,"
                    f"adelay={delay_ms}|{delay_ms}[sfx{j}]"
                )
                labels.append(f"[sfx{j}]")
            parts.append(
                "".join(labels)
                + f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0,"
                  f"alimiter=limit={10 ** (amix['true_peak_limit_db'] / 20):.4f},"
                  f"aresample={amix['sample_rate']},"
                  f"apad,atrim=end={dur_s:.4f},asetpts=PTS-STARTPTS[aout]"
            )
        else:
            inputs += [
                "-f", "lavfi", "-t", f"{dur_s:.4f}",
                "-i", f"anullsrc=r={amix['sample_rate']}:cl=stereo"
            ]
            parts.append(f"[1:a]atrim=end={dur_s:.4f},asetpts=PTS-STARTPTS[aout]")

        return inputs + [
            "-filter_complex", ";".join(parts),
            "-map", f"[{vfinal}]", "-map", "[aout]",
            *video_encode_args(rp),
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            "-c:a", rp["audio_codec"], "-b:a", rp["audio_bitrate"],
            "-ar", str(rp["audio_sample_rate"]), "-ac", "2",
            "-t", f"{dur_s:.4f}", "-max_muxing_queue_size", "1024",
            "-movflags", rp["movflags"], "-map_metadata", "-1", str(out_path),
        ]

    if len(recipe.segments) == 1:
        s0 = recipe.segments[0]
        t0 = s0.trim_in_ms / 1000.0
        d0 = (s0.trim_out_ms - s0.trim_in_ms) / 1000.0
        vf = ",".join(_segment_filters(s0, rp))
        seg_out = wd / f"_seg_{key}_0.mp4"
        temps.append(str(seg_out))
        inter = _intermediate_profile(rp)
        cmds.append([
            FFMPEG, "-hide_banner", "-y",
            "-ss", f"{t0:.6f}", "-t", f"{d0:.6f}", "-i", str(input_path),
            "-vf", vf, "-an", *video_encode_args(inter),
            "-movflags", "+faststart", "-map_metadata", "-1", str(seg_out),
        ])
        cmds.append(finish_cmd(["-i", str(seg_out)]))
        return cmds, temps

    inter = _intermediate_profile(rp)
    seg_files: list[pathlib.Path] = []
    for i, seg in enumerate(recipe.segments):
        t0 = seg.trim_in_ms / 1000.0
        d0 = (seg.trim_out_ms - seg.trim_in_ms) / 1000.0
        vf = ",".join(_segment_filters(seg, rp))
        seg_out = wd / f"_seg_{key}_{i}.mp4"
        seg_files.append(seg_out)
        temps.append(str(seg_out))
        cmds.append([
            FFMPEG, "-hide_banner", "-y",
            "-ss", f"{t0:.6f}", "-t", f"{d0:.6f}", "-i", str(input_path),
            "-vf", vf, "-an", *video_encode_args(inter),
            "-movflags", "+faststart", "-map_metadata", "-1", str(seg_out),
        ])

    list_path = wd / f"_concat_{key}.txt"
    temps.append(str(list_path))
    list_path.write_text(
        "".join(f"file '{p.as_posix()}'\n" for p in seg_files), encoding="utf-8"
    )
    cmds.append(finish_cmd(["-f", "concat", "-safe", "0", "-i", str(list_path)]))
    return cmds, temps


def build_concat_plan(cut_files: list[tuple[str, float, float]], out_path: str,
                      rp: dict, workdir: str,
                      key: str = "asm") -> tuple[list[list[str]], list[str]]:
    """Normalize raw cuts independently, then concatenate with the demuxer."""
    wd = pathlib.Path(workdir)
    cmds: list[list[str]] = []
    temps: list[str] = []
    seg_files: list[pathlib.Path] = []

    inter = _intermediate_profile(rp)
    vf = (
        f"scale={rp['width']}:{rp['height']}:"
        f"force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={rp['width']}:{rp['height']},"
        f"fps={rp['fps']},format={rp['pix_fmt']},setsar=1"
    )

    for i, (path, t0, t1) in enumerate(cut_files):
        seg_out = wd / f"_cut_{key}_{i}.mp4"
        seg_files.append(seg_out)
        temps.append(str(seg_out))
        cmds.append([
            FFMPEG, "-hide_banner", "-y",
            "-ss", f"{t0:.6f}", "-t", f"{max(t1 - t0, 0.001):.6f}", "-i", str(path),
            "-vf", vf,
            "-af", f"aresample={rp['audio_sample_rate']},aformat=channel_layouts=stereo",
            *video_encode_args(inter),
            "-c:a", rp["audio_codec"], "-b:a", rp["audio_bitrate"],
            "-ar", str(rp["audio_sample_rate"]), "-ac", "2",
            "-movflags", "+faststart", "-map_metadata", "-1", str(seg_out),
        ])

    list_path = wd / f"_concat_{key}.txt"
    temps.append(str(list_path))
    list_path.write_text(
        "".join(f"file '{p.as_posix()}'\n" for p in seg_files), encoding="utf-8"
    )

    cmds.append([
        FFMPEG, "-hide_banner", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_path),
        *video_encode_args(rp),
        "-c:a", rp["audio_codec"], "-b:a", rp["audio_bitrate"],
        "-ar", str(rp["audio_sample_rate"]), "-ac", "2",
        "-max_muxing_queue_size", "1024",
        "-movflags", rp["movflags"], "-map_metadata", "-1", str(out_path),
    ])
    return cmds, temps
