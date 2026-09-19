import json
import logging
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".oga", ".opus", ".webm", ".mka", ".wav", ".flac"}
)
GAIN_SKIP_THRESHOLD_DB = 1.0

_FFMPEG_TIMEOUT = 60.0
_JSON_OBJECT_PATTERN = re.compile(r"\{[^{}]*\}")
_ENCODERS: dict[str, tuple[str, ...]] = {
    ".mp3": ("-c:a", "libmp3lame", "-b:a", "192k"),
    ".m4a": ("-c:a", "aac", "-b:a", "192k"),
    ".m4b": ("-c:a", "aac", "-b:a", "192k"),
    ".aac": ("-c:a", "aac", "-b:a", "192k"),
    ".ogg": ("-c:a", "libvorbis", "-q:a", "5"),
    ".oga": ("-c:a", "libvorbis", "-q:a", "5"),
    ".opus": ("-c:a", "libopus", "-b:a", "128k"),
    ".webm": ("-c:a", "libopus", "-b:a", "96k"),
    ".mka": ("-c:a", "libopus", "-b:a", "96k"),
    ".wav": ("-c:a", "pcm_s16le"),
    ".flac": ("-c:a", "flac"),
}


def ffmpeg_available(ffmpeg_path: str) -> bool:
    return shutil.which(ffmpeg_path) is not None


def is_audio_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in AUDIO_EXTENSIONS


def measure_loudness(ffmpeg_path: str, data: bytes, suffix: str) -> float | None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        input_path = Path(tmp_dir) / f"input{suffix.lower()}"
        input_path.write_bytes(data)
        result = _run_ffmpeg(
            ffmpeg_path,
            "-i",
            str(input_path),
            "-af",
            "loudnorm=print_format=json",
            "-f",
            "null",
            "-",
        )
    if result is None:
        return None
    return _parse_input_i(result.stderr.decode("utf-8", errors="replace"))


def normalize_audio(ffmpeg_path: str, data: bytes, suffix: str, target_lufs: float) -> bytes | None:
    suffix = suffix.lower()
    input_lufs = measure_loudness(ffmpeg_path, data, suffix)
    if input_lufs is None:
        return None
    gain_db = target_lufs - input_lufs
    if abs(gain_db) < GAIN_SKIP_THRESHOLD_DB:
        return data
    with tempfile.TemporaryDirectory() as tmp_dir:
        input_path = Path(tmp_dir) / f"input{suffix}"
        output_path = Path(tmp_dir) / f"output{suffix}"
        input_path.write_bytes(data)
        result = _run_ffmpeg(
            ffmpeg_path,
            "-y",
            "-i",
            str(input_path),
            "-af",
            f"volume={gain_db:.2f}dB,alimiter=limit=-1dB",
            *_ENCODERS.get(suffix, ()),
            str(output_path),
        )
        if result is None or not output_path.is_file() or output_path.stat().st_size == 0:
            return None
        return output_path.read_bytes()


def _run_ffmpeg(ffmpeg_path: str, *args: str) -> subprocess.CompletedProcess[bytes] | None:
    command = [ffmpeg_path, "-hide_banner", "-nostats", *args]
    try:
        result = subprocess.run(  # noqa: S603
            command, capture_output=True, timeout=_FFMPEG_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("ffmpeg failed to run: %s", exc)
        return None
    if result.returncode != 0:
        logger.debug(
            "ffmpeg exited with %s: %s",
            result.returncode,
            result.stderr.decode("utf-8", errors="replace")[-500:],
        )
        return None
    return result


def _parse_input_i(stderr: str) -> float | None:
    matches = _JSON_OBJECT_PATTERN.findall(stderr)
    if not matches:
        return None
    try:
        value = float(json.loads(matches[-1]).get("input_i"))
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return None
    return value if math.isfinite(value) else None
