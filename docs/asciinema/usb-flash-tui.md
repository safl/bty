# `bty` interactive flash - keystroke recipe

The wizard is, well, interactive -- there's no canned shell
script that captures the keystroke-by-keystroke flow. Use this
recipe to record a clean walkthrough by hand under `asciinema
rec`.

## Setup

On a target booted from the USB live env (where ``bty`` is
already running on tty1), or on a workstation install:

```bash
asciinema rec --cols 100 --rows 28 \
    --title "bty: interactive flash" \
    --idle-time-limit 1.5 \
    usb-flash-tui.cast
```

`--idle-time-limit 1.5` collapses any pause longer than 1.5s in
the final cast, so reading-time pauses don't bloat the duration.

## Recipe

The wizard is a five-stage prompt-driven flow. Each stage accepts
a row number (1-based) to pick from a list, or a single letter
(`b` back, `q` quit, `r` refresh). Take a breath between each
step (~2-3 seconds) so the cast is followable.

| # | Stage | Keystrokes | What happens |
|---|---|---|---|
| 1 | (launch) | `sudo bty` `<Enter>` | Wizard launches. Stage 1 prompt: pick a catalog source. |
| 2 | SELECT_CATALOG | `d` `<Enter>` (default) OR `c` `<Enter>` + URL OR `l` `<Enter>` (local only) | Catalog source set. Auto-skipped when local image-root has images. |
| 3 | SELECT_IMAGE | `1` `<Enter>` (or another row number) | Picks an image. Wizard prints the picked image at the top of the next screen. |
| 4 | SELECT_DISK | `1` `<Enter>` (or another row number) | Picks a target disk. |
| 5 | CONFIRM_FLASH | `y` `<Enter>` | Wizard shows the flash plan + validation, runs the write with a Rich progress bar. |
| 6 | (wait) | -- | Progress bar updates as the write streams; "Flash completed." panel on success. |
| 7 | REBOOT_OR_DONE | `r` `<Enter>` to reboot, `q` to quit | Boot into the freshly-written image, or stay at the wizard. |

Back-nav: `b` (or `<Backspace>`) at any stage drops the most
recent commit and returns one stage. Useful in the recording to
demonstrate undo without re-launching.

## Pre-recording sanity checklist

Before hitting `asciinema rec`:

- [ ] `BTY_IMAGES` is mounted (or `BTY_IMAGE_ROOT` points at a
      dir with at least one supported image: `.qcow2`, `.img`,
      `.img.{zst,xz,gz,bz2}`).
- [ ] The target disk is **not** mounted -- the wizard refuses
      to flash a mounted disk and shows a red Panel, which can
      be intentional content for the recording but is usually a
      distraction.
- [ ] You ran `sudo` already; without root the wizard's flash
      stage exits with a clean error.
- [ ] Terminal size is 100x28; otherwise the player crops oddly.

## Editing tip

If a long pause sneaks in mid-record (someone interrupts you, or
a disk takes longer than expected), you can post-edit with
`asciinema-edit`:

```bash
pipx install asciinema-edit
asciinema-edit speed --speed 4 --start 12 --end 45 in.cast > out.cast
asciinema-edit quantize --range '0,3' in.cast > out.cast
```

`speed --speed 4 --start 12 --end 45` 4x's seconds 12-45 (the
slow flash bit). `quantize --range '0,3'` clamps any pause longer
than 3s to exactly 3s.
