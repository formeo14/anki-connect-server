import atexit
import logging
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from anki_connect_server import loudness
from anki_connect_server.anki_wrapper import AnkiWrapper
from anki_connect_server.asbwebsocket import (
    ASBWebSocketServer,
    NoClientResponseError,
    get_asb_server,
    no_client_response_handler,
)
from anki_connect_server.asbwebsocket import router as asb_router
from anki_connect_server.auto_sync import AutoSync
from anki_connect_server.config import Config, get_config
from anki_connect_server.handlers import API_VERSION, dispatch, error_reply, success_reply
from anki_connect_server.types import JsonValue

logger = logging.getLogger(__name__)


def create_anki_wrapper(config: Config | None = None) -> AnkiWrapper:
    settings = config or get_config()
    return AnkiWrapper(settings.COLLECTION_PATH)


def create_auto_sync(wrapper: AnkiWrapper, config: Config | None = None) -> AutoSync | None:
    settings = config or get_config()
    if not settings.SYNC_AFTER_MINE:
        return None
    return AutoSync(wrapper.sync_to_ankiweb, settings.SYNC_AFTER_MINE_DELAY)


def create_asb_ws_server(config: Config | None = None) -> ASBWebSocketServer:
    settings = config or get_config()
    return ASBWebSocketServer(
        post_mine_action=settings.ASB_POST_MINE_ACTION,
        intercept_field=settings.ASB_INTERCEPT_FIELD,
        intercept_value=settings.ASB_INTERCEPT_VALUE,
    )


def start_audio_target_measurement(wrapper: AnkiWrapper, config: Config | None = None) -> None:
    settings = config or get_config()
    if settings.AUDIO_NORMALIZATION != "auto" or loudness.stored_target(wrapper.col) is not None:
        return
    threading.Thread(target=wrapper.measure_and_store_audio_target, daemon=True).start()


@asynccontextmanager  # type: ignore[deprecated]
async def app_lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.anki_wrapper = create_anki_wrapper()
    atexit.register(app.state.anki_wrapper.close)
    app.state.asb_ws_server = create_asb_ws_server()
    app.state.auto_sync = create_auto_sync(app.state.anki_wrapper)
    start_audio_target_measurement(app.state.anki_wrapper)
    try:
        yield
    finally:
        auto_sync: AutoSync | None = getattr(app.state, "auto_sync", None)
        if auto_sync is not None:
            auto_sync.cancel()
            app.state.auto_sync = None
        wrapper: AnkiWrapper | None = getattr(app.state, "anki_wrapper", None)
        if wrapper is not None:
            wrapper.close()
            app.state.anki_wrapper = None
        asb: ASBWebSocketServer | None = getattr(app.state, "asb_ws_server", None)
        if asb is not None:
            await asb.disconnect_all()
            app.state.asb_ws_server = None


app = FastAPI(
    title="AnkiConnect Server",
    description="Headless AnkiConnect-compatible REST API server with AnkiWeb sync",
    version="0.1.0",
    lifespan=app_lifespan,
)
app.include_router(asb_router)
app.add_exception_handler(NoClientResponseError, no_client_response_handler)


class AnkiConnectRequest(BaseModel):
    action: str
    version: int = 6
    params: dict[str, JsonValue] = {}


@app.middleware("http")
async def cors_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    origin = request.headers.get("origin", "*")
    if request.method == "OPTIONS":
        headers = {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "*",
            "Access-Control-Allow-Headers": "*",
        }
        if request.headers.get("access-control-request-private-network") == "true":
            headers["Access-Control-Allow-Private-Network"] = "true"
        return Response(status_code=200, headers=headers)
    response = await call_next(request)
    if "origin" in request.headers:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "*"
    return response


def get_request_wrapper(request: Request) -> AnkiWrapper:
    wrapper: AnkiWrapper | None = getattr(request.app.state, "anki_wrapper", None)
    if wrapper is None:
        raise RuntimeError("Server not initialized")
    return wrapper


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.post("/", response_model=None)
@app.post("/api", response_model=None)
async def handle_request(request: Request) -> JsonValue:
    body = await request.body()
    if not body:
        return {"apiVersion": f"AnkiConnect v.{API_VERSION}"}
    try:
        req = AnkiConnectRequest.model_validate_json(body)
    except ValidationError as e:
        return error_reply(str(e))
    wrapper = get_request_wrapper(request)
    try:
        asb = get_asb_server(request)
        if asb.should_intercept_add_note(req.action, req.params):
            result = await asb.intercept_add_note(
                req.params, lambda: dispatch(req.action, req.params, wrapper)
            )
        else:
            result = await dispatch(req.action, req.params, wrapper)
        auto_sync: AutoSync | None = getattr(request.app.state, "auto_sync", None)
        if auto_sync is not None:
            auto_sync.schedule(req.action)
        return success_reply(req.version, result)
    except ValueError as e:
        # Client-facing errors (unknown action, missing/invalid params) are
        # reported in the response body per the AnkiConnect convention with
        # HTTP 200 so existing clients keep working.
        return error_reply(str(e))
    # Any other exception (corrupted collection, Anki backend crash, etc.) is
    # a server error and propagates as HTTP 500 via the handler below.


@app.exception_handler(Exception)
async def server_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Surface unexpected (non-ValueError) exceptions as HTTP 500 instead of
    swallowing them into a 200 with an error field. A corrupted-collection crash
    is a server fault, not a client error, and should not be reported with 200."""
    logger.error(
        "Unhandled exception processing %s %s: %s",
        request.method,
        request.url.path,
        exc,
        exc_info=exc,
    )
    return JSONResponse(status_code=500, content={"result": None, "error": str(exc)})


def run_server() -> None:
    """Run the FastAPI server."""
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(name)s: %(message)s")
    settings = get_config()
    uvicorn.run(app, host=settings.BIND, port=settings.PORT)


if __name__ == "__main__":
    run_server()
