# srt/transcodarr_core/subtitles/sync.py
from __future__ import annotations
import os, re, shlex, subprocess, logging
import contextlib

def try_autosync_sub(video_path, srt_path):
    """
    Creates a synced SRT using ffsubsync 0.4.x.
    Returns path to synced SRT if created, else None.
    """
    synced_path = os.path.splitext(srt_path)[0] + ".synced.srt"
    # remove any stale file
    try:
        if os.path.exists(synced_path):
            os.remove(synced_path)
    except Exception:
        pass

    cmd = [
        "ffsubsync",
        video_path,
        "-i", srt_path,
        "-o", synced_path,
        "--reference-stream", "a:0",
    ]
    logging.info("[SUBFIX] Running autosync: %s", " ".join(shlex.quote(x) for x in cmd))
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        logging.info("[SUBFIX] rc=%s", res.returncode)
        if res.stdout.strip():
            logging.info("[SUBFIX] STDOUT:\n%s", res.stdout.strip()[:4000])
        if res.stderr.strip():
            logging.info("[SUBFIX] STDERR:\n%s", res.stderr.strip()[:4000])

        if res.returncode == 0 and os.path.exists(synced_path) and os.path.getsize(synced_path) > 0:
            logging.info("[SUBFIX] Produced: %s", os.path.basename(synced_path))
            return synced_path

        logging.warning("[SUBFIX] No synced file produced; leaving original")
        # cleanup empty file if any
        try:
            if os.path.exists(synced_path) and os.path.getsize(synced_path) == 0:
                os.remove(synced_path)
        except Exception:
            pass
        return None

    except FileNotFoundError:
        logging.error("[SUBFIX] ffsubsync not found on PATH")
        return None
    except Exception as e:
        logging.error(f"[SUBFIX] Unexpected error: {e}", exc_info=True)
        return None




def ffsubsync_offset_seconds(video_path, srt_path):
    """
    Returns float offset_seconds as reported by ffsubsync (0.4.x),
    or None on failure. Creates a temp out then deletes it.
    """
    tmp_out = os.path.splitext(srt_path)[0] + ".ffsubsync_probe.srt"
    try:
        if os.path.exists(tmp_out):
            os.remove(tmp_out)
    except Exception:
        pass

    cmd = [
        "ffsubsync",
        video_path,
        "-i", srt_path,
        "-o", tmp_out,
        "--reference-stream", "a:0",  # pin main audio
    ]

    logging.info("[FFSUBSYNC-PROBE] %s", " ".join(shlex.quote(x) for x in cmd))
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    m = re.search(r"offset seconds:\s*([-+]?\d+(?:\.\d+)?)", out)
    try:
        if os.path.exists(tmp_out):
            os.remove(tmp_out)
    except Exception:
        pass
    if p.returncode != 0:
        logging.warning("[FFSUBSYNC-PROBE] rc=%s; could not compute offset", p.returncode)
        return None
    if m:
        try:
            val = float(m.group(1))
            logging.info("[FFSUBSYNC-PROBE] offset_seconds=%.3f", val)
            return val
        except Exception:
            return None
    return None

def try_autosync_until_ok(video_path: str, srt_path: str, *,
                           max_offset: float, max_retries: int,
                           min_improvement: float) -> tuple[bool, float, str]:
    if not srt_path or not os.path.exists(srt_path):
        return (False, float("inf"), srt_path)

    off = ffsubsync_offset_seconds(video_path, srt_path)
    if off is None:
        logging.warning("[SUBCHECK] Could not compute offset for candidate.")
        return (False, float("inf"), srt_path)

    best_abs = abs(off)
    best_file = srt_path
    logging.info(f"[SUBCHECK] Initial offset={off:.3f}s (thr={max_offset:.3f}s)")

    attempt = 0
    while best_abs >= max_offset and attempt < max_retries:
        attempt += 1
        logging.warning(f"[SUBCHECK] Misaligned ({best_abs:.3f}s) — auto-sync attempt {attempt}/{max_retries}")
        fixed = try_autosync_sub(video_path, best_file)
        if not fixed:
            logging.warning("[SUBCHECK] Auto-sync produced no output — stopping retries")
            break

        new_off = ffsubsync_offset_seconds(video_path, fixed)
        if new_off is None:
            logging.warning("[SUBCHECK] Post-sync probe failed — stopping retries")
            break

        new_abs = abs(new_off)
        improvement = best_abs - new_abs
        logging.info(f"[SUBCHECK] Post-sync offset={new_off:.3f}s (improved {improvement:+.3f}s)")

        if improvement < min_improvement:
            logging.warning(f"[SUBCHECK] Improvement < {min_improvement:.3f}s — stopping retries")
            if new_abs < best_abs:
                best_abs, best_file = new_abs, fixed
            else:
                with contextlib.suppress(Exception):
                    if os.path.exists(fixed) and os.path.getsize(fixed) == 0:
                        os.remove(fixed)
            break

        best_abs, best_file = new_abs, fixed

    ok = best_abs < max_offset
    return (ok, best_abs, best_file)