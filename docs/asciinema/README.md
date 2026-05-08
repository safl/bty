# asciinema captures for the docs

Replayable scripts for recording the terminal walkthroughs that ship
with the bty docs. Each script either runs end-to-end (canned) or
documents the live keystrokes the operator should make (interactive
flows that can't be canned, like the TUI).

Why scripts instead of one-off captures? **Reproducibility.** When
the CLI surface changes (new flag, renamed subcommand, different
output shape), the recordings go stale. Re-record by replaying the
script under `asciinema rec`; you don't have to remember what to
type.

## Recording

Each canned script is meant to be passed to `asciinema rec -c`:

```bash
# Install asciinema once
pipx install asciinema

# Record a flow (replace SCRIPT.sh + OUT.cast)
asciinema rec --cols 100 --rows 28 \
    --title "bty: USB flash walkthrough" \
    -c './usb-flash-cli.sh' usb-flash-cli.cast
```

`--cols` / `--rows` matter: the embedded player honours the cast's
declared dimensions. 100x28 fits nicely in a doc page without
horizontal scroll.

## Uploading + embedding

Upload to <https://asciinema.org/>:

```bash
asciinema upload usb-flash-cli.cast
```

It returns a URL like `https://asciinema.org/a/123456`. Embed in
the markdown docs as:

```markdown
<script src="https://asciinema.org/a/123456.js" id="asciicast-123456"
        async></script>
```

For Sphinx + MyST you may want the
[`sphinxcontrib-asciinema`](https://pypi.org/project/sphinxcontrib-asciinema/)
extension; it lets you write `{asciinema}\`123456\`` directly. Add
it to ``docs/src/conf.py`` ``extensions`` and the corresponding
PyPI package to ``docs/tooling`` if you want first-class support.

## Available scripts

| Script | Type | Walkthrough |
|---|---|---|
| `usb-flash-cli.sh` | canned | Step 5b: ``bty list / inspect / flash --dry-run / flash --yes`` |
| `usb-flash-tui.md` | live notes | Step 5a: keystroke recipe for recording the TUI by hand |

Add new scripts here as walkthroughs grow; keep names short
(`<flow>-<step>.sh`) and reference them from the walkthrough page.
