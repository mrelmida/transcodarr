from __future__ import annotations
import os, subprocess, logging

def strip_image_based_subs(file_path):
    """
    Remove all subtitle streams (e.g., PGS image-based) from the file
    so Bazarr thinks it has no subtitles.
    """
    try:
        base_name = os.path.splitext(file_path)[0]
        temp_path = base_name + "_nosubs" + os.path.splitext(file_path)[1]

        cmd = [
            "ffmpeg", "-y",
            "-i", file_path,
            "-map", "0:v",  # keep all video
            "-map", "0:a",  # keep all audio
            "-c", "copy",
            temp_path
        ]

        logging.info(f"Stripping subtitles from: {file_path}")
        subprocess.run(cmd, check=True)

        # Replace original file
        os.replace(temp_path, file_path)
        logging.info(f"Subtitles removed. File updated in place: {file_path}")
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to strip subtitles from {file_path}: {e}")
        return False

def extract_embedded_subtitles(file_path):
    """Extract the first embedded subtitle stream to SRT if it's text-based."""
    try:
        base_name = os.path.splitext(file_path)[0]
        srt_output = base_name + ".srt"

        # Get codec type for first subtitle
        probe = subprocess.run([
            "ffprobe", "-v", "error",
            "-select_streams", "s:0",
            "-show_entries", "stream=codec_name",
            "-of", "csv=p=0", file_path
        ], capture_output=True, text=True)

        codec = probe.stdout.strip()
        if not codec:
            logging.info(f"No embedded subtitle stream found in: {file_path}")
            return None

        logging.info(f"Found embedded subtitle codec: {codec}")

        # Only extract if it's text-based
        if codec not in ("subrip", "ass", "ssa", "webvtt", "mov_text"):
            logging.warning(f"First subtitle is image-based ({codec}), skipping extraction for: {file_path}")
            strip_image_based_subs(file_path)
            return None

        # Run ffmpeg and capture errors for visibility
        process = subprocess.run([
            "ffmpeg", "-y", "-i", file_path,
            "-map", "0:s:0", srt_output
        ], capture_output=True, text=True)

        if process.returncode != 0:
            logging.error(f"FFmpeg subtitle extraction failed for {file_path}")
            logging.error(process.stderr)  # <-- This gives you the actual reason
            return None

        if os.path.exists(srt_output):
            logging.info(f"Extracted embedded subtitles: {srt_output}")
            return srt_output

        logging.warning(f"Subtitle extraction reported success but no file created: {srt_output}")
        return None

    except Exception as e:
        logging.error(f"Subtitle extraction crashed for {file_path}: {e}")
        logging.error(traceback.format_exc())
        return None