"""Real-ffmpeg tests: generated sine waves stored through the wrapper must come
out at the configured loudness. Skipped when ffmpeg is not installed."""

import base64
import io
import math
import shutil
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

import pytest

from anki_connect_server import audio, loudness
from anki_connect_server.anki_wrapper import AnkiWrapper
from anki_connect_server.config import get_config

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
SAMPLE_RATE = 44100
TOLERANCE_LU = 1.5

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture(autouse=True)
def fresh_config_cache():
    get_config.cache_clear()
    yield
    get_config.cache_clear()


@pytest.fixture
def normalization(monkeypatch: pytest.MonkeyPatch):
    def configure(mode: str, target: float | None = None) -> None:
        monkeypatch.setenv("ANKICONNECT_AUDIO_NORMALIZATION", mode)
        if target is not None:
            monkeypatch.setenv("ANKICONNECT_AUDIO_NORMALIZATION_TARGET_LUFS", str(target))
        get_config.cache_clear()

    return configure


def sine_wav(amplitude: float, seconds: float = 1.5) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(
            b"".join(
                struct.pack(
                    "<h", int(amplitude * 32767 * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE))
                )
                for i in range(int(SAMPLE_RATE * seconds))
            )
        )
    return buffer.getvalue()


def to_webm(wav: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in.wav", Path(tmp) / "out.webm"
        src.write_bytes(wav)
        subprocess.run(  # noqa: S603
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(src),
                "-c:a",
                "libopus",
                str(dst),
            ],
            check=True,
        )
        return dst.read_bytes()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def measure(data: bytes, suffix: str = ".wav") -> float:
    value = audio.measure_loudness(FFMPEG, data, suffix)
    assert value is not None
    return value


def stored(wrapper: AnkiWrapper, filename: str) -> bytes:
    value = wrapper.retrieve_media_file(filename)
    assert value is not None
    return base64.b64decode(value)


QUIET = sine_wav(0.03)
LOUD = sine_wav(0.9)


def test_measure_loudness_separates_quiet_and_loud():
    assert measure(QUIET) < -30.0
    assert -10.0 < measure(LOUD) < 0.0


def test_corrupt_bytes_fail_soft():
    assert audio.measure_loudness(FFMPEG, b"not audio", ".wav") is None
    assert audio.normalize_audio(FFMPEG, b"not audio", ".wav", -23.0) is None


@pytest.mark.parametrize("target", [-23.0, -16.0])
def test_fixed_mode_brings_quiet_wav_to_target(anki_wrapper: AnkiWrapper, normalization, target):
    normalization("fixed", target)
    anki_wrapper.store_media_file("quiet.wav", b64(QUIET))
    assert abs(measure(stored(anki_wrapper, "quiet.wav")) - target) <= TOLERANCE_LU


def test_fixed_mode_normalizes_asbplayer_webm_clip(anki_wrapper: AnkiWrapper, normalization):
    normalization("fixed", -20.0)
    clip = to_webm(QUIET)
    assert measure(clip, ".webm") < -30.0
    anki_wrapper.store_media_file("asbp_clip.webm", b64(clip))
    assert abs(measure(stored(anki_wrapper, "asbp_clip.webm"), ".webm") + 20.0) <= TOLERANCE_LU


def test_fixed_mode_attenuates_loud_clip(anki_wrapper: AnkiWrapper, normalization):
    normalization("fixed", -23.0)
    anki_wrapper.store_media_file("loud.wav", b64(LOUD))
    assert abs(measure(stored(anki_wrapper, "loud.wav")) + 23.0) <= TOLERANCE_LU


def test_already_on_target_audio_is_stored_untouched(anki_wrapper: AnkiWrapper, normalization):
    normalization("fixed", round(measure(QUIET), 1))
    anki_wrapper.store_media_file("quiet.wav", b64(QUIET))
    assert stored(anki_wrapper, "quiet.wav") == QUIET


def test_auto_mode_uses_measured_collection_target(anki_wrapper: AnkiWrapper, normalization):
    anki_wrapper.store_media_file("asbp_reference.wav", b64(QUIET))
    reference = measure(QUIET)
    assert anki_wrapper.measure_and_store_audio_target() == pytest.approx(reference, abs=0.1)
    assert loudness.stored_target(anki_wrapper.col) == pytest.approx(reference, abs=0.1)

    normalization("auto")
    anki_wrapper.store_media_file("loud.wav", b64(LOUD))
    assert abs(measure(stored(anki_wrapper, "loud.wav")) - reference) <= TOLERANCE_LU


def test_auto_mode_without_stored_target_uses_fixed_target(
    anki_wrapper: AnkiWrapper, normalization
):
    normalization("auto", -18.0)
    anki_wrapper.store_media_file("loud.wav", b64(LOUD))
    assert abs(measure(stored(anki_wrapper, "loud.wav")) + 18.0) <= TOLERANCE_LU


def test_off_mode_stores_bytes_unchanged(anki_wrapper: AnkiWrapper):
    anki_wrapper.store_media_file("loud.wav", b64(LOUD))
    assert stored(anki_wrapper, "loud.wav") == LOUD


def test_non_audio_and_corrupt_files_pass_through(anki_wrapper: AnkiWrapper, normalization):
    normalization("fixed")
    anki_wrapper.store_media_file("page.html", b64(b"<html/>"))
    anki_wrapper.store_media_file("broken.mp3", b64(b"not an mp3"))
    assert stored(anki_wrapper, "page.html") == b"<html/>"
    assert stored(anki_wrapper, "broken.mp3") == b"not an mp3"


def test_missing_ffmpeg_binary_disables_normalization(
    anki_wrapper: AnkiWrapper, normalization, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ANKICONNECT_FFMPEG_PATH", "no-such-ffmpeg")
    normalization("fixed")
    anki_wrapper.store_media_file("loud.wav", b64(LOUD))
    assert stored(anki_wrapper, "loud.wav") == LOUD
