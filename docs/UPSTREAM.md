# Possible StreamController upstream improvements

The plugin works with an unmodified StreamController `1.5.0-beta.16`. No core patch is required.

Small public APIs would nevertheless simplify this and similar plugins:

1. A documented plugin lifecycle callback (`on_start`, `on_stop`) that is guaranteed for app
   shutdown, plugin disable, uninstall, and reload. This would replace the current `AppQuit`
   signal plus `on_uninstall` fallback.
2. A public host-command abstraction wrapping `flatpak-spawn --host --watch-bus`, argument passing,
   timeout, cancellation, and child cleanup.
3. An optional shared compositor service exposing immutable workspace/window snapshots and keyed
   subscriptions. It should remain optional so plugins can support newer compositor APIs without
   waiting for a StreamController release.

The existing internal Hyprland window-grabber listener is intentionally not imported: it models
only active-window changes, has a polling fallback, and is not a stable plugin API.
