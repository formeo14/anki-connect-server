# AnkiConnect Server - Docker Image

[![Docker](https://img.shields.io/badge/ghcr.io-anki--connect--server-blue)](https://github.com/formeo14/anki-connect-server/pkgs/container/anki-connect-server)

## Overview

Headless AnkiConnect-compatible REST API server with AnkiWeb sync support and MCP server integration. Run your Anki collection on any server without the Anki desktop app.

## Features

- **No Anki Desktop Required** - Direct collection access, no Qt/GUI needed
- **Full AnkiConnect API** - Version 6 API compatibility
- **AnkiWeb Sync** - Automatic synchronization with your AnkiWeb account
- **Audio Normalization** (optional) - ffmpeg-based loudness normalization of mined sentence audio to a fixed LUFS target or the measured level of your existing audio
- **MCP Server** - Model Context Protocol integration for AI assistants
- **Lightweight** - Python 3.12 + FastAPI on slim base image
- **Production Ready** - Proper health checks and signal handling

## Quick Start

### Run with Docker

```bash
docker run -d \
  -p 8765:8765 \
  -v /path/to/collection.anki2:/data/collection.anki2 \
  -e ANKICONNECT_COLLECTION_PATH=/data/collection.anki2 \
  -e ANKICONNECT_ANKIWEB_USER=your@email.com \
  -e ANKICONNECT_ANKIWEB_PASS=your_password \
  --name anki-connect-server \
  ghcr.io/formeo14/anki-connect-server:latest
```

### Docker Compose

```yaml
version: '3.8'

services:
  anki-connect-server:
    image: ghcr.io/formeo14/anki-connect-server:latest
    container_name: anki-connect-server
    ports:
      - "8765:8765"
    volumes:
      - ./collection.anki2:/data/collection.anki2
    environment:
      - ANKICONNECT_COLLECTION_PATH=/data/collection.anki2
      - ANKICONNECT_ANKIWEB_USER=${ANKIWEB_USER}
      - ANKICONNECT_ANKIWEB_PASS=${ANKIWEB_PASS}
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8765/health"]
      interval: 30s
      timeout: 10s
      retries: 3
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANKICONNECT_COLLECTION_PATH` | No | temp path | Path to your `.anki2` collection file |
| `ANKICONNECT_PORT` | No | `8765` | Server port |
| `ANKICONNECT_BIND` | No | `0.0.0.0` | Bind address |
| `ANKICONNECT_ANKIWEB_USER` | No | - | AnkiWeb username (for sync) |
| `ANKICONNECT_ANKIWEB_PASS` | No | - | AnkiWeb password (for sync) |
| `ANKICONNECT_ANKIWEB_URL` | No | - | Custom sync server URL |
| `ANKICONNECT_AUDIO_NORMALIZATION` | No | `off` | Loudness normalization for stored audio: `off`, `fixed`, or `auto` (requires ffmpeg) |
| `ANKICONNECT_AUDIO_NORMALIZATION_TARGET_LUFS` | No | `-23.0` | Target loudness (LUFS) for `fixed` mode and the fallback for `auto` |
| `ANKICONNECT_FFMPEG_PATH` | No | `ffmpeg` | Path to the ffmpeg binary used for normalization |

## API Usage

### Get Deck Names

```bash
curl -X POST http://localhost:8765/api \
  -H "Content-Type: application/json" \
  -d '{"action": "deckNames", "version": 6}'
```

### Add a Note

```bash
curl -X POST http://localhost:8765/api \
  -H "Content-Type: application/json" \
  -d '{
    "action": "addNote",
    "version": 6,
    "params": {
      "note": {
        "deckName": "Default",
        "modelName": "Basic",
        "fields": {"Front": "Hello", "Back": "World"}
      }
    }
  }'
```

### Sync with AnkiWeb

```bash
curl -X POST http://localhost:8765/api \
  -H "Content-Type: application/json" \
  -d '{"action": "sync", "version": 6}'
```

## Health Check

The container includes a health check endpoint:

```bash
curl http://localhost:8765/health
# Returns: {"status":"healthy"}
```

## Audio Normalization

The image ships with ffmpeg. Audio that reaches the server through
`storeMediaFile` or `addNote` attachments (asbplayer sentence clips, Yomitan
word audio) can be loudness-normalized before it is written to the media
folder:

- `ANKICONNECT_AUDIO_NORMALIZATION=fixed` - bring every clip to
  `ANKICONNECT_AUDIO_NORMALIZATION_TARGET_LUFS`
- `ANKICONNECT_AUDIO_NORMALIZATION=auto` - bring every clip to the measured
  level of the audio already in your collection. The level is measured once
  (in the background on first start, or with
  `anki-connect-server measure-loudness`) and stored in the collection
  config, so it survives restarts; until it exists the fixed target is used
- `off` (default) - bytes are stored untouched

Normalization applies a static gain (with a limiter) so short clips keep their
dynamics; clips already within 1 LU of the target are stored as-is. Files are
regular media and sync like any other media. Without ffmpeg, or when ffmpeg
fails on a file, the original bytes are stored.

## MCP Server

The image also includes MCP server support for AI assistants:

```bash
docker run -d \
  -v /path/to/collection.anki2:/data/collection.anki2 \
  -e ANKICONNECT_COLLECTION_PATH=/data/collection.anki2 \
  ghcr.io/formeo14/anki-connect-server \
  uv run anki-connect-server mcp
```

## Volumes

- `/data` - Directory for Anki collection and media files
  - Mount your `collection.anki2` file here
  - Anki media directory will be created automatically

## Security Notes

⚠️ **Important:**
- Never expose port 8765 to the public internet without authentication
- Use a reverse proxy with TLS for production deployments
- Store AnkiWeb credentials securely (use Docker secrets or external secret management)
- Bind to `127.0.0.1` if only local access is needed

## Building from Source

```bash
git clone https://github.com/formeo14/anki-connect-server.git
cd anki-connect-server
docker build -t anki-connect-server .
```

## Links

- [GitHub Repository](https://github.com/formeo14/anki-connect-server)
- [PyPI Package](https://pypi.org/project/anki-connect-server/)
- [Full Documentation](https://github.com/formeo14/anki-connect-server#readme)
- [AnkiConnect API Reference](https://github.com/FooSoft/anki-connect)
