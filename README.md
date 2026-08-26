# Hyprland Workspaces for StreamController

Event-driven Stream Deck workspace keys for current Hyprland. Each key switches to one numeric or
named workspace and shows whether it is focused, visible on another monitor, or inactive, together
with the applications currently present there.

## Features

- one central Hyprland service for every configured key
- direct Hyprland IPC; no polling and no `hyprctl` dependency
- short press switches workspace; long press moves the focused window there and follows it
- separate **Move focused window to workspace** action for dedicated move keys
- initial `clients`, `workspaces`, and `monitors` JSON snapshot
- live socket2 updates for workspace, window, focus, title, monitor, and reload events
- focused / visible / inactive multi-monitor states
- translucent state tint with per-key opacity control
- configurable workspace-label color, font family, weight, and size
- deduplicated app icons with window-count badges and `+N` overflow
- freedesktop desktop-entry and icon-theme lookup, including Flatpak exports
- local PWA/webapp icons for Chromium-family browsers instead of a generic browser icon
- automatic reconnect with bounded exponential backoff
- targeted subscriber updates and shared icon/render caches
- clean socket, thread, executor, and helper-process shutdown

## Supported versions

This release intentionally follows the newest Hyprland API and does not support legacy dispatcher
implementations.

- tested: StreamController `1.5.0-beta.16`
- tested: Hyprland `0.56.2`, Lua configuration provider
- required: StreamController `1.5.0-beta.16` or newer within the 1.x plugin API
- required: Hyprland `0.56.x` or newer with `configProvider: lua`

The switch dispatcher is the current Lua form:

```text
hl.dsp.focus({ workspace = 3 })
hl.dsp.focus({ workspace = "name:Web" })
```

Moving the focused window uses the current Lua window dispatcher:

```text
hl.dsp.window.move({ workspace = 3, follow = true })
hl.dsp.window.move({ workspace = "name:Web", follow = true })
```

## Installation

The plugin is not published in the StreamController store yet. Clone or place the complete project
folder in StreamController's `plugins` directory. The folder name may be arbitrary; keep its
contents together. The source repository is
[`morris178/StreamDeckHyprPlugin`](https://github.com/morris178/StreamDeckHyprPlugin).

For the Flatpak default data directory:

```text
~/.var/app/com.core447.StreamController/data/plugins/com_morris_HyprlandWorkspaces/
```

For a source build, use `<StreamController data directory>/plugins/`. A development launch with
`--data data` therefore uses `data/plugins/`.

Restart StreamController, add the **Hyprland Workspace** action to a key, and enter either:

```text
3
Web
name:Web
```

No helper, shell script, service, `hyprctl`, or extra Flatpak permission must be installed manually.

## Key actions

The normal **Hyprland Workspace** key deliberately waits until StreamController distinguishes a
short from a long press:

- short press: switch to the configured workspace
- long press: move Hyprland's currently focused window to the configured workspace and follow it

Following is enabled by default, so both gestures end on the workspace represented by the key. It
can be disabled per action with **Follow moved window** when background placement is preferred.

The separate **Move focused window to workspace** action performs the move on a normal short press.
It shares the same central service, target parsing, state subscription, rendering, and transport;
it does not open another socket or start another helper.

## Flatpak operation

The current StreamController manifest already permits host commands through
`org.freedesktop.Flatpak`. The plugin uses that existing permission to run its bundled transport
helper with `flatpak-spawn --host --watch-bus`. The helper talks directly to Hyprland's two IPC
sockets and streams NDJSON events back to the plugin. It exits on disconnect and is explicitly
terminated during plugin shutdown.

No permission override is required. In particular, the plugin does not request broad runtime-dir
access merely to reach Hyprland's sockets.

## Native/source operation

The native transport opens these sockets directly and starts no helper process:

```text
$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/.socket.sock
$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/.socket2.sock
```

If the environment signature is absent or stale, the newest valid runtime instance is discovered.

## Visual states

- green: workspace is active on the focused monitor (`focused`)
- blue: workspace is active on another monitor (`visible`)
- gray: workspace is not active on any monitor (`inactive`)
- red: Hyprland is unavailable or reconnecting

Apps with multiple windows are shown once with a count badge. More than four distinct apps are
collapsed to three icons plus `+N`. Icons are rendered directly on the workspace background without
additional cards or frames.

The colored state background is translucent by default, so the StreamController page background
remains visible. Each Workspace action can set its own background opacity from 0–100%, workspace
label color, font family (sans, condensed, serif, or monospace), weight, and preferred size. Long
workspace names still shrink automatically to fit the key.

## Webapp icons

Chromium-family app windows are distinguished from ordinary browser windows through their Hyprland
`class`/`initialClass`. The resolver supports Chrome, Chromium, Brave, and Edge and checks, in order:

1. an exact freedesktop desktop entry,
2. the locally installed PWA manifest icon,
3. the browser profile's local `Favicons` database,
4. the normal browser icon as a temporary fallback.

No website or external favicon service is contacted. Both native StreamController and its Flatpak
can read these per-user browser files with their existing permissions. If Chromium has not written
a newly opened webapp's favicon yet, the shared resolver performs three bounded background retries
and refreshes only workspace keys containing that webapp.

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for transport selection, snapshot/event ordering,
threading, caches, targeted updates, lifecycle, and trade-offs. Possible optional StreamController
core improvements are listed in [docs/UPSTREAM.md](docs/UPSTREAM.md).

## Tests

The state and transport layers use only the Python standard library. Renderer tests additionally
need Pillow, which StreamController already bundles:

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m unittest discover -s tests -t . -v
```

To execute against the exact installed Flatpak runtime without installing host dependencies:

```bash
flatpak run --command=sh com.core447.StreamController \
  -c 'cd /path/to/StreamDeckHyprPlugin && python3 -m unittest discover -s tests -t . -v'
```

Fixtures cover snapshots, event parsing, open/close/move, workspace lifecycle, multiple monitors,
resync, reconnect, duplicate/unknown events, transport selection, short/long-press gestures,
focused-window move/follow commands, shared subscriptions, icon
resolution, icon caching, Chromium webapp detection, local PWA/favicon lookup, targeted icon
refresh, app deduplication, and render caching.

## Troubleshooting

**The key is red**

Check that StreamController runs inside the current Hyprland session and that these return Lua and
0.56.x respectively:

```bash
hyprctl status -j
hyprctl version -j
```

The plugin itself does not require `hyprctl`; these commands are only convenient diagnostics.

**A named workspace does not switch**

Enter its exact, case-sensitive Hyprland name as `Web` or `name:Web`. Special workspaces and
relative selectors are deliberately outside this action's scope.

**An app uses the fallback letter icon**

Verify that its `.desktop` entry has an `Icon=` value and preferably `StartupWMClass=` matching
Hyprland's `class` or `initialClass`. The resolver also checks desktop IDs, names, executables, and
Flatpak exports.

**A webapp still uses the browser icon**

The window must have a webapp-specific Hyprland class such as `chrome-chatgpt.com__-Default` or a
`crx_<app-id>` class. A normal browser class such as `google-chrome` deliberately stays on the
browser icon because Hyprland does not expose the active tab URL. Newly created app windows may use
the browser icon briefly while the bounded local retries wait for Chromium to store their favicon.

**A custom key image prevents live rendering**

Remove the user-selected image for that action. StreamController intentionally gives a custom user
asset precedence over an action's dynamic image.

## Debug logging

StreamController writes its normal logs below its configured data directory, typically
`~/.var/app/com.core447.StreamController/data/logs/`. Search for `HyprlandWorkspaces`,
`Hyprland workspace`, or `host helper`. Normal connected operation is intentionally quiet; reconnect
failures are logged once per bounded retry.

## Known limitations

- no screenshots or workspace capture
- no special-workspace action
- no geometric mini-layout in version 1; app icons have priority
- no compatibility layer for pre-0.56 or legacy Hyprland dispatcher syntax
- desktop entries with no usable icon fall back to a neutral generated icon
- normal browser windows show the browser icon; Hyprland does not expose per-tab favicons

This project intentionally contains no Omarchy, Quickshell, or other-compositor integration.

## License

MIT. See [LICENSE](LICENSE).
