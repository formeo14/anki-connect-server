"""Tests for the persisted automatic loudness target."""

from pathlib import Path

import pytest

from anki_connect_server import audio, loudness
from anki_connect_server.anki_wrapper import AnkiWrapper


def _write_media(wrapper: AnkiWrapper, names: list[str]) -> None:
    media_dir = Path(wrapper.col.media.dir())  # type: ignore[union-attr]
    for name in names:
        (media_dir / name).write_bytes(name.encode())


def _fake_measure(loudness_by_name: dict[str, float | None]):
    def measure(_ffmpeg: str, data: bytes, _suffix: str) -> float | None:
        return loudness_by_name[data.decode()]

    return measure


def test_stored_target_round_trip(anki_wrapper: AnkiWrapper):
    assert loudness.stored_target(anki_wrapper.col) is None
    loudness.store_target(anki_wrapper.col, -19.5)
    assert loudness.stored_target(anki_wrapper.col) == -19.5


def test_measure_collection_target_prefers_sentence_audio(
    anki_wrapper: AnkiWrapper, monkeypatch: pytest.MonkeyPatch
):
    _write_media(anki_wrapper, ["asbp_a.webm", "asbp_b.mp3", "yomitan_audio_1.mp3", "pic.png"])
    monkeypatch.setattr(
        audio,
        "measure_loudness",
        _fake_measure({"asbp_a.webm": -30.0, "asbp_b.mp3": -20.0, "yomitan_audio_1.mp3": -5.0}),
    )
    assert loudness.measure_collection_target(anki_wrapper.col, "ffmpeg") == -25.0


def test_measure_collection_target_falls_back_to_all_audio(
    anki_wrapper: AnkiWrapper, monkeypatch: pytest.MonkeyPatch
):
    _write_media(anki_wrapper, ["word1.mp3", "word2.ogg"])
    monkeypatch.setattr(
        audio, "measure_loudness", _fake_measure({"word1.mp3": -10.0, "word2.ogg": None})
    )
    assert loudness.measure_collection_target(anki_wrapper.col, "ffmpeg") == -10.0


def test_measure_collection_target_without_audio_returns_none(anki_wrapper: AnkiWrapper):
    _write_media(anki_wrapper, ["pic.png"])
    assert loudness.measure_collection_target(anki_wrapper.col, "ffmpeg") is None


def test_measure_collection_target_respects_sample_limit(
    anki_wrapper: AnkiWrapper, monkeypatch: pytest.MonkeyPatch
):
    names = [f"asbp_{i}.mp3" for i in range(5)]
    _write_media(anki_wrapper, names)
    seen: list[str] = []

    def measure(_ffmpeg: str, data: bytes, _suffix: str) -> float:
        seen.append(data.decode())
        return -20.0

    monkeypatch.setattr(audio, "measure_loudness", measure)
    assert loudness.measure_collection_target(anki_wrapper.col, "ffmpeg", max_samples=2) == -20.0
    assert len(seen) == 2


def test_wrapper_measure_and_store_audio_target(
    anki_wrapper: AnkiWrapper, monkeypatch: pytest.MonkeyPatch
):
    _write_media(anki_wrapper, ["asbp_a.webm"])
    monkeypatch.setattr(audio, "measure_loudness", _fake_measure({"asbp_a.webm": -28.0}))
    assert anki_wrapper.measure_and_store_audio_target() == -28.0
    assert loudness.stored_target(anki_wrapper.col) == -28.0


@pytest.mark.parametrize(
    ("mode", "stored", "expect_thread"),
    [("auto", None, True), ("auto", -20.0, False), ("fixed", None, False), ("off", None, False)],
)
def test_startup_measurement_only_when_auto_and_unset(
    anki_wrapper: AnkiWrapper, monkeypatch: pytest.MonkeyPatch, mode, stored, expect_thread
):
    from anki_connect_server import api
    from anki_connect_server.config import Config

    if stored is not None:
        loudness.store_target(anki_wrapper.col, stored)
    started: list[object] = []

    class Thread:
        def __init__(self, target: object, daemon: bool) -> None:
            started.append(target)

        def start(self) -> None:
            pass

    monkeypatch.setattr(api.threading, "Thread", Thread)
    api.start_audio_target_measurement(
        anki_wrapper, Config(COLLECTION_PATH="x", AUDIO_NORMALIZATION=mode)
    )
    assert started == ([anki_wrapper.measure_and_store_audio_target] if expect_thread else [])


def test_normalize_existing_media_overwrites_in_place_keeping_names(
    anki_wrapper: AnkiWrapper, monkeypatch: pytest.MonkeyPatch
):
    names = ["asbp_K-ON_MixedCase_AbC.mp3", "asbp_quiet.webm", "asbp_shot.jpeg", "yomitan_w.mp3"]
    _write_media(anki_wrapper, names)
    monkeypatch.setattr(
        audio,
        "measure_loudness",
        lambda _f, data, _s: {b"LOUD:": -19.0}.get(data[:5], -30.0),
    )
    monkeypatch.setattr(
        audio,
        "normalize_audio",
        lambda _f, data, _s, _t: data if data.startswith(b"LOUD:") else b"LOUD:" + data,
    )
    results = loudness.normalize_existing_media(anki_wrapper.col, "ffmpeg", -19.0)
    media_dir = Path(anki_wrapper.col.media.dir())  # type: ignore[union-attr]
    assert [r.name for r in results] == ["asbp_K-ON_MixedCase_AbC.mp3", "asbp_quiet.webm"]
    assert all((r.before_lufs, r.after_lufs) == (-30.0, -19.0) for r in results)
    assert sorted(p.name for p in media_dir.iterdir()) == sorted(names)
    assert (
        media_dir / "asbp_K-ON_MixedCase_AbC.mp3"
    ).read_bytes() == b"LOUD:asbp_K-ON_MixedCase_AbC.mp3"
    assert (media_dir / "yomitan_w.mp3").read_bytes() == b"yomitan_w.mp3"

    again = loudness.normalize_existing_media(anki_wrapper.col, "ffmpeg", -19.0, dry_run=True)
    assert all(r.before_lufs == r.after_lufs for r in again)
