import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

MUTATING_ACTIONS = frozenset(
    {
        "addNote",
        "addNotes",
        "updateNoteFields",
        "storeMediaFile",
        "deleteMediaFile",
        "addTags",
        "removeTags",
        "deleteNotes",
        "suspend",
        "unsuspend",
        "changeDeck",
        "createDeck",
        "deleteDecks",
    }
)


class AutoSync:
    def __init__(self, sync: Callable[[], object], delay_seconds: float) -> None:
        self._sync = sync
        self._delay = delay_seconds
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def schedule(self, action: str) -> None:
        if action not in MUTATING_ACTIONS:
            return
        logger.info("Auto-sync scheduled in %.0fs after %s", self._delay, action)
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._delay, self._run)
            self._timer.daemon = True
            self._timer.start()

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _run(self) -> None:
        with self._lock:
            self._timer = None
        try:
            self._sync()
            logger.info("Auto-sync after mining completed")
        except Exception as e:
            logger.warning("Auto-sync after mining failed: %s", e)
