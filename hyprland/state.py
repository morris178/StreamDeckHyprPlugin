from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from typing import Iterable

from .event_parser import HyprlandEvent, int_field
from .models import (
    Monitor,
    Window,
    Workspace,
    WorkspaceTarget,
    WorkspaceView,
    WorkspaceVisualState,
    normalize_address,
)


def _pair(value: object) -> tuple[int, int]:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError):
            pass
    return 0, 0


def window_from_json(client: dict) -> Window | None:
    address = normalize_address(client.get("address", ""))
    workspace = client.get("workspace") or {}
    workspace_id = int_field(workspace.get("id", 0))
    workspace_name = str(workspace.get("name", ""))
    if not address or workspace_id == 0:
        return None
    return Window(
        address=address,
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        app_class=str(client.get("class", "")),
        initial_class=str(client.get("initialClass", "")),
        title=str(client.get("title", "")),
        position=_pair(client.get("at")),
        size=_pair(client.get("size")),
        focus_history_id=int_field(client.get("focusHistoryID", -1), -1),
    )


def workspace_from_json(item: dict) -> Workspace | None:
    workspace_id = int_field(item.get("id", 0))
    name = str(item.get("name", ""))
    if workspace_id == 0 or not name:
        return None
    return Workspace(workspace_id=workspace_id, name=name, monitor=str(item.get("monitor", "")))


def monitor_from_json(item: dict) -> Monitor | None:
    name = str(item.get("name", ""))
    if not name:
        return None
    active = item.get("activeWorkspace") or {}
    return Monitor(
        monitor_id=int_field(item.get("id", -1), -1),
        name=name,
        active_workspace_id=int_field(active.get("id", 0)),
        active_workspace_name=str(active.get("name", "")),
        focused=bool(item.get("focused", False)),
        position=(int_field(item.get("x", 0)), int_field(item.get("y", 0))),
        size=(int_field(item.get("width", 0)), int_field(item.get("height", 0))),
        scale=float(item.get("scale", 1.0) or 1.0),
    )


@dataclass(frozen=True, slots=True)
class StateChange:
    keys: frozenset[str]
    needs_resync: bool = False


class WorkspaceState:
    """Thread-safe, transport-independent projection of Hyprland state."""

    def __init__(self) -> None:
        self._lock = RLock()
        self.workspaces: dict[int, Workspace] = {}
        self.monitors: dict[str, Monitor] = {}
        self.windows: dict[str, Window] = {}
        self.focused_window: str = ""
        self.generation = 0

    @staticmethod
    def _keys_for(workspace: Workspace | None) -> set[str]:
        if workspace is None:
            return set()
        return {f"id:{workspace.workspace_id}", f"name:{workspace.name}"}

    def all_keys(self) -> set[str]:
        with self._lock:
            keys: set[str] = set()
            for workspace in self.workspaces.values():
                keys.update(self._keys_for(workspace))
            return keys

    def replace_snapshot(
        self,
        clients: Iterable[dict],
        workspaces: Iterable[dict],
        monitors: Iterable[dict],
    ) -> set[str]:
        parsed_workspaces = {
            workspace.workspace_id: workspace
            for item in workspaces
            if (workspace := workspace_from_json(item)) is not None
        }
        parsed_monitors = {
            monitor.name: monitor
            for item in monitors
            if (monitor := monitor_from_json(item)) is not None
        }
        parsed_windows: dict[str, Window] = {}
        for item in clients:
            window = window_from_json(item)
            if window is None:
                continue
            parsed_windows[window.address] = window
            workspace = parsed_workspaces.get(window.workspace_id)
            if workspace is None:
                workspace = Workspace(window.workspace_id, window.workspace_name)
                parsed_workspaces[window.workspace_id] = workspace
            workspace.windows[window.address] = window

        with self._lock:
            changed = self.all_keys()
            self.workspaces = parsed_workspaces
            self.monitors = parsed_monitors
            self.windows = parsed_windows
            self.generation += 1
            changed.update(self.all_keys())
            return changed

    def view(self, target: WorkspaceTarget, connected: bool = True, error: str | None = None) -> WorkspaceView:
        with self._lock:
            workspace = None
            if target.workspace_id is not None:
                workspace = self.workspaces.get(target.workspace_id)
            else:
                workspace = next((item for item in self.workspaces.values() if item.name == target.name), None)

            if workspace is None:
                return WorkspaceView(
                    target=target,
                    workspace_id=target.workspace_id,
                    name=target.display_name,
                    monitor="",
                    visual_state=WorkspaceVisualState.INACTIVE,
                    windows=(),
                    connected=connected,
                    error=error,
                )

            visible_monitors = [
                monitor
                for monitor in self.monitors.values()
                if monitor.active_workspace_id == workspace.workspace_id
            ]
            if any(monitor.focused for monitor in visible_monitors):
                visual_state = WorkspaceVisualState.FOCUSED
            elif visible_monitors:
                visual_state = WorkspaceVisualState.VISIBLE
            else:
                visual_state = WorkspaceVisualState.INACTIVE
            windows = tuple(
                sorted(
                    (deepcopy(window) for window in workspace.windows.values()),
                    key=lambda window: (window.focus_history_id < 0, window.focus_history_id, window.address),
                )
            )
            return WorkspaceView(
                target=target,
                workspace_id=workspace.workspace_id,
                name=workspace.name,
                monitor=workspace.monitor,
                visual_state=visual_state,
                windows=windows,
                connected=connected,
                error=error,
            )

    def apply(self, event: HyprlandEvent) -> StateChange:
        with self._lock:
            change = self._apply_locked(event)
            if change.keys:
                self.generation += 1
            return change

    def _apply_locked(self, event: HyprlandEvent) -> StateChange:
        name, fields = event.name, event.fields

        if name == "openwindow" and len(fields) >= 4:
            address, workspace_name, app_class = fields[:3]
            address = normalize_address(address)
            title = ",".join(fields[3:])
            workspace = self._workspace_by_name(workspace_name)
            if workspace is None:
                return StateChange(frozenset(), needs_resync=True)
            old = self.windows.get(address)
            if old is not None:
                old_workspace = self.workspaces.get(old.workspace_id)
                if old_workspace:
                    old_workspace.windows.pop(address, None)
            window = Window(address, workspace.workspace_id, workspace.name, app_class, app_class, title)
            self.windows[address] = window
            workspace.windows[address] = window
            keys = self._keys_for(workspace)
            if old is not None:
                keys.update(self._keys_for(self.workspaces.get(old.workspace_id)))
            return StateChange(frozenset(keys))

        if name in {"closewindow", "kill"} and fields:
            window = self.windows.pop(normalize_address(fields[0]), None)
            if window is None:
                return StateChange(frozenset())
            workspace = self.workspaces.get(window.workspace_id)
            if workspace:
                workspace.windows.pop(window.address, None)
            return StateChange(frozenset(self._keys_for(workspace)))

        if name == "movewindowv2" and len(fields) >= 3:
            return self._move_window(normalize_address(fields[0]), int_field(fields[1]), fields[2])
        if name == "movewindow" and len(fields) >= 2:
            workspace = self._workspace_by_name(fields[1])
            if workspace is None:
                return StateChange(frozenset(), needs_resync=True)
            return self._move_window(normalize_address(fields[0]), workspace.workspace_id, workspace.name)

        if name == "windowtitlev2" and len(fields) >= 2:
            window = self.windows.get(normalize_address(fields[0]))
            if window is None:
                # A final title event may race with closewindow.
                return StateChange(frozenset())
            new_title = ",".join(fields[1:])
            if window.title == new_title:
                return StateChange(frozenset())
            window.title = new_title
            # Titles are retained for diagnostics/layout previews, but the v1 icon
            # renderer does not use them, so a title-only update needs no redraw.
            return StateChange(frozenset())

        if name == "activewindowv2" and fields:
            address = normalize_address(fields[0])
            if address == self.focused_window:
                return StateChange(frozenset())
            keys: set[str] = set()
            old_window = self.windows.get(self.focused_window)
            new_window = self.windows.get(address)
            if old_window:
                keys.update(self._keys_for(self.workspaces.get(old_window.workspace_id)))
            if new_window:
                keys.update(self._keys_for(self.workspaces.get(new_window.workspace_id)))
            self.focused_window = address
            # Active-window focus does not change the workspace key's v1 visual.
            return StateChange(frozenset())

        if name in {"createworkspacev2", "destroyworkspacev2"} and len(fields) >= 2:
            workspace_id, workspace_name = int_field(fields[0]), fields[1]
            if not workspace_id:
                return StateChange(frozenset(), needs_resync=True)
            if name == "createworkspacev2":
                workspace = self.workspaces.get(workspace_id)
                if workspace is None:
                    workspace = Workspace(workspace_id, workspace_name)
                    self.workspaces[workspace_id] = workspace
                return StateChange(frozenset(self._keys_for(workspace)))
            workspace = self.workspaces.pop(workspace_id, None)
            keys = self._keys_for(workspace) | {f"id:{workspace_id}", f"name:{workspace_name}"}
            return StateChange(frozenset(keys))

        if name == "renameworkspace" and len(fields) >= 2:
            workspace = self.workspaces.get(int_field(fields[0]))
            if workspace is None:
                return StateChange(frozenset(), needs_resync=True)
            keys = self._keys_for(workspace)
            workspace.name = fields[1]
            for window in workspace.windows.values():
                window.workspace_name = fields[1]
            keys.update(self._keys_for(workspace))
            return StateChange(frozenset(keys))

        if name == "moveworkspacev2" and len(fields) >= 3:
            workspace = self.workspaces.get(int_field(fields[0]))
            if workspace is None:
                return StateChange(frozenset(), needs_resync=True)
            workspace.monitor = fields[2]
            return StateChange(frozenset(self._keys_for(workspace)), needs_resync=True)

        if name == "workspacev2" and len(fields) >= 2:
            workspace = self.workspaces.get(int_field(fields[0]))
            if workspace is None:
                return StateChange(frozenset(), needs_resync=True)
            return self._focus_workspace(workspace)

        if name == "focusedmonv2" and len(fields) >= 2:
            return self._focus_monitor(fields[0], int_field(fields[1]))

        if name in {"monitoraddedv2", "monitorremovedv2", "configreloaded"}:
            return StateChange(frozenset(self.all_keys()), needs_resync=True)

        return StateChange(frozenset())

    def _workspace_by_name(self, name: str) -> Workspace | None:
        return next((workspace for workspace in self.workspaces.values() if workspace.name == name), None)

    def _move_window(self, address: str, workspace_id: int, workspace_name: str) -> StateChange:
        window = self.windows.get(address)
        workspace = self.workspaces.get(workspace_id)
        if window is None or workspace is None:
            return StateChange(frozenset(), needs_resync=True)
        old_workspace = self.workspaces.get(window.workspace_id)
        if old_workspace and old_workspace.workspace_id == workspace_id:
            return StateChange(frozenset())
        keys = self._keys_for(old_workspace) | self._keys_for(workspace)
        if old_workspace:
            old_workspace.windows.pop(address, None)
        window.workspace_id = workspace_id
        window.workspace_name = workspace_name
        workspace.windows[address] = window
        return StateChange(frozenset(keys))

    def _focus_workspace(self, workspace: Workspace) -> StateChange:
        monitor = self.monitors.get(workspace.monitor)
        if monitor is None:
            return StateChange(frozenset(), needs_resync=True)
        keys: set[str] = set()
        old_workspace = self.workspaces.get(monitor.active_workspace_id)
        keys.update(self._keys_for(old_workspace))
        for candidate in self.monitors.values():
            if candidate.focused and candidate is not monitor:
                keys.update(self._keys_for(self.workspaces.get(candidate.active_workspace_id)))
            candidate.focused = candidate is monitor
        monitor.active_workspace_id = workspace.workspace_id
        monitor.active_workspace_name = workspace.name
        keys.update(self._keys_for(workspace))
        return StateChange(frozenset(keys))

    def _focus_monitor(self, monitor_name: str, workspace_id: int) -> StateChange:
        monitor = self.monitors.get(monitor_name)
        if monitor is None:
            return StateChange(frozenset(), needs_resync=True)
        keys: set[str] = set()
        for candidate in self.monitors.values():
            if candidate.focused or candidate is monitor:
                keys.update(self._keys_for(self.workspaces.get(candidate.active_workspace_id)))
            candidate.focused = candidate is monitor
        if workspace_id:
            monitor.active_workspace_id = workspace_id
            workspace = self.workspaces.get(workspace_id)
            if workspace:
                monitor.active_workspace_name = workspace.name
                keys.update(self._keys_for(workspace))
        return StateChange(frozenset(keys))
