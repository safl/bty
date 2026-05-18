"""Cross-cutting consistency invariants.

These tests don't exercise behavior; they enforce repo-structure
contracts. They catch a class of bug the behavioral tests can't:
"X exists in one place but not the other", "Y route was added
without a corresponding Z", etc. Several v0.19.x / v0.20.x bugs
were of this shape:

* /catalog/cache endpoint existed; no UI button surfaced it.
* /images route accepted GET; not HEAD -> 405.
* plymouth packages in the netboot live env where the netboot
  path explicitly didn't want them.
* __BTY_VERSION__ stamped by the USB bake script; not by the
  netboot bake script.
* Modprobe blacklist file in modprobe.d; matching kernel cmdline
  entry NOT in every cmdline insertion site.

Each test below picks one such cross-cutting invariant and pins
it. A future change that breaks the invariant fails CI rather
than reaching an operator's hardware.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------
# 1. Every @app.get / api_route that's a fetch-route also accepts HEAD
# ----------------------------------------------------------------------


def test_fetch_routes_accept_head() -> None:
    """Routes that return a ``FileResponse`` (or other large-body
    response) should accept HEAD as well as GET. Clients use HEAD
    to size buffers and check liveness without paying the byte
    transfer cost:

      * bty-tui's ``flash.probe_image_url`` HEADs the URL before
        streaming. v0.20.7's "image URL not reachable" bug was
        ``/images/{key}/{name}`` returning 405 on HEAD.
      * UEFI HTTP-Boot firmware HEADs the bootfile URL to size
        its fetch buffer before the GET (already wired for
        ``/boot/{name}``).

    Heuristic: any route whose handler returns a ``FileResponse``
    (or whose path starts with ``/images`` or ``/boot``) should
    have HEAD in its allowed methods.

    Locates the routes by AST-parsing ``src/bty/web/_app.py`` --
    cheap, no live-app needed. The check is "if the route is in
    the byte-serving family AND it doesn't list HEAD, fail".
    """
    src = (REPO_ROOT / "src" / "bty" / "web" / "_app.py").read_text()
    tree = ast.parse(src)

    fetch_route_paths = ("/images/", "/boot/")
    violations: list[str] = []

    def _check_decorator(deco: ast.expr, func_name: str) -> None:
        if not isinstance(deco, ast.Call):
            return
        # @app.get("/path") vs @app.api_route("/path", methods=...)
        attr = deco.func
        method_name: str | None = None
        if isinstance(attr, ast.Attribute):
            method_name = attr.attr
        if method_name not in ("get", "api_route"):
            return
        # Extract the path arg.
        if not deco.args:
            return
        if not isinstance(deco.args[0], ast.Constant):
            return
        path = deco.args[0].value
        if not isinstance(path, str):
            return
        if not any(path.startswith(p) for p in fetch_route_paths):
            return
        # Look for methods=[...] kwarg.
        methods: list[str] = []
        for kw in deco.keywords:
            if kw.arg == "methods" and isinstance(kw.value, ast.List):
                methods.extend(
                    item.value.upper()
                    for item in kw.value.elts
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                )
        if method_name == "get":
            methods = ["GET"]  # bare @app.get only allows GET
        if "HEAD" not in methods:
            violations.append(
                f"{func_name} @ {path!r}: methods={methods!r}, missing HEAD. "
                "Fetch-family routes (/images, /boot) must accept HEAD so "
                "clients can probe Content-Length without downloading bytes."
            )

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for deco in node.decorator_list:
                _check_decorator(deco, node.name)

    assert not violations, "fetch-route HEAD coverage gap:\n" + "\n".join(violations)


# ----------------------------------------------------------------------
# 2. Every DELETE catalog endpoint has a UI button
# ----------------------------------------------------------------------


def test_delete_catalog_endpoints_have_ui_surface() -> None:
    """``DELETE /catalog/...`` API endpoints exist for cache eviction
    + entry deletion; both must have a button in ``/ui/images.html``.
    v0.20.9 fixed a gap where ``DELETE /catalog/cache/{name}`` had
    no UI button -- operators had to curl from the shell.

    Heuristic: every ``@app.delete("/catalog/...")`` route in
    bty-web's _app.py must have at least one corresponding action
    in the JS handler in ``images.html`` (fetch with method DELETE
    targeting the same path prefix).
    """
    src = (REPO_ROOT / "src" / "bty" / "web" / "_app.py").read_text()
    template = (REPO_ROOT / "src" / "bty" / "web" / "_templates" / "ui" / "images.html").read_text()

    # Collect @app.delete("/catalog/...") paths.
    delete_paths: list[str] = []
    for m in re.finditer(r'@app\.delete\(\s*"(/catalog/[^"]+)"', src):
        path = m.group(1)
        # Strip path params for substring matching against the
        # template (the template builds URLs by concatenating
        # encodeURIComponent(name) onto a base prefix).
        prefix = re.sub(r"/\{[^}]+\}", "/", path).rstrip("/")
        delete_paths.append(prefix)

    assert delete_paths, "no @app.delete /catalog/* routes found in _app.py"

    missing = []
    for prefix in delete_paths:
        # The JS handler hits the route via fetch("<prefix>/" + encoded);
        # match on the prefix-with-slash substring.
        target = prefix.rstrip("/") + '"'
        target_slash = prefix.rstrip("/") + '/"'
        if target not in template and target_slash not in template:
            missing.append(prefix)
    assert not missing, (
        f"DELETE catalog endpoints missing UI surface in images.html: "
        f"{missing!r}. Add a button + JS handler hitting these endpoints."
    )


# ----------------------------------------------------------------------
# 3. iPXE templates carry the same baseline cmdline tokens
# ----------------------------------------------------------------------


def test_ipxe_templates_share_baseline_cmdline_tokens() -> None:
    """Both ``ipxe_tui.j2`` and ``ipxe_flash.j2`` render kernel
    cmdlines for the SAME live env. Tokens that are essential for
    the live env to boot correctly (plymouth disable, nouveau
    blacklist, console plumbing) must appear in BOTH templates.

    A previous bug shape: a token gets added to one template (to
    fix a tui-mode issue), but the flash-mode template ships
    without it and the next flash-mode boot wedges on the same
    hardware. v0.20.2 ran into exactly this with plymouth.enable=0.

    Pinned baseline shared between the two templates:
    """
    tui = (REPO_ROOT / "src" / "bty" / "web" / "_templates" / "ipxe_tui.j2").read_text()
    flash = (REPO_ROOT / "src" / "bty" / "web" / "_templates" / "ipxe_flash.j2").read_text()

    baseline_tokens = (
        "boot=live",
        "fetch=${bty-base}/boot/bty-netboot-x86_64.squashfs",
        "components",
        "quiet",
        "console=tty0",
        "console=ttyS0,115200",
        "plymouth.enable=0",
        "modprobe.blacklist=nouveau",
        "nouveau.modeset=0",
        "bty.server=${bty-base}",
        "bty.mac={{ mac }}",
    )
    for token in baseline_tokens:
        assert token in tui, f"ipxe_tui.j2 missing baseline token {token!r}"
        assert token in flash, f"ipxe_flash.j2 missing baseline token {token!r}"


# ----------------------------------------------------------------------
# 4. __BTY_VERSION__ substitution covers every bake script
# ----------------------------------------------------------------------


def test_bty_version_substitution_runs_in_every_bake_script() -> None:
    """Files in bty-media/ that carry the ``__BTY_VERSION__``
    placeholder must be reached by SOME bake script's substitution
    step. v0.20.1 fixed a gap: the USB ISO bake substituted
    __BTY_VERSION__, the netboot bake did not, so the netboot
    live env's /etc/issue / motd carried the literal placeholder
    on a booted target.

    Pin: at minimum, the two bake scripts that produce live env
    output (``usb_iso_build.py``, ``live_build.py``) must both
    contain the sed substitution incantation. Any future bake
    script that produces an artifact deriving from the same
    chroot tree must also include it.
    """
    scripts_dir = REPO_ROOT / "cijoe" / "scripts"
    # Live-env bake scripts: every script whose name ends in _build.py
    # AND that touches bty-media live-build (heuristic: contains
    # "live-build" in its source). Both should substitute the
    # version placeholder.
    bake_scripts = [
        scripts_dir / "usb_iso_build.py",
        scripts_dir / "live_build.py",
    ]
    for script in bake_scripts:
        body = script.read_text()
        assert "__BTY_VERSION__" in body, (
            f"{script.name} touches the live-build tree but contains no "
            "__BTY_VERSION__ substitution. The booted live env's /etc/issue "
            "/ motd / shell prompt will carry the literal placeholder."
        )
        # Must call _read_bty_version (or import it) AND run the sed
        # substitution against the build dir.
        assert "_read_bty_version" in body, (
            f"{script.name} uses __BTY_VERSION__ but does not call "
            "``_read_bty_version`` -- the placeholder won't get replaced."
        )
        assert "sed -i s/__BTY_VERSION__/" in body, (
            f"{script.name} should run the canonical sed substitution "
            "``sed -i s/__BTY_VERSION__/<version>/g`` against the build dir."
        )


# ----------------------------------------------------------------------
# 5. All systemd unit files in includes.chroot have an [Install] section
# ----------------------------------------------------------------------


def test_systemd_units_in_live_env_declare_install_section() -> None:
    """A systemd unit without ``[Install] WantedBy=...`` can be
    enabled via systemctl but won't be picked up if anything
    queries its state (``is-enabled`` returns ``static``). The
    bty live env enables several units in
    ``hooks/normal/0900-bty-enable-flash.hook.chroot``; each must
    actually have an [Install] section or the enable is a no-op.
    """
    units_dir = (
        REPO_ROOT
        / "bty-media"
        / "live-build"
        / "config"
        / "includes.chroot"
        / "etc"
        / "systemd"
        / "system"
    )
    if not units_dir.exists():
        pytest.skip("no live-build systemd units dir")
    missing = []
    for unit in units_dir.rglob("*.service"):
        if unit.is_symlink():
            continue
        body = unit.read_text()
        if "[Install]" not in body:
            missing.append(unit.relative_to(REPO_ROOT))
    assert not missing, (
        f"systemd .service units missing [Install] section: {missing}. "
        f"``systemctl enable`` is a no-op for static units."
    )


# ----------------------------------------------------------------------
# 6. Every hook file in hooks/normal/ has executable bit set
# ----------------------------------------------------------------------


def test_chroot_hooks_are_executable() -> None:
    """live-build runs hooks via direct exec, not ``sh <hook>``.
    A hook without the executable bit silently fails to run --
    no warning in the log, no error, just the configuration that
    the hook would have applied is missing in the final squashfs.
    v0.19.x ran into this shape with the bty-tui-on-tty1 enable
    hook briefly.
    """
    hooks_dir = REPO_ROOT / "bty-media" / "live-build" / "config" / "hooks" / "normal"
    if not hooks_dir.exists():
        pytest.skip("no live-build hooks dir")
    non_exec = []
    for hook in sorted(hooks_dir.iterdir()):
        if hook.suffix not in (".chroot", ".binary"):
            continue
        if not hook.stat().st_mode & 0o111:
            non_exec.append(hook.relative_to(REPO_ROOT))
    assert not non_exec, (
        f"chroot hooks missing executable bit: {non_exec}. live-build will silently skip them."
    )


# ----------------------------------------------------------------------
# 7. Every iPXE / live cmdline path includes plymouth.enable=0
# ----------------------------------------------------------------------


def test_plymouth_kill_token_on_every_cmdline_insertion_point() -> None:
    """``plymouth.enable=0`` must appear on every kernel cmdline
    bty emits, because plymouth-quit-wait wedges on certain
    Intel iGPUs and bty-flash-on-boot / bty-tui-on-tty1 have
    ``After=plymouth-quit.service`` deps. Insertion points:

      * iPXE templates (already tested elsewhere)
      * live-build auto/config BOOTAPPEND (both branches)
      * cloud-init's GRUB_CMDLINE_LINUX_DEFAULT EXTRA on the
        server appliance

    Asserts the token is present in every cmdline insertion site
    rather than only on some.
    """
    cmdline_sources = (
        REPO_ROOT / "src" / "bty" / "web" / "_templates" / "ipxe_tui.j2",
        REPO_ROOT / "src" / "bty" / "web" / "_templates" / "ipxe_flash.j2",
        REPO_ROOT / "bty-media" / "live-build" / "auto" / "config",
        REPO_ROOT / "bty-media" / "auxiliary" / "cloudinit-base-server.user",
    )
    missing = []
    for path in cmdline_sources:
        body = path.read_text()
        if "plymouth.enable=0" not in body:
            missing.append(path.relative_to(REPO_ROOT))
    assert not missing, (
        f"plymouth.enable=0 missing from cmdline insertion sites: {missing}. "
        "Hardware that wedges on plymouth-quit-wait will hang the boot."
    )


# ----------------------------------------------------------------------
# 8. ssh credentials documented in /etc/issue if sshd is installed
# ----------------------------------------------------------------------


def test_live_env_etc_issue_documents_ssh_when_sshd_is_installed() -> None:
    """If the live env ships sshd (operator-targeted remote
    diagnostic access), the /etc/issue banner the operator sees
    on tty1 must tell them how to connect. A baked sshd that
    isn't advertised is invisible -- the operator who's seeing
    a wedge has no way to know they can ssh in.

    Invariant: when ``openssh-server`` is in any bty-media
    package list, ``/etc/issue`` must mention ``ssh`` somewhere.
    """
    pkg_lists_dir = REPO_ROOT / "bty-media" / "live-build" / "config" / "package-lists"
    ships_sshd = any(
        "openssh-server" in p.read_text() for p in pkg_lists_dir.glob("*.list.chroot*")
    )
    etc_issue = (
        REPO_ROOT / "bty-media" / "live-build" / "config" / "includes.chroot" / "etc" / "issue"
    )
    if not ships_sshd:
        pytest.skip("sshd not in any live-env package list; nothing to advertise")
    assert etc_issue.exists(), "/etc/issue is missing despite sshd being baked"
    body = etc_issue.read_text()
    assert "ssh" in body.lower(), (
        "live env ships sshd but /etc/issue does not document how to ssh in. "
        "An operator looking at the console can't find the credential."
    )


# ----------------------------------------------------------------------
# 9. Pyproject version + git tag invariant
# ----------------------------------------------------------------------


def test_pyproject_version_is_well_formed() -> None:
    """``pyproject.toml``'s ``version`` must be a well-formed
    semver string (PEP 440 release segment). Common breakage mode:
    a release commit accidentally lands with a trailing newline,
    quote-escape goof, or non-monotonic version -- PyPI rejects
    the upload silently in some shapes.
    """
    import tomllib

    body = (REPO_ROOT / "pyproject.toml").read_text()
    parsed = tomllib.loads(body)
    version = parsed["project"]["version"]
    assert isinstance(version, str), version
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:[a-zA-Z0-9.+-]+)?", version), version


# ----------------------------------------------------------------------
# 10. Every entry in MEMORY.md resolves to a file
# ----------------------------------------------------------------------


def test_memory_md_index_entries_resolve() -> None:
    """The auto-memory index at
    ``.claude/projects/.../memory/MEMORY.md`` is shared with the
    project (one of the few CLAUDE-side files committed). Each
    line is ``- [Title](file.md) - description``. Every
    referenced .md file must exist next to MEMORY.md, or the
    index is stale.

    Skipped if the project isn't using auto-memory.
    """
    memory_md_candidates = list(
        (REPO_ROOT / ".claude").rglob("MEMORY.md") if (REPO_ROOT / ".claude").exists() else []
    )
    if not memory_md_candidates:
        pytest.skip("no .claude/.../MEMORY.md in repo (auto-memory unused)")
    memory_md = memory_md_candidates[0]
    body = memory_md.read_text()
    refs = re.findall(r"\]\(([^)]+\.md)\)", body)
    missing = [r for r in refs if not (memory_md.parent / r).exists()]
    assert not missing, f"MEMORY.md references missing files: {missing}"


# ----------------------------------------------------------------------
# 11. Plymouth script scales the logo to a framebuffer-sane size
# ----------------------------------------------------------------------


def test_plymouth_script_scales_logo() -> None:
    """The plymouth script must scale the source PNG before
    rendering. The mascot asset is the same 600x600 PNG synced
    across docs / web / plymouth via the test_smoke invariant; on
    a framebuffer console that's larger than the boot screen would
    show without scaling. Regression: v0.20.4 synced the asset
    without adding the scale call, so the next bty-usb boot showed
    a "huge zoomed critter" instead of a centred splash.

    Heuristic: the script must contain an ``Image(...).Scale(...)``
    or a ``.Scale(`` invocation against the logo image variable.
    """
    script_path = (
        REPO_ROOT
        / "bty-media"
        / "live-build"
        / "config"
        / "includes.chroot"
        / "usr"
        / "share"
        / "plymouth"
        / "themes"
        / "bty"
        / "bty.script"
    )
    body = script_path.read_text()
    assert ".Scale(" in body, (
        "plymouth script does not call .Scale() on the logo image. "
        "A 600x600 source PNG renders at native size on the framebuffer "
        "console, which looks like a huge zoomed mascot. Scale to a "
        "fraction of Window.GetWidth() / Window.GetHeight()."
    )


# ----------------------------------------------------------------------
# 12. Every Pydantic model in _models.py has a docstring
# ----------------------------------------------------------------------


def test_pydantic_models_have_docstrings() -> None:
    """Every public Pydantic model in ``bty.web._models`` should
    carry a docstring -- they appear in /docs (FastAPI auto-
    generates OpenAPI schemas from them) AND show up in the
    Sphinx API ref. Missing docstrings produce empty schema
    descriptions, which makes operators / API consumers parse
    the field names alone.
    """
    src = (REPO_ROOT / "src" / "bty" / "web" / "_models.py").read_text()
    tree = ast.parse(src)
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name.startswith("_"):
            continue
        if not ast.get_docstring(node):
            missing.append(node.name)
    assert not missing, (
        f"Pydantic models missing docstrings: {missing}. "
        "OpenAPI schemas will surface empty descriptions."
    )
