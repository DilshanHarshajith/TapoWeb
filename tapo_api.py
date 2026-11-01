import re
import threading
import time

try:
    from pytapo import Tapo
except ImportError:
    Tapo = None


class TapoError(Exception):
    pass


def friendly_tapo_error(exc):
    text = str(exc)
    lowered = text.lower()
    if "temporary suspension" in lowered:
        seconds = 1800
        match = re.search(r"try again in (\d+) seconds", text, re.IGNORECASE)
        if match:
            seconds = int(match.group(1))
        minutes = max(1, round(seconds / 60))
        return f"Tapo login is temporarily locked by the camera. Try again in about {minutes} minutes."
    if "invalid authentication" in lowered or "authority" in lowered:
        return "Wrong Tapo account password. Use the password you sign in to the Tapo app with."
    if "not supported" in lowered or "does not support" in lowered:
        return "This camera does not support that control over the local API."
    if "timed out" in lowered or "timeout" in lowered:
        return "Camera did not respond."
    if "connection" in lowered or "unreachable" in lowered:
        return "Could not reach the camera's local API."
    return text or exc.__class__.__name__


def _suspension_seconds(text):
    match = re.search(r"try again in (\d+) seconds", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return 1800


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("on", "1", "true", "yes")
    if isinstance(value, dict):
        for key in ("status", "switch", "state", "enabled", "on", "force_wtl_state"):
            if key in value:
                result = _as_bool(value[key])
                if result is not None:
                    return result
        for item in value.values():
            if isinstance(item, (dict, bool, int, str)):
                result = _as_bool(item)
                if result is not None:
                    return result
    return None


class TapoLight:
    def __init__(self, host, cloud_password):
        self.host = host
        self.cloud_password = cloud_password
        self._lock = threading.RLock()
        self._client = None
        self._suspended_until = 0.0
        self._spotlight_mode = None

    def _client_or_raise(self):
        if Tapo is None:
            raise TapoError("pytapo is not installed")
        now = time.monotonic()
        if now < self._suspended_until:
            seconds = int(self._suspended_until - now)
            minutes = max(1, round(seconds / 60))
            raise TapoError(
                f"Tapo login is temporarily locked by the camera. Try again in about {minutes} minutes."
            )
        if self._client is None:
            try:
                self._client = Tapo(
                    self.host,
                    "admin",
                    self.cloud_password,
                    printWarnInformation=False,
                )
            except Exception as exc:
                text = str(exc)
                if "temporary suspension" in text.lower():
                    self._suspended_until = time.monotonic() + _suspension_seconds(text)
                raise TapoError(friendly_tapo_error(exc)) from exc
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
                raise TapoError(friendly_tapo_error(exc)) from exc

    def _spotlight_state(self, client):
        errors = []
        for mode, getter in (
            ("status", getattr(client, "getWhitelampStatus", None)),
            ("force", getattr(client, "getForceWhitelampState", None)),
        ):
            if getter is None:
                continue
            try:
                value = getter()
            except Exception as exc:
                errors.append(exc)
                continue
            state = _as_bool(value)
            if state is not None:
                self._spotlight_mode = mode
                return state
        if errors:
            raise errors[-1]
        return None

    def state(self):
        def action(client):
            result = {"on": None, "mode": None}
            errors = []
            try:
                result["on"] = self._spotlight_state(client)
            except Exception as exc:
                errors.append(exc)
            try:
                result["mode"] = client.getDayNightMode()
            except Exception as exc:
                errors.append(exc)
            if result["on"] is None and result["mode"] is None:
                if errors:
                    raise errors[-1]
                raise TapoError("Camera did not return spotlight or night mode state")
            return result

        return self._call(action)

    def set_spotlight(self, on):
        def action(client):
            current = self._spotlight_state(client)
            if current is not None and current == on:
                return {"on": on}
            if self._spotlight_mode == "status":
                client.reverseWhitelampStatus()
                return {"on": on}
            if self._spotlight_mode == "force":
                client.setForceWhitelampState(on)
                return {"on": on}
            errors = []
            try:
                client.setForceWhitelampState(on)
                self._spotlight_mode = "force"
                return {"on": on}
            except Exception as exc:
                errors.append(exc)
            try:
                client.reverseWhitelampStatus()
                self._spotlight_mode = "status"
                return {"on": on}
            except Exception as exc:
                errors.append(exc)
            raise errors[-1]

        return self._call(action)

    def set_night_mode(self, mode):
        if mode not in ("auto", "on", "off"):
            raise TapoError("Night mode must be auto, day or night")

        def action(client):
            client.setDayNightMode(mode)
            return {"mode": mode}

        return self._call(action)
