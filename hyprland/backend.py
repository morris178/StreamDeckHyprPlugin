from __future__ import annotations

from collections.abc import Iterator
import threading

from .models import WorkspaceTarget
from .transport import FlatpakTransport, HyprlandTransport


class HyprlandBackend:
    """The only Hyprland-facing API consumed by the state service."""

    def __init__(self, transport: HyprlandTransport):
        self.transport = transport

    def snapshot(self) -> dict:
        if isinstance(self.transport, FlatpakTransport):
            return self.transport.snapshot()
        return {
            "clients": self.transport.query_json("clients"),
            "workspaces": self.transport.query_json("workspaces"),
            "monitors": self.transport.query_json("monitors"),
            "status": self.transport.query_json("status"),
            "version": self.transport.query_json("version"),
        }

    def validate_current_hyprland(self, snapshot: dict) -> None:
        status = snapshot.get("status") or {}
        version = snapshot.get("version") or {}
        if status.get("configProvider") != "lua":
            raise RuntimeError("Hyprland's current Lua configuration provider is required")
        raw_version = str(version.get("version", "0.0.0"))
        try:
            parts = tuple(int(part) for part in raw_version.split(".")[:2])
        except ValueError as exc:
            raise RuntimeError(f"Unrecognized Hyprland version: {raw_version}") from exc
        if parts < (0, 56):
            raise RuntimeError(f"Hyprland 0.56 or newer is required (found {raw_version})")

    def switch_to_workspace(self, target: WorkspaceTarget) -> None:
        self.transport.dispatch_workspace(target.lua_selector())

    def move_focused_window(self, target: WorkspaceTarget, follow: bool = True) -> None:
        self.transport.move_focused_window(target.lua_selector(), follow=follow)

    def events(self, stop_event: threading.Event) -> Iterator[tuple[str, str]]:
        return self.transport.event_lines(stop_event)

    def close(self) -> None:
        self.transport.close()
