import threading

from onvif import ONVIFCamera


class CameraError(Exception):
    pass


def _get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _token_of(obj):
    token = _get(obj, "token") or _get(obj, "_token") or _get(obj, "PresetToken")
    if isinstance(token, dict):
        token = token.get("_value_1") or token.get("token")
    return str(token) if token is not None else None


def friendly_error(exc):
    text = str(exc)
    lowered = text.lower()
    if "authority failure" in lowered or "notauthorized" in lowered or "unauthorized" in lowered:
        return "Wrong camera username or password"
    if "timed out" in lowered or "timeout" in lowered:
        return "Camera did not respond"
    if "connection refused" in lowered or "connection reset" in lowered:
        return "Could not connect to the camera"
    return text or exc.__class__.__name__


class CameraController:
    MAX_HOLD_SECONDS = 10.0

    def __init__(self, host, port, user, password):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self._lock = threading.RLock()
        self._timer = None
        self._connect()

    def _connect(self):
        try:
            cam = ONVIFCamera(self.host, self.port, self.user, self.password)
            info = cam.devicemgmt.GetDeviceInformation()
            media = cam.create_media_service()
            profiles = media.GetProfiles()
            if not profiles:
                raise CameraError("Camera returned no media profiles")
            ptz = cam.create_ptz_service()
        except CameraError:
            raise
        except Exception as exc:
            raise CameraError(friendly_error(exc)) from exc

        self._cam = cam
        self._info = info
        self._media = media
        self._profiles = profiles
        self._ptz = ptz
        self._profile = profiles[0]
        self._token = _token_of(self._profile)
        if not self._token:
            raise CameraError("Could not read the camera profile token")

    def device(self):
        info = self._info or {}
        return {
            "manufacturer": str(_get(info, "Manufacturer", "") or ""),
            "model": str(_get(info, "Model", "") or ""),
            "firmware": str(_get(info, "FirmwareVersion", "") or ""),
            "serial": str(_get(info, "SerialNumber", "") or ""),
            "hardware": str(_get(info, "HardwareId", "") or ""),
        }

    def _cancel_timer(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def move(self, pan, tilt, zoom, speed):
        with self._lock:
            velocity = {"PanTilt": {"x": pan * speed, "y": tilt * speed}}
            if zoom:
                velocity["Zoom"] = {"x": zoom * speed}
            try:
                self._ptz.ContinuousMove({"ProfileToken": self._token, "Velocity": velocity})
            except Exception as exc:
                raise CameraError(friendly_error(exc)) from exc
            self._cancel_timer()
            self._timer = threading.Timer(self.MAX_HOLD_SECONDS, self.stop)
            self._timer.daemon = True
            self._timer.start()

    def stop(self):
        with self._lock:
            self._cancel_timer()
            try:
                self._ptz.Stop({"ProfileToken": self._token, "PanTilt": True, "Zoom": True})
            except Exception:
                pass

    def home(self, speed=0.6):
        with self._lock:
            try:
                self._ptz.GotoHomePosition(
                    {
                        "ProfileToken": self._token,
                        "Speed": {
                            "PanTilt": {"x": speed, "y": speed},
                            "Zoom": {"x": speed},
                        },
                    }
                )
                return
            except Exception:
                pass
            try:
                self._ptz.AbsoluteMove(
                    {
                        "ProfileToken": self._token,
                        "Position": {"PanTilt": {"x": 0.0, "y": 0.0}},
                        "Speed": {"PanTilt": {"x": speed, "y": speed}},
                    }
                )
            except Exception as exc:
                raise CameraError(friendly_error(exc)) from exc

    def position(self):
        with self._lock:
            try:
                status = self._ptz.GetStatus({"ProfileToken": self._token})
            except Exception as exc:
                raise CameraError(friendly_error(exc)) from exc
            position = _get(status, "Position")
            pan_tilt = _get(position, "PanTilt")
            return {
                "pan": float(_get(pan_tilt, "x", 0.0) or 0.0),
                "tilt": float(_get(pan_tilt, "y", 0.0) or 0.0),
            }

    def absolute(self, pan, tilt, zoom, speed):
        with self._lock:
            position = {"PanTilt": {"x": pan, "y": tilt}}
            move_speed = {"PanTilt": {"x": speed, "y": speed}}
            if zoom is not None:
                position["Zoom"] = {"x": zoom}
                move_speed["Zoom"] = {"x": speed}
            try:
                self._ptz.AbsoluteMove(
                    {"ProfileToken": self._token, "Position": position, "Speed": move_speed}
                )
            except Exception as exc:
                raise CameraError(friendly_error(exc)) from exc

    def presets(self):
        with self._lock:
            try:
                presets = self._ptz.GetPresets({"ProfileToken": self._token}) or []
            except Exception as exc:
                raise CameraError(friendly_error(exc)) from exc
            result = []
            for preset in presets:
                token = _token_of(preset)
                name = _get(preset, "Name") or _get(preset, "name") or ""
                if token:
                    result.append({"token": token, "name": str(name) or f"Preset {token}"})
            return result

    def goto_preset(self, token, speed=0.6):
        with self._lock:
            try:
                self._ptz.GotoPreset(
                    {
                        "ProfileToken": self._token,
                        "PresetToken": str(token),
                        "Speed": {
                            "PanTilt": {"x": speed, "y": speed},
                            "Zoom": {"x": speed},
                        },
                    }
                )
            except Exception as exc:
                raise CameraError(friendly_error(exc)) from exc

    def set_preset(self, name):
        with self._lock:
            try:
                result = self._ptz.SetPreset(
                    {"ProfileToken": self._token, "PresetName": name or ""}
                )
            except Exception as exc:
                raise CameraError(friendly_error(exc)) from exc
            return _token_of(result) if result is not None else None

    def remove_preset(self, token):
        with self._lock:
            try:
                self._ptz.RemovePreset({"ProfileToken": self._token, "PresetToken": str(token)})
            except Exception as exc:
                raise CameraError(friendly_error(exc)) from exc
