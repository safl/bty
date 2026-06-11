"""Shared security boundaries for bty-web.

This module collects validation rules that are duplicated across
multiple route handlers / managers. Centralising them keeps the
security policy auditable in one place: a future audit reviewer
checks ``_security.py`` once instead of grepping for the same
``"/" in name or "\\\\" in name or ...`` invariant in N modules.

The functions raise :class:`ValueError` on rejection so call sites
can let the exception propagate (FastAPI turns it into 422) or
catch + re-raise as :class:`HTTPException` 400 for a tidier
operator-facing detail message.
"""

from __future__ import annotations

# Characters that turn a basename into a path-traversal vector or
# break path-joining semantics on POSIX filesystems. The current
# rule is the intersection of "things sqlite / filesystem could
# misinterpret" + "things the URL routing layer might decode away":
#
# - ``/``     -- POSIX path separator; ``image_root / "a/b"`` would
#                escape the root entirely.
# - ``\\``    -- Windows path separator. bty-web targets Linux but
#                an operator running tests on macOS / Windows or
#                a stray sample input from a cross-platform setup
#                would slip through a UNIX-only check.
# - ``\0``    -- C-string terminator; any underlying syscall that
#                takes a const char* will truncate at this byte.
#                We've never observed this in production but
#                paranoia is cheap.
#
# Plus the two literal entries ``"."`` and ``".."`` which are valid
# basenames per spec but always mean "self" / "parent" to path APIs.
_BAD_CHARS: frozenset[str] = frozenset(("/", "\\", "\x00"))
_BAD_NAMES: frozenset[str] = frozenset((".", ".."))


def validate_basename(name: str, *, label: str = "name") -> None:
    """Reject ``name`` if it's anything other than a plain basename.

    Replaces a family of per-module ``_reject_traversal_name``
    helpers that earlier bty-web releases scattered across the
    catalog manager, the hash manager, the ``/catalog/cache/{name}``
    route, and the export/import bundle-id checks (the first two
    plus the cache route were retired in v0.40; the export/import
    one still routes through this helper). Each site had the same
    rule expressed in slightly different shapes; this one helper is
    the auditable single source of truth.

    ``label`` lands in the error message so the operator can tell
    which input was rejected when multiple basenames flow through
    one request.

    :raises ValueError: when ``name`` is empty, contains a path
       separator / NUL byte, or is ``.`` / ``..``.
    """
    if not name:
        raise ValueError(f"invalid {label}: empty")
    if name in _BAD_NAMES:
        raise ValueError(f"invalid {label}: {name!r} (path-traversal alias)")
    for bad in _BAD_CHARS:
        if bad in name:
            raise ValueError(
                f"invalid {label}: {name!r} contains {bad!r} "
                f"(must be a plain basename, no path separators or NUL bytes)"
            )
