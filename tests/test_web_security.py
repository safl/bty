"""Tests for ``bty.web._security``.

The validator centralises a rule duplicated across multiple sites
(catalog / hash / app / portability bundle ids); pinning its
accept + reject behaviour in one place is what makes the
centralisation worth doing. Mirror coverage of the per-site
checks we replaced.
"""

from __future__ import annotations

import pytest

from bty.web._security import validate_basename


@pytest.mark.parametrize(
    "good",
    [
        "demo.img.gz",
        "nosi-debian-sysdev.img.gz",
        "catalog-deadbeefcafe-fedora-sysdev.img.gz",
        "2026-05-25T07-00-00Z",  # backup_id shape
        "single-word",
        "a",  # one char OK
        "name_with_underscore",
        "name.with.dots.but.not.dot-alone",
    ],
)
def test_validate_basename_accepts_plain_basenames(good: str) -> None:
    """Anything that's a single path-component with no traversal-y
    characters passes silently. Includes the realistic shapes the
    wizard / catalog / hash boundaries see: image filenames,
    catalog-prefixed filenames, ISO-8601 backup_ids."""
    validate_basename(good)  # must not raise


@pytest.mark.parametrize(
    "bad",
    [
        "",  # empty
        ".",  # self
        "..",  # parent
        "foo/bar",  # POSIX separator
        "foo\\bar",  # Windows separator
        "foo\x00bar",  # NUL byte
        "../../etc/passwd",
        "../foo",
        "foo/../bar",
        "subdir/file.img",
        "C:\\Windows\\evil",
    ],
)
def test_validate_basename_rejects_traversal_shapes(bad: str) -> None:
    """Every shape the previous per-module checks rejected: empty
    string, ``.`` / ``..``, any of the path-separator characters,
    NUL. The new helper is the single auditable source of truth."""
    with pytest.raises(ValueError):
        validate_basename(bad)


def test_validate_basename_label_appears_in_error_message() -> None:
    """``label`` flows into the error so an operator-facing 400
    response says ``invalid backup_id: '...'`` instead of just
    ``invalid name: '...'`` -- useful when multiple basenames flow
    through one request and the operator needs to know which one
    was bad."""
    with pytest.raises(ValueError, match="invalid backup_id"):
        validate_basename("..", label="backup_id")
    with pytest.raises(ValueError, match="invalid image_name"):
        validate_basename("foo/bar", label="image_name")


def test_validate_basename_default_label_is_name() -> None:
    """When the caller doesn't supply a label, the message names
    the parameter as ``name`` -- matches the historical per-module
    helper message text so existing operator-facing 400s read the
    same."""
    with pytest.raises(ValueError, match="invalid name"):
        validate_basename("")
