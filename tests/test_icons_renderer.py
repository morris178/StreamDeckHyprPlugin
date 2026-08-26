from __future__ import annotations

import base64
from contextlib import closing
from io import BytesIO
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from hyprland.models import Window, WorkspaceTarget, WorkspaceView, WorkspaceVisualState
from rendering.workspace_renderer import (
    DEFAULT_TITLE_COLOR,
    PALETTE,
    WorkspaceRenderer,
    WorkspaceRenderStyle,
)
from services.icon_resolver import IconResolver
from services.web_app_icons import ChromiumWebAppIconResolver, detect_web_app


class IconResolverTests(unittest.TestCase):
    def test_desktop_entry_resolution_and_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            applications = root / "applications"
            icons = root / "icons/hicolor/64x64/apps"
            applications.mkdir(parents=True)
            icons.mkdir(parents=True)
            (applications / "org.example.Editor.desktop").write_text(
                "[Desktop Entry]\nName=Example Editor\nStartupWMClass=ExampleEditor\nIcon=example-editor\nExec=editor\n",
                encoding="utf-8",
            )
            Image.new("RGBA", (64, 64), (10, 220, 80, 255)).save(icons / "example-editor.png")
            window = Window("0x1", 1, "1", "ExampleEditor", "ExampleEditor", "File")
            with patch.dict(
                os.environ,
                {"XDG_DATA_HOME": str(root), "XDG_DATA_DIRS": str(root)},
                clear=False,
            ):
                resolver = IconResolver(size=32)
                first = resolver.resolve_window(window)
                second = resolver.resolve_window(window)
            self.assertEqual(first.size, (32, 32))
            self.assertEqual(first.getpixel((16, 16))[:3], (10, 220, 80))
            self.assertEqual(second.tobytes(), first.tobytes())
            self.assertEqual((resolver.cache_misses, resolver.cache_hits), (1, 1))

    def test_missing_icon_uses_neutral_fallback_and_caches(self):
        resolver = IconResolver(size=32)
        resolver._desktop_entries = ()
        window = Window("0x2", 1, "1", "missing-app", "missing-app", "")
        image = resolver.resolve_window(window)
        resolver.resolve_window(window)
        self.assertEqual(image.size, (32, 32))
        self.assertEqual(resolver.cache_hits, 1)

    def test_chrome_pwa_adds_browser_desktop_candidates(self):
        resolver = IconResolver(size=32)
        window = Window(
            "0x3",
            1,
            "1",
            "chrome-example.com__-Default",
            "chrome-example.com__-Default",
            "",
        )
        candidates = resolver.candidates_for(window)
        self.assertIn("google-chrome", candidates)

    def test_web_app_favicon_precedes_browser_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_favicon_database(root, "https://chatgpt.com/", (15, 210, 120, 255))
            browser_icon = BytesIO()
            Image.new("RGBA", (32, 32), (220, 30, 40, 255)).save(browser_icon, format="PNG")

            def host_loader(candidates):
                if "google-chrome" not in candidates:
                    return None
                return {
                    "data": base64.b64encode(browser_icon.getvalue()).decode("ascii"),
                    "suffix": ".png",
                }

            resolver = IconResolver(
                host_loader=host_loader,
                size=32,
                web_app_resolver=ChromiumWebAppIconResolver((root,)),
            )
            window = Window(
                "0x4",
                5,
                "5",
                "chrome-chatgpt.com__-Default",
                "chrome-chatgpt.com__-Default",
                "ChatGPT",
            )
            image = resolver.resolve_window(window)
            self.assertEqual(image.getpixel((16, 16)), (15, 210, 120, 255))

    def test_web_app_browser_fallback_expires_for_local_retry(self):
        browser_icon = BytesIO()
        Image.new("RGBA", (32, 32), (220, 30, 40, 255)).save(browser_icon, format="PNG")

        def host_loader(candidates):
            if "google-chrome" not in candidates:
                return None
            return {
                "data": base64.b64encode(browser_icon.getvalue()).decode("ascii"),
                "suffix": ".png",
            }

        class DeferredWebAppResolver:
            calls = 0

            def resolve(self, _identity):
                self.calls += 1
                if self.calls < 2:
                    return None
                return Image.new("RGBA", (32, 32), (15, 210, 120, 255))

        web_apps = DeferredWebAppResolver()
        resolver = IconResolver(
            host_loader=host_loader,
            size=32,
            web_app_resolver=web_apps,
            web_app_retry_seconds=10,
            web_app_retry_attempts=0,
        )
        window = Window(
            "0x5",
            5,
            "5",
            "chrome-chatgpt.com__-Default",
            "chrome-chatgpt.com__-Default",
            "ChatGPT",
        )
        with patch("services.icon_resolver.time.monotonic", return_value=100):
            first = resolver.resolve_window(window)
            pending_token = resolver.cache_token(window)
        with patch("services.icon_resolver.time.monotonic", return_value=105):
            second = resolver.resolve_window(window)
        with patch("services.icon_resolver.time.monotonic", return_value=111):
            third = resolver.resolve_window(window)
            resolved_token = resolver.cache_token(window)

        self.assertEqual(first.getpixel((16, 16)), (220, 30, 40, 255))
        self.assertEqual(second.getpixel((16, 16)), (220, 30, 40, 255))
        self.assertEqual(third.getpixel((16, 16)), (15, 210, 120, 255))
        self.assertEqual(web_apps.calls, 2)
        self.assertIn("revision:1", pending_token)
        self.assertIn("pending", pending_token)
        self.assertIn("revision:2", resolved_token)
        self.assertNotIn("pending", resolved_token)

    def test_web_app_background_retry_reports_resolved_icon(self):
        browser_icon = BytesIO()
        Image.new("RGBA", (32, 32), (220, 30, 40, 255)).save(browser_icon, format="PNG")

        def host_loader(candidates):
            if "google-chrome" not in candidates:
                return None
            return {
                "data": base64.b64encode(browser_icon.getvalue()).decode("ascii"),
                "suffix": ".png",
            }

        class DeferredWebAppResolver:
            calls = 0

            def resolve(self, _identity):
                self.calls += 1
                if self.calls < 2:
                    return None
                return Image.new("RGBA", (32, 32), (15, 210, 120, 255))

        updated = threading.Event()
        resolver = IconResolver(
            host_loader=host_loader,
            size=32,
            web_app_resolver=DeferredWebAppResolver(),
            web_app_retry_seconds=0.01,
            web_app_retry_attempts=1,
            on_icon_updated=lambda _window: updated.set(),
        )
        window = Window(
            "0x6",
            5,
            "5",
            "chrome-chatgpt.com__-Default",
            "chrome-chatgpt.com__-Default",
            "ChatGPT",
        )
        try:
            resolver.resolve_window(window)
            self.assertTrue(updated.wait(timeout=1))
            image = resolver.resolve_window(window)
            self.assertEqual(image.getpixel((16, 16)), (15, 210, 120, 255))
        finally:
            resolver.stop()

    @staticmethod
    def _write_favicon_database(root: Path, page_url: str, color: tuple[int, ...]) -> None:
        profile = root / "Default"
        profile.mkdir(parents=True)
        payload = BytesIO()
        Image.new("RGBA", (64, 64), color).save(payload, format="PNG")
        with closing(sqlite3.connect(profile / "Favicons")) as connection:
            connection.executescript(
                """
                CREATE TABLE favicons(id INTEGER PRIMARY KEY, url TEXT NOT NULL, icon_type INTEGER DEFAULT 1);
                CREATE TABLE icon_mapping(id INTEGER PRIMARY KEY, page_url TEXT NOT NULL, icon_id INTEGER, page_url_type INTEGER DEFAULT 0);
                CREATE TABLE favicon_bitmaps(id INTEGER PRIMARY KEY, icon_id INTEGER NOT NULL, last_updated INTEGER DEFAULT 0, image_data BLOB, width INTEGER DEFAULT 0, height INTEGER DEFAULT 0, last_requested INTEGER DEFAULT 0);
                """
            )
            connection.execute("INSERT INTO favicons(id, url) VALUES(1, ?)", (f"{page_url}favicon.ico",))
            connection.execute("INSERT INTO icon_mapping(page_url, icon_id) VALUES(?, 1)", (page_url,))
            connection.execute(
                "INSERT INTO favicon_bitmaps(icon_id, image_data, width, height) VALUES(1, ?, 64, 64)",
                (payload.getvalue(),),
            )
            connection.commit()


class ChromiumWebAppIconTests(unittest.TestCase):
    def test_detects_url_webapp_but_not_regular_browser(self):
        identity = detect_web_app("chrome-chatgpt.com__-Default")
        self.assertIsNotNone(identity)
        self.assertEqual(identity.hostname, "chatgpt.com")
        self.assertEqual(identity.profile, "Default")
        self.assertIsNone(detect_web_app("google-chrome", "google-chrome"))

    def test_detects_crx_and_profile_webapp_ids(self):
        app_id = "abcdefghijklmnopabcdefghijklmnop"
        crx_identity = detect_web_app(f"crx_{app_id}")
        profile_identity = detect_web_app(f"chrome-{app_id}-Profile_2")
        self.assertEqual(crx_identity.app_id, app_id)
        self.assertEqual(profile_identity.app_id, app_id)
        self.assertEqual(profile_identity.profile, "Profile 2")

    def test_reads_largest_local_manifest_icon(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app_id = "abcdefghijklmnopabcdefghijklmnop"
            icon_dir = root / "Default/Web Applications/Manifest Resources" / app_id / "Icons"
            icon_dir.mkdir(parents=True)
            Image.new("RGBA", (32, 32), (220, 30, 30, 255)).save(icon_dir / "32.png")
            Image.new("RGBA", (128, 128), (20, 80, 230, 255)).save(icon_dir / "128.png")
            resolver = ChromiumWebAppIconResolver((root,))
            image = resolver.resolve(detect_web_app(f"crx_{app_id}"))
            self.assertEqual(image.size, (128, 128))
            self.assertEqual(image.getpixel((64, 64)), (20, 80, 230, 255))


class RendererTests(unittest.TestCase):
    def test_render_style_normalizes_saved_settings(self):
        style = WorkspaceRenderStyle.from_settings(
            {
                "background_opacity": 25,
                "title_color": [1.0, 0.5, 0.0, 1.0],
                "title_font": "MONOSPACE",
                "title_size": 99,
                "title_weight": "regular",
            }
        )
        self.assertEqual(style.background_alpha, 64)
        self.assertEqual(style.title_color, (255, 128, 0, 255))
        self.assertEqual(style.title_font, "monospace")
        self.assertEqual(style.title_size, 34)
        self.assertEqual(style.title_weight, "regular")

        fallback = WorkspaceRenderStyle.from_settings(
            {
                "background_opacity": "invalid",
                "title_color": "invalid",
                "title_font": "comic-sans",
                "title_weight": "heavy",
            }
        )
        self.assertEqual(fallback, WorkspaceRenderStyle())
        self.assertEqual(fallback.title_color, DEFAULT_TITLE_COLOR)

    def test_render_states_and_cache(self):
        resolver = IconResolver(size=32)
        resolver._desktop_entries = ()
        renderer = WorkspaceRenderer(resolver)
        view = WorkspaceView(
            target=WorkspaceTarget.parse("1"),
            workspace_id=1,
            name="1",
            monitor="DP-1",
            visual_state=WorkspaceVisualState.FOCUSED,
            windows=(Window("0x1", 1, "1", "kitty", "kitty", "Shell"),),
        )
        first = renderer.render(view)
        second = renderer.render(view)
        self.assertEqual(first.size, (96, 96))
        self.assertEqual(first.tobytes(), second.tobytes())
        self.assertEqual(len(renderer._cache), 1)

    def test_duplicate_apps_are_grouped(self):
        windows = (
            Window("0x1", 1, "1", "firefox", "firefox", "A"),
            Window("0x2", 1, "1", "firefox", "firefox", "B"),
            Window("0x3", 1, "1", "kitty", "kitty", "C"),
        )
        groups = WorkspaceRenderer._group_apps(windows)
        self.assertEqual([(group.identity, group.count) for group in groups], [("firefox", 2), ("kitty", 1)])

    def test_app_tiles_adapt_to_group_count(self):
        renderer = WorkspaceRenderer(IconResolver(size=32))
        single = renderer._tile_layout(1)
        pair = renderer._tile_layout(2)
        grid = renderer._tile_layout(4)

        self.assertEqual((len(single), len(pair), len(grid)), (1, 2, 4))
        self.assertGreater(single[0][2], pair[0][2])
        self.assertGreater(pair[0][2], grid[0][2])
        for layout in (single, pair, grid):
            for x, y, cell in layout:
                self.assertGreater(cell, 0)
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(x + cell, renderer.size[0])
                self.assertLessEqual(y + cell, renderer.size[1])

    def test_state_rail_uses_workspace_state_accent(self):
        resolver = IconResolver(size=32)
        resolver._desktop_entries = ()
        renderer = WorkspaceRenderer(resolver)
        for state in WorkspaceVisualState:
            with self.subTest(state=state):
                view = WorkspaceView(
                    target=WorkspaceTarget.parse("1"),
                    workspace_id=1,
                    name="1",
                    monitor="DP-1",
                    visual_state=state,
                    windows=(),
                )
                image = renderer.render(view)
                self.assertEqual(image.getpixel((48, 4)), PALETTE[state][1])

    def test_empty_workspace_has_no_content_label(self):
        resolver = IconResolver(size=32)
        renderer = WorkspaceRenderer(resolver)
        view = WorkspaceView(
            target=WorkspaceTarget.parse("2"),
            workspace_id=2,
            name="2",
            monitor="",
            visual_state=WorkspaceVisualState.INACTIVE,
            windows=(),
        )
        image = renderer.render(view)
        background = PALETTE[WorkspaceVisualState.INACTIVE][0]
        self.assertEqual(image.getpixel((48, 62)), background)

    def test_background_opacity_is_real_image_alpha(self):
        resolver = IconResolver(size=32)
        renderer = WorkspaceRenderer(resolver)
        view = WorkspaceView(
            target=WorkspaceTarget.parse("2"),
            workspace_id=2,
            name="2",
            monitor="",
            visual_state=WorkspaceVisualState.INACTIVE,
            windows=(),
        )
        image = renderer.render(
            view,
            WorkspaceRenderStyle.from_settings({"background_opacity": 25}),
        )
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.getpixel((48, 62)), (22, 24, 29, 64))

    def test_per_action_style_participates_in_render_cache(self):
        resolver = IconResolver(size=32)
        renderer = WorkspaceRenderer(resolver)
        view = WorkspaceView(
            target=WorkspaceTarget.parse("Web"),
            workspace_id=8,
            name="Web",
            monitor="",
            visual_state=WorkspaceVisualState.INACTIVE,
            windows=(),
        )
        first_style = WorkspaceRenderStyle.from_settings(
            {"title_color": [255, 80, 40, 255], "title_font": "sans"}
        )
        second_style = WorkspaceRenderStyle.from_settings(
            {"title_color": [40, 180, 255, 255], "title_font": "serif"}
        )
        first = renderer.render(view, first_style)
        second = renderer.render(view, second_style)
        self.assertNotEqual(first.tobytes(), second.tobytes())
        self.assertEqual(len(renderer._cache), 2)


if __name__ == "__main__":
    unittest.main()
