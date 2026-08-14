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
        # A caveat the *node* knows and the summary can't (see models.py's
        # listing of a 2.x drum kit). Captured per selection rather than
        # threaded through the worker, since the generation counter already
        # discards anything that arrives for a selection the user left.
        self._node_note = ""
        self.show_node(None)

    def show_node(self, node: TreeNode | None) -> None:
        self._gen += 1
        gen = self._gen
        self._node_note = node.note if node is not None else ""

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
            # The loop check is off unless the user turned it on: it reads the
            # PCM around every loop the preset touches, which is far more work
            # than summarising one. Config is loaded here rather than held,
            # so toggling it in Settings takes effect on the next selection.
            from ..config import Config
            self._run(summary.summarize_preset,
                      (bank, preset_obj, Config.load().loop_click_check),
                      gen, self._apply_preset)
        elif node.kind == "xpm":
            self._browser.setHtml("<i>Loading…</i>")
            self._run(xpm_import.summarize_xpm, (str(node.payload),), gen, self._apply_xpm)
        elif node.kind == "mpc_project":
            if node.handle is None:
                self._render_kv(f"MPC project ({node.format_label})",
                                 [("Size", human_size(node.size))],
                                 note="Expand this project in the tree to see its programs.")
            else:
                self._browser.setHtml("<i>Loading…</i>")
                self._run(xpm_import.summarize_project, (node.handle,), gen, self._apply_project)
        elif node.kind == "mpc_program":
            self._browser.setHtml("<i>Loading…</i>")
            path, preset_index = node.payload
            # The project node parsed the whole file to list its programs, so
            # summarise straight off that Bank -- re-reading every WAV per
            # click would cost seconds on a real project. Only a program with
            # no live project above it (there is no such row today, but a
            # search hit could grow one) falls back to its own parse.
            project = node.parent.handle if node.parent is not None else None
            if project is not None:
                self._run(xpm_import.summarize_program, (project, preset_index),
                          gen, self._apply_xpm)
            else:
                self._run(xpm_import.summarize_xpm, (str(path), None, preset_index),
                          gen, self._apply_xpm)
        elif node.kind in ("foreign_bank", "foreign_preset"):
            self._show_foreign(node, gen)
        elif node.kind == "unsupported":
            self._render_kv(node.format_label or "Unsupported format",
                             [("Name", node.label), ("Size", human_size(node.size))],
                             note=node.note or "Real content, but VinSamLib has no "
                                                "reader for this format yet.")
        else:
            self._browser.setHtml("")

    # A full zone table for an import source means parsing it, and parsing a
    # soundfont means reading every sample it holds. That is fine for the
    # small ones -- an .sfz or .exs is a few MB of external references -- and
    # not fine for a 1 GB SoundFont, where it would cost seconds and gigabytes
    # of RSS on a single click in the tree. Past this size the pane shows what
    # the header already told it and says how to see the rest.
    _PARSE_FOR_DETAIL_MAX = 64 << 20

    def _show_foreign(self, node: TreeNode, gen: int) -> None:
        from ..build import foreign_import
        title = f"{node.format_label} file" if node.kind == "foreign_bank" \
            else f"Instrument ({node.format_label})"
        if node.empty_reason:
            # Readable, and holding nothing this app can use. Say which,
            # rather than showing an empty zone table that looks like a bug.
            self._render_kv(title, [("Name", node.label),
                                    ("Size", human_size(node.size))],
                            note=node.empty_reason)
            return
        path = node.payload if node.kind == "foreign_bank" else node.payload[0]
        ordinal = None if node.kind == "foreign_bank" else node.payload[1]
        size = node.size or (node.parent.size if node.parent else 0)
        if size > self._PARSE_FOR_DETAIL_MAX:
            rows = [("Name", node.label), ("Size", human_size(size))]
            if node.kind == "foreign_bank" and node.children:
                rows.append(("Instruments", str(len(node.children))))
            self._render_kv(title, rows, note=(
                node.note or "") + ("<br>" if node.note else "") +
                "Large file — import it to read its zones.")
            return
        self._browser.setHtml("<i>Loading…</i>")
        self._run(foreign_import.summarize_foreign, (str(path), ordinal),
                  gen, self._apply_foreign)

    def _apply_foreign(self, gen: int, xs: xpm_import.XpmSummary) -> None:
        if gen != self._gen:
            return
        html = (f"<b>{_escape(xs.preset_name) or '(untitled)'}</b><br>"
                f"Samples: {xs.sample_count}<br>"
                f"Total sample size: {human_size(xs.total_sample_bytes)}<br><br>"
                f"{zone_stats_lines(xs.zones)}")
        self._browser.setHtml(html)

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
        voice_label = "Keymaps" if ps.format == "KRZ" else "Voices"
        html = (f"<b>Preset ({ps.format})</b><br>{voice_label}: {ps.voice_count}<br>"
                f"Total sample size: {human_size(ps.total_sample_bytes)}<br><br>"
                f"{zone_stats_lines(ps.zones)}")
        if ps.notes:
            import html as _html
            html += "<br><br>" + "<br>".join(
                f"<span style='color:#c07000'>⚠ {_html.escape(n)}</span>"
                for n in ps.notes)
        self._browser.setHtml(html)

    def _apply_xpm(self, gen: int, xs: xpm_import.XpmSummary) -> None:
        if gen != self._gen:
            return
        html = (f"<b>MPC Program</b><br>"
                f"Preset: {_escape(xs.preset_name) or '(untitled)'}<br>"
                f"Samples: {xs.sample_count}<br>"
                f"Total sample size: {human_size(xs.total_sample_bytes)}<br><br>"
                f"{zone_stats_lines(xs.zones)}")
        if self._node_note:
            html += f"<br><br><i>{_escape(self._node_note)}</i>"
        self._browser.setHtml(html)

    def _apply_project(self, gen: int, ps: xpm_import.ProjectSummary) -> None:
        # Deliberately the same shape _apply_bank() renders for a real E4B or
        # KRZ bank: a project IS the MPC's bank, and the programs listed here
        # are the rows the tree shows under it.
        if gen != self._gen:
            return
        names = ps.program_names[:30]
        more = len(ps.program_names) - len(names)
        html = (f"<b>MPC Project</b><br>"
                f"Programs: {len(ps.program_names)}<br>"
                f"Samples: {ps.sample_count}<br>"
                f"Total sample size: {human_size(ps.total_sample_bytes)}<br><br>" +
                "<br>".join(_escape(n) or "(untitled)" for n in names))
        if more > 0:
            html += f"<br><i>… {more} more</i>"
        self._browser.setHtml(html)

    def _apply_error(self, gen: int, message: str) -> None:
        if gen != self._gen:
            return
        last_line = workers.last_error_line(message)
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


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _range_or_single(values: tuple, unit: str) -> str:
    if not values:
        return ""
    if len(values) == 1:
        return f"{values[0]}{unit}"
    return f"{values[0]}–{values[-1]}{unit}"


def zone_stats_lines(zones: list) -> str:
    """Shared by DetailPane's _apply_preset (E4B/KRZ) and _apply_xpm, and
    reused by bank_pane.py's own New Bank selection info -- same
    ZoneSummary shape (banks/summary.py) either way. Condensed lines
    instead of one row per zone (a real preset can carry dozens): how many
    distinct key zones and how many distinct samples in total; how many
    distinct velocity layers and how many samples fall in each one (a
    range if that varies zone to zone, a single count if it doesn't); and
    bit depth/sample rate actually read off the samples themselves (a
    range if the preset mixes rates/depths, a single value if uniform)."""
    stats = summary.zone_stats(zones)
    if stats is None:
        return "<i>No zones.</i>"
    if stats.vel_samples_min == stats.vel_samples_max:
        vel_samples = _plural(stats.vel_samples_min, "sample")
    else:
        vel_samples = f"{stats.vel_samples_min}–{stats.vel_samples_max} samples"
    lines = [f"{_plural(stats.key_zone_count, 'key zone')} with "
             f"{_plural(stats.total_samples, 'sample')}",
             f"{_plural(stats.vel_layer_count, 'velocity layer')} with "
             f"{vel_samples} each"]
    format_bits = ", ".join(filter(None, [
        _range_or_single(stats.bit_depths, "-bit"),
        _range_or_single(stats.sample_rates, " Hz"),
    ]))
    if format_bits:
        lines.append(format_bits)
    return "<br>".join(lines)


def _escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
