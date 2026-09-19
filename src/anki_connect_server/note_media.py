import html
import logging
from collections.abc import Callable
from typing import cast

from anki.notes import Note

from anki_connect_server.types import JsonObject, JsonValue

logger = logging.getLogger(__name__)

type MediaStore = Callable[..., str | None]

_FIELD_TEMPLATES = {
    "audio": "[sound:{}]",
    "video": "[sound:{}]",
    "picture": '<img src="{}">',
}


def attach_note_media(note: Note, payload: JsonObject, store: MediaStore) -> None:
    for key, template in _FIELD_TEMPLATES.items():
        for media in _as_list(payload.get(key)):
            _attach(note, media, template, store)


def _as_list(value: JsonValue) -> list[JsonObject]:
    entries = value if isinstance(value, list) else [value]
    return [cast(JsonObject, entry) for entry in entries if isinstance(entry, dict)]


def _attach(note: Note, media: JsonObject, template: str, store: MediaStore) -> None:
    fields = [field for field in _as_str_list(media.get("fields")) if field in note]
    if not fields:
        return
    try:
        filename = store(
            filename=str(media.get("filename", "")),
            data=cast(str | None, media.get("data")),
            path=cast(str | None, media.get("path")),
            url=cast(str | None, media.get("url")),
            skip_hash=cast(str | None, media.get("skipHash")),
        )
    except Exception as e:
        logger.warning("Failed to store note media %s: %s", media.get("filename"), e)
        text = html.escape(str(e))
    else:
        if filename is None:
            return
        text = template.format(filename)
    for field in fields:
        note[field] += text


def _as_str_list(value: JsonValue) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
