"""Yomitan-compatibility tests.

These tests mimic the exact wire behaviour of Yomitan's AnkiConnect client
(ext/js/comm/anki-connect.js). Yomitan always sends POST JSON bodies of the
shape ``{action, params, version: 2}`` (its ``_localVersion`` is 2), which
means it expects RAW replies: on success the bare result without the
``{"result", "error"}`` wrapper, and on error ``{"result": null, "error": ...}``
(errors are always wrapped so Yomitan can throw ``Anki error: <msg>``).
"""

import base64

import pytest
from httpx import ASGITransport, AsyncClient

from anki_connect_server.api import app
from anki_connect_server.asbwebsocket import ASBWebSocketServer
from anki_connect_server.types import JsonValue

# Yomitan's AnkiConnect._localVersion.
YOMITAN_VERSION = 2


@pytest.fixture
def app_with_wrapper(anki_wrapper):
    """Attach a real AnkiWrapper to app.state for the duration of the test."""
    app.state.anki_wrapper = anki_wrapper
    app.state.asb_ws_server = ASBWebSocketServer()
    yield anki_wrapper
    app.state.anki_wrapper = None
    app.state.asb_ws_server = None


def yomitan_note(front: str = "Bonjour", back: str = "Hello") -> dict[str, JsonValue]:
    """Build a note exactly like Yomitan's AnkiNoteInput, including the
    ``options`` object Yomitan sends with duplicateScope settings."""
    return {
        "deckName": "Default",
        "modelName": "Basic",
        "fields": {"Front": front, "Back": back},
        "options": {
            "allowDuplicate": True,
            "duplicateScope": "collection",
            "duplicateScopeOptions": {
                "deckName": None,
                "checkChildren": False,
                "checkAllModels": False,
            },
        },
    }


async def yomitan_invoke(
    client: AsyncClient,
    action: str,
    params: dict[str, JsonValue] | None = None,
    version: int = YOMITAN_VERSION,
):
    """POST a request body shaped exactly like Yomitan's ``_invoke``:
    ``{action, params, version}`` with ``version: 2``."""
    body = {"action": action, "params": params if params is not None else {}, "version": version}
    return await client.post("/", json=body)


@pytest.mark.asyncio
async def test_yomitan_version_raw_and_wrapped_replies(app_with_wrapper):
    """Yomitan _getVersion/_invoke: ``version`` with version=2 must return the
    RAW number (no {"result", "error"} wrapper), while version=6 gets the
    wrapped reply."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await yomitan_invoke(client, "version")
        assert response.status_code == 200
        assert response.json() == 6

        response = await yomitan_invoke(client, "version", version=6)
        assert response.status_code == 200
        assert response.json() == {"result": 6, "error": None}


@pytest.mark.asyncio
async def test_yomitan_get_version_bare_number(app_with_wrapper):
    """Yomitan isConnected(): ``version`` with version=2 must reply with a bare
    JSON number, not an object -- anything else would make Yomitan treat the
    server as disconnected (typeof version === 'number' check)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await yomitan_invoke(client, "version")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, int)
        assert not isinstance(data, dict)
        assert data == 6


@pytest.mark.asyncio
async def test_yomitan_is_connected_flow(app_with_wrapper):
    """Yomitan isConnected(): a successful ``version`` call with version=2
    returning a number means the server is treated as connected."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await yomitan_invoke(client, "version")
        assert response.status_code == 200
        version = response.json()
        # Yomitan: typeof version === 'number' ? version : 0, then 0 < 2 means
        # "Extension and plugin versions incompatible".
        assert isinstance(version, (int, float))
        assert version >= YOMITAN_VERSION


@pytest.mark.asyncio
async def test_yomitan_add_note_raw_id(app_with_wrapper):
    """Yomitan addNote: sends {note: {..., options: {...}}} with version=2 and
    expects the note id as a RAW number (number|null)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await yomitan_invoke(client, "addNote", {"note": yomitan_note()})
        assert response.status_code == 200
        data = response.json()
        # Raw reply: a bare int, not {"result": ..., "error": ...}.
        assert isinstance(data, int)
        assert data > 0


@pytest.mark.asyncio
async def test_yomitan_duplicate_detection_flow(app_with_wrapper):
    """Yomitan duplicate flow: addNote with allowDuplicate:true creates the
    note; canAddNotesWithErrorDetail with allowDuplicate:false reports the
    duplicate error; multi findNotes (findNoteIds) returns the note id."""
    front, back = "Bonjour", "Hello"
    note = yomitan_note(front, back)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await yomitan_invoke(client, "addNote", {"note": note})
        assert response.status_code == 200
        note_id = response.json()
        assert isinstance(note_id, int)

        # Same note but allowDuplicate:false -> duplicate error detail.
        duplicate_check = dict(note)
        duplicate_check["options"] = {"allowDuplicate": False}
        response = await yomitan_invoke(
            client, "canAddNotesWithErrorDetail", {"notes": [duplicate_check]}
        )
        assert response.status_code == 200
        details = response.json()
        assert isinstance(details, list)
        assert len(details) == 1
        assert details[0]["canAdd"] is False
        assert details[0]["error"] == "cannot create note because it is a duplicate"

        # Yomitan findNoteIds: multi with a version-less findNotes sub-action;
        # the reply must be a raw array (of raw arrays) containing the note id.
        query = f'"deck:Default" "front:{front}"'
        response = await yomitan_invoke(
            client, "multi", {"actions": [{"action": "findNotes", "params": {"query": query}}]}
        )
        assert response.status_code == 200
        result = response.json()
        assert isinstance(result, list)
        assert isinstance(result[0], list)
        assert note_id in result[0]


@pytest.mark.asyncio
async def test_yomitan_deck_scoped_duplicate_check(app_with_wrapper):
    """Yomitan duplicateScope "deck": a note is only a duplicate when a note
    with the same first field has a card in the checked deck; "deck-root"
    style checks pass an explicit deckName plus checkChildren."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await yomitan_invoke(client, "createDeck", {"deck": "Mining::Sub"})
        note = yomitan_note()
        note["deckName"] = "Mining::Sub"
        response = await yomitan_invoke(client, "addNote", {"note": note})
        assert isinstance(response.json(), int)

        def scoped(deck_name: str, **scope_options: JsonValue) -> dict[str, JsonValue]:
            check = yomitan_note()
            check["deckName"] = deck_name
            check["options"] = {
                "allowDuplicate": False,
                "duplicateScope": "deck",
                "duplicateScopeOptions": {
                    "deckName": None,
                    "checkChildren": False,
                    "checkAllModels": False,
                    **scope_options,
                },
            }
            return check

        response = await yomitan_invoke(
            client,
            "canAddNotes",
            {
                "notes": [
                    scoped("Default"),
                    scoped("Mining::Sub"),
                    scoped("Default", deckName="Mining", checkChildren=True),
                    scoped("Default", deckName="Mining"),
                    scoped("Default", deckName="Missing"),
                ]
            },
        )
        assert response.status_code == 200
        assert response.json() == [True, False, False, True, True]


@pytest.mark.asyncio
async def test_yomitan_add_note_attachments(app_with_wrapper):
    """Yomitan (word audio) and asbplayer exportCard both attach media through
    the AnkiConnect note.audio / note.picture objects: the file is stored and a
    [sound:..] / <img> reference is appended to the listed fields."""
    audio_data = base64.b64encode(b"forvo").decode()
    picture_data = base64.b64encode(b"png").decode()
    note = yomitan_note()
    note["audio"] = [
        {"filename": "yomitan_audio_1.mp3", "data": audio_data, "fields": ["Back"]},
        {"filename": "ignored.mp3", "data": audio_data, "fields": ["NoSuchField"]},
    ]
    note["picture"] = {"filename": "asbp_shot.jpeg", "data": picture_data, "fields": ["Back"]}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await yomitan_invoke(client, "addNote", {"note": note})
        note_id = response.json()
        assert isinstance(note_id, int)

        response = await yomitan_invoke(client, "notesInfo", {"notes": [note_id]})
        back = response.json()[0]["fields"]["Back"]["value"]
        assert back == 'Hello[sound:yomitan_audio_1.mp3]<img src="asbp_shot.jpeg">'

        response = await yomitan_invoke(client, "retrieveMediaFile", {"filename": "asbp_shot.jpeg"})
        assert response.json() == picture_data
        response = await yomitan_invoke(client, "retrieveMediaFile", {"filename": "ignored.mp3"})
        assert response.json() is None


@pytest.mark.asyncio
async def test_yomitan_can_add_notes_ignores_attachments(app_with_wrapper):
    note = yomitan_note()
    note["audio"] = {
        "filename": "probe.mp3",
        "data": base64.b64encode(b"x").decode(),
        "fields": ["Back"],
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await yomitan_invoke(client, "canAddNotes", {"notes": [note]})
        assert response.json() == [True]
        response = await yomitan_invoke(client, "retrieveMediaFile", {"filename": "probe.mp3"})
        assert response.json() is None


@pytest.mark.asyncio
async def test_yomitan_can_add_notes_does_not_insert(app_with_wrapper):
    """Yomitan canAddNotes: must be validate-only (no insertion). After calling
    canAddNotes with a new note, findNotes must confirm it was not created."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await yomitan_invoke(client, "addNote", {"note": yomitan_note()})
        first_id = response.json()

        response = await yomitan_invoke(
            client, "canAddNotes", {"notes": [yomitan_note(front="Second", back="Note")]}
        )
        assert response.status_code == 200
        assert response.json() == [True]

        response = await yomitan_invoke(client, "findNotes", {"query": '"front:Second"'})
        assert response.json() == []
        response = await yomitan_invoke(client, "findNotes", {"query": "deck:Default"})
        assert response.json() == [first_id]


@pytest.mark.asyncio
async def test_yomitan_store_media_file_roundtrip(app_with_wrapper):
    """Yomitan storeMediaFile: {filename, data} with version=2 must return the
    stored filename as a RAW string; retrieveMediaFile returns the same base64."""
    data = base64.b64encode(b"testaudio").decode()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await yomitan_invoke(
            client,
            "storeMediaFile",
            {"filename": "yomitan_audio_1.mp3", "data": data},
        )
        assert response.status_code == 200
        assert response.json() == "yomitan_audio_1.mp3"

        response = await yomitan_invoke(
            client, "retrieveMediaFile", {"filename": "yomitan_audio_1.mp3"}
        )
        assert response.status_code == 200
        assert response.json() == data


@pytest.mark.asyncio
async def test_yomitan_notes_info_shape(app_with_wrapper):
    """Yomitan notesInfo/_normalizeNoteInfoArray: needs noteId, modelName,
    tags, fields (value/order) and cards (number[]) keys; a missing note must
    yield an empty object entry (normalized to null by Yomitan)."""
    front, back = "InfoFront", "InfoBack"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await yomitan_invoke(client, "addNote", {"note": yomitan_note(front, back)})
        note_id = response.json()

        response = await yomitan_invoke(client, "notesInfo", {"notes": [note_id, 999999999]})
        assert response.status_code == 200
        result = response.json()
        assert len(result) == 2
        entry = result[0]
        assert entry["noteId"] == note_id
        assert entry["modelName"] == "Basic"
        assert isinstance(entry["tags"], list)
        assert entry["fields"]["Front"]["value"] == front
        assert entry["fields"]["Front"]["order"] == 0
        assert entry["fields"]["Back"]["value"] == back
        assert isinstance(entry["cards"], list)
        assert all(isinstance(card_id, int) for card_id in entry["cards"])
        # Missing note keeps index alignment with an empty object.
        assert result[1] == {}


@pytest.mark.asyncio
async def test_yomitan_cards_info_shape(app_with_wrapper):
    """Yomitan cardsInfo/_normalizeCardInfoArray: needs cardId, note, flags and
    queue keys; a missing card must yield an empty object entry."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await yomitan_invoke(client, "addNote", {"note": yomitan_note("CardFront")})
        note_id = response.json()
        response = await yomitan_invoke(client, "findCards", {"query": f"nid:{note_id}"})
        card_id = response.json()[0]

        response = await yomitan_invoke(client, "cardsInfo", {"cards": [card_id, 999999999]})
        assert response.status_code == 200
        result = response.json()
        assert len(result) == 2
        entry = result[0]
        assert entry["cardId"] == card_id
        assert entry["note"] == note_id
        assert isinstance(entry["flags"], int)
        assert isinstance(entry["queue"], int)
        assert result[1] == {}


@pytest.mark.asyncio
async def test_yomitan_unknown_action_error(app_with_wrapper):
    """Yomitan _invoke: a non-ok action must produce {"result": null,
    "error": "unsupported action"} with HTTP 200 so Yomitan throws
    'Anki error: unsupported action' (isErrorUnsupportedAction)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await yomitan_invoke(client, "notAnAction")
        assert response.status_code == 200
        assert response.json() == {"result": None, "error": "unsupported action"}


@pytest.mark.asyncio
async def test_yomitan_api_reflect(app_with_wrapper):
    """Yomitan apiReflect/apiExists: {scopes, actions} with version=2 must
    return {"scopes": [...], "actions": [...]} filtered to supported actions."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await yomitan_invoke(
            client,
            "apiReflect",
            {"scopes": ["actions"], "actions": ["addNote", "notAnAction"]},
        )
        assert response.status_code == 200
        assert response.json() == {"scopes": ["actions"], "actions": ["addNote"]}


@pytest.mark.asyncio
async def test_yomitan_gui_actions_report_unsupported(app_with_wrapper):
    """Yomitan viewNotes: guiEditNote falls back to guiBrowse only when the
    server reports "unsupported action"; a headless server has no GUI, so both
    must report exactly that instead of pretending to succeed."""
    unsupported_gui_calls: list[tuple[str, dict[str, JsonValue]]] = [
        ("guiEditNote", {"note": 1}),
        ("guiBrowse", {"query": "nid:1"}),
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for action, params in unsupported_gui_calls:
            response = await yomitan_invoke(client, action, params)
            assert response.status_code == 200
            assert response.json() == {"result": None, "error": "unsupported action"}


@pytest.mark.asyncio
async def test_yomitan_sync_without_credentials(app_with_wrapper):
    """Yomitan makeAnkiSync: sends _invoke('sync', {version}) with version=2,
    i.e. the params object carries the version key. Params validation must
    pass and the missing-credentials error must surface with HTTP 200."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await yomitan_invoke(client, "sync", {"version": YOMITAN_VERSION})
        assert response.status_code == 200
        data = response.json()
        assert data["result"] is None
        assert "ANKIWEB" in data["error"]


@pytest.mark.asyncio
async def test_yomitan_update_note_fields(app_with_wrapper):
    """Yomitan updateNoteFields: {note: {id, fields}} with version=2 must
    resolve to null (raw) and the change must be visible via notesInfo."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await yomitan_invoke(client, "addNote", {"note": yomitan_note()})
        note_id = response.json()

        response = await yomitan_invoke(
            client,
            "updateNoteFields",
            {"note": {"id": note_id, "fields": {"Front": "changed"}}},
        )
        assert response.status_code == 200
        assert response.json() is None

        response = await yomitan_invoke(client, "notesInfo", {"notes": [note_id]})
        assert response.json()[0]["fields"]["Front"]["value"] == "changed"


@pytest.mark.asyncio
async def test_yomitan_add_note_case_insensitive_fields(app_with_wrapper):
    """Yomitan sends field names as declared by the note type, but casing may
    differ; createNote must match fields case-insensitively."""
    note = yomitan_note()
    note["fields"] = {"front": "Bonjour", "BACK": "Hello"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await yomitan_invoke(client, "addNote", {"note": note})
        assert response.status_code == 200
        note_id = response.json()
        assert isinstance(note_id, int)

        response = await yomitan_invoke(client, "notesInfo", {"notes": [note_id]})
        entry = response.json()[0]
        assert entry["fields"]["Front"]["value"] == "Bonjour"
        assert entry["fields"]["Back"]["value"] == "Hello"


@pytest.mark.asyncio
async def test_yomitan_multi_error_and_raw_shapes(app_with_wrapper):
    """Yomitan _invokeMulti/findNoteIds: multi replies must be a raw array
    where each sub-action error is {"result": null, "error": ...} and each
    successful sub-action is the RAW result."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await yomitan_invoke(
            client,
            "multi",
            {
                "actions": [
                    {"action": "notAnAction", "params": {}},
                    {"action": "findNotes", "params": {"query": "deck:Default"}},
                ]
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0] == {"result": None, "error": "unsupported action"}
        assert isinstance(result[1], list)
        assert result[1] == []


@pytest.mark.asyncio
async def test_empty_body_returns_api_version(app_with_wrapper):
    """Yomitan-compatible empty POST: the real AnkiConnect web server replies
    to an empty request body with {"apiVersion": "AnkiConnect v.6"}."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/", content=b"", headers={"Content-Type": "application/json"})
        assert response.status_code == 200
        assert response.json() == {"apiVersion": "AnkiConnect v.6"}


@pytest.mark.asyncio
async def test_yomitan_cors_headers(app_with_wrapper):
    """Yomitan is a browser extension: its fetch() sends an Origin header and
    a CORS preflight (OPTIONS). The server must answer the preflight with 200
    and the appropriate Access-Control headers, including the private-network
    allowance for localhost access."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Preflight.
        response = await client.options(
            "/",
            headers={
                "Origin": "chrome-extension://yomitan",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "chrome-extension://yomitan"

        # Preflight from a private network (localhost) request.
        response = await client.options(
            "/",
            headers={
                "Origin": "chrome-extension://yomitan",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Private-Network": "true",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-private-network"] == "true"

        # Actual POST with an Origin header must expose the reply cross-origin.
        response = await client.post(
            "/",
            json={"action": "version", "params": {}, "version": YOMITAN_VERSION},
            headers={"Origin": "chrome-extension://yomitan"},
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "chrome-extension://yomitan"


@pytest.mark.asyncio
async def test_yomitan_suspend_flow(app_with_wrapper):
    """Yomitan suspendCards: suspend {cards} with version=2 returns a raw
    boolean; re-suspending an already-suspended card returns False."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await yomitan_invoke(client, "addNote", {"note": yomitan_note("SuspendMe")})
        note_id = response.json()
        response = await yomitan_invoke(client, "findCards", {"query": f"nid:{note_id}"})
        card_id = response.json()[0]

        response = await yomitan_invoke(client, "suspend", {"cards": [card_id]})
        assert response.status_code == 200
        assert response.json() is True

        response = await yomitan_invoke(client, "suspend", {"cards": [card_id]})
        assert response.status_code == 200
        assert response.json() is False
