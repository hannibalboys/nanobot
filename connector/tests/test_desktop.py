"""Tests for the client-side desktop controller (add-connector-desktop-control)."""

from __future__ import annotations

import asyncio
import json

import pytest

from nanobot_connector.client import ConnectorClient
from nanobot_connector.config import ConnectorClientConfig
from nanobot_connector.desktop import DesktopController, DesktopError, ScreenFrame


class FakeCapture:
    def __init__(self, *, ok=True, width=800, height=600):
        self._ok = ok
        self._w = width
        self._h = height
        self.count = 0
        self.last_max_dimension = None

    def available(self):
        return self._ok

    def capture(self, *, max_dimension):
        self.count += 1
        self.last_max_dimension = max_dimension
        return ScreenFrame(image_b64="aW1n", width=self._w, height=self._h, fmt="png")


class FakeInput:
    def __init__(self, *, ok=True):
        self._ok = ok
        self.injected = []

    def available(self):
        return self._ok

    def inject(self, action):
        self.injected.append(action)


class ReasonCapture(FakeCapture):
    def unavailable_reason(self):
        return "缺少桌面截屏依赖"


class ReasonInput(FakeInput):
    def unavailable_reason(self):
        return "缺少键鼠控制依赖"


def _controller(**kwargs):
    return DesktopController(
        capture_backend=kwargs.pop("capture", FakeCapture()),
        input_backend=kwargs.pop("input", FakeInput()),
        **kwargs,
    )


async def _approve(operator, goal):
    return True


async def test_capture_refused_without_session():
    ctl = _controller(on_local_authorize=_approve)
    with pytest.raises(DesktopError) as ei:
        ctl.capture("s1")
    assert ei.value.code == "session_inactive"


async def test_input_refused_without_session():
    ctl = _controller(on_local_authorize=_approve)
    with pytest.raises(DesktopError) as ei:
        ctl.inject("s1", {"type": "click", "x": 1, "y": 1})
    assert ei.value.code == "session_inactive"


async def test_start_requires_local_authorization():
    ctl = _controller()  # no authorize hook → fail-closed
    with pytest.raises(DesktopError) as ei:
        await ctl.start_session("s1", operator="webui", goal="do it")
    assert ei.value.code == "desktop_unsupported"


async def test_start_denied_by_handler():
    async def deny(operator, goal):
        return False

    ctl = _controller(on_local_authorize=deny)
    with pytest.raises(DesktopError) as ei:
        await ctl.start_session("s1", operator="webui", goal="x")
    assert ei.value.code == "session_ended"
    assert not ctl.active


async def test_no_permission_refuses_start():
    ctl = _controller(capture=FakeCapture(ok=False), on_local_authorize=_approve)
    with pytest.raises(DesktopError) as ei:
        await ctl.start_session("s1", operator="webui", goal="x")
    assert ei.value.code == "no_permission"


async def test_start_reports_capture_backend_diagnostic():
    ctl = _controller(capture=ReasonCapture(ok=False), on_local_authorize=_approve)
    with pytest.raises(DesktopError, match="缺少桌面截屏依赖"):
        await ctl.start_session("s1", operator="webui", goal="x")


async def test_start_requires_input_backend():
    ctl = _controller(input=ReasonInput(ok=False), on_local_authorize=_approve)
    with pytest.raises(DesktopError, match="缺少键鼠控制依赖"):
        await ctl.start_session("s1", operator="webui", goal="x")


async def test_full_session_capture_and_inject():
    indicator = []
    ctl = _controller(on_local_authorize=_approve, on_indicator=indicator.append)
    await ctl.start_session("s1", operator="webui", goal="click login")
    assert ctl.active
    assert indicator[-1] is True  # capture indicator on

    frame = ctl.capture("s1")
    assert frame.width == 800 and frame.height == 600

    ctl.inject("s1", {"type": "click", "x": 100, "y": 200})
    assert ctl.input_backend.injected == [{"type": "click", "x": 100, "y": 200}]

    ctl.end_session()
    assert not ctl.active
    assert indicator[-1] is False  # indicator off after end


async def test_server_caps_clamp_capture_dimension():
    cap = FakeCapture()
    ctl = DesktopController(
        capture_backend=cap, input_backend=FakeInput(),
        max_dimension=1280, max_fps=4, on_local_authorize=_approve,
    )
    # server requests a tighter cap than the client's own → client uses the min
    await ctl.start_session("s1", operator="webui", goal="x", max_dimension=640, max_fps=1)
    ctl.capture("s1")
    assert cap.last_max_dimension == 640
    # a looser server cap does not exceed the client's own
    await ctl.start_session("s2", operator="webui", goal="x", max_dimension=4000)
    ctl.capture("s2")
    assert cap.last_max_dimension == 1280


async def test_out_of_bounds_rejected():
    ctl = _controller(on_local_authorize=_approve)
    await ctl.start_session("s1", operator="webui", goal="x")
    ctl.capture("s1")  # establishes screen size 800x600
    with pytest.raises(DesktopError) as ei:
        ctl.inject("s1", {"type": "click", "x": 9999, "y": 10})
    assert ei.value.code == "out_of_bounds"


async def test_unknown_action_rejected():
    ctl = _controller(on_local_authorize=_approve)
    await ctl.start_session("s1", operator="webui", goal="x")
    ctl.capture("s1")
    with pytest.raises(DesktopError):
        ctl.inject("s1", {"type": "format_disk"})


async def test_session_id_mismatch_rejected():
    ctl = _controller(on_local_authorize=_approve)
    await ctl.start_session("s1", operator="webui", goal="x")
    with pytest.raises(DesktopError) as ei:
        ctl.capture("other")
    assert ei.value.code == "session_ended"


# --- client dispatch -----------------------------------------------------


class FakeWS:
    def __init__(self):
        self.frames = []

    async def send(self, data):
        self.frames.append(json.loads(data))


def _client(desktop):
    cfg = ConnectorClientConfig(server="wss://h/connector/ws", device_token="t")
    return ConnectorClient(cfg, desktop=desktop)


async def _rpc(client, ws, rpc_id, method, params):
    await client._handle_rpc(ws, {"type": "rpc_request", "id": rpc_id, "method": method, "params": params})
    if client._exec_tasks:
        await asyncio.gather(*list(client._exec_tasks))


async def test_client_declares_desktop_capability():
    ctl = _controller(on_local_authorize=_approve)
    client = _client(ctl)

    class Rec:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            self.sent.append(json.loads(data))

    rec = Rec()
    await client._register(rec)
    assert "desktop" in rec.sent[0]["node"]["capabilities"]


async def test_client_desktop_session_flow():
    ctl = _controller(on_local_authorize=_approve)
    client = _client(ctl)
    ws = FakeWS()

    await _rpc(client, ws, "1", "desktop.session.start",
               {"sessionId": "s1", "operator": "webui", "goal": "log in"})
    assert ws.frames[-1]["result"]["started"] == "s1"

    await _rpc(client, ws, "2", "desktop.capture", {"sessionId": "s1"})
    cap = ws.frames[-1]["result"]
    assert cap["width"] == 800 and cap["image"] == "aW1n"

    await _rpc(client, ws, "3", "desktop.input",
               {"sessionId": "s1", "action": {"type": "click", "x": 10, "y": 20}})
    assert ws.frames[-1]["result"]["ok"] is True

    await _rpc(client, ws, "4", "desktop.session.end", {"sessionId": "s1"})
    assert ws.frames[-1]["result"]["ended"] is True


async def test_client_desktop_capture_without_session_errors():
    ctl = _controller(on_local_authorize=_approve)
    client = _client(ctl)
    ws = FakeWS()
    await _rpc(client, ws, "1", "desktop.capture", {"sessionId": "s1"})
    resp = ws.frames[-1]
    assert resp["ok"] is False
    assert resp["error"]["code"] == "session_inactive"


async def test_client_without_desktop_no_capability():
    cfg = ConnectorClientConfig(server="wss://h/connector/ws", device_token="t")
    client = ConnectorClient(cfg)  # desktop_enabled default False → no controller
    assert client.desktop is None

    class Rec:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            self.sent.append(json.loads(data))

    rec = Rec()
    await client._register(rec)
    assert "desktop" not in rec.sent[0]["node"]["capabilities"]
