"""
PXE chain test: containerized bty-web + a bridged QEMU client
=============================================================

Brings bty-web up as a container and PXE-boots a QEMU client VM against it over
a host bridge:

- Server: the bty-web container (built by ``pxe_prepare`` from this checkout)
  publishes its HTTP API on the host with ``BTY_ADMIN_PASSWORD`` set, so the
  test drives the production HTTP API directly (``POST /ui/login``,
  ``PUT /boot/<name>``, ``PUT /images/<name>``, ``PUT /machines/<mac>``). It is
  reachable from the client over a host bridge that carries the server-side IP.

- DHCP + TFTP: a test-side dnsmasq bound to the bridge hands the client an
  address and the iPXE NBP, then chainloads bty-web's HTTP iPXE script. bty
  serves no DHCP; this is test-side machinery for the synthetic segment.

- Client: a QEMU VM with a tap on the bridge and a blank virtio disk. After the
  chain runs, ``bty`` in auto-flash mode pulls the dummy image, writes it to
  /dev/vda, and reaches ``bty: flash complete; rebooting`` on /dev/console.

Asserts the chain progresses by tailing the client serial console for the
markers in ``[test.pxe.chain_markers]``. Needs root (bridge/tap/dnsmasq) and KVM.

Retargetable: False
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging as log
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from argparse import ArgumentParser
from pathlib import Path

ARTIFACT_NAME_FMTS = (
    "bty-netboot-pc-x86_64-v{version}.vmlinuz",
    "bty-netboot-pc-x86_64-v{version}.initrd",
    "bty-netboot-pc-x86_64-v{version}.squashfs",
)

# bty-web publishes :8080; the test reaches it on the host (seeding via
# loopback, the client via the bridge IP). withcache publishes :8081
# alongside and is the single source of truth for the catalog since
# v0.66.0 (bty-web reads GET /catalog + POST /catalog/entries against
# it). We run withcache on the host network so bty-web can reach it via
# ``http://127.0.0.1:8081``; the container listens on all interfaces.
BTY_HTTP_PORT = 8080
WITHCACHE_HTTP_PORT = 8081
CONTAINER_NAME = "bty-pxe-test"
WITHCACHE_CONTAINER_NAME = "withcache-pxe-test"
WITHCACHE_IMAGE = "ghcr.io/safl/withcache:latest"
WITHCACHE_PASSWORD = "pxe-test-withcache"

HEALTHZ_TIMEOUT = 180  # container start is far quicker than a VM boot
CHAIN_TIMEOUT = 600  # total for all client-side markers to appear


def _read_bty_version() -> str:
    pyproject = Path.cwd().parent / "pyproject.toml"
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("version") and "=" in stripped:
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"could not find version line in {pyproject}")


def _artifact_names() -> tuple[str, ...]:
    version = _read_bty_version()
    return tuple(fmt.format(version=version) for fmt in ARTIFACT_NAME_FMTS)


def add_args(parser: ArgumentParser):
    del parser


def main(args, cijoe):
    del args
    cfg = cijoe.getconf("test.pxe", {})
    if not cfg:
        log.error("missing [test.pxe] section in cijoe config")
        return errno.EINVAL

    workspace = Path.cwd() / "_build" / "test-pxe"
    boot_stage = workspace / "boot"
    dummy_image = workspace / "test-image.qcow2"
    tftproot = workspace / "tftproot"
    if not dummy_image.is_file():
        log.error(f"dummy image missing: {dummy_image} (did pxe_prepare run?)")
        return errno.ENOENT
    artifact_names = _artifact_names()
    for name in artifact_names:
        if not (boot_stage / name).is_file():
            log.error(f"live artifact missing in workspace: {boot_stage / name}")
            return errno.ENOENT

    image = cfg.get("bty_image", "bty-web:pxetest")
    withcache_image = cfg.get("withcache_image", WITHCACHE_IMAGE)
    seed_base = f"http://127.0.0.1:{BTY_HTTP_PORT}"
    # The withcache URL bty embeds in the flash plan is fetched by
    # the CLIENT VM, not by bty itself, so it must be reachable from
    # the bridge network. Withcache on --network=host binds to
    # 0.0.0.0:8081 so the bridge IP works for both bty (which reaches
    # it via loopback semantics) and the client VM (bridge).
    withcache_base = f"http://{cfg['server_pxe_ip']}:{WITHCACHE_HTTP_PORT}"
    client_log = workspace / "client.serial.log"

    container = None
    withcache = None
    dnsmasq = None
    client = None
    net_up = False
    try:
        _setup_network(cfg, tftproot)
        net_up = True
        dnsmasq = _start_dnsmasq(cfg, tftproot, workspace)

        # withcache first so bty-web's startup catalog sync can hit
        # a running server. Both containers listen on the host (bty on
        # 8080, withcache on 8081); bty-web reaches withcache via
        # loopback inside the shared host netns.
        withcache = _run_withcache(withcache_image)
        log.info(f"Waiting for withcache /healthz on {withcache_base}")
        if not _wait_until(
            lambda: _http_ready(withcache_base), HEALTHZ_TIMEOUT, "withcache /healthz"
        ):
            log.error("withcache container did not become healthy")
            return errno.ETIMEDOUT

        container = _run_container(image, cfg["bty_password"], withcache_url=withcache_base)

        log.info(f"Waiting for bty-web /healthz on {seed_base}")
        if not _wait_until(lambda: _http_ready(seed_base), HEALTHZ_TIMEOUT, "bty-web /healthz"):
            log.error("bty-web container did not become healthy; logs:")
            _dump_container_logs()
            return errno.ETIMEDOUT

        log.info("POST /ui/login (BTY_ADMIN_PASSWORD set on the container)")
        try:
            token = _login(seed_base, cfg["bty_password"])
        except Exception as exc:
            log.error(f"login failed: {exc}")
            return errno.EACCES

        log.info("PUT /boot/<live trio>")
        for name in artifact_names:
            _put_file(seed_base, token, "/boot", boot_stage / name, name)

        # Stage the dummy image under /boot (arbitrary uploads via
        # PUT /boot/<name> still work) so the catalog entry's src is
        # a real reachable URL. bty-web serves the bytes via /boot,
        # withcache Downloads it into its own cache, then bty binds
        # the machine to the catalog entry via its bty_image_ref.
        log.info(f"PUT /boot/{cfg['machine_image']} (1 MiB dummy) + sha256 sidecar")
        _put_file(seed_base, token, "/boot", dummy_image, cfg["machine_image"])
        dummy_sha = _sha256_file(dummy_image)
        sidecar = f"{cfg['machine_image']}.sha256"
        sidecar_body = f"{dummy_sha}  {cfg['machine_image']}\n".encode()
        _put_bytes(seed_base, token, "/boot", sidecar_body, sidecar)

        # Catalog entry URL must be reachable from the client VM, not
        # from loopback. Use the bridge IP the live env will see.
        catalog_url_base = f"http://{cfg['server_pxe_ip']}:{BTY_HTTP_PORT}"
        image_url = f"{catalog_url_base}/boot/{cfg['machine_image']}"
        log.info(f"withcache POST /catalog/entries (src={image_url})")
        bty_image_ref = _add_and_download_catalog_entry(withcache_base, image_url)

        log.info("PUT /ui/settings/upstream to trigger withcache_catalog.refresh() on bty")
        # bty-web polls withcache at startup, but the container has
        # already started. Force a refresh so the just-added entry
        # is visible before the machine bind.
        _refresh_bty_catalog(seed_base, token)

        log.info(f"PUT /machines/{cfg['client_mac']} (boot_mode=bty-flash-always)")
        _put_assignment(seed_base, token, cfg, bty_image_ref)

        firmware = str(cfg.get("client_firmware", "bios")).lower()
        if firmware == "uefi" and _find_ovmf() is None:
            log.warning("client_firmware=uefi but no OVMF found; falling back to BIOS")
            firmware = "bios"
        log.info(f"Starting client VM (firmware={firmware}, PXE boot on {cfg['tap_iface']})")
        client = _start_client_vm(workspace, cfg, client_log, firmware)

        markers = _build_markers(cfg)
        seen = _wait_for_chain_markers(client_log, markers, CHAIN_TIMEOUT)
        missing = [k for k, ok in seen.items() if not ok]
        if missing:
            log.error(f"PXE chain incomplete; missing markers: {', '.join(missing)}")
            _dump_tail(client_log, 200)
            _dump_container_logs()
            return errno.EPROTO

        log.info("PXE chain test PASSED - all markers seen on client serial console")
        return 0
    finally:
        if client is not None:
            _terminate(client, "client VM")
        _stop_container(container)
        _stop_container(withcache, name=WITHCACHE_CONTAINER_NAME)
        if dnsmasq is not None:
            _terminate(dnsmasq, "dnsmasq", sudo=True)
        if net_up:
            _teardown_network(cfg)


# ---------- network: host bridge + tap + dnsmasq ---------------------------


def _setup_network(cfg, tftproot):
    """Create the bridge carrying the server-side IP and a client tap on it.
    The tap is owned by the current user so the (non-root) QEMU client can open
    it. Seed the TFTP root with the iPXE NBPs from the distro ``ipxe`` package."""
    bridge = cfg["bridge"]
    tap = cfg["tap_iface"]
    ip = cfg["server_pxe_ip"]
    user = _whoami()

    _teardown_network(cfg)  # idempotent: clear any leftovers from a prior run
    _sudo(["ip", "link", "add", bridge, "type", "bridge"])
    _sudo(["ip", "addr", "add", f"{ip}/24", "dev", bridge])
    _sudo(["ip", "link", "set", bridge, "up"])
    _sudo(["ip", "tuntap", "add", "dev", tap, "mode", "tap", "user", user])
    _sudo(["ip", "link", "set", tap, "master", bridge])
    _sudo(["ip", "link", "set", tap, "up"])

    # On a runner with docker installed, ``br_netfilter`` is loaded and the
    # FORWARD policy is DROP, so frames crossing a Linux bridge get passed to
    # iptables and the client's DHCP broadcast can be dropped. Make this
    # synthetic test bridge bypass iptables entirely. Best-effort: the sysctl
    # only exists when br_netfilter is loaded (and if it isn't, there's nothing
    # filtering the bridge anyway).
    _sudo(["sysctl", "-w", "net.bridge.bridge-nf-call-iptables=0"], check=False)
    _sudo(["sysctl", "-w", "net.bridge.bridge-nf-call-ip6tables=0"], check=False)
    _sudo(["sysctl", "-w", "net.bridge.bridge-nf-call-arptables=0"], check=False)

    tftproot.mkdir(parents=True, exist_ok=True)
    for nbp in ("undionly.kpxe", "ipxe.efi"):
        src = Path("/usr/lib/ipxe") / nbp
        if src.is_file():
            shutil.copy2(src, tftproot / nbp)
        else:
            log.warning(f"iPXE NBP not found: {src} (install the 'ipxe' package)")


def _teardown_network(cfg):
    bridge = cfg["bridge"]
    tap = cfg["tap_iface"]
    # Best-effort; ignore failures (interfaces may not exist).
    _sudo(["ip", "link", "set", tap, "down"], check=False)
    _sudo(["ip", "link", "del", tap], check=False)
    _sudo(["ip", "link", "set", bridge, "down"], check=False)
    _sudo(["ip", "link", "del", bridge], check=False)


def _start_dnsmasq(cfg, tftproot, workspace):
    """Test-side dnsmasq on the bridge: full DHCP + TFTP, chainloading bty-web's
    HTTP iPXE script. bty serves no DHCP; this is the synthetic segment's only
    DHCP source."""
    conf = workspace / "dnsmasq.conf"
    server_ip = cfg["server_pxe_ip"]
    # dnsmasq is launched as root (via sudo) but drops privileges after binding
    # its sockets. Its default drop target is ``nobody``/``dnsmasq``, which on a
    # CI runner cannot traverse the 0750 ``/home/<user>`` to reach the workspace
    # tftp-root -> "TFTP directory inaccessible: Permission denied -> FAILED to
    # start up", i.e. no DHCP at all. Pin the drop target to the user who owns
    # the workspace (and the tap) so the tftp-root stays readable.
    user = _whoami()
    conf.write_text(
        "# Test-only DHCP+TFTP for the synthetic PXE bridge (test machinery,\n"
        "# not part of bty: production relies on the operator's LAN DHCP).\n"
        "port=0\n"  # DHCP + TFTP only; no DNS service (nothing for it to bind 53)
        f"user={user}\n"  # don't drop to 'nobody'; keep the runner-owned tftp-root readable
        "log-dhcp\n"  # log DHCP transactions so a future failure leaves a trail
        f"interface={cfg['bridge']}\n"
        "bind-interfaces\n"
        "except-interface=lo\n"
        f"dhcp-range={cfg['dhcp_range_lo']},{cfg['dhcp_range_hi']},{cfg['pxe_netmask']},1h\n"
        "enable-tftp\n"
        f"tftp-root={tftproot}\n"
        "dhcp-match=set:bios,option:client-arch,0\n"
        "dhcp-match=set:efi,option:client-arch,7\n"
        "dhcp-match=set:efi,option:client-arch,9\n"
        "dhcp-userclass=set:ipxe,iPXE\n"
        "dhcp-boot=tag:!ipxe,tag:bios,undionly.kpxe\n"
        "dhcp-boot=tag:!ipxe,tag:efi,ipxe.efi\n"
        f"dhcp-boot=tag:ipxe,http://{server_ip}:{BTY_HTTP_PORT}/pxe-bootstrap.ipxe\n",
        encoding="utf-8",
    )
    log_path = workspace / "dnsmasq.log"
    proc = subprocess.Popen(
        [
            "sudo",
            "-n",
            "dnsmasq",
            "--keep-in-foreground",
            "--log-facility=-",
            f"--conf-file={conf}",
        ],
        stdin=subprocess.DEVNULL,
        stdout=open(log_path, "wb"),  # noqa: SIM115 - lives for the dnsmasq process
        stderr=subprocess.STDOUT,
    )
    return proc


def _whoami():
    import getpass

    return getpass.getuser()


# ---------- bty-web container ----------------------------------------------


def _run_container(image, admin_password, *, withcache_url):
    """Run the bty-web container detached, publishing :8080, with the admin
    password set so the test exercises the gated login path.

    ``withcache_url`` is written as ``BTY_WITHCACHE_URL`` so the
    withcache_catalog client points at the sibling withcache
    container (running via --network=host on the same loopback).
    Host networking keeps the seed URL identical between the host
    and the container.
    """
    subprocess.run(
        ["podman", "rm", "-f", CONTAINER_NAME],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    subprocess.run(
        [
            "podman",
            "run",
            "-d",
            "--name",
            CONTAINER_NAME,
            "--network=host",
            "-e",
            f"BTY_ADMIN_PASSWORD={admin_password}",
            "-e",
            f"BTY_WITHCACHE_URL={withcache_url}",
            "-e",
            f"BTY_WITHCACHE_PASSWORD={WITHCACHE_PASSWORD}",
            image,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return CONTAINER_NAME


def _run_withcache(image):
    """Run the withcache container detached on --network=host so
    both bty-web (loopback via env var) and the test host (loopback
    via seed URL) can reach it on :8081 without port-forwarding
    conflicts.

    WITHCACHE_ADMIN_PASSWORD gates the catalog write endpoints; the
    test posts via ``Authorization: Bearer <pw>`` since it has no
    session cookie. Since v0.10.0 there is no auto-fetch: the test
    hits POST /catalog/entries/{name}/download explicitly to fill
    the cache before the flash chain reads from /b/<url>.
    """
    subprocess.run(
        ["podman", "rm", "-f", WITHCACHE_CONTAINER_NAME],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    subprocess.run(
        [
            "podman",
            "run",
            "-d",
            "--name",
            WITHCACHE_CONTAINER_NAME,
            "--network=host",
            "-e",
            f"WITHCACHE_ADMIN_PASSWORD={WITHCACHE_PASSWORD}",
            image,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return WITHCACHE_CONTAINER_NAME


def _stop_container(handle, *, name=None):
    """Kill + remove the container identified by ``handle``. When
    ``handle`` is None but a name is given, still tries to remove
    the named container (idempotent cleanup)."""
    target = handle or name
    if target is None:
        return
    subprocess.run(
        ["podman", "rm", "-f", target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _dump_container_logs():
    log.error(f"--- podman logs {CONTAINER_NAME} ---")
    res = subprocess.run(
        ["podman", "logs", "--tail", "200", CONTAINER_NAME],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in (res.stdout + res.stderr).splitlines():
        log.error(line)


# ---------- client VM ------------------------------------------------------


_OVMF_PAIRS = (
    ("/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd"),
    ("/usr/share/OVMF/OVMF_CODE.fd", "/usr/share/OVMF/OVMF_VARS.fd"),
    ("/usr/share/ovmf/OVMF_CODE.fd", "/usr/share/ovmf/OVMF_VARS.fd"),
)


def _find_ovmf():
    for code, vars_tpl in _OVMF_PAIRS:
        if Path(code).is_file() and Path(vars_tpl).is_file():
            return code, vars_tpl
    return None


def _start_client_vm(workspace, cfg, log_path, firmware="bios"):
    blank_disk = workspace / "client-blank.qcow2"
    if not blank_disk.exists():
        subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", str(blank_disk), "8G"],
            check=True,
            capture_output=True,
        )
    fw_args: list[str] = []
    if firmware == "uefi":
        ovmf = _find_ovmf()
        if ovmf is None:
            raise RuntimeError("client_firmware=uefi but no OVMF firmware found")
        code, vars_tpl = ovmf
        vars_copy = workspace / "client-ovmf-vars.fd"
        shutil.copy(vars_tpl, vars_copy)
        fw_args = [
            "-drive",
            f"if=pflash,format=raw,unit=0,readonly=on,file={code}",
            "-drive",
            f"if=pflash,format=raw,unit=1,file={vars_copy}",
        ]
    cmd = [
        "qemu-system-x86_64",
        "-enable-kvm",
        "-cpu",
        "host",
        *fw_args,
        "-smp",
        "1",
        # live-boot streams the ~650 MiB squashfs into tmpfs before pivot, so the
        # client needs headroom over the squashfs size.
        "-m",
        "2G",
        # Stable serial so bty-web's target_disk_serial safety gate matches.
        "-drive",
        f"file={blank_disk},if=none,id=flashdrive,format=qcow2",
        "-device",
        "virtio-blk-pci,drive=flashdrive,serial=BTYTEST",
        "-nographic",
        # Two ``-serial`` directives so QEMU emulates BOTH UARTs with a
        # well-defined backend: COM1 (ttyS0) writes to the log we tail
        # for chain markers, COM2 (ttyS1) drops bytes onto a null
        # backend. Without the second ``-serial null``, QEMU still
        # emulates the COM2 register set (the i440FX SuperIO has two
        # UARTs unconditionally) but with no chardev attached, and the
        # kernel-side 8250 driver's view of that port becomes
        # configuration-dependent. The PXE templates list both
        # ``console=ttyS0`` and ``console=ttyS1`` to cover Dell / iLO
        # (COM1) and Supermicro (COM2) SoL bridges in the field; the
        # explicit null backend here makes the in-CI behavior
        # deterministic regardless of which order they're listed.
        "-serial",
        f"file:{log_path}",
        "-serial",
        "null",
        "-boot",
        "n",
        # PXE NIC on the host bridge via a pre-created, user-owned tap.
        "-netdev",
        f"tap,id=pxe,ifname={cfg['tap_iface']},script=no,downscript=no",
        "-device",
        f"virtio-net,netdev=pxe,mac={cfg['client_mac']},bootindex=1",
    ]
    return subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


# ---------- HTTP seeding (production API) -----------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl


def _login(base, password):
    import http.cookies
    import urllib.parse

    body = urllib.parse.urlencode({"password": password}).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/ui/login",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        resp = opener.open(req, timeout=10)
        status, set_cookie = resp.status, resp.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as exc:
        status = exc.code
        set_cookie = exc.headers.get("Set-Cookie", "") if exc.headers else ""
    if status not in (200, 303):
        raise RuntimeError(f"/ui/login returned {status}")
    cookie = http.cookies.SimpleCookie()
    cookie.load(set_cookie)
    if "bty-token" not in cookie:
        raise RuntimeError(f"/ui/login did not return a bty-token cookie: {set_cookie!r}")
    return cookie["bty-token"].value


def _put_file(base, token, base_path, src_path, name):
    url = f"{base}{base_path}/{name}"
    size = src_path.stat().st_size
    with src_path.open("rb") as fh:
        req = urllib.request.Request(
            url,
            data=fh,
            method="PUT",
            headers={
                "Cookie": f"bty-token={token}",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size),
            },
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            if resp.status != 200:
                raise RuntimeError(f"PUT {url} returned {resp.status}")


def _put_bytes(base, token, base_path, body, name):
    url = f"{base}{base_path}/{name}"
    req = urllib.request.Request(
        url,
        data=body,
        method="PUT",
        headers={
            "Cookie": f"bty-token={token}",
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(body)),
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status not in (200, 201):
            raise RuntimeError(f"PUT {url} returned {resp.status}")


def _add_and_download_catalog_entry(withcache_base, image_url):
    """POST the entry into withcache's catalog, then trigger an
    explicit Download so the /b/<url> flash path finds a cache hit.

    Since withcache v0.10.0 there is no auto-fetch on cache miss;
    the operator (or this test) hits POST /catalog/entries/{name}/download
    to fill the cache. The withcache-side worker fetches
    ``image_url`` (served by bty-web's /boot) and stores it under
    the canonical cache key.

    Returns the ``bty_image_ref`` for the entry, computed locally
    as ``sha256(image_url)`` -- the same value bty derives on its
    side when refreshing the catalog.
    """
    name = "pxe-test-image"
    body = json.dumps(
        {
            "name": name,
            "src": image_url,
            "resolved_src": image_url,
            "format": "img",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{withcache_base}/catalog/entries",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {WITHCACHE_PASSWORD}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status not in (200, 201):
            raise RuntimeError(f"withcache POST /catalog/entries returned {resp.status}")

    log.info(f"withcache POST /catalog/entries/{name}/download (async fetch)")
    dl_req = urllib.request.Request(
        f"{withcache_base}/catalog/entries/{name}/download",
        method="POST",
        headers={"Authorization": f"Bearer {WITHCACHE_PASSWORD}"},
    )
    with urllib.request.urlopen(dl_req, timeout=30) as resp:
        if resp.status not in (200, 201, 202):
            raise RuntimeError(f"withcache POST download returned {resp.status}")

    # Poll GET /catalog until this entry's ``downloaded_at`` is set,
    # so bty-web's downloaded-first bind gate accepts the ref.
    if not _wait_until(
        lambda: _catalog_entry_downloaded(withcache_base, name),
        HEALTHZ_TIMEOUT,
        f"withcache entry {name!r} downloaded",
    ):
        raise RuntimeError(
            f"withcache never marked entry {name!r} as downloaded; "
            "check the container logs for a fetch error"
        )

    return hashlib.sha256(image_url.encode("utf-8")).hexdigest()


def _catalog_entry_downloaded(withcache_base, name):
    """True iff GET /catalog reports the entry has ``downloaded_at``
    set. Used as a poll condition for the async Download."""
    try:
        with urllib.request.urlopen(f"{withcache_base}/catalog", timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return False
    for entry in payload.get("entries") or []:
        if entry.get("name") == name and entry.get("downloaded_at"):
            return True
    return False


def _refresh_bty_catalog(base, token):
    """POST /admin/withcache/refresh so bty picks up the catalog
    entry withcache just registered. bty polls withcache once at
    process start; a mid-run addition is invisible until refresh
    fires (this endpoint or a restart)."""
    req = urllib.request.Request(
        f"{base}/admin/withcache/refresh",
        data=b"",
        method="POST",
        headers={"Cookie": f"bty-token={token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status != 204:
            raise RuntimeError(f"POST /admin/withcache/refresh returned {resp.status}")


def _put_assignment(base, token, cfg, bty_image_ref):
    body = json.dumps(
        {
            "bty_image_ref": bty_image_ref,
            "boot_mode": "bty-flash-always",
            "target_disk_serial": "BTYTEST",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/machines/{cfg['client_mac']}",
        data=body,
        method="PUT",
        headers={"Cookie": f"bty-token={token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status != 200:
            raise RuntimeError(f"PUT /machines returned {resp.status}")


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- markers + small utils ------------------------------------------


def _build_markers(cfg):
    out = [(entry["key"], entry["needle"]) for entry in cfg.get("chain_markers", [])]
    mac_hyphen = cfg["client_mac"].replace(":", "-")
    out.append(("ipxe-fetch-permac", f"/pxe/{mac_hyphen}"))
    return out


def _wait_for_chain_markers(log_path, markers, timeout):
    seen = {key: False for key, _ in markers}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not all(seen.values()):
        if log_path.exists():
            body = log_path.read_text(encoding="utf-8", errors="replace")
            for key, needle in markers:
                if not seen[key] and needle in body:
                    log.info(f"  + {key}: matched {needle!r}")
                    seen[key] = True
        if all(seen.values()):
            break
        time.sleep(2)
    return seen


def _wait_until(predicate, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(2)
    log.error(f"timed out after {timeout:.0f}s waiting for: {what}")
    return False


def _http_ready(base):
    try:
        with urllib.request.urlopen(f"{base}/healthz", timeout=2):
            return True
    except Exception:
        return False


def _dump_tail(path, lines):
    if not path.is_file():
        log.error(f"{path}: file does not exist")
        return
    body = path.read_text(encoding="utf-8", errors="replace")
    log.error(f"--- last {lines} lines of {path} ---")
    for line in body.splitlines()[-lines:]:
        log.error(line)


def _sudo(cmd, check=True):
    return subprocess.run(["sudo", "-n", *cmd], check=check, capture_output=True, text=True)


def _terminate(proc, what, sudo=False):
    log.info(f"Terminating {what} (pid={proc.pid})")
    if sudo:
        # dnsmasq runs under sudo, so the child is root; signal via sudo kill.
        subprocess.run(["sudo", "-n", "kill", str(proc.pid)], check=False)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
