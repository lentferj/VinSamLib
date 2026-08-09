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


# ── dragging something that is not a preset yet ────────────────────────────
#
# A soundfont-style source (SF2, SFZ, EXS24, TAL, GIG) can be dragged into
# New Bank too, but it cannot travel the way a real preset does: there is no
# (bank, preset_obj) pair to carry, because none exists until mpc2emu has
# converted the thing. What such a drag carries is a *request* -- "import
# this file, this entry of it" -- which MainWindow answers asynchronously
# with the Convert Options dialog and a worker.
#
# Deliberately a SECOND mime type rather than a variant of the descriptor
# above. BankPane._acceptable() reads descriptor["format"] as the format of
# the bank being built and rejects an empty one; an import request has no
# such format until the user picks a target in the dialog, so it must not
# occupy that field. With a separate type every existing check stays exactly
# as it was, and "is this a preset or a request?" is one hasFormat() call.

IMPORT_MIME_TYPE = "application/x-vinsamlib-import"


def build_import_mime_data(requests: list[dict]) -> QMimeData:
    """requests: list of {"path", "format", "ordinal", "name"} dicts.

    Nothing but JSON rides along -- unlike a preset drag there are no live
    Python objects to preserve, which also means such a drag would survive
    leaving the process if it ever needed to.
    """
    mime = QMimeData()
    mime.setData(IMPORT_MIME_TYPE, json.dumps(requests).encode("utf-8"))
    return mime


def import_requests_from(mime: QMimeData) -> list[dict]:
    if not mime.hasFormat(IMPORT_MIME_TYPE):
        return []
    raw = bytes(mime.data(IMPORT_MIME_TYPE))
    try:
        found = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return []
    return [r for r in found if isinstance(r, dict) and r.get("path")]


def has_import_request(mime: QMimeData) -> bool:
    return mime.hasFormat(IMPORT_MIME_TYPE)
