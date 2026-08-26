from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import logging
import random
import threading
from typing import TypeAlias

try:
    from ..hyprland.backend import HyprlandBackend
    from ..hyprland.event_parser import parse_event_line
    from ..hyprland.models import Window, WorkspaceTarget, WorkspaceView
    from ..hyprland.state import WorkspaceState
except ImportError:  # Direct source-tree test import.
    from hyprland.backend import HyprlandBackend
    from hyprland.event_parser import parse_event_line
    from hyprland.models import Window, WorkspaceTarget, WorkspaceView
    from hyprland.state import WorkspaceState


Subscriber: TypeAlias = Callable[[WorkspaceView], None]


class WorkspaceService:
    """One event-driven Hyprland service shared by every Workspace action."""

    def __init__(self, backend: HyprlandBackend, logger: logging.Logger | None = None):
        self.backend = backend
        self.state = WorkspaceState()
        self.log = logger or logging.getLogger(__name__)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._subscribers: dict[str, set[Subscriber]] = {}
        self._targets: dict[str, WorkspaceTarget] = {}
        self._connected = False
        self._error: str | None = "Connecting to Hyprland"
        self._instance_signature = ""
        self._commands = ThreadPoolExecutor(max_workers=1, thread_name_prefix="HyprlandCommand")

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def instance_signature(self) -> str:
        with self._lock:
            return self._instance_signature

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, name="HyprlandWorkspaceService", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self.backend.close()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        self._commands.shutdown(wait=True, cancel_futures=True)

    def subscribe(self, target: WorkspaceTarget, callback: Subscriber) -> None:
        with self._lock:
            self._targets[target.key] = target
            self._subscribers.setdefault(target.key, set()).add(callback)
            view = self.state.view(target, self._connected, self._error)
        callback(view)

    def unsubscribe(self, target: WorkspaceTarget, callback: Subscriber) -> None:
        with self._lock:
            callbacks = self._subscribers.get(target.key)
            if callbacks is None:
                return
            callbacks.discard(callback)
            if not callbacks:
                self._subscribers.pop(target.key, None)
                self._targets.pop(target.key, None)

    def switch_to_workspace(self, target: WorkspaceTarget) -> None:
        """Dispatch asynchronously; Hyprland events remain the source of truth."""

        self._submit_workspace_command(
            target,
            operation="switch to",
            command=lambda: self.backend.switch_to_workspace(target),
        )

    def move_focused_window(self, target: WorkspaceTarget, follow: bool = True) -> None:
        """Move Hyprland's focused window; resulting events update workspace state."""

        self._submit_workspace_command(
            target,
            operation="move focused window to",
            command=lambda: self.backend.move_focused_window(target, follow=follow),
        )

    def _submit_workspace_command(
        self,
        target: WorkspaceTarget,
        operation: str,
        command: Callable[[], None],
    ) -> None:
        """Serialize IPC commands without ever blocking an action callback."""

        def run_command() -> None:
            try:
                command()
            except Exception as exc:
                self.log.warning(f"Could not {operation} workspace {target.display_name}: {exc}")
                with self._lock:
                    self._error = str(exc)
                self._notify_keys({target.key})

        try:
            self._commands.submit(run_command)
        except RuntimeError:
            # The service is already shutting down.
            return

    def notify_app_icon_changed(self, window: Window) -> None:
        """Target only configured workspaces containing the newly resolved app icon."""
        wanted = self._window_app_tokens(window)
        if not wanted:
            return
        changed: set[str] = set()
        with self._lock:
            for key, target in self._targets.items():
                view = self.state.view(target, self._connected, self._error)
                if any(wanted & self._window_app_tokens(candidate) for candidate in view.windows):
                    changed.add(key)
        if changed:
            self._notify_keys(changed)

    @staticmethod
    def _window_app_tokens(window: Window) -> set[str]:
        return {
            "".join(character for character in value.casefold() if character.isalnum())
            for value in (window.initial_class, window.app_class)
            if value
        } - {""}

    def resync(self) -> set[str]:
        snapshot = self.backend.snapshot()
        self.backend.validate_current_hyprland(snapshot)
        changed = self.state.replace_snapshot(
            snapshot.get("clients") or [],
            snapshot.get("workspaces") or [],
            snapshot.get("monitors") or [],
        )
        with self._lock:
            self._connected = True
            self._error = None
        return changed

    def process_event(self, raw_line: str) -> set[str]:
        event = parse_event_line(raw_line)
        if event is None:
            return set()
        with self._lock:
            had_error = self._error is not None
            self._error = None
        change = self.state.apply(event)
        changed = set(change.keys)
        if had_error:
            changed.update(self._configured_keys())
        if change.needs_resync:
            changed.update(self.resync())
        if changed:
            self._notify_keys(changed)
        return changed

    def _run(self) -> None:
        backoff = 0.5
        while not self._stop_event.is_set():
            try:
                for signature, raw_line in self.backend.events(self._stop_event):
                    if self._stop_event.is_set():
                        return
                    if signature != self.instance_signature:
                        with self._lock:
                            self._instance_signature = signature
                        changed = self.resync()
                        self._notify_keys(changed | self._configured_keys())
                        self.log.info(f"Connected to Hyprland instance {signature}")
                        backoff = 0.5
                    if raw_line:
                        self.process_event(raw_line)
                if not self._stop_event.is_set():
                    raise RuntimeError("Hyprland event stream ended")
            except Exception as exc:
                if self._stop_event.is_set():
                    return
                message = str(exc)
                with self._lock:
                    was_connected = self._connected
                    self._connected = False
                    self._error = message
                    self._instance_signature = ""
                if was_connected:
                    self.log.warning(f"Hyprland connection lost: {message}")
                else:
                    self.log.debug(f"Hyprland unavailable: {message}")
                self._notify_keys(self._configured_keys())
                delay = min(30.0, backoff) * random.uniform(0.9, 1.1)
                self._stop_event.wait(delay)
                backoff = min(30.0, backoff * 2.0)

    def _configured_keys(self) -> set[str]:
        with self._lock:
            return set(self._subscribers)

    def _notify_keys(self, keys: set[str]) -> None:
        deliveries: list[tuple[Subscriber, WorkspaceView]] = []
        with self._lock:
            for key in keys:
                target = self._targets.get(key)
                if target is None:
                    continue
                view = self.state.view(target, self._connected, self._error)
                deliveries.extend((callback, view) for callback in tuple(self._subscribers.get(key, ())))
        for callback, view in deliveries:
            try:
                callback(view)
            except Exception:
                self.log.exception(f"Workspace subscriber failed for {view.target.key}")
