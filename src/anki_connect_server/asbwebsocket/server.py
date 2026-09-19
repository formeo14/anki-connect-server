import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable

from fastapi import WebSocket
from pydantic import ValidationError

from anki_connect_server.asbwebsocket.models import (
    ClientCommand,
    ClientResponse,
    MineSubtitleResponseBody,
    PostMineAction,
)
from anki_connect_server.types import JsonObject, JsonValue

logger = logging.getLogger(__name__)


class NoClientResponseError(RuntimeError):
    pass


class ASBWebSocketServer:
    def __init__(
        self,
        *,
        post_mine_action: int = PostMineAction.update_last_card,
        intercept_field: str = "",
        intercept_value: str = "",
        response_timeout: float = 5.0,
    ) -> None:
        self.post_mine_action = post_mine_action
        self.intercept_field = intercept_field
        self.intercept_value = intercept_value
        self.response_timeout = response_timeout
        self._clients: set[WebSocket] = set()
        self._pending: dict[str, asyncio.Future[ClientResponse]] = {}

    @property
    def has_clients(self) -> bool:
        return bool(self._clients)

    def add_client(self, ws: WebSocket) -> None:
        self._clients.add(ws)
        logger.info("Client connected: %s", ws.client)

    def remove_client(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        logger.info("Client disconnected: %s", ws.client)

    async def disconnect_all(self) -> None:
        clients, self._clients = self._clients, set()
        for ws in clients:
            try:
                await ws.close()
            except Exception:
                logger.debug("Error closing WebSocket client", exc_info=True)
            logger.info("Forcefully disconnected client: %s", ws.client)

    async def handle_message(self, ws: WebSocket, data: str) -> None:
        if data == "PING":
            await ws.send_text("PONG")
            return
        try:
            response = ClientResponse.model_validate_json(data)
        except ValidationError:
            return
        future = self._pending.get(response.messageId)
        if future is not None and not future.done():
            future.set_result(response)

    async def publish_message(self, command: ClientCommand) -> None:
        payload = command.model_dump_json()
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception:
                logger.debug("Failed to send to WebSocket client", exc_info=True)

    async def publish_message_and_await_response(self, command: ClientCommand) -> ClientResponse:
        if not self._clients:
            raise NoClientResponseError("No asbplayer clients connected")
        future: asyncio.Future[ClientResponse] = asyncio.get_running_loop().create_future()
        self._pending[command.messageId] = future
        try:
            await self.publish_message(command)
            return await asyncio.wait_for(future, self.response_timeout)
        except TimeoutError:
            raise NoClientResponseError("Timed out waiting for asbplayer response") from None
        finally:
            del self._pending[command.messageId]

    def should_intercept_add_note(self, action: str, params: JsonObject) -> bool:
        if action != "addNote" or not self._clients:
            return False
        if not self.intercept_field or not self.intercept_value:
            return True
        note = params.get("note")
        if not isinstance(note, dict):
            return False
        fields = note.get("fields")
        if not isinstance(fields, dict):
            return False
        return fields.get(self.intercept_field) == self.intercept_value

    async def intercept_add_note(
        self, params: JsonObject, add_note: Callable[[], Awaitable[JsonValue]]
    ) -> JsonValue:
        note = params.get("note")
        fields = note.get("fields") if isinstance(note, dict) else None
        command = ClientCommand(
            command="mine-subtitle",
            messageId=str(uuid.uuid4()),
            body={"fields": fields, "postMineAction": self.post_mine_action},
        )

        if self.post_mine_action == PostMineAction.update_last_card:
            result = await add_note()
            if isinstance(result, int):
                command.body["noteId"] = result
            await self.publish_message(command)
            return result

        response = await self.publish_message_and_await_response(command)
        try:
            published = MineSubtitleResponseBody.model_validate(
                response.body, strict=True
            ).published
        except ValidationError:
            published = False
        if published:
            return -1
        return await add_note()

    async def load_subtitles(self, files: list[JsonValue]) -> None:
        await self.publish_message_and_await_response(
            ClientCommand(
                command="load-subtitles", messageId=str(uuid.uuid4()), body={"files": files}
            )
        )

    async def seek(self, timestamp: float, media_id: str = "") -> None:
        body: JsonObject = {"timestamp": timestamp}
        if media_id:
            body["mediaId"] = media_id
        await self.publish_message_and_await_response(
            ClientCommand(command="seek-timestamp", messageId=str(uuid.uuid4()), body=body)
        )

    async def get_bound_media(self) -> JsonValue:
        response = await self.publish_message_and_await_response(
            ClientCommand(command="get-bound-media", messageId=str(uuid.uuid4()), body={})
        )
        return response.body

    async def get_subtitles(
        self, media_id: str = "", track_numbers: list[int] | None = None
    ) -> JsonValue:
        body: JsonObject = {}
        if media_id:
            body["mediaId"] = media_id
        if track_numbers:
            body["trackNumbers"] = list(track_numbers)
        response = await self.publish_message_and_await_response(
            ClientCommand(command="get-subtitles", messageId=str(uuid.uuid4()), body=body)
        )
        return response.body
