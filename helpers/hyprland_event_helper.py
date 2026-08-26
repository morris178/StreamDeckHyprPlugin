#!/usr/bin/env python3
"""Tiny stdlib-only host bridge used by the StreamController Flatpak.

It is executed with ``flatpak-spawn --host --watch-bus`` and is never installed.
The helper performs transport work only: direct IPC, event forwarding, and reading
an icon file that the sandbox cannot necessarily see.
"""

from __future__ import annotations

import base64
import configparser
import json
import os
from pathlib import Path
import socket
import stat
import sys


def emit(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def discover_instance() -> tuple[str, Path]:
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    root = runtime / "hypr"
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    candidates: list[Path] = [root / signature] if signature else []
    if root.is_dir():
        try:
            candidates.extend(
                sorted(
                    (path for path in root.iterdir() if path.is_dir() and path.name != signature),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
            )
        except OSError:
            pass
    for candidate in candidates:
        try:
            if stat.S_ISSOCK((candidate / ".socket.sock").stat().st_mode) and stat.S_ISSOCK(
                (candidate / ".socket2.sock").stat().st_mode
            ):
                return candidate.name, candidate
        except OSError:
            continue
    raise RuntimeError("No current Hyprland IPC instance found on host")


def request(payload: str) -> bytes:
    _, instance = discover_instance()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5.0)
    chunks: list[bytes] = []
    try:
        sock.connect(str(instance / ".socket.sock"))
        sock.sendall(payload.encode("utf-8"))
        sock.shutdown(socket.SHUT_WR)
        while True:
            chunk = sock.recv(65_536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        sock.close()
    return b"".join(chunks)


def query_json(command: str):
    return json.loads(request(f"j/{command}"))


def snapshot() -> None:
    emit(
        {
            "clients": query_json("clients"),
            "workspaces": query_json("workspaces"),
            "monitors": query_json("monitors"),
            "status": query_json("status"),
            "version": query_json("version"),
        }
    )


def switch(lua_selector: str) -> None:
    result = request(f"/dispatch hl.dsp.focus({{ workspace = {lua_selector} }})")
    emit({"result": result.decode("utf-8", errors="replace").strip()})


def move(lua_selector: str, follow: bool) -> None:
    lua_follow = "true" if follow else "false"
    result = request(
        "/dispatch hl.dsp.window.move({ "
        f"workspace = {lua_selector}, follow = {lua_follow} "
        "})"
    )
    emit({"result": result.decode("utf-8", errors="replace").strip()})


def events() -> None:
    signature, instance = discover_instance()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(instance / ".socket2.sock"))
    emit({"type": "connected", "signature": signature})
    buffer = b""
    try:
        while True:
            chunk = sock.recv(65_536)
            if not chunk:
                raise RuntimeError("Hyprland event socket closed")
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                emit({"type": "event", "raw": line.decode("utf-8", errors="replace")})
    finally:
        sock.close()


def normalized(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def data_roots() -> list[Path]:
    home = Path.home()
    roots = [
        Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share"),
        Path("/usr/local/share"),
        Path("/usr/share"),
        home / ".local/share/flatpak/exports/share",
        Path("/var/lib/flatpak/exports/share"),
    ]
    roots.extend(Path(value) for value in os.environ.get("XDG_DATA_DIRS", "").split(":") if value)
    result: list[Path] = []
    for root in roots:
        if root not in result and root.is_dir():
            result.append(root)
    return result


def desktop_score(path: Path, section: configparser.SectionProxy, candidates: set[str]) -> int:
    values = [
        path.stem,
        section.get("StartupWMClass", ""),
        section.get("X-Flatpak", ""),
        section.get("Name", ""),
        Path(section.get("Exec", "").split(" ", 1)[0]).name,
    ]
    score = 0
    for index, value in enumerate(values):
        token = normalized(value)
        if not token:
            continue
        if token in candidates:
            score = max(score, 100 - index * 5)
        elif any(token in candidate or candidate in token for candidate in candidates if len(candidate) >= 4):
            score = max(score, 50 - index * 3)
    return score


def find_icon_file(icon: str, roots: list[Path]) -> Path | None:
    path = Path(icon)
    if path.is_absolute() and path.is_file():
        return path
    extensions = (".png", ".svg", ".xpm")
    for root in roots:
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
    # Theme fallback is deliberately last because it scans the filesystem.
    wanted = {f"{icon}{extension}" for extension in extensions}
    for root in roots:
        icon_root = root / "icons"
        if not icon_root.is_dir():
            continue
        for directory, _, files in os.walk(icon_root):
            match = next((name for name in files if name in wanted), None)
            if match:
                return Path(directory) / match
    return None


def icon(candidates: list[str]) -> None:
    candidate_tokens = {normalized(candidate.removeprefix("name:")) for candidate in candidates}
    candidate_tokens.discard("")
    roots = data_roots()
    best: tuple[int, str] = (0, "")
    for root in roots:
        applications = root / "applications"
        if not applications.is_dir():
            continue
        for desktop_file in applications.rglob("*.desktop"):
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                parser.read(desktop_file, encoding="utf-8")
                section = parser["Desktop Entry"]
            except (OSError, KeyError, configparser.Error, UnicodeError):
                continue
            score = desktop_score(desktop_file, section, candidate_tokens)
            if score > best[0] and section.get("Icon"):
                best = score, section.get("Icon", "")
    icon_path = find_icon_file(best[1], roots) if best[1] else None
    if icon_path is None:
        emit({})
        return
    try:
        data = base64.b64encode(icon_path.read_bytes()).decode("ascii")
    except OSError:
        emit({})
        return
    emit({"data": data, "suffix": icon_path.suffix.lower(), "name": best[1]})


def main(argv: list[str]) -> int:
    try:
        command = argv[1] if len(argv) > 1 else ""
        if command == "snapshot":
            snapshot()
        elif command == "switch" and len(argv) == 3:
            switch(argv[2])
        elif command == "move" and len(argv) == 4 and argv[3] in {"true", "false"}:
            move(argv[2], argv[3] == "true")
        elif command == "events":
            events()
        elif command == "icon" and len(argv) > 2:
            icon(argv[2:])
        else:
            raise RuntimeError("Invalid helper invocation")
        return 0
    except Exception as exc:  # Last-resort transport error boundary.
        emit({"type": "error", "error": str(exc), "message": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
