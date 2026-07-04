"""Direct unit tests for the ``boot_state`` Jinja filter.

The filter has 9 real branches (3 alternating modes x 3 signal
states) plus a KeyError / TypeError / IndexError guard for row
shapes that lack the columns. Prior to the extraction it was
only exercised through one composite UI render test at
``test_web_ui.py::test_ui_dashboard_boot_state_column_renders``,
so a regression on any single branch would have surfaced as
"something on the dashboard looks off" rather than pointing at
the miscoded arm.

The v0.33.22 regression these branches were introduced to
prevent -- "flashed; booting disk" lighting up the moment iPXE
pulled the kernel, well before the live env could actually
reach ``bty`` -- can silently return if any of the ``armed +
has-completion-signal`` gates degrade to ``armed`` alone. These
tests pin the gate explicitly.
"""

from __future__ import annotations

from bty.web._app import _boot_state

# ---------- non-alternating modes -----------------------------------------


def test_non_alternating_modes_return_empty() -> None:
    """``ipxe-exit`` and ``bty-tui`` have no in-cycle position; the
    filter returns ``""`` so the row's state cell renders blank."""
    for mode in ("ipxe-exit", "bty-tui"):
        assert _boot_state({"boot_mode": mode, "saw_flasher_boot": 0}) == ""
        assert _boot_state({"boot_mode": mode, "saw_flasher_boot": 1}) == ""


# ---------- bty-flash-once -----------------------------------------------


def test_flash_once_pending_flash_when_unarmed() -> None:
    """Fresh binding + no PXE contact yet -> the operator's next
    action is "power it on so bty flashes it"."""
    assert _boot_state({"boot_mode": "bty-flash-once", "saw_flasher_boot": 0}) == "pending flash"


def test_flash_once_live_env_running_when_armed_without_completion() -> None:
    """The v0.33.22 gate: the iPXE chain fired (armed) but the live
    env has not POSTed the flash-done signal yet. Distinct from
    the "done" state so the operator can see the flash is
    in-progress rather than think it already completed."""
    assert (
        _boot_state(
            {
                "boot_mode": "bty-flash-once",
                "saw_flasher_boot": 1,
                "last_flashed_at": None,
            }
        )
        == "live env running; awaiting flash"
    )


def test_flash_once_flashed_booting_disk_when_armed_and_completed() -> None:
    """Both signals: iPXE ran (armed) AND the live env POSTed the
    /flash/done signal (last_flashed_at set). The "done" label
    is now safe."""
    assert (
        _boot_state(
            {
                "boot_mode": "bty-flash-once",
                "saw_flasher_boot": 1,
                "last_flashed_at": "2026-07-04T12:00:00+00:00",
            }
        )
        == "flashed; booting disk"
    )


# ---------- bty-flash-always ---------------------------------------------


def test_flash_always_ready_to_flash_when_unarmed() -> None:
    """The always-mode idle state carries a distinct label
    ("ready to flash") from once-mode's "pending flash" so the
    operator can tell "will flash on next PXE" from "will flash
    on the first PXE only"."""
    assert _boot_state({"boot_mode": "bty-flash-always", "saw_flasher_boot": 0}) == "ready to flash"


def test_flash_always_live_env_running_when_armed_without_completion() -> None:
    """Same v0.33.22 gate as once-mode: armed alone is not enough
    to claim the flash finished."""
    assert (
        _boot_state(
            {
                "boot_mode": "bty-flash-always",
                "saw_flasher_boot": 1,
                "last_flashed_at": None,
            }
        )
        == "live env running; awaiting flash"
    )


def test_flash_always_flashed_booting_disk_when_armed_and_completed() -> None:
    assert (
        _boot_state(
            {
                "boot_mode": "bty-flash-always",
                "saw_flasher_boot": 1,
                "last_flashed_at": "2026-07-04T12:00:00+00:00",
            }
        )
        == "flashed; booting disk"
    )


# ---------- bty-inventory ------------------------------------------------


def test_inventory_pending_inventory_when_unarmed() -> None:
    assert _boot_state({"boot_mode": "bty-inventory", "saw_flasher_boot": 0}) == "pending inventory"


def test_inventory_live_env_running_when_armed_without_completion() -> None:
    """inventory-mode's v0.33.22 gate uses ``known_disks_at`` as its
    completion signal (the inventory POST from the live env), NOT
    ``last_flashed_at`` -- pin the distinction so a future edit
    that swaps them silently degrades this arm."""
    assert (
        _boot_state(
            {
                "boot_mode": "bty-inventory",
                "saw_flasher_boot": 1,
                "known_disks_at": None,
                "last_flashed_at": None,
            }
        )
        == "live env running; awaiting inventory"
    )


def test_inventory_done_gated_on_known_disks_at_not_last_flashed_at() -> None:
    """Regression pin: an inventory-mode row with ``last_flashed_at``
    from a previous cycle but no fresh ``known_disks_at`` must NOT
    read as "inventoried; booting disk". The completion signal per
    mode is distinct."""
    assert (
        _boot_state(
            {
                "boot_mode": "bty-inventory",
                "saw_flasher_boot": 1,
                "known_disks_at": None,
                "last_flashed_at": "2026-06-30T12:00:00+00:00",
            }
        )
        == "live env running; awaiting inventory"
    )


def test_inventory_inventoried_booting_disk_when_armed_and_completed() -> None:
    assert (
        _boot_state(
            {
                "boot_mode": "bty-inventory",
                "saw_flasher_boot": 1,
                "known_disks_at": "2026-07-04T12:00:00+00:00",
            }
        )
        == "inventoried; booting disk"
    )


# ---------- guard clause --------------------------------------------------


def test_missing_boot_mode_key_returns_empty() -> None:
    """The two required keys (``boot_mode`` + ``saw_flasher_boot``)
    are read at the top; a KeyError there returns ``""`` rather
    than propagating a 500 through a template render."""
    assert _boot_state({"saw_flasher_boot": 0}) == ""


def test_missing_saw_flasher_boot_key_returns_empty() -> None:
    assert _boot_state({"boot_mode": "bty-flash-once"}) == ""


def test_none_input_returns_empty() -> None:
    """A None row (mid-discovery events with no machine record yet)
    triggers TypeError on the ``m['key']`` subscript; guard returns
    empty."""
    assert _boot_state(None) == ""


def test_missing_completion_signal_column_treated_as_no_signal() -> None:
    """A discovery-time row shape may lack ``last_flashed_at`` /
    ``known_disks_at`` entirely (columns not projected). The inner
    ``_has`` swallows the KeyError and treats absent as False, so
    the mid-cycle label surfaces rather than crashing."""
    assert (
        _boot_state(
            {
                "boot_mode": "bty-flash-once",
                "saw_flasher_boot": 1,
                # last_flashed_at intentionally absent
            }
        )
        == "live env running; awaiting flash"
    )


def test_unknown_mode_returns_empty() -> None:
    """A row with a boot_mode value the filter doesn't recognise
    (stale enum drift, mid-migration bug) returns empty rather
    than crashing the render."""
    assert _boot_state({"boot_mode": "some-unknown-mode", "saw_flasher_boot": 1}) == ""
