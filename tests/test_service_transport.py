from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from hyprland.backend import HyprlandBackend
from hyprland.models import Window, WorkspaceTarget
from hyprland.transport import FlatpakTransport, NativeTransport, select_transport
from services.workspace_service import WorkspaceService


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeBackend:
    def __init__(self, fail_first_stream: bool = False):
        self.fail_first_stream = fail_first_stream
        self.stream_calls = 0
        self.snapshot_calls = 0
        self.closed = False
        self.switched = []

    def snapshot(self):
        self.snapshot_calls += 1
        return {
            "clients": fixture("clients.json"),
            "workspaces": fixture("workspaces.json"),
            "monitors": fixture("monitors.json"),
            "status": {"configProvider": "lua"},
            "version": {"version": "0.56.2"},
        }

    def validate_current_hyprland(self, snapshot):
        return None

    def switch_to_workspace(self, target):
        self.switched.append(target)

    def events(self, stop_event):
        self.stream_calls += 1
        if self.fail_first_stream and self.stream_calls == 1:
            raise RuntimeError("socket disappeared")
        yield "test-signature", ""
        while not stop_event.wait(0.01):
            pass

    def close(self):
        self.closed = True


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.service = WorkspaceService(self.backend)
        self.service.resync()

    def tearDown(self):
        self.service.stop()

    def test_multiple_actions_for_same_workspace(self):
        target = WorkspaceTarget.parse("1")
        first, second = [], []
        self.service.subscribe(target, first.append)
        self.service.subscribe(target, second.append)
        first.clear()
        second.clear()
        self.service.process_event("openwindow>>ccc,1,kitty,Terminal")
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)

    def test_targeted_move_notification(self):
        one, two, three = [], [], []
        self.service.subscribe(WorkspaceTarget.parse("1"), one.append)
        self.service.subscribe(WorkspaceTarget.parse("code"), two.append)
        self.service.subscribe(WorkspaceTarget.parse("3"), three.append)
        one.clear(), two.clear(), three.clear()
        self.service.process_event("movewindowv2>>0xaaa,2,code")
        self.assertEqual((len(one), len(two), len(three)), (1, 1, 0))

    def test_unsubscribe_one_of_multiple_actions(self):
        target = WorkspaceTarget.parse("1")
        first, second = [], []
        self.service.subscribe(target, first.append)
        self.service.subscribe(target, second.append)
        self.service.unsubscribe(target, first.append)
        first.clear(), second.clear()
        self.service.process_event("openwindow>>ccc,1,kitty,Terminal")
        self.assertEqual((len(first), len(second)), (0, 1))

    def test_icon_update_notifies_only_workspaces_containing_app(self):
        one, two, three = [], [], []
        self.service.subscribe(WorkspaceTarget.parse("1"), one.append)
        self.service.subscribe(WorkspaceTarget.parse("code"), two.append)
        self.service.subscribe(WorkspaceTarget.parse("3"), three.append)
        self.service.process_event(
            "openwindow>>webapp,1,chrome-chatgpt.com__-Default,ChatGPT"
        )
        one.clear(), two.clear(), three.clear()

        self.service.notify_app_icon_changed(
            Window(
                "0xwebapp",
                1,
                "1",
                "chrome-chatgpt.com__-Default",
                "chrome-chatgpt.com__-Default",
                "ChatGPT",
            )
        )
        self.assertEqual((len(one), len(two), len(three)), (1, 0, 0))

    def test_switch_is_asynchronous_and_does_not_mutate_state(self):
        target = WorkspaceTarget.parse("3")
        before = self.service.state.view(target).visual_state
        self.service.switch_to_workspace(target)
        deadline = time.monotonic() + 1
        while not self.backend.switched and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.backend.switched, [target])
        self.assertEqual(self.service.state.view(target).visual_state, before)

    def test_socket_reconnect_resyncs(self):
        self.service.stop()
        backend = FakeBackend(fail_first_stream=True)
        service = WorkspaceService(backend)
        service.start()
        deadline = time.monotonic() + 2
        while not service.connected and time.monotonic() < deadline:
            time.sleep(0.02)
        service.stop()
        self.assertTrue(backend.closed)
        self.assertGreaterEqual(backend.stream_calls, 2)
        self.assertEqual(backend.snapshot_calls, 1)


class TransportTests(unittest.TestCase):
    def test_native_selection(self):
        transport = select_transport("/plugin/helper.py", environ={}, flatpak_info_path="/definitely/missing")
        self.assertIsInstance(transport, NativeTransport)

    def test_flatpak_selection(self):
        transport = select_transport(
            "/plugin/helper.py",
            environ={"FLATPAK_ID": "com.core447.StreamController"},
            flatpak_info_path="/definitely/missing",
        )
        self.assertIsInstance(transport, FlatpakTransport)
        self.assertEqual(transport._argv("events")[:4], ["flatpak-spawn", "--host", "--watch-bus", "python3"])

    def test_current_lua_dispatch_syntax_numeric_and_named(self):
        transport = NativeTransport(environ={})
        requests = []
        transport._request = lambda request: requests.append(request) or b"ok"  # type: ignore[method-assign]
        transport.dispatch_workspace("3")
        transport.dispatch_workspace('"name:Web"')
        self.assertEqual(requests[0], "/dispatch hl.dsp.focus({ workspace = 3 })")
        self.assertEqual(requests[1], '/dispatch hl.dsp.focus({ workspace = "name:Web" })')

    def test_backend_rejects_non_current_hyprland(self):
        backend = HyprlandBackend(NativeTransport(environ={}))
        with self.assertRaises(RuntimeError):
            backend.validate_current_hyprland(
                {"status": {"configProvider": "hyprlang"}, "version": {"version": "0.54.0"}}
            )


if __name__ == "__main__":
    unittest.main()
