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


#: Resident objects an S3000XL can hold at once — STAT.max_blocks, measured
#: on Jan's 32 MB machine by s3ked and relayed 2026-08-14. Programs, KEYGROUPS
#: and samples all cost exactly 1 and share this one pool; the LOAD page shows
#: it as `free P/K/S`. Their measurement: max_blocks 1006, free 884, and
#: 2 programs + 58 keygroups + 62 samples = 122 used, exact.
#:
#: A DEFAULT, not a constant, for two reasons. It is one machine, and whether
#: it moves with fitted memory is untested. And it is a ceiling on what is
#: RESIDENT, shared with whatever is already loaded, so a volume that loads
#: onto an empty machine may not load onto a full one — any check computed
#: from a file alone is a floor.
RESIDENT_OBJECTS_DEFAULT = 1006


def volume_objects(files: Sequence[tuple[str, bytes]]) -> int:
    """Resident objects a volume costs: programs + KEYGROUPS + samples.

    WHY THIS EXISTS BESIDE THE FILE COUNT. `MAX_FILES_PER_VOLUME = 510` is the
    directory's limit and it is not what stops a volume loading. Keygroups
    are counted by the sampler and appear in no directory, so a volume can sit
    comfortably inside 510 entries and still overrun the pool (what the
    machine does then is unverified -- see describe_object_cost). Measured on material
    this program converts: six programs from E4B use 21 directory entries and
    216 objects — about 32 keygroups per program. Filled to the 510-entry cap
    a volume would ask for roughly 5 200 objects against 1 006, so the
    directory limit is about five times too loose to protect anyone.

    The keygroup count is read from byte 0x2a of each program file, the same
    field the parser uses, so this cannot drift from what the writer emits.

    Like `volume_ram_words`, a FIGURE and not a limit — see that docstring for
    why an assembler must not refuse what a user may have a plan for.
    """
    total = 0
    for name, data in files:
        upper = name.upper()
        if upper.endswith((".S3", ".S1")):
            total += 1                                   # the sample itself
        elif upper.endswith((".P3", ".P1")):
            # 1 for the program, plus one per keygroup. A program too short to
            # carry the field is counted as itself alone rather than guessed
            # at: an unreadable program is not evidence of zero keygroups.
            total += 1 + (data[0x2a] if len(data) > 0x2a else 0)
    return total


def describe_object_cost(name: str, files: Sequence[tuple[str, bytes]],
                         budget: int = RESIDENT_OBJECTS_DEFAULT) -> str:
    """One line per volume: objects needed against the resident pool."""
    n = volume_objects(files)
    # NOT "will not load". Nobody has verified what the machine does when the
    # object pool is exceeded, and the one ceiling that IS measured -- sample
    # RAM -- does not refuse, it HALF-loads: one warning, then normal
    # behaviour with every keygroup pointing at an absent sample playing
    # silence. Asserting a failure mode we have not seen would be the same
    # unverified confidence this project keeps catching elsewhere. Put to
    # s3ked; until then the wording says what is known and what is not.
    note = (f" — exceeds the {budget}-object pool; what the machine does then "
            f"is unverified (the RAM ceiling half-loads rather than refusing)"
            if n > budget else "")
    return f"  {name}: about {n:,} resident objects (P/K/S){note}"


def describe_ram_cost(name: str, files: Sequence[tuple[str, bytes]]) -> str:
    """One line per volume: what it needs, and which machines it overruns."""
    w = volume_ram_words(files)
    mb = w * 2 / 1048576
    over = [f"{m} MB" for m, cap in sorted(_MACHINE_WORDS.items()) if w > cap]
    note = (f" — exceeds {', '.join(over)}, will HALF-LOAD there"
            if over else "")
    return f"  {name}: about {w:,} words ({mb:.1f} MB) of sample RAM{note}"


def plan_partitions(volumes: Sequence[tuple], kind: str = "akai_hd",
                    part_mb: int = 60) -> list[list[tuple]]:
    """Group volumes into partitions exactly as the build will.

    Calls mpc2emu's OWN `_plan_partitions` rather than reimplementing the
    rule. A preview that merely resembles the build is worse than none: it
    would be read as a promise, and the whole point is to see the layout at
    the moment it can still be changed. Their function is private, so this
    couples to an underscore name — that is deliberate. An import that breaks
    is loud; a copied rule that drifts is silent, and this project has been
    caught by the silent kind more than once this week.

    Raises whatever the writer raises (a volume too big for any partition,
    for instance), so the preview refuses in exactly the cases the build
    would rather than showing a layout that cannot be written.
    """
    sys_blocks = akai_image.PARTHEAD_BLKS + (
        akai_image.CDINFO_BLKS if kind == "akai_cd3000" else 0)
    part_blocks = min(
        akai_image.PART_MAX_BLOCKS,
        max(sys_blocks + akai_image.VOLDIR_HD_BLKS,
            (part_mb * 1048576) // akai_image.HD_BLOCK))
    return akai_image._plan_partitions(list(volumes), part_blocks,
                                       sys_blocks=sys_blocks)


def describe_partition_groups(volumes: Sequence[tuple], groups: Sequence[Sequence[int]],
                              kind: str = "akai_hd", part_mb: int = 60) -> list[str]:
    """Describe a grouping the USER chose, rather than one the writer planned.

    Same shape of output as describe_partition_plan so the dialog reads the
    same either way, but each partition is a group the user set with a break
    in the Pending queue. Over-full groups are FLAGGED here rather than left
    for the writer to raise on: the point of a preview is to be told before
    the build, and a break that cannot be honoured is exactly what a user
    would want to move.
    """
    akai_image_mod = akai_image
    sys_blocks = akai_image_mod.PARTHEAD_BLKS + (
        akai_image_mod.CDINFO_BLKS if kind == "akai_cd3000" else 0)
    part_blocks = min(
        akai_image_mod.PART_MAX_BLOCKS,
        max(sys_blocks + akai_image_mod.VOLDIR_HD_BLKS,
            (part_mb * 1048576) // akai_image_mod.HD_BLOCK))
    cap_mb = part_blocks * akai_image_mod.HD_BLOCK / 1048576

    lines = []
    for i, group in enumerate(groups):
        used = sys_blocks + sum(
            akai_image_mod.VOLDIR_HD_BLKS
            + sum(akai_image_mod._blocks(len(d), akai_image_mod.HD_BLOCK)
                  for _n, d in volumes[gi][1])
            for gi in group)
        used_mb = used * akai_image_mod.HD_BLOCK / 1048576
        names = ", ".join(volumes[gi][0] for gi in group[:4])
        more = f" +{len(group) - 4} more" if len(group) > 4 else ""
        over = "  ⚠ over" if used > part_blocks else ""
        letter = chr(ord("A") + i)
        lines.append(
            f"  Partition {letter}: {len(group)}/{akai_image_mod.ROOTDIR_ENTRIES} "
            f"volume(s), {used_mb:.1f}/{cap_mb:.0f} MB — {names}{more}{over}")
    if any("⚠ over" in ln for ln in lines):
        lines.append("  ⚠ a partition break puts more than a partition holds "
                     "— move or remove it, or the build will refuse")
    lines.append("  (partition breaks set in Pending for Image)")
    return lines


def describe_partition_plan(volumes: Sequence[tuple], kind: str = "akai_hd",
                            part_mb: int = 60) -> list[str]:
    """One line per partition: how full it is, and what lands in it.

    THE HIERARCHY IS disk -> PARTITION -> VOLUME -> program/sample, and the
    limits sit on two different axes that must not be run together:

      * a VOLUME holds at most MAX_FILES_PER_VOLUME directory entries, and it
        is the unit the sampler LOADS;
      * a PARTITION holds at most 60 MB and 100 volumes, and a disk at most 18
        partitions -- structural limits of the media;
      * the OBJECT POOL and sample RAM are neither. They bound what is
        RESIDENT at once, across whatever has been loaded from wherever, so a
        per-partition total of either would look informative and mean nothing.

    So this reports only the structural facts, and the per-volume object and
    RAM figures stay where they are.
    """
    parts = plan_partitions(volumes, kind=kind, part_mb=part_mb)
    sys_blocks = akai_image.PARTHEAD_BLKS + (
        akai_image.CDINFO_BLKS if kind == "akai_cd3000" else 0)
    part_blocks = min(
        akai_image.PART_MAX_BLOCKS,
        max(sys_blocks + akai_image.VOLDIR_HD_BLKS,
            (part_mb * 1048576) // akai_image.HD_BLOCK))
    cap_mb = part_blocks * akai_image.HD_BLOCK / 1048576

    lines = []
    for i, part in enumerate(parts):
        used = sys_blocks + sum(
            akai_image.VOLDIR_HD_BLKS
            + sum(akai_image._blocks(len(d), akai_image.HD_BLOCK)
                  for _n, d in files)
            for _name, files in part)
        used_mb = used * akai_image.HD_BLOCK / 1048576
        letter = chr(ord("A") + i)
        names = ", ".join(n for n, _f in part[:4])
        more = f" +{len(part) - 4} more" if len(part) > 4 else ""
        lines.append(
            f"  Partition {letter}: {len(part)}/{akai_image.ROOTDIR_ENTRIES} "
            f"volume(s), {used_mb:.1f}/{cap_mb:.0f} MB — {names}{more}")
    if len(parts) > akai_image.MAX_PARTITIONS:
        lines.append(f"  ⚠ {len(parts)} partitions — a disk holds "
                     f"{akai_image.MAX_PARTITIONS}")
    elif len(parts) > 1:
        # The built disk can carry MORE partitions than this: it is auto-sized
        # to the content plus a quarter, then carved into whole partitions, so
        # the last one or two may come out empty and ready for later appends.
        # Said here because a user who reads "A and B" and then finds a C on
        # the sampler would reasonably think the preview lied.
        lines.append("  (the disk may carry further empty partitions, sized "
                     "for later appends)")
    return lines


def create_image(kind: str, output_path: str, folders: Sequence[str],
                 volume_label: str = "", size_mb: Optional[int] = None,
                 config: Optional[Config] = None,
                 partitions: Optional[Sequence[Sequence[int]]] = None) -> str:
    """Build AKAI media from folders of loose AKAI files. Returns a log line."""
    ensure_available(config)
    if kind not in AKAI_IMAGE_KINDS:
        raise AkaiWriteUnavailable(f"unknown AKAI image kind: {kind}")
    if Path(output_path).exists():
        raise AkaiWriteUnavailable(f"{output_path} already exists — choose a new name.")
    if not folders:
        raise AkaiWriteUnavailable("an AKAI image needs at least one volume.")

    volumes = [volume_from_folder(f) for f in folders]

    # A volume directory holds MAX_FILES_PER_VOLUME entries and no more. This
    # is checked in banks/akai.py's assemble(), which the New Bank path goes
    # through -- but NOT on this one: volume_from_folder() returns whatever
    # AKAI-typed files the user's folder holds, and nothing between it and the
    # writer counted them. Samples and programs SHARE the directory, so 300
    # one-sample programs is 600 entries rather than 300.
    for name, files in volumes:
        if len(files) > vs_akai.MAX_FILES_PER_VOLUME:
            raise AkaiWriteUnavailable(
                f"volume {name!r} holds {len(files)} files; an AKAI volume "
                f"directory takes {vs_akai.MAX_FILES_PER_VOLUME}. Samples and "
                f"programs share those entries, so split the folder.")

    if kind == "akai_floppy":
        if len(volumes) > 1:
            raise AkaiWriteUnavailable(
                f"a floppy holds exactly one volume; {len(volumes)} were given. "
                f"Build a hard disk or CD-ROM image instead.")
        name, files = volumes[0]
        info = akai_image.build_akai_floppy_image(
            files, output_path, volume_name=volume_label or name, density="hd")
    else:
        # `partitions` is index lists into `volumes` (mpc2emu fdc7e39). Passed
        # only when the user actually set breaks; None keeps the writer's own
        # planning, and a grouping identical to what it would have chosen
        # produces a byte-identical image.
        # `partitions` is index lists into `volumes` (mpc2emu fdc7e39), and
        # since their d391280 an explicit grouping SIZES THE DISK for its own
        # partitions when no size is given. We computed that here first and
        # deleted it: two copies of one sizing rule is the drift this project
        # refused for the partition PLANNER a day earlier, and refusing it
        # there while keeping it here would be inconsistent. An older mpc2emu
        # raises instead, which is loud -- see the re-raise below.
        kwargs = {"partitions": [list(g) for g in partitions]} if partitions else {}
        try:
            info = akai_image.build_akai_hd_image(
                volumes, output_path, size_mb=size_mb,
                cdrom=(kind == "akai_cd3000"),
                cd_label=(volume_label or None) if kind == "akai_cd3000" else None,
                **kwargs)
        except Exception as ex:
            # An mpc2emu predating d391280 sizes the disk from the CONTENT and
            # then cannot fit the partitions the grouping asks for. Its message
            # says "raise --hda-size", which is its CLI flag and means nothing
            # here, so the hint is restated in this program's terms.
            if partitions and "partitions but a" in str(ex):
                raise AkaiWriteUnavailable(
                    f"{ex}\n\nThe partition breaks need a larger disk than "
                    f"this content. Update mpc2emu (its writer sizes the disk "
                    f"for an explicit grouping), or remove some breaks."
                ) from ex
            raise
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
    """The writer's summary as a sentence, leading with the hierarchy.

    It used to print the dict as `k=v, k=v`, which buried the one number the
    user cannot get anywhere else -- how many PARTITIONS their queue turned
    into -- among block counts they have no use for.
    """
    if not isinstance(info, dict):
        return str(info)
    label = AKAI_IMAGE_KINDS.get(kind, (kind,))[0]
    parts = info.get("partitions")
    vols = info.get("volumes")
    files = info.get("files")
    if parts is None and vols is None and info.get("partitions_used") is None:
        bits = [f"{k}={v}" for k, v in info.items()]
        return f"{label}: " + ", ".join(bits)
    # Two counts, and they answer different questions (mpc2emu fdc7e39,
    # after this project read one as the other): 'partitions' is the SLOTS the
    # disk was carved into, which follow from its size; 'partitions_used' is
    # how many hold a volume. The user wants to know where their volumes went,
    # so that leads -- but the spare slots are worth naming rather than
    # hiding, since they are what a later append will fill.
    used = info.get("partitions_used")
    head = f"{label}: "
    if used is not None and parts is not None and parts != used:
        head += f"{used} of {parts} partitions used, "
    elif used is not None:
        head += f"{used} partition{'s' if used != 1 else ''}, "
    elif parts is not None:
        head += f"{parts} partition{'s' if parts != 1 else ''}, "
    head += f"{vols} volume{'s' if vols != 1 else ''}"
    if files is not None:
        head += f", {files} file{'s' if files != 1 else ''}"
    total = info.get("bytes")
    if total:
        head += f", {total / 1048576:.1f} MB"
    free = info.get("free_blocks")
    if free is not None:
        head += f" ({free} free block{'s' if free != 1 else ''})"
    return head
