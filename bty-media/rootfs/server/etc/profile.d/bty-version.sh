# shellcheck shell=sh
# Show the bty version on every interactive shell start so the
# operator can read it back without invoking ``bty --version``
# themselves. ``__BTY_VERSION__`` is substituted at bake time by
# ``cijoe/scripts/gen_userdata.py`` (same convention the live env
# uses via the live-build hook). PS1 prefix keeps the version
# visible during long shell sessions where the motd has scrolled
# off the screen.
if [ -n "${PS1:-}" ]; then
    printf 'bty __BTY_VERSION__\n'
    PS1='[bty __BTY_VERSION__] '"${PS1}"
fi
