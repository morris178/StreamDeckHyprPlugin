from __future__ import annotations

import os

from loguru import logger as log

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.PluginManager.ActionInputSupport import ActionInputSupport
from src.backend.PluginManager.PluginBase import PluginBase
from src.Signals.Signals import AppQuit

from .actions.workspace import WorkspaceAction
from .hyprland.backend import HyprlandBackend
from .hyprland.transport import FlatpakTransport, select_transport
from .rendering.workspace_renderer import WorkspaceRenderer
from .services.icon_resolver import IconResolver
from .services.render_scheduler import RenderScheduler
from .services.workspace_service import WorkspaceService


class HyprlandWorkspacePlugin(PluginBase):
    def __init__(self):
        super().__init__()
        helper_path = os.path.join(self.PATH, "helpers", "hyprland_event_helper.py")
        self.hyprland_transport = select_transport(helper_path)
        self.hyprland_backend = HyprlandBackend(self.hyprland_transport)
        self.workspace_service = WorkspaceService(self.hyprland_backend, log.bind(plugin="HyprlandWorkspaces"))

        host_loader = None
        if isinstance(self.hyprland_transport, FlatpakTransport):
            host_loader = self.hyprland_transport.load_host_icon
        self.icon_resolver = IconResolver(
            host_loader=host_loader,
            size=48,
            on_icon_updated=self.workspace_service.notify_app_icon_changed,
        )
        self.workspace_renderer = WorkspaceRenderer(self.icon_resolver)
        self.render_scheduler = RenderScheduler(self.workspace_renderer)
        self._stopped = False

        holder = ActionHolder(
            plugin_base=self,
            action_core=WorkspaceAction,
            action_id_suffix="Workspace",
            action_name=self.locale_manager.get("actions.workspace.name", "Hyprland Workspace"),
            description="Switch to and display one Hyprland workspace with live application icons.",
            requirements="Hyprland 0.56 or newer using the Lua configuration provider.",
            settings_schema={
                "workspace": {
                    "type": "string",
                    "description": "Numeric workspace ID or a named workspace (plain name or name:Name).",
                    "default": "1",
                    "required": True,
                    "example": "3",
                },
                "background_opacity": {
                    "type": "number",
                    "description": "Opacity of the state-colored key background in percent.",
                    "default": 68,
                    "minimum": 0,
                    "maximum": 100,
                },
                "title_color": {
                    "type": "array",
                    "description": "RGBA color of the workspace name or number.",
                    "default": [248, 249, 251, 255],
                },
                "title_font": {
                    "type": "string",
                    "description": "Workspace label font family.",
                    "default": "sans",
                    "enum": ["sans", "condensed", "serif", "monospace"],
                },
                "title_weight": {
                    "type": "string",
                    "description": "Workspace label font weight.",
                    "default": "bold",
                    "enum": ["regular", "bold"],
                },
                "title_size": {
                    "type": "number",
                    "description": "Preferred workspace label size in pixels; long names shrink to fit.",
                    "default": 24,
                    "minimum": 12,
                    "maximum": 34,
                },
            },
            action_support={
                Input.Key: ActionInputSupport.SUPPORTED,
                Input.Dial: ActionInputSupport.UNSUPPORTED,
                Input.Touchscreen: ActionInputSupport.UNSUPPORTED,
                Input.TouchKey: ActionInputSupport.UNSUPPORTED,
                Input.Screen: ActionInputSupport.UNSUPPORTED,
            },
        )
        self.add_action_holder(holder)
        self.register(
            plugin_name=self.locale_manager.get("plugin.name", "Hyprland Workspaces"),
            github_repo="https://github.com/morris178/StreamDeckHyprPlugin",
            plugin_version="1.3.0",
            app_version="1.5.0-beta.16",
        )

        # StreamController has no dedicated public plugin-unload hook yet.
        import globals as gl

        gl.signal_manager.connect_signal(AppQuit, self.stop)
        self.workspace_service.start()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.icon_resolver.stop()
        self.workspace_service.stop()
        self.render_scheduler.stop()

    def on_uninstall(self) -> None:
        self.stop()
        super().on_uninstall()

    def on_disconnect(self, conn) -> None:
        self.stop()
        super().on_disconnect(conn)
