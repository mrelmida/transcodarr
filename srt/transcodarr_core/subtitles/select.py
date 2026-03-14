# srt/transcodarr_core/subtitles/select.py
from __future__ import annotations
import os, re, subprocess, logging
from pathlib import Path
from typing import List, Optional, Tuple

from .fetch import fetch_extra_subs
from .sync import try_autosync_until_ok
from ..ffmpeg.probe import get_duration_seconds
from ..meta import load_unified_meta
from ..subtitles.sanitize import sanitize_for_movtext

# -------------------------------------------------------------------
# Basic SRT timestamp sanity
# -------------------------------------------------------------------
_TS = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})"
)

def _ts_to_sec(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

def srt_basic_sanity(srt_path: str, video_seconds: float):
    """
    Fast sanity checks on SRT timing vs video length.
    Returns (ok: bool, info: dict)
    """
    first_start = None
    last_end = 0.0
    total_on = 0.0
    n = 0
    prev_end = -1.0
    monotonic = True
    try:
        with open(srt_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _TS.search(line)
                if not m:
                    continue
                s_h, s_m, s_s, s_ms, e_h, e_m, e_s, e_ms = m.groups()
                s = _ts_to_sec(s_h, s_m, s_s, s_ms)
                e = _ts_to_sec(e_h, e_m, e_s, e_ms)
                if s < 0 or e < 0 or e <= s or (
                    video_seconds and (s > video_seconds or e > video_seconds + 2.0)
                ):
                    monotonic = False
                total_on += max(0.0, e - s)
                if first_start is None or s < first_start:
                    first_start = s
                if e > last_end:
                    last_end = e
                if prev_end > e:
                    monotonic = False
                prev_end = e
                n += 1
    except Exception as ex:
        return False, {"error": f"read_fail:{ex}"}

    if not n or not video_seconds or video_seconds <= 0:
        return False, {
            "error": "no_cues_or_bad_video_len",
            "n_cues": n,
            "video_seconds": video_seconds,
        }

    coverage = total_on / video_seconds
    head_gap = (first_start or 0.0)
    tail_gap = max(0.0, video_seconds - last_end)

    info = {
        "n_cues": n,
        "coverage": round(coverage, 3),
        "first_start_s": round(first_start or 0.0, 2),
        "last_end_s": round(last_end, 2),
        "head_gap_s": round(head_gap, 2),
        "tail_gap_s": round(tail_gap, 2),
        "monotonic": monotonic,
    }

    # Soft-accept long-credit gaps when overall coverage is reasonable
    long_credit_cap = max(600.0, 0.10 * video_seconds)  # 10 min or 20% of film, whichever larger

    if not monotonic:
        return False, {**info, "reason": "non_monotonic_or_out_of_bounds"}
    if coverage < 0.12:
        return False, {**info, "reason": "low_coverage"}
    if head_gap > 300:
        return False, {**info, "reason": "missing_beginning"}
    if tail_gap > long_credit_cap:
        return False, {**info, "reason": "missing_end"}  # large gap and low coverage → reject
    if video_seconds >= 5400 and n < 150:
        return False, {**info, "reason": "too_few_cues"}

    return True, info

MIN_SRT_SIZE_KB = int(os.getenv("MIN_SRT_SIZE_KB", "5"))  # 1..10 typical

# -------------------------------------------------------------------
# Episode helpers: parse from name; fall back to unified meta
# -------------------------------------------------------------------
_EP_PATTERNS = [
    re.compile(r"[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})"),  # S01E02 / S1E2 / S01-E02
    re.compile(r"\b(\d{1,2})[xX](\d{1,3})\b"),         # 1x02 / 01x002
]

def _parse_season_episode(text: str) -> Optional[Tuple[int, int]]:
    name = os.path.basename(text)
    for rx in _EP_PATTERNS:
        m = rx.search(name)
        if m:
            s, e = m.groups()
            try:
                return int(s), int(e)
            except Exception:
                pass
    return None

def _episode_key_from_meta(video_path: str) -> Optional[Tuple[int, int]]:
    """
    Prefer *.meta.json sidecar; else use filename.
    """
    meta = load_unified_meta(video_path) or {}
    if (meta.get("kind") or "").lower() == "episode":
        season = meta.get("season")
        eps = meta.get("episodes") or []
        ep = eps[0] if isinstance(eps, list) and eps else None
        if isinstance(season, int) and isinstance(ep, int):
            return season, ep
    return _parse_season_episode(video_path)

def _is_tv_episode(video_path: str) -> bool:
    meta = load_unified_meta(video_path) or {}
    if (meta.get("kind") or "").lower() == "episode":
        return True
    return _parse_season_episode(video_path) is not None

def _same_episode(video_path: str, srt_path: str) -> bool:
    """
    True if the SRT filename (or inference) matches the video's episode key.
    Movies (no ep key) return True to avoid filtering.
    """
    v_key = _episode_key_from_meta(video_path)
    if not v_key:
        return True  # treat as movie
    s_key = _parse_season_episode(srt_path)
    return bool(s_key and s_key == v_key)

# -------------------------------------------------------------------
# Candidate quality gates
# -------------------------------------------------------------------
def _ffmpeg_dry_mux_ok(video_path, srt_path):
    safe = sanitize_for_movtext(srt_path)
    cmd = [
        "ffmpeg","-v","error","-nostdin","-y",
        "-i", video_path,
        "-sub_charenc","UTF-8","-i", safe,
        "-map","0:v:0","-map","0:a:0?","-map","1:0",
        "-c:v","copy","-c:a","copy","-c:s","mov_text",
        "-t","5","-f","mp4","-movflags","frag_keyframe+empty_moov",
        "/dev/null"
    ]
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode == 0

def _score_srt_name(srt: Path, video_stem: str) -> float:
    n = srt.name.lower()
    kb = max(0, srt.stat().st_size // 1024)
    if kb < MIN_SRT_SIZE_KB:
        return -999  # hard-reject tiny stubs

    # prefer already-synced artifacts
    if n.endswith(".synced.srt") or ".synced." in n:
        return 1e6

    score = 0.0
    if re.search(r"\b(eng|english|en)\b", n): score += 3
    if re.search(r"\b(cc|sdh)\b", n):        score += 2
    if "forced" in n:                         score -= 2

    if n.startswith(video_stem.lower()):      score += 1
    score += min(kb / 50.0, 2.0)              # gentle size bonus (<= +2)

    return score

# -------------------------------------------------------------------
# Pickers
# -------------------------------------------------------------------
def find_subtitle_file(file_path: str) -> Optional[str]:
    """
    Choose the best SRT in the same folder as the video.
    Returns path (str) or None.
    """
    v = Path(file_path)
    folder = v.parent
    cands = list(folder.glob("*.srt"))
    if not cands:
        return None

    # TV: restrict to same-episode SRTs
    if _is_tv_episode(file_path):
        before = len(cands)
        cands = [p for p in cands if _same_episode(file_path, str(p))]
        if not cands:
            logging.info("[SRT PICK] %d local SRTs found, none matched episode %s", before, _episode_key_from_meta(file_path))
            return None

    scored = sorted(
        ((p, _score_srt_name(p, v.stem)) for p in cands),
        key=lambda t: t[1],
        reverse=True,
    )
    scored = [t for t in scored if t[1] > -500]
    if not scored:
        return None

    for p, _ in scored[:3]:
        if _ffmpeg_dry_mux_ok(str(v), str(p)):
            logging.info(f"[SRT PICK] Selected: {p.name}")
            return str(p)

    logging.warning("[SRT PICK] No suitable subtitle candidate found.")
    return None

def list_local_subtitle_candidates(
    file_path: str,
    limit: int = 10,
    quick_verify_top_k: int = 5,
) -> List[str]:
    """
    Return an ordered list of local .srt candidates in the same folder as the video.
    """
    v = Path(file_path)
    folder = v.parent
    cands = list(folder.glob("*.srt"))
    if not cands:
        return []

    # TV: restrict to same-episode SRTs up front
    if _is_tv_episode(file_path):
        before = len(cands)
        cands = [p for p in cands if _same_episode(file_path, str(p))]
        if not cands:
            logging.info("[SRT PICK] %d local SRTs found, none matched episode %s", before, _episode_key_from_meta(file_path))
            return []

    scored = sorted(
        ((p, _score_srt_name(p, v.stem)) for p in cands),
        key=lambda t: t[1],
        reverse=True,
    )
    scored = [t for t in scored if t[1] > -500]
    if not scored:
        return []

    # Quick parseability check for the top K
    verified: List[str] = []
    top = scored[:quick_verify_top_k]
    rest = scored[quick_verify_top_k:]

    for p, _ in top:
        if _ffmpeg_dry_mux_ok(str(v), str(p)):
            verified.append(str(p))
        else:
            logging.info(f"[SRT PICK] Dropped (dry-mux failed): {p.name}")

    verified.extend(str(p) for p, _ in rest)

    out = verified[:limit]
    if out:
        logging.info("[SRT PICK] Local candidates (best-first): %s", ", ".join(Path(x).name for x in out))
    else:
        logging.info("[SRT PICK] No local candidates survived scoring/verification")
    return out

def pick_working_sub(
    video_path: str,
    initial_srt: Optional[str],
    *,
    max_offset: float,
    max_retries: int,
    min_improvement: float,
    meta_override: dict | None = None,
) -> Optional[str]:
    """
    Try: [extracted(if any)] + all local .srt candidates (best-first).
    Only if locals all fail, fetch Subliminal and try those.
    For each candidate: bounded autosync -> sanity check (inside this function).
    """
    # Build local list (best-first)
    local_list = list_local_subtitle_candidates(video_path, limit=10, quick_verify_top_k=5)

    # Ensure extracted/explicit `initial_srt` (if present) is tried first
    if initial_srt and os.path.exists(initial_srt):
        if initial_srt in local_list:
            local_list = [initial_srt] + [p for p in local_list if p != initial_srt]
        else:
            local_list.insert(0, initial_srt)

    def _try_one(cand_path: str) -> Optional[str]:
        logging.info(f"[SUBPICK] Testing candidate: {os.path.basename(cand_path)}")
        ok_align, abs_off, chosen = try_autosync_until_ok(
            video_path,
            cand_path,
            max_offset=max_offset,
            max_retries=max_retries,
            min_improvement=min_improvement,
        )
        if not ok_align:
            logging.warning(
                f"[SUBPICK] Alignment failed for {os.path.basename(cand_path)} (|offset|={abs_off:.3f}s)"
            )
            return None

        total_duration = get_duration_seconds(video_path)
        ok_sane, meta = srt_basic_sanity(chosen, total_duration or get_duration_seconds(video_path))
        if not ok_sane:
            logging.warning(f"[SUBSANITY] Rejected {os.path.basename(chosen)} ({meta.get('reason')}): {meta}")
            return None

        logging.info(f"[SUBPICK] Accepted candidate: {os.path.basename(chosen)} (|offset|={abs_off:.3f}s)")
        return chosen

    # 1) Try all local (including extracted-first) before Subliminal
    for cand in local_list:
        res = _try_one(cand)
        if res:
            return res

    # 2) Local failed → try Subliminal
    logging.info("[SUBPICK] Local candidates exhausted — trying Subliminal providers...")
    try:
        extra_paths = fetch_extra_subs(video_path, lang="en", max_downloads=5, meta_override=meta_override) or []
    except Exception as e:
        logging.warning(f"[SUBPICK] Subliminal fetch failed: {e}")
        extra_paths = []

    # TV: restrict fetched subs to same episode
    if _is_tv_episode(video_path):
        extra_paths = [p for p in extra_paths if _same_episode(video_path, p)]
        if not extra_paths:
            logging.info("[SUBPICK] No fetched subs matched the episode key.")
            return None

    for cand in extra_paths:
        res = _try_one(cand)
        if res:
            return res

    logging.warning("[SUBPICK] All candidates (local + fetched) failed alignment and/or sanity.")
    return None