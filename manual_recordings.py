import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime


class ManualRecordingError(Exception):
    pass


DESTINATIONS = ("download", "server", "both")
MAX_NAME_LENGTH = 120
MAX_COMMENT_LENGTH = 500


def _clean_text(value, limit):
    text = str(value or "").strip()
    return text[:limit] or None


def _friendly_error(exc):
    text = str(exc or "").strip()
    lowered = text.lower()
    if "ffmpeg" in lowered or "ffprobe" in lowered:
        return "Manual recording needs ffmpeg and ffprobe in PATH."
    if "permission" in lowered:
        return "The recording folder is not writable."
    if "no space" in lowered:
        return "The recording folder is out of disk space."
    return text or "Manual recording failed."


def _remove_file(path):
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _remove_record_files(record):
    for key in ("path", "tsPath", "metaPath"):
        _remove_file(record.get(key))


class ManualRecordingManager:
    """Records the live RTSP stream to an MP4 file on the server.

    The camera's local API does not expose manual SD-card recording, so the
    clip is captured with ffmpeg on the host. One recording runs at a time to
    avoid overloading the camera, and it is independent from the HLS live view.
    """

    MAX_DOWNLOAD_ONLY_AGE = 6 * 60 * 60

    def __init__(self, directory=None, ffmpeg="ffmpeg", ffprobe="ffprobe"):
        self._directory = os.path.abspath(
            directory
            or os.environ.get("TAPO_RECORDINGS_DIR")
            or os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "manual-recordings"
            )
        )
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._lock = threading.RLock()
        self._records = {}
        self._active = None
        self._restore()

    @property
    def directory(self):
        return self._directory

    def _restore(self):
        """Rebuild the library from MP4 files left by a previous process.

        The server library is meant to survive restarts when the recordings
        folder is on a persistent volume, so metadata is kept in a small JSON
        sidecar next to each clip. Older files without a sidecar are treated as
        server-library clips rather than being deleted.
        """
        try:
            names = os.listdir(self._directory)
        except OSError:
            return
        for name in names:
            if not (name.startswith("manual-") and name.endswith(".mp4")):
                continue
            path = os.path.join(self._directory, name)
            if not os.path.isfile(path):
                continue
            meta_path = path[:-4] + ".json"
            meta = {}
            try:
                with open(meta_path, "r", encoding="utf-8") as file:
                    loaded = json.load(file)
                if isinstance(loaded, dict):
                    meta = loaded
            except (OSError, ValueError):
                meta = {}
            destination = str(meta.get("destination") or "server").lower()
            if destination not in DESTINATIONS:
                destination = "server"
            try:
                created = float(meta.get("createdAt") or os.path.getmtime(path))
            except (TypeError, ValueError, OSError):
                created = time.time()
            record_id = str(meta.get("id") or uuid.uuid4().hex)
            while record_id in self._records:
                record_id = uuid.uuid4().hex
            try:
                duration = max(0.0, float(meta.get("duration") or 0.0))
            except (TypeError, ValueError):
                duration = 0.0
            try:
                started = float(meta.get("startedAt") or created)
            except (TypeError, ValueError):
                started = created
            self._records[record_id] = {
                "id": record_id,
                "fileName": name,
                "path": path,
                "tsPath": path[:-4] + ".ts",
                "metaPath": meta_path,
                "name": _clean_text(meta.get("name"), MAX_NAME_LENGTH),
                "comment": _clean_text(meta.get("comment"), MAX_COMMENT_LENGTH),
                "createdAt": created,
                "startedAt": started,
                "endedAt": meta.get("endedAt"),
                "duration": duration,
                "size": os.path.getsize(path),
                "state": "ready",
                "destination": destination,
                "keep": destination in ("server", "both"),
                "error": None,
                "proc": None,
                "stopEvent": None,
                "thread": None,
            }

    def _ensure_directory(self):
        try:
            os.makedirs(self._directory, exist_ok=True)
        except OSError as exc:
            raise ManualRecordingError(
                f"Could not create the recording folder: {exc}"
            ) from exc

    def start(self, url, destination, name=None):
        destination = str(destination or "download").strip().lower()
        if destination not in DESTINATIONS:
            raise ManualRecordingError("Choose where to save the recording")
        if shutil.which(self._ffmpeg) is None:
            raise ManualRecordingError("Manual recording needs ffmpeg in PATH")
        self._ensure_directory()
        self._prune()
        with self._lock:
            if self._active is not None:
                state = self._active.get("state")
                if state in ("starting", "recording"):
                    raise ManualRecordingError("A recording is already in progress")
                raise ManualRecordingError(
                    "Wait for the previous recording to finish"
                )
            created = time.time()
            record_id = uuid.uuid4().hex
            stamp = datetime.fromtimestamp(created).strftime("%Y%m%d-%H%M%S")
            file_name = f"manual-{stamp}-{record_id[:6]}.mp4"
            record = {
                "id": record_id,
                "fileName": file_name,
                "path": os.path.join(self._directory, file_name),
                "tsPath": os.path.join(self._directory, file_name[:-4] + ".ts"),
                "metaPath": os.path.join(self._directory, file_name[:-4] + ".json"),
                "name": _clean_text(name, MAX_NAME_LENGTH),
                "comment": None,
                "createdAt": created,
                "startedAt": created,
                "endedAt": None,
                "duration": 0.0,
                "size": 0,
                "state": "starting",
                "destination": destination,
                "keep": destination in ("server", "both"),
                "error": None,
                "proc": None,
                "stopEvent": threading.Event(),
                "thread": None,
            }
            thread = threading.Thread(
                target=self._run,
                args=(record, url),
                daemon=True,
                name=f"manual-recording-{record_id[:8]}",
            )
            record["thread"] = thread
            self._records[record_id] = record
            self._active = record
            thread.start()
        return self._public(record)

    def snapshot(self):
        self._prune()
        with self._lock:
            active = self._active
            active_public = self._public(active) if active is not None else None
            recordings = sorted(
                (
                    self._public(record)
                    for record in self._records.values()
                    if record["state"] == "ready"
                ),
                key=lambda item: item["createdAt"],
                reverse=True,
            )
            failures = sorted(
                (
                    self._public(record)
                    for record in self._records.values()
                    if record["state"] == "error"
                ),
                key=lambda item: item["createdAt"],
                reverse=True,
            )[:5]
        return {
            "active": active_public,
            "recordings": recordings,
            "failures": failures,
        }

    def active(self):
        with self._lock:
            record = self._active
            return self._public(record) if record is not None else None

    def get(self, record_id):
        with self._lock:
            record = self._records.get(record_id)
            return self._public(record) if record is not None else None

    def update(self, record_id, name=None, comment=None):
        """Store a user-provided name and comment on a recording.

        ``name`` and ``comment`` are only changed when the caller passes them
        (``None`` leaves the existing value alone). Passing an empty string
        clears the field. In-progress recordings keep the values in memory and
        the metadata sidecar written when they finish persists them.
        """
        with self._lock:
            record = self._records.get(record_id)
            if record is None:
                return None
            if name is not None:
                record["name"] = _clean_text(name, MAX_NAME_LENGTH)
            if comment is not None:
                record["comment"] = _clean_text(comment, MAX_COMMENT_LENGTH)
            ready = record["state"] == "ready"
        if ready:
            self._write_meta(record)
        return self._public(record)

    def path(self, record_id):
        with self._lock:
            record = self._records.get(record_id)
            if record is None or record["state"] != "ready":
                return None
            path = record.get("path")
        return path if path and os.path.isfile(path) else None

    def stop(self):
        with self._lock:
            record = self._active
            if record is None:
                return None
            if record["state"] in ("starting", "recording"):
                record["state"] = "stopping"
                record["stopEvent"].set()
            return self._public(record)

    def delete(self, record_id):
        with self._lock:
            record = self._records.get(record_id)
            if record is None:
                return False
            if record is self._active:
                raise ManualRecordingError(
                    "Stop the recording before deleting it"
                )
            self._records.pop(record_id, None)
        _remove_record_files(record)
        return True

    def close(self):
        with self._lock:
            record = self._active
            if record is not None and record["state"] in ("starting", "recording"):
                record["state"] = "stopping"
                record["stopEvent"].set()
            thread = record.get("thread") if record is not None else None
        if thread is not None and thread.is_alive():
            thread.join(timeout=15)

    def _prune(self):
        cutoff = time.time() - self.MAX_DOWNLOAD_ONLY_AGE
        removed = []
        with self._lock:
            for record_id, record in list(self._records.items()):
                if record is self._active:
                    continue
                if record["state"] == "ready":
                    if not os.path.isfile(record.get("path") or ""):
                        removed.append(self._records.pop(record_id))
                        continue
                    if record["keep"]:
                        continue
                if record["createdAt"] >= cutoff:
                    continue
                removed.append(self._records.pop(record_id))
        for record in removed:
            _remove_record_files(record)

    def _public(self, record):
        state = record["state"]
        duration = record["duration"]
        if state in ("starting", "recording", "stopping"):
            duration = max(0.0, time.time() - record["startedAt"])
        elif state == "finalizing" and record["endedAt"]:
            duration = max(0.0, record["endedAt"] - record["startedAt"])
        return {
            "id": record["id"],
            "fileName": record["fileName"],
            "name": record.get("name"),
            "comment": record.get("comment"),
            "state": state,
            "createdAt": record["createdAt"],
            "startedAt": record["startedAt"],
            "endedAt": record["endedAt"],
            "duration": round(float(duration or 0), 1),
            "size": int(record["size"] or 0),
            "destination": record["destination"],
            "keep": record["keep"],
            "error": record["error"],
        }

    def _run(self, record, url):
        error = None
        proc = None
        try:
            command = [
                self._ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-rtsp_transport",
                "tcp",
                "-i",
                url,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c",
                "copy",
                "-f",
                "mpegts",
                "-y",
                record["tsPath"],
            ]
            proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with self._lock:
                record["proc"] = proc
                if not record["stopEvent"].is_set():
                    record["state"] = "recording"

            while True:
                if record["stopEvent"].wait(0.5):
                    break
                if proc.poll() is not None:
                    break

            with self._lock:
                record["endedAt"] = time.time()
                if record["stopEvent"].is_set():
                    record["state"] = "stopping"

            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass

            with self._lock:
                record["proc"] = None
                record["state"] = "finalizing"
                record["endedAt"] = record["endedAt"] or time.time()
                record["duration"] = max(
                    0.0, record["endedAt"] - record["startedAt"]
                )

            if (
                not os.path.isfile(record["tsPath"])
                or os.path.getsize(record["tsPath"]) < 1024
            ):
                raise ManualRecordingError(
                    "The camera did not send any video. Try again while the live view is running."
                )
            self._finalize(record)
        except Exception as exc:
            error = exc
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
            with self._lock:
                record["proc"] = None
                if error is not None:
                    record["state"] = "error"
                    record["error"] = _friendly_error(error)
                    record["keep"] = False
                    _remove_record_files(record)
                if self._active is record:
                    self._active = None

    def _finalize(self, record):
        path = record["path"]
        ts_path = record["tsPath"]
        if not self._remux(ts_path, path):
            raise ManualRecordingError("Could not convert the recording to MP4")
        duration = self._probe_duration(path) or record["duration"]
        size = os.path.getsize(path)
        with self._lock:
            record["duration"] = duration
            record["size"] = size
            record["state"] = "ready"
            record["endedAt"] = record["endedAt"] or time.time()
        self._write_meta(record)
        _remove_file(ts_path)

    def _write_meta(self, record):
        meta = {
            key: record.get(key)
            for key in (
                "id",
                "name",
                "comment",
                "createdAt",
                "startedAt",
                "endedAt",
                "duration",
                "size",
                "destination",
                "keep",
            )
        }
        path = record.get("metaPath")
        if not path:
            return
        temporary = path + ".tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as file:
                json.dump(meta, file)
            os.replace(temporary, path)
        except OSError:
            _remove_file(temporary)

    def _remux(self, source, destination):
        base = [
            self._ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            source,
            "-movflags",
            "+faststart",
        ]
        attempts = [
            [*base, "-c", "copy", destination],
            [*base, "-c:v", "copy", "-c:a", "aac", "-b:a", "96k", destination],
        ]
        for command in attempts:
            try:
                result = subprocess.run(
                    command, capture_output=True, timeout=300, check=False
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if (
                result.returncode == 0
                and os.path.isfile(destination)
                and os.path.getsize(destination) > 0
            ):
                return True
            _remove_file(destination)
        return False

    def _probe_duration(self, path):
        if shutil.which(self._ffprobe) is None:
            return None
        try:
            result = subprocess.run(
                [
                    self._ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=nw=1:nk=1",
                    path,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        try:
            value = float(result.stdout.strip())
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None
