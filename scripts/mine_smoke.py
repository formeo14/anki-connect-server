"""Replay the Yomitan + asbplayer mining flow against a running server.

    uv run python scripts/mine_smoke.py            # mine one test note with a quiet clip
    uv run python scripts/mine_smoke.py --sync     # push the collection to AnkiWeb
    uv run python scripts/mine_smoke.py --cleanup  # delete the test deck again

Plays Yomitan (addNote, version 2) and then asbplayer in update-last-card
mode (findNotes -> storeMediaFile of a .webm -> updateNoteFields) and reports
the clip's loudness before and after the server stored it.
"""

import argparse
import base64
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from anki_connect_server import audio

SERVER = "http://127.0.0.1:8765"
TEST_DECK = "AnkiConnectServer Test"
CLIP_NAME = "asbp_smoke_test.webm"


def invoke(action: str, params: dict[str, object] | None = None, version: int = 6) -> object:
    body = json.dumps({"action": action, "params": params or {}, "version": version}).encode()
    request = urllib.request.Request(
        SERVER, body, {"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request) as response:  # noqa: S310
        reply = json.load(response)
    if version > 4:
        if reply["error"] is not None:
            sys.exit(f"{action} failed: {reply['error']}")
        return reply["result"]
    if isinstance(reply, dict) and reply.get("error"):
        sys.exit(f"{action} failed: {reply['error']}")
    return reply


def quiet_clip() -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / CLIP_NAME
        subprocess.run(  # noqa: S603
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                "-af",
                "volume=-30dB",
                "-c:a",
                "libopus",
                str(path),
            ],
            check=True,
        )
        return path.read_bytes()


def loudness(data: bytes) -> str:
    value = audio.measure_loudness("ffmpeg", data, ".webm")
    return f"{value:.1f} LUFS" if value is not None else "unmeasurable"


def pick_model() -> tuple[str, list[str]]:
    names: list[str] = invoke("modelNames")  # type: ignore[assignment]
    for name in sorted(names, key=lambda n: (n != "Basic", n)):
        fields: list[str] = invoke("modelFieldNames", {"modelName": name})  # type: ignore[assignment]
        if len(fields) >= 2:
            return name, fields
    sys.exit("No note type with at least two fields found")


def mine() -> None:
    model, (word_field, sentence_field, *_) = pick_model()
    invoke("createDeck", {"deck": TEST_DECK})
    clip = quiet_clip()
    print(f"Clip before: {loudness(clip)}")

    note = {
        "deckName": TEST_DECK,
        "modelName": model,
        "fields": {word_field: "食べる (smoke test)", sentence_field: ""},
        "options": {"allowDuplicate": True},
    }
    note_id = invoke("addNote", {"note": note}, version=2)
    print(f"Yomitan addNote -> note {note_id}")

    recent = invoke("findNotes", {"query": "added:1"})
    print(f"asbplayer findNotes added:1 -> {len(recent)} note(s)")  # type: ignore[arg-type]
    stored_name = invoke(
        "storeMediaFile", {"filename": CLIP_NAME, "data": base64.b64encode(clip).decode()}
    )
    invoke(
        "updateNoteFields",
        {"note": {"id": note_id, "fields": {sentence_field: f"[sound:{stored_name}]"}}},
    )
    print(f"asbplayer storeMediaFile + updateNoteFields -> [sound:{stored_name}]")

    stored = base64.b64decode(invoke("retrieveMediaFile", {"filename": stored_name}))  # type: ignore[arg-type]
    print(f"Clip after:  {loudness(stored)}")
    info = invoke("notesInfo", {"notes": [note_id]})
    print(f"Note field:  {info[0]['fields'][sentence_field]['value']}")  # type: ignore[index]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sync", action="store_true", help="sync the collection to AnkiWeb")
    parser.add_argument("--cleanup", action="store_true", help=f"delete the '{TEST_DECK}' deck")
    args = parser.parse_args()
    if args.cleanup:
        invoke("deleteDecks", {"decks": [TEST_DECK], "cardsToo": True})
        print(f"Deleted deck '{TEST_DECK}'")
    elif args.sync:
        print(json.dumps(invoke("sync"), indent=2))
    else:
        mine()


if __name__ == "__main__":
    main()
