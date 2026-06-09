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
        # The JS handler hits the route via either
        # ``fetch("<prefix>/" + encoded)`` (path-param style) or
        # ``fetch("<prefix>?src=" + encoded)`` (query-param style;
        # /catalog/entries uses this so the operator's literal src
        # URL doesn't need URL-segment encoding tricks).
        base = prefix.rstrip("/")
        candidates = (base + '"', base + '/"', base + "?")
        if not any(c in template for c in candidates):
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
    # Render the templates against the current bty version so the
    # netboot URLs (which carry ``-v{{ bty_version }}``) are concrete
    # before token-grep -- the production renderer sets bty_version
    # as a Jinja global, so this mirrors what a real iPXE client sees.
    import jinja2

    import bty

    tpl_dir = REPO_ROOT / "src" / "bty" / "web" / "_templates"
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(tpl_dir)))
    env.globals["bty_version"] = bty.__version__
    tui_body = env.get_template("ipxe_tui.j2").render(mac="{{ mac }}")
    flash_body = env.get_template("ipxe_flash.j2").render(mac="{{ mac }}")

    def _kernel_line(body: str) -> str:
        for ln in body.splitlines():
            if ln.startswith("kernel "):
                return ln
        raise AssertionError("template has no ``kernel`` line")

    tui = _kernel_line(tui_body)
    flash = _kernel_line(flash_body)

    from bty.web._releases import ARTIFACT_NAMES

    baseline_tokens = (
        "boot=live",
        f"fetch=${{bty-base}}/boot/{ARTIFACT_NAMES[2]}",
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
    # Bake scripts that emit live-env artifacts derived from the
    # bty-media trees. Each must substitute ``__BTY_VERSION__`` via
    # SOME mechanism:
    #
    #   * usb_iso_build.py + live_build.py shell out to ``sed -i``
    #     across the copied live-build tree (the trees mostly carry
    #     templated text files like /etc/issue, /etc/motd, the
    #     boot-banner script).
    bake_scripts: list[tuple[Path, tuple[str, ...]]] = [
        (scripts_dir / "usb_iso_build.py", ("sed -i s/__BTY_VERSION__/",)),
        (scripts_dir / "live_build.py", ("sed -i s/__BTY_VERSION__/",)),
    ]
    for script, substitution_hints in bake_scripts:
        body = script.read_text()
        assert "__BTY_VERSION__" in body, (
            f"{script.name} produces a live-env artifact but contains no "
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

    Asserts the token is present in every cmdline insertion site
    rather than only on some.
    """
    cmdline_sources = (
        REPO_ROOT / "src" / "bty" / "web" / "_templates" / "ipxe_tui.j2",
        REPO_ROOT / "src" / "bty" / "web" / "_templates" / "ipxe_flash.j2",
        REPO_ROOT / "bty-media" / "live-build" / "auto" / "config",
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


def test_every_boot_mode_has_a_machine_row_badge() -> None:
    """Every ``BOOT_MODES`` value must have an explicit
    ``m.boot_mode == '<value>'`` badge case in ``_machine_row.html``.

    The template ends in an ``{% else %}`` "unrecognised boot policy
    (stale record)" fallback; without this pin a newly-added policy
    would silently render with that grey fallback badge (wrong colour,
    wrong tooltip) instead of its own. Mirrors the
    decision-tree-coverage pin -- the policy set is one source of
    truth, the badge ladder another.
    """
    from bty.web._models import BOOT_MODES

    body = (
        REPO_ROOT / "src" / "bty" / "web" / "_templates" / "ui" / "_machine_row.html"
    ).read_text()
    cased = set(re.findall(r"m\.boot_mode == '([^']+)'", body))
    missing = [p for p in BOOT_MODES if p not in cased]
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


def test_pxe_chain_test_uses_a_valid_boot_mode() -> None:
    """The QEMU PXE chain test (the release's integration gate) PUTs a
    machine assignment with a literal ``boot_mode``; it must be a
    current ``BOOT_MODES`` member.

    A stale name 422s the assignment and fails the whole release before
    PyPI publish -- exactly what a v0.23.0 release run hit, because the
    ``flash`` -> ``bty-flash-always`` rename updated src/tests/docs but
    missed this cijoe harness. The QEMU test only runs in the release
    pipeline (needs media + qemu), so this string check runs in plain
    ``make ci`` to catch the drift early.
    """
    from bty.web._models import BOOT_MODES

    src = (REPO_ROOT / "cijoe" / "scripts" / "pxe_run_chain_test.py").read_text()
    m = re.search(r'"boot_mode":\s*"([^"]+)"', src)
    assert m is not None, "no boot_mode literal found in pxe_run_chain_test.py"
    assert m.group(1) in BOOT_MODES, (
        f"pxe_run_chain_test.py sends boot_mode={m.group(1)!r}, which is not in "
        f"BOOT_MODES {BOOT_MODES!r}. The assignment PUT will 422 and fail the "
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
    # The netboot URLs carry ``-v{{ bty_version }}`` in their basenames
    # (since the version-in-filename convention landed); the regex matches
    # either the rendered version-suffixed form OR the raw-template
    # placeholder form so the test stays meaningful before and after
    # rendering.
    artifact_re = re.compile(
        r"/boot/bty-netboot-x86_64-v[^.]+\.(vmlinuz|initrd|squashfs)"
        r"(\?mac=\{\{ mac \}\})?"
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


def test_every_boot_mode_is_handled_by_the_pxe_decision_tree() -> None:
    """Every value in ``BOOT_MODES`` must appear as a literal string
    in ``_app.py`` -- the PXE handler decides what to serve with an
    explicit per-policy branch (``policy == "ipxe-exit"``, ``policy in
    ("bty-flash-always", "bty-flash-once")``, ...). A policy added to
    the tuple (or renamed) without a matching branch silently falls
    through to the default, serving the wrong thing on real hardware.

    This is the exact bug shape the bty-* rename risked: the policy
    set is one source of truth, the decision tree another, and they
    can drift. Pin them together. The dropdown UI is already
    drift-proof (it loops over ``BOOT_MODES`` directly), so the
    decision tree is the surface that needs a guard.
    """
    from bty.web._models import BOOT_MODES

    src = (REPO_ROOT / "src" / "bty" / "web" / "_app.py").read_text()
    missing = [p for p in BOOT_MODES if f'"{p}"' not in src]
    assert not missing, (
        f"boot policies in BOOT_MODES with no literal branch in _app.py: "
        f"{missing!r}. The PXE decision tree handles each policy explicitly; "
        f"a policy with no branch falls through to the default and serves the "
        f"wrong iPXE config. Add (or fix) the branch in the GET /pxe/{{mac}} "
        f"handler."
    )


def test_bty_web_env_vars_are_covered_by_config_schema() -> None:
    """v0.42+: every ``BTY_*`` env var that bty-web reads from
    ``os.environ`` must either:

    * follow the ``BTY_<SECTION>_<KEY>`` convention and map to a
      field in :mod:`bty.web._config` (the Config dataclass IS the
      schema; the env layer is just a per-key override on top of
      the TOML), OR
    * appear in a small allow-list of legacy / not-yet-migrated
      names below.

    The allow-list shrinks toward zero as the v0.42 migration
    progresses; removing an entry here is the canonical way to
    enforce "this knob now lives in bty.toml".
    """
    from dataclasses import fields
    from typing import get_type_hints

    from bty.web._config import Config

    web = REPO_ROOT / "src" / "bty" / "web"
    env_keys: set[str] = set()
    for name in ("__init__.py", "_app.py", "_ui.py", "_db.py", "_auth.py"):
        src = (web / name).read_text()
        env_keys.update(re.findall(r'os\.environ\.get\(\s*"(BTY_[A-Z0-9_]+)"', src))
        env_keys.update(re.findall(r'os\.environ\[\s*"(BTY_[A-Z0-9_]+)"', src))
    assert env_keys, "scan should find at least one BTY_* lookup"

    # Names that follow the convention map to a Config field.
    schema_keys: set[str] = {"BTY_CONFIG_FILE", "BTY_CONFIG_DIR"}
    for section_name, section_cls in get_type_hints(Config).items():
        for fld in fields(section_cls):
            schema_keys.add(f"BTY_{section_name.upper()}_{fld.name.upper()}")

    # Legacy names still read directly by call sites pending
    # migration to ``cfg.*``. Each entry should map to a Config
    # field via the section/key convention; removing it from this
    # list is how the migration is enforced.
    legacy_pending_migration: set[str] = {
        # callers still on direct env reads (legacy v0.41 names):
        "BTY_STATE_DIR",
        "BTY_BOOT_DIR",
        "BTY_BACKUP_DIR",
        "BTY_CATALOG_FILE",
        "BTY_SESSION_SECRET",
        "BTY_TRUSTED_PROXY",
        "BTY_WEB_HOST",
        "BTY_WEB_PORT",
        "BTY_WITHCACHE_URL",
        "BTY_TFTP_PROBE_HOST",
        "BTY_MAX_UPLOAD_BYTES",
        # _app.py only consults this on startup to seed boot artifacts
        # into BTY_BOOT_DIR if the container shipped baked ones; not
        # an operator-facing config knob.
        "BTY_BOOT_SEED_DIR",
        # _settings_store / _releases use these via a module-level
        # alias rather than a direct os.environ read; the scan won't
        # actually see them but list them so the test text documents
        # the surface.
    }

    allowed = schema_keys | legacy_pending_migration
    missing = sorted(k for k in env_keys if k not in allowed)
    assert not missing, (
        f"bty-web reads env vars that aren't in the Config schema and "
        f"aren't on the legacy allow-list: {missing}. Either add them "
        f"as fields under the matching section in src/bty/web/_config.py "
        f"OR migrate the call sites to cfg.* and add the name to the "
        f"legacy allow-list in this test (then drop the allow-list "
        f"entry once migrated)."
    )


def test_tftp_sidecar_serves_its_baked_nbp_dir() -> None:
    """The bty-tftp sidecar bakes the iPXE NBPs into one directory and
    serves that same directory over TFTP. If the ``cp`` target dir and
    the ``in.tftpd`` serve dir drift, clients 404 on the bootfile. Pin
    the two to each other (bty-web is HTTP-only now, so this self-check
    on the sidecar replaces the old container-vs-appliance dnsmasq check).
    """
    containerfile = (REPO_ROOT / "deploy" / "tftp" / "Containerfile").read_text()

    # The ENTRYPOINT's last arg is the dir in.tftpd serves.
    serve = re.search(r'ENTRYPOINT\s+\[.*"([^"]+)"\s*\]', containerfile)
    assert serve, "deploy/tftp/Containerfile must have an in.tftpd ENTRYPOINT"
    serve_dir = serve.group(1)
    assert serve_dir == "/opt/ipxe", (
        f"tftp sidecar serves {serve_dir!r}; the NBPs are staged into /opt/ipxe"
    )
    # The stock NBPs are copied into that same dir.
    assert f"{serve_dir}/" in containerfile or f" {serve_dir}\n" in containerfile, (
        f"Containerfile should copy the iPXE NBPs into {serve_dir} (the served dir)"
    )


def test_docker_bty_uid_aligned_across_surfaces() -> None:
    """The Dockerfile pins the in-container bty user to a fixed UID and
    the Makefile ``docker-run`` target chowns the host-side bind-mount to
    the same UID. If they drift, the bind-mount isn't writable by the bty
    user and bty-web can't write state.db -- the operator sees nothing on
    http://localhost:8080/ui.

    Pin the alignment so a future Dockerfile UID bump also bumps the
    Makefile chown.
    """
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text()
    makefile = (REPO_ROOT / "Makefile").read_text()

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
        f"bind-mount won't be writable by the bty user."
    )


def test_docker_run_publishes_http_only() -> None:
    """The bty-web container is HTTP-only -- TFTP moved to the separate
    bty-tftp sidecar. ``make docker-run`` must publish just 8080:8080 and
    must NOT publish udp/69 again (a regression would imply the container
    is back to bundling dnsmasq, which it isn't).
    """
    makefile = (REPO_ROOT / "Makefile").read_text()
    make_ports = {
        f"{host}:{cont}/{proto or 'tcp'}"
        for host, cont, proto in re.findall(r"-p\s+(\d+):(\d+)(?:/(tcp|udp))?", makefile)
    }
    assert "8080:8080/tcp" in make_ports, (
        f"Makefile docker-run must publish 8080:8080; got {sorted(make_ports)}"
    )
    assert not any("69" in p for p in make_ports), (
        f"Makefile docker-run still publishes udp/69 {sorted(make_ports)} -- "
        f"the bty-web container is HTTP-only; TFTP is the bty-tftp sidecar."
    )


def test_docker_healthcheck_honors_configured_port() -> None:
    """The container's HEALTHCHECK must probe the *configured*
    ``BTY_WEB_PORT``, not a hardcoded port. An operator who
    overrides the port (``docker run -e BTY_WEB_PORT=9000``) would
    otherwise get a permanently-unhealthy container -- the probe
    keeps hitting the stale default while bty-web listens
    elsewhere. The probe reads ``BTY_WEB_PORT`` from the environment at
    runtime (a Python one-liner, no curl dependency); guard against a
    regression to a literal ``:8080`` in the probe URL.
    """
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text()
    healthcheck = next(
        (line for line in dockerfile.splitlines() if "healthz" in line and "http" in line),
        None,
    )
    assert healthcheck is not None, "Dockerfile HEALTHCHECK healthz probe not found"
    assert "BTY_WEB_PORT" in healthcheck, (
        f"HEALTHCHECK must read BTY_WEB_PORT at runtime, got: {healthcheck.strip()!r}. "
        f"A hardcoded port breaks the health probe for any operator who overrides it."
    )
    assert "127.0.0.1:8080/healthz" not in healthcheck, (
        "HEALTHCHECK appears to hardcode :8080 again -- read BTY_WEB_PORT instead."
    )


def test_publish_scripts_write_basename_into_sha256_sidecar() -> None:
    """Every bake/publish script that emits a ``.sha256`` sidecar via
    a shell ``sha256sum ... > ...`` redirect must ``cd`` into the
    artifact's directory first, so the recorded filename is a
    BASENAME rather than the absolute build-host path.

    Otherwise an operator's ``sha256sum -c <artifact>.sha256``
    (documented in docs/src/walkthrough-*.md) looks for a
    nonexistent ``/home/runner/.../<artifact>`` and fails. The
    netboot / usb scripts use ``cd <dir> && sha256sum <basename>``.
    Scripts that build the sidecar in-Python via
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
