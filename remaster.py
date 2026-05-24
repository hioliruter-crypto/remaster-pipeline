#!/usr/bin/env python3
"""
remaster.py — Automated video remastering and localization pipeline.

Dependencies: tqdm, ffmpeg-python (optional)

All heavy video/audio processing is done via FFmpeg subprocess calls.
No MoviePy usage anywhere.

Usage:
    python remaster.py /path/to/working_directory
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None  # type: ignore

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("remaster")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
OUTPUT_RESOLUTION = (1920, 1080)
RANDOM_SEED: int = int(time.time())


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def run_ffmpeg(cmd: list[str], desc: str = "FFmpeg", duration_s: float | None = None) -> None:
    """Run an FFmpeg command with progress bar driven by stderr parsing."""
    log.info("Running: %s", " ".join(cmd))
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=False,
    )

    progress_bar = None
    if tqdm and duration_s and duration_s > 0:
        progress_bar = tqdm(total=100, desc=desc, unit="%", leave=True)

    last_pct = 0

    def _read_stderr() -> bytes:
        assert process.stderr is not None
        chunks: list[bytes] = []
        while True:
            chunk = process.stderr.read(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if progress_bar and duration_s:
                text = chunk.decode("utf-8", errors="replace")
                # Parse time=HH:MM:SS.xx
                matches = re.findall(r"time=(\d+):(\d+):(\d+\.\d+)", text)
                if matches:
                    h, m, s = matches[-1]
                    current_s = int(h) * 3600 + int(m) * 60 + float(s)
                    pct = min(int(current_s / duration_s * 100), 100)
                    nonlocal last_pct
                    if pct > last_pct:
                        progress_bar.update(pct - last_pct)
                        last_pct = pct
        return b"".join(chunks)

    stderr_data = _read_stderr()
    process.wait()

    if progress_bar:
        if last_pct < 100:
            progress_bar.update(100 - last_pct)
        progress_bar.close()

    if process.returncode != 0:
        err_text = stderr_data.decode("utf-8", errors="replace")[-3000:]
        log.error("FFmpeg failed (rc=%d) for [%s]:\n%s", process.returncode, desc, err_text)
        raise RuntimeError(f"FFmpeg failed for step: {desc}")


def detect_nvenc() -> bool:
    """Check if h264_nvenc encoder is available."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        return "h264_nvenc" in result.stdout
    except Exception:
        return False


def get_duration(path: str | Path) -> float:
    """Probe file duration in seconds."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True, timeout=30,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def ts_to_seconds(ts: str) -> float:
    """Convert HH:MM:SS to seconds."""
    parts = ts.split(":")
    h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
    return h * 3600 + m * 60 + s


def get_fps(path: str | Path) -> str:
    """Probe file frame rate as a ratio string (e.g. '30000/1001')."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True, timeout=30,
    )
    fps_str = result.stdout.strip()
    if not fps_str or "/" not in fps_str:
        return "30000/1001"  # safe fallback
    return fps_str


# ---------------------------------------------------------------------------
# MODULE 1 — NONLINEAR EDITING
# ---------------------------------------------------------------------------

def module1_edit(
    work_dir: Path,
    config: dict[str, Any],
    tmp_dir: Path,
    remaster_params: dict[str, Any],
) -> Path:
    """Extract segments, apply per-segment filters, and concatenate.

    ARCHITECTURE NOTE — Concat Demuxer Trap Prevention:
    ALL segments are force-transcoded to a uniform intermediate standard
    (same codec, fps, pixel format, timebase) regardless of whether they
    have per-segment filters (hflip/delogo) or not. This guarantees that
    the concat demuxer never encounters mismatched stream parameters.

    Output format: MPEG-TS (.ts) — avoids moov-atom issues and enables
    seamless byte-level concatenation.

    ARCHITECTURE NOTE — Delogo Coordinate Safety:
    The delogo filter is applied HERE (Module 1) on the raw segment in its
    ORIGINAL resolution, BEFORE any crop/scale from Module 2. This ensures
    that watermark_zone coordinates from config.json match the source frame
    dimensions exactly.
    """
    segments = config["segments"]
    global_settings = config.get("global_settings", {})
    watermark_zone = global_settings.get("watermark_zone", "")
    source = work_dir / "source_video.mp4"

    # Probe source fps to enforce uniform frame rate across all segments
    source_fps = get_fps(source)
    log.info("Source video fps: %s", source_fps)

    # Probe source resolution for delogo coordinate validation
    res_result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0:s=x",
            str(source),
        ],
        capture_output=True, text=True, timeout=30,
    )
    source_resolution = res_result.stdout.strip() or "unknown"
    log.info("Source video resolution: %s", source_resolution)
    if watermark_zone:
        log.info(
            "Delogo watermark_zone='%s' will be applied at source resolution %s "
            "(BEFORE any crop/scale in Module 2).",
            watermark_zone, source_resolution,
        )

    # Uniform intermediate encoding parameters for ALL segments
    INTERMEDIATE_CODEC = [
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-r", source_fps,
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
    ]

    segment_files: list[Path] = []

    for idx, seg in enumerate(segments):
        start = seg["start"]
        end = seg["end"]
        flip = seg.get("flip", False)
        delogo = seg.get("delogo", False)

        # Use .ts format for seamless concat demuxer compatibility
        seg_out = tmp_dir / f"seg_{idx:04d}.ts"

        # Build per-segment filter chain (Module 2-D: per-segment flags)
        # NOTE: delogo runs at ORIGINAL resolution (before Module 2 crop/scale)
        vfilters: list[str] = []
        if delogo and watermark_zone:
            # Apply delogo FIRST so coordinates match source resolution
            log.info(
                "  Segment %d: applying delogo at source resolution %s (zone: %s)",
                idx, source_resolution, watermark_zone,
            )
            vfilters.append(f"delogo={watermark_zone}")
        if flip:
            vfilters.append("hflip")

        cmd = [
            "ffmpeg", "-y",
            "-ss", start, "-to", end,
            "-i", str(source),
        ]

        # ALWAYS re-encode — no stream copy — uniform intermediate standard
        if vfilters:
            cmd += ["-vf", ",".join(vfilters)]

        cmd += INTERMEDIATE_CODEC
        cmd.append(str(seg_out))

        seg_duration = ts_to_seconds(end) - ts_to_seconds(start)
        run_ffmpeg(cmd, desc=f"Segment {idx}", duration_s=seg_duration)
        segment_files.append(seg_out)

    # Build concat list
    concat_list = tmp_dir / "concat_list.txt"
    with open(concat_list, "w") as f:
        for sf in segment_files:
            f.write(f"file '{sf}'\n")

    concat_out = tmp_dir / "concatenated.mp4"
    # All segments share identical codec params → safe to stream-copy concat
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        str(concat_out),
    ]
    total_dur = sum(
        ts_to_seconds(s["end"]) - ts_to_seconds(s["start"]) for s in segments
    )
    run_ffmpeg(cmd, desc="Concatenate", duration_s=total_dur)

    return concat_out


# ---------------------------------------------------------------------------
# MODULE 2 — VISUAL REMASTERING
# ---------------------------------------------------------------------------

def module2_remaster(
    concat_video: Path,
    work_dir: Path,
    tmp_dir: Path,
    remaster_params: dict[str, Any],
) -> Path:
    """Apply complex filtergraph: zoom/crop, rotation, color grade, overlay."""

    crop_pct = remaster_params["crop_pct"]
    rotation_deg = remaster_params["rotation_deg"]
    contrast = remaster_params["contrast"]
    gamma = remaster_params["gamma"]
    saturation = remaster_params["saturation"]
    overlay_opacity = remaster_params["overlay_opacity"]

    # --- Determine overlay asset ---
    overlay_png = work_dir / "overlay.png"
    overlay_mp4 = work_dir / "noise.mp4"
    has_png_overlay = overlay_png.exists()
    has_mp4_overlay = overlay_mp4.exists()

    # --- Build main video filtergraph ---
    keep_ratio = 1.0 - 2.0 * crop_pct
    crop_w = f"iw*{keep_ratio:.6f}"
    crop_h = f"ih*{keep_ratio:.6f}"

    rotation_rad = math.radians(rotation_deg)

    vf_stages: list[str] = []

    # A. Dynamic zoom + crop
    vf_stages.append(f"crop={crop_w}:{crop_h}")
    vf_stages.append("scale=1920:1080:flags=lanczos")

    # B. Micro rotation
    vf_stages.append(
        f"rotate={rotation_rad:.8f}:ow=rotw({rotation_rad:.8f}):oh=roth({rotation_rad:.8f}):fillcolor=black"
    )
    vf_stages.append("scale=1920:1080:flags=lanczos")

    # C. Color grading
    vf_stages.append(
        f"eq=contrast={contrast:.4f}:gamma={gamma:.4f}:saturation={saturation:.4f}"
    )

    remastered_out = tmp_dir / "remastered.mp4"
    video_duration = get_duration(concat_video)

    if has_png_overlay:
        # E. PNG overlay: apply opacity via colorchannelmixer on overlay input
        # [0:v] main video filters -> [base]
        # [1:v] format=rgba,colorchannelmixer=aa=opacity -> [ovr]
        # [base][ovr] overlay=format=yuv420 -> [out]
        main_chain = ",".join(vf_stages)
        filter_complex = (
            f"[0:v]{main_chain}[base];"
            f"[1:v]format=rgba,colorchannelmixer=aa={overlay_opacity:.4f}[ovr];"
            f"[base][ovr]overlay=0:0:format=yuv420[out]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-i", str(concat_video),
            "-i", str(overlay_png),
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-an",
            str(remastered_out),
        ]
    elif has_mp4_overlay:
        # E. MP4 overlay via blend
        # INFINITE LOOP PROTECTION: use both -shortest AND explicit -t limit
        # based on the known duration of the main video. -stream_loop -1 on
        # the overlay input makes it infinite; we MUST cap the output.
        main_chain = ",".join(vf_stages)
        filter_complex = (
            f"[0:v]{main_chain}[base];"
            f"[1:v]scale=1920:1080:flags=lanczos[ovr];"
            f"[base][ovr]blend=all_mode=overlay:all_opacity={overlay_opacity:.4f}[out]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-i", str(concat_video),
            "-stream_loop", "-1", "-i", str(overlay_mp4),
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-an",
            "-shortest",
            "-t", f"{video_duration:.3f}",
            str(remastered_out),
        ]
    else:
        # No overlay — just apply video filters
        main_chain = ",".join(vf_stages)
        cmd = [
            "ffmpeg", "-y",
            "-i", str(concat_video),
            "-vf", main_chain,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-an",
            str(remastered_out),
        ]

    run_ffmpeg(cmd, desc="Visual Remaster", duration_s=video_duration)
    return remastered_out


# ---------------------------------------------------------------------------
# MODULE 3 — SPEED ADJUSTMENT
# ---------------------------------------------------------------------------

def module3_speed(
    video_path: Path,
    speed_multiplier: float,
    tmp_dir: Path,
) -> Path:
    """Apply speed multiplier to video via setpts."""
    if abs(speed_multiplier - 1.0) < 0.001:
        log.info("Speed multiplier ~1.0, skipping speed adjustment.")
        return video_path

    pts_factor = 1.0 / speed_multiplier
    speed_out = tmp_dir / "speed_adjusted.mp4"
    video_duration = get_duration(video_path)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-vf", f"setpts={pts_factor:.6f}*PTS",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-an",
        str(speed_out),
    ]

    run_ffmpeg(cmd, desc="Speed Adjust", duration_s=video_duration / speed_multiplier)
    return speed_out


# ---------------------------------------------------------------------------
# MODULE 4 — AUDIO ENGINE
# ---------------------------------------------------------------------------

def module4_audio(
    work_dir: Path,
    tmp_dir: Path,
) -> Path:
    """Build final audio mix: voiceover + looped background music at -22dB.

    INFINITE LOOP PROTECTION (triple-layer):
    1. amix filter uses duration=shortest → stops when voiceover ends.
    2. Explicit -t flag caps output at voiceover duration.
    3. background_music uses -stream_loop -1 but is the SECOND input to amix
       with duration=shortest, so it cannot extend output beyond voiceover.
    """
    # Find voiceover
    vo_mp3 = work_dir / "voiceover.mp3"
    vo_wav = work_dir / "voiceover.wav"
    voiceover = vo_mp3 if vo_mp3.exists() else vo_wav

    bg_music = work_dir / "background_music.mp3"

    vo_duration = get_duration(voiceover)
    log.info("Voiceover duration: %.2fs — this is the master clock for audio.", vo_duration)
    audio_out = tmp_dir / "final_audio.aac"

    # amix duration=shortest ensures output stops when the SHORTER input ends.
    # Since voiceover is finite and bg_music is looped, voiceover is the limiter.
    filter_complex = (
        "[0:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[vo];"
        "[1:a]volume=-22dB,aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[bg];"
        "[vo][bg]amix=inputs=2:duration=first[out]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(voiceover),
        "-stream_loop", "-1", "-i", str(bg_music),
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{vo_duration:.3f}",
        str(audio_out),
    ]

    run_ffmpeg(cmd, desc="Audio Mix", duration_s=vo_duration)
    return audio_out


# ---------------------------------------------------------------------------
# MODULE 5 — EXPORT
# ---------------------------------------------------------------------------

def module5_export(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    use_nvenc: bool,
) -> None:
    """Mux video + audio and export final file.

    INFINITE LOOP PROTECTION:
    - -shortest stops encoding when the shorter stream ends.
    - Explicit -t caps at min(video, audio) as a hard failsafe.
    The primary timeline controller is the SHORTER of video/audio.
    """
    video_duration = get_duration(video_path)
    audio_duration = get_duration(audio_path)
    final_duration = min(video_duration, audio_duration)
    log.info(
        "Export durations — video: %.2fs, audio: %.2fs → output cap: %.2fs",
        video_duration, audio_duration, final_duration,
    )

    if use_nvenc:
        v_codec = ["h264_nvenc"]
        v_preset = ["-preset", "fast"]
    else:
        v_codec = ["libx264"]
        v_preset = ["-preset", "medium"]

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", *v_codec,
        "-b:v", "15M",
        *v_preset,
        "-c:a", "aac", "-b:a", "192k",
        "-map_metadata", "-1",
        "-movflags", "+faststart",
        "-shortest",
        "-t", f"{final_duration:.3f}",
        str(output_path),
    ]

    run_ffmpeg(cmd, desc="Final Export", duration_s=final_duration)


# ---------------------------------------------------------------------------
# MAIN PIPELINE
# ---------------------------------------------------------------------------

def validate_inputs(work_dir: Path) -> dict[str, Path]:
    """Validate all required input assets exist."""
    required: dict[str, list[str]] = {
        "source_video": ["source_video.mp4"],
        "voiceover": ["voiceover.mp3", "voiceover.wav"],
        "background_music": ["background_music.mp3"],
        "overlay": ["overlay.png", "noise.mp4"],
        "config": ["config.json"],
    }

    found: dict[str, Path] = {}
    for key, candidates in required.items():
        for c in candidates:
            p = work_dir / c
            if p.exists():
                found[key] = p
                break
        if key not in found:
            if key == "overlay":
                log.warning("No overlay asset found (overlay.png or noise.mp4) — skipping overlay.")
            else:
                log.error(
                    "Missing required asset '%s'. Expected one of: %s in %s",
                    key, candidates, work_dir,
                )
                sys.exit(1)

    return found


def main() -> None:
    """Entry point: orchestrate the full remastering pipeline."""
    if len(sys.argv) < 2:
        print("Usage: python remaster.py /path/to/working_directory")
        sys.exit(1)

    work_dir = Path(sys.argv[1]).resolve()
    if not work_dir.is_dir():
        log.error("Working directory does not exist: %s", work_dir)
        sys.exit(1)

    log.info("=" * 60)
    log.info("VIDEO REMASTERING PIPELINE")
    log.info("Working directory: %s", work_dir)
    log.info("=" * 60)

    # Validate inputs
    found = validate_inputs(work_dir)

    # Load config
    with open(found["config"], "r") as f:
        config = json.load(f)

    # Setup temp directory
    tmp_dir = work_dir / "tmp"
    tmp_dir.mkdir(exist_ok=True)

    # Generate random remaster parameters (reproducible via logged seed)
    random.seed(RANDOM_SEED)
    log.info("Random seed: %d", RANDOM_SEED)

    remaster_params: dict[str, Any] = {
        "crop_pct": random.uniform(0.05, 0.08),
        "rotation_deg": random.uniform(-0.5, 0.5),
        "contrast": random.uniform(1.02, 1.05),
        "gamma": random.uniform(0.95, 1.05),
        "saturation": random.uniform(1.03, 1.07),
        "overlay_opacity": random.uniform(0.03, 0.05),
    }

    log.info("Remaster parameters:")
    log.info("  Crop %%:          %.4f%%", remaster_params["crop_pct"] * 100)
    log.info("  Rotation:        %.4f°", remaster_params["rotation_deg"])
    log.info("  Contrast:        %.4f", remaster_params["contrast"])
    log.info("  Gamma:           %.4f", remaster_params["gamma"])
    log.info("  Saturation:      %.4f", remaster_params["saturation"])
    log.info("  Overlay opacity: %.4f", remaster_params["overlay_opacity"])

    # Detect NVENC
    use_nvenc = detect_nvenc()
    log.info("NVENC available: %s", use_nvenc)

    global_settings = config.get("global_settings", {})
    speed_multiplier = global_settings.get("speed_multiplier", 1.0)

    # -----------------------------------------------------------------------
    # MODULE 1: Nonlinear Editing (segment extraction + concat)
    # -----------------------------------------------------------------------
    log.info("-" * 60)
    log.info("MODULE 1: Nonlinear Editing")
    log.info("-" * 60)
    concat_video = module1_edit(work_dir, config, tmp_dir, remaster_params)

    # -----------------------------------------------------------------------
    # MODULE 2: Visual Remastering
    # -----------------------------------------------------------------------
    log.info("-" * 60)
    log.info("MODULE 2: Visual Remastering")
    log.info("-" * 60)
    remastered_video = module2_remaster(concat_video, work_dir, tmp_dir, remaster_params)

    # -----------------------------------------------------------------------
    # MODULE 3: Speed Adjustment
    # -----------------------------------------------------------------------
    log.info("-" * 60)
    log.info("MODULE 3: Speed Adjustment (%.4fx)", speed_multiplier)
    log.info("-" * 60)
    speed_video = module3_speed(remastered_video, speed_multiplier, tmp_dir)

    # -----------------------------------------------------------------------
    # MODULE 4: Audio Engine
    # -----------------------------------------------------------------------
    log.info("-" * 60)
    log.info("MODULE 4: Audio Engine")
    log.info("-" * 60)
    final_audio = module4_audio(work_dir, tmp_dir)

    # -----------------------------------------------------------------------
    # MODULE 5: Export
    # -----------------------------------------------------------------------
    log.info("-" * 60)
    log.info("MODULE 5: Final Export")
    log.info("-" * 60)
    output_path = work_dir / "output_remastered.mp4"
    module5_export(speed_video, final_audio, output_path, use_nvenc)

    # Cleanup
    log.info("Cleaning up temporary files...")
    shutil.rmtree(tmp_dir, ignore_errors=True)

    log.info("=" * 60)
    log.info("DONE — Output: %s", output_path)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
