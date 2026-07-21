"""Client-side controlled desktop control (add-connector-desktop-control).

Screen capture + keyboard/mouse injection, allowed ONLY inside a controlled
session the device owner explicitly authorized on this machine. This is the
highest-risk connector capability, so the safety rules live here on the device:

- **session-gated**: capture/input are refused unless a session is active.
- **local authorization**: starting a session requires the owner to approve on
  this machine (fail-closed: no handler ⇒ denied).
- **bounds-checked**: injected coordinates must fall inside the captured screen.
- **capture indicator**: a hook fires while capturing so the UI can show it.
- **no OS-permission bypass**: missing screen-recording/accessibility grants
  degrade gracefully with guidance; nothing is forced.

Capture and input backends are injectable so the lifecycle/security logic is
testable without real OS automation; the real backends (mss / pynput) are
lazy-imported only when a session actually runs.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from nanobot_connector import protocol as proto


class DesktopError(Exception):
    """Carries a stable protocol error ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ScreenFrame:
    image_b64: str  # base64 PNG/JPEG
    width: int
    height: int
    fmt: str = "png"


class CaptureBackend(Protocol):
    def available(self) -> bool: ...  # OS permission present  # pragma: no cover
    def capture(self, *, max_dimension: int) -> ScreenFrame: ...  # pragma: no cover


class InputBackend(Protocol):
    def available(self) -> bool: ...  # pragma: no cover
    def inject(self, action: dict) -> None: ...  # pragma: no cover


# on_indicator(active) -> None : toggle the on-device "capturing" indicator.
IndicatorHook = Callable[[bool], None]
# on_local_authorize(operator, goal) -> awaitable[bool]
SessionAuthorizeHook = Callable[[str, str], Awaitable[bool]]


def _clamp_bounds(action: dict, width: int, height: int) -> None:
    """Reject actions whose coordinates fall outside the captured screen."""
    for x_key, y_key in (("x", "y"), ("to_x", "to_y")):
        if x_key in action or y_key in action:
            x = action.get(x_key)
            y = action.get(y_key)
            if x is not None and not (0 <= int(x) < width):
                raise DesktopError(proto.ERROR_OUT_OF_BOUNDS, f"{x_key}={x} outside 0..{width}")
            if y is not None and not (0 <= int(y) < height):
                raise DesktopError(proto.ERROR_OUT_OF_BOUNDS, f"{y_key}={y} outside 0..{height}")


@dataclass
class DesktopController:
    """One device's desktop control: at most one active session at a time."""

    capture_backend: CaptureBackend
    input_backend: InputBackend
    max_fps: int = 2
    max_dimension: int = 1280
    on_indicator: IndicatorHook | None = None
    on_local_authorize: SessionAuthorizeHook | None = None
    _session_id: str | None = field(default=None, init=False)
    _last_capture_ts: float = field(default=0.0, init=False)
    _last_size: tuple[int, int] = field(default=(0, 0), init=False)
    # Effective caps for the current session = min(local, server-requested).
    _eff_dimension: int = field(default=0, init=False)
    _eff_fps: int = field(default=0, init=False)

    @property
    def active(self) -> bool:
        return self._session_id is not None

    async def start_session(
        self, session_id: str, *, operator: str, goal: str,
        max_dimension: int | None = None, max_fps: int | None = None,
    ) -> None:
        """Open a controlled session after local owner authorization.

        Fail-closed: with no authorize hook, a session is refused. Missing OS
        capture permission also refuses (with guidance) rather than half-open.
        Server-requested caps are clamped against the device's own limits.
        """
        if not self.capture_backend.available():
            raise DesktopError(
                proto.ERROR_NO_PERMISSION,
                "screen recording / accessibility permission is not granted on this device",
            )
        if self.on_local_authorize is None:
            raise DesktopError(proto.ERROR_DESKTOP_UNSUPPORTED, "no local authorization handler")
        approved = False
        try:
            approved = bool(await self.on_local_authorize(operator, goal))
        except Exception:  # noqa: BLE001 - a broken handler must not auto-approve
            approved = False
        if not approved:
            raise DesktopError(proto.ERROR_SESSION_ENDED, "session not authorized on device")
        self._eff_dimension = min(self.max_dimension, max_dimension) if max_dimension else self.max_dimension
        self._eff_fps = min(self.max_fps, max_fps) if max_fps else self.max_fps
        self._session_id = session_id
        self._set_indicator(True)

    def end_session(self) -> None:
        self._session_id = None
        self._set_indicator(False)

    def capture(self, session_id: str) -> ScreenFrame:
        self._require_session(session_id)
        fps = self._eff_fps or self.max_fps
        dimension = self._eff_dimension or self.max_dimension
        # Simple frame-rate cap: refuse captures faster than the effective fps.
        now = time.monotonic()
        min_interval = 1.0 / max(1, fps)
        if self._last_capture_ts and (now - self._last_capture_ts) < min_interval:
            time.sleep(min_interval - (now - self._last_capture_ts))
        frame = self.capture_backend.capture(max_dimension=dimension)
        self._last_capture_ts = time.monotonic()
        self._last_size = (frame.width, frame.height)
        return frame

    def inject(self, session_id: str, action: dict) -> None:
        self._require_session(session_id)
        atype = action.get("type", "")
        if atype not in proto.DESKTOP_ACTIONS:
            raise DesktopError(proto.ERROR_OUT_OF_BOUNDS, f"unknown action type: {atype}")
        if not self.input_backend.available():
            raise DesktopError(proto.ERROR_NO_PERMISSION, "input injection permission not granted")
        width, height = self._last_size
        if width and height:
            _clamp_bounds(action, width, height)
        self.input_backend.inject(action)

    def _require_session(self, session_id: str) -> None:
        if self._session_id is None:
            raise DesktopError(proto.ERROR_SESSION_INACTIVE, "no active desktop session")
        if session_id != self._session_id:
            raise DesktopError(proto.ERROR_SESSION_ENDED, "session id mismatch or ended")

    def _set_indicator(self, active: bool) -> None:
        if self.on_indicator is not None:
            try:
                self.on_indicator(active)
            except Exception:  # noqa: BLE001 - indicator is best-effort
                pass


# --- real backends (lazy) ------------------------------------------------


class _MssCapture:
    """Real screen capture via ``mss`` + ``Pillow`` (lazy-imported)."""

    def available(self) -> bool:
        try:
            import mss  # noqa: F401
            import PIL  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        return True

    def capture(self, *, max_dimension: int) -> ScreenFrame:  # pragma: no cover - needs a display
        import base64
        import io

        import mss
        from PIL import Image

        with mss.mss() as sct:
            shot = sct.grab(sct.monitors[0])
            img = Image.frombytes("RGB", shot.size, shot.rgb)
        w, h = img.size
        scale = min(1.0, max_dimension / max(w, h))
        if scale < 1.0:
            img = img.resize((int(w * scale), int(h * scale)))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return ScreenFrame(
            image_b64=base64.b64encode(buf.getvalue()).decode(),
            width=img.size[0], height=img.size[1], fmt="png",
        )


class _PynputInput:
    """Real input injection via ``pynput`` (lazy-imported)."""

    def available(self) -> bool:
        try:
            import pynput  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        return True

    def inject(self, action: dict) -> None:  # pragma: no cover - needs a desktop
        from pynput import keyboard
        from pynput import mouse as mouse_mod

        atype = action.get("type")
        if atype in ("click", "double_click", "right_click", "move", "drag"):
            mouse = mouse_mod.Controller()
            if "x" in action and "y" in action:
                mouse.position = (int(action["x"]), int(action["y"]))
            if atype == "move":
                return
            button = mouse_mod.Button.right if atype == "right_click" else mouse_mod.Button.left
            if atype == "drag" and "to_x" in action:
                mouse.press(button)
                mouse.position = (int(action["to_x"]), int(action["to_y"]))
                mouse.release(button)
            else:
                mouse.click(button, 2 if atype == "double_click" else 1)
        elif atype in ("type", "key"):
            keyboard.Controller().type(str(action.get("text", "")))
        elif atype == "scroll":
            mouse_mod.Controller().scroll(int(action.get("dx", 0)), int(action.get("dy", 0)))


def default_controller(**kwargs: Any) -> DesktopController:
    """A controller wired to the real OS backends."""
    return DesktopController(
        capture_backend=_MssCapture(), input_backend=_PynputInput(), **kwargs
    )
