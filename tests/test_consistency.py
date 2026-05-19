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

      * ``bty.flash.probe_image_url`` HEADs the URL before
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
    blacklist, console plumbing) must appear in BOTH templates'
    ``kernel`` line (not just doc comments).

    A previous bug shape: a token gets added to one template (to
    fix a tui-mode issue), but the flash-mode template ships
    without it and the next flash-mode boot wedges on the same
    hardware. v0.20.2 ran into exactly this with plymouth.enable=0.

    The token list is asserted against the actual ``kernel`` line
    only (not the whole template body) so a comment mentioning
    a token can't spoof its presence.
    """
    tui_body = (REPO_ROOT / "src" / "bty" / "web" / "_templates" / "ipxe_tui.j2").read_text()
    flash_body = (REPO_ROOT / "src" / "bty" / "web" / "_templates" / "ipxe_flash.j2").read_text()

    def _kernel_line(body: str) -> str:
        for ln in body.splitlines():
            if ln.startswith("kernel "):
                return ln
        raise AssertionError("template has no ``kernel`` line")

    tui = _kernel_line(tui_body)
    flash = _kernel_line(flash_body)

    baseline_tokens = (
        "boot=live",
        "fetch=${bty-base}/boot/bty-netboot-x86_64.squashfs",
        "components",
        "console=tty0",
        "console=ttyS0,115200",
        "plymouth.enable=0",
        "modprobe.blacklist=nouveau",
        "nouveau.modeset=0",
        "bty.server=${bty-base}",
        "bty.mac={{ mac }}",
    )
    for token in baseline_tokens:
        assert token in tui, f"ipxe_tui.j2 kernel line missing token {token!r}: {tui!r}"
        assert token in flash, f"ipxe_flash.j2 kernel line missing token {token!r}: {flash!r}"

    # Transparency invariant: NEITHER template ships ``quiet`` on
    # the kernel cmdline. v0.22.1 retired plymouth + dropped quiet
    # from every cmdline insertion point so a wedge between two
    # ``[ OK ] Started X`` lines is immediately diagnostic.
    # Both templates' header comments contain the phrase ``NO
    # quiet`` -- this assertion checks the kernel line itself, so
    # a future edit that adds quiet (without removing the comment)
    # fails here.
    for label, line in (("ipxe_tui.j2", tui), ("ipxe_flash.j2", flash)):
        assert " quiet" not in line, (
            f"{label} kernel line carries ``quiet`` -- transparency was "
            f"the v0.22.1 deliberate choice: {line!r}"
        )


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
    # Bake scripts that emit live-env / appliance artifacts derived
    # from the bty-media trees. Each must substitute
    # ``__BTY_VERSION__`` via SOME mechanism. The substitution
    # mechanism varies by bake style:
    #
    #   * usb_iso_build.py + live_build.py shell out to ``sed -i``
    #     across the copied live-build tree (the trees mostly carry
    #     templated text files like /etc/issue, /etc/motd, the
    #     boot-banner script).
    #   * gen_userdata.py renders cloud-init user-data for the
    #     server appliance from rootfs/server/. It does the
    #     substitution in-Python via ``text.replace(...)`` because
    #     cloud-init's write_files YAML is generated string-by-
    #     string and a single sed pass over the rendered YAML
    #     would also rewrite the placeholder inside any binary
    #     base64 block.
    bake_scripts: list[tuple[Path, tuple[str, ...]]] = [
        (scripts_dir / "usb_iso_build.py", ("sed -i s/__BTY_VERSION__/",)),
        (scripts_dir / "live_build.py", ("sed -i s/__BTY_VERSION__/",)),
        # gen_userdata's in-Python replace; either spelling is fine.
        (
            scripts_dir / "gen_userdata.py",
            (
                'replace("__BTY_VERSION__"',
                "replace('__BTY_VERSION__'",
            ),
        ),
    ]
    for script, substitution_hints in bake_scripts:
        body = script.read_text()
        assert "__BTY_VERSION__" in body, (
            f"{script.name} produces a live-env / appliance artifact but contains no "
            "__BTY_VERSION__ substitution. The booted target's /etc/issue / motd "
            "/ shell prompt will carry the literal placeholder."
        )
        assert "_read_bty_version" in body, (
            f"{script.name} uses __BTY_VERSION__ but does not call "
            "``_read_bty_version`` -- the placeholder won't get replaced."
        )
        assert any(hint in body for hint in substitution_hints), (
            f"{script.name} should perform a __BTY_VERSION__ substitution "
            f"matching one of: {substitution_hints!r}."
        )


# ----------------------------------------------------------------------
# 5. All systemd unit files in includes.chroot have an [Install] section
# ----------------------------------------------------------------------


def test_systemd_units_in_live_env_declare_install_section() -> None:
    """A systemd unit without ``[Install] WantedBy=...`` can be
    enabled via systemctl but won't be picked up if anything
    queries its state (``is-enabled`` returns ``static``). The
    bty live env enables several units in
    ``hooks/normal/0900-bty-enable-services.hook.chroot``; each must
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
    v0.19.x ran into this shape with the bty-on-tty1 enable
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
    bty emits, because plymouth-quit-wait used to wedge on certain
    Intel iGPUs (MS-01, EPYC bring-up box) and any service ordered
    ``After=plymouth-quit.service`` would block forever. The
    package is retired in the bty chroot, but cmdline belt-and-
    braces stays in case a future transitively-pulled package
    reintroduces it. Insertion points:

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


# ----------------------------------------------------------------------
# 11. Boot-banner script + units are wired together
# ----------------------------------------------------------------------


def test_boot_banner_script_and_units_exist_and_are_wired() -> None:
    """The three-step boot banner (v0.22.1 plymouth replacement)
    fires at early / mid / late checkpoints so the operator sees
    "BTY step N of 3" mixed into the systemd init log. If any
    of the four pieces -- script + three units + the enable-hook
    entry -- drops out, the operator loses one of those visible
    checkpoints.

    Pin:
      * /usr/local/sbin/bty-boot-banner exists + is executable.
      * Three systemd units exist (early, mid, late).
      * The enable hook (0900-bty-enable-services.hook.chroot)
        contains ``systemctl enable bty-banner-<phase>.service``
        for each phase.
    """
    live_root = REPO_ROOT / "bty-media" / "live-build" / "config" / "includes.chroot"
    script = live_root / "usr" / "local" / "sbin" / "bty-boot-banner"
    assert script.is_file(), f"missing banner script: {script}"
    assert script.stat().st_mode & 0o111, f"banner script not +x: {script}"

    for phase in ("early", "mid", "late"):
        unit = live_root / "etc" / "systemd" / "system" / f"bty-banner-{phase}.service"
        assert unit.is_file(), f"missing banner unit: {unit}"

    hook = (
        REPO_ROOT
        / "bty-media"
        / "live-build"
        / "config"
        / "hooks"
        / "normal"
        / "0900-bty-enable-services.hook.chroot"
    )
    hook_body = hook.read_text()
    for phase in ("early", "mid", "late"):
        assert f"systemctl enable bty-banner-{phase}.service" in hook_body, (
            f"enable hook does not enable bty-banner-{phase}.service"
        )


# ----------------------------------------------------------------------
# 12. bty-boot-banner script + units stay byte-identical across trees
# ----------------------------------------------------------------------


def test_boot_banner_files_synced_across_live_env_and_server_trees() -> None:
    """The banner script + most units are duplicated between
    ``bty-media/live-build/config/includes.chroot/`` (live env)
    and ``bty-media/rootfs/server/`` (appliance) -- live-build's
    chroot includes are NOT shared with the cloud-init rootfs.
    A manual ``cp`` is the current sync mechanism; this test
    keeps the two copies honest.

    Exception: ``bty-banner-late.service`` has a slightly
    divergent ``[Unit]`` block (different commentary, different
    references in the doc comment) but the ``[Service]`` and
    ``[Install]`` sections must match byte-for-byte. Critically:
    NEITHER copy may carry a ``Before=`` directive. The unit is
    ``After=multi-user.target`` AND ``WantedBy=multi-user.target``;
    adding ``Before=<anything-also-WantedBy-multi-user.target>``
    creates an ordering cycle that systemd silently breaks by
    dropping a unit from the boot transaction. This bit us on
    v0.22.4 when the appliance's ``Before=bty-web.service`` got
    bty-web silently removed from the boot, so /healthz never
    answered. Test guards both trees against re-introducing the
    trap.
    """
    import hashlib

    live = REPO_ROOT / "bty-media" / "live-build" / "config" / "includes.chroot"
    server = REPO_ROOT / "bty-media" / "rootfs" / "server"

    # Script: byte-for-byte identical.
    live_script = live / "usr" / "local" / "sbin" / "bty-boot-banner"
    server_script = server / "usr" / "local" / "sbin" / "bty-boot-banner"
    assert live_script.is_file(), f"missing {live_script}"
    assert server_script.is_file(), f"missing {server_script}"
    assert (
        hashlib.sha256(live_script.read_bytes()).hexdigest()
        == hashlib.sha256(server_script.read_bytes()).hexdigest()
    ), (
        "bty-boot-banner drifted between the live-env and "
        "server-rootfs trees. Sync via:\n"
        f"  cp {live_script.relative_to(REPO_ROOT)} {server_script.relative_to(REPO_ROOT)}"
    )

    # Early + mid units: identical.
    for phase in ("early", "mid"):
        live_unit = live / "etc" / "systemd" / "system" / f"bty-banner-{phase}.service"
        server_unit = server / "etc" / "systemd" / "system" / f"bty-banner-{phase}.service"
        assert live_unit.read_bytes() == server_unit.read_bytes(), (
            f"bty-banner-{phase}.service drifted; sync the file"
        )

    # Late unit: [Unit] section is intentionally divergent
    # (different Before= + different commentary explaining the
    # hand-off target). [Service] + [Install] sections must
    # match -- those are the load-bearing pieces.
    live_late = (live / "etc" / "systemd" / "system" / "bty-banner-late.service").read_text()
    server_late = (server / "etc" / "systemd" / "system" / "bty-banner-late.service").read_text()

    def _section(body: str, name: str) -> str:
        """Extract the named ini-style section from a systemd unit."""
        lines: list[str] = []
        in_section = False
        for raw in body.splitlines():
            stripped = raw.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                in_section = stripped == f"[{name}]"
                continue
            if in_section and stripped:
                lines.append(raw)
        return "\n".join(lines)

    assert _section(live_late, "Service") == _section(server_late, "Service"), (
        "bty-banner-late.service [Service] block drifted; reconcile."
    )
    assert _section(live_late, "Install") == _section(server_late, "Install"), (
        "bty-banner-late.service [Install] block drifted; reconcile."
    )

    # Cycle-trap guard: NEITHER copy may have a ``Before=`` directive
    # while being ``After=multi-user.target`` + ``WantedBy=multi-user.
    # target``. See the docstring above for the v0.22.4 incident.
    for label, body in (("live", live_late), ("server", server_late)):
        unit_section = _section(body, "Unit")
        for line in unit_section.splitlines():
            assert not line.strip().startswith("Before="), (
                f"bty-banner-late.service ({label} tree) has a "
                f"``Before=`` directive: {line.strip()!r}. This "
                f"creates an ordering cycle with the multi-user."
                f"target wantedby; systemd will silently drop a "
                f"service from boot. Drop the Before= line."
            )

    # Server has the marker files; live env does not.
    server_variant = server / "etc" / "bty" / "variant"
    server_mode = server / "etc" / "bty" / "mode"
    assert server_variant.is_file() and server_variant.read_text().strip(), (
        f"server rootfs missing /etc/bty/variant marker: {server_variant}"
    )
    assert server_mode.is_file() and server_mode.read_text().strip(), (
        f"server rootfs missing /etc/bty/mode marker: {server_mode}"
    )


# ----------------------------------------------------------------------
# 13. Every Pydantic model in _models.py has a docstring
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
