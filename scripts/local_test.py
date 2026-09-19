"""Start a local server against your real AnkiWeb collection.

    uv run python scripts/local_test.py

First run: writes a .env and asks for AnkiWeb credentials. Then: syncs (downloads the collection on first start), measures the audio
target when AUDIO_NORMALIZATION=auto, and serves on the configured port.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENV_FILE = REPO / ".env"
ENV_TEMPLATE = """# Local test configuration written by scripts/local_test.py (gitignored).
ANKICONNECT_ANKIWEB_USER=
ANKICONNECT_ANKIWEB_PASS=

# Local copy of your AnkiWeb collection (downloaded on first start; gitignored)
ANKICONNECT_COLLECTION_PATH=local-test/collection.anki2
ANKICONNECT_PORT=8765
ANKICONNECT_BIND=127.0.0.1

# Sync to AnkiWeb automatically 15 s after the last mining request
ANKICONNECT_SYNC_AFTER_MINE=true

# asbplayer: update the note Yomitan just added with the sentence clip + screenshot
ANKICONNECT_ASB_POST_MINE_ACTION=2

# Loudness normalization: fixed (to the LUFS below) | auto (match your existing audio) | off
ANKICONNECT_AUDIO_NORMALIZATION=auto
ANKICONNECT_AUDIO_NORMALIZATION_TARGET_LUFS=-23.0
"""


def ensure_env_file() -> None:
    if ENV_FILE.exists():
        return
    ENV_FILE.write_text(ENV_TEMPLATE, encoding="utf-8")
    sys.exit(
        f"Created {ENV_FILE}\n"
        "Fill in ANKICONNECT_ANKIWEB_USER and ANKICONNECT_ANKIWEB_PASS, then run this again."
    )


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    ensure_env_file()
    from anki_connect_server import loudness
    from anki_connect_server.api import create_anki_wrapper, run_server
    from anki_connect_server.config import get_config

    config = get_config()
    if not (config.ANKIWEB_USER and config.ANKIWEB_PASS):
        sys.exit(f"Set ANKICONNECT_ANKIWEB_USER and ANKICONNECT_ANKIWEB_PASS in {ENV_FILE}")
    Path(config.COLLECTION_PATH).parent.mkdir(parents=True, exist_ok=True)

    wrapper = create_anki_wrapper(config)
    try:
        result = wrapper.sync_to_ankiweb(progress=lambda message: print(f"  {message}"))
        print(f"Sync: {result.collection.outcome.value}, media {result.media.outcome}")
        print(f"Decks: {', '.join(wrapper.deck_names())}")
        if config.AUDIO_NORMALIZATION == "auto" and loudness.stored_target(wrapper.col) is None:
            print("Measuring loudness of existing audio...")
            target = wrapper.measure_and_store_audio_target()
            print(
                f"  target: {target:.1f} LUFS" if target else "  no audio found, using fixed target"
            )
    finally:
        wrapper.close()

    print(
        f"\nNormalization: {config.AUDIO_NORMALIZATION} "
        f"(target {config.AUDIO_NORMALIZATION_TARGET_LUFS} LUFS)\n"
        f"Yomitan   -> AnkiConnect address: http://{config.BIND}:{config.PORT}\n"
        f"asbplayer -> AnkiConnect URL:     http://{config.BIND}:{config.PORT}\n"
        f"asbplayer -> WebSocket URL:       ws://{config.BIND}:{config.PORT}/ws\n"
        "Smoke test (other terminal): uv run python scripts/mine_smoke.py\n"
        "Push to AnkiWeb when done:   uv run python scripts/mine_smoke.py --sync\n"
    )
    run_server()


if __name__ == "__main__":
    main()
