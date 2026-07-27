"""
Drag-and-drop plumbing shared between the drag sources (the Explorer tree)
and the drop target (the New Bank column). Explorer and the New Bank column
live in the same QApplication, so the actual (bank, preset_obj) Python
objects ride along as a plain attribute on the QMimeData instance — PyQt
preserves object identity for same-process drags, so none of this needs
real serialization. The bytes under DRAG_MIME_TYPE carry just enough
(format, name) for a drop target to sanity-check a drag *before* it's
released — e.g. to reject a KRZ preset over an E4B-locked bank — without
touching the real objects; nothing is materialized until a drop is
actually accepted (`banks/*.assemble()` still does the real work).
"""

from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import QMimeData

DRAG_MIME_TYPE = "application/x-vinsamlib-items"


def build_mime_data(items: list[tuple[Any, Any, str, str]]) -> QMimeData:
    """items: list of (bank, preset_obj, format, name)."""
    mime = QMimeData()
    descriptor = [{"format": fmt, "name": name} for _bank, _preset, fmt, name in items]
    mime.setData(DRAG_MIME_TYPE, json.dumps(descriptor).encode("utf-8"))
    mime.vinsamlib_payload = [(bank, preset) for bank, preset, _fmt, _name in items]
    return mime


def descriptor_from(mime: QMimeData) -> list[dict]:
    if not mime.hasFormat(DRAG_MIME_TYPE):
        return []
    raw = bytes(mime.data(DRAG_MIME_TYPE))
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return []


def payload_from(mime: QMimeData) -> list[tuple[Any, Any]]:
    return getattr(mime, "vinsamlib_payload", [])
