from enum import IntEnum

from pydantic import BaseModel

from anki_connect_server.types import JsonValue


class PostMineAction(IntEnum):
    none = 0
    show_anki_dialog = 1
    update_last_card = 2
    export_card = 3
    show_update_card_dialog = 4


class SubtitleFile(BaseModel):
    name: str
    base64: str


class AsbplayerLoadSubtitlesRequest(BaseModel):
    files: list[SubtitleFile] = []


class AsbplayerSeekRequest(BaseModel):
    timestamp: float
    mediaId: str = ""


class ClientCommand(BaseModel):
    command: str
    messageId: str
    body: dict[str, JsonValue]


class ClientResponse(BaseModel):
    command: str
    messageId: str
    body: JsonValue = None


class MineSubtitleResponseBody(BaseModel):
    published: bool = False
