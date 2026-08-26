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
    WorkspaceVisualState.FOCUSED: ((11, 54, 45, 255), (62, 238, 177, 255)),
    WorkspaceVisualState.VISIBLE: ((20, 42, 69, 255), (91, 170, 255, 255)),
    WorkspaceVisualState.INACTIVE: ((22, 24, 29, 255), (101, 107, 118, 255)),
}

OUTLINE = {
    WorkspaceVisualState.FOCUSED: (43, 136, 109, 255),
    WorkspaceVisualState.VISIBLE: (52, 91, 133, 255),
    WorkspaceVisualState.INACTIVE: (67, 72, 82, 255),
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
        for group in groups:
            self.icon_resolver.prepare_window(group.window)
        key = (
            self.size,
            view.target.key,
            view.workspace_id,
            view.name,
            view.visual_state.value,
            view.connected,
            bool(view.error),
            tuple(
                (group.identity, group.count, self.icon_resolver.cache_token(group.window))
                for group in groups
            ),
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
        outline = OUTLINE[view.visual_state]
        if not view.connected or view.error:
            background, accent, outline = (45, 28, 30, 255), (233, 95, 105, 255), (132, 57, 64, 255)
        image = Image.new("RGBA", self.size, background)
        draw = ImageDraw.Draw(image)

        # Keep the outer edge quiet: StreamController draws its own blue selection
        # outline around a selected key. Workspace state is carried by the colored
        # status rail instead of another heavy, easily-confused border.
        draw.rounded_rectangle(
            (1, 1, width - 2, height - 2),
            radius=max(8, width // 10),
            outline=outline,
            width=max(1, width // 64),
        )
        rail_margin = max(7, width // 12)
        rail_height = max(3, height // 28)
        draw.rounded_rectangle(
            (rail_margin, 3, width - rail_margin - 1, 3 + rail_height),
            radius=max(1, rail_height // 2),
            fill=accent,
        )

        title = view.name or view.target.display_name
        title, title_font = self._fit_title(draw, title, width - 2 * rail_margin)
        title_box = draw.textbbox((0, 0), title, font=title_font)
        draw.text(
            (rail_margin, 9 - title_box[1]),
            title,
            font=title_font,
            fill=(248, 249, 251, 255),
        )

        if not view.connected or view.error:
            self._draw_disconnected(draw, width, height, accent)
            return image

        if not groups:
            return image

        overflow = max(0, len(groups) - 3) if len(groups) > 4 else 0
        visible_groups = groups[:3] if overflow else groups[:4]
        tile_count = len(visible_groups) + bool(overflow)
        layout = self._tile_layout(tile_count)
        for (x, y, cell), group in zip(layout, visible_groups):
            self._draw_tile(image, x, y, cell)
            icon = self.icon_resolver.resolve_window(group.window)
            icon_padding = max(2, cell // 11)
            icon.thumbnail((cell - icon_padding * 2, cell - icon_padding * 2), Image.Resampling.LANCZOS)
            image.alpha_composite(icon, (x + (cell - icon.width) // 2, y + (cell - icon.height) // 2))
            if group.count > 1:
                self._badge(draw, x + cell - 1, y + cell - 1, str(group.count), accent, cell)
        if overflow:
            x, y, cell = layout[-1]
            self._draw_tile(image, x, y, cell, outline=accent)
            draw = ImageDraw.Draw(image)
            overflow_font = self._font(max(12, cell // 2), bold=True)
            text = f"+{overflow}"
            box = draw.textbbox((0, 0), text, font=overflow_font)
            draw.text(
                (x + (cell - (box[2] - box[0])) / 2, y + (cell - (box[3] - box[1])) / 2 - box[1]),
                text,
                font=overflow_font,
                fill=(234, 237, 242, 255),
            )
        return image

    def _tile_layout(self, count: int) -> list[tuple[int, int, int]]:
        """Return adaptive app-tile geometry for one to four distinct apps."""
        if count <= 0:
            return []
        width, height = self.size
        content_top = max(32, height * 17 // 50)
        content_bottom = height - max(6, height // 16)
        available_height = max(1, content_bottom - content_top)

        if count == 1:
            cell = min(width * 13 // 24, available_height)
            return [((width - cell) // 2, content_top + (available_height - cell) // 2, cell)]

        if count == 2:
            gap = max(5, width // 18)
            cell = min((width - gap - max(10, width // 10)) // 2, available_height * 4 // 5)
            start_x = (width - 2 * cell - gap) // 2
            y = content_top + (available_height - cell) // 2
            return [(start_x, y, cell), (start_x + cell + gap, y, cell)]

        gap = max(4, width // 22)
        cell = min((width - gap - max(26, width // 4)) // 2, (available_height - gap) // 2)
        start_x = (width - 2 * cell - gap) // 2
        start_y = content_top + (available_height - 2 * cell - gap) // 2
        return [
            (start_x + (index % 2) * (cell + gap), start_y + (index // 2) * (cell + gap), cell)
            for index in range(count)
        ]

    @staticmethod
    def _draw_tile(
        image: Image.Image,
        x: int,
        y: int,
        cell: int,
        outline: tuple[int, ...] = (255, 255, 255, 30),
    ) -> None:
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        radius = max(4, cell // 5)
        overlay_draw.rounded_rectangle(
            (x + 1, y + 2, x + cell, y + cell + 1),
            radius=radius,
            fill=(0, 0, 0, 55),
        )
        overlay_draw.rounded_rectangle(
            (x, y, x + cell - 1, y + cell - 1),
            radius=radius,
            fill=(7, 10, 14, 118),
            outline=outline,
            width=1,
        )
        image.alpha_composite(overlay)

    def _fit_title(self, draw: ImageDraw.ImageDraw, title: str, max_width: int):
        title = title.strip() or "?"
        start_size = max(18, self.size[0] // 4)
        minimum_size = max(11, self.size[0] // 8)
        for size in range(start_size, minimum_size - 1, -1):
            font = self._font(size, bold=True)
            box = draw.textbbox((0, 0), title, font=font)
            if box[2] - box[0] <= max_width:
                return title, font

        font = self._font(minimum_size, bold=True)
        shortened = title
        while len(shortened) > 1:
            shortened = shortened[:-1]
            candidate = f"{shortened}…"
            box = draw.textbbox((0, 0), candidate, font=font)
            if box[2] - box[0] <= max_width:
                return candidate, font
        return "…", font

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

    def _badge(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        text: str,
        accent: tuple[int, ...],
        cell: int,
    ) -> None:
        font = self._font(max(9, cell // 3), bold=True)
        box = draw.textbbox((0, 0), text, font=font)
        text_width = box[2] - box[0]
        text_height = box[3] - box[1]
        badge_height = max(14, cell * 2 // 5)
        badge_width = max(badge_height, text_width + 7)
        left, top = x - badge_width, y - badge_height
        draw.rounded_rectangle((left, top, x, y), radius=badge_height // 2, fill=accent)
        draw.text(
            (left + (badge_width - text_width) / 2, top + (badge_height - text_height) / 2 - box[1]),
            text,
            font=font,
            fill=(9, 16, 18, 255),
        )

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
