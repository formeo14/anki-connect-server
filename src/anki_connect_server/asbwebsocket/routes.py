import logging
from contextlib import suppress

from fastapi import APIRouter, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from anki_connect_server.asbwebsocket.models import (
    AsbplayerLoadSubtitlesRequest,
    AsbplayerSeekRequest,
)
from anki_connect_server.asbwebsocket.server import ASBWebSocketServer
from anki_connect_server.types import JsonValue

logger = logging.getLogger(__name__)

router = APIRouter()


def get_asb_server(request: Request | WebSocket) -> ASBWebSocketServer:
    server: ASBWebSocketServer | None = getattr(request.app.state, "asb_ws_server", None)
    if server is None:
        raise RuntimeError("Server not initialized")
    return server


async def no_client_response_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.warning("%s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content=None)


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    server = get_asb_server(ws)
    await ws.accept()
    server.add_client(ws)
    try:
        while True:
            await server.handle_message(ws, await ws.receive_text())
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("WebSocket client error", exc_info=True)
    finally:
        server.remove_client(ws)


@router.post("/disconnect-ws-clients")
async def disconnect_ws_clients(request: Request) -> Response:
    await get_asb_server(request).disconnect_all()
    return Response()


@router.post("/asbplayer/load-subtitles")
async def asbplayer_load_subtitles(
    body: AsbplayerLoadSubtitlesRequest, request: Request
) -> Response:
    await get_asb_server(request).load_subtitles([f.model_dump() for f in body.files])
    return Response()


@router.post("/asbplayer/seek")
async def asbplayer_seek(body: AsbplayerSeekRequest, request: Request) -> Response:
    await get_asb_server(request).seek(body.timestamp, body.mediaId)
    return Response()


@router.get("/asbplayer/bound-media")
async def asbplayer_bound_media(request: Request) -> JsonValue:
    return await get_asb_server(request).get_bound_media()


@router.get("/asbplayer/subtitles")
async def asbplayer_subtitles(
    request: Request,
    media_id: str = Query("", alias="mediaId"),
    track_numbers: str = Query("", alias="trackNumbers"),
) -> JsonValue:
    parsed: list[int] = []
    for part in track_numbers.split(","):
        with suppress(ValueError):
            parsed.append(int(part.strip()))
    return await get_asb_server(request).get_subtitles(media_id, parsed)
