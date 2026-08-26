# Architecture

## Compatibility baseline

This version deliberately targets the current stack as of 26 August 2026:

- StreamController `1.5.0-beta.16`
- Hyprland `0.56.x` with the Lua configuration provider
- the current socket2 event protocol

It does not carry a legacy Hyprland dispatcher implementation. Workspace changes use:

```text
hl.dsp.focus({ workspace = ... })
```

The command is sent directly to `.socket.sock`. Numeric workspaces are Lua numbers; named
workspaces are escaped strings in `name:Name` form.

Focused-window moves use:

```text
hl.dsp.window.move({ workspace = ..., follow = true })
```

## Ownership and data flow

```text
WorkspaceAction(s)
      │ subscribe / switch / move focused window
      ▼
WorkspaceService (one per plugin object)
      │
      ├── WorkspaceState
      │     ├── workspaces
      │     ├── windows
      │     └── monitors
      │
      ├── keyed subscribers (id:N / name:Name)
      └── HyprlandBackend
             ├── NativeTransport  ── direct Unix sockets
             └── FlatpakTransport ── flatpak-spawn host helper
```

`WorkspaceState` has no StreamController, GTK, process, or socket dependencies. Snapshot and
event tests therefore run without a compositor.

## Startup and reconnect ordering

The listener connects to socket2 before requesting the snapshot. Events produced while the
snapshot is collected remain buffered by the event socket and are applied afterward. This
avoids the usual snapshot/listener race.

On disconnect the service clears its connected status and retries with exponential backoff and
jitter (0.5 to 30 seconds). Every connection, changed instance signature, config reload, monitor
topology event, or detected inconsistency causes a complete resync. There is no periodic polling.

## Flatpak transport

StreamController's current manifest already grants `org.freedesktop.Flatpak`, which permits
`flatpak-spawn --host`. The plugin starts its bundled, stdlib-only helper as:

```text
flatpak-spawn --host --watch-bus python3 helpers/hyprland_event_helper.py events
```

The helper is run from the plugin directory and is not installed on the host. `--watch-bus`
provides a second lifecycle guard if StreamController disappears. The plugin also explicitly
terminates and waits for it during shutdown. Snapshots and dispatches use short-lived modes of
the same helper and direct Hyprland IPC, so host `hyprctl` is not required.

For icons, the host helper may read the selected icon file and return it as base64. This is
needed because host icon themes and Flatpak export directories are not all reliably visible
inside the sandbox. Results are cached in the plugin.

Chromium-family webapps use a plugin-local resolver before the generic browser fallback. Exact
desktop entries and locally installed PWA manifest resources are preferred; URL-style app windows
then resolve against the read-only per-profile Chromium `Favicons` SQLite database. Chrome,
Chromium, Brave, Edge, multiple profiles, and Flatpak browser profile locations are considered.
The database is opened in immutable/read-only mode to avoid contending with the running browser.
No network favicon service is used. A temporary browser fallback has a bounded cache lifetime so a
favicon written shortly after `openwindow` can replace it. The shared resolver makes at most three
backed-off retries (5, 10, and 20 seconds), then targets only subscribed workspaces containing that
webapp. Retry timers are cancelled during plugin shutdown; this is finite recovery, not polling.

## Rendering and performance

Socket reads and commands never run on GTK's main thread. App/icon lookup and PIL rendering use
a shared two-worker render scheduler. A 40 ms per-action debounce coalesces event bursts. The
final `ActionCore.set_media(PIL.Image)` call is marshalled through `GLib.idle_add`.

The service maps subscribers by canonical workspace key. A move from workspace 2 to 4 only
notifies subscribers for 2 and 4. Render and icon caches suppress identical work. Window-title
events update the internal model but do not render in version 1 because titles are not displayed.

Idle work consists of a blocking socket read; there is no timer or polling loop.

## Command gestures

The single command executor also serializes focused-window moves. A normal Workspace action maps
`SHORT_UP` to `hl.dsp.focus(...)` and `HOLD_START` to
`hl.dsp.window.move({ workspace = ..., follow = true })`. Waiting for the resolved gesture is
important: switching on key-down would change focus before the hold action could move the intended
window. The dedicated move action invokes the same move method on `SHORT_UP`.

Both native and Flatpak calls return immediately to StreamController while IPC runs in the
background. Hyprland stays the source of truth; socket2 move and workspace events update the source
and destination subscribers without an optimistic state change.

## Visual semantics

- `focused`: active workspace of the monitor whose `focused` field is true.
- `visible`: active workspace of another monitor.
- `inactive`: not active on any monitor.

App entries are deduplicated by normalized `initialClass`/`class`. A badge shows repeated windows.
The renderer displays four app groups, or three groups plus `+N` overflow when there are more.
Its render-cache key includes the icon resolver revision, preventing a temporary browser fallback
from hiding a favicon that becomes available later.

## Trade-offs

- Version 1 renders app icons, not screenshots or a geometric mini-layout. Window geometry is
  retained in the model so a later optional layout renderer does not require a transport change.
- A monitor topology or workspace-to-monitor move triggers a snapshot because socket2 does not
  carry enough authoritative data to reconstruct every monitor's complete active state safely.
- The plugin intentionally uses StreamController's public action rendering surface. The sole
  lifecycle workaround is subscribing to `AppQuit`, because there is currently no dedicated
  public plugin-unload callback.

## Upstream material reviewed

The implementation was checked against StreamController main commit
`745b4940ecdd35adbfedecac62ce1609f09c3dfd` and the locally installed
`1.5.0-beta.16` Flatpak, plus Hyprland `0.56.2` (`efb50993`). Relevant primary sources:

- [StreamController ActionCore](https://github.com/StreamController/StreamController/blob/main/src/backend/PluginManager/ActionCore.py)
- [StreamController PluginBase](https://github.com/StreamController/StreamController/blob/main/src/backend/PluginManager/PluginBase.py)
- [StreamController Hyprland integration](https://github.com/StreamController/StreamController/blob/main/src/backend/WindowGrabber/Integrations/Hyprland.py)
- [StreamController Flatpak manifest](https://github.com/StreamController/StreamController/blob/main/com.core447.StreamController.yml)
- [Hyprland IPC and socket2 events](https://wiki.hypr.land/IPC/)
- [current Hyprland dispatchers](https://wiki.hypr.land/Configuring/Basics/Dispatchers/)
- [current hyprctl command contract](https://github.com/hyprwm/Hyprland/blob/main/docs/hyprctl.1.rst)
- [flatpak-spawn command reference](https://docs.flatpak.org/en/latest/flatpak-command-reference.html#flatpak-spawn)
