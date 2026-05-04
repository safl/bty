"""bty.web — HTTP server with browser UI for fleet provisioning.

Requires the ``[web]`` install extra. Real implementation (MAC-keyed
assignment, per-MAC iPXE config rendering, online CIJOE orchestration)
lands in later milestones. This is a scaffold so the ``bty-web``
console-script wiring is exercised end-to-end.
"""

import bty


def main() -> None:
    print(f"bty-web {bty.__version__}: scaffold only — web server lands in milestone 11")
