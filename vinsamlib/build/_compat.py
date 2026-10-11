"""Capability probes for mpc2emu checkout compatibility.

Replaces `fn.__code__.co_varnames` (includes locals, so a local named
like the parameter falsely answers "supported") with `inspect.signature`
based checks.
"""

import inspect


def accepts_kw(fn, name: str) -> bool:
    """Return True if callable *fn* accepts keyword argument *name*."""
    try:
        sig = inspect.signature(fn)
    except ValueError:
        return False
    return name in sig.parameters
