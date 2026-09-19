"""Tests for the ffmpeg-based audio helpers, with subprocess.run stubbed."""

import subprocess
from pathlib import Path

import pytest

from anki_connect_server import audio

LOUDNORM_STDERR = b"""
[Parsed_loudnorm_0 @ 0x1]
{
\t"input_i" : "-31.50",
\t"input_tp" : "-12.00",
\t"input_lra" : "3.10",
\t"input_thresh" : "-41.70",
\t"output_i" : "-23.00",
\t"target_offset" : "0.10"
}
"""


class FakeFfmpeg:
    def __init__(self, input_i: bytes = LOUDNORM_STDERR, returncode: int = 0, output: bytes = b"n"):
        self.calls: list[list[str]] = []
        self.stderr = input_i
        self.returncode = returncode
        self.output = output

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(command)
        if command[-1] != "-":
            Path(command[-1]).write_bytes(self.output)
        return subprocess.CompletedProcess(command, self.returncode, b"", self.stderr)


@pytest.fixture
def fake_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> FakeFfmpeg:
    fake = FakeFfmpeg()
    monkeypatch.setattr(audio.subprocess, "run", fake)
    return fake


@pytest.mark.parametrize(
    ("filename", "expected"),
    [("clip.webm", True), ("a.MP3", True), ("word.opus", True), ("pic.jpg", False), ("x", False)],
)
def test_is_audio_file(filename, expected):
    assert audio.is_audio_file(filename) is expected


def test_measure_loudness_parses_last_json_report(fake_ffmpeg: FakeFfmpeg):
    assert audio.measure_loudness("ffmpeg", b"data", ".webm") == -31.5
    command = fake_ffmpeg.calls[0]
    assert command[0] == "ffmpeg"
    assert command[-4:] == ["-af", "loudnorm=print_format=json", "-f", "null", "-"][-4:]
    assert command[command.index("-i") + 1].endswith("input.webm")


@pytest.mark.parametrize(
    "stderr",
    [b"no json here", b'{"input_i": "-inf"}', b'{"other": 1}', b'{"input_i": "abc"}'],
)
def test_measure_loudness_rejects_unusable_reports(fake_ffmpeg: FakeFfmpeg, stderr):
    fake_ffmpeg.stderr = stderr
    assert audio.measure_loudness("ffmpeg", b"data", ".mp3") is None


def test_measure_loudness_returns_none_when_ffmpeg_fails(fake_ffmpeg: FakeFfmpeg):
    fake_ffmpeg.returncode = 1
    assert audio.measure_loudness("ffmpeg", b"data", ".mp3") is None


def test_measure_loudness_returns_none_when_ffmpeg_missing(monkeypatch: pytest.MonkeyPatch):
    def missing(*_: object, **__: object) -> None:
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(audio.subprocess, "run", missing)
    assert audio.measure_loudness("ffmpeg", b"data", ".mp3") is None


def test_normalize_audio_applies_static_gain_with_limiter(fake_ffmpeg: FakeFfmpeg):
    fake_ffmpeg.output = b"louder"
    assert audio.normalize_audio("ffmpeg", b"quiet", ".webm", -23.0) == b"louder"
    measure, encode = fake_ffmpeg.calls
    assert "loudnorm=print_format=json" in measure
    assert encode[encode.index("-af") + 1] == "volume=8.50dB,alimiter=limit=-1dB"
    assert encode[encode.index("-c:a") + 1] == "libopus"
    assert encode[-1].endswith("output.webm")
    assert "@rate@" not in encode


def test_normalize_audio_skips_reencode_when_within_threshold(fake_ffmpeg: FakeFfmpeg):
    assert audio.normalize_audio("ffmpeg", b"fine", ".mp3", -31.0) == b"fine"
    assert len(fake_ffmpeg.calls) == 1


def test_normalize_audio_returns_none_when_measurement_fails(fake_ffmpeg: FakeFfmpeg):
    fake_ffmpeg.stderr = b""
    assert audio.normalize_audio("ffmpeg", b"data", ".mp3", -23.0) is None


def test_normalize_audio_returns_none_on_empty_output(fake_ffmpeg: FakeFfmpeg):
    fake_ffmpeg.output = b""
    assert audio.normalize_audio("ffmpeg", b"data", ".wav", -16.0) is None


def test_normalize_audio_unknown_suffix_lets_ffmpeg_infer_codec(fake_ffmpeg: FakeFfmpeg):
    assert audio.normalize_audio("ffmpeg", b"data", ".xyz", -16.0) == b"n"
    assert "-c:a" not in fake_ffmpeg.calls[1]
