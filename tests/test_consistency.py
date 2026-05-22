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


def test_local_boot_ipxe_templates_are_firmware_aware() -> None:
    """Any iPXE template that runs ``sanboot --drive`` must guard it
    behind ``iseq ${platform} efi``.

    ``sanboot --drive`` uses BIOS INT13 drive numbering, which does not
    exist on UEFI -- so an unguarded one fails on every UEFI box (the
    common case). The guard makes UEFI ``exit`` to the firmware boot
    order instead. This pins the fix for ipxe_sanboot.j2 + ipxe_unknown.j2;
    a bare BIOS sanboot in any served template is exactly how UEFI
    netboot stayed broken (the QEMU chain test is BIOS-only).
    """
    tdir = REPO_ROOT / "src" / "bty" / "web" / "_templates"
    for tpl in sorted(tdir.glob("*.j2")):
        text = tpl.read_text()
        runs_sanboot = any(
            "sanboot --" in ln and not ln.lstrip().startswith("#") for ln in text.splitlines()
        )
        if runs_sanboot:
            assert "iseq ${platform} efi" in text, (
                f"{tpl.name} runs `sanboot --drive` (BIOS-only) without an "
                "`iseq ${platform} efi` UEFI guard -- it will fail on UEFI targets"
            )


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
        # Don't let systemd-gpt-auto-generator auto-mount the flash
        # target's existing partitions in the live env.
        "systemd.gpt_auto=0",
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


# ----------------------------------------------------------------------
# 14. Docker bty UID stays in sync across Dockerfile + Makefile + compose
# ----------------------------------------------------------------------


def test_subnav_pill_keys_match_route_validator_whitelist() -> None:
    """Each /ui page with a sub-nav strip defines its pill set in
    the template (``{% with sections=[{"key": ..., ...}, ...] %}``)
    AND validates ``?section=`` in the route handler. A drift
    between the two surfaces is a real bug shape: an operator
    clicks a freshly-added pill -> the route's validator falls
    back to ``list`` -> the operator sees the wrong page.

    Walks the boot / images / machines templates' first
    ``sections=[...]`` block, extracts the pill ``key`` values,
    and asserts each is present in the matching
    ``if section not in (...)`` whitelist in ``_ui.py``.
    """
    import re

    ui_dir = REPO_ROOT / "src" / "bty" / "web" / "_templates" / "ui"
    ui_py = (REPO_ROOT / "src" / "bty" / "web" / "_ui.py").read_text()

    def _pill_keys(tmpl: str) -> list[str]:
        body = (ui_dir / tmpl).read_text()
        m = re.search(r"{% with sections=\[(.*?)\] ,", body, re.DOTALL)
        if not m:
            return []
        return re.findall(r'"key":\s*"([^"]+)"', m.group(1))

    # The route validator that gates the ?section= path on each
    # page lives inside ``_ui.py`` as ``section not in (...)``
    # tuples. Walk them out of the source via a regex scan.
    validators = re.findall(r"section\s+not\s+in\s+\(([^)]+)\)", ui_py)
    validator_keys = {
        k.strip().strip('"') for tup in validators for k in tup.split(",") if k.strip()
    }

    drifts = [
        f"{tmpl} renders pill ``{key}`` but no _ui.py validator whitelists it"
        for tmpl in ("netboot.html", "images.html", "machines.html")
        for key in _pill_keys(tmpl)
        if key not in validator_keys
    ]
    assert not drifts, "\n".join(drifts)


def test_every_ui_page_uses_the_intro_box_partial() -> None:
    """Every operator-facing /ui page (dashboard / machines /
    images / boot / events / settings) renders its intro
    paragraph through the shared ``ui/_intro_box.html`` partial
    rather than open-coding the ``alert alert-info ...`` DOM.

    Pinning the contract here means a future page that lands a
    bespoke info-box can't drift from the canonical styling --
    the test fails CI when the partial isn't used, prompting
    the author to either import the macro or extend the
    partial (e.g. with a colour variant) intentionally.
    """
    ui_dir = REPO_ROOT / "src" / "bty" / "web" / "_templates" / "ui"
    pages = (
        "dashboard.html",
        "machines.html",
        "images.html",
        "netboot.html",
        "events.html",
        "settings.html",
    )
    intro_box_import = '_intro_box.html" import render as intro_box'
    bare_alert_info = 'class="alert alert-info'
    missing = []
    open_coded = []
    for name in pages:
        body = (ui_dir / name).read_text()
        if "{% block intro %}" not in body:
            continue  # page deliberately has no intro
        if intro_box_import not in body:
            missing.append(name)
        if bare_alert_info in body:
            open_coded.append(name)
    assert not missing, (
        f"pages with an intro block but not using ``_intro_box.html``: {missing}. "
        f"Either import the partial or remove the intro block."
    )
    assert not open_coded, (
        f"pages still open-code an ``alert alert-info`` div instead of "
        f"calling the partial: {open_coded}. Migrate to "
        f"``{{% call intro_box() %}}...{{% endcall %}}``."
    )


def test_plan_modes_emitted_by_server_are_handled_by_the_client() -> None:
    """Every ``plan.mode`` the server can return from
    ``GET /pxe/{mac}/plan`` (``_app.py``) must be handled explicitly by
    the live-env client's ``_fetch_and_dispatch_plan`` (``tui/_app.py``).

    The client falls back to ``interactive`` for an unknown mode, so a
    server-side mode added without a matching client branch wouldn't
    crash -- it would just silently drop the operator into the wizard
    instead of doing what the new mode intended (e.g. mode=inventory
    posting disks + rebooting). Pin the server->client contract so that
    drift fails CI instead.
    """
    server = (REPO_ROOT / "src" / "bty" / "web" / "_app.py").read_text()
    client = (REPO_ROOT / "src" / "bty" / "tui" / "_app.py").read_text()
    emitted = set(re.findall(r'"mode": "(\w+)"', server))
    handled = set(re.findall(r'mode == "(\w+)"', client))
    assert emitted, "no plan modes found in _app.py -- regex drifted?"
    missing = emitted - handled
    assert not missing, (
        f"server emits plan mode(s) {sorted(missing)} that the live-env client "
        f"(_fetch_and_dispatch_plan) doesn't handle -- they'd silently fall back to "
        f"the interactive wizard. Add a matching ``mode == ...`` branch."
    )


def test_every_boot_policy_has_a_machine_row_badge() -> None:
    """Every ``BOOT_POLICIES`` value must have an explicit
    ``m.boot_policy == '<value>'`` badge case in ``_machine_row.html``.

    The template ends in an ``{% else %}`` "unrecognised boot policy
    (stale record)" fallback; without this pin a newly-added policy
    would silently render with that grey fallback badge (wrong colour,
    wrong tooltip) instead of its own. Mirrors the
    decision-tree-coverage pin -- the policy set is one source of
    truth, the badge ladder another.
    """
    from bty.web._models import BOOT_POLICIES

    body = (
        REPO_ROOT / "src" / "bty" / "web" / "_templates" / "ui" / "_machine_row.html"
    ).read_text()
    cased = set(re.findall(r"m\.boot_policy == '([^']+)'", body))
    missing = [p for p in BOOT_POLICIES if p not in cased]
    assert not missing, (
        f"boot policies with no explicit badge case in _machine_row.html: {missing!r}. "
        f"They'd fall to the grey 'unrecognised' badge. Add a {{% elif %}} branch."
    )


def test_every_recorded_event_kind_is_registered() -> None:
    """Every event ``kind=`` recorded anywhere in src must be in
    ``KNOWN_EVENT_KINDS`` -- the catalogue that drives the /ui/events
    filter dropdown (and the docs event-kind table). An event recorded
    under an unregistered kind still lands in the DB, but the operator
    can't filter for it and it's invisible to the catalogue, so it
    silently drifts.

    The regex matches a dotted lowercase kind value not preceded by a
    word char or dot, which excludes ``subject_kind=`` / ``flash_kind=``
    / ``s.kind`` and only catches the event-kind ``kind="..."`` form.
    """
    from bty.web._events_log import KNOWN_EVENT_KINDS

    known = set(KNOWN_EVENT_KINDS)
    pat = re.compile(r'(?<![\w.])kind="([a-z][a-z0-9]*(?:\.[a-z0-9_]+)+)"')
    recorded: set[str] = set()
    for f in (REPO_ROOT / "src" / "bty").rglob("*.py"):
        for m in pat.finditer(f.read_text()):
            recorded.add(m.group(1))
    assert recorded, "no event kinds matched -- the regex drifted?"
    missing = sorted(recorded - known)
    assert not missing, (
        "event kinds recorded in src but missing from KNOWN_EVENT_KINDS "
        f"(so absent from the /ui/events filter dropdown): {missing}. "
        "Add them to KNOWN_EVENT_KINDS in _events_log.py."
    )


def test_pxe_chain_test_uses_a_valid_boot_policy() -> None:
    """The QEMU PXE chain test (the release's integration gate) PUTs a
    machine assignment with a literal ``boot_policy``; it must be a
    current ``BOOT_POLICIES`` member.

    A stale name 422s the assignment and fails the whole release before
    PyPI publish -- exactly what a v0.23.0 release run hit, because the
    ``flash`` -> ``bty-flash-always`` rename updated src/tests/docs but
    missed this cijoe harness. The QEMU test only runs in the release
    pipeline (needs media + qemu), so this string check runs in plain
    ``make ci`` to catch the drift early.
    """
    from bty.web._models import BOOT_POLICIES

    src = (REPO_ROOT / "cijoe" / "scripts" / "pxe_run_chain_test.py").read_text()
    m = re.search(r'"boot_policy":\s*"([^"]+)"', src)
    assert m is not None, "no boot_policy literal found in pxe_run_chain_test.py"
    assert m.group(1) in BOOT_POLICIES, (
        f"pxe_run_chain_test.py sends boot_policy={m.group(1)!r}, which is not in "
        f"BOOT_POLICIES {BOOT_POLICIES!r}. The assignment PUT will 422 and fail the "
        f"release's PXE chain gate. Update the harness to a current policy."
    )


def test_live_env_boot_ordering_invariants() -> None:
    """Pin the load-bearing live-env systemd ordering. Each of these is
    silent if broken (systemd just reorders / drops a unit) but every
    one prevents a known boot-failure class:

      * ``bty-on-tty1`` After ``network-online.target`` -- ``bty`` has
        to reach the server over HTTP, so the network must be up first.
      * ``bty-clock-from-http`` Before ``bty-on-tty1`` -- a wrong clock
        breaks TLS / apt-signature / oras checks against the server
        (the v0.19.6 clock-skew incident). The clock must be stepped
        before ``bty`` does any HTTP/TLS, which is every boot policy
        (flash / tui / inventory all fetch from the server).
      * ``var-lib-bty-images.mount`` has ``ConditionPathExists`` +
        ``nofail`` -- the SAME image boots over USB (has a BTY_IMAGES
        partition) and over the network (does not); a hard mount would
        stall every netboot ~90s waiting for a device that never
        appears.

    See also ``test_boot_banner_files_synced_across_live_env_and_server_trees``
    for the matching "no Before= on the banner units" cycle guard.
    """
    units = (
        REPO_ROOT
        / "bty-media"
        / "live-build"
        / "config"
        / "includes.chroot"
        / "etc"
        / "systemd"
        / "system"
    )
    tty1 = (units / "bty-on-tty1.service").read_text()
    assert re.search(r"^After=.*\bnetwork-online\.target\b", tty1, re.MULTILINE), (
        "bty-on-tty1.service must be ordered After network-online.target -- bty "
        "reaches the server over HTTP and needs the network up first."
    )

    clock = (units / "bty-clock-from-http.service").read_text()
    clock_before = [ln for ln in clock.splitlines() if ln.startswith("Before=")]
    assert any("bty-on-tty1.service" in ln for ln in clock_before), (
        "bty-clock-from-http.service must be ordered Before bty-on-tty1.service -- "
        "a skewed clock breaks TLS/oras to the server, so step it before bty runs."
    )

    mount = (units / "var-lib-bty-images.mount").read_text()
    assert "ConditionPathExists=/dev/disk/by-label/BTY_IMAGES" in mount, (
        "var-lib-bty-images.mount must be gated on the BTY_IMAGES device existing, "
        "so it's skipped on a netboot env that has no such partition."
    )
    assert re.search(r"^Options=.*\bnofail\b", mount, re.MULTILINE), (
        "var-lib-bty-images.mount must use nofail, or every netboot wastes ~90s "
        "waiting for a BTY_IMAGES device that never appears."
    )


def test_live_env_chains_tag_boot_urls_with_mac() -> None:
    """The live-env iPXE chains (``ipxe_flash.j2`` + ``ipxe_tui.j2``)
    must tag every ``/boot`` artifact URL with ``?mac={{ mac }}``.

    That query string is what arms the ``saw_flasher_boot`` bit: the
    server keys the one-shot sanboot off seeing ``GET /boot/...?mac=``
    (proof the box booted the live env). It drives the
    ``bty-flash-always`` AND ``bty-inventory`` alternation. Drop the
    ``?mac=`` and a box never arms -> it reflashes / re-inventories on
    every PXE boot WITHOUT ever sanbooting the disk: the exact
    under-PXE-first loop those policies were built to break. Pin it so a
    future template edit can't silently reintroduce the loop.
    """
    tmpl_dir = REPO_ROOT / "src" / "bty" / "web" / "_templates"
    # The kernel + initrd URLs are fetched by iPXE (handles query
    # strings fine) and MUST carry ?mac= -- that's what arms the bit.
    # The squashfs URL is fetched by live-boot's initramfs, which
    # derives a local filename from it and CHOKES on the ?/: in a
    # ?mac= query ("Unable to find a live file system on the network");
    # it MUST NOT be tagged. The kernel/initrd fetch already armed the
    # bit, so the squashfs tag is redundant anyway. Pin BOTH directions
    # so neither the loop bug (no tag) nor the live-boot bug (squashfs
    # tagged) can recur.
    artifact_re = re.compile(
        r"/boot/bty-netboot-x86_64\.(vmlinuz|initrd|squashfs)(\?mac=\{\{ mac \}\})?"
    )
    violations: list[str] = []
    for name in ("ipxe_flash.j2", "ipxe_tui.j2"):
        body = (tmpl_dir / name).read_text()
        seen: set[str] = set()
        for m in artifact_re.finditer(body):
            artifact, tag = m.group(1), m.group(2)
            seen.add(artifact)
            if artifact in ("vmlinuz", "initrd") and tag is None:
                violations.append(f"{name}: {artifact} URL missing ?mac= (arming won't fire)")
            if artifact == "squashfs" and tag is not None:
                violations.append(
                    f"{name}: squashfs fetch= URL carries ?mac= -- live-boot can't fetch it"
                )
        for required in ("vmlinuz", "initrd", "squashfs"):
            assert required in seen, f"{name}: no {required} URL found at all"
    assert not violations, "live-env chain ?mac= tagging is wrong:\n" + "\n".join(violations)


def test_every_boot_policy_is_handled_by_the_pxe_decision_tree() -> None:
    """Every value in ``BOOT_POLICIES`` must appear as a literal string
    in ``_app.py`` -- the PXE handler decides what to serve with an
    explicit per-policy branch (``policy == "sanboot"``, ``policy in
    ("bty-flash-always", "bty-flash-once")``, ...). A policy added to
    the tuple (or renamed) without a matching branch silently falls
    through to the default, serving the wrong thing on real hardware.

    This is the exact bug shape the bty-* rename risked: the policy
    set is one source of truth, the decision tree another, and they
    can drift. Pin them together. The dropdown UI is already
    drift-proof (it loops over ``BOOT_POLICIES`` directly), so the
    decision tree is the surface that needs a guard.
    """
    from bty.web._models import BOOT_POLICIES

    src = (REPO_ROOT / "src" / "bty" / "web" / "_app.py").read_text()
    missing = [p for p in BOOT_POLICIES if f'"{p}"' not in src]
    assert not missing, (
        f"boot policies in BOOT_POLICIES with no literal branch in _app.py: "
        f"{missing!r}. The PXE decision tree handles each policy explicitly; "
        f"a policy with no branch falls through to the default and serves the "
        f"wrong iPXE config. Add (or fix) the branch in the GET /pxe/{{mac}} "
        f"handler."
    )


def test_bty_web_help_documents_every_env_var() -> None:
    """Every ``BTY_*`` env var the bty-web runtime reads from
    ``os.environ`` must be documented in the argparse description
    that ``bty-web --help`` prints. Without this pin a future
    "read BTY_FOO_BAR" addition can land without the operator-
    facing surface picking it up, and the only way to discover
    the knob is grep-the-source.

    Scans ``__init__.py`` (the entry point), ``_app.py`` and
    ``_ui.py`` (which read e.g. ``BTY_TRUSTED_PROXY`` /
    ``BTY_BOOT_RELEASE_REPO`` directly) for the read sites, and
    asserts each name appears in the ``--help`` description block
    that lives in ``__init__.py``.
    """
    web = REPO_ROOT / "src" / "bty" / "web"
    help_body = (web / "__init__.py").read_text()
    # Read sites across the runtime modules.
    env_keys: set[str] = set()
    for name in ("__init__.py", "_app.py", "_ui.py"):
        src = (web / name).read_text()
        env_keys.update(re.findall(r'os\.environ\.get\(\s*"(BTY_[A-Z0-9_]+)"', src))
        env_keys.update(re.findall(r'os\.environ\[\s*"(BTY_[A-Z0-9_]+)"', src))
    # BTY_QUIET is a docker-entrypoint shell knob, not read by the
    # Python runtime; not expected in the Python --help.
    env_keys.discard("BTY_QUIET")
    assert env_keys, "scan should find at least one BTY_* lookup"

    # The name must appear in the --help description block (we
    # approximate "in user-facing help" as "present in __init__.py
    # outside its own os.environ read line").
    help_outside = re.sub(r"os\.environ(?:\.get)?[\[(][^\])]*[\])]", "", help_body)
    missing = sorted(k for k in env_keys if k not in help_outside)
    assert not missing, (
        f"bty-web --help is missing env-var documentation for: {missing}. "
        f"Add a line under the argparse description in "
        f"src/bty/web/__init__.py."
    )


def test_dnsmasq_tftp_root_agrees_across_deployment_shapes() -> None:
    """The bty-web container (docker/dnsmasq.conf) and the
    bty-server appliance (rootfs .../dnsmasq.d/bty-pxe.conf) both
    run dnsmasq as the TFTP daemon. They must agree on the
    ``tftp-root`` -- it's where the iPXE binaries (``ipxe.efi`` /
    ``undionly.kpxe``) are staged, and a PXE client that fetches
    a bootfile from one path while the daemon serves another just
    404s. The Dockerfile stages the binaries into that same root,
    so pin all three to one another.
    """
    docker_conf = (REPO_ROOT / "docker" / "dnsmasq.conf").read_text()
    appliance_conf = (
        REPO_ROOT / "bty-media" / "rootfs" / "server" / "etc" / "dnsmasq.d" / "bty-pxe.conf"
    ).read_text()
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text()

    def _tftp_root(conf: str) -> str | None:
        m = re.search(r"^tftp-root=(\S+)", conf, re.MULTILINE)
        return m.group(1) if m else None

    docker_root = _tftp_root(docker_conf)
    appliance_root = _tftp_root(appliance_conf)
    assert docker_root, "docker/dnsmasq.conf must set tftp-root"
    assert appliance_root, "appliance bty-pxe.conf must set tftp-root"
    assert docker_root == appliance_root, (
        f"dnsmasq tftp-root mismatch: docker={docker_root!r} vs "
        f"appliance={appliance_root!r}. PXE clients would 404 on "
        f"the bootfile from one deployment shape."
    )
    # The Dockerfile must stage the iPXE binaries into that same root.
    assert f"{docker_root}/ipxe.efi" in dockerfile, (
        f"Dockerfile should stage ipxe.efi into {docker_root} (the configured tftp-root)"
    )


def test_docker_bty_uid_aligned_across_surfaces() -> None:
    """The Dockerfile pins the in-container bty user to a fixed
    UID; the Makefile ``docker-run`` target chowns the host-side
    bind-mount to the same UID; the docker-compose comments
    document it. If any of those three drift the
    ``make docker-clean docker-build docker-run`` flow comes up
    and immediately exits 1 (the entrypoint's writability
    preflight kicks in) and the operator sees nothing on
    http://localhost:8080/ui -- the exact bug v0.22.11 shipped.

    Pin the alignment so a future Dockerfile bump that changes
    the UID has to also bump the Makefile + compose surfaces.
    """
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text()
    makefile = (REPO_ROOT / "Makefile").read_text()
    compose = (REPO_ROOT / "docker" / "docker-compose.yml").read_text()

    uid_match = re.search(r"useradd\s+--uid\s+(\d+)\s+--gid\s+\d+", dockerfile)
    assert uid_match, (
        "Dockerfile must pin the bty user to a fixed UID via "
        "``useradd --uid N --gid N``. Without an explicit UID the "
        "value drifts when apt's package order changes."
    )
    uid = uid_match.group(1)

    chown_match = re.search(r"chown\s+-R\s+(\d+):(\d+)\s+bty-data", makefile)
    assert chown_match, "Makefile docker-run target must chown bty-data"
    assert chown_match.group(1) == uid and chown_match.group(2) == uid, (
        f"Makefile chowns bty-data to {chown_match.group(1)}:{chown_match.group(2)} "
        f"but Dockerfile pins bty to uid {uid}. Align them or the "
        f"entrypoint's writability preflight will reject the bind-mount."
    )

    # docker-compose.yml documents the UID in operator comments.
    # Look for the literal "(uid N" so a future operator copying
    # the chown command finds the right number.
    assert f"(uid {uid}" in compose, (
        f"docker-compose.yml comments should reference uid {uid} "
        f"(matching the Dockerfile pin); operator copy-paste relies "
        f"on that number being current."
    )


def test_docker_run_and_compose_publish_same_ports() -> None:
    """``make docker-run`` (the manual single-command path) and
    ``docker-compose.yml`` (the orchestrated path) should publish
    the same port set. Otherwise the operator's mental model
    silently differs between deployment shapes -- e.g. compose
    serves TFTP but the make target doesn't, and a PXE client
    appears to be unreachable from one but not the other.
    """
    makefile = (REPO_ROOT / "Makefile").read_text()
    compose = (REPO_ROOT / "docker" / "docker-compose.yml").read_text()

    # Match ``-p HOST:CONTAINER[/proto]`` flags in the docker-run
    # rule. ``-p 69:69/udp`` -> ("69", "69", "udp"). TCP is the
    # default when /proto is missing; normalise.
    make_ports = {
        f"{host}:{cont}/{proto or 'tcp'}"
        for host, cont, proto in re.findall(r"-p\s+(\d+):(\d+)(?:/(tcp|udp))?", makefile)
    }
    # docker-compose YAML: ``"HOST:CONTAINER[/proto]"`` strings
    # inside the ports list. Same regex shape.
    compose_ports = {
        f"{host}:{cont}/{proto or 'tcp'}"
        for host, cont, proto in re.findall(r'"(\d+):(\d+)(?:/(tcp|udp))?"', compose)
    }
    assert make_ports == compose_ports, (
        f"Makefile docker-run ports {sorted(make_ports)} != "
        f"docker-compose.yml ports {sorted(compose_ports)}. "
        f"One deployment shape can't reach a service the other "
        f"can; align them or the operator gets surprises."
    )


def test_docker_healthcheck_honors_configured_port() -> None:
    """The container's HEALTHCHECK must probe the *configured*
    ``BTY_WEB_PORT``, not a hardcoded port. An operator who
    overrides the port (``docker run -e BTY_WEB_PORT=9000``) would
    otherwise get a permanently-unhealthy container -- the probe
    keeps hitting the stale default while bty-web listens
    elsewhere. The fix is shell-form CMD with runtime env
    expansion (``${BTY_WEB_PORT:-8080}``); guard against a
    regression back to a literal ``:8080`` in the probe URL.
    """
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text()
    healthcheck = next(
        (
            line
            for line in dockerfile.splitlines()
            if "curl" in line and "healthz" in line and "http" in line
        ),
        None,
    )
    assert healthcheck is not None, "Dockerfile HEALTHCHECK curl line not found"
    assert "${BTY_WEB_PORT" in healthcheck, (
        f"HEALTHCHECK must expand BTY_WEB_PORT at runtime, got: {healthcheck.strip()!r}. "
        f"A hardcoded port breaks the health probe for any operator who overrides it."
    )
    assert "127.0.0.1:8080/healthz" not in healthcheck, (
        "HEALTHCHECK appears to hardcode :8080 again -- use ${BTY_WEB_PORT:-8080}."
    )


def test_publish_scripts_write_basename_into_sha256_sidecar() -> None:
    """Every bake/publish script that emits a ``.sha256`` sidecar via
    a shell ``sha256sum ... > ...`` redirect must ``cd`` into the
    artifact's directory first, so the recorded filename is a
    BASENAME rather than the absolute build-host path.

    Otherwise an operator's ``sha256sum -c <artifact>.sha256``
    (documented in docs/src/walkthrough-*.md) looks for a
    nonexistent ``/home/runner/.../<artifact>`` and fails. The
    netboot / usb scripts already used ``cd <dir> && sha256sum
    <basename>``; img_gz_publish + diskimage_build embedded the
    absolute path until this was pinned (broke ``-c`` for the
    server-x86 image). Scripts that build the sidecar in-Python via
    ``write_text(f"{digest}  {path.name}")`` carry no shell
    ``sha256sum`` redirect and are naturally exempt.
    """
    scripts_dir = REPO_ROOT / "cijoe" / "scripts"
    for script in sorted(scripts_dir.glob("*.py")):
        for line in script.read_text(encoding="utf-8").splitlines():
            if "sha256sum" not in line or ">" not in line:
                continue  # not a sidecar-writing redirect
            assert "cd " in line, (
                f"{script.name}: a ``sha256sum ... >`` sidecar redirect lacks a "
                f"``cd <dir>`` -- the recorded path will be absolute and break "
                f"an operator's ``sha256sum -c``. Use ``cd {{dir}} && sha256sum "
                f"{{name}} > {{name}}.sha256``. Offending line: {line.strip()!r}"
            )


def test_state_disk_mounts_at_state_dir_not_images_subdir() -> None:
    """The persistent-state disk (LABEL=BTY_IMAGE_STORE) must mount at
    the whole state dir ``/var/lib/bty``, NOT the ``images/`` subdir.

    Pre-0.22.17 mounted only ``/var/lib/bty/images`` -- but the bulk of
    bty's data (the multi-GB content ``cache/`` and ``state.db``) are
    SIBLINGS of ``images/`` under the state dir, so they stayed on the
    rootfs and were lost on reflash. ``bty-state-migrate`` + the baked
    fstab line now target the state dir itself. Pin all three surfaces
    (cloud-init fstab line, the migrate script, the bty-web mount
    ordering) so the granularity bug can't regress.
    """
    cloudinit = (REPO_ROOT / "bty-media" / "auxiliary" / "cloudinit-base-server.user").read_text(
        encoding="utf-8"
    )
    fstab_lines = [
        ln for ln in cloudinit.splitlines() if "LABEL=BTY_IMAGE_STORE" in ln and "ext4" in ln
    ]
    assert fstab_lines, "no LABEL=BTY_IMAGE_STORE fstab line in the server cloud-init"
    for ln in fstab_lines:
        assert "BTY_IMAGE_STORE /var/lib/bty " in ln, (
            f"state disk must mount at /var/lib/bty (the whole state dir), not a subdir; "
            f"got: {ln.strip()!r}"
        )
        assert "/var/lib/bty/images " not in ln, (
            "fstab mounts the images/ subdir again -- that strands cache/ + state.db on the "
            "rootfs (the pre-0.22.17 bug). Mount /var/lib/bty itself."
        )

    migrate = (
        REPO_ROOT
        / "bty-media"
        / "rootfs"
        / "server"
        / "usr"
        / "local"
        / "sbin"
        / "bty-state-migrate"
    ).read_text(encoding="utf-8")
    assert "STATE_DIR=/var/lib/bty\n" in migrate, (
        "bty-state-migrate must target STATE_DIR=/var/lib/bty"
    )

    bty_web = (
        REPO_ROOT
        / "bty-media"
        / "rootfs"
        / "server"
        / "etc"
        / "systemd"
        / "system"
        / "bty-web.service"
    ).read_text(encoding="utf-8")
    assert "After=var-lib-bty.mount" in bty_web, (
        "bty-web.service must order After=var-lib-bty.mount so it reads state from the "
        "migrated disk, not the rootfs underneath a slow-to-mount disk"
    )
