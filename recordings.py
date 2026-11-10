import asyncio
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

try:
    from pytapo import Tapo
except ImportError:
    Tapo = None

from tapo_api import friendly_tapo_error, _suspension_seconds


class RecordingError(Exception):
    pass


def friendly_recording_error(exc):
    text = str(exc)
    lowered = text.lower()
    if "aiofiles" in lowered:
        return "Recording support is missing aiofiles. Reinstall the Python requirements."
    if "ffmpeg" in lowered or "ffprobe" in lowered:
        return "Recording downloads need ffmpeg and ffprobe in PATH."
    if "no sd" in lowered or "sdcard" in lowered or "sd card" in lowered:
        return "The camera did not find an SD card."
    return friendly_tapo_error(exc)


def normalize_date(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 8:
        raise RecordingError("Recording date must be in YYYYMMDD format")
    return digits[:8]


def _flatten_results(items):
    if not items:
        return []
    if isinstance(items, dict):
        items = [items]
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if any(key in item for key in ("startTime", "start_time", "endTime", "end_time", "date")):
            result.append(item)
            continue
        for value in item.values():
            if isinstance(value, dict):
                result.append(value)
    return result


def _as_timestamp(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_recordings(raw):
    recordings = []
    for item in _flatten_results(raw):
        start = _as_timestamp(item.get("startTime", item.get("start_time")))
        end = _as_timestamp(item.get("endTime", item.get("end_time")))
        if start is None or end is None or end <= start:
            continue
        recording = {
            "startTime": start,
            "endTime": end,
            "duration": end - start,
        }
        video_type = item.get(
            "video_type",
            item.get("videoType", item.get("vedio_type")),
        )
        if video_type is not None:
            recording["type"] = str(video_type)
        recordings.append(recording)
    # Newest first is the default everywhere recordings are listed.
    recordings.sort(key=lambda entry: entry["startTime"], reverse=True)
    return recordings


def _normalize_storage(raw):
    if not raw:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    cards = []
    for card in raw:
        if not isinstance(card, dict):
            continue
        info = card.get("hd_info_1")
        if not isinstance(info, dict):
            info = card
        name = info.get("disk_name") or info.get("name") or "SD card"
        space = info.get("space") or info.get("capacity") or ""
        cards.append({"name": str(name), "space": str(space), "info": info})
    return cards


def _clean_text(value, limit):
    text = str(value or "").strip()
    return text[:limit] or None


class RecordingNotes:
    """Persistent names and comments for SD card clips.

    SD card clips live on the camera and cannot be edited there, so the notes
    are kept in a small JSON file next to the server recording library. The
    file is tied to a clip's start and end timestamp.
    """

    MAX_NAME_LENGTH = 120
    MAX_COMMENT_LENGTH = 500
    MAX_ENTRIES = 5000

    def __init__(self, directory):
        self.directory = os.path.abspath(directory)
        self.path = os.path.join(self.directory, "sd-recording-notes.json")
        self._lock = threading.RLock()
        self._notes = self._load()

    @staticmethod
    def key(start, end):
        start = _as_timestamp(start)
        end = _as_timestamp(end)
        if start is None or end is None:
            return None
        return f"{start}-{end}"

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        notes = {}
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            name = _clean_text(value.get("name"), self.MAX_NAME_LENGTH)
            comment = _clean_text(value.get("comment"), self.MAX_COMMENT_LENGTH)
            if name is None and comment is None:
                continue
            notes[str(key)] = {
                "name": name,
                "comment": comment,
                "updatedAt": value.get("updatedAt") or 0,
            }
        return notes

    def _save(self):
        try:
            os.makedirs(self.directory, exist_ok=True)
        except OSError as exc:
            raise RecordingError("Could not save the recording note") from exc
        temporary = self.path + ".tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as file:
                json.dump(self._notes, file)
            os.replace(temporary, self.path)
        except OSError as exc:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise RecordingError("Could not save the recording note") from exc

    def get(self, start, end):
        key = self.key(start, end)
        if key is None:
            return None
        with self._lock:
            note = self._notes.get(key)
            return dict(note) if note else None

    def annotate(self, recordings):
        with self._lock:
            notes = {key: dict(value) for key, value in self._notes.items()}
        for recording in recordings:
            note = notes.get(
                self.key(recording.get("startTime"), recording.get("endTime"))
            )
            recording["name"] = note.get("name") if note else None
            recording["comment"] = note.get("comment") if note else None
        return recordings

    def update(self, start, end, name=None, comment=None):
        key = self.key(start, end)
        if key is None:
            raise RecordingError("A valid recording start and end time are required")
        with self._lock:
            existing = self._notes.get(key, {})
            updated = {
                "name": existing.get("name"),
                "comment": existing.get("comment"),
                "updatedAt": existing.get("updatedAt") or 0,
            }
            if name is not None:
                updated["name"] = _clean_text(name, self.MAX_NAME_LENGTH)
            if comment is not None:
                updated["comment"] = _clean_text(comment, self.MAX_COMMENT_LENGTH)
            if updated["name"] is None and updated["comment"] is None:
                self._notes.pop(key, None)
            else:
                updated["updatedAt"] = time.time()
                self._notes[key] = updated
                if len(self._notes) > self.MAX_ENTRIES:
                    ordered = sorted(
                        self._notes.items(),
                        key=lambda item: item[1].get("updatedAt") or 0,
                        reverse=True,
                    )
                    self._notes = dict(ordered[: self.MAX_ENTRIES])
            self._save()
        return {
            "name": updated.get("name"),
            "comment": updated.get("comment"),
        }


class RecordingService:
    """Small wrapper around the Tapo local API for SD card browsing."""

    def __init__(self, host, cloud_password):
        self.host = host
        self.cloud_password = (cloud_password or "").strip()
        self._lock = threading.RLock()
        self._client = None
        self._suspended_until = 0.0

    def _login(self):
        if Tapo is None:
            raise RecordingError("pytapo is not installed")
        now = time.monotonic()
        if now < self._suspended_until:
            seconds = int(self._suspended_until - now)
            minutes = max(1, round(seconds / 60))
            raise RecordingError(
                f"Tapo login is temporarily locked by the camera. Try again in about {minutes} minutes."
            )
        try:
            return Tapo(
                self.host,
                "admin",
                self.cloud_password,
                cloudPassword=self.cloud_password,
                printWarnInformation=False,
            )
        except Exception as exc:
            text = str(exc)
            if "temporary suspension" in text.lower():
                self._suspended_until = time.monotonic() + _suspension_seconds(text)
            raise RecordingError(friendly_recording_error(exc)) from exc

    def _client_or_raise(self):
        with self._lock:
            if self._client is None:
                self._client = self._login()
            return self._client

    def _call(self, action):
        with self._lock:
            client = self._client_or_raise()
            try:
                return action(client)
            except Exception as exc:
                text = str(exc)
                if "temporary suspension" in text.lower():
                    self._suspended_until = time.monotonic() + _suspension_seconds(text)
                self._client = None
                raise RecordingError(friendly_recording_error(exc)) from exc

    def connect(self):
        """Create a dedicated client for a download worker."""
        with self._lock:
            return self._login()

    def close(self):
        with self._lock:
            client = self._client
            self._client = None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def storage(self):
        return _normalize_storage(self._call(lambda client: client.getSDCard()))

    def dates(self, start_date="20000101", end_date=None):
        raw = self._call(
            lambda client: client.getRecordingsList(start_date, end_date)
        )
        dates = set()
        for item in _flatten_results(raw):
            value = item.get("date")
            if value:
                try:
                    dates.add(normalize_date(value))
                except RecordingError:
                    continue
        return sorted(dates, reverse=True)

    def list_for_date(self, date):
        day = normalize_date(date)
        raw = self._call(lambda client: client.getRecordings(day))
        return _normalize_recordings(raw)


class _ThreadBoundTapo:
    """Run every Tapo call on one dedicated thread.

    pytapo's synchronous API drives its own internal event loop. The Downloader
    calls those methods from inside its own asyncio loop, which makes pytapo
    raise "Cannot run the event loop while another loop is running". Forwarding
    each call to a single worker thread keeps the internal loop off the
    Downloader's thread while keeping every call on the same thread.
    """

    def __init__(self, tapo, executor):
        self._tapo = tapo
        self._executor = executor

    def __getattr__(self, name):
        value = getattr(self._tapo, name)
        if not callable(value):
            return value
        executor = self._executor

        def bound(*args, **kwargs):
            return executor.submit(value, *args, **kwargs).result()

        return bound


class RecordingDownloadManager:
    """Downloads SD card clips serially into a temporary directory."""

    MAX_AGE_SECONDS = 6 * 60 * 60

    def __init__(self, service):
        self._service = service
        self._lock = threading.RLock()
        self._jobs = {}
        self._queue = queue.Queue()
        self._stop_event = threading.Event()
        self._directory = tempfile.mkdtemp(prefix="tapo-recordings-")
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    @property
    def directory(self):
        with self._lock:
            return self._directory

    def create(self, start, end):
        start = _as_timestamp(start)
        end = _as_timestamp(end)
        if start is None or end is None or end <= start:
            raise RecordingError("A valid recording start and end time are required")
        self._prune()
        job = {
            "id": uuid.uuid4().hex,
            "startTime": start,
            "endTime": end,
            "duration": end - start,
            "state": "queued",
            "action": "Queued",
            "progress": 0.0,
            "total": float(end - start),
            "error": None,
            "created": time.time(),
            "path": None,
        }
        with self._lock:
            if self._stop_event.is_set():
                raise RecordingError("Recording downloads are not available")
            self._jobs[job["id"]] = job
        self._queue.put(job["id"])
        return self._public_job(job)

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            return self._public_job(job) if job else None

    def path(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job["state"] != "ready":
                return None
            path = job["path"]
        return path if path and os.path.isfile(path) else None

    def stop(self):
        with self._lock:
            self._stop_event.set()
            self._jobs.clear()
            directory = self._directory
            self._directory = None
        self._queue.put(None)
        if directory:
            shutil.rmtree(directory, ignore_errors=True)

    def _public_job(self, job):
        return {
            "id": job["id"],
            "startTime": job["startTime"],
            "endTime": job["endTime"],
            "duration": job["duration"],
            "state": job["state"],
            "action": job["action"],
            "progress": round(float(job["progress"]), 2),
            "total": round(float(job["total"]), 2),
            "error": job["error"],
        }

    def _prune(self):
        cutoff = time.time() - self.MAX_AGE_SECONDS
        removed = []
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if job["created"] < cutoff and job["state"] in ("ready", "error"):
                    removed.append(self._jobs.pop(job_id))
        for job in removed:
            if job.get("path"):
                try:
                    os.remove(job["path"])
                except OSError:
                    pass

    def _update(self, job, **values):
        with self._lock:
            job.update(values)

    def _worker(self):
        while not self._stop_event.is_set():
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job_id is None:
                break
            with self._lock:
                job = self._jobs.get(job_id)
            if job is None:
                continue
            self._run_job(job)
        with self._lock:
            self._jobs.clear()

    def _run_job(self, job):
        if self._stop_event.is_set():
            return

        try:
            from pytapo.media_stream.downloader import Downloader
        except ImportError:
            self._update(
                job,
                state="error",
                action="Error",
                error="Recording support is missing aiofiles. Reinstall the Python requirements.",
            )
            return

        with self._lock:
            directory = self._directory
            stopped = self._stop_event.is_set()
        if stopped or not directory:
            return

        client = None
        executor = None
        try:
            self._update(job, state="downloading", action="Connecting")
            # pytapo's synchronous API and the Downloader both own event loops.
            # Keep the API calls on their own single thread so neither loop
            # interrupts the other.
            executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="tapo-download"
            )
            client = executor.submit(self._service.connect).result()
            time_correction = executor.submit(client.getTimeCorrection).result()
            if not isinstance(time_correction, (int, float)):
                time_correction = 0
            file_name = f"{job['startTime']}-{job['endTime']}.mp4"
            path = os.path.join(directory, file_name)
            downloader = Downloader(
                _ThreadBoundTapo(client, executor),
                job["startTime"],
                job["endTime"],
                time_correction,
                directory + os.sep,
                padding=0,
                fileName=file_name,
                progressInterval=0.5,
                output="mp4",
            )
            asyncio.run(self._consume(downloader, job))
            with self._lock:
                stopped = self._stop_event.is_set()
            if stopped:
                return
            if not os.path.isfile(path) or os.path.getsize(path) == 0:
                raise RecordingError("Camera did not send any recording data")
            self._update(
                job,
                state="ready",
                action="Ready",
                progress=job["duration"],
                total=float(job["duration"]),
                path=path,
            )
        except Exception as exc:
            self._update(
                job,
                state="error",
                action="Error",
                error=friendly_recording_error(exc),
            )
        finally:
            if executor is not None:
                if client is not None:
                    try:
                        executor.submit(client.close).result(timeout=10)
                    except Exception:
                        pass
                executor.shutdown(wait=False, cancel_futures=True)

    async def _consume(self, downloader, job):
        last_action = ""
        async for status in downloader.download():
            if self._stop_event.is_set():
                return
            action = str(status.get("currentAction") or "")
            last_action = action
            progress = status.get("progress")
            total = status.get("total")
            converting = action.startswith("Converting")
            with self._lock:
                job["action"] = action or job["action"]
                if progress is not None and not (converting and not total):
                    job["progress"] = max(0.0, float(progress))
                if total:
                    job["total"] = float(total)
            if action in ("Recording in progress", "Camera busy", "Giving up"):
                break
        if last_action == "Recording in progress":
            raise RecordingError("The recording is still being written by the camera")
        if last_action == "Camera busy":
            raise RecordingError("The camera is busy with another stream. Try again later.")
        if last_action == "Giving up":
            raise RecordingError("The camera stopped sending the recording")


_END_OF_STREAM = object()


class _StreamStopped(Exception):
    pass


class RecordingStreamManager:
    """Streams one SD card clip into a live HLS playlist.

    The Tapo playback endpoint sends the recording in realtime as MPEG-TS. The
    video is piped straight into ffmpeg while the private G.711 audio stream is
    fed through a second pipe, so playback can begin after the first segment
    instead of waiting for the complete clip to download.

    .. note::
        Video backpressure is handled by a separate writer task so camera
        responses (which also carry audio) are never starved.
    """

    _max_queued_video = 64 * 1024 * 1024

    def __init__(self, service, ffmpeg="ffmpeg"):
        self._service = service
        self._ffmpeg = ffmpeg
        self._lock = threading.RLock()
        self._stream = None
        self._root = tempfile.mkdtemp(prefix="tapo-recording-streams-")
        self._closed = False

    def start(self, start, end):
        start = _as_timestamp(start)
        end = _as_timestamp(end)
        if start is None or end is None or end <= start:
            raise RecordingError("A valid recording start and end time are required")
        self._stop_current()
        stream = {
            "id": uuid.uuid4().hex,
            "startTime": start,
            "endTime": end,
            "duration": end - start,
            "state": "starting",
            "action": "Connecting",
            "error": None,
            "created": time.time(),
            "directory": None,
            "playlist": None,
            "proc": None,
            "log": None,
            "audio_r": None,
            "audio_w": None,
            "thread": None,
            "stop_event": threading.Event(),
            "async_stop": None,
            "loop": None,
            "stopping": False,
            "natural_end": False,
            "ts_buffer": bytearray(),
        }
        with self._lock:
            if self._closed:
                raise RecordingError("Recording playback is not available")
            directory = os.path.join(self._root, stream["id"])
            os.makedirs(directory, exist_ok=True)
            stream["directory"] = directory
            stream["playlist"] = os.path.join(directory, "index.m3u8")
            thread = threading.Thread(
                target=self._run,
                args=(stream,),
                daemon=True,
                name=f"tapo-recording-{stream['id'][:8]}",
            )
            stream["thread"] = thread
            self._stream = stream
            thread.start()
        return self._public(stream)

    def get(self, stream_id):
        with self._lock:
            stream = self._stream
            if stream is None or stream["id"] != stream_id:
                return None
        return self._public(stream)

    def directory(self, stream_id):
        with self._lock:
            stream = self._stream
            if stream is None or stream["id"] != stream_id:
                return None
            directory = stream.get("directory")
        if directory and os.path.isdir(directory):
            return directory
        return None

    def playlist(self, stream_id):
        directory = self.directory(stream_id)
        if not directory:
            return None
        path = os.path.join(directory, "index.m3u8")
        return path if os.path.isfile(path) else None

    def stop(self, stream_id=None):
        with self._lock:
            stream = self._stream
            if stream_id is not None and (stream is None or stream["id"] != stream_id):
                return False
            self._stream = None
        if stream is not None:
            self._stop_stream(stream)
        return True

    def close(self):
        with self._lock:
            self._closed = True
            stream = self._stream
            self._stream = None
            root = self._root
            self._root = None
        if stream is not None:
            self._stop_stream(stream)
        if root:
            shutil.rmtree(root, ignore_errors=True)

    def _stop_current(self):
        with self._lock:
            stream = self._stream
            self._stream = None
        if stream is not None:
            self._stop_stream(stream)

    def _stop_stream(self, stream):
        stream["stopping"] = True
        stream["stop_event"].set()
        loop = stream.get("loop")
        async_stop = stream.get("async_stop")
        if loop is not None and async_stop is not None:
            try:
                loop.call_soon_threadsafe(async_stop.set)
            except RuntimeError:
                pass
        proc = stream.get("proc")
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            except OSError:
                pass
        self._close_audio(stream)
        thread = stream.get("thread")
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=6)
        self._cleanup_directory(stream)
        with self._lock:
            if stream["state"] in ("starting", "playing", "finalizing"):
                stream["state"] = "stopped"
                stream["action"] = "Stopped"

    def _run(self, stream):
        error = None
        try:
            asyncio.run(self._stream_recording(stream))
        except _StreamStopped:
            pass
        except Exception as exc:
            error = exc
        finally:
            proc = stream.get("proc")
            if proc is not None and proc.returncode is None:
                try:
                    proc.terminate()
                except (ProcessLookupError, OSError):
                    pass
            self._close_audio(stream)
            self._close_log(stream)
            with self._lock:
                if stream["stopping"]:
                    if stream["state"] != "error":
                        stream["state"] = "stopped"
                        stream["action"] = "Stopped"
                elif error is not None:
                    stream["state"] = "error"
                    stream["action"] = "Error"
                    stream["error"] = friendly_recording_error(error)
                elif stream["state"] != "error":
                    stream["state"] = "ended"
                    stream["action"] = "Finished"

    async def _stream_recording(self, stream):
        executor = None
        client = None
        proc = None
        video_queue = None
        writer_task = None
        stream["loop"] = asyncio.get_running_loop()
        stream["async_stop"] = asyncio.Event()
        stream["abort"] = asyncio.Event()
        stream["writer_error"] = None
        stream["queued_bytes"] = 0
        try:
            if stream["stop_event"].is_set():
                raise _StreamStopped()

            executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="tapo-recording-api"
            )
            client = executor.submit(self._service.connect).result()
            if stream["stop_event"].is_set():
                raise _StreamStopped()
            try:
                time_correction = executor.submit(client.getTimeCorrection).result()
            except Exception:
                time_correction = 0
            if not isinstance(time_correction, (int, float)):
                time_correction = 0
            if time.time() - 60 - time_correction < stream["endTime"]:
                raise RecordingError(
                    "The recording is still being written by the camera"
                )

            include_audio, audio_format, audio_rate = self._audio_config(
                client, executor
            )
            user_id = executor.submit(client.getUserID).result()
            request = self._build_request(stream, user_id)
            from pytapo.media_stream._utils import StreamType

            media = executor.submit(
                client.getMediaSession, StreamType.Download
            ).result()
            media.set_window_size(50)
            async with media:
                responses = media.transceive(
                    json.dumps(request), no_data_timeout=20
                )
                first = None
                while True:
                    resp = await self._next_response(responses, stream)
                    if resp is _END_OF_STREAM:
                        break
                    if resp.mimetype == "application/json":
                        self._check_json_response(resp)
                        continue
                    if resp.mimetype == "video/mp2t":
                        first = resp
                        break
                if first is None:
                    raise RecordingError(
                        "The camera did not send any recording data"
                    )

                if first.audioPayloadType is not None:
                    include_audio = True
                    audio_format = self._audio_format_from_payload(
                        first.audioPayloadType, audio_format
                    )
                if stream["stop_event"].is_set():
                    raise _StreamStopped()
                proc = await self._spawn_ffmpeg(
                    stream, include_audio, audio_format, audio_rate
                )
                stream["proc"] = proc
                video_queue = asyncio.Queue()
                writer_task = asyncio.create_task(
                    self._write_video(stream, proc, video_queue)
                )
                with self._lock:
                    if stream["stopping"]:
                        raise _StreamStopped()
                    stream["state"] = "playing"
                    stream["action"] = "Playing"
                first_data_at = time.monotonic()
                self._queue_response(stream, first, include_audio, video_queue)
                deadline = first_data_at + stream["duration"] + 2.0

                while True:
                    resp = await self._next_response(responses, stream)
                    if resp is _END_OF_STREAM:
                        break
                    if resp.mimetype == "application/json":
                        if self._is_finished(resp):
                            break
                        self._check_json_response(resp)
                        continue
                    if resp.mimetype != "video/mp2t":
                        continue
                    self._queue_response(stream, resp, include_audio, video_queue)
                    if time.monotonic() >= deadline:
                        break

                stream["natural_end"] = True
                with self._lock:
                    if stream["state"] not in ("error", "stopped"):
                        stream["state"] = "finalizing"
                        stream["action"] = "Finishing"
        finally:
            if proc is not None:
                await self._stop_video_writer(stream, video_queue, writer_task)
                await self._finish_ffmpeg(stream, proc)
            self._close_audio(stream)
            self._close_log(stream)
            if client is not None and executor is not None:
                try:
                    executor.submit(client.close).result(timeout=10)
                except Exception:
                    pass
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)

    async def _write_video(self, stream, proc, queue):
        """Write queued video to ffmpeg without stalling audio intake.

        ffmpeg reads its two inputs in lockstep. If the camera is still
        delivering audio, blocking on video backpressure while holding the
        only reader of the camera stream deadlocks both sides. The queue lets
        the intake loop keep feeding audio while this task waits for ffmpeg.
        """
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    return
                stream["queued_bytes"] = max(
                    0, stream["queued_bytes"] - len(chunk)
                )
                if proc.stdin is None or proc.stdin.is_closing():
                    raise RecordingError("The recording stream stopped")
                proc.stdin.write(chunk)
                await asyncio.wait_for(proc.stdin.drain(), timeout=60)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            stream["writer_error"] = exc
            abort = stream.get("abort")
            if abort is not None:
                abort.set()

    async def _stop_video_writer(self, stream, queue, writer_task):
        if writer_task is None:
            return
        if queue is not None:
            try:
                queue.put_nowait(None)
            except Exception:
                pass
        try:
            await asyncio.wait_for(asyncio.shield(writer_task), timeout=30)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            writer_task.cancel()
            try:
                await writer_task
            except (asyncio.CancelledError, Exception):
                pass
        except Exception:
            pass

    async def _next_response(self, responses, stream):
        data_task = asyncio.ensure_future(responses.__anext__())
        stop_task = asyncio.ensure_future(stream["async_stop"].wait())
        abort = stream.get("abort")
        abort_task = asyncio.ensure_future(abort.wait()) if abort is not None else None
        waiters = {data_task, stop_task}
        if abort_task is not None:
            waiters.add(abort_task)
        try:
            done, _ = await asyncio.wait(
                waiters, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            data_task.cancel()
            stop_task.cancel()
            if abort_task is not None:
                abort_task.cancel()
            raise
        finally:
            stop_task.cancel()
            try:
                await stop_task
            except (asyncio.CancelledError, Exception):
                pass
            if abort_task is not None:
                abort_task.cancel()
                try:
                    await abort_task
                except (asyncio.CancelledError, Exception):
                    pass
        if data_task in done:
            try:
                return data_task.result()
            except StopAsyncIteration:
                return _END_OF_STREAM
        data_task.cancel()
        try:
            await data_task
        except (asyncio.CancelledError, StopAsyncIteration, Exception):
            pass
        if abort_task is not None and abort_task in done:
            error = stream.get("writer_error")
            if error is not None:
                raise error
        raise _StreamStopped()

    def _audio_config(self, client, executor):
        include_audio = True
        audio_format = "alaw"
        audio_rate = 8000
        try:
            config = executor.submit(client.getAudioConfig).result(timeout=15)
        except Exception:
            return include_audio, audio_format, audio_rate
        audio_config = (config or {}).get("audio_config") or {}
        microphone = audio_config.get("microphone")
        if isinstance(microphone, dict):
            if not microphone:
                include_audio = False
            else:
                encode = str(microphone.get("encode_type", "")).lower()
                if "ulaw" in encode:
                    audio_format = "mulaw"
                elif "alaw" in encode:
                    audio_format = "alaw"
                sampling = microphone.get("sampling_rate")
                if sampling is not None:
                    try:
                        audio_rate = int(sampling) * 1000
                    except (TypeError, ValueError):
                        pass
        return include_audio, audio_format, audio_rate

    @staticmethod
    def _audio_format_from_payload(payload_type, default):
        try:
            from rtp import PayloadType
        except ImportError:
            return default
        if payload_type == PayloadType.PCMU:
            return "mulaw"
        if payload_type == PayloadType.PCMA:
            return "alaw"
        return default

    @staticmethod
    def _build_request(stream, user_id):
        return {
            "type": "request",
            "seq": 1,
            "params": {
                "playback": {
                    "client_id": user_id,
                    "channels": [0, 1],
                    "scale": "1/1",
                    "start_time": str(stream["startTime"]),
                    "end_time": str(stream["endTime"]),
                    "event_type": [1, 2],
                },
                "method": "get",
            },
        }

    @staticmethod
    def _json_params(resp):
        try:
            return json.loads(resp.plaintext.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _check_json_response(self, resp):
        data = self._json_params(resp)
        if data.get("type") != "response":
            return
        error_code = (data.get("params") or {}).get("error_code")
        if error_code in (-52405, -52407, -52417, -52435):
            raise RecordingError(
                "The camera is busy with another stream. Stop the live view and try again."
            )
        if error_code not in (0, None):
            raise RecordingError(
                f"The camera refused the recording request (error {error_code})"
            )

    def _is_finished(self, resp):
        data = self._json_params(resp)
        params = data.get("params") or {}
        return (
            data.get("type") == "notification"
            and params.get("event_type") == "stream_status"
            and params.get("status") == "finished"
        )

    async def _spawn_ffmpeg(self, stream, include_audio, audio_format, audio_rate):
        directory = stream["directory"]
        playlist = os.path.join(directory, "index.m3u8")
        log = open(os.path.join(directory, "ffmpeg.log"), "ab", buffering=0)
        stream["log"] = log
        command = [
            self._ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-probesize",
            "32768",
            "-analyzeduration",
            "0",
            "-f",
            "mpegts",
            "-i",
            "pipe:0",
        ]
        pass_fds = ()
        audio_r = audio_w = None
        if include_audio:
            audio_r, audio_w = os.pipe()
            stream["audio_r"] = audio_r
            stream["audio_w"] = audio_w
            pass_fds = (audio_r,)
            command += [
                "-f",
                audio_format,
                "-ar",
                str(audio_rate),
                "-i",
                f"/dev/fd/{audio_r}",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "64k",
            ]
        else:
            command += ["-map", "0:v:0", "-c:v", "copy"]
        command += [
            "-f",
            "hls",
            "-hls_time",
            "1",
            "-hls_playlist_type",
            "event",
            "-hls_flags",
            "temp_file",
            "-hls_segment_filename",
            os.path.join(directory, "seg%05d.ts"),
            playlist,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=log,
                pass_fds=pass_fds,
            )
        finally:
            if audio_r is not None:
                try:
                    os.close(audio_r)
                except OSError:
                    pass
                stream["audio_r"] = None
        return proc

    def _queue_response(self, stream, resp, include_audio, queue):
        """Feed camera audio immediately and enqueue video for the writer task."""
        if include_audio and resp.audioPayload:
            audio_w = stream.get("audio_w")
            if audio_w is not None:
                try:
                    os.write(audio_w, resp.audioPayload)
                except (BrokenPipeError, OSError) as exc:
                    raise RecordingError(
                        "The camera stopped sending recording audio"
                    ) from exc
        data = resp.plaintext
        if not data:
            return
        buf = stream["ts_buffer"]
        buf.extend(data)
        while len(buf) >= 188 and buf[0] != 0x47:
            pos = buf.find(b"\x47", 1)
            if pos < 0:
                buf.clear()
                return
            del buf[:pos]
        count = len(buf) - (len(buf) % 188)
        if count <= 0:
            return
        chunk = bytes(buf[:count])
        del buf[:count]
        if stream["queued_bytes"] + len(chunk) > self._max_queued_video:
            raise RecordingError(
                "The recording stream is falling behind playback"
            )
        stream["queued_bytes"] += len(chunk)
        queue.put_nowait(chunk)

    async def _finish_ffmpeg(self, stream, proc):
        graceful = stream.get("natural_end") and not stream["stopping"]
        if proc.stdin is not None and not proc.stdin.is_closing():
            try:
                proc.stdin.close()
            except (OSError, RuntimeError):
                pass
        self._close_audio(stream)
        if graceful:
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
                return
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if proc.returncode is None:
            try:
                proc.terminate()
            except (ProcessLookupError, OSError):
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass

    @staticmethod
    def _close_audio(stream):
        audio_w = stream.get("audio_w")
        stream["audio_w"] = None
        if audio_w is not None:
            try:
                os.close(audio_w)
            except OSError:
                pass

    @staticmethod
    def _close_log(stream):
        log = stream.get("log")
        stream["log"] = None
        if log is not None:
            try:
                log.close()
            except OSError:
                pass

    @staticmethod
    def _cleanup_directory(stream):
        directory = stream.get("directory")
        stream["directory"] = None
        stream["playlist"] = None
        if directory:
            shutil.rmtree(directory, ignore_errors=True)

    def _public(self, stream):
        ready = False
        path = stream.get("playlist")
        if path:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as file:
                    playlist = file.read()
            except OSError:
                playlist = ""
            durations = [
                float(value)
                for value in re.findall(r"#EXTINF:\s*([0-9.]+)", playlist)
            ]
            if any(duration > 0 for duration in durations):
                directory = stream.get("directory")
                if directory:
                    try:
                        ready = any(
                            name.endswith(".ts")
                            and os.path.getsize(os.path.join(directory, name)) > 0
                            for name in os.listdir(directory)
                        )
                    except OSError:
                        ready = False
        with self._lock:
            return {
                "id": stream["id"],
                "startTime": stream["startTime"],
                "endTime": stream["endTime"],
                "duration": stream["duration"],
                "state": stream["state"],
                "action": stream["action"],
                "error": stream["error"],
                "ready": ready,
            }
