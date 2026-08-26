from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import re


MAX_WORKSPACE_ID = 2_147_483_647


def normalize_address(value: object) -> str:
    address = str(value or "").strip().lower()
    if not address:
        return ""
    if address.startswith("0x"):
        return address
    if re.fullmatch(r"[0-9a-f]+", address):
        return f"0x{address}"
    return address


class WorkspaceVisualState(str, Enum):
    FOCUSED = "focused"
    VISIBLE = "visible"
    INACTIVE = "inactive"


@dataclass(frozen=True, slots=True)
class WorkspaceTarget:
    """A configured numeric or named Hyprland workspace."""

    workspace_id: int | None = None
    name: str | None = None

    @classmethod
    def parse(cls, value: object) -> "WorkspaceTarget":
        text = str(value if value is not None else "").strip()
        if not text:
            raise ValueError("Workspace must not be empty")
        if re.fullmatch(r"[0-9]+", text):
            workspace_id = int(text)
            if not 1 <= workspace_id <= MAX_WORKSPACE_ID:
                raise ValueError(f"Workspace ID must be between 1 and {MAX_WORKSPACE_ID}")
            return cls(workspace_id=workspace_id)

        if text.startswith("name:"):
            text = text[5:].strip()
        if not text:
            raise ValueError("Named workspace must not be empty")
        if text.startswith("special:") or text == "special":
            raise ValueError("Special workspaces are not supported")
        return cls(name=text)

    @property
    def key(self) -> str:
        return f"id:{self.workspace_id}" if self.workspace_id is not None else f"name:{self.name}"

    @property
    def display_name(self) -> str:
        return str(self.workspace_id) if self.workspace_id is not None else str(self.name)

    def lua_selector(self) -> str:
        if self.workspace_id is not None:
            return str(self.workspace_id)
        # JSON string syntax is a valid Lua string literal for these values.
        return json.dumps(f"name:{self.name}", ensure_ascii=False)


@dataclass(slots=True)
class Window:
    address: str
    workspace_id: int
    workspace_name: str
    app_class: str = ""
    initial_class: str = ""
    title: str = ""
    position: tuple[int, int] = (0, 0)
    size: tuple[int, int] = (0, 0)
    focus_history_id: int = -1

    @property
    def app_identity(self) -> str:
        return (self.initial_class or self.app_class or "unknown").strip()


@dataclass(slots=True)
class Workspace:
    workspace_id: int
    name: str
    monitor: str = ""
    windows: dict[str, Window] = field(default_factory=dict)


@dataclass(slots=True)
class Monitor:
    monitor_id: int
    name: str
    active_workspace_id: int = 0
    active_workspace_name: str = ""
    focused: bool = False
    position: tuple[int, int] = (0, 0)
    size: tuple[int, int] = (0, 0)
    scale: float = 1.0


@dataclass(frozen=True, slots=True)
class WorkspaceView:
    target: WorkspaceTarget
    workspace_id: int | None
    name: str
    monitor: str
    visual_state: WorkspaceVisualState
    windows: tuple[Window, ...]
    connected: bool = True
    error: str | None = None

    @property
    def signature(self) -> tuple:
        return (
            self.target.key,
            self.workspace_id,
            self.name,
            self.monitor,
            self.visual_state.value,
            self.connected,
            self.error,
            tuple(
                (
                    window.address,
                    window.app_class,
                    window.initial_class,
                    window.title,
                    window.workspace_id,
                    window.position,
                    window.size,
                    window.focus_history_id,
                )
                for window in self.windows
            ),
        )
