#!/usr/bin/env python3
import argparse
import atexit
import os
import re
import shutil
import subprocess
import threading
import time
from functools import wraps
from urllib.parse import quote

from flask import Flask, Response, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import NotFound

from camera import CameraController, CameraError
from manual_recordings import ManualRecordingError, ManualRecordingManager
from recordings import (
    RecordingDownloadManager,
    RecordingError,
    RecordingService,
    RecordingStreamManager,
)
from stream import StreamManager
from talk import TalkError, TapoTalk
from tapo_api import TapoError, TapoLight

DEFAULT_HOST = ""
DEFAULT_ONVIF_PORT = 2020
DEFAULT_RTSP_PORT = 554
AUTO_CONNECT_RETRY_MIN = 2.0
AUTO_CONNECT_RETRY_MAX = 60.0

app = Flask(__name__, static_folder="static", static_url_path="/static")
state_lock = threading.RLock()
auto_connect_lock = threading.Lock()
auto_connect_stop = threading.Event()
auto_connect_state = {"pending": False, "error": None}
_UNSET = object()
controller = None
credentials = None
light_controller = None
recording_service = None
recording_downloads = None
recording_streams = None
talk_session = None
stream_manager = StreamManager()
manual_recordings = ManualRecordingManager()


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


def get_recording_service():
    with state_lock:
        service = recording_service
        creds = credentials
    if service is None:
        if creds is None:
            raise RecordingError("Not connected to the camera")
        raise RecordingError(
            "Add your Tapo account password in Settings to browse SD card recordings"
        )
    return service


def get_recording_downloads():
    global recording_downloads
    with state_lock:
        service = recording_service
        creds = credentials
        manager = recording_downloads
        if manager is not None:
            return manager
        if service is None:
            if creds is None:
                raise RecordingError("Not connected to the camera")
            raise RecordingError(
                "Add your Tapo account password in Settings to retrieve SD card recordings"
            )
        manager = RecordingDownloadManager(service)
        recording_downloads = manager
        return manager


def get_recording_streams():
    global recording_streams
    with state_lock:
        service = recording_service
        creds = credentials
        manager = recording_streams
        if manager is not None:
            return manager
        if service is None:
            if creds is None:
                raise RecordingError("Not connected to the camera")
            raise RecordingError(
                "Add your Tapo account password in Settings to play SD card recordings"
            )
        manager = RecordingStreamManager(service)
        recording_streams = manager
        return manager


def close_recordings():
    global recording_service, recording_downloads, recording_streams
    with state_lock:
        stream_manager_ = recording_streams
        manager = recording_downloads
        service = recording_service
        recording_streams = None
        recording_downloads = None
        recording_service = None
    if stream_manager_ is not None:
        stream_manager_.close()
    if manager is not None:
        manager.stop()
    if service is not None:
        service.close()


def get_talk_credentials():
    with state_lock:
        creds = dict(credentials) if credentials else None
    if creds is None:
        raise TalkError("Not connected to the camera")
    if not creds.get("cloud_password"):
        raise TalkError(
            "Add your Tapo account password in Settings to call the camera"
        )
    return creds


def close_talk():
    global talk_session
    with state_lock:
        session = talk_session
        talk_session = None
    if session is not None:
        session.stop()


def current_status():
    with state_lock:
        cam = controller
        creds = credentials
        has_cloud_password = bool(creds and creds.get("cloud_password"))
        talk = talk_session
    with auto_connect_lock:
        auto_pending = auto_connect_state["pending"]
        auto_error = auto_connect_state["error"]
    talk_active = talk is not None and talk.active
    manual_recording = manual_recordings.active()
    if cam is None:
        return {
            "authenticated": False,
            "stream": stream_manager.stream,
            "stream_running": stream_manager.is_running(),
            "has_cloud_password": has_cloud_password,
            "talk_active": talk_active,
            "auto_connect_pending": auto_pending,
            "auto_connect_error": auto_error,
            "manual_recording": manual_recording,
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
        "talk_active": talk_active,
        "auto_connect_pending": auto_pending,
        "auto_connect_error": auto_error,
        "manual_recording": manual_recording,
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
    global controller, credentials, light_controller, recording_service
    # A manual login supersedes the environment auto-connect, which may still
    # be waiting for the camera to appear on the network.
    auto_connect_stop.set()
    _update_auto_connect_state(pending=False, error=None)
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
    new_recordings = RecordingService(host, cloud_password) if cloud_password else None
    url = rtsp_url(host, DEFAULT_RTSP_PORT, user, password, stream)
    close_talk()
    close_recordings()
    manual_recordings.stop()
    with state_lock:
        old_controller = controller
        controller = new_controller
        light_controller = new_light
        recording_service = new_recordings
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
    close_talk()
    close_recordings()
    manual_recordings.stop()
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
    manual_recordings.stop()
    stream_manager.start(url, stream)
    with state_lock:
        credentials["stream"] = stream
    return jsonify(current_status())


@app.post("/api/stream/ensure")
def ensure_stream():
    with state_lock:
        creds = dict(credentials) if credentials else None
    if creds is None:
        return jsonify(error="Not connected to the camera"), 401
    if not stream_manager.is_running():
        url = rtsp_url(
            creds["host"],
            DEFAULT_RTSP_PORT,
            creds["user"],
            creds["password"],
            creds["stream"],
        )
        stream_manager.start(url, creds["stream"])
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


@app.get("/api/recordings/dates")
def recording_dates():
    try:
        service = get_recording_service()
        return jsonify({"storage": service.storage(), "dates": service.dates()})
    except RecordingError as exc:
        return jsonify(error=str(exc)), 400


@app.get("/api/recordings")
def recordings_for_date():
    date = str(request.args.get("date") or "").strip()
    if not date:
        return jsonify(error="Recording date is required"), 400
    try:
        service = get_recording_service()
        return jsonify({"date": date, "recordings": service.list_for_date(date)})
    except RecordingError as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/recordings/download")
def recording_download_create():
    data = request.get_json(silent=True) or {}
    try:
        manager = get_recording_downloads()
        job = manager.create(data.get("start"), data.get("end"))
    except RecordingError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(job), 202


@app.get("/api/recordings/download/<job_id>")
def recording_download_status(job_id):
    with state_lock:
        manager = recording_downloads
    job = manager.get(job_id) if manager is not None else None
    if job is None:
        return jsonify(error="Recording download not found"), 404
    return jsonify(job)


@app.get("/api/recordings/file/<job_id>")
def recording_file(job_id):
    with state_lock:
        manager = recording_downloads
    if manager is None:
        return jsonify(error="Recording download not found"), 404
    job = manager.get(job_id)
    path = manager.path(job_id)
    if job is None or path is None:
        return jsonify(error="Recording is not ready"), 404
    as_attachment = request.args.get("download") in ("1", "true", "yes")
    filename = f"recording-{job['startTime']}-{job['endTime']}.mp4"
    return send_file(
        path,
        mimetype="video/mp4",
        conditional=True,
        as_attachment=as_attachment,
        download_name=filename,
        max_age=0,
    )


@app.post("/api/recordings/stream")
def recording_stream_start():
    data = request.get_json(silent=True) or {}
    try:
        manager = get_recording_streams()
        # The camera generally serves one media session at a time. Stop the
        # RTSP live stream before opening the SD card playback session.
        close_talk()
        stream_manager.stop()
        time.sleep(0.4)
        stream = manager.start(data.get("start"), data.get("end"))
    except RecordingError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(stream), 202


@app.get("/api/recordings/stream/<stream_id>")
def recording_stream_status(stream_id):
    with state_lock:
        manager = recording_streams
    stream = manager.get(stream_id) if manager is not None else None
    if stream is None:
        return jsonify(error="Recording stream not found"), 404
    return jsonify(stream)


@app.delete("/api/recordings/stream/<stream_id>")
def recording_stream_stop(stream_id):
    with state_lock:
        manager = recording_streams
    if manager is None or not manager.stop(stream_id):
        return jsonify(error="Recording stream not found"), 404
    return jsonify(ok=True)


@app.get("/api/recordings/stream/<stream_id>/index.m3u8")
def recording_stream_playlist(stream_id):
    with state_lock:
        manager = recording_streams
    path = manager.playlist(stream_id) if manager is not None else None
    if path is None:
        return jsonify(error="Recording stream is not ready"), 404
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as file:
            content = file.read()
    except OSError:
        return jsonify(error="Recording stream is not ready"), 404
    if "#EXTINF" not in content:
        return jsonify(error="Recording stream is not ready"), 404
    response = Response(content, mimetype="application/vnd.apple.mpegurl")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/api/recordings/stream/<stream_id>/<path:filename>")
def recording_stream_segment(stream_id, filename):
    with state_lock:
        manager = recording_streams
    directory = manager.directory(stream_id) if manager is not None else None
    if directory is None:
        return jsonify(error="Recording stream not found"), 404
    name = os.path.basename(filename)
    if not re.fullmatch(r"seg\d+\.ts", name):
        return jsonify(error="Not found"), 404
    try:
        response = send_from_directory(
            directory, name, mimetype="video/mp2t", conditional=True
        )
    except NotFound:
        return jsonify(error="Not found"), 404
    response.headers["Cache-Control"] = "public, max-age=30"
    return response


@app.get("/api/manual-recordings")
def manual_recordings_list():
    return jsonify(manual_recordings.snapshot())


@app.post("/api/manual-recordings")
def manual_recordings_start():
    data = request.get_json(silent=True) or {}
    with state_lock:
        creds = dict(credentials) if credentials else None
    if creds is None:
        return jsonify(error="Not connected to the camera"), 401
    url = rtsp_url(
        creds["host"],
        DEFAULT_RTSP_PORT,
        creds["user"],
        creds["password"],
        creds["stream"],
    )
    try:
        record = manual_recordings.start(url, data.get("destination"))
    except ManualRecordingError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(record), 202


@app.delete("/api/manual-recordings")
def manual_recordings_stop():
    record = manual_recordings.stop()
    if record is None:
        return jsonify(error="No recording is in progress"), 404
    return jsonify(record), 202


@app.get("/api/manual-recordings/<record_id>")
def manual_recording_status(record_id):
    record = manual_recordings.get(record_id)
    if record is None:
        return jsonify(error="Recording not found"), 404
    return jsonify(record)


@app.get("/api/manual-recordings/<record_id>/file")
def manual_recording_file(record_id):
    record = manual_recordings.get(record_id)
    path = manual_recordings.path(record_id)
    if record is None or path is None:
        return jsonify(error="Recording is not ready"), 404
    as_attachment = request.args.get("download") in ("1", "true", "yes")
    return send_file(
        path,
        mimetype="video/mp4",
        conditional=True,
        as_attachment=as_attachment,
        download_name=record["fileName"],
        max_age=0,
    )


@app.delete("/api/manual-recordings/<record_id>")
def manual_recording_delete(record_id):
    try:
        deleted = manual_recordings.delete(record_id)
    except ManualRecordingError as exc:
        return jsonify(error=str(exc)), 409
    if not deleted:
        return jsonify(error="Recording not found"), 404
    return jsonify(ok=True)


@app.post("/api/talk/start")
def talk_start():
    global talk_session
    try:
        creds = get_talk_credentials()
    except TalkError as exc:
        return jsonify(error=str(exc)), 400
    close_talk()
    session = TapoTalk(creds["host"], creds["cloud_password"])
    try:
        session.start()
    except TalkError as exc:
        return jsonify(error=str(exc)), 400
    with state_lock:
        talk_session = session
    return jsonify(active=True)


@app.post("/api/talk/audio")
def talk_audio():
    data = request.get_data(cache=False)
    if not data:
        return jsonify(ok=True)
    if len(data) > 256 * 1024:
        return jsonify(error="Audio chunk is too large"), 413
    with state_lock:
        session = talk_session
    if session is None:
        return jsonify(error="The camera call is not active"), 409
    try:
        session.send_audio(data)
    except TalkError as exc:
        close_talk()
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True)


@app.post("/api/talk/stop")
def talk_stop():
    close_talk()
    return jsonify(active=False)


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


def _update_auto_connect_state(pending=_UNSET, error=_UNSET):
    with auto_connect_lock:
        if pending is not _UNSET:
            auto_connect_state["pending"] = pending
        if error is not _UNSET:
            auto_connect_state["error"] = error


def _env_connection():
    user = (os.environ.get("TAPO_USER") or "").strip()
    password = os.environ.get("TAPO_PASS") or ""
    if not user or not password:
        return None
    host = (os.environ.get("TAPO_HOST") or "").strip()
    if not host:
        print("Auto-connect skipped: set TAPO_HOST to the camera address")
        return None
    try:
        onvif_port = int(os.environ.get("TAPO_PORT", DEFAULT_ONVIF_PORT))
    except (TypeError, ValueError):
        print("Auto-connect skipped: TAPO_PORT must be a number")
        return None
    stream = os.environ.get("TAPO_STREAM", "stream1")
    if stream not in ("stream1", "stream2"):
        stream = "stream1"
    return {
        "host": host,
        "port": onvif_port,
        "user": user,
        "password": password,
        "cloud_password": (os.environ.get("TAPO_CLOUD_PASS", "") or "").strip(),
        "stream": stream,
    }


def start_auto_connect():
    """Connect using the TAPO_* environment, retrying in the background.

    A container started at boot can easily win the race against the network or
    the camera coming online. A single failed attempt used to leave the app with
    no credentials even though they were supplied through the environment, so
    the UI asked for them again after every reboot.
    """
    creds = _env_connection()
    if creds is None:
        return
    _update_auto_connect_state(pending=True, error=None)
    threading.Thread(
        target=_auto_connect_worker,
        args=(creds,),
        name="auto-connect",
        daemon=True,
    ).start()


def _auto_connect_worker(creds):
    global controller, credentials, light_controller, recording_service
    delay = AUTO_CONNECT_RETRY_MIN
    attempt = 0
    while not auto_connect_stop.is_set():
        attempt += 1
        try:
            cam = CameraController(
                creds["host"], creds["port"], creds["user"], creds["password"]
            )
        except CameraError as exc:
            message = str(exc)
            with state_lock:
                manual_session = credentials is not None
            if manual_session or auto_connect_stop.is_set():
                _update_auto_connect_state(pending=False, error=None)
                return
            if message == "Wrong camera username or password":
                # Retrying cannot help and could lock the camera account.
                _update_auto_connect_state(pending=False, error=message)
                print(
                    f"Auto-connect failed: {message}. "
                    "Check TAPO_USER and TAPO_PASS."
                )
                return
            _update_auto_connect_state(error=message)
            print(
                f"Auto-connect attempt {attempt} failed: {message}; "
                f"retrying in {delay:g}s"
            )
            if auto_connect_stop.wait(delay):
                return
            delay = min(AUTO_CONNECT_RETRY_MAX, delay * 2)
            continue

        has_cloud_password = bool(creds["cloud_password"])
        light = (
            TapoLight(creds["host"], creds["cloud_password"])
            if has_cloud_password
            else None
        )
        recordings = (
            RecordingService(creds["host"], creds["cloud_password"])
            if has_cloud_password
            else None
        )
        with state_lock:
            if credentials is not None or auto_connect_stop.is_set():
                # A session was opened from the UI while we were connecting.
                _update_auto_connect_state(pending=False, error=None)
                return
            controller = cam
            light_controller = light
            recording_service = recordings
            credentials = dict(creds)
        stream_manager.start(
            rtsp_url(
                creds["host"],
                DEFAULT_RTSP_PORT,
                creds["user"],
                creds["password"],
                creds["stream"],
            ),
            creds["stream"],
        )
        device = cam.device()
        manufacturer = str(device.get("manufacturer", "") or "")
        model = str(device.get("model", "") or "")
        _update_auto_connect_state(pending=False, error=None)
        print(f"Connected to {manufacturer} {model}".strip())
        return


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

    start_auto_connect()
    atexit.register(auto_connect_stop.set)
    atexit.register(stream_manager.stop)
    atexit.register(close_talk)
    atexit.register(close_recordings)
    atexit.register(manual_recordings.close)
    print(f"Tapo web UI: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
