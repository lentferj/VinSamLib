"""
88-key (A0-C8) piano keyboard widget for the Sample Placement dialog:
paints real white/black key shapes and overlays each sample's key range
in that sample's own color (same colors the matrix table uses), with a
small dark marker on each zone's root note. Read-only -- the matrix's
spinboxes are the only inputs; this is purely a visual cross-check.

Standard 88-key layout: 52 white keys, 36 black keys, starting at A0
(MIDI 21) through C8 (MIDI 108). Each of the 12 pitch classes maps to a
fixed position in "white-key-width" units (integers for white keys,
half-integers for the black keys between them, with the two natural
gaps -- no black key between B/C or E/F) so any note's x position is a
pure function of its MIDI number, no per-instance key list needed.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .note_naming import midi_to_name

LOW = 21    # A0
HIGH = 108  # C8
WHITE_KEY_COUNT = 52

_WHITE_POS = {0: 0.0, 1: 0.5, 2: 1.0, 3: 1.5, 4: 2.0, 5: 3.0, 6: 3.5,
              7: 4.0, 8: 4.5, 9: 5.0, 10: 5.5, 11: 6.0}
_WHITE_PCS = frozenset((0, 2, 4, 5, 7, 9, 11))


def is_white(midi: int) -> bool:
    return (midi % 12) in _WHITE_PCS


def _abs_white_pos(midi: int) -> float:
    return 7 * (midi // 12) + _WHITE_POS[midi % 12]


_ORIGIN = _abs_white_pos(LOW)


class PianoKeyboardWidget(QWidget):
    def __init__(self, octave_offset: int = 1, parent=None):
        super().__init__(parent)
        self._octave_offset = octave_offset
        self._zones: list[dict] = []   # [{"lo","root","hi","color"}, ...]
        self.setMinimumHeight(90)
        self.setMinimumWidth(WHITE_KEY_COUNT * 12)

    def set_zones(self, zones: list[dict]) -> None:
        self._zones = zones
        self.update()

    def _white_key_width(self) -> float:
        return self.width() / WHITE_KEY_COUNT

    def _key_rect(self, midi: int, white_w: float, height: int) -> QRectF:
        x = (_abs_white_pos(midi) - _ORIGIN) * white_w
        if is_white(midi):
            return QRectF(x, 0, white_w, height)
        black_w = white_w * 0.62
        black_h = height * 0.62
        return QRectF(x - black_w / 2, 0, black_w, black_h)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        white_w = self._white_key_width()
        h = self.height()

        # 1) white key bases
        p.setPen(QPen(QColor(110, 110, 110)))
        p.setBrush(QColor(255, 255, 255))
        for midi in range(LOW, HIGH + 1):
            if is_white(midi):
                p.drawRect(self._key_rect(midi, white_w, h))

        # 2) black key bases, on top of the whites they overlap
        p.setPen(QPen(QColor(30, 30, 30)))
        p.setBrush(QColor(15, 15, 15))
        for midi in range(LOW, HIGH + 1):
            if not is_white(midi):
                p.drawRect(self._key_rect(midi, white_w, h))

        # 3) small "C" labels inside the bottom of each white C key
        p.setPen(QColor(150, 150, 150))
        font = p.font()
        font.setPointSizeF(max(6.0, white_w * 0.42))
        p.setFont(font)
        for midi in range(LOW, HIGH + 1):
            if midi % 12 == 0:
                r = self._key_rect(midi, white_w, h)
                p.drawText(r.adjusted(1, 0, -1, -3),
                           Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
                           midi_to_name(midi, self._octave_offset))

        # 4) per-sample range overlays, drawn last so they tint both
        #    white and black keys alike (overlapping zones visibly blend,
        #    a bonus cue on top of the matrix's red-highlighted rows).
        p.setPen(Qt.PenStyle.NoPen)
        for zone in self._zones:
            color = QColor(zone["color"])
            color.setAlpha(150)
            p.setBrush(color)
            lo = max(LOW, zone["lo"])
            hi = min(HIGH, zone["hi"])
            for midi in range(lo, hi + 1):
                p.drawRect(self._key_rect(midi, white_w, h))

        # 5) root-note markers, on top of the overlay
        p.setBrush(QColor(20, 20, 20))
        for zone in self._zones:
            root = zone["root"]
            if not (LOW <= root <= HIGH):
                continue
            r = self._key_rect(root, white_w, h)
            marker = QRectF(r.center().x() - r.width() * 0.16, r.bottom() - r.height() * 0.2,
                             r.width() * 0.32, r.height() * 0.15)
            p.drawRect(marker)

        p.end()
