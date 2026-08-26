from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time
from typing import Callable

from PIL import Image

try:
    from ..hyprland.models import WorkspaceView
    from ..rendering.workspace_renderer import WorkspaceRenderer, WorkspaceRenderStyle
except ImportError:  # Direct source-tree test import.
    from hyprland.models import WorkspaceView
    from rendering.workspace_renderer import WorkspaceRenderer, WorkspaceRenderStyle


class RenderScheduler:
    """Shared targeted render pool with per-action burst coalescing."""

    def __init__(self, renderer: WorkspaceRenderer, debounce_seconds: float = 0.04):
        self.renderer = renderer
        self.debounce_seconds = debounce_seconds
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="WorkspaceRender")
        self._lock = threading.Lock()
        self._latest: dict[
            object,
            tuple[WorkspaceView, WorkspaceRenderStyle, Callable[[Image.Image, WorkspaceView], None]],
        ] = {}
        self._active: set[object] = set()
        self._stopped = False

    def schedule(
        self,
        owner: object,
        view: WorkspaceView,
        style: WorkspaceRenderStyle,
        callback: Callable[[Image.Image, WorkspaceView], None],
    ) -> None:
        with self._lock:
            if self._stopped:
                return
            self._latest[owner] = (view, style, callback)
            if owner in self._active:
                return
            self._active.add(owner)
            self._executor.submit(self._work, owner)

    def cancel(self, owner: object) -> None:
        with self._lock:
            self._latest.pop(owner, None)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._latest.clear()
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _work(self, owner: object) -> None:
        time.sleep(self.debounce_seconds)
        while True:
            with self._lock:
                item = self._latest.pop(owner, None)
                if item is None or self._stopped:
                    self._active.discard(owner)
                    return
            view, style, callback = item
            image = self.renderer.render(view, style)
            try:
                callback(image, view)
            except Exception:
                pass
            with self._lock:
                if owner not in self._latest or self._stopped:
                    self._active.discard(owner)
                    return
            time.sleep(self.debounce_seconds)
