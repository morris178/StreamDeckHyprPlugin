from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
import os
from pathlib import Path
import socket
import stat
import subprocess
import threading
from typing import Any


class HyprlandUnavailable(RuntimeError):
    pass


class HyprlandCommandError(RuntimeError):
    pass


def _runtime_dir(environ: Mapping[str, str]) -> Path:
    return Path(environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")


def discover_instance(environ: Mapping[str, str] | None = None) -> tuple[str, Path]:
    env = os.environ if environ is None else environ
    root = _runtime_dir(env) / "hypr"
    signature = env.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    candidates: list[Path] = []
    if signature:
        candidates.append(root / signature)
    if root.is_dir():
        try:
            candidates.extend(
                sorted(
                    (item for item in root.iterdir() if item.is_dir() and item.name != signature),
                    key=lambda item: item.stat().st_mtime,
                    reverse=True,
                )
            )
        except OSError:
            pass
    for candidate in candidates:
        event_socket = candidate / ".socket2.sock"
        command_socket = candidate / ".socket.sock"
        try:
            if stat.S_ISSOCK(event_socket.stat().st_mode) and stat.S_ISSOCK(command_socket.stat().st_mode):
                return candidate.name, candidate
        except OSError:
            continue
    raise HyprlandUnavailable("No current Hyprland IPC instance found")


class HyprlandTransport(ABC):
    is_flatpak = False

    @abstractmethod
    def query_json(self, command: str) -> Any:
        raise NotImplementedError

    @abstractmethod
    def dispatch_workspace(self, lua_selector: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def event_lines(self, stop_event: threading.Event) -> Iterator[tuple[str, str]]:
        """Yield ``(instance_signature, raw_event_line)`` tuples until disconnected."""
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError

    def load_host_icon(self, candidates: list[str]) -> dict | None:
        return None


class NativeTransport(HyprlandTransport):
    """Direct, process-free Hyprland socket transport for native StreamController."""

    def __init__(self, environ: Mapping[str, str] | None = None, timeout: float = 5.0):
        self.environ = os.environ if environ is None else environ
        self.timeout = timeout
        self._event_socket: socket.socket | None = None
        self._lock = threading.Lock()

    def _request(self, request: str) -> bytes:
        _, instance_dir = discover_instance(self.environ)
        path = str(instance_dir / ".socket.sock")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        chunks: list[bytes] = []
        try:
            sock.connect(path)
            sock.sendall(request.encode("utf-8"))
            sock.shutdown(socket.SHUT_WR)
            while True:
                chunk = sock.recv(65_536)
                if not chunk:
                    break
                chunks.append(chunk)
        except OSError as exc:
            raise HyprlandUnavailable(f"Hyprland IPC request failed: {exc}") from exc
        finally:
            sock.close()
        return b"".join(chunks)

    def query_json(self, command: str) -> Any:
        import json

        raw = self._request(f"j/{command}")
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HyprlandCommandError(f"Invalid JSON response for {command}: {exc}") from exc

    def dispatch_workspace(self, lua_selector: str) -> None:
        request = f"/dispatch hl.dsp.focus({{ workspace = {lua_selector} }})"
        result = self._request(request).decode("utf-8", errors="replace").strip()
        if result != "ok":
            raise HyprlandCommandError(result or "Hyprland rejected the workspace dispatcher")

    def event_lines(self, stop_event: threading.Event) -> Iterator[tuple[str, str]]:
        signature, instance_dir = discover_instance(self.environ)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with self._lock:
            self._event_socket = sock
        buffer = b""
        try:
            sock.connect(str(instance_dir / ".socket2.sock"))
            yield signature, ""
            while not stop_event.is_set():
                try:
                    chunk = sock.recv(65_536)
                except OSError as exc:
                    if stop_event.is_set():
                        return
                    raise HyprlandUnavailable(f"Hyprland event socket failed: {exc}") from exc
                if not chunk:
                    raise HyprlandUnavailable("Hyprland event socket closed")
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    yield signature, line.decode("utf-8", errors="replace")
        finally:
            with self._lock:
                if self._event_socket is sock:
                    self._event_socket = None
            sock.close()

    def close(self) -> None:
        with self._lock:
            sock = self._event_socket
            self._event_socket = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


class FlatpakTransport(HyprlandTransport):
    """Host-helper transport requiring only StreamController's existing Flatpak permission."""

    is_flatpak = True

    def __init__(self, helper_path: str, timeout: float = 10.0):
        self.helper_path = str(Path(helper_path).resolve())
        self.timeout = timeout
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()

    def _argv(self, *arguments: str) -> list[str]:
        return [
            "flatpak-spawn",
            "--host",
            "--watch-bus",
            "python3",
            self.helper_path,
            *arguments,
        ]

    def _run_json(self, *arguments: str) -> Any:
        import json

        try:
            completed = subprocess.run(
                self._argv(*arguments),
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd="/",
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            raise HyprlandUnavailable(f"Flatpak host helper failed: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
            raise HyprlandUnavailable(f"Flatpak host helper failed: {detail}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise HyprlandCommandError(f"Invalid host-helper response: {exc}") from exc
        if isinstance(payload, dict) and payload.get("error"):
            raise HyprlandCommandError(str(payload["error"]))
        return payload

    def query_json(self, command: str) -> Any:
        snapshot = self._run_json("snapshot")
        if command not in snapshot:
            raise HyprlandCommandError(f"Snapshot did not contain {command}")
        return snapshot[command]

    def snapshot(self) -> dict:
        return self._run_json("snapshot")

    def dispatch_workspace(self, lua_selector: str) -> None:
        result = self._run_json("switch", lua_selector)
        if result.get("result") != "ok":
            raise HyprlandCommandError(str(result.get("result") or "dispatcher failed"))

    def event_lines(self, stop_event: threading.Event) -> Iterator[tuple[str, str]]:
        import json

        try:
            process = subprocess.Popen(
                self._argv("events"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                cwd="/",
            )
        except (FileNotFoundError, OSError) as exc:
            raise HyprlandUnavailable(f"Could not start Flatpak host helper: {exc}") from exc
        with self._lock:
            self._process = process
        signature = ""
        try:
            assert process.stdout is not None
            for output_line in process.stdout:
                if stop_event.is_set():
                    return
                try:
                    payload = json.loads(output_line)
                except json.JSONDecodeError:
                    continue
                if payload.get("type") == "connected":
                    signature = str(payload.get("signature", ""))
                    yield signature, ""
                elif payload.get("type") == "event" and signature:
                    yield signature, str(payload.get("raw", ""))
                elif payload.get("type") == "error":
                    raise HyprlandUnavailable(str(payload.get("message", "host helper error")))
            if not stop_event.is_set():
                raise HyprlandUnavailable(f"Flatpak host helper exited with {process.wait()}")
        finally:
            with self._lock:
                if self._process is process:
                    self._process = None
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

    def load_host_icon(self, candidates: list[str]) -> dict | None:
        if not candidates:
            return None
        result = self._run_json("icon", *candidates)
        return result if result.get("data") else None

    def close(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
        if process is not None and process.poll() is None:
            process.terminate()


def running_under_flatpak(environ: Mapping[str, str] | None = None, info_path: str = "/.flatpak-info") -> bool:
    env = os.environ if environ is None else environ
    return bool(env.get("FLATPAK_ID")) or Path(info_path).exists()


def select_transport(
    helper_path: str,
    environ: Mapping[str, str] | None = None,
    flatpak_info_path: str = "/.flatpak-info",
) -> HyprlandTransport:
    if running_under_flatpak(environ, flatpak_info_path):
        return FlatpakTransport(helper_path)
    return NativeTransport(environ)
