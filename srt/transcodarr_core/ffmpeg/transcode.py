# src/transcodarr_core/ffmpeg/transcode.py
from __future__ import annotations
import os, subprocess, logging, contextlib, json
from dataclasses import dataclass
from ..config import Settings, get_setting
from ..ffmpeg.probe import get_duration_seconds, ffprobe_json, detect_hdr
from ..subtitles.sanitize import sanitize_for_movtext

@dataclass
class Progress:
    percent: float
    seconds: float
    message: str

def format_progress_bar(percent, length=30):
    filled = int(length * percent)
    return "[" + "█" * filled + "░" * (length - filled) + "]"

def bytes_to_gb(bytes_val: int | float) -> float:
    return float(bytes_val) / (1024 ** 3)

def _resolve_compression_tier(file_path: str) -> tuple[str | None, str | None]:
    """Check file size against compression tiers and return (preset, crf) override."""
    enabled = get_setting("COMPRESSION_TIERS_ENABLED", "false")
    if str(enabled).lower() != "true":
        return (None, None)

    tiers_json = get_setting("COMPRESSION_TIERS", "")
    if not tiers_json:
        return (None, None)

    try:
        tiers = json.loads(tiers_json)
    except (json.JSONDecodeError, TypeError):
        logging.warning("[TIERS] Invalid COMPRESSION_TIERS JSON, ignoring")
        return (None, None)

    if not tiers:
        return (None, None)

    try:
        file_size_gb = bytes_to_gb(os.path.getsize(file_path))
    except OSError:
        return (None, None)

    # Sort tiers by min_gb ascending
    tiers.sort(key=lambda t: float(t.get("min_gb", 0)))

    for tier in tiers:
        min_gb = float(tier.get("min_gb", 0))
        max_gb = float(tier.get("max_gb", 0))  # 0 means unlimited
        if file_size_gb >= min_gb and (max_gb == 0 or file_size_gb < max_gb):
            preset = tier.get("preset")
            crf = tier.get("crf", "")
            logging.info(
                "[TIERS] File %.2f GB matched tier [%g-%s GB]: preset=%s crf=%s",
                file_size_gb, min_gb, "unlimited" if max_gb == 0 else str(max_gb),
                preset, crf or "default"
            )
            return (preset, crf)

    return (None, None)

def build_ffmpeg_cmd(file_path: str, srt_path: str, out_temp: str, settings=None) -> list[str]:
    ffmpeg_threads = get_setting("FFMPEG_THREADS", "1")
    x264_threads = get_setting("X264_THREADS", "4")

    # Read encoding settings from DB (falls back to env -> defaults)
    resolution = get_setting("TARGET_RESOLUTION", "1920x1080")
    preset = get_setting("TARGET_PRESET", "fast")
    profile = get_setting("TARGET_PROFILE", "high")
    audio_bitrate = get_setting("TARGET_AUDIO_BITRATE", "448k")
    audio_channels = get_setting("TARGET_AUDIO_CHANNELS", "6")
    crf = get_setting("TARGET_CRF", "")
    normalize = get_setting("TARGET_AUDIO_NORMALIZE", "true")
    video_mode = get_setting("VIDEO_STREAM_MODE", "encode")
    audio_mode = get_setting("AUDIO_STREAM_MODE", "encode")

    # Compression tier override (size-based preset/CRF)
    tier_preset, tier_crf = _resolve_compression_tier(file_path)
    if tier_preset is not None:
        preset = tier_preset
    if tier_crf is not None and tier_crf != "":
        crf = tier_crf

    # Sanitize the SRT for mov_text robustness (if provided)
    srt_safe = sanitize_for_movtext(srt_path) if srt_path else None

    cmd = [
        "ffmpeg", "-y", "-y", "-threads", ffmpeg_threads,
        "-progress", "pipe:1", "-nostats",
        "-i", file_path,
    ]
    if srt_safe:
        cmd += ["-sub_charenc", "UTF-8", "-i", srt_safe]
    cmd += [
        "-map", "0:v:0",
        "-map", "0:a:0?",
    ]
    if srt_safe:
        cmd += ["-map", "1:0"]
    if video_mode == "copy":
        cmd += ["-c:v", "copy"]
    else:
        cmd += [
            "-c:v", "libx264",
            "-x264-params", f"threads={x264_threads}",
        ]

        # Probe source for HDR metadata and dimensions (single ffprobe call)
        hdr_info = detect_hdr(file_path)

        # Build composable video filter chain
        vf_filters: list[str] = []

        # 1. HDR → SDR tone mapping (must come before scaling)
        if hdr_info["is_hdr"]:
            logging.info("[HDR] Applying tone mapping for %s", os.path.basename(file_path))
            vf_filters += [
                "zscale=t=linear:npl=100",
                "format=gbrpf32le",
                "zscale=p=bt709",
                "tonemap=hable:desat=0",
                "zscale=t=bt709:m=bt709:r=tv",
                "format=yuv420p",
            ]

        # 2. Resolution scaling (aspect-ratio-preserving)
        if resolution and resolution.lower() == "1080p_max":
            src_h = hdr_info["height"]
            if src_h > 1080 or src_h == 0:
                # src_h == 0 means probe failed; assume downscale needed
                vf_filters.append("scale=-2:1080")
        elif resolution and resolution.lower() != "source":
            w, h = resolution.split("x")
            vf_filters.append(f"scale={w}:{h}")

        if vf_filters:
            cmd += ["-vf", ",".join(vf_filters)]

        # Pixel format: HDR chain already includes format=yuv420p
        if not hdr_info["is_hdr"]:
            cmd += ["-pix_fmt", "yuv420p"]

        cmd += [
            "-profile:v", profile,
            "-preset", preset,
        ]

        # CRF: empty = let codec decide, otherwise set explicitly
        if crf:
            cmd += ["-crf", crf]

    if audio_mode == "copy":
        cmd += ["-c:a", "copy"]
    else:
        # Audio normalization
        if normalize.lower() != "false":
            cmd += ["-af", "loudnorm=I=-14:TP=-1:LRA=11"]
        cmd += [
            "-c:a", "aac", "-b:a", audio_bitrate, "-ac", audio_channels,
        ]
    if srt_safe:
        cmd += ["-c:s", "mov_text", "-metadata:s:s:0", "language=eng"]
    cmd += [
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-movflags", "+faststart",
        "-fflags", "+genpts",
        "-max_muxing_queue_size", "4096",
        "-max_interleave_delta", "0",  # helps with odd interleaving
        "-avoid_negative_ts", "make_zero",  # safer timestamps
        out_temp
    ]

    return cmd

def build_sub_copy_mux_cmd(video_no_subs: str, srt_path: str, out_with_subs: str) -> list[str]:
    ffmpeg_threads = get_setting("FFMPEG_THREADS", "1")
    # Always sanitize for mov_text
    srt_safe = sanitize_for_movtext(srt_path)
    return [
        "ffmpeg","-y", "-threads", ffmpeg_threads,"-loglevel","error",
        "-i", video_no_subs,
        "-sub_charenc","UTF-8","-i", srt_safe,
        "-map","0:v:0","-map","0:a:0?","-map","1:0",
        "-c","copy","-c:s","mov_text","-metadata:s:s:0","language=eng",
        "-map_metadata","-1","-map_chapters","-1","-movflags","+faststart",
        "-max_muxing_queue_size","4096",
        "-max_interleave_delta", "0",  # helps with odd interleaving
        "-avoid_negative_ts", "make_zero",  # safer timestamps
        out_with_subs
    ]

def _probe_subs(path: str) -> tuple[int, list[str]]:
    try:
        cmd = [
            "ffprobe","-v","error","-select_streams","s",
            "-show_entries","stream=codec_name:stream_tags=language",
            "-of","json", path
        ]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            return 0, []
        data = json.loads(p.stdout or "{}")
        streams = data.get("streams", []) or []
        langs = []
        for s in streams:
            t = s.get("tags", {}) or {}
            if s.get("codec_name") == "mov_text":
                langs.append((t.get("language") or "").lower() or "und")
        return len(streams), langs
    except Exception:
        return 0, []

def run_ffmpeg_with_progress(cmd: list[str], total_duration: float | None, progress_file: str | None = None, source_path: str = "", register_path: str = ""):
    from ..worker_pool import register_proc, unregister_proc

    reg_key = register_path or source_path
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=1, universal_newlines=True
    )
    register_proc(proc, reg_key)
    recent: list[str] = []
    last_progress_write = 0.0
    try:
        for line in proc.stdout:  # type: ignore[arg-type]
            line = (line or "").strip()
            if len(recent) > 200:
                recent.pop(0)
            recent.append(line)

            if total_duration and line.startswith("out_time_ms"):
                try:
                    elapsed_sec = int(line.split("=")[1]) / 1_000_000
                except Exception:
                    elapsed_sec = None
                if elapsed_sec is not None and total_duration > 0:
                    pct = max(0.0, min(1.0, elapsed_sec / total_duration))
                    # Write progress to file periodically (every 1%)
                    if progress_file and (pct - last_progress_write) >= 0.01:
                        try:
                            _update_progress_file(progress_file, pct)
                            last_progress_write = pct
                        except Exception:
                            pass
                    yield Progress(percent=pct, seconds=elapsed_sec,
                                   message=f"{pct*100:6.2f}% ({elapsed_sec:.1f}s)")
            else:
                logging.debug(line)
        rc = proc.wait()
        if rc != 0:
            tail = "\n".join(recent[-30:])
            raise RuntimeError(f"ffmpeg exited with {rc}\n--- ffmpeg tail ---\n{tail}\n--- end tail ---")
    finally:
        unregister_proc(proc, reg_key)
        with contextlib.suppress(Exception):
            proc.kill()


def _update_progress_file(progress_file: str, percent: float):
    """Update just the progress field in an existing progress file."""
    import time
    try:
        with open(progress_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data["progress"] = round(percent * 100, 1)
    data["updated_at"] = time.time()
    with open(progress_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

def run_ffmpeg(file_path: str, srt_path: str, out_path: str, base_name: str, s: Settings, progress_file: str | None = None, register_path: str = "") -> None:
    total_duration = get_duration_seconds(file_path) or 0.0
    total_size_gb = bytes_to_gb(os.path.getsize(file_path))

    def _log_progress(cmd: list[str]):
        last_logged = -1.0
        for prog in run_ffmpeg_with_progress(cmd, total_duration, progress_file, source_path=file_path, register_path=register_path):
            if abs(prog.percent - last_logged) >= 0.0001 and prog.percent <= 1.0:
                bar = format_progress_bar(prog.percent)
                logging.info(
                    f"[TRANSCODING] {base_name} {bar} {prog.percent * 100:6.2f}%  "
                    f"[{total_size_gb * prog.percent:.2f} GB / {total_size_gb:.2f} GB]"
                )
                last_logged = prog.percent

    # 1) Combined encode (v+a+s)
    try:
        logging.info(f"[SUBS] Trying standard trancode with subs.")
        cmd = build_ffmpeg_cmd(file_path, srt_path, out_path, s)
        _log_progress(cmd)
        if not os.path.exists(out_path):
            raise RuntimeError(f"Expected output not found: {out_path}")
        return
    except Exception as e:
        logging.error(f"[FFMPEG] Combined encode failed: {e}")

    # 2) Fallback: transcode video+audio without subs, then mux subs separately
    tmp_no_subs = out_path + ".nosubs.mp4"
    tmp_with_subs = out_path + ".withsubs.mp4"

    try:
        logging.info("[SUBS] Trying fallback: transcode without subs, then mux subs.")
        cmd_no_subs = build_ffmpeg_cmd(file_path, None, tmp_no_subs, s)
        _log_progress(cmd_no_subs)
        if not os.path.exists(tmp_no_subs):
            raise RuntimeError(f"Fallback transcode produced no output: {tmp_no_subs}")
    except Exception as e:
        logging.error(f"[FFMPEG] Fallback transcode (no subs) also failed: {e}")
        raise

    # 3) Mux sanitized subs into that video (skip if no subs provided)
    if not srt_path:
        os.replace(tmp_no_subs, out_path)
        return

    try:
        logging.info("[SUBS] Muxing subs into fallback transcode output.")
        cmd = build_sub_copy_mux_cmd(tmp_no_subs, srt_path, tmp_with_subs)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"Sub copy-mux failed (rc={proc.returncode}): {proc.stderr or proc.stdout}")
        cnt, langs = _probe_subs(tmp_with_subs)
        logging.info(f"[SUBS] muxed subtitle tracks: {cnt} (langs={langs})")
        with contextlib.suppress(Exception):
            if os.path.exists(out_path):
                os.remove(out_path)
        os.replace(tmp_with_subs, out_path)
    finally:
        with contextlib.suppress(Exception):
            if os.path.exists(tmp_no_subs):
                os.remove(tmp_no_subs)
        with contextlib.suppress(Exception):
            if os.path.exists(tmp_with_subs):
                os.remove(tmp_with_subs)