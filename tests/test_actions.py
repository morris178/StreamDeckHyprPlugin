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

from hyprland.models import Window, WorkspaceTarget, WorkspaceView, WorkspaceVisualState


class FakeWorkspaceService:
    def __init__(self):
        self.switched = []
        self.moved = []
        self.views = {}
        self.subscribers = {}

    def subscribe(self, target, callback):
        self.subscribers.setdefault(target.key, set()).add(callback)
        view = self.views.get(target.key)
        if view is not None:
            callback(view)

    def unsubscribe(self, target, callback):
        callbacks = self.subscribers.get(target.key)
        if callbacks is None:
            return
        callbacks.discard(callback)
        if not callbacks:
            self.subscribers.pop(target.key, None)

    def publish(self, view):
        self.views[view.target.key] = view
        for callback in tuple(self.subscribers.get(view.target.key, ())):
            callback(view)

    def subscriber_count(self, target):
        return len(self.subscribers.get(target.key, ()))

    def switch_to_workspace(self, target):
        self.switched.append(target)

    def move_focused_window(self, target, follow=True):
        self.moved.append((target, follow))


class FakeRenderScheduler:
    def __init__(self):
        self.scheduled = []
        self.cancelled = []

    def schedule(self, owner, view, style, callback):
        self.scheduled.append((owner, view, style, callback))

    def cancel(self, owner):
        self.cancelled.append(owner)


def make_view(target, *app_classes):
    windows = tuple(
        Window(
            address=f"0x{index}",
            workspace_id=target.workspace_id or -1,
            workspace_name=target.display_name,
            app_class=app_class,
            initial_class=app_class,
            title=app_class,
        )
        for index, app_class in enumerate(app_classes, start=1)
    )
    return WorkspaceView(
        target=target,
        workspace_id=target.workspace_id,
        name=target.display_name,
        monitor="DP-1",
        visual_state=WorkspaceVisualState.INACTIVE,
        windows=windows,
    )


def make_cached_action(service, scheduler):
    action = WorkspaceAction.__new__(WorkspaceAction)
    action._target = None
    action._last_view = None
    action._subscribed = False
    action._render_style = object()
    action.on_ready_called = True
    action.plugin_base = SimpleNamespace(
        workspace_service=service,
        render_scheduler=scheduler,
    )
    action.get_settings = lambda: {"workspace": "3"}
    action.show_error = lambda **_kwargs: None

    # ActionCore.on_disconnect() expects these lifecycle fields even when the
    # action has never launched a per-action backend.
    action.server = None
    action.backend_connection = None
    action.backend_process = None
    action.backend = None
    action.backend_launch_pending = False
    return action


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


class PageCachingTests(unittest.TestCase):
    def setUp(self):
        self.target = WorkspaceTarget.parse("3")
        self.initial_view = make_view(self.target, "kitty")
        self.service = FakeWorkspaceService()
        self.service.views[self.target.key] = self.initial_view
        self.scheduler = FakeRenderScheduler()

    def make_action(self):
        action = make_cached_action(self.service, self.scheduler)
        action._change_target("3")
        return action

    def test_cached_page_return_forces_render_without_state_change(self):
        action = self.make_action()
        self.scheduler.scheduled.clear()

        action.on_update()

        self.assertEqual(len(self.scheduler.scheduled), 1)
        self.assertIs(self.scheduler.scheduled[0][1], self.initial_view)

    def test_cached_page_return_uses_state_received_while_hidden(self):
        action = self.make_action()
        updated_view = make_view(self.target, "kitty", "firefox")

        self.service.publish(updated_view)
        self.scheduler.scheduled.clear()
        action.on_update()

        self.assertIs(action._last_view, updated_view)
        self.assertEqual(len(self.scheduler.scheduled), 1)
        self.assertIs(self.scheduler.scheduled[0][1], updated_view)

    def test_cache_eviction_unsubscribes_and_cancels_render(self):
        action = self.make_action()
        self.assertEqual(self.service.subscriber_count(self.target), 1)

        action.on_removed_from_cache()

        self.assertEqual(self.service.subscriber_count(self.target), 0)
        self.assertFalse(action._subscribed)
        self.assertEqual(self.scheduler.cancelled, [action])

    def test_eviction_keeps_second_action_subscribed(self):
        first = self.make_action()
        second = self.make_action()
        first_view = first._last_view
        self.assertEqual(self.service.subscriber_count(self.target), 2)

        first.on_removed_from_cache()
        updated_view = make_view(self.target, "firefox")
        self.service.publish(updated_view)

        self.assertEqual(self.service.subscriber_count(self.target), 1)
        self.assertIs(first._last_view, first_view)
        self.assertIs(second._last_view, updated_view)


if __name__ == "__main__":
    unittest.main()
