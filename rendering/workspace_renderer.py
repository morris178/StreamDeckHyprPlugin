from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock

from PIL import Image, ImageDraw, ImageFont

try:
    from ..hyprland.models import Window, WorkspaceView, WorkspaceVisualState
    from ..services.icon_resolver import IconResolver, normalize
except ImportError:  # Direct source-tree test import.
    from hyprland.models import Window, WorkspaceView, WorkspaceVisualState
    from services.icon_resolver import IconResolver, normalize


@dataclass(slots=True)
class AppGroup:
    identity: str
    window: Window
    count: int = 1


PALETTE = {
    WorkspaceVisualState.FOCUSED: ((13, 67, 56, 255), (70, 235, 176, 255)),
    WorkspaceVisualState.VISIBLE: ((25, 49, 78, 255), (91, 170, 255, 255)),
    WorkspaceVisualState.INACTIVE: ((24, 26, 31, 255), (91, 96, 106, 255)),
}


class WorkspaceRenderer:
    def __init__(self, icon_resolver: IconResolver, size: tuple[int, int] = (96, 96), cache_size: int = 128):
        self.icon_resolver = icon_resolver
        self.size = size
        self.cache_size = cache_size
        self._cache: OrderedDict[tuple, Image.Image] = OrderedDict()
        self._lock = RLock()

    def render(self, view: WorkspaceView) -> Image.Image:
        groups = self._group_apps(view.windows)
        key = (
            self.size,
            view.target.key,
            view.workspace_id,
            view.name,
            view.visual_state.value,
            view.connected,
            bool(view.error),
            tuple((group.identity, group.count) for group in groups),
        )
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached.copy()
        image = self._render_uncached(view, groups)
        with self._lock:
            self._cache[key] = image.copy()
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        return image

    def _render_uncached(self, view: WorkspaceView, groups: list[AppGroup]) -> Image.Image:
        width, height = self.size
        background, accent = PALETTE[view.visual_state]
        if not view.connected or view.error:
            background, accent = (45, 28, 30, 255), (233, 95, 105, 255)
        image = Image.new("RGBA", self.size, background)
        draw = ImageDraw.Draw(image)
        border_width = max(2, width // 32)
        draw.rounded_rectangle(
            (1, 1, width - 2, height - 2),
            radius=max(8, width // 10),
            outline=accent,
            width=border_width,
        )
        title_font = self._font(max(17, width // 4), bold=True)
        title = view.name or view.target.display_name
        if len(title) > 9:
            title = f"{title[:8]}…"
        title_box = draw.textbbox((0, 0), title, font=title_font)
        draw.text(((width - (title_box[2] - title_box[0])) / 2, 4 - title_box[1]), title, font=title_font, fill=(248, 249, 251, 255))

        if not view.connected or view.error:
            self._draw_disconnected(draw, width, height, accent)
            return image

        if not groups:
            empty_font = self._font(max(10, width // 9))
            text = "empty"
            box = draw.textbbox((0, 0), text, font=empty_font)
            draw.text(((width - (box[2] - box[0])) / 2, height * 0.62), text, font=empty_font, fill=(164, 169, 179, 255))
            return image

        visible_groups = groups[:4]
        overflow = len(groups) - 3 if len(groups) > 4 else 0
        if overflow > 0:
            visible_groups = groups[:3]
        cell = min(31, max(23, width // 3))
        gap = max(3, width // 24)
        columns = 2
        total_width = columns * cell + gap
        start_x = (width - total_width) // 2
        start_y = max(31, height - (2 * cell + gap) - 5)
        for index, group in enumerate(visible_groups):
            x = start_x + (index % 2) * (cell + gap)
            y = start_y + (index // 2) * (cell + gap)
            icon = self.icon_resolver.resolve_window(group.window)
            icon.thumbnail((cell, cell), Image.Resampling.LANCZOS)
            image.alpha_composite(icon, (x + (cell - icon.width) // 2, y + (cell - icon.height) // 2))
            if group.count > 1:
                self._badge(draw, x + cell - 2, y + cell - 2, str(group.count), accent)
        if overflow:
            index = 3
            x = start_x + (index % 2) * (cell + gap)
            y = start_y + (index // 2) * (cell + gap)
            overflow_font = self._font(max(11, cell // 2), bold=True)
            text = f"+{overflow}"
            box = draw.textbbox((0, 0), text, font=overflow_font)
            draw.text(
                (x + (cell - (box[2] - box[0])) / 2, y + (cell - (box[3] - box[1])) / 2 - box[1]),
                text,
                font=overflow_font,
                fill=(234, 237, 242, 255),
            )
        return image

    @staticmethod
    def _group_apps(windows: tuple[Window, ...]) -> list[AppGroup]:
        groups: OrderedDict[str, AppGroup] = OrderedDict()
        for window in windows:
            identity = normalize(window.app_identity) or normalize(window.app_class) or "unknown"
            if identity in groups:
                groups[identity].count += 1
            else:
                groups[identity] = AppGroup(identity, window)
        return list(groups.values())

    def _badge(self, draw: ImageDraw.ImageDraw, x: int, y: int, text: str, accent: tuple[int, ...]) -> None:
        radius = max(7, self.size[0] // 13)
        draw.ellipse((x - radius * 2, y - radius * 2, x, y), fill=accent)
        font = self._font(max(9, radius), bold=True)
        box = draw.textbbox((0, 0), text, font=font)
        draw.text((x - radius - (box[2] - box[0]) / 2, y - radius - (box[3] - box[1]) / 2 - box[1]), text, font=font, fill=(12, 18, 20, 255))

    def _draw_disconnected(self, draw: ImageDraw.ImageDraw, width: int, height: int, accent: tuple[int, ...]) -> None:
        y = height * 0.64
        draw.line((width * 0.31, y, width * 0.69, y), fill=accent, width=max(3, width // 24))
        draw.line((width * 0.42, y - 10, width * 0.58, y + 10), fill=accent, width=max(3, width // 24))

    @staticmethod
    def _font(size: int, bold: bool = False):
        filename = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        try:
            return ImageFont.truetype(filename, size)
        except OSError:
            return ImageFont.load_default()
