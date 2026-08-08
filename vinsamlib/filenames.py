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


#: Characters that genuinely cannot appear in one path component on some
#: platform we support: separators, the Windows-reserved set, and controls.
_UNSAFE = set('/\\:*?"<>|')


def safe_path_component(name: str, fallback: str = DEFAULT_STEM) -> str:
    """`name` made usable as a path component, changing as little as possible.

    The gentler sibling of `safe_filename`, and the difference is about who
    sees the result. Use this where the name **survives into something the
    user reads** -- a suggested filename in a Save dialog, or a temp stem
    that a builder turns back into the bank's name on a rebuilt image.
    `&`, `+`, `(`, `'` are all perfectly legal in a filename, and a library
    really does contain `Synths & Keys`; rewriting that to `Synths _ Keys`
    is a downgrade nobody asked for.

    Use `safe_filename` instead where the result is internal, or where
    matching mpc2emu's `models.safe_filename` matters.

    Measured on 3 147 bank names inside real EMU3 and K2000 images: 100 differ
    under the strict allowlist, but only **41** are genuinely unusable --
    `GroovesFilz/Hitz`, `Synth/FX/Misc...`, and others carrying `/` or a
    trailing dot. Those 41 are the ones this touches.
    """
    out = "".join("_" if (c in _UNSAFE or ord(c) < 32 or ord(c) == 127) else c
                  for c in name)
    out = out.strip(" .")
    return out or fallback


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
