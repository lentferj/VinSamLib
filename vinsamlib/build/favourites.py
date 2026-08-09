"""Turning a hand-written list of favourite preset numbers into positions.

The workflow this serves: while auditioning a CD on the hardware, Jan notes
the numbers the machine shows for the presets worth keeping — a column in a
spreadsheet, one bank per column. Rebuilding those as a new bank meant
counting rows in the Explorer, 105 of them in one measured case, against a
bank holding 990 presets.

WHAT THE NUMBERS MEAN, which is the whole of this module:

**They are positions after loading, not stored ids.** On an E4XT, a bank
loaded into an empty machine numbers its presets from 0, so `P002` is simply
the third preset in bank order.

**On a K2000 you choose the destination bank at load time**, so the same
program appears as 205 loaded at 200 and as 405 loaded at 400. Only the
offset from the load point is meaningful. `base` here is that load point.

**It is a subtraction, not `number % 100`.** A K2000 bank is not capped at a
hundred programs: load 150 starting at 400 and they run to 549, so the
hundreds digit is not a bank identifier and taking it modulo would fold the
second half back over the first. No bank in this library exceeds 100 programs
(largest measured: exactly 100, over 500 banks) so the two agree today, and
that is precisely the kind of agreement that stops holding on someone else's
media.

Stored ids are deliberately NOT consulted. They usually start at 200 for KRZ
— 370 of 400 banks measured — but 30 start at 300, 400 or 800, or have gaps,
and none of that is what the hardware displayed when the note was written.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

#: `P002`, `002`, `p 12` — the notations that appear in a hand-kept list. The
#: `P` is optional because the K2000 columns are bare numbers; it is accepted
#: because the E4XT ones carry it.
_ENTRY = re.compile(r"^P?\s*(\d{1,4})$", re.IGNORECASE)
#: Real cell separators from a spreadsheet copy. NOT whitespace: see below.
_FIELDS = re.compile(r"[\t,;|]")


def parse_numbers(text: str) -> list[int]:
    """Every preset number in pasted text, in order, deduped.

    A LINE CONTRIBUTES ONLY IF ALL OF ITS TOKENS ARE NUMBERS, and that rule is
    the whole design. The obvious approach -- scan the text for anything that
    looks like a number -- fails on the very first real file: the column is
    headed with the bank's name, and that name is `Big Bank 64`. Scanning
    picked up the 128, resolved it to a real preset, and quietly added a
    106th favourite to a list of 105. It parsed, it matched, and nothing
    looked wrong.

    So a heading is skipped as a heading: it has a word in it, therefore it is
    not a row of numbers. Blank lines and stray punctuation drop out the same
    way, which keeps a paste-as-is usable without making the user tidy it.

    Fields split on tab/comma/semicolon first -- a whole spreadsheet block
    pastes that way, several banks side by side -- and on whitespace only
    when every token then qualifies, which is what allows `200 206 207` on one
    line while still rejecting `Big Bank 64`.
    """
    out: list[int] = []
    seen: set[int] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        fields = [f.strip() for f in _FIELDS.split(line) if f.strip()]
        if len(fields) == 1:
            fields = line.split()
        matches = [_ENTRY.match(f) for f in fields]
        if not matches or not all(matches):
            continue          # a heading, a note, anything not purely numbers
        for m in matches:
            n = int(m.group(1))
            if n not in seen:
                seen.add(n)
                out.append(n)
    return out


def infer_base(numbers: Iterable[int], fmt: str) -> int:
    """The load point the numbers were written against.

    E4B/EIII: always 0 — the machine was empty, so the first preset is 0.

    KRZ: the hundred at or below the lowest number, because that is the bank
    the user chose on load. A guess, and a wrong one when every favourite in
    a 150-program bank happens to fall past the first hundred; the caller
    shows it and lets it be overridden rather than deciding silently.
    """
    nums = [n for n in numbers]
    if fmt != "KRZ" or not nums:
        return 0
    return (min(nums) // 100) * 100


def resolve(numbers: Iterable[int], base: int, count: int) -> tuple[list[int], list[int]]:
    """(positions, unresolved) for `count` presets loaded at `base`.

    A number below the base, or past the last preset, cannot be a position in
    this bank and comes back as unresolved rather than being clamped: a
    clamped one would silently add the wrong preset, which is worse than
    saying so. Most often it means the list belongs to a different bank, or
    the base is wrong.
    """
    positions: list[int] = []
    missing: list[int] = []
    for n in numbers:
        pos = n - base
        if 0 <= pos < count:
            positions.append(pos)
        else:
            missing.append(n)
    return positions, missing


def describe(fmt: str, base: int, positions: list, missing: list,
             count: int) -> str:
    """One line for the dialog, naming the reading actually used."""
    where = "from 0" if base == 0 else f"from {base}"
    got = f"{len(positions)} of {len(positions) + len(missing)} entr" \
          f"{'y' if len(positions) + len(missing) == 1 else 'ies'}"
    tail = ""
    if missing:
        shown = ", ".join(str(m) for m in missing[:6])
        tail = (f" — {len(missing)} outside this bank's {count} preset(s): "
                f"{shown}{' …' if len(missing) > 6 else ''}")
    return f"Reading as {fmt} preset numbers {where}: {got} matched{tail}"
