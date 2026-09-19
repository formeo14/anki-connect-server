"""Tests for the debounced sync after mining."""

import threading
import time

import pytest
from httpx import ASGITransport, AsyncClient

from anki_connect_server.api import app, create_auto_sync
from anki_connect_server.asbwebsocket import ASBWebSocketServer
from anki_connect_server.auto_sync import AutoSync
from anki_connect_server.config import Config


class SyncSpy:
    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail
        self.done = threading.Event()

    def __call__(self) -> None:
        self.calls += 1
        self.done.set()
        if self.fail:
            raise RuntimeError("AnkiWeb down")


def test_mutating_actions_are_debounced_into_one_sync():
    spy = SyncSpy()
    auto_sync = AutoSync(spy, delay_seconds=0.05)
    auto_sync.schedule("addNote")
    auto_sync.schedule("storeMediaFile")
    auto_sync.schedule("updateNoteFields")
    assert spy.done.wait(2)
    auto_sync.cancel()
    assert spy.calls == 1


def test_read_only_actions_do_not_schedule():
    spy = SyncSpy()
    auto_sync = AutoSync(spy, delay_seconds=0.01)
    auto_sync.schedule("findNotes")
    auto_sync.schedule("version")
    assert not spy.done.wait(0.1)


def test_cancel_prevents_pending_sync():
    spy = SyncSpy()
    auto_sync = AutoSync(spy, delay_seconds=0.05)
    auto_sync.schedule("addNote")
    auto_sync.cancel()
    assert not spy.done.wait(0.2)


def test_failed_sync_is_logged_not_raised(caplog: pytest.LogCaptureFixture):
    spy = SyncSpy(fail=True)
    auto_sync = AutoSync(spy, delay_seconds=0.01)
    with caplog.at_level("WARNING"):
        auto_sync.schedule("addNote")
        assert spy.done.wait(2)
        deadline = time.monotonic() + 2
        while not caplog.records and time.monotonic() < deadline:
            time.sleep(0.01)
    assert any("Auto-sync after mining failed" in r.message for r in caplog.records)


def test_create_auto_sync_respects_config(anki_wrapper):
    off = Config(COLLECTION_PATH="x", SYNC_AFTER_MINE=False)
    on = Config(COLLECTION_PATH="x", SYNC_AFTER_MINE=True, SYNC_AFTER_MINE_DELAY=1.5)
    assert create_auto_sync(anki_wrapper, off) is None
    assert isinstance(create_auto_sync(anki_wrapper, on), AutoSync)


@pytest.mark.asyncio
async def test_api_schedules_sync_only_after_mutating_actions(anki_wrapper):
    scheduled: list[str] = []

    class Recorder:
        def schedule(self, action: str) -> None:
            scheduled.append(action)

    app.state.anki_wrapper = anki_wrapper
    app.state.asb_ws_server = ASBWebSocketServer()
    app.state.auto_sync = Recorder()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/", json={"action": "deckNames", "version": 6, "params": {}})
            note = {
                "deckName": "Default",
                "modelName": "Basic",
                "fields": {"Front": "a", "Back": ""},
            }
            await client.post(
                "/", json={"action": "addNote", "version": 6, "params": {"note": note}}
            )
    finally:
        app.state.anki_wrapper = None
        app.state.asb_ws_server = None
        app.state.auto_sync = None
    assert scheduled == ["deckNames", "addNote"]
