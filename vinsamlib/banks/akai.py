"""
AKAI S1000/S3000-series container: parse and assemble at the raw-file level.

An AKAI "bank" is not a file — it is a **volume**, a flat set of files on a
disk (or in a folder), where each program (`.P3`) names the samples (`.S3`)
it plays and both live side by side. That is the one structural difference
from E4B/KRZ/EIII, and it drives everything below: `AkaiBank` holds the
files of one volume, `assemble()` returns a *list of files* rather than one
blob, and the container that carries them (a hard disk, a CD3000 disc, a
floppy) is a separate concern — see `vfs/akai.py` to read one and
`build/akai_image.py` to write one.

The format is documented in ``mpc2emu/docs/AKAI_S3000_FORMAT.md``, which
cites its own sources (Ohsaki's binary analysis, cross-checked against
akaiutil); Akai never published it. This module is written from those
documented offsets and is independent of mpc2emu's own reader for the same
reason `banks/e4b.py` is independent of `writers/e4b_writer.py`:

  **Assembly here is file-level surgery, not parse-and-re-serialize.** An
  AKAI program carries per-keygroup filter and amplitude envelopes, LFO
  routing, modulation depths and pitch-bend settings across its 192-byte
  common block and 192 bytes per keygroup. mpc2emu's `parse_program_bytes`
  reads about a dozen of those fields, which is everything its `Bank` model
  can hold and the right scope for *converting*. Copying the program file
  verbatim keeps all of them, which is the right scope for *librarying*.

And it means browsing an AKAI disk needs no mpc2emu checkout at all — the
same reason `banks/summary.py` walks KRZ itself rather than reaching for
`parsers.krz_parser`. That matters more here than there: AKAI support is on
an unmerged mpc2emu branch, so a checkout of its `main` has none of it.

**Zones name their sample; they do not index it.** This is the failure mode
mpc2emu's own `cbe6f10` was about, and AKAI is more exposed to it than any
format here — a name is 12 characters (E4B/KRZ allow 16), and it is unique
only *within one volume*, which is exactly how the sampler resolves it. Two
volumes may each hold a different `BASS` and neither is wrong. `assemble()`
therefore renames on collision and patches the naming zone, rather than
letting one volume's sample silently answer to another volume's program —
see its docstring.

Program file layout (little-endian throughout; `AKAI_S3000_FORMAT.md`
"Program file"):
    0x00        header id: 1 = S1000, 3 = S3000
    0x03..0x0f  name, 12 bytes, AKAI-encoded
    0x0f        MIDI program number
    0x11        polyphony
    0x13/0x14   lowest / highest key
    0x15        octave shift (signed)
    0x18/0x19   pan (signed) / loudness
    0x2a        number of keygroups, 1..99
    0xc0 + n*0xc0   keygroup n, each:
        0x03/0x04   lo / hi key
        0x05        tune offset, signed 16-bit
        0x07        filter frequency
        0x0c..0x0f  amplitude attack / decay / sustain / release
        0x1f        number of velocity zones in use
        zones at 0x22, 0x3a, 0x52, 0x6a (a uniform 0x18 stride -- the
        primary spec's 0x53 for the third one is off by one; see
        ZONE_OFFSETS for the measurement), each:
            +0x00   sample name, 12 bytes
            +0x0c   lo velocity        +0x0d  hi velocity
            +0x0e   tune, signed 16-bit
            +0x10   loudness (signed)  +0x12  pan (signed)

Sample file layout:
    0x00        header id (1 or 3)
    0x01        bandwidth: 0 = 10 kHz, 1 = 20 kHz
    0x02        root note
    0x03..0x0f  name, 12 bytes
    0x13        playback type; 2 (and only 2) means "no loop"
    0x14        pitch offset, signed 16-bit fixed point (cents * 256)
    0x1a        length in SAMPLES, 32-bit
    0x26/0x2c   loop 1 start / length, 32-bit
    0x30        loop 1 repeats; 0 means the loop is unused
    0x8a        sample rate, 16-bit
    0xc0        PCM, 16-bit mono
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# ── name codec ───────────────────────────────────────────────────────────────
# Names are not ASCII: a 41-symbol alphabet, one byte per character
# (AKAI_S3000_FORMAT.md "Character encoding"). Both of that document's
# references agree on this table exactly.
AKAI_ALPHABET = ("0123456789"                    # 0x00-0x09
                 " "                              # 0x0A
                 "ABCDEFGHIJKLMNOPQRSTUVWXYZ"    # 0x0B-0x24
                 "#+-.")                          # 0x25-0x28

_AKAI_REVERSE = {c: i for i, c in enumerate(AKAI_ALPHABET)}
_AKAI_SPACE = _AKAI_REVERSE[" "]

NAME_LEN = 12

SAMPLE_HDR = 0xC0
PROGRAM_COMMON = 0xC0
KEYGROUP_LEN = 0xC0

#: Velocity-zone offsets within a keygroup: a **uniform 0x18 stride**, which
#: is 12 name bytes plus a 12-byte parameter record.
#:
#: The primary spec lists the third one as 0x53, making the deltas
#: 0x18/0x19/0x17, and both implementations built on it copied that. It is
#: off by one, and 29 372 keygroups on eight commercial library discs say so
#: three independent ways:
#:
#:   - Zones fill in order, so slot 3 should be populated only when 1 and 2
#:     are. At 0x52 that holds for 99.1%; at 0x53, 50.9% -- noise.
#:   - Zones 1 and 2, whose offsets nobody disputes, end their record with
#:     ff ff ff ff. At 0x52 so does slot 3, in 25 842 keygroups. At 0x53,
#:     in none.
#:   - A name field names real samples. Of the slot-3 zones whose record is
#:     coherent at 0x52, **547 of 550 (99.5%)** name a sample on their own
#:     volume -- better than slot 1's own 88.5% baseline. At 0x53: **0 of
#:     29 180**, and every name comes back with a spurious trailing "0",
#:     which is what 0x00 decodes to in this alphabet.
#:
#: Reading at 0x53 shifts that zone's name and every one of its parameters
#: by a byte. Writing there puts the name where the sampler will not find it.
ZONE_OFFSETS = (0x22, 0x3A, 0x52, 0x6A)
ZONE_STRIDE = 0x18

MAX_KEYGROUPS = 99
MAX_ZONES_PER_KEYGROUP = len(ZONE_OFFSETS)

HDR_ID_S1000 = 1
HDR_ID_S3000 = 3

#: The file type is a letter naming the kind of file, in one of three ranges
#: by sampler generation: `A`-`Z` for the S900, `a`-`z` for the S1000, and
#: the S1000 letters with bit 7 set for the S3000. A sample is `s`, so an
#: S3000 one is 0xF3; a program is `p`, so 0xF0.
#:
#: The extension is not decoration — the directory entry stores only this
#: byte, so it is the sole place a file's type comes from, and a file without
#: a recognised one cannot be placed on AKAI media at all.
_S900_RANGE = (ord("A"), ord("Z"))
_S1000_RANGE = (ord("a"), ord("z"))
_S3000_RANGE = (ord("a") | 0x80, ord("z") | 0x80)

_FTYPE_CDSETUP = ord("T")                 # CD3000 CD-ROM setup   -> .CD
_FTYPE_CDSAMPLE = ord("h") | 0x80         # CD3000 sample params  -> .s+

#: The types this project itself writes, kept as names for readability.
FILE_TYPES = {
    "S3": ord("s") | 0x80,
    "P3": ord("p") | 0x80,
    "M3": ord("m") | 0x80,
    "S1": ord("s"),
    "P1": ord("p"),
}


def ftype_to_ext(ftype: int) -> str:
    """The extension for a file-type byte.

    A rule rather than a table, and the difference is measurable: only
    `.S1`/`.P1` carry the generation digit in the S1000 range — an FX file is
    `.X`, not `.X1` — plus two special cases. mpc2emu's five-entry table left
    375 files unnamed across eight real library discs (344 `.X`, 17 `.M3`,
    9 `.D`, 5 `.Q`), and the extension is the one thing that says what such a
    file is.
    """
    if ftype == _FTYPE_CDSETUP:
        return "CD"
    if ftype == _FTYPE_CDSAMPLE:
        return "s+"
    if _S900_RANGE[0] <= ftype <= _S900_RANGE[1]:
        return f"{chr(ord('A') + ftype - _S900_RANGE[0])}9"
    if _S1000_RANGE[0] <= ftype <= _S1000_RANGE[1]:
        letter = chr(ord("A") + ftype - _S1000_RANGE[0])
        return f"{letter}1" if ftype in (ord("p"), ord("s")) else letter
    if _S3000_RANGE[0] <= ftype <= _S3000_RANGE[1]:
        return f"{chr(ord('A') + ftype - _S3000_RANGE[0])}3"
    return f"x{ftype:02x}"


_EXT_FTYPE = {ftype_to_ext(t).upper(): t
              for t in (list(range(_S900_RANGE[0], _S900_RANGE[1] + 1))
                        + list(range(_S1000_RANGE[0], _S1000_RANGE[1] + 1))
                        + list(range(_S3000_RANGE[0], _S3000_RANGE[1] + 1))
                        + [_FTYPE_CDSETUP, _FTYPE_CDSAMPLE])}


def ext_to_ftype(ext: str) -> Optional[int]:
    """The file-type byte an extension stands for, or None if it names no
    AKAI type at all. The inverse of `ftype_to_ext`, built from it so the
    two cannot drift."""
    return _EXT_FTYPE.get(ext.upper().lstrip("."))

SAMPLE_TYPES = {FILE_TYPES["S3"], FILE_TYPES["S1"]}
PROGRAM_TYPES = {FILE_TYPES["P3"], FILE_TYPES["P1"]}

#: A volume holds at most this many files (AKAI_S3000_FORMAT.md "Volume
#: directory"), which is the real ceiling on what assemble() may produce.
MAX_FILES_PER_VOLUME = 510


class AkaiFormatError(ValueError):
    pass


def akai_to_str(raw: bytes) -> str:
    """Decode an AKAI-encoded name, trailing blanks removed. An
    out-of-alphabet byte becomes '.', as both of the format's references do —
    one stray byte in one name must not stop a whole disk from listing."""
    return "".join(AKAI_ALPHABET[b] if b < len(AKAI_ALPHABET) else "."
                   for b in raw).rstrip()


def str_to_akai(name: str, length: int = NAME_LEN) -> bytes:
    """Encode to the AKAI alphabet, space-padded to `length`. There is no
    lower case and no underscore in that alphabet, so anything outside it
    becomes a space rather than failing."""
    out = bytearray()
    for ch in name.upper()[:length]:
        out.append(_AKAI_REVERSE.get(ch, _AKAI_SPACE))
    out.extend([_AKAI_SPACE] * (length - len(out)))
    return bytes(out)


def display_name(name: str) -> str:
    """What the sampler's own front panel will show for `name`.

    Round-tripped through the encoding rather than returned as given: an
    `EMU_BANK_01` that appears as `EMU BANK 01` on the hardware is confusing
    to chase after the fact."""
    return akai_to_str(str_to_akai(name))


def _s8(v: int) -> int:
    return v - 256 if v > 127 else v


def _u16(data: bytes, off: int) -> int:
    return struct.unpack_from("<H", data, off)[0]


def _s16(data: bytes, off: int) -> int:
    # Tune offsets are signed; read unsigned, a -1 semitone becomes +65535.
    return struct.unpack_from("<h", data, off)[0]


def _u32(data: bytes, off: int) -> int:
    return struct.unpack_from("<I", data, off)[0]


# ── samples ──────────────────────────────────────────────────────────────────

@dataclass
class AkaiSample:
    """One `.S3`/`.S1` file, held verbatim."""

    name: str
    body: bytes                  # 0xC0-byte header + 16-bit mono PCM
    filename: str = ""

    @property
    def size(self) -> int:
        return len(self.body)

    @property
    def is_s3000(self) -> bool:
        return bool(self.body) and self.body[0] == HDR_ID_S3000

    @property
    def root_key(self) -> int:
        root = self.body[0x02] if len(self.body) > 0x02 else 60
        return root if 0 < root < 128 else 60

    @property
    def sample_rate(self) -> int:
        if len(self.body) < 0x8C:
            return 44100
        return _u16(self.body, 0x8A) or 44100

    @property
    def frame_count(self) -> int:
        """Frames as the header DECLARES them, which is what to trust: a file
        lifted off a disk is padded out to a block boundary, so its length
        overstates its audio."""
        if len(self.body) < 0x1E:
            return 0
        declared = _u32(self.body, 0x1A)
        physical = max(0, len(self.body) - SAMPLE_HDR) // 2
        return min(declared, physical) if declared else physical

    @property
    def pcm(self) -> bytes:
        return self.body[SAMPLE_HDR:SAMPLE_HDR + self.frame_count * 2]

    @property
    def loop(self) -> str:
        """'none' | 'forward'. Playback type 2 is the only value that means
        unlooped; every other one sustains in some form. Only loop 1 is
        considered — the other seven are alternates the hardware switches
        between, and a zeroed `repeats` field is how an unused one reads."""
        if len(self.body) < 0x32:
            return "?"
        if self.body[0x13] == 2:
            return "none"
        if _u32(self.body, 0x2C) > 0 and _u16(self.body, 0x30) != 0:
            return "forward"
        return "none"


def parse_sample(data: bytes, filename: str = "") -> Optional[AkaiSample]:
    """One AKAI sample file -> AkaiSample, or None if it is not one."""
    if len(data) < SAMPLE_HDR or data[0x00] not in (HDR_ID_S1000, HDR_ID_S3000):
        return None
    name = akai_to_str(data[0x03:0x03 + NAME_LEN])
    if not name:
        name = Path(filename).stem.upper()[:NAME_LEN]
    return AkaiSample(name=name, body=data, filename=filename)


# ── programs ─────────────────────────────────────────────────────────────────

@dataclass
class AkaiZone:
    sample_name: str
    name_offset: int             # absolute offset of the 12-byte name in the program body
    lo_vel: int
    hi_vel: int
    tune: int                    # 1/16 semitone, as stored
    loudness: int
    pan: int


@dataclass
class AkaiKeygroup:
    lo_key: int
    hi_key: int
    tune: int
    filter_freq: int
    amp_attack: int
    amp_decay: int
    amp_sustain: int
    amp_release: int
    zones: list[AkaiZone] = field(default_factory=list)


@dataclass
class AkaiProgram:
    """One `.P3`/`.P1` file, held verbatim alongside what it declares."""

    name: str
    body: bytes
    filename: str = ""
    keygroups: list[AkaiKeygroup] = field(default_factory=list)

    @property
    def is_s3000(self) -> bool:
        return bool(self.body) and self.body[0] == HDR_ID_S3000

    @property
    def midi_program(self) -> int:
        return self.body[0x0F] if len(self.body) > 0x0F else 0

    @property
    def size(self) -> int:
        return len(self.body)

    @property
    def zone_refs(self) -> list[tuple[int, str]]:
        """(offset of the name field within `body`, sample name it holds) for
        every velocity zone — the AKAI counterpart of `E4BPreset.zone_refs`,
        and the hook `assemble()` patches a rename through. A name, not an
        index, because that is genuinely what the format stores."""
        return [(z.name_offset, z.sample_name)
                for kg in self.keygroups for z in kg.zones]

    @property
    def sample_names(self) -> list[str]:
        seen: dict[str, None] = {}
        for _off, name in self.zone_refs:
            seen.setdefault(name.strip().upper(), None)
        return list(seen)


def _parse_zone(body: bytes, base: int) -> Optional[AkaiZone]:
    if base + 0x14 > len(body):
        return None
    raw = body[base:base + NAME_LEN]
    # An unused zone is blank, and "blank" has two spellings — one of which
    # is a trap. 0x00 decodes to the DIGIT '0', not to a space, so a
    # never-populated zone reads as the perfectly valid name "000000000000"
    # and would be taken for a real sample. Test the raw bytes, not the
    # decoded string.
    if not raw or all(b == 0x00 for b in raw) or all(b == _AKAI_SPACE for b in raw):
        return None
    name = akai_to_str(raw)
    if not name:
        return None
    # An unused slot is not always blank. On real discs a keygroup that uses
    # two zones commonly leaves stale bytes in the third slot -- 3 323 of the
    # 3 873 nested slot-3 reads across eight library discs, of which just 1%
    # name a real sample, against 99.5% for the coherent ones. A velocity
    # range that is inverted or past the MIDI ceiling is the tell, and it
    # costs nothing to require what the field means: a zone like that could
    # not sound anyway.
    lo_vel, hi_vel = body[base + 0x0C], body[base + 0x0D]
    if lo_vel > hi_vel or hi_vel > 127:
        return None
    return AkaiZone(
        sample_name=name, name_offset=base,
        lo_vel=body[base + 0x0C], hi_vel=body[base + 0x0D],
        tune=_s16(body, base + 0x0E),
        loudness=_s8(body[base + 0x10]), pan=_s8(body[base + 0x12]))


def parse_program(data: bytes, filename: str = "") -> Optional[AkaiProgram]:
    """One AKAI program file -> AkaiProgram, or None if it is not one."""
    if len(data) < PROGRAM_COMMON or data[0x00] not in (HDR_ID_S1000, HDR_ID_S3000):
        return None

    n_kg = data[0x2A]
    # A corrupt or misidentified file can claim keygroups it does not carry;
    # believe the file length over the header, same as mpc2emu's reader.
    available = (len(data) - PROGRAM_COMMON) // KEYGROUP_LEN
    if n_kg < 1 or n_kg > available:
        n_kg = available

    keygroups: list[AkaiKeygroup] = []
    for i in range(n_kg):
        off = PROGRAM_COMMON + i * KEYGROUP_LEN
        if off + KEYGROUP_LEN > len(data):
            break
        zones = [z for z in (_parse_zone(data, off + b) for b in ZONE_OFFSETS) if z]
        if not zones:
            continue
        keygroups.append(AkaiKeygroup(
            lo_key=data[off + 0x03], hi_key=data[off + 0x04],
            tune=_s16(data, off + 0x05), filter_freq=data[off + 0x07],
            amp_attack=data[off + 0x0C], amp_decay=data[off + 0x0D],
            amp_sustain=data[off + 0x0E], amp_release=data[off + 0x0F],
            zones=zones))

    name = akai_to_str(data[0x03:0x03 + NAME_LEN])
    if not name:
        name = Path(filename).stem.upper()[:NAME_LEN]
    return AkaiProgram(name=name, body=data, filename=filename, keygroups=keygroups)


# ── a volume ─────────────────────────────────────────────────────────────────

@dataclass
class AkaiBank:
    """One AKAI **volume**: the programs and samples that resolve against
    each other. The unit `banks/e4b.py` calls a bank file."""

    path: str                    # "<image>:A/NAME", or a directory path
    name: str
    programs: list[AkaiProgram] = field(default_factory=list)
    samples: dict[str, AkaiSample] = field(default_factory=dict)   # keyed by UPPER name
    partition: str = ""
    warnings: list[str] = field(default_factory=list)

    def find_sample(self, name: str) -> Optional[AkaiSample]:
        return self.samples.get(name.strip().upper())

    @property
    def total_size(self) -> int:
        return (sum(p.size for p in self.programs)
                + sum(s.size for s in self.samples.values()))

    def missing_samples(self, program: Optional[AkaiProgram] = None) -> list[str]:
        """Sample names this volume's programs play but do not contain.

        Not automatically damage: AKAI libraries were routinely shipped with
        a program on one floppy and its samples on another, and the sampler
        resolves against whatever is loaded into memory, not against the
        disk. Worth showing, never worth refusing over."""
        progs = [program] if program is not None else self.programs
        missing: dict[str, None] = {}
        for p in progs:
            for name in p.sample_names:
                if name not in self.samples:
                    missing.setdefault(name, None)
        return list(missing)


def parse_volume(files: Iterable[tuple[str, bytes]], name: str = "",
                 path: str = "", partition: str = "") -> AkaiBank:
    """Build an AkaiBank from `(filename, data)` pairs — one volume's files,
    however they were obtained (a disk image, a folder, an archive).

    Type comes from the extension, since that is the only place the format
    keeps it; a file whose extension does not name a type is still offered to
    both readers, because several extraction tools emit `.a3p`/`.a3s` or no
    extension at all and the header id then settles it.
    """
    bank = AkaiBank(path=path or name, name=name, partition=partition)
    for filename, data in files:
        ext = filename.rpartition(".")[2].upper()
        ftype = ext_to_ftype(ext)
        if ftype in PROGRAM_TYPES or ext in ("A3P", "S3P"):
            prog = parse_program(data, filename)
            if prog is not None:
                bank.programs.append(prog)
            else:
                bank.warnings.append(f"{filename}: not a readable AKAI program")
            continue
        if ftype in SAMPLE_TYPES or ext in ("A3S", "S3S"):
            samp = parse_sample(data, filename)
            if samp is not None:
                _add_sample(bank, samp, filename)
            else:
                bank.warnings.append(f"{filename}: not a readable AKAI sample")
            continue
        if ftype is not None:
            # A typed file that is neither a program nor a sample: a multi
            # (.M3), an effects file (.X), a cue list (.Q), drum settings
            # (.D). Its type byte already says what it is, so it must NOT
            # fall through to the header sniff below -- an effects file
            # begins with the same header id a program does, and 90 of them
            # on one library disc read as 90 phantom programs with key
            # ranges like 200-0 before this line existed.
            continue
        # No recognised extension at all -- several extraction tools drop it.
        # Only here does the header id get to decide.
        prog = parse_program(data, filename)
        if prog is not None and prog.keygroups:
            bank.programs.append(prog)
            continue
        samp = parse_sample(data, filename)
        if samp is not None:
            _add_sample(bank, samp, filename)
    return bank


def _add_sample(bank: AkaiBank, samp: AkaiSample, filename: str) -> None:
    key = samp.name.strip().upper()
    if key in bank.samples:
        # Within one volume the sampler resolves a name to exactly one file,
        # so a duplicate is a damaged directory rather than a choice. Keep
        # the first and say so, instead of silently letting the last win.
        bank.warnings.append(
            f"{filename}: a second sample also named {samp.name!r} — kept the first")
        return
    bank.samples[key] = samp


def parse_dir(directory: str) -> AkaiBank:
    """Read a folder of loose AKAI files as one volume."""
    d = Path(directory)
    files = [(f.name, f.read_bytes()) for f in sorted(d.iterdir()) if f.is_file()]
    return parse_volume(files, name=display_name(d.name), path=str(d))


# ── assembly ─────────────────────────────────────────────────────────────────

def _unique_akai_name(base: str, taken: set[str]) -> str:
    """A free 12-character AKAI name near `base`.

    Twelve characters is four fewer than every other format here allows, so
    names that were distinct upstream can arrive already colliding; the
    counter goes into the last characters rather than being appended, since
    appending would just be truncated straight back off."""
    base = (display_name(base).strip() or "SAMPLE")[:NAME_LEN]
    if base.upper() not in taken:
        return base
    for n in range(1, 10000):
        suffix = str(n)
        cand = (base[:NAME_LEN - len(suffix)].rstrip() + suffix)[:NAME_LEN]
        if cand.upper() not in taken:
            return cand
    raise AkaiFormatError(f"cannot find a free name near {base!r}")


def assemble(selections: list[tuple[AkaiBank, AkaiProgram]],
             sample_names: Optional[dict] = None,
             volume_name: str = "") -> list[tuple[str, bytes]]:
    """Build one new AKAI volume from selected (source volume, program) pairs.

    Returns `[(filename, data), ...]` — a volume is a set of files, so that
    is what assembly produces. Hand it to `build/akai_image.py` to put on
    media, or write the files out as they are.

    Every program and sample file is copied **verbatim**; the only bytes
    changed are the 12-character sample-name fields inside a program's
    velocity zones, and only when a rename was forced or requested.

    Three things this has to get right, all of them consequences of a zone
    naming its sample rather than indexing it:

    - **Same name, same content** across two source volumes is one sample.
      Deduplicated, so pulling the same material in through two programs
      does not write its PCM twice.
    - **Same name, different content** is the dangerous case, and it is not
      rare: a name is unique only within a volume, so two volumes may each
      hold their own `BASS`. The second one is renamed and every zone that
      named it is patched to the new name. Without this the first volume's
      `BASS` would answer for both programs — silently, at the wrong pitch,
      which is precisely the class of fault mpc2emu's `cbe6f10` fixed in its
      own model.
    - **A program's own name** may collide too; programs are files in the
      same directory. Renamed the same way, though nothing references a
      program by name, so only its filename changes.

    `sample_names` renames on the way out, `{current name: new name}`,
    applied before collision handling so a requested name still has to be
    unique. A requested name is normalised through the AKAI alphabet, so
    what comes back is what the sampler will actually show.
    """
    if not selections:
        raise ValueError("no programs selected")

    requested = {k.strip().upper(): v for k, v in (sample_names or {}).items()}

    files: list[tuple[str, bytes]] = []
    taken_samples: set[str] = set()
    taken_programs: set[str] = set()
    # (source volume identity, original name) -> final name, so the same
    # sample reached through two programs of the SAME volume stays one file
    # while a namesake from a different volume gets its own.
    resolved: dict[tuple[int, str], str] = {}
    # (final name, content) -> already written, for cross-volume dedupe.
    written: dict[tuple[str, bytes], str] = {}

    for src, program in selections:
        body = bytearray(program.body)
        for name_off, zone_name in program.zone_refs:
            key = (id(src), zone_name.strip().upper())
            final = resolved.get(key)
            if final is None:
                samp = src.find_sample(zone_name)
                if samp is None:
                    # The sample this zone names is not in this volume (see
                    # AkaiBank.missing_samples). Leave the name exactly as it
                    # is: the sampler resolves against loaded memory, so a
                    # program that finds its sample elsewhere still works,
                    # and rewriting the name could only break that.
                    continue
                wanted = requested.get(samp.name.strip().upper(), samp.name)
                dedupe = (display_name(wanted).strip()[:NAME_LEN], samp.body)
                if dedupe in written:
                    final = written[dedupe]
                else:
                    final = _unique_akai_name(wanted, taken_samples)
                    taken_samples.add(final.upper())
                    written[dedupe] = final
                    sbody = bytearray(samp.body)
                    sbody[0x03:0x03 + NAME_LEN] = str_to_akai(final)
                    ext = "S1" if not samp.is_s3000 else "S3"
                    files.append((f"{final.strip()}.{ext}", bytes(sbody)))
                resolved[key] = final
            if final.strip().upper() != zone_name.strip().upper():
                body[name_off:name_off + NAME_LEN] = str_to_akai(final)

        pname = _unique_akai_name(
            volume_name if (volume_name and len(selections) == 1) else program.name,
            taken_programs)
        taken_programs.add(pname.upper())
        # The name lives in the file's own header as well as in its filename;
        # patching one and not the other would show two different names
        # depending on which one a reader trusts.
        body[0x03:0x03 + NAME_LEN] = str_to_akai(pname)
        ext = "P1" if not program.is_s3000 else "P3"
        files.append((f"{pname.strip()}.{ext}", bytes(body)))

    if len(files) > MAX_FILES_PER_VOLUME:
        raise ValueError(
            f"{len(files)} files — an AKAI volume holds at most "
            f"{MAX_FILES_PER_VOLUME}. Split the selection across volumes.")
    return files


def write_volume(files: list[tuple[str, bytes]], out_dir: str) -> list[str]:
    """Write an assembled volume out as loose files. Returns the paths."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for filename, data in files:
        p = out / filename
        p.write_bytes(data)
        written.append(str(p))
    return written
