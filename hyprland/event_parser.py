from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HyprlandEvent:
    name: str
    fields: tuple[str, ...]
    raw: str


def parse_event_line(line: str | bytes) -> HyprlandEvent | None:
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    raw = line.rstrip("\r\n")
    if not raw or ">>" not in raw:
        return None
    name, payload = raw.split(">>", 1)
    name = name.strip().lower()
    if not name:
        return None
    return HyprlandEvent(name=name, fields=tuple(payload.split(",")), raw=raw)


def int_field(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
