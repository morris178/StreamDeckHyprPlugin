from __future__ import annotations

from types import SimpleNamespace
import sys
import unittest

# StreamController's globals module parses process arguments at import time. Keep
# unittest discovery flags away from that application-level parser.
_original_argv = sys.argv[:]
sys.argv = sys.argv[:1]
try:
    from actions.workspace import MoveFocusedWindowAction, WorkspaceAction
finally:
    sys.argv = _original_argv

from hyprland.models import WorkspaceTarget


class FakeWorkspaceService:
    def __init__(self):
        self.switched = []
        self.moved = []

    def switch_to_workspace(self, target):
        self.switched.append(target)

    def move_focused_window(self, target, follow=True):
        self.moved.append((target, follow))


class ActionGestureTests(unittest.TestCase):
    def setUp(self):
        self.target = WorkspaceTarget.parse("3")
        self.service = FakeWorkspaceService()
        self.action = SimpleNamespace(
            _target=self.target,
            plugin_base=SimpleNamespace(workspace_service=self.service),
            get_settings=lambda: {"follow_moved_window": True},
            show_error=lambda **_kwargs: None,
        )
        self.action._move_focused_window = lambda: WorkspaceAction._move_focused_window(self.action)

    def test_workspace_key_waits_for_resolved_gesture(self):
        WorkspaceAction.on_key_down(self.action, {})
        self.assertEqual((self.service.switched, self.service.moved), ([], []))

        WorkspaceAction.on_key_short_up(self.action, {})
        self.assertEqual(self.service.switched, [self.target])
        self.assertEqual(self.service.moved, [])

    def test_workspace_long_press_moves_and_follows(self):
        WorkspaceAction.on_key_hold_start(self.action, {})
        self.assertEqual(self.service.switched, [])
        self.assertEqual(self.service.moved, [(self.target, True)])

    def test_dedicated_move_action_uses_short_press(self):
        MoveFocusedWindowAction.on_key_short_up(self.action, {})
        self.assertEqual(self.service.moved, [(self.target, True)])

    def test_follow_can_be_disabled(self):
        self.action.get_settings = lambda: {"follow_moved_window": False}
        WorkspaceAction._move_focused_window(self.action)
        self.assertEqual(self.service.moved, [(self.target, False)])


if __name__ == "__main__":
    unittest.main()
