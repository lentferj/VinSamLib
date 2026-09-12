"""How much audio a REFERENCE-style instrument actually needs.

Some formats keep their samples inside the file (SF2, GIG, and every hardware
bank here), so the file's own size is what loading it costs. Others are a
small text or XML document naming WAVs that live beside them, and there the
file size is the one number that cannot answer the question -- an MPC keygroup
program of 999 KB pulled in 20.2 MB of audio, and a 21.8 KB TAL preset named
80.6 MB of it.

This resolves those references and adds up what they point at, WITHOUT
reading any audio: it stats the files. That is what makes it affordable in
the library scan, which is where it runs -- once per file, rather than every
time a folder is expanded.

Deduped by resolved path, because a multisample routinely names one WAV from
several zones and the sampler loads it once.
"""

from __future__ import annotations

import re
import wave
from pathlib import Path
from typing import Optional

#: `<SampleName>Foo-024 C0</SampleName>` in MPC keygroup/drum XML. The audio
#: sits beside the program as `<name>.WAV`, in whatever case the pack used.
_XPM_SAMPLE_NAME = re.compile(rb"<SampleName>([^<]+)</SampleName>")

#: `url="..\Crystal Bellz\Crystal Bellz (0).wav"` in a TAL preset -- written
#: the way Windows wrote it, which is why the separators are normalised. The
#: same trap build/foreign_import._stage_tal_samples() exists for.
_TAL_URL = re.compile(rb'url="([^"]+)"')

_AUDIO_EXTS = (".wav", ".WAV", ".aif", ".aiff", ".AIF", ".AIFF")


#: What every sampler here stores. The figure this module reports is what
#: the audio becomes on the instrument, not what it weighs on disk -- a 24-bit
#: WAV loses a third on the way in, and reporting the file size claimed 9.3 MB
#: where the converter's own summary said 6.2 MB for the same five samples.
_TARGET_BYTES_PER_SAMPLE = 2


def _audio_bytes(path: Path) -> int:
    """What this audio file becomes once loaded, in bytes.

    frames x channels x 2. The header is enough to know that, so this opens
    each file but reads no audio -- `wave` parses the chunk table and stops.
    Falls back to the file's own size for anything it cannot parse, which is
    the old behaviour and never worse than silence.

    Bit depth is the only conversion folded in here. Channel count is not:
    a stereo sample stays two channels' worth of bytes whether the target
    keeps it as one stereo sample (E4B) or splits it into an -L/-R pair
    (AKAI). Sample RATE is not either -- nothing is resampled unless the
    user asks, and that is a Convert Options choice this cannot see.
    """
    try:
        with wave.open(str(path)) as w:
            return w.getnframes() * w.getnchannels() * _TARGET_BYTES_PER_SAMPLE
    except Exception:
        try:
            return path.stat().st_size
        except OSError:
            return 0


def _stat_total(paths) -> int:
    return sum(_audio_bytes(p) for p in paths)


def _xpm_audio(path: Path, blob: bytes) -> int:
    here = path.parent
    found = set()
    for raw in _XPM_SAMPLE_NAME.findall(blob):
        name = raw.decode("utf-8", "replace").strip()
        if not name:
            continue
        for ext in _AUDIO_EXTS:
            cand = here / f"{name}{ext}"
            if cand.exists():
                found.add(cand)
                break
    return _stat_total(found)


def _tal_audio(path: Path, blob: bytes) -> int:
    here = path.parent
    found = set()
    for raw in _TAL_URL.findall(blob):
        url = raw.decode("utf-8", "replace").strip()
        if not url:
            continue
        cand = here / Path(url.replace("\\", "/"))
        try:
            cand = cand.resolve()
        except OSError:
            continue
        if cand.exists():
            found.add(cand)
    return _stat_total(found)


#: Extensions this module can answer for. SFZ and EXS24 are reference-style
#: too and are deliberately NOT here yet: their sample references need their
#: own syntax handled (an SFZ has include/define and per-region default paths),
#: and a half-right figure is worse than none. SF2 and GIG are absent for the
#: opposite reason -- they EMBED their audio, so their file size is already
#: the honest answer.
HANDLED = {".xpm": _xpm_audio, ".xty": _xpm_audio, ".xpj": _xpm_audio,
           ".talsmpl": _tal_audio}


def referenced_audio_bytes(path: str) -> Optional[int]:
    """Bytes of audio the instrument at `path` references, or None.

    None means "not a format this can answer for", which is a different thing
    from 0 -- 0 is a real instrument whose references all failed to resolve,
    and that is worth showing as "no audio" rather than as silence.
    """
    p = Path(path)
    fn = HANDLED.get(p.suffix.lower())
    if fn is None:
        return None
    try:
        blob = p.read_bytes()
    except OSError:
        return None
    try:
        return fn(p, blob)
    except Exception:
        return None
