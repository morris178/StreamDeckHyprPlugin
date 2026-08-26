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
import time
from typing import Callable

from PIL import Image, ImageDraw, ImageFont

try:
    from ..hyprland.models import Window
    from .web_app_icons import ChromiumWebAppIconResolver, WebAppIdentity, detect_web_app
except ImportError:  # Direct source-tree test import.
    from hyprland.models import Window
    from services.web_app_icons import ChromiumWebAppIconResolver, WebAppIdentity, detect_web_app


HostIconLoader = Callable[[list[str]], dict | None]
IconUpdatedCallback = Callable[[Window], None]


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

    def __init__(
        self,
        host_loader: HostIconLoader | None = None,
        size: int = 48,
        web_app_resolver: ChromiumWebAppIconResolver | None = None,
        web_app_retry_seconds: float = 5.0,
        web_app_retry_attempts: int = 3,
        on_icon_updated: IconUpdatedCallback | None = None,
    ):
        self.host_loader = host_loader
        self.size = size
        self.web_app_resolver = web_app_resolver or ChromiumWebAppIconResolver()
        self.web_app_retry_seconds = web_app_retry_seconds
        self.web_app_retry_attempts = web_app_retry_attempts
        self.on_icon_updated = on_icon_updated
        self._lock = threading.RLock()
        self._desktop_entries: tuple[DesktopEntry, ...] | None = None
        self._cache: dict[tuple[str, ...], Image.Image] = {}
        self._cache_expiry: dict[tuple[str, ...], float] = {}
        self._cache_revision: dict[tuple[str, ...], int] = {}
        self._path_cache: dict[str, Path | None] = {}
        self._retry_timers: dict[tuple[str, ...], threading.Timer] = {}
        self._retry_attempts: dict[tuple[str, ...], int] = {}
        self._stopped = False
        self.cache_hits = 0
        self.cache_misses = 0

    def resolve_window(self, window: Window) -> Image.Image:
        candidates = self.candidates_for(window)
        web_app = detect_web_app(window.initial_class, window.app_class)
        cache_key = self._cache_key(candidates, web_app)
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(cache_key)
            expiry = self._cache_expiry.get(cache_key)
            if cached is not None and (expiry is None or now < expiry):
                self.cache_hits += 1
                return cached.copy()
            if cached is not None:
                self._cache.pop(cache_key, None)
                self._cache_expiry.pop(cache_key, None)
        self.cache_misses += 1
        temporary_fallback = False
        if web_app is not None:
            primary_candidates = self._primary_candidates(window)
            image = self._resolve(primary_candidates)
            if image is None:
                image = self.web_app_resolver.resolve(web_app)
            if image is None:
                image = self._resolve(candidates)
                temporary_fallback = True
        else:
            image = self._resolve(candidates)
        if image is None:
            image = self.fallback(window.app_identity)
            temporary_fallback = web_app is not None
        image = self._fit(image)
        timer_to_cancel = None
        with self._lock:
            self._cache[cache_key] = image.copy()
            self._cache_revision[cache_key] = self._cache_revision.get(cache_key, 0) + 1
            if temporary_fallback:
                self._cache_expiry[cache_key] = now + self.web_app_retry_seconds
            else:
                self._cache_expiry.pop(cache_key, None)
                self._retry_attempts.pop(cache_key, None)
                timer_to_cancel = self._retry_timers.pop(cache_key, None)
        if timer_to_cancel is not None:
            timer_to_cancel.cancel()
        if temporary_fallback:
            self._schedule_web_app_retry(cache_key, window)
        return image

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            timers = tuple(self._retry_timers.values())
            self._retry_timers.clear()
            self._retry_attempts.clear()
        for timer in timers:
            timer.cancel()

    def _schedule_web_app_retry(self, cache_key: tuple[str, ...], window: Window) -> None:
        with self._lock:
            current = self._retry_timers.get(cache_key)
            attempt = self._retry_attempts.get(cache_key, 0)
            if (
                self._stopped
                or self.web_app_retry_attempts <= 0
                or attempt >= self.web_app_retry_attempts
                or (current is not None and current.is_alive())
            ):
                return
            delay = min(30.0, self.web_app_retry_seconds * (2**attempt))
            timer = threading.Timer(delay, self._retry_web_app, args=(cache_key, window))
            timer.name = "WebAppIconRetry"
            timer.daemon = True
            self._retry_attempts[cache_key] = attempt + 1
            self._retry_timers[cache_key] = timer
            timer.start()

    def _retry_web_app(self, cache_key: tuple[str, ...], window: Window) -> None:
        with self._lock:
            self._retry_timers.pop(cache_key, None)
            if self._stopped:
                return
            self._cache.pop(cache_key, None)
            self._cache_expiry.pop(cache_key, None)
        self.resolve_window(window)
        with self._lock:
            resolved = not self._stopped and cache_key in self._cache and cache_key not in self._cache_expiry
        if resolved and self.on_icon_updated is not None:
            try:
                self.on_icon_updated(window)
            except Exception:
                pass

    def cache_token(self, window: Window) -> tuple:
        """Expose retry state so the workspace render cache cannot hide a new favicon."""
        candidates = self.candidates_for(window)
        web_app = detect_web_app(window.initial_class, window.app_class)
        cache_key = self._cache_key(candidates, web_app)
        with self._lock:
            expiry = self._cache_expiry.get(cache_key)
            revision = self._cache_revision.get(cache_key, 0)
        if expiry is None:
            return (*cache_key, f"revision:{revision}")
        now = time.monotonic()
        return (*cache_key, f"revision:{revision}", "pending" if now < expiry else "retry")

    def prepare_window(self, window: Window) -> None:
        """Populate or refresh an icon before a renderer builds its cache key."""
        candidates = self.candidates_for(window)
        web_app = detect_web_app(window.initial_class, window.app_class)
        cache_key = self._cache_key(candidates, web_app)
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(cache_key)
            expiry = self._cache_expiry.get(cache_key)
            current = cached is not None and (expiry is None or now < expiry)
        if not current:
            self.resolve_window(window)

    @staticmethod
    def _cache_key(candidates: list[str], web_app: WebAppIdentity | None) -> tuple[str, ...]:
        prefix = (f"webapp:{web_app.cache_key}",) if web_app is not None else ("application",)
        return (*prefix, *(normalize(candidate) for candidate in candidates if candidate))

    @staticmethod
    def _primary_candidates(window: Window) -> list[str]:
        candidates: list[str] = []
        for value in (window.initial_class, window.app_class):
            value = value.strip()
            if value and value not in candidates:
                candidates.append(value)
        return candidates or ["unknown"]

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
        if image.width and image.height:
            scale = min(self.size / image.width, self.size / image.height)
            target = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
            if target != image.size:
                image = image.resize(target, Image.Resampling.LANCZOS)
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
