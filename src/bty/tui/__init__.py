"""bty.tui — terminal UI on top of bty.

Requires the ``[tui]`` install extra. Real implementation lands in a later
milestone; this is a scaffold so the ``bty-tui`` console-script wiring is
exercised end-to-end.
"""

import bty


def main() -> None:
    print(f"bty-tui {bty.__version__}: scaffold only — TUI lands in milestone 10")
