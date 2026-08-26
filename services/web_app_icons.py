from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import re
import sqlite3

from PIL import Image


CHROMIUM_APP_ID = re.compile(r"[a-p]{32}")
PROFILE_SUFFIX = re.compile(r"-(Default|Profile_[0-9]+)$", re.IGNORECASE)
BROWSER_PREFIXES = (
    "microsoft-edge",
    "google-chrome",
    "chromium",
    "chrome",
    "brave",
    "msedge",
)


@dataclass(frozen=True, slots=True)
class WebAppIdentity:
    window_class: str
    browser: str
    profile: str = ""
    app_id: str = ""
    hostname: str = ""

    @property
    def cache_key(self) -> str:
        return f"{self.browser}:{self.profile}:{self.app_id or self.hostname or self.window_class}"


def detect_web_app(*window_classes: str) -> WebAppIdentity | None:
    """Recognize current Chromium-family app-window IDs without matching normal tabs."""
    for original in window_classes:
        value = original.strip()
        lowered = value.casefold()
        if not lowered:
            continue

        if lowered.startswith("crx_") or lowered.startswith("crx-"):
            app_id = lowered[4:]
            if CHROMIUM_APP_ID.fullmatch(app_id):
                return WebAppIdentity(value, "chromium", app_id=app_id)

        for prefix in BROWSER_PREFIXES:
            marker = f"{prefix}-"
            if not lowered.startswith(marker):
                continue
            encoded_identity = value[len(marker) :]
            profile_match = PROFILE_SUFFIX.search(encoded_identity)
            if profile_match is None:
                continue
            profile = profile_match.group(1).replace("_", " ")
            encoded_identity = encoded_identity[: profile_match.start()].rstrip("_")
            normalized_identity = encoded_identity.casefold()
            if CHROMIUM_APP_ID.fullmatch(normalized_identity):
                return WebAppIdentity(value, prefix, profile=profile, app_id=normalized_identity)

            # Chromium app-mode windows encode the URL origin before a double
            # underscore, e.g. chrome-chatgpt.com__-Default. Restrict this to a
            # hostname-like value so ordinary browser classes cannot be mistaken
            # for web apps.
            hostname = normalized_identity.split("__", 1)[0].rstrip("_").rstrip(".")
            if _is_hostname(hostname):
                return WebAppIdentity(value, prefix, profile=profile, hostname=hostname)
    return None


def _is_hostname(value: str) -> bool:
    if not value or len(value) > 253 or "." not in value:
        return False
    labels = value.split(".")
    return all(
        label
        and len(label) <= 63
        and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) is not None
        for label in labels
    )


class ChromiumWebAppIconResolver:
    """Read Chromium-family PWA icons and favicons entirely from local profiles."""

    def __init__(self, roots: tuple[Path, ...] | None = None):
        self.roots = roots if roots is not None else self.default_roots()

    @staticmethod
    def default_roots() -> tuple[Path, ...]:
        home = Path.home()
        candidates = (
            home / ".config/google-chrome",
            home / ".config/chromium",
            home / ".config/BraveSoftware/Brave-Browser",
            home / ".config/microsoft-edge",
            home / ".var/app/com.google.Chrome/config/google-chrome",
            home / ".var/app/org.chromium.Chromium/config/chromium",
            home / ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser",
            home / ".var/app/com.microsoft.Edge/config/microsoft-edge",
        )
        return tuple(dict.fromkeys(path for path in candidates if path.is_dir()))

    def resolve(self, identity: WebAppIdentity) -> Image.Image | None:
        profiles = self._profiles(identity)
        if identity.app_id:
            image = self._manifest_icon(profiles, identity.app_id)
            if image is not None:
                return image
        if identity.hostname:
            return self._favicon(profiles, identity.hostname)
        return None

    def _profiles(self, identity: WebAppIdentity) -> tuple[Path, ...]:
        roots = sorted(self.roots, key=lambda root: self._root_priority(root, identity.browser))
        profiles: list[Path] = []
        for root in roots:
            if identity.profile:
                preferred = root / identity.profile
                if preferred.is_dir():
                    profiles.append(preferred)
            default = root / "Default"
            if default.is_dir() and default not in profiles:
                profiles.append(default)
            try:
                others = sorted(path for path in root.glob("Profile *") if path.is_dir())
            except OSError:
                others = []
            profiles.extend(path for path in others if path not in profiles)
        return tuple(profiles)

    @staticmethod
    def _root_priority(root: Path, browser: str) -> int:
        value = str(root).casefold()
        if "brave" in browser:
            return 0 if "brave" in value else 10
        if "edge" in browser or browser == "msedge":
            return 0 if "edge" in value else 10
        if browser == "chromium":
            return 0 if value.endswith("/chromium") else 10
        return 0 if value.endswith("/google-chrome") else 10

    def _manifest_icon(self, profiles: tuple[Path, ...], app_id: str) -> Image.Image | None:
        for profile in profiles:
            icon_dir = profile / "Web Applications/Manifest Resources" / app_id / "Icons"
            if not icon_dir.is_dir():
                continue
            try:
                candidates = sorted(
                    (path for path in icon_dir.iterdir() if path.is_file()),
                    key=self._icon_file_size,
                    reverse=True,
                )
            except OSError:
                continue
            for path in candidates:
                image = self._open_path(path)
                if image is not None:
                    return image
        return None

    @staticmethod
    def _icon_file_size(path: Path) -> int:
        try:
            return int(path.stem) if path.stem.isdigit() else path.stat().st_size
        except OSError:
            return 0

    def _favicon(self, profiles: tuple[Path, ...], hostname: str) -> Image.Image | None:
        for profile in profiles:
            database = profile / "Favicons"
            if not database.is_file():
                continue
            try:
                uri = f"{database.resolve().as_uri()}?immutable=1"
                with closing(sqlite3.connect(uri, uri=True, timeout=0.05)) as connection:
                    row = connection.execute(
                        """
                        SELECT favicon_bitmaps.image_data
                          FROM icon_mapping
                          JOIN favicons ON favicons.id = icon_mapping.icon_id
                          JOIN favicon_bitmaps ON favicon_bitmaps.icon_id = favicons.id
                         WHERE icon_mapping.page_url = ?
                            OR icon_mapping.page_url LIKE ?
                            OR icon_mapping.page_url = ?
                            OR icon_mapping.page_url LIKE ?
                         ORDER BY (favicon_bitmaps.width * favicon_bitmaps.height) DESC,
                                  favicon_bitmaps.last_updated DESC
                         LIMIT 1
                        """,
                        (
                            f"https://{hostname}",
                            f"https://{hostname}/%",
                            f"http://{hostname}",
                            f"http://{hostname}/%",
                        ),
                    ).fetchone()
            except (OSError, sqlite3.Error):
                continue
            if row and isinstance(row[0], bytes) and 0 < len(row[0]) <= 4 * 1024 * 1024:
                image = self._open_bytes(row[0])
                if image is not None:
                    return image
        return None

    @staticmethod
    def _open_path(path: Path) -> Image.Image | None:
        try:
            with Image.open(path) as image:
                return image.convert("RGBA")
        except (OSError, ValueError):
            return None

    @staticmethod
    def _open_bytes(data: bytes) -> Image.Image | None:
        try:
            with Image.open(BytesIO(data)) as image:
                return image.convert("RGBA")
        except (OSError, ValueError):
            return None
