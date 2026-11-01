import os
import shutil
import subprocess
import tempfile
import threading


class StreamManager:
    def __init__(self, ffmpeg="ffmpeg"):
        self._ffmpeg = ffmpeg
        self._lock = threading.RLock()
        self._proc = None
        self._dir = None
        self._log = None
        self._url = None
        self._stream = "stream1"
        self._stop_event = threading.Event()
        self._thread = None

    @property
    def directory(self):
        with self._lock:
            return self._dir

    @property
    def stream(self):
        with self._lock:
            return self._stream

    def is_running(self):
        with self._lock:
            return bool(self._proc and self._proc.poll() is None)

    def latest_segment(self):
        with self._lock:
            directory = self._dir
        if not directory:
            return None
        try:
            names = [name for name in os.listdir(directory) if name.endswith(".ts")]
        except OSError:
            return None
        if not names:
            return None

        def modified(name):
            try:
                return os.path.getmtime(os.path.join(directory, name))
            except OSError:
                return 0

        names.sort(key=modified)
        chosen = names[-2] if len(names) > 1 else names[-1]
        path = os.path.join(directory, chosen)
        return path if os.path.exists(path) else None

    def start(self, url, stream):
        with self._lock:
            self._stop_event.set()
            self._kill_locked()
            self._url = url
            self._stream = stream
            self._dir = tempfile.mkdtemp(prefix="tapo-hls-")
            self._stop_event = threading.Event()
            self._spawn_locked()
            self._thread = threading.Thread(
                target=self._supervise, args=(self._stop_event,), daemon=True
            )
            self._thread.start()

    def stop(self):
        with self._lock:
            self._stop_event.set()
            self._url = None
            self._kill_locked()

    def _spawn_locked(self):
        playlist = os.path.join(self._dir, "index.m3u8")
        log_path = os.path.join(self._dir, "ffmpeg.log")
        self._log = open(log_path, "ab", buffering=0)
        command = [
            self._ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-rtsp_transport",
            "tcp",
            "-i",
            self._url,
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-f",
            "hls",
            "-hls_time",
            "0.6",
            "-hls_list_size",
            "6",
            "-hls_flags",
            "delete_segments+omit_endlist",
            "-hls_segment_filename",
            os.path.join(self._dir, "seg%05d.ts"),
            playlist,
        ]
        self._proc = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=self._log
        )

    def _close_log_locked(self):
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None

    def _kill_locked(self):
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        self._close_log_locked()
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    def _clear_output_locked(self):
        if not self._dir or not os.path.isdir(self._dir):
            return
        for name in os.listdir(self._dir):
            if name.endswith(".ts") or name.endswith(".m3u8"):
                try:
                    os.remove(os.path.join(self._dir, name))
                except OSError:
                    pass

    def _supervise(self, stop_event):
        failures = 0
        while not stop_event.wait(2):
            with self._lock:
                if stop_event.is_set():
                    return
                if self._proc is not None and self._proc.poll() is None:
                    failures = 0
                    continue
                self._proc = None
                self._clear_output_locked()
                self._close_log_locked()
                try:
                    self._spawn_locked()
                    failures += 1
                except OSError:
                    failures += 1
            stop_event.wait(min(30, 2 * failures))
