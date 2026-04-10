# Mach Mukke

Interactive music making system. Submit music wishes via web interface, and they get downloaded and added to your MPD queue automatically.

## Architecture

- **Server**: FastAPI app that receives music wishes, downloads them via `yt-dlp`, and hosts the web frontend
- **Web Client**: Browser-based UI to submit and track music wishes
- **Player Client**: TUI Interface listening for completed downloads and adds them to the `rmpc` queue

## Recommended Hosting (Production)

Run Mach Mukke behind a secure reverse proxy that terminates TLS/SSL (for example Pangolin). The app container itself should stay on an internal Docker network and not expose public ports directly.

### `docker-compose.yml` example

```yml
services:
  mach_mukke:
    image: ghcr.io/404simon/mach-mukke:latest
    environment:
      - MACH_MUKKE_API_KEY=secret
      - MACH_MUKKE_LASTFM_API_SECRET=secret
      - MACH_MUKKE_LASTFM_API_KEY=key
      - BIRTHDAY_NAME=Mustermann
      - BIRTHDAY_AGE=999
    networks:
      - pangolin

networks:
  pangolin:
    name: pangolin
    external: true
```

## Local Development

### Setup

```bash
uv sync
```

### Start the server

```bash
export MACH_MUKKE_API_KEY="your-secret-key"
export MACH_MUKKE_LASTFM_API_KEY="your-lastfm-key"
export MACH_MUKKE_LASTFM_API_SECRET="your-lastfm-secret"
# optional: default is false (wishing disabled)
export MACH_MUKKE_WISHING_ENABLED="false"
uv run src/mach_mukke/server.py
```

The server starts on `http://localhost:8000`.
Navigate to `http://localhost:8000` in your browser. Enter a song and submit your wish.

### Start the player client

On the machine with `rmpc`:

```bash
export MACH_MUKKE_API_KEY="your-secret-key"
export MACH_MUKKE_SERVER_URL="http://your-server:8000"
uv run src/mach_mukke/player_client.py
```

The client opens a TUI that listens for download completion events via SSE, saves files to `~/Music/mach_mukke`, and adds them to the `rmpc` queue. It also shows whether wishing is currently enabled and supports `togglewishing` to switch that state (API key required). `togglewishing` gates the web UI/cookie flow; API-key clients (like the TUI) can still submit wishes. Use the `similar` command to fetch similar tracks for the current `rmpc` queue.

## Requirements

- Python 3.13+
- `yt-dlp` (for the server)
- `rmpc` (for the player client)
- Last.fm API key + secret (for similar track lookup)
