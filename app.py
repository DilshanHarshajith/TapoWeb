#!/usr/bin/env python3
import argparse
import atexit
import os
import shutil
import subprocess
import threading
from functools import wraps
from urllib.parse import quote

from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.exceptions import NotFound

from camera import CameraController, CameraError
from stream import StreamManager
from tapo_api import TapoError, TapoLight

DEFAULT_HOST = ""
DEFAULT_ONVIF_PORT = 2020
DEFAULT_RTSP_PORT = 554

app = Flask(__name__, static_folder="static", static_url_path="/static")
state_lock = threading.RLock()
controller = None
credentials = None
light_controller = None
stream_manager = StreamManager()


def rtsp_url(host, port, user, password, stream):
    return (
        f"rtsp://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{stream}"
    )


def get_controller():
    with state_lock:
        return controller


def get_light():
    with state_lock:
        light = light_controller
        creds = credentials
    if light is None:
        if creds is None:
            raise TapoError("Not connected to the camera")
        raise TapoError(
            "Add your Tapo account password in Settings to use the spotlight and night mode"
        )
    return light


def current_status():
    with state_lock:
        cam = controller
        creds = credentials
        has_cloud_password = bool(creds and creds.get("cloud_password"))
    if cam is None:
        return {
            "authenticated": False,
            "stream": stream_manager.stream,
            "stream_running": stream_manager.is_running(),
            "has_cloud_password": has_cloud_password,
        }
    return {
        "authenticated": True,
        "host": cam.host,
        "port": cam.port,
        "username": cam.user,
        "device": cam.device(),
        "stream": stream_manager.stream,
        "stream_running": stream_manager.is_running(),
        "max_hold": CameraController.MAX_HOLD_SECONDS,
        "has_cloud_password": has_cloud_password,
    }


def camera_route(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        cam = get_controller()
        if cam is None:
            return jsonify(error="Not connected to the camera"), 401
        try:
            return fn(cam, *args, **kwargs)
        except CameraError as exc:
            return jsonify(error=str(exc)), 400

    return wrapper


def clamp(value, low, high, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/status")
def status():
    return jsonify(current_status())


@app.post("/api/session")
def create_session():
    global controller, credentials, light_controller
    data = request.get_json(silent=True) or {}
    host = str(data.get("host") or DEFAULT_HOST).strip()
    if not host:
        return jsonify(error="Host is required"), 400
    user = str(data.get("user") or "").strip()
    password = str(data.get("password") or "")
    cloud_password = str(data.get("cloud_password") or "").strip()
    stream = data.get("stream") or "stream1"
    if stream not in ("stream1", "stream2"):
        stream = "stream1"
    try:
        onvif_port = int(data.get("port") or DEFAULT_ONVIF_PORT)
    except (TypeError, ValueError):
        return jsonify(error="Invalid ONVIF port"), 400
    if not user:
        return jsonify(error="Username is required"), 400

    with state_lock:
        existing = credentials
    if (
        not password
        and existing
        and existing["host"] == host
        and existing["port"] == onvif_port
        and existing["user"] == user
    ):
        password = existing["password"]
    if (
        not cloud_password
        and existing
        and existing["host"] == host
        and existing["user"] == user
    ):
        cloud_password = existing.get("cloud_password") or ""
    if not password:
        return jsonify(error="Password is required"), 400

    try:
        new_controller = CameraController(host, onvif_port, user, password)
    except CameraError as exc:
        return jsonify(error=str(exc)), 400

    new_light = TapoLight(host, cloud_password) if cloud_password else None
    url = rtsp_url(host, DEFAULT_RTSP_PORT, user, password, stream)
    with state_lock:
        old_controller = controller
        controller = new_controller
        light_controller = new_light
        credentials = {
            "host": host,
            "port": onvif_port,
            "user": user,
            "password": password,
            "cloud_password": cloud_password,
            "stream": stream,
        }
    if old_controller is not None:
        old_controller.stop()
    stream_manager.start(url, stream)
    return jsonify(current_status())


@app.delete("/api/session")
def delete_session():
    global controller, credentials, light_controller
    with state_lock:
        old_controller = controller
        controller = None
        credentials = None
        light_controller = None
    if old_controller is not None:
        old_controller.stop()
    stream_manager.stop()
    return jsonify(current_status())


@app.post("/api/stream")
def switch_stream():
    data = request.get_json(silent=True) or {}
    stream = data.get("stream") or "stream1"
    if stream not in ("stream1", "stream2"):
        return jsonify(error="Unknown stream"), 400
    with state_lock:
        creds = dict(credentials) if credentials else None
    if creds is None:
        return jsonify(error="Not connected to the camera"), 401
    url = rtsp_url(creds["host"], DEFAULT_RTSP_PORT, creds["user"], creds["password"], stream)
    stream_manager.start(url, stream)
    with state_lock:
        credentials["stream"] = stream
    return jsonify(current_status())


@app.get("/api/presets")
@camera_route
def list_presets(cam):
    return jsonify(cam.presets())


@app.post("/api/ptz/move")
@camera_route
def ptz_move(cam):
    data = request.get_json(silent=True) or {}
    pan = clamp(data.get("pan", 0), -1, 1, 0)
    tilt = clamp(data.get("tilt", 0), -1, 1, 0)
    zoom = clamp(data.get("zoom", 0), -1, 1, 0)
    speed = clamp(data.get("speed", 0.6), 0.05, 1, 0.6)
    if pan == 0 and tilt == 0 and zoom == 0:
        cam.stop()
        return jsonify(ok=True)
    cam.move(pan, tilt, zoom, speed)
    return jsonify(ok=True)


@app.post("/api/ptz/stop")
@camera_route
def ptz_stop(cam):
    cam.stop()
    return jsonify(ok=True)


@app.get("/api/ptz/position")
@camera_route
def ptz_position(cam):
    return jsonify(cam.position())


@app.post("/api/ptz/home")
@camera_route
def ptz_home(cam):
    data = request.get_json(silent=True) or {}
    speed = clamp(data.get("speed", 0.6), 0.05, 1, 0.6)
    cam.home(speed)
    return jsonify(ok=True)


@app.post("/api/ptz/absolute")
@camera_route
def ptz_absolute(cam):
    data = request.get_json(silent=True) or {}
    pan = clamp(data.get("pan", 0), -1, 1, 0)
    tilt = clamp(data.get("tilt", 0), -1, 1, 0)
    zoom = data.get("zoom")
    zoom = None if zoom is None else clamp(zoom, -1, 1, 0)
    speed = clamp(data.get("speed", 0.6), 0.05, 1, 0.6)
    cam.absolute(pan, tilt, zoom, speed)
    return jsonify(ok=True)


@app.post("/api/ptz/preset/goto")
@camera_route
def preset_goto(cam):
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    if token is None or str(token) == "":
        return jsonify(error="Preset token is required"), 400
    speed = clamp(data.get("speed", 0.6), 0.05, 1, 0.6)
    cam.goto_preset(token, speed)
    return jsonify(ok=True)


@app.post("/api/ptz/preset/set")
@camera_route
def preset_set(cam):
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    token = cam.set_preset(name)
    return jsonify(ok=True, token=token)


@app.post("/api/ptz/preset/remove")
@camera_route
def preset_remove(cam):
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    if token is None or str(token) == "":
        return jsonify(error="Preset token is required"), 400
    cam.remove_preset(token)
    return jsonify(ok=True)


@app.get("/api/light")
def light_state():
    try:
        return jsonify(get_light().state())
    except TapoError as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/light")
def light_set():
    data = request.get_json(silent=True) or {}
    on = data.get("on")
    if not isinstance(on, bool):
        return jsonify(error="'on' must be true or false"), 400
    try:
        return jsonify(get_light().set_spotlight(on))
    except TapoError as exc:
        return jsonify(error=str(exc)), 400


@app.get("/api/night")
def night_state():
    try:
        return jsonify(get_light().state())
    except TapoError as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/night")
def night_set():
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode") or "")
    if mode not in ("auto", "on", "off"):
        return jsonify(error="Night mode must be auto, day or night"), 400
    try:
        return jsonify(get_light().set_night_mode(mode))
    except TapoError as exc:
        return jsonify(error=str(exc)), 400


@app.get("/api/snapshot")
def snapshot():
    with state_lock:
        creds = dict(credentials) if credentials else None
    if creds is None:
        return jsonify(error="Not connected to the camera"), 401
    url = rtsp_url(
        creds["host"], DEFAULT_RTSP_PORT, creds["user"], creds["password"], creds["stream"]
    )
    segment = stream_manager.latest_segment()
    if segment:
        source = ["-i", segment]
    else:
        source = ["-rtsp_transport", "tcp", "-i", url]
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        *source,
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-",
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=20)
    except subprocess.TimeoutExpired:
        return jsonify(error="Snapshot timed out"), 504
    if not result.stdout:
        return jsonify(error="Could not grab a snapshot"), 502
    return Response(
        result.stdout,
        mimetype="image/jpeg",
        headers={"Content-Disposition": 'inline; filename="snapshot.jpg"'},
    )


@app.get("/hls/<path:filename>")
def hls(filename):
    directory = stream_manager.directory
    if not directory or not os.path.isdir(directory):
        return jsonify(error="Stream is not running"), 503
    mimetype = None
    if filename.endswith(".m3u8"):
        mimetype = "application/vnd.apple.mpegurl"
    elif filename.endswith(".ts"):
        mimetype = "video/mp2t"
    try:
        response = send_from_directory(directory, filename, mimetype=mimetype)
    except NotFound:
        return jsonify(error="Not found"), 404
    if filename.endswith(".m3u8"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    else:
        response.headers["Cache-Control"] = "public, max-age=30"
    return response


def auto_connect_from_env():
    global controller, credentials, light_controller
    user = os.environ.get("TAPO_USER")
    password = os.environ.get("TAPO_PASS")
    if not user or not password:
        return
    host = os.environ.get("TAPO_HOST", DEFAULT_HOST)
    if not host:
        print("Auto-connect skipped: set TAPO_HOST to the camera address")
        return
    onvif_port = int(os.environ.get("TAPO_PORT", DEFAULT_ONVIF_PORT))
    stream = os.environ.get("TAPO_STREAM", "stream1")
    cloud_password = (os.environ.get("TAPO_CLOUD_PASS", "") or "").strip()
    if stream not in ("stream1", "stream2"):
        stream = "stream1"
    try:
        cam = CameraController(host, onvif_port, user, password)
    except CameraError as exc:
        print(f"Auto-connect failed: {exc}")
        return
    controller = cam
    light_controller = TapoLight(host, cloud_password) if cloud_password else None
    credentials = {
        "host": host,
        "port": onvif_port,
        "user": user,
        "password": password,
        "cloud_password": cloud_password,
        "stream": stream,
    }
    stream_manager.start(
        rtsp_url(host, DEFAULT_RTSP_PORT, user, password, stream), stream
    )
    device = cam.device()
    print(f"Connected to {device.get('manufacturer', '')} {device.get('model', '')}".strip())


def main():
    parser = argparse.ArgumentParser(description="Tapo C520WS PTZ web app")
    parser.add_argument(
        "--host", default=os.environ.get("TAPO_WEB_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("TAPO_WEB_PORT", "8080"))
    )
    args = parser.parse_args()

    if shutil.which("ffmpeg") is None:
        print("ffmpeg was not found in PATH; the live view will not work")

    auto_connect_from_env()
    atexit.register(stream_manager.stop)
    print(f"Tapo web UI: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
