from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from hyprland.models import Window, WorkspaceTarget, WorkspaceView, WorkspaceVisualState
from rendering.workspace_renderer import WorkspaceRenderer
from services.icon_resolver import IconResolver


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


class RendererTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
