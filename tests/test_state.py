from __future__ import annotations

import json
from pathlib import Path
import unittest

from hyprland.event_parser import parse_event_line
from hyprland.models import WorkspaceTarget, WorkspaceVisualState
from hyprland.state import WorkspaceState, monitor_from_json, window_from_json, workspace_from_json


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def populated_state() -> WorkspaceState:
    state = WorkspaceState()
    state.replace_snapshot(fixture("clients.json"), fixture("workspaces.json"), fixture("monitors.json"))
    return state


def event(line: str):
    parsed = parse_event_line(line)
    assert parsed is not None
    return parsed


class SnapshotParsingTests(unittest.TestCase):
    def test_client_fields(self):
        window = window_from_json(fixture("clients.json")[0])
        self.assertEqual(window.address, "0xaaa")
        self.assertEqual(window.initial_class, "firefox")
        self.assertEqual(window.position, (10, 20))
        self.assertEqual(window.size, (1200, 800))

    def test_workspace_fields(self):
        workspace = workspace_from_json(fixture("workspaces.json")[1])
        self.assertEqual((workspace.workspace_id, workspace.name, workspace.monitor), (2, "code", "DP-2"))

    def test_monitor_fields(self):
        monitor = monitor_from_json(fixture("monitors.json")[0])
        self.assertTrue(monitor.focused)
        self.assertEqual(monitor.active_workspace_id, 1)

    def test_snapshot_attaches_windows(self):
        state = populated_state()
        self.assertIn("0xaaa", state.workspaces[1].windows)
        self.assertIn("0xbbb", state.workspaces[2].windows)

    def test_resync_replaces_stale_state(self):
        state = populated_state()
        state.replace_snapshot([], fixture("workspaces.json")[:1], fixture("monitors.json")[:1])
        self.assertEqual(state.windows, {})
        self.assertEqual(set(state.workspaces), {1})


class EventTests(unittest.TestCase):
    def setUp(self):
        self.state = populated_state()

    def test_parser_preserves_commas_in_remainder(self):
        parsed = event("windowtitlev2>>0xaaa,One, Two")
        self.assertEqual(parsed.fields, ("0xaaa", "One", " Two"))

    def test_open_and_duplicate_open(self):
        change = self.state.apply(event("openwindow>>0xccc,1,kitty,Terminal"))
        self.assertIn("id:1", change.keys)
        self.assertIn("0xccc", self.state.workspaces[1].windows)
        self.state.apply(event("openwindow>>0xccc,1,kitty,Terminal"))
        self.assertEqual(list(self.state.windows).count("0xccc"), 1)

    def test_close_is_idempotent(self):
        # Current socket2 uses the bare hex address while clients -j uses 0x.
        self.state.apply(event("closewindow>>aaa"))
        second = self.state.apply(event("closewindow>>0xaaa"))
        self.assertNotIn("0xaaa", self.state.windows)
        self.assertEqual(second.keys, frozenset())

    def test_move_notifies_source_and_destination(self):
        change = self.state.apply(event("movewindowv2>>0xaaa,2,code"))
        self.assertEqual(change.keys, frozenset({"id:1", "name:1", "id:2", "name:code"}))
        self.assertIn("0xaaa", self.state.workspaces[2].windows)
        self.assertNotIn("0xaaa", self.state.workspaces[1].windows)

    def test_unknown_move_requests_resync(self):
        change = self.state.apply(event("movewindowv2>>0xmissing,2,code"))
        self.assertTrue(change.needs_resync)

    def test_title_and_focus_updates(self):
        title_change = self.state.apply(event("windowtitlev2>>0xaaa,New, title"))
        focus_change = self.state.apply(event("activewindowv2>>0xaaa"))
        self.assertEqual(self.state.windows["0xaaa"].title, "New, title")
        self.assertEqual(title_change.keys, frozenset())
        self.assertEqual(focus_change.keys, frozenset())

    def test_workspace_create_delete(self):
        self.state.apply(event("createworkspacev2>>4,chat"))
        self.assertEqual(self.state.workspaces[4].name, "chat")
        change = self.state.apply(event("destroyworkspacev2>>4,chat"))
        self.assertNotIn(4, self.state.workspaces)
        self.assertIn("name:chat", change.keys)

    def test_workspace_focus_changes_active_monitor_workspace(self):
        change = self.state.apply(event("workspacev2>>3,3"))
        self.assertEqual(self.state.monitors["DP-1"].active_workspace_id, 3)
        self.assertIn("id:1", change.keys)
        self.assertIn("id:3", change.keys)

    def test_focused_monitor_changes_focused_vs_visible(self):
        self.state.apply(event("focusedmonv2>>DP-2,2"))
        one = self.state.view(WorkspaceTarget.parse("1"))
        two = self.state.view(WorkspaceTarget.parse("code"))
        self.assertEqual(one.visual_state, WorkspaceVisualState.VISIBLE)
        self.assertEqual(two.visual_state, WorkspaceVisualState.FOCUSED)

    def test_multi_monitor_initial_semantics(self):
        self.assertEqual(
            self.state.view(WorkspaceTarget.parse("1")).visual_state,
            WorkspaceVisualState.FOCUSED,
        )
        self.assertEqual(
            self.state.view(WorkspaceTarget.parse("name:code")).visual_state,
            WorkspaceVisualState.VISIBLE,
        )
        self.assertEqual(
            self.state.view(WorkspaceTarget.parse("3")).visual_state,
            WorkspaceVisualState.INACTIVE,
        )

    def test_unknown_event_is_ignored(self):
        generation = self.state.generation
        change = self.state.apply(event("futureevent>>a,b"))
        self.assertEqual(change.keys, frozenset())
        self.assertEqual(self.state.generation, generation)

    def test_recorded_current_event_stream_is_safe(self):
        lines = (FIXTURES / "events.txt").read_text(encoding="utf-8").splitlines()
        for line in lines:
            parsed = parse_event_line(line)
            self.assertIsNotNone(parsed)
            self.state.apply(parsed)
        self.assertNotIn("0xccc", self.state.windows)


class WorkspaceTargetTests(unittest.TestCase):
    def test_numeric_and_named_lua_selectors(self):
        self.assertEqual(WorkspaceTarget.parse("3").lua_selector(), "3")
        self.assertEqual(WorkspaceTarget.parse("name:Web Apps").lua_selector(), '"name:Web Apps"')

    def test_invalid_targets(self):
        for value in ("", "0", "2147483648", "special:music"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                WorkspaceTarget.parse(value)


if __name__ == "__main__":
    unittest.main()
