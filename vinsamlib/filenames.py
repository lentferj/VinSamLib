"""Turning device metadata into something safe to use as a filename.

A preset or sample name is metadata and may contain anything the source
device allowed. An E4XT preset really is called ``Inv/Vel>Q Arco``; across
7 931 real names in this author's own E4B/KRZ library there are 21 carrying
control characters (``\\r``, ``\\x7f``, and worse) and 9 ending in a dot
(``Minneap.Percuss.``, ``Rest ...``). Any of those used as a path component
is a bug waiting for a platform to notice it — ``/`` becomes a directory
separator and the write fails on a directory nobody created, a trailing dot
or space is invalid on Windows, and a NUL raises outright.

This is an **allowlist**, not a denylist, which is the whole point. VinSamLib
used to strip the characters Windows forbids (``\\ / : * ? " < > |``) and let
everything else through, which is how those 30 names survived: nobody thinks
to add ``\\x7f`` to a denylist. Keeping only what is known-safe cannot be
outflanked by a byte nobody thought of.

Deliberately duplicated from mpc2emu's ``models.safe_filename`` rather than
called through the bridge, and the duplication is the point: **New Bank's
"Save as…" must work with no mpc2emu checkout at all** (see the README's
"What it is, and what needs mpc2emu"), and importing it here would quietly
make the one genuinely self-contained write path depend on it. Same
semantics, same fallback shape — if upstream's changes, this should follow
it, and there is a test that compares the two whenever mpc2emu is available.

Separate from a device's own name field, which is a different problem with a
different answer: those truncate to 12 or 16 characters and use the device's
own alphabet (see ``banks/akai.py``'s ``str_to_akai``, or mpc2emu's
``_safe_name``). This one does not truncate — a filesystem does not care how
long a name is, and shortening it here would collide names that were
distinct.
"""

from __future__ import annotations

#: Everything outside this becomes "_". Alphanumerics are locale-aware via
#: str.isalnum(), so accented characters in a real sample name survive.
_ALLOWED_PUNCT = " _-."

DEFAULT_STEM = "UNNAMED"


def safe_filename(name: str, fallback: str = DEFAULT_STEM) -> str:
    """`name` reduced to one safe path component.

    Never returns an empty string, and never returns something a filesystem
    will reject: no separators, no control characters, no leading or
    trailing dot or space.
    """
    out = "".join(c if (c.isalnum() or c in _ALLOWED_PUNCT) else "_"
                  for c in name)
    out = out.strip(" .")
    return out or fallback
