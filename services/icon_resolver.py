from __future__ import annotations

import base64
import configparser
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
import os
from pathlib import Path
import re
import threading
from typing import Callable

from PIL import Image, ImageDraw, ImageFont

try:
    from ..hyprland.models import Window
except ImportError:  # Direct source-tree test import.
    from hyprland.models import Window


HostIconLoader = Callable[[list[str]], dict | None]


OVERRIDES = {
    "code": ("visual-studio-code", "com.visualstudio.code"),
    "googlechrome": ("google-chrome", "com.google.Chrome"),
    "chromiumbrowser": ("chromium", "org.chromium.Chromium"),
    "orgwezfurlongwezterm": ("wezterm", "org.wezfurlong.wezterm"),
}


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


@dataclass(frozen=True, slots=True)
class DesktopEntry:
    desktop_id: str
    name: str
    icon: str
    startup_wm_class: str
    executable: str
    source: Path


class IconResolver:
    """Resolve Hyprland app IDs through freedesktop desktop entries and icon themes."""

    def __init__(self, host_loader: HostIconLoader | None = None, size: int = 48):
        self.host_loader = host_loader
        self.size = size
        self._lock = threading.RLock()
        self._desktop_entries: tuple[DesktopEntry, ...] | None = None
        self._cache: dict[tuple[str, ...], Image.Image] = {}
        self._path_cache: dict[str, Path | None] = {}
        self.cache_hits = 0
        self.cache_misses = 0

    def resolve_window(self, window: Window) -> Image.Image:
        candidates = self.candidates_for(window)
        cache_key = tuple(normalize(candidate) for candidate in candidates if candidate)
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                self.cache_hits += 1
                return cached.copy()
        self.cache_misses += 1
        image = self._resolve(candidates) or self.fallback(window.app_identity)
        image = self._fit(image)
        with self._lock:
            self._cache[cache_key] = image.copy()
        return image

    def candidates_for(self, window: Window) -> list[str]:
        candidates: list[str] = []
        for value in (window.initial_class, window.app_class):
            value = value.strip()
            if value and value not in candidates:
                candidates.append(value)
            token = normalize(value)
            for override in OVERRIDES.get(token, ()):
                if override not in candidates:
                    candidates.append(override)
            lowered = value.casefold()
            if lowered.startswith("chrome-") or lowered.startswith("crx_"):
                candidates.extend(
                    item for item in ("google-chrome", "com.google.Chrome") if item not in candidates
                )
            elif lowered.startswith("chromium-"):
                candidates.extend(
                    item for item in ("chromium", "org.chromium.Chromium") if item not in candidates
                )
        return candidates or ["unknown"]

    def _resolve(self, candidates: list[str]) -> Image.Image | None:
        if self.host_loader is not None:
            try:
                payload = self.host_loader(candidates)
                if payload:
                    data = base64.b64decode(payload["data"])
                    return self._open_bytes(data, str(payload.get("suffix", "")))
            except Exception:
                return None

        entry = self._match_desktop_entry(candidates)
        if entry is None:
            return None
        path = self._find_icon(entry.icon)
        return self._open_path(path) if path else None

    def _match_desktop_entry(self, candidates: list[str]) -> DesktopEntry | None:
        tokens = {normalize(candidate) for candidate in candidates if normalize(candidate)}
        best_score = 0
        best_entry = None
        for entry in self._entries():
            values = (entry.desktop_id, entry.startup_wm_class, entry.name, entry.executable)
            score = 0
            for index, value in enumerate(values):
                token = normalize(value)
                if token in tokens:
                    score = max(score, 100 - index * 5)
                elif token and any(
                    token in candidate or candidate in token for candidate in tokens if len(candidate) >= 4
                ):
                    score = max(score, 50 - index * 3)
            if score > best_score:
                best_score, best_entry = score, entry
        return best_entry

    def _entries(self) -> tuple[DesktopEntry, ...]:
        with self._lock:
            if self._desktop_entries is not None:
                return self._desktop_entries
        entries: list[DesktopEntry] = []
        seen: set[Path] = set()
        for root in self._data_roots():
            applications = root / "applications"
            if not applications.is_dir():
                continue
            for path in applications.rglob("*.desktop"):
                if path in seen:
                    continue
                seen.add(path)
                parser = configparser.ConfigParser(interpolation=None, strict=False)
                try:
                    parser.read(path, encoding="utf-8")
                    section = parser["Desktop Entry"]
                except (OSError, UnicodeError, configparser.Error, KeyError):
                    continue
                if not section.get("Icon"):
                    continue
                executable = Path(section.get("Exec", "").split(" ", 1)[0]).name
                entries.append(
                    DesktopEntry(
                        desktop_id=path.stem,
                        name=section.get("Name", ""),
                        icon=section.get("Icon", ""),
                        startup_wm_class=section.get("StartupWMClass", section.get("X-Flatpak", "")),
                        executable=executable,
                        source=path,
                    )
                )
        result = tuple(entries)
        with self._lock:
            self._desktop_entries = result
        return result

    @staticmethod
    def _data_roots() -> tuple[Path, ...]:
        home = Path.home()
        values = [
            Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share"),
            *(
                Path(value)
                for value in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
                if value
            ),
            home / ".local/share/flatpak/exports/share",
            Path("/var/lib/flatpak/exports/share"),
        ]
        return tuple(dict.fromkeys(path for path in values if path.is_dir()))

    def _find_icon(self, icon: str) -> Path | None:
        with self._lock:
            if icon in self._path_cache:
                return self._path_cache[icon]
        absolute = Path(icon)
        if absolute.is_absolute() and absolute.is_file():
            result: Path | None = absolute
        else:
            result = self._find_themed_icon(icon)
        with self._lock:
            self._path_cache[icon] = result
        return result

    def _find_themed_icon(self, icon: str) -> Path | None:
        for root in self._data_roots():
            for relative in (
                f"pixmaps/{icon}",
                f"pixmaps/{icon}.png",
                f"pixmaps/{icon}.svg",
                f"icons/hicolor/128x128/apps/{icon}.png",
                f"icons/hicolor/96x96/apps/{icon}.png",
                f"icons/hicolor/64x64/apps/{icon}.png",
                f"icons/hicolor/scalable/apps/{icon}.svg",
            ):
                candidate = root / relative
                if candidate.is_file():
                    return candidate
        wanted = {f"{icon}.png", f"{icon}.svg", f"{icon}.xpm"}
        for root in self._data_roots():
            icon_root = root / "icons"
            if not icon_root.is_dir():
                continue
            for directory, _, files in os.walk(icon_root):
                match = next((name for name in files if name in wanted), None)
                if match:
                    return Path(directory) / match
        return None

    def _open_path(self, path: Path) -> Image.Image | None:
        try:
            if path.suffix.casefold() == ".svg":
                return self._open_bytes(path.read_bytes(), ".svg")
            with Image.open(path) as image:
                return image.convert("RGBA")
        except (OSError, ValueError):
            return None

    def _open_bytes(self, data: bytes, suffix: str) -> Image.Image | None:
        try:
            if suffix.casefold() == ".svg":
                import cairosvg

                data = cairosvg.svg2png(bytestring=data, output_width=self.size, output_height=self.size)
            with Image.open(BytesIO(data)) as image:
                return image.convert("RGBA")
        except (ImportError, OSError, ValueError):
            return None

    def _fit(self, image: Image.Image) -> Image.Image:
        image = image.convert("RGBA")
        image.thumbnail((self.size, self.size), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (self.size, self.size), (0, 0, 0, 0))
        canvas.alpha_composite(image, ((self.size - image.width) // 2, (self.size - image.height) // 2))
        return canvas

    @lru_cache(maxsize=64)
    def fallback(self, identity: str) -> Image.Image:
        canvas = Image.new("RGBA", (self.size, self.size), (68, 73, 82, 255))
        draw = ImageDraw.Draw(canvas)
        margin = max(2, self.size // 12)
        draw.rounded_rectangle(
            (margin, margin, self.size - margin - 1, self.size - margin - 1),
            radius=max(3, self.size // 7),
            outline=(170, 178, 190, 255),
            width=max(1, self.size // 18),
        )
        letter = next((character.upper() for character in identity if character.isalnum()), "?")
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", self.size // 2)
        except OSError:
            font = ImageFont.load_default()
        box = draw.textbbox((0, 0), letter, font=font)
        draw.text(
            ((self.size - (box[2] - box[0])) / 2, (self.size - (box[3] - box[1])) / 2 - box[1]),
            letter,
            font=font,
            fill=(238, 241, 245, 255),
        )
        return canvas
