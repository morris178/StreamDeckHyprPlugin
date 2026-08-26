from __future__ import annotations

from PIL import Image

from GtkHelper.ComboRow import SimpleComboRowItem
from GtkHelper.GenerativeUI.ColorButtonRow import ColorButtonRow
from GtkHelper.GenerativeUI.ComboRow import ComboRow
from GtkHelper.GenerativeUI.EntryRow import EntryRow
from GtkHelper.GenerativeUI.ScaleRow import ScaleRow
from src.backend.PluginManager.InputBases import KeyAction

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

try:
    from ..hyprland.models import WorkspaceTarget, WorkspaceView
    from ..rendering.workspace_renderer import (
        DEFAULT_BACKGROUND_OPACITY,
        DEFAULT_TITLE_COLOR,
        WorkspaceRenderStyle,
    )
except ImportError:  # Direct source-tree test import.
    from hyprland.models import WorkspaceTarget, WorkspaceView
    from rendering.workspace_renderer import (
        DEFAULT_BACKGROUND_OPACITY,
        DEFAULT_TITLE_COLOR,
        WorkspaceRenderStyle,
    )


class WorkspaceAction(KeyAction):
    """A Stream Deck key representing one numeric or named Hyprland workspace."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_configuration = True
        self._target: WorkspaceTarget | None = None
        self._last_view: WorkspaceView | None = None
        self._subscribed = False
        self._render_style = WorkspaceRenderStyle.from_settings(self.get_settings())
        translate = self.plugin_base.locale_manager.get
        self.workspace_row = EntryRow(
            action_core=self,
            var_name="workspace",
            default_value="1",
            title="actions.workspace.setting",
            filter_func=lambda value: value.strip(),
            on_change=self._on_workspace_changed,
        )
        self.background_opacity_row = ScaleRow(
            action_core=self,
            var_name="background_opacity",
            default_value=DEFAULT_BACKGROUND_OPACITY,
            min=0,
            max=100,
            step=1,
            digits=0,
            title="actions.workspace.background-opacity",
            subtitle="actions.workspace.background-opacity.subtitle",
            on_change=self._on_style_changed,
        )
        self.title_color_row = ColorButtonRow(
            action_core=self,
            var_name="title_color",
            default_value=DEFAULT_TITLE_COLOR,
            title="actions.workspace.title-color",
            on_change=self._on_style_changed,
        )
        self.title_font_row = ComboRow(
            action_core=self,
            var_name="title_font",
            default_value="sans",
            items=[
                SimpleComboRowItem("sans", translate("actions.workspace.font.sans", "Sans")),
                SimpleComboRowItem("condensed", translate("actions.workspace.font.condensed", "Condensed")),
                SimpleComboRowItem("serif", translate("actions.workspace.font.serif", "Serif")),
                SimpleComboRowItem("monospace", translate("actions.workspace.font.monospace", "Monospace")),
            ],
            title="actions.workspace.title-font",
            on_change=self._on_style_changed,
        )
        self.title_weight_row = ComboRow(
            action_core=self,
            var_name="title_weight",
            default_value="bold",
            items=[
                SimpleComboRowItem("regular", translate("actions.workspace.font.regular", "Regular")),
                SimpleComboRowItem("bold", translate("actions.workspace.font.bold", "Bold")),
            ],
            title="actions.workspace.title-weight",
            on_change=self._on_style_changed,
        )
        self.title_size_row = ScaleRow(
            action_core=self,
            var_name="title_size",
            default_value=24,
            min=12,
            max=34,
            step=1,
            digits=0,
            title="actions.workspace.title-size",
            on_change=self._on_style_changed,
        )

    def on_ready(self) -> None:
        self._render_style = WorkspaceRenderStyle.from_settings(self.get_settings())
        self._change_target(self.get_settings().get("workspace", "1"))

    def on_update(self) -> None:
        if self._last_view is not None:
            self._schedule_render(self._last_view)
        else:
            self.on_ready()

    def on_key_down(self, _event_data=None) -> None:
        if self._target is None:
            self.show_error(duration=1)
            return
        self.plugin_base.workspace_service.switch_to_workspace(self._target)

    # StreamController 1.5 passes event data to all KeyAction callbacks while
    # the inherited no-op methods still have parameterless signatures.
    def on_key_up(self, _event_data=None) -> None:
        pass

    def on_key_short_up(self, _event_data=None) -> None:
        pass

    def on_key_hold_start(self, _event_data=None) -> None:
        pass

    def on_key_hold_stop(self, _event_data=None) -> None:
        pass

    def on_disconnect(self) -> None:
        self._unsubscribe()
        self.plugin_base.render_scheduler.cancel(self)
        super().on_disconnect()

    def on_remove(self) -> None:
        self._unsubscribe()
        self.plugin_base.render_scheduler.cancel(self)

    def _on_workspace_changed(self, _widget, new_value: str, _old_value: str) -> None:
        self._change_target(new_value)

    def _on_style_changed(self, _widget, _new_value, _old_value) -> None:
        self._render_style = WorkspaceRenderStyle.from_settings(self.get_settings())
        if self._last_view is not None:
            self._schedule_render(self._last_view)

    def _change_target(self, configured_value: object) -> None:
        try:
            target = WorkspaceTarget.parse(configured_value)
        except ValueError:
            self._unsubscribe()
            self._target = None
            if self.on_ready_called:
                self.show_error(duration=-1)
            return
        if target == self._target and self._subscribed:
            return
        self._unsubscribe()
        self._target = target
        self._subscribed = True
        self.plugin_base.workspace_service.subscribe(target, self._on_workspace_view)

    def _unsubscribe(self) -> None:
        if self._target is not None and self._subscribed:
            self.plugin_base.workspace_service.unsubscribe(self._target, self._on_workspace_view)
        self._subscribed = False

    def _on_workspace_view(self, view: WorkspaceView) -> None:
        self._last_view = view
        self._schedule_render(view)

    def _schedule_render(self, view: WorkspaceView) -> None:
        self.plugin_base.render_scheduler.schedule(
            self,
            view,
            self._render_style,
            self._render_finished,
        )

    def _render_finished(self, image: Image.Image, view: WorkspaceView) -> None:
        GLib.idle_add(self._apply_render, image, view.target.key, view.connected and not view.error)

    def _apply_render(self, image: Image.Image, target_key: str, healthy: bool) -> bool:
        if self._target is None or self._target.key != target_key or not self._subscribed:
            return False
        try:
            self.set_media(image=image, size=1.0, valign=0.0, halign=0.0)
            if healthy:
                self.hide_error()
        except Exception:
            # The action can disappear between scheduling and the GTK callback.
            return False
        return False
