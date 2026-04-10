# Mach Mukke

Interactive music making system. Submit music wishes via web interface, and they get downloaded and added to your MPD queue automatically.

## Architecture

- **Server**: FastAPI app that receives music wishes, downloads them via yt-dlp, and hosts the web frontend
- **Web Client**: Browser-based UI to submit and track music wishes
- **Player Client**: Polls the server for new downloads and adds them to rmpc queue
- **API Key**: Can be used by the player client to submit wishes without the cookie login

## Setup

```bash
uv sync
```

## Usage

### 1. Start the Server

```bash
export MACH_MUKKE_API_KEY="your-secret-key"
export MACH_MUKKE_LASTFM_API_KEY="your-lastfm-key"
export MACH_MUKKE_LASTFM_API_SECRET="your-lastfm-secret"
# optional: default is false (wishing disabled)
export MACH_MUKKE_WISHING_ENABLED="false"
uv run src/mach_mukke/server.py
```

The server starts on `http://localhost:8000`.

### 2. Open the Web Client

Navigate to `http://localhost:8000` in your browser. Enter a song and submit your wish.

### 3. Start the Player Client

On the machine with rmpc:

```bash
export MACH_MUKKE_API_KEY="your-secret-key"
export MACH_MUKKE_SERVER_URL="http://your-server:8000"
uv run src/mach_mukke/player_client.py
```

The client opens a TUI that polls for new downloads, saves them to `~/Music/mach_mukke`, and adds them to the rmpc queue. It also shows whether wishing is currently enabled and supports `togglewishing` to switch that state (API key required). `togglewishing` gates the web UI/cookie flow; API-key clients (like the TUI) can still submit wishes. Use the `similar` command to fetch similar tracks for the current rmpc queue.

## Requirements

- Python 3.13+
- yt-dlp (for the server)
- rmpc (for the player client)
- a Last.fm API key + secret (for similar track lookup)
