import logging
import random
from pathlib import Path

from anki.collection import Collection

from anki_connect_server import audio

logger = logging.getLogger(__name__)

TARGET_CONFIG_KEY = "ankiConnectServer.audioTargetLufs"
SENTENCE_AUDIO_PREFIX = "asbp_"
MAX_SAMPLES = 20


def stored_target(col: Collection) -> float | None:
    value = col.get_config(TARGET_CONFIG_KEY, None)
    return float(value) if isinstance(value, (int, float)) else None


def store_target(col: Collection, target_lufs: float) -> None:
    col.set_config(TARGET_CONFIG_KEY, target_lufs)


def measure_collection_target(
    col: Collection, ffmpeg_path: str, max_samples: int = MAX_SAMPLES
) -> float | None:
    measurements = [
        loudness
        for path in _sample_audio_files(Path(col.media.dir()), max_samples)  # type: ignore[union-attr]
        if (loudness := audio.measure_loudness(ffmpeg_path, path.read_bytes(), path.suffix))
        is not None
    ]
    if not measurements:
        return None
    return sum(measurements) / len(measurements)


def _sample_audio_files(media_dir: Path, max_samples: int) -> list[Path]:
    try:
        candidates = [
            path
            for path in media_dir.iterdir()
            if path.is_file() and audio.is_audio_file(path.name)
        ]
    except OSError as exc:
        logger.debug("Cannot list media directory %s: %s", media_dir, exc)
        return []
    sentence_audio = [path for path in candidates if path.name.startswith(SENTENCE_AUDIO_PREFIX)]
    pool = sentence_audio or candidates
    return random.sample(pool, min(max_samples, len(pool)))
