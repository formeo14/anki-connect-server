import secrets
import tempfile
from functools import cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_collection_path() -> str:
    """Return a temp path with a random filename for a fresh Anki collection.

    Used when ``ANKICONNECT_COLLECTION_PATH`` is not configured. The random
    filename ensures each process gets its own collection, so concurrent MCP
    instances (e.g. spawned by a client) don't contend for the same SQLite lock.
    """
    return str(Path(tempfile.gettempdir()) / f"anki-connect-server-{secrets.token_hex(8)}.anki2")


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_prefix="ANKICONNECT_",
    )

    PORT: int = 8765
    BIND: str = "127.0.0.1"

    COLLECTION_PATH: str = ""

    ANKIWEB_USER: str | None = None
    ANKIWEB_PASS: str | None = None

    ANKIWEB_URL: str | None = None
    SYNC_AFTER_MINE: bool = False
    SYNC_AFTER_MINE_DELAY: float = 15.0

    # asbplayer WebSocket server settings (see asbwebsocket module).
    ASB_POST_MINE_ACTION: int = 2
    ASB_INTERCEPT_FIELD: str = ""
    ASB_INTERCEPT_VALUE: str = ""

    AUDIO_NORMALIZATION: str = "off"
    AUDIO_NORMALIZATION_TARGET_LUFS: float = -23.0
    FFMPEG_PATH: str = "ffmpeg"

    @field_validator("COLLECTION_PATH")
    @classmethod
    def _validate_collection_path(cls, v: str) -> str:
        if not v:
            return _default_collection_path()
        return v

    @field_validator("AUDIO_NORMALIZATION")
    @classmethod
    def _validate_audio_normalization(cls, v: str) -> str:
        value = v.strip().lower()
        if value not in {"off", "auto", "fixed"}:
            raise ValueError("AUDIO_NORMALIZATION must be one of: off, auto, fixed")
        return value


@cache
def get_config() -> Config:
    return Config()
