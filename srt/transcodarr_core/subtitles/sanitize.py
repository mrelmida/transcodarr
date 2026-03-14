# src/transcodarr_core/subtitles/sanitize.py
from __future__ import annotations
import os, io, re, textwrap, unicodedata, tempfile
from typing import Optional, Tuple

# timestamp line
_TS = re.compile(r"^\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*$")
_NUM = re.compile(r"^\s*\d+\s*$")

def _to_sec(h, m, s, ms) -> float:
    return int(h)*3600 + int(m)*60 + int(s) + int(ms)/1000.0

def _clean_line(line: str, keep_tags: bool, ascii_only: bool) -> str:
    # drop control chars except newline
    line = "".join(ch for ch in line if ch == "\n" or ord(ch) >= 32)
    # normalize unicode + strip zero-width & BOM
    line = unicodedata.normalize("NFC", line).replace("\u200b", "").replace("\ufeff", "")
    if not keep_tags:
        line = re.sub(r"</?(i|b|u)>", "", line, flags=re.I)
        line = re.sub(r"\{\\.*?\}", "", line)  # ASS inline
    # strip other HTMLish tags that mov_text may choke on
    line = re.sub(r"<[^>]+>", "", line)
    if ascii_only:
        # brutal but robust: ditch unsupported glyphs (emojis, CJK, etc.)
        line = unicodedata.normalize("NFKD", line).encode("ascii", "ignore").decode("ascii")
    return line

def _wrap_block(txt: str, width: int, max_lines: int) -> str:
    if width <= 0 or max_lines <= 0:
        return txt.strip()
    wrapped: list[str] = []
    for raw in txt.splitlines():
        parts = textwrap.wrap(raw, width=width, break_long_words=True, break_on_hyphens=True) or [raw]
        wrapped.extend(parts)
    wrapped = [w.strip() for w in wrapped if w.strip()]
    return "\n".join(wrapped[:max_lines]).strip()

def sanitize_for_movtext(
    src_srt: str,
    *,
    video_seconds: Optional[float] = None,
    strict: bool = True,
) -> str:
    """
    Make an SRT safe for MP4/mov_text:
      - Normalize/strip risky tags & control chars
      - Optional ASCII-only (strict) to avoid glyph crashes
      - Wrap lines (default 42 cols, 2 lines)
      - Enforce sane cue durations & bounds; drop bad cues
      - Clip tail cues to video length, if provided
    Returns path to temp sanitized .srt (caller may delete).
    Tunables (env):
      MOVTEXT_WRAP_COLS (default 42), MOVTEXT_MAX_LINES (default 2),
      SRT_KEEP_TAGS (0/1), SRT_ASCII_ONLY (0/1 when strict=True),
      SRT_MAX_CUE_SEC (default 12), SRT_MIN_CUE_SEC (default 0.25)
    """
    WRAP_COLS   = int(os.getenv("MOVTEXT_WRAP_COLS", "42"))
    WRAP_LINES  = int(os.getenv("MOVTEXT_MAX_LINES", "2"))
    KEEP_TAGS   = os.getenv("SRT_KEEP_TAGS", "0") == "1"
    ASCII_ONLY  = (os.getenv("SRT_ASCII_ONLY", "0") == "1") if strict else False
    MAX_CUE_SEC = float(os.getenv("SRT_MAX_CUE_SEC", "12"))
    MIN_CUE_SEC = float(os.getenv("SRT_MIN_CUE_SEC", "0.25"))
    TAIL_PAD    = 0.2  # clip end within video_seconds - TAIL_PAD

    raw = open(src_srt, "rb").read()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except Exception:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")

    out = io.StringIO()
    lines = text.splitlines()
    i = 0
    cue_idx = 1

    while i < len(lines):
        # number (optional but we’ll re-number)
        if _NUM.match(lines[i]):
            i += 1  # skip original numbering

        # timestamp
        if i >= len(lines):
            break
        m = _TS.match(lines[i])
        if not m:
            # skip garbage until next plausible cue
            i += 1
            continue

        s = _to_sec(*m.groups()[0:4])
        e = _to_sec(*m.groups()[4:8])
        i += 1

        # gather text block
        buf: list[str] = []
        while i < len(lines) and not _NUM.match(lines[i]) and not _TS.match(lines[i]):
            if lines[i].strip() == "":
                i += 1
                break
            buf.append(lines[i])
            i += 1

        # bounds/duration sanity
        if e <= s:
            continue
        dur = e - s
        if dur < MIN_CUE_SEC:
            continue
        if dur > MAX_CUE_SEC:
            e = s + MAX_CUE_SEC

        if video_seconds and e > video_seconds - TAIL_PAD:
            e = max(s + MIN_CUE_SEC, (video_seconds - TAIL_PAD))

        # clean + wrap
        cleaned = _clean_line("\n".join(buf), keep_tags=KEEP_TAGS, ascii_only=ASCII_ONLY)
        wrapped = _wrap_block(cleaned, WRAP_COLS, WRAP_LINES)
        if not wrapped:
            continue

        # emit renumbered cue
        def _fmt(t: float) -> str:
            t = max(0.0, t)
            ms = int(round((t - int(t)) * 1000))
            sec = int(t) % 60
            minu = (int(t) // 60) % 60
            hour = int(t) // 3600
            return f"{hour:02d}:{minu:02d}:{sec:02d},{ms:03d}"

        out.write(f"{cue_idx}\n")
        out.write(f"{_fmt(s)} --> {_fmt(e)}\n")
        out.write(wrapped + "\n\n")
        cue_idx += 1

    fd, tmp_path = tempfile.mkstemp(prefix="srt_sanitized_", suffix=".srt")
    os.close(fd)
    with open(tmp_path, "w", encoding="utf-8") as g:
        g.write(out.getvalue())
    return tmp_path