"""Two-way audio ("call") support for Tapo cameras.

Tapo cameras expose a talkback service on TCP port 8800. The protocol is
similar to the media stream: an HTTP multipart request is authenticated with
the Tapo account (cloud) password, a talk session is requested with a JSON
part, and audio is then sent as MPEG-TS parts containing G.711 A-law.

The implementation below mirrors the working protocol used by go2rtc. It is
deliberately self-contained so a call does not depend on the Tapo control API
(and therefore cannot trigger the control API's login lockout).
"""

import asyncio
import hashlib
import json
import os
import re
import struct
import threading
import time

from tapo_api import friendly_tapo_error


class TalkError(Exception):
    pass


def friendly_talk_error(exc):
    text = str(exc)
    lowered = text.lower()
    if "talk_is_used" in lowered or "-52411" in text:
        return "The camera is already in a call. End the other call and try again."
    if "temporary suspension" in lowered:
        return friendly_tapo_error(exc)
    if "authority" in lowered or "unauthorized" in lowered or "401" in lowered:
        return (
            "The camera rejected the call login. Check the Tapo account password "
            "in Settings."
        )
    if "timed out" in lowered or "timeout" in lowered:
        return "The camera did not answer the call."
    if "connection" in lowered or "refused" in lowered or "unreachable" in lowered:
        return "Could not reach the camera's talkback service."
    return friendly_tapo_error(exc)


_TALK_PATH = "/stream"
_TALK_PORT = 8800
_CLIENT_BOUNDARY = b"----client-stream-boundary--"
_DEFAULT_DEVICE_BOUNDARY = b"--device-stream-boundary--"
_TALK_REQUEST = {
    "params": {"talk": {"mode": "aec"}, "method": "get"},
    "seq": 3,
    "type": "request",
}

# G.711 A-law is sent in 20 ms frames: 160 samples at 8 kHz.
_FRAME_BYTES = 160
_FRAME_CLOCK = 1800  # 160 samples * 90 kHz / 8 kHz


def _md5(value):
    return hashlib.md5(value.encode()).hexdigest()


def _mpeg_crc32(data):
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
    return crc


def _encode_pts(pts):
    pts &= 0x1FFFFFFFF
    return bytes(
        (
            0x20 | ((pts >> 29) & 0x0E) | 1,
            (pts >> 22) & 0xFF,
            ((pts >> 14) & 0xFE) | 1,
            (pts >> 7) & 0xFF,
            ((pts << 1) & 0xFE) | 1,
        )
    )


class _MpegTSMuxer:
    """Minimal MPEG-TS muxer for Tapo's PCMA audio stream."""

    PACKET_SIZE = 188
    PMT_PID = 0x1000
    PES_PID = 0x100
    STREAM_TYPE_PCMA_TAPO = 0x90
    PES_STREAM_ID = 0xC0

    def __init__(self):
        self._continuity = 0
        self.header = self._build_pat() + self._build_pmt()

    def _psi_packet(self, pid, section):
        header = bytes(
            (
                0x47,
                0x40 | ((pid >> 8) & 0x1F),
                pid & 0xFF,
                0x10,  # payload only, continuity counter 0
            )
        )
        return (header + b"\x00" + section).ljust(self.PACKET_SIZE, b"\x00")

    def _build_pat(self):
        section = bytearray((0x00,))  # table_id
        section_length = 13
        section += bytes((0xB0 | (section_length >> 8), section_length & 0xFF))
        section += b"\x00\x01"  # transport_stream_id
        section += b"\xC1"  # version 0, current/next 1
        section += b"\x00\x00"  # section number, last section
        section += b"\x00\x01"  # program number
        section += bytes(
            (0xE0 | ((self.PMT_PID >> 8) & 0x1F), self.PMT_PID & 0xFF)
        )
        section += struct.pack("<I", _mpeg_crc32(section))
        return self._psi_packet(0, bytes(section))

    def _build_pmt(self):
        section = bytearray((0x02,))  # table_id
        section_length = 13 + 5
        section += bytes((0xB0 | (section_length >> 8), section_length & 0xFF))
        section += b"\x00\x01"  # program number
        section += b"\xC1"  # version 0, current/next 1
        section += b"\x00\x00"  # section number, last section
        section += b"\xFF\xFF"  # PCR PID: no PCR
        section += b"\xF0\x00"  # program info length 0
        section += bytes(
            (
                self.STREAM_TYPE_PCMA_TAPO,
                0xE0 | ((self.PES_PID >> 8) & 0x1F),
                self.PES_PID & 0xFF,
                0xF0,
                0x00,  # ES info length 0
            )
        )
        section += struct.pack("<I", _mpeg_crc32(section))
        return self._psi_packet(self.PMT_PID, bytes(section))

    def payload(self, pts, audio):
        pes = bytearray()
        pes += b"\x00\x00\x01"
        pes.append(self.PES_STREAM_ID)
        size = 3 + 5 + len(audio)
        pes += struct.pack(">H", size if size <= 0xFFFF else 0)
        pes += b"\x80\x80\x05"  # marker bits, PTS only, 5-byte PTS
        pes += _encode_pts(pts)
        pes += audio

        output = bytearray()
        first = True
        while pes:
            chunk = pes[: self.PACKET_SIZE - 4]
            del pes[: self.PACKET_SIZE - 4]
            adaptation = len(chunk) < self.PACKET_SIZE - 4
            flags = 0x10 | (self._continuity & 0x0F)
            if adaptation:
                flags |= 0x20
            if first:
                flags_head = 0x40
            else:
                flags_head = 0x00
            self._continuity = (self._continuity + 1) & 0x0F
            output += bytes(
                (
                    0x47,
                    flags_head | ((self.PES_PID >> 8) & 0x1F),
                    self.PES_PID & 0xFF,
                    flags,
                )
            )
            if adaptation:
                pad = self.PACKET_SIZE - 4 - 1 - len(chunk)
                output.append(pad)
                output += b"\x00" * pad
            output += chunk
            first = False
        return bytes(output)


def _parse_digest(value):
    params = {}
    for match in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', value or ""):
        params[match.group(1).lower()] = (
            match.group(2) if match.group(2) is not None else match.group(3)
        )
    return params


def _password_digest(cloud_password, method):
    return hashlib.new(method, cloud_password.encode()).hexdigest().upper()


class TapoTalk:
    """A single live talkback session with a Tapo camera.

    The protocol runs on its own asyncio loop in a daemon thread. Audio can be
    pushed from any thread with :meth:`send_audio`.
    """

    def __init__(self, host, cloud_password, port=_TALK_PORT):
        self.host = host
        self.cloud_password = (cloud_password or "").strip()
        self.port = int(port)
        self._lock = threading.RLock()
        self._thread = None
        self._loop = None
        self._reader = None
        self._writer = None
        self._reader_task = None
        self._stop_event = None
        self._send_lock = None
        self._session_id = None
        self._device_boundary = _DEFAULT_DEVICE_BOUNDARY
        self._muxer = _MpegTSMuxer()
        self._pts = 0
        self._audio_tail = b""
        self._started = False
        self._error = None
        self._ready = threading.Event()

    @property
    def active(self):
        with self._lock:
            return self._started

    def start(self, timeout=15):
        if not self.cloud_password:
            raise TalkError(
                "Add your Tapo account password in Settings to call the camera"
            )
        with self._lock:
            if self._started:
                return
            self._error = None
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._thread_main,
                daemon=True,
                name="tapo-talk",
            )
            self._thread.start()
        if not self._ready.wait(timeout):
            self.stop()
            raise TalkError("Timed out starting the camera call")
        with self._lock:
            error = self._error
        if error:
            self.stop()
            raise TalkError(error)

    def stop(self):
        with self._lock:
            self._started = False
            loop = self._loop
            stop_event = self._stop_event
            thread = self._thread
        if loop is not None and stop_event is not None:
            try:
                loop.call_soon_threadsafe(stop_event.set)
            except RuntimeError:
                pass
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=6)

    def send_audio(self, data):
        if not data:
            return
        with self._lock:
            if not self._started or self._loop is None or self._stop_event is None:
                raise TalkError(self._error or "The camera call is not active")
            loop = self._loop
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._send_audio_async(bytes(data)), loop
            )
        except RuntimeError as exc:
            raise TalkError("The camera call has ended") from exc
        try:
            future.result(timeout=10)
        except asyncio.TimeoutError as exc:
            future.cancel()
            raise TalkError("The camera stopped accepting call audio") from exc
        except TimeoutError as exc:
            future.cancel()
            raise TalkError("The camera stopped accepting call audio") from exc
        except Exception as exc:
            if isinstance(exc, TalkError):
                raise
            raise TalkError(friendly_talk_error(exc)) from exc

    def _thread_main(self):
        error = None
        try:
            asyncio.run(self._run())
        except Exception as exc:
            error = exc
        finally:
            with self._lock:
                self._started = False
                self._loop = None
                if not self._ready.is_set():
                    self._error = friendly_talk_error(
                        error or "Could not start the camera call"
                    )
                elif error is not None:
                    self._error = friendly_talk_error(error)
                self._ready.set()

    async def _run(self):
        loop = asyncio.get_running_loop()
        with self._lock:
            self._loop = loop
            self._stop_event = asyncio.Event()
            self._send_lock = asyncio.Lock()
        try:
            await self._connect()
            session_id = await self._request_talk()
            with self._lock:
                self._session_id = session_id
            await self._send_part(b"audio/mp2t", self._muxer.header, trailer=False)
            with self._lock:
                self._started = True
                self._error = None
            self._ready.set()
            self._reader_task = asyncio.create_task(self._drain())
            await self._stop_event.wait()
        finally:
            task = self._reader_task
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            await self._close()

    async def _connect(self):
        challenge = None
        methods = ["md5"]
        for attempt in range(2):
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=10
            )
            try:
                await self._write_http(writer, None)
                status, headers = await self._read_http_response(reader)
                if status == 200:
                    self._reader = reader
                    self._writer = writer
                    self._set_boundary(headers)
                    return
                if status != 401:
                    raise TalkError(
                        f"The camera refused the call connection (HTTP {status})"
                    )
                challenge = headers.get("www-authenticate", "")
                if attempt == 0:
                    method = "sha256" if 'encrypt_type="3"' in challenge else "md5"
                    methods = [method] if method == "md5" else ["sha256", "md5"]
                auth = self._authorization(challenge, methods[attempt])
                await self._write_http(writer, auth)
                status, headers = await self._read_http_response(reader)
                if status == 200:
                    self._reader = reader
                    self._writer = writer
                    self._set_boundary(headers)
                    return
                if status == 401 and attempt == 0 and methods[0] == "sha256":
                    continue
                raise TalkError(
                    "The camera rejected the call login. Check the Tapo account "
                    "password in Settings."
                )
            finally:
                if self._writer is not writer:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except (Exception, asyncio.CancelledError):
                        pass
        raise TalkError("Could not log in to the camera's talkback service")

    def _set_boundary(self, headers):
        content_type = headers.get("content-type", "")
        match = re.search(r"boundary=([^;]+)", content_type)
        if match:
            self._device_boundary = match.group(1).strip().strip('"').encode()

    def _authorization(self, challenge, method):
        params = _parse_digest(challenge)
        realm = params.get("realm", "")
        nonce = params.get("nonce", "")
        qop = params.get("qop", "")
        if qop:
            qop = "auth" if "auth" in qop.split(",") else qop.split(",")[0]
        hashed = _password_digest(self.cloud_password, method)
        ha1 = _md5(f"admin:{realm}:{hashed}")
        ha2 = _md5(f"POST:{_TALK_PATH}")
        pieces = [
            'username="admin"',
            f'realm="{realm}"',
            f'nonce="{nonce}"',
            f'uri="{_TALK_PATH}"',
        ]
        if qop:
            nc = "00000001"
            cnonce = os.urandom(16).hex()
            response = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
            pieces += [
                f"qop={qop}",
                f"nc={nc}",
                f'cnonce="{cnonce}"',
            ]
        else:
            response = _md5(f"{ha1}:{nonce}:{ha2}")
        pieces.append(f'response="{response}"')
        if params.get("opaque"):
            pieces.append(f'opaque="{params["opaque"]}"')
            pieces.append("algorithm=MD5")
        return "Digest " + ", ".join(pieces)

    async def _write_http(self, writer, authorization):
        lines = [
            f"POST {_TALK_PATH} HTTP/1.1",
            f"Host: {self.host}:{self.port}",
            "Content-Type: multipart/mixed; boundary=--client-stream-boundary--",
            "Connection: keep-alive",
            "Content-Length: -1",
        ]
        if authorization:
            lines.append(f"Authorization: {authorization}")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await writer.drain()

    async def _read_http_response(self, reader):
        data = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        text = data.decode("latin-1")
        lines = text.split("\r\n")
        try:
            status = int(lines[0].split(" ")[1])
        except (IndexError, ValueError) as exc:
            raise TalkError("The camera sent an invalid talkback response") from exc
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length") or 0)
        if 0 < length <= 1024 * 1024:
            await asyncio.wait_for(reader.readexactly(length), timeout=10)
        return status, headers

    async def _request_talk(self):
        await self._send_part(
            b"application/json",
            json.dumps(_TALK_REQUEST, separators=(",", ":")).encode(),
            trailer=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            remaining = max(0.5, deadline - time.monotonic())
            headers, body = await self._read_part(remaining)
            mimetype = headers.get("content-type", "")
            if not mimetype.startswith("application/json"):
                continue
            if headers.get("x-if-encrypt") == "1":
                raise TalkError("The camera encrypted the call response")
            try:
                data = json.loads(body.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            params = data.get("params") or {}
            error_code = data.get("error_code", params.get("error_code"))
            if error_code not in (None, 0):
                if error_code == -52411:
                    raise TalkError("The camera is already in a call")
                raise TalkError(f"The camera refused the call (error {error_code})")
            session_id = params.get("session_id")
            if session_id is not None:
                return str(session_id)
        raise TalkError("The camera did not start a call session")

    async def _read_part(self, timeout):
        try:
            header_block = await asyncio.wait_for(
                self._reader.readuntil(b"\r\n\r\n"), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            raise TalkError("The camera did not answer the call") from exc
        headers = {}
        for line in header_block.decode("latin-1").split("\r\n"):
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length") or 0)
        body = b""
        if length:
            body = await asyncio.wait_for(
                self._reader.readexactly(length), timeout=timeout
            )
        return headers, body

    async def _send_part(self, mimetype, body, trailer=True):
        headers = [
            f"Content-Type: {mimetype.decode()}",
            f"Content-Length: {len(body)}",
        ]
        if self._session_id is not None:
            headers += [
                "X-If-Encrypt: 0",
                f"X-Session-Id: {self._session_id}",
            ]
        part = bytearray()
        part += _CLIENT_BOUNDARY + b"\r\n"
        for header in headers:
            part += header.encode() + b"\r\n"
        part += b"\r\n"
        part += body
        if trailer:
            part += b"\r\n"
        async with self._send_lock:
            if self._writer is None:
                raise TalkError("The camera call has ended")
            self._writer.write(part)
            await asyncio.wait_for(self._writer.drain(), timeout=8)

    async def _send_audio_async(self, data):
        stop_event = self._stop_event
        if stop_event is None or stop_event.is_set():
            raise TalkError("The camera call has ended")
        self._audio_tail += data
        frames = []
        while len(self._audio_tail) >= _FRAME_BYTES:
            frame = self._audio_tail[:_FRAME_BYTES]
            self._audio_tail = self._audio_tail[_FRAME_BYTES:]
            frames.append(self._muxer.payload(self._pts & 0x1FFFFFFFF, frame))
            self._pts += _FRAME_CLOCK
        if not frames:
            return
        await self._send_part(b"audio/mp2t", b"".join(frames), trailer=False)

    async def _drain(self):
        try:
            while not self._stop_event.is_set():
                try:
                    data = await asyncio.wait_for(self._reader.read(4096), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                if not data:
                    break
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            if self._stop_event is not None:
                self._stop_event.set()

    async def _close(self):
        writer = self._writer
        self._writer = None
        self._reader = None
        if writer is not None:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=3)
            except (Exception, asyncio.CancelledError):
                pass
