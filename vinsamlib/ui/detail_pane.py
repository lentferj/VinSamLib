"""
DetailPane: text summary of whatever's selected in the Explorer tree. Bank
and preset/program summaries are computed off the GUI thread (an E4B preset
summary reassembles + reparses through mpc2emu — see banks/summary.py) and
applied only if the selection hasn't moved on by the time the result comes
back (the generation-counter pattern below).
"""

from __future__ import annotations

from PySide6.QtWidgets import QTextBrowser, QVBoxLayout, QWidget

from . import workers
from .models import TreeNode, human_size
from ..banks import summary
from ..build import xpm_import


class DetailPane(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        self._browser = QTextBrowser()
        layout.addWidget(self._browser)
        self._gen = 0
        self._live_workers: list[workers.Worker] = []
        self.show_node(None)

    def show_node(self, node: TreeNode | None) -> None:
        self._gen += 1
        gen = self._gen

        if node is None:
            self._browser.setHtml("<i>Nothing selected.</i>")
        elif node.kind == "directory":
            self._render_kv("Directory", [("Path", node.payload)])
        elif node.kind == "volume_root":
            kind = type(node.handle).__name__ if node.handle else "Image"
            self._render_kv(kind, [("Size", human_size(node.size)), ("Path", str(node.payload))])
        elif node.kind == "folder":
            self._render_kv("In-image folder", [("Name", node.label)])
        elif node.kind == "bank":
            if node.handle is None:
                self._render_kv(f"Bank{f' ({node.format_label})' if node.format_label else ''}",
                                 [("Size", human_size(node.size))],
                                 note="Expand this bank in the tree to see its contents.")
            else:
                self._browser.setHtml("<i>Loading…</i>")
                self._run(summary.summarize_bank, (node.handle,), gen, self._apply_bank)
        elif node.kind == "preset":
            self._browser.setHtml("<i>Loading…</i>")
            bank, preset_obj = node.payload
            self._run(summary.summarize_preset, (bank, preset_obj), gen, self._apply_preset)
        elif node.kind == "xpm":
            self._browser.setHtml("<i>Loading…</i>")
            self._run(xpm_import.summarize_xpm, (str(node.payload),), gen, self._apply_xpm)
        else:
            self._browser.setHtml("")

    # -- rendering ------------------------------------------------------------

    def _render_kv(self, title: str, rows: list[tuple[str, str]], note: str = "") -> None:
        html = f"<b>{title}</b><br>" + "".join(f"{k}: {v}<br>" for k, v in rows)
        if note:
            html += f"<br><i>{note}</i>"
        self._browser.setHtml(html)

    def _apply_bank(self, gen: int, bs: summary.BankSummary) -> None:
        if gen != self._gen:
            return
        names = bs.preset_names[:30]
        more = len(bs.preset_names) - len(names)
        html = (f"<b>Bank ({bs.format})</b><br>"
                f"Presets: {bs.preset_count}<br>"
                f"Samples: {bs.sample_count}<br>"
                f"Size: {human_size(bs.total_size)}<br><br>" +
                "<br>".join(_escape(n) or "(untitled)" for n in names))
        if more > 0:
            html += f"<br><i>… {more} more</i>"
        self._browser.setHtml(html)

    def _apply_preset(self, gen: int, ps: summary.PresetSummary) -> None:
        if gen != self._gen:
            return
        voice_label = "Voices" if ps.format == "E4B" else "Keymaps"
        html = (f"<b>Preset ({ps.format})</b><br>{voice_label}: {ps.voice_count}<br>"
                f"Total sample size: {human_size(ps.total_sample_bytes)}<br><br>"
                f"{_zone_table(ps.zones)}")
        self._browser.setHtml(html)

    def _apply_xpm(self, gen: int, xs: xpm_import.XpmSummary) -> None:
        if gen != self._gen:
            return
        html = (f"<b>XPM Program</b><br>"
                f"Preset: {_escape(xs.preset_name) or '(untitled)'}<br>"
                f"Samples: {xs.sample_count}<br>"
                f"Total sample size: {human_size(xs.total_sample_bytes)}<br><br>"
                f"{_zone_table(xs.zones)}")
        self._browser.setHtml(html)

    def _apply_error(self, gen: int, message: str) -> None:
        if gen != self._gen:
            return
        last_line = message.strip().splitlines()[-1] if message else "error"
        self._browser.setHtml(f"<i>Failed to load: {_escape(last_line)}</i>")

    # -- background work --------------------------------------------------------

    def _run(self, fn, args, gen: int, on_done) -> None:
        w = workers.Worker(fn, *args)
        w.signals.finished.connect(lambda result, g=gen: on_done(g, result))
        w.signals.error.connect(lambda msg, g=gen: self._apply_error(g, msg))
        w.signals.finished.connect(lambda *_: self._live_workers.remove(w) if w in self._live_workers else None)
        w.signals.error.connect(lambda *_: self._live_workers.remove(w) if w in self._live_workers else None)
        self._live_workers.append(w)
        workers.run(w)


def _zone_table(zones: list) -> str:
    """Shared by _apply_preset (E4B/KRZ) and _apply_xpm -- same
    ZoneSummary shape (banks/summary.py) either way, so both get the same
    sample/key/vel/root/loop table rather than XPM getting a lesser view."""
    if not zones:
        return "<i>No zones.</i>"
    rows = "".join(
        f"<tr><td>{_escape(z.sample_name)}</td><td>{z.lo_key}–{z.hi_key}</td>"
        f"<td>{z.lo_vel}–{z.hi_vel}</td><td>{z.root_key}</td><td>{z.loop}</td></tr>"
        for z in zones
    )
    return ("<table cellspacing='4' cellpadding='2'>"
            "<tr><th align='left'>Sample</th><th align='left'>Key</th>"
            "<th align='left'>Vel</th><th align='left'>Root</th><th align='left'>Loop</th></tr>"
            f"{rows}</table>")


def _escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
