"""Tests for the embedded asbplayer WebSocket server."""

import base64
import json
import threading
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from anki_connect_server import anki_wrapper as anki_wrapper_module
from anki_connect_server import api, audio
from anki_connect_server.anki_wrapper import AnkiWrapper
from anki_connect_server.api import app, create_asb_ws_server
from anki_connect_server.asbwebsocket import (
    ASBWebSocketServer,
    NoClientResponseError,
    PostMineAction,
)
from anki_connect_server.asbwebsocket.models import ClientCommand, ClientResponse
from anki_connect_server.config import Config
from anki_connect_server.types import JsonObject


def add_note_request(front: str = "Hello") -> JsonObject:
    fields: JsonObject = {"Front": front, "Back": "World"}
    note: JsonObject = {"deckName": "Default", "modelName": "Basic", "fields": fields}
    return {"action": "addNote", "version": 6, "params": {"note": note}}


class Responder:
    """Answers the next command published to ``ws`` from a background thread,
    because ``TestClient`` blocks the test thread while the request is in flight."""

    def __init__(self, ws: WebSocketTestSession, body: object) -> None:
        self.command: dict[str, object] = {}
        self._thread = threading.Thread(target=self._run, args=(ws, body), daemon=True)
        self._thread.start()

    def _run(self, ws: WebSocketTestSession, body: object) -> None:
        self.command = json.loads(ws.receive_text())
        response = {"command": "response", "messageId": self.command["messageId"], "body": body}
        ws.send_text(json.dumps(response))

    def wait(self) -> dict[str, object]:
        self._thread.join(timeout=5)
        assert not self._thread.is_alive()
        return self.command


@pytest.fixture
def asb(anki_wrapper: AnkiWrapper, monkeypatch: pytest.MonkeyPatch) -> ASBWebSocketServer:
    """Have the app lifespan serve the test's wrapper and a fast-timeout ASB server.

    ``with TestClient(app)`` is required (rather than a bare ``TestClient``) so
    the WebSocket session and the HTTP requests share one event loop; otherwise
    responses could never resolve the pending futures.
    """
    server = ASBWebSocketServer(post_mine_action=PostMineAction.none, response_timeout=0.3)
    monkeypatch.setattr(api, "create_anki_wrapper", lambda: anki_wrapper)
    monkeypatch.setattr(api, "create_asb_ws_server", lambda: server)
    monkeypatch.setattr(api.atexit, "register", lambda *_: None)
    return server


@pytest.fixture
def client(asb: ASBWebSocketServer) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize(
    ("action", "params", "field", "value", "expected"),
    [
        ("deckNames", {}, "", "", False),
        ("addNotes", {"notes": []}, "", "", False),
        ("addNote", {"note": {"fields": {"Front": "x"}}}, "", "", True),
        ("addNote", {"note": {"fields": {"Front": "x"}}}, "Misc", "", True),
        ("addNote", {"note": {"fields": {"Misc": "asb"}}}, "Misc", "asb", True),
        ("addNote", {"note": {"fields": {"Misc": "other"}}}, "Misc", "asb", False),
        ("addNote", {"note": {"fields": {"Front": "x"}}}, "Misc", "asb", False),
        ("addNote", {"note": {"fields": {"Misc": 1}}}, "Misc", "asb", False),
        ("addNote", {"note": "bad"}, "Misc", "asb", False),
        ("addNote", {}, "Misc", "asb", False),
    ],
)
def test_should_intercept_add_note(action, params, field, value, expected):
    server = ASBWebSocketServer(intercept_field=field, intercept_value=value)
    assert server.should_intercept_add_note(action, params) is False
    server.add_client(AsyncMock())
    assert server.should_intercept_add_note(action, params) is expected


@pytest.mark.asyncio
async def test_handle_message_ping_pong():
    ws = AsyncMock()
    await ASBWebSocketServer().handle_message(ws, "PING")
    ws.send_text.assert_awaited_once_with("PONG")


@pytest.mark.asyncio
async def test_handle_message_ignores_invalid_json():
    ws = AsyncMock()
    await ASBWebSocketServer().handle_message(ws, "not json")
    await ASBWebSocketServer().handle_message(ws, '{"command": "response"}')
    ws.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_and_await_response_matches_message_id():
    server = ASBWebSocketServer(response_timeout=1)
    ws = AsyncMock()
    server.add_client(ws)

    async def reply(payload: str) -> None:
        command = ClientCommand.model_validate_json(payload)
        unrelated = ClientResponse(command="response", messageId="other", body={"published": True})
        await server.handle_message(ws, unrelated.model_dump_json())
        matching = ClientResponse(command="response", messageId=command.messageId, body=[1, 2])
        await server.handle_message(ws, matching.model_dump_json())

    ws.send_text.side_effect = reply
    command = ClientCommand(command="get-subtitles", messageId=str(uuid.uuid4()), body={})
    response = await server.publish_message_and_await_response(command)
    assert response.messageId == command.messageId
    assert response.body == [1, 2]


@pytest.mark.asyncio
async def test_publish_and_await_response_without_clients_raises():
    with pytest.raises(NoClientResponseError):
        await ASBWebSocketServer().publish_message_and_await_response(
            ClientCommand(command="seek-timestamp", messageId="1", body={})
        )


@pytest.mark.asyncio
async def test_publish_and_await_response_timeout_raises():
    server = ASBWebSocketServer(response_timeout=0.05)
    server.add_client(AsyncMock())
    with pytest.raises(NoClientResponseError):
        await server.publish_message_and_await_response(
            ClientCommand(command="seek-timestamp", messageId="1", body={})
        )


@pytest.mark.asyncio
async def test_publish_message_survives_broken_client():
    server = ASBWebSocketServer()
    broken, healthy = AsyncMock(), AsyncMock()
    broken.send_text.side_effect = RuntimeError("closed")
    server.add_client(broken)
    server.add_client(healthy)
    await server.publish_message(ClientCommand(command="x", messageId="1", body={}))
    healthy.send_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_all_closes_clients():
    server = ASBWebSocketServer()
    clients = [AsyncMock(), AsyncMock()]
    clients[0].close.side_effect = RuntimeError("already closed")
    for ws in clients:
        server.add_client(ws)
    await server.disconnect_all()
    for ws in clients:
        ws.close.assert_awaited_once()
    assert not server.has_clients


@pytest.mark.asyncio
async def test_intercept_add_note_update_last_card_publishes_note_id():
    server = ASBWebSocketServer(post_mine_action=PostMineAction.update_last_card)
    ws = AsyncMock()
    server.add_client(ws)
    calls: list[str] = []

    async def add_note() -> int:
        calls.append("add")
        return 42

    params: JsonObject = {"note": {"fields": {"Front": "a"}}}
    assert await server.intercept_add_note(params, add_note) == 42
    command = ClientCommand.model_validate_json(ws.send_text.await_args.args[0])
    assert calls == ["add"]
    assert command.command == "mine-subtitle"
    assert uuid.UUID(command.messageId)
    assert command.body == {"fields": {"Front": "a"}, "postMineAction": 2, "noteId": 42}


@pytest.mark.asyncio
async def test_intercept_add_note_update_last_card_publishes_even_when_add_fails():
    """Mirror the Go proxy: asbplayer is still told to mine when our own
    addNote fails (e.g. duplicate), and the error propagates to Yomitan."""
    server = ASBWebSocketServer(post_mine_action=PostMineAction.update_last_card)
    ws = AsyncMock()
    server.add_client(ws)

    async def add_note() -> int:
        raise ValueError("cannot create note because it is a duplicate")

    with pytest.raises(ValueError, match="duplicate"):
        await server.intercept_add_note({"note": {"fields": {"Front": "a"}}}, add_note)
    command = ClientCommand.model_validate_json(ws.send_text.await_args.args[0])
    assert command.body == {"fields": {"Front": "a"}, "postMineAction": 2}


def test_ws_ping_pong_and_removal_on_disconnect(client: TestClient, asb: ASBWebSocketServer):
    with client.websocket_connect("/ws") as ws:
        ws.send_text("PING")
        assert ws.receive_text() == "PONG"
        assert asb.has_clients
    assert not asb.has_clients


def test_add_note_without_clients_is_not_intercepted(client: TestClient, anki_wrapper: AnkiWrapper):
    response = client.post("/", json=add_note_request("Plain"))
    assert response.status_code == 200
    assert response.json()["result"] > 0
    assert len(anki_wrapper.find_notes("Plain")) == 1


def test_add_note_update_last_card(client: TestClient, asb: ASBWebSocketServer):
    asb.post_mine_action = PostMineAction.update_last_card
    with client.websocket_connect("/ws") as ws:
        response = client.post("/", json=add_note_request("Mined"))
        command = json.loads(ws.receive_text())

    note_id = response.json()["result"]
    assert note_id > 0
    assert command["command"] == "mine-subtitle"
    assert command["body"] == {
        "fields": {"Front": "Mined", "Back": "World"},
        "postMineAction": 2,
        "noteId": note_id,
    }


def test_add_note_published_returns_minus_one(client: TestClient, anki_wrapper: AnkiWrapper):
    with client.websocket_connect("/ws") as ws:
        responder = Responder(ws, {"published": True})
        response = client.post("/", json=add_note_request("Intercepted"))
        command = responder.wait()

    assert response.json() == {"result": -1, "error": None}
    assert command["body"] == {
        "fields": {"Front": "Intercepted", "Back": "World"},
        "postMineAction": 0,
    }
    assert anki_wrapper.find_notes("Intercepted") == []


@pytest.mark.parametrize("body", [{"published": False}, {}, {"published": "yes"}, "x", None])
def test_add_note_not_published_falls_through(client: TestClient, anki_wrapper: AnkiWrapper, body):
    with client.websocket_connect("/ws") as ws:
        responder = Responder(ws, body)
        response = client.post("/", json=add_note_request("Fallback"))
        responder.wait()

    assert response.json()["result"] > 0
    assert len(anki_wrapper.find_notes("Fallback")) == 1


def test_add_note_timeout_returns_500_without_creating_note(
    client: TestClient, anki_wrapper: AnkiWrapper
):
    with client.websocket_connect("/ws"):
        response = client.post("/", json=add_note_request("Timeout"))

    assert response.status_code == 500
    assert response.json() is None
    assert anki_wrapper.find_notes("Timeout") == []


def test_add_note_intercept_field_mismatch_skips_interception(
    client: TestClient, asb: ASBWebSocketServer, anki_wrapper: AnkiWrapper
):
    asb.intercept_field, asb.intercept_value = "Back", "asbplayer"
    with client.websocket_connect("/ws"):
        response = client.post("/", json=add_note_request("NoMatch"))

    assert response.json()["result"] > 0
    assert len(anki_wrapper.find_notes("NoMatch")) == 1


def test_disconnect_ws_clients(client: TestClient, asb: ASBWebSocketServer):
    with client.websocket_connect("/ws"):
        assert asb.has_clients
        assert client.post("/disconnect-ws-clients").status_code == 200
        assert not asb.has_clients


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/asbplayer/load-subtitles", {"files": [{"name": "a.srt", "base64": "dA=="}]}),
        ("POST", "/asbplayer/seek", {"timestamp": 1.5}),
        ("GET", "/asbplayer/bound-media", None),
        ("GET", "/asbplayer/subtitles", None),
    ],
)
def test_asbplayer_endpoints_without_clients_return_500(client: TestClient, method, path, body):
    response = client.request(method, path, json=body)
    assert response.status_code == 500
    assert response.json() is None


def test_load_subtitles(client: TestClient):
    files = [{"name": "a.srt", "base64": "dA=="}]
    with client.websocket_connect("/ws") as ws:
        responder = Responder(ws, {})
        response = client.post("/asbplayer/load-subtitles", json={"files": files})
        command = responder.wait()

    assert response.status_code == 200
    assert response.text == ""
    assert command["command"] == "load-subtitles"
    assert command["body"] == {"files": files}


@pytest.mark.parametrize(
    ("request_body", "expected_body"),
    [
        ({"timestamp": 12.5, "mediaId": "abc"}, {"timestamp": 12.5, "mediaId": "abc"}),
        ({"timestamp": 3}, {"timestamp": 3.0}),
    ],
)
def test_seek(client: TestClient, request_body, expected_body):
    with client.websocket_connect("/ws") as ws:
        responder = Responder(ws, {})
        response = client.post("/asbplayer/seek", json=request_body)
        command = responder.wait()

    assert response.status_code == 200
    assert command["command"] == "seek-timestamp"
    assert command["body"] == expected_body


def test_seek_rejects_invalid_body(client: TestClient):
    assert client.post("/asbplayer/seek", json={"timestamp": "soon"}).status_code == 422


def test_bound_media(client: TestClient):
    media = {"media": [{"id": "x", "type": "streaming"}]}
    with client.websocket_connect("/ws") as ws:
        responder = Responder(ws, media)
        response = client.get("/asbplayer/bound-media")
        command = responder.wait()

    assert response.json() == media
    assert command["command"] == "get-bound-media"
    assert command["body"] == {}


@pytest.mark.parametrize(
    ("query", "expected_body"),
    [
        ("", {}),
        ("?mediaId=abc&trackNumbers=0,%201,x,2", {"mediaId": "abc", "trackNumbers": [0, 1, 2]}),
        ("?trackNumbers=x", {}),
    ],
)
def test_subtitles(client: TestClient, query, expected_body):
    subtitles = {"subtitles": [{"text": "hi", "start": 0, "end": 1, "track": 0}]}
    with client.websocket_connect("/ws") as ws:
        responder = Responder(ws, subtitles)
        response = client.get(f"/asbplayer/subtitles{query}")
        command = responder.wait()

    assert response.json() == subtitles
    assert command["command"] == "get-subtitles"
    assert command["body"] == expected_body


def test_create_asb_ws_server_uses_config():
    config = Config(
        COLLECTION_PATH="x.anki2",
        ASB_POST_MINE_ACTION=3,
        ASB_INTERCEPT_FIELD="Misc",
        ASB_INTERCEPT_VALUE="asb",
    )
    server = create_asb_ws_server(config)
    assert server.post_mine_action == 3
    assert (server.intercept_field, server.intercept_value) == ("Misc", "asb")


@pytest.mark.asyncio
async def test_lifespan_disconnects_clients_on_shutdown(asb: ASBWebSocketServer):
    async with api.app_lifespan(app):
        assert app.state.asb_ws_server is asb
        asb.add_client(AsyncMock())
    assert app.state.asb_ws_server is None
    assert not asb.has_clients


def test_update_last_card_flow_normalizes_asbplayer_audio(
    client: TestClient,
    asb: ASBWebSocketServer,
    anki_wrapper: AnkiWrapper,
    monkeypatch: pytest.MonkeyPatch,
):
    """End-to-end mining round trip in the default updateLastCard mode:
    Yomitan adds the note (version 2, raw replies) -> mine-subtitle is
    published -> asbplayer looks up the note, stores its .webm sentence audio
    (normalised on the way in) and writes [sound:...] into the field."""
    asb.post_mine_action = PostMineAction.update_last_card
    monkeypatch.setattr(
        anki_wrapper_module,
        "get_config",
        lambda: Config(COLLECTION_PATH="x", AUDIO_NORMALIZATION="fixed"),
    )
    monkeypatch.setattr(audio, "ffmpeg_available", lambda _path: True)
    normalized: list[tuple[str, float]] = []

    def normalize(_ffmpeg: str, data: bytes, suffix: str, target: float) -> bytes:
        normalized.append((suffix, target))
        return b"LOUD:" + data

    monkeypatch.setattr(audio, "normalize_audio", normalize)

    def yomitan(action: str, params: JsonObject) -> Any:
        response = client.post("/", json={"action": action, "params": params, "version": 2})
        assert response.status_code == 200
        return response.json()

    asbplayer = yomitan
    with client.websocket_connect("/ws") as ws:
        note: JsonObject = {
            "deckName": "Default",
            "modelName": "Basic",
            "fields": {"Front": "食べる", "Back": ""},
        }
        note_id = yomitan("addNote", {"note": note})
        command = json.loads(ws.receive_text())
    assert isinstance(note_id, int)
    assert command["body"]["noteId"] == note_id

    assert asbplayer("findNotes", {"query": "added:1"}) == [note_id]
    clip = base64.b64encode(b"opus-bytes").decode()
    stored = asbplayer("storeMediaFile", {"filename": "asbp_clip_1.webm", "data": clip})
    assert stored == "asbp_clip_1.webm"
    info = asbplayer("notesInfo", {"notes": [note_id]})
    assert info[0]["fields"]["Back"]["value"] == ""
    asbplayer(
        "updateNoteFields",
        {"note": {"id": note_id, "fields": {"Back": "[sound:asbp_clip_1.webm]"}}},
    )

    assert normalized == [(".webm", -23.0)]
    assert (
        anki_wrapper.retrieve_media_file("asbp_clip_1.webm")
        == base64.b64encode(b"LOUD:opus-bytes").decode()
    )
    assert asbplayer("notesInfo", {"notes": [note_id]})[0]["fields"]["Back"]["value"] == (
        "[sound:asbp_clip_1.webm]"
    )


def test_asbplayer_update_note_fields_payload_is_accepted(
    client: TestClient, anki_wrapper: AnkiWrapper
):
    """asbplayer sends the whole note object to updateNoteFields (deckName,
    modelName, tags, options next to id/fields) plus field names the note type
    may not have; the reference plugin ignores all of that."""
    note_id = anki_wrapper.add_note(
        {"deckName": "Default", "modelName": "Basic", "fields": {"Front": "超", "Back": ""}}
    )
    response = client.post(
        "/",
        json={
            "action": "updateNoteFields",
            "version": 6,
            "params": {
                "note": {
                    "id": note_id,
                    "deckName": "Default",
                    "modelName": "Basic",
                    "tags": [],
                    "options": {"allowDuplicate": True, "duplicateScope": "collection"},
                    "fields": {"Back": "[sound:asbp_clip.mp3]", "Picture": "<img src=x>"},
                }
            },
        },
    )
    assert response.json() == {"result": None, "error": None}
    assert anki_wrapper.notes_info([note_id])[0]["fields"]["Back"]["value"] == (  # type: ignore[index]
        "[sound:asbp_clip.mp3]"
    )
