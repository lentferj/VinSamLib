"""Writing AKAI S1000/S3000 media — hard disk, CD3000 disc, floppy.

The counterpart to `vfs/akai.py`, which reads them. Unlike every other image
kind here, the content is not a list of *bank files*: an AKAI volume is a set
of files, so what goes onto the media is
`[(volume_name, [(filename, bytes), ...]), ...]`. `build/images.py` adapts
its own bank-path interface onto that by treating each path as a **folder**,
which is exactly what New Bank's AKAI "Save as…" produces.

Delegates the actual layout to mpc2emu's `writers.akai_s3000_image` rather
than reimplementing it. That is a deliberate split from the read side, which
is VinSamLib's own: reading has to work with no mpc2emu checkout, and a
second reader costs little; a second *writer* would mean a second partition
header, FAT, checksum and CD-ROM index to get wrong, against one whose
output is byte-identical to `akaiutil`'s over whole images.

**This is gated, and the gate is the point.** Akai never published the
format; every byte is reconstructed, and six separate faults survived
"byte-identical to an independent implementation" before real discs found
them — four of those in the *writer*, which is precisely what `akaiutil`
cannot check, since it takes a file's type from the directory entry rather
than the file. No S3000XL has yet mounted anything written here.

So: `Config.check_akai_write_support()` must pass, which needs an mpc2emu
checkout that actually has the writer, and nothing in the UI offers these
kinds until it does. The branch this lives on is not merged for the same
reason. When hardware confirms it, the gate is one check and the media is
already tested.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from ..banks import akai as vs_akai
from ..config import Config
from ..mpc2emu_bridge import akai_image

#: kind -> (human label, default volume label, builder keyword)
AKAI_IMAGE_KINDS: dict[str, tuple[str, str]] = {
    "akai_hd": ("AKAI S3000 hard disk (SCSI/ZuluSCSI)", "VOLUME 001"),
    "akai_cd3000": ("AKAI CD3000 CD-ROM (raw, not ISO 9660)", "VOLUME 001"),
    "akai_floppy": ("AKAI floppy, 1.6 MB high density", "VOLUME 001"),
}

#: A hard disk and a CD can be grown in place; a floppy has no room worth
#: the trouble, which matches how fat12_floppy is treated for K2000.
AKAI_APPENDABLE = {"akai_hd", "akai_cd3000"}


class AkaiWriteUnavailable(RuntimeError):
    """Raised when AKAI media writing is not available; message is safe to show."""


def ensure_available(config: Optional[Config] = None) -> None:
    """Raise unless this checkout can write AKAI media at all."""
    cfg = config or Config.load()
    ok, reason = cfg.check_akai_write_support()
    if not ok:
        raise AkaiWriteUnavailable(reason)


def volume_from_folder(folder: str) -> tuple[str, list[tuple[str, bytes]]]:
    """One AKAI volume read out of a folder of loose files.

    The folder's own name becomes the volume name, normalised through the
    AKAI alphabet so what the sampler's front panel shows is what the caller
    sees here -- `MY_BANK` displays as `MY BANK`, and silently differing
    would be confusing to chase later.

    Only files the format has a type for are carried. A stray `.DS_Store` or
    a README in a folder someone assembled by hand has no directory-entry
    type byte and therefore cannot go on the media at all; skipping it
    beats failing the whole build over it.
    """
    d = Path(folder)
    if not d.is_dir():
        raise AkaiWriteUnavailable(
            f"{folder} is not a folder. An AKAI volume is a set of files, so "
            f"this kind of image is built from folders -- the ones New Bank's "
            f"Save as… writes -- not from single bank files.")
    files: list[tuple[str, bytes]] = []
    skipped: list[str] = []
    for f in sorted(d.iterdir()):
        if not f.is_file():
            continue
        if vs_akai.ext_to_ftype(f.suffix.lstrip(".")) is None:
            skipped.append(f.name)
            continue
        files.append((f.name, f.read_bytes()))
    if not files:
        raise AkaiWriteUnavailable(
            f"{d.name} holds no AKAI files"
            + (f" ({len(skipped)} other file(s) were skipped)" if skipped else "")
            + ". An AKAI file's type comes from its extension -- .P3 for a "
              "program, .S3 for a sample -- and one without a recognised "
              "extension cannot be placed on the media.")
    return vs_akai.display_name(d.name) or "VOLUME 001", files


#: Bytes of header on a sample file that never reach sample RAM. MEASURED on
#: 60 samples by s3ked (2026-08-12, via mpc2emu), 60 of 60 with no other value
#: appearing — good evidence, but not documented, so an absolute figure
#: inherits that uncertainty. Say "about" at a boundary.
_SAMPLE_HEADER_BYTES = 150

#: Sample memory in 16-bit WORDS, which is how a sampler reports it: a 32 MB
#: S3000XL says 16 777 216, and x2 is 32 MB exactly.
_MACHINE_WORDS = {8: 4_194_304, 16: 8_388_608, 32: 16_777_216}


def volume_ram_words(files: Sequence[tuple[str, bytes]]) -> int:
    """Audio words a volume will need in the sampler's sample RAM.

    NOT the volume's size on media. Programs cost nothing — their bytes are
    header data — and every sample carries `_SAMPLE_HEADER_BYTES` that never
    reach RAM, so media size OVERSTATES the requirement.

    WHY THIS IS REPORTED AT BUILD TIME. An over-RAM volume does not refuse to
    load: it HALF-LOADS. A measured CD-ROM volume needing 30 768 270 words on
    a 32 MB machine loaded 10 programs and 60 of 88 samples, said
    "insufficient waveform memory!" ONCE, and then behaved normally — every
    keygroup pointing at one of the 28 absent samples playing silence.
    Nothing at build time knows the machine's size and nothing at load time
    says it twice, so the user meets it as a bank with holes in it.

    A CONVERTER can cap its output because it chooses what goes in. An
    ASSEMBLER cannot: `volume_from_folder()` takes whatever the user's folder
    holds, and a CD3000 image has ~650 MB to fill, so exceeding any S3000XL
    is easy and looks like nothing at build time. Hence a figure and not a
    limit — refusing a 40 MB volume would be wrong for someone with a plan
    for it.

    Measured across 499 real volumes in this author's library: none exceeds a
    32 MB machine, median 1.8 MB, largest 48%. That is a fact about volumes
    somebody already made fit a sampler, and says nothing about what this
    builder can emit from an arbitrary folder.
    """
    return sum(max(0, len(data) - _SAMPLE_HEADER_BYTES) // 2
               for name, data in files
               if name.upper().endswith((".S3", ".S1")))


def describe_ram_cost(name: str, files: Sequence[tuple[str, bytes]]) -> str:
    """One line per volume: what it needs, and which machines it overruns."""
    w = volume_ram_words(files)
    mb = w * 2 / 1048576
    over = [f"{m} MB" for m, cap in sorted(_MACHINE_WORDS.items()) if w > cap]
    note = (f" — exceeds {', '.join(over)}, will HALF-LOAD there"
            if over else "")
    return f"  {name}: about {w:,} words ({mb:.1f} MB) of sample RAM{note}"


def create_image(kind: str, output_path: str, folders: Sequence[str],
                 volume_label: str = "", size_mb: Optional[int] = None,
                 config: Optional[Config] = None) -> str:
    """Build AKAI media from folders of loose AKAI files. Returns a log line."""
    ensure_available(config)
    if kind not in AKAI_IMAGE_KINDS:
        raise AkaiWriteUnavailable(f"unknown AKAI image kind: {kind}")
    if Path(output_path).exists():
        raise AkaiWriteUnavailable(f"{output_path} already exists — choose a new name.")
    if not folders:
        raise AkaiWriteUnavailable("an AKAI image needs at least one volume.")

    volumes = [volume_from_folder(f) for f in folders]
    if kind == "akai_floppy":
        if len(volumes) > 1:
            raise AkaiWriteUnavailable(
                f"a floppy holds exactly one volume; {len(volumes)} were given. "
                f"Build a hard disk or CD-ROM image instead.")
        name, files = volumes[0]
        info = akai_image.build_akai_floppy_image(
            files, output_path, volume_name=volume_label or name, density="hd")
    else:
        info = akai_image.build_akai_hd_image(
            volumes, output_path, size_mb=size_mb,
            cdrom=(kind == "akai_cd3000"),
            cd_label=(volume_label or None) if kind == "akai_cd3000" else None)
    lines = [_describe(kind, info)]
    lines += [describe_ram_cost(n, f) for n, f in volumes]
    return "\n".join(lines)


def append_volumes(image_path: str, folders: Sequence[str],
                   on_duplicate: str = "add-new",
                   config: Optional[Config] = None) -> tuple[int, str]:
    """Append volumes to an existing AKAI hard disk or CD-ROM, in place.

    `on_duplicate` is never left as mpc2emu's own 'prompt' default -- nothing
    here may touch stdin, the same rule build/images.py applies to every
    other builder."""
    ensure_available(config)
    volumes = [volume_from_folder(f) for f in folders]
    info = akai_image.append_akai_volumes(image_path, volumes,
                                           on_duplicate=on_duplicate)
    return len(volumes), _describe("akai_hd", info)


def _describe(kind: str, info: dict) -> str:
    if not isinstance(info, dict):
        return str(info)
    bits = [f"{k}={v}" for k, v in info.items()]
    return f"{AKAI_IMAGE_KINDS.get(kind, (kind,))[0]}: " + ", ".join(bits)
