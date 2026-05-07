# `bty-tui` interactive flash - keystroke recipe

The TUI is, well, interactive - there's no canned shell script that
captures the keystroke-by-keystroke flow. Use this recipe to record
a clean walkthrough by hand under `asciinema rec`.

## Setup

On a target booted from the USB live env, with at least one image on
`BTY_IMAGES`:

```bash
asciinema rec --cols 100 --rows 28 \
    --title "bty-tui: interactive flash" \
    --idle-time-limit 1.5 \
    usb-flash-tui.cast
```

`--idle-time-limit 1.5` collapses any pause longer than 1.5s in the
final cast, so reading-time pauses don't bloat the duration.

## Recipe

Below are the keystrokes the viewer should see. Take a breath
between each step (~2-3 seconds) so the cast is followable.

| # | Action | What happens |
|---|---|---|
| 1 | `sudo bty-tui` `<Enter>` | TUI launches in two panes: Images (left), Disks (right). Cursor starts in Images. |
| 2 | `<Down>` until your image is highlighted | Status bar shows the image's format + size. |
| 3 | `<Enter>` | Image is selected (visual highlight changes). |
| 4 | `<Tab>` | Cursor moves to the Disks pane. |
| 5 | `<Down>` until the target disk is highlighted | Status bar shows the disk's size + mount points. |
| 6 | `<Enter>` | Disk is selected. |
| 7 | `F` | Flash-plan modal opens. Shows: image format, virtual size, target size, validation status. |
| 8 | `<Enter>` | Confirms the flash. Plan modal closes; status modal opens with a live progress stream. |
| 9 | (wait) | Progress stream updates: `[probing]`, `[validating]`, `[writing]`, `[reread-partition-table]`, `[done]`. |
| 10 | `<Enter>` | Status modal acknowledges; back to the two panes. |
| 11 | `q` | Quit the TUI. |

## Pre-recording sanity checklist

Before hitting `asciinema rec`:

- [ ] `BTY_IMAGES` is mounted and has at least one supported image
      (`.qcow2` / `.img` / `.img.zst`).
- [ ] The target disk is **not** mounted (`bty flash` refuses to flash
      a mounted disk; the TUI surfaces the same error in red, which
      can be intentional content for the recording but is usually a
      distraction).
- [ ] You ran `sudo` already (TUI in read-only mode also works as
      content but the `F` keystroke will refuse).
- [ ] Terminal size is 100x28; otherwise the player crops oddly.

## Editing tip

If a long pause sneaks in mid-record (someone interrupts you, or a
disk takes longer than expected), you can post-edit with
`asciinema-edit`:

```bash
pipx install asciinema-edit
asciinema-edit speed --speed 4 --start 12 --end 45 in.cast > out.cast
asciinema-edit quantize --range '0,3' in.cast > out.cast
```

`speed --speed 4 --start 12 --end 45` 4x's seconds 12-45 (the slow
flash bit). `quantize --range '0,3'` clamps any pause longer than 3s
to exactly 3s.
