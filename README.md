# TapoWeb

A small web UI for controlling a TP-Link Tapo camera (developed against the C520WS PTZ model). It gives you a live HLS stream, pan/tilt/zoom controls, presets, snapshots, SD card recording playback and downloads, and — with your Tapo account password — spotlight and day/night control.

The app talks to the camera directly on your local network. Credentials are kept in memory only and are never written to disk.

> This is an unofficial project and is not affiliated with or endorsed by TP-Link.

## Features

- Low-latency live view (RTSP → HLS via ffmpeg)
- Pan/tilt with on-screen D-pad and arrow keys, speed slider
- Presets: save, go to, delete
- JPEG snapshots
- Digital zoom and fullscreen
- Spotlight toggle and day/night mode via the Tapo local API
- SD card recordings: browse clips by date, stream them directly, and download them as MP4
- Separate Live view and Recordings tabs
- Optional auto-connect from environment variables
- Ready-to-run Docker image and Compose setup

## Requirements

- A Tapo camera with ONVIF/RTSP enabled
- A camera account (username/password) for ONVIF/RTSP
- Your Tapo app account password for spotlight, night mode and SD card recordings (optional)

For Docker, that's all you need — Python and ffmpeg are bundled in the image. For a manual install you also need Python 3.9+ and [ffmpeg](https://ffmpeg.org/) in `PATH`.

## Run with Docker (recommended)

The container runs the web UI on port `8080` and includes ffmpeg, so nothing has to be installed on the host.

### 1. Configure (optional)

```bash
cp .env.example .env
```

Edit `.env` if you want the app to connect to the camera automatically at startup:

```dotenv
TAPO_HOST=192.168.1.100
TAPO_USER=camera-user
TAPO_PASS=camera-pass
TAPO_CLOUD_PASS=tapo-account-pass
```

You can also leave these blank and enter the details in the Settings dialog after the app starts. Note that credentials entered in the UI live in memory only, so they are lost whenever the container restarts — use `.env` for a persistent setup.

### 2. Start the container

```bash
docker compose up -d --build
```

Then open <http://localhost:8080> (or `http://<host-ip>:8080` from another device on your network).

Check that it is running and healthy:

```bash
docker compose ps
docker compose logs -f
```

### 3. Stop, update, or remove

```bash
docker compose stop          # stop
docker compose up -d --build # rebuild and restart after updating the code
docker compose down          # stop and remove the container
```

### Docker options

`compose.yaml` reads the following variables from `.env` (or your shell):

| Variable | Default | Description |
| --- | --- | --- |
| `TAPO_WEB_PORT` | `8080` | Host port for the web UI (the container always listens on `8080`) |
| `TAPO_WEB_BIND` | `0.0.0.0` | Host interface to publish on; use `127.0.0.1` to restrict access to the Docker host |
| `TAPO_HOST` | – | Camera IP address or hostname |
| `TAPO_USER` | – | Camera account username |
| `TAPO_PASS` | – | Camera account password |
| `TAPO_PORT` | `2020` | ONVIF port |
| `TAPO_STREAM` | `stream1` | `stream1` (HD) or `stream2` (SD) |
| `TAPO_CLOUD_PASS` | – | Tapo account password for spotlight, night mode and SD card recordings |

The image also sets `TAPO_WEB_HOST=0.0.0.0` and `TAPO_WEB_PORT=8080` internally. The same environment variables work with a plain `docker run`:

```bash
docker build -t tapoweb .

docker run -d \
  --name tapoweb \
  --restart unless-stopped \
  -p 8080:8080 \
  -e TAPO_HOST=192.168.1.100 \
  -e TAPO_USER=camera-user \
  -e TAPO_PASS=camera-pass \
  tapoweb
```

### Camera networking

The container must be able to reach the camera's IP address on your LAN. Docker's default bridge network allows outbound connections, so port publishing alone is normally enough — no camera ports need to be exposed to the container.

If the camera is unreachable from the container (some NAS, VM, or VLAN setups), use host networking on Linux instead. In `compose.yaml`, replace the `ports:` mapping with:

```yaml
    network_mode: host
```

The UI is then available directly on port `8080` (change `TAPO_WEB_PORT` if that port is taken). Host networking is only supported natively on Linux.

> The UI has no authentication. Only publish it on networks you trust, and prefer `TAPO_WEB_BIND=127.0.0.1` when you don't need access from other devices.

## Manual setup

```bash
git clone https://github.com/DilshanHarshajith/TapoWeb TapoWeb
cd TapoWeb

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

> The ONVIF module comes from the `onvif-zeep` package. Do not install the unrelated `onvif` package — it fails on modern Python and does not provide `ONVIFCamera`.

## Run

```bash
./run.sh
```

or:

```bash
python3 app.py
```

Then open <http://127.0.0.1:8080> and enter your camera details in the Settings dialog.

### Options

```
python3 app.py --host 0.0.0.0 --port 8080
```

| Flag | Environment variable | Default |
| --- | --- | --- |
| `--host` | `TAPO_WEB_HOST` | `127.0.0.1` |
| `--port` | `TAPO_WEB_PORT` | `8080` |

### Auto-connect

Set these variables to connect at startup instead of using the UI:

| Variable | Description |
| --- | --- |
| `TAPO_HOST` | Camera IP address or hostname (required) |
| `TAPO_USER` | Camera account username |
| `TAPO_PASS` | Camera account password |
| `TAPO_PORT` | ONVIF port (default `2020`) |
| `TAPO_STREAM` | `stream1` (HD) or `stream2` (SD) |
| `TAPO_CLOUD_PASS` | Tapo account password for spotlight, night mode and SD card recordings |

`TAPO_USER` and `TAPO_PASS` are required for auto-connect; `TAPO_HOST` must also be set.

Example:

```bash
TAPO_HOST=192.168.1.100 \
TAPO_USER=camera-user \
TAPO_PASS=camera-pass \
TAPO_CLOUD_PASS=tapo-account-pass \
./run.sh
```

## Recordings

The **Recordings** tab browses the clips stored on the camera's SD card and plays them without downloading the whole file first.

1. Open **Settings** and enter your Tapo account password, or set `TAPO_CLOUD_PASS`. The camera-account password is not enough for recordings.
2. Switch to **Recordings** and pick a date. The panel shows SD card usage and every clip recorded that day.
3. Select a clip to play it, or use **Download clip** to render an MP4.

Playback is remuxed live from the camera's local playback stream into HLS, so it usually starts within a few seconds. A few things to know:

- The camera generally serves one media stream at a time. Starting playback pauses the live RTSP stream; switching back to **Live view** restarts it automatically.
- Playback continues in real time as the camera delivers the clip. Seeking is limited to the part that has already been received.
- Clips that the camera is still writing (roughly the last minute) cannot be played or downloaded yet.
- **Download clip** runs as a background MP4 job with progress in the panel. When it finishes, the browser download starts automatically. Downloads run one at a time and are cached on the server for up to 6 hours, or until you disconnect.
- The UI waits for an active download to finish before starting a new playback.

## Notes

- The camera account password and the Tapo account password are different. The spotlight, day/night and SD card recording controls require the Tapo account password (the one you sign in to the Tapo app with), not the camera account password.
- SD card playback and downloads need `ffmpeg` and `ffprobe` in `PATH`; the Docker image includes both. The camera's live RTSP stream is paused during playback and restarted when you return to Live view.
- If spotlight/night mode or recordings report a wrong password, double-check the Tapo account password (no leading/trailing spaces) and that the camera's local API is not temporarily locked after repeated failed attempts.
- Repeated wrong Tapo passwords can lock the camera's local API for about 30 minutes.
- The live view is also transcoded to HLS on the fly by ffmpeg. If `ffmpeg` is missing, the UI loads but video will not play. The Docker image includes it.
- Credentials live in server memory for the lifetime of the process.
- HLS segments are written to a temporary directory inside the container and cleaned up automatically.

## Project layout

```
app.py           Flask app and HTTP API
camera.py        ONVIF camera and PTZ controls
stream.py        ffmpeg HLS stream manager
recordings.py    SD card recording browser, download and HLS playback manager
tapo_api.py      Tapo local API for spotlight / night mode
static/          Frontend (HTML, CSS, JS, hls.js)
run.sh           Convenience launcher for manual installs
Dockerfile       Container image (Python + ffmpeg, non-root user)
compose.yaml     Docker Compose service definition
.env.example     Template for Docker/auto-connect settings
```

## License

MIT
