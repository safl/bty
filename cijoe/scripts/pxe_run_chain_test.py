"""
Run the PXE chain test against the production server qcow2
============================================================

Boots two QEMU VMs sharing an L2 segment via ``-netdev socket``:

- Server: dual NIC. ``bty-web-init.service`` creates the state dir
  tree on first boot and writes ``/etc/default/bty-web``;
  ``bty-web.service`` starts. The mgmt NIC is QEMU user-mode with
  a host port-forward so the script can drive bty-web's HTTP API.
  The PXE NIC opens a socket listener for the client to dial in.
  Auth uses the appliance's baked-in default credential
  (``bty / bty``, set by ``cloudinit-base-server.user`` at image
  build time) - same model as PiKVM, Octoprint, etc.

- Client: single NIC joined to the server's PXE socket. PXE-boot
  enabled. Blank virtio disk attached as the flash target. After
  the chain runs, ``bty-flash-on-boot`` pulls the dummy image we
  uploaded earlier, runs ``bty flash --yes`` against /dev/vda, and
  reaches the "flash complete; rebooting" marker.

The test uses production paths for everything except DHCP setup
on the synthesised PXE segment. ``POST /ui/login`` uses the
default credential, ``PUT /boot/<name>`` and ``PUT /images/<name>``
upload the artefacts, and ``PUT /machines/<mac>`` pins the per-MAC
plan.

DHCP is the one piece that has to be test-side: the server VM and
client VM share an isolated ``-netdev socket`` segment with no
external DHCP server, but bty-web-activate-pxe deliberately only
supports proxy-DHCP (refusing to be a full-DHCP source is a
deliberate safety choice - rogue-DHCP on the wrong NIC trashes a
LAN's lease table). So the test SSHes in as ``odus`` and drops a
test-only ``/etc/dnsmasq.d/test-fulldhcp.conf`` plus restarts
dnsmasq. None of that machinery exists in production bty.

Asserts the chain progresses by tailing the client's serial-console
log for marker strings configured in
``[test.pxe.chain_markers]``.

Retargetable: False
"""

from __future__ import annotations

import errno
import json
import logging as log
import socket
import subprocess
import time
import urllib.error
import urllib.request
from argparse import ArgumentParser
from pathlib import Path

ARTIFACT_NAMES = (
    "bty-netboot-x86_64.vmlinuz",
    "bty-netboot-x86_64.initrd",
    "bty-netboot-x86_64.squashfs",
)

HEALTHZ_TIMEOUT = 300  # cloud-init + bty-web-init + bty-web takes a while
CHAIN_TIMEOUT = 600  # total for all client-side markers to appear


def add_args(parser: ArgumentParser):
    del parser


def main(args, cijoe):
    del args
    cfg = cijoe.getconf("test.pxe", {})
    if not cfg:
        log.error("missing [test.pxe] section in cijoe config")
        return errno.EINVAL

    workspace = Path.cwd() / "_build" / "test-pxe"
    server_qcow2 = workspace / "server.qcow2"
    boot_stage = workspace / "boot"
    dummy_image = workspace / "test-image.qcow2"
    for path, label in (
        (server_qcow2, "server qcow2"),
        (dummy_image, "dummy image"),
    ):
        if not path.is_file():
            log.error(f"{label} missing: {path}")
            log.error("did pxe_customize_server run?")
            return errno.ENOENT
    for name in ARTIFACT_NAMES:
        if not (boot_stage / name).is_file():
            log.error(f"live artefact missing in workspace: {boot_stage / name}")
            return errno.ENOENT

    pxe_socket_port = _free_port()
    mgmt_port = _free_port()
    ssh_port = _free_port()
    server_log = workspace / "server.serial.log"
    client_log = workspace / "client.serial.log"

    server = _start_server_vm(server_qcow2, mgmt_port, ssh_port, pxe_socket_port, server_log, cfg)
    client = None
    try:
        log.info(f"Waiting for bty-web /healthz on 127.0.0.1:{mgmt_port}")
        if not _wait_until(
            lambda: _http_ready("127.0.0.1", mgmt_port),
            HEALTHZ_TIMEOUT,
            "bty-web /healthz",
        ):
            return errno.ETIMEDOUT

        log.info("POST /ui/login (PAM, default appliance credential)")
        try:
            token = _login("127.0.0.1", mgmt_port, cfg["bty_password"])
        except Exception as exc:
            log.error(f"login failed: {exc}")
            return errno.EACCES

        log.info("PUT /boot/<live trio>")
        for name in ARTIFACT_NAMES:
            _put_file("127.0.0.1", mgmt_port, token, "/boot", boot_stage / name, name)

        log.info(f"PUT /images/{cfg['machine_image']} (1 MiB dummy)")
        _put_file(
            "127.0.0.1",
            mgmt_port,
            token,
            "/images",
            dummy_image,
            cfg["machine_image"],
        )

        # Wait for sshd to come up + drop the test-only dnsmasq
        # config that does full DHCP on the synthesised PXE
        # segment. bty-web-activate-pxe is intentionally proxy-only
        # (full DHCP would let an operator rogue-DHCP a LAN by
        # accident), so this step is genuinely test-side machinery.
        log.info(f"Waiting for sshd on 127.0.0.1:{ssh_port}")
        if not _wait_until(
            lambda: _ssh_ready("127.0.0.1", ssh_port),
            HEALTHZ_TIMEOUT,
            "sshd",
        ):
            return errno.ETIMEDOUT
        log.info("Configuring full-DHCP for the isolated PXE segment via SSH")
        _ssh_setup_test_dhcp("127.0.0.1", ssh_port, cfg)

        log.info(f"PUT /machines/{cfg['client_mac']} (boot_policy=flash)")
        _put_assignment("127.0.0.1", mgmt_port, token, cfg)

        log.info(f"Starting client VM (PXE boot, joined to socket :{pxe_socket_port})")
        client = _start_client_vm(workspace, pxe_socket_port, client_log, cfg)

        markers = _build_markers(cfg)
        seen = _wait_for_chain_markers(client_log, markers, CHAIN_TIMEOUT)

        missing = [k for k, ok in seen.items() if not ok]
        if missing:
            log.error(f"PXE chain incomplete; missing markers: {', '.join(missing)}")
            _dump_tail(client_log, 200)
            return errno.EPROTO

        log.info("PXE chain test PASSED - all markers seen on client serial console")
        return 0

    finally:
        for proc, name in ((client, "client"), (server, "server")):
            if proc is None:
                continue
            log.info(f"Terminating {name} VM (pid={proc.pid})")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


# ---------- VM lifecycle ---------------------------------------------------


def _start_server_vm(qcow2, mgmt_port, ssh_port, socket_port, log_path, cfg):
    cmd = [
        "qemu-system-x86_64",
        "-enable-kvm",
        "-cpu",
        "host",
        "-smp",
        "2",
        "-m",
        "2G",
        "-drive",
        f"file={qcow2},if=virtio",
        "-nographic",
        "-serial",
        f"file:{log_path}",
        # Mgmt NIC: user-mode with two host port-forwards: 8080 for
        # bty-web's HTTP API, 22 for the test-side SSH that drops
        # the test-only dnsmasq config (the synthesised socket-net
        # segment has no real DHCP, and bty-web-activate-pxe only
        # does proxy-DHCP).
        "-netdev",
        (
            f"user,id=mgmt,"
            f"hostfwd=tcp:127.0.0.1:{mgmt_port}-:8080,"
            f"hostfwd=tcp:127.0.0.1:{ssh_port}-:22"
        ),
        "-device",
        f"virtio-net,netdev=mgmt,addr=0x{int(cfg['mgmt_nic_slot']):x}",
        # PXE NIC: socket listener for the client to dial in.
        "-netdev",
        f"socket,id=pxe,listen=:{socket_port}",
        "-device",
        f"virtio-net,netdev=pxe,addr=0x{int(cfg['pxe_nic_slot']):x}",
    ]
    return subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _start_client_vm(workspace, socket_port, log_path, cfg):
    blank_disk = workspace / "client-blank.qcow2"
    if not blank_disk.exists():
        subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", str(blank_disk), "8G"],
            check=True,
            capture_output=True,
        )
    cmd = [
        "qemu-system-x86_64",
        "-enable-kvm",
        "-cpu",
        "host",
        "-smp",
        "1",
        "-m",
        "1G",
        "-drive",
        f"file={blank_disk},if=virtio",
        "-nographic",
        "-serial",
        f"file:{log_path}",
        "-boot",
        "n",
        "-netdev",
        f"socket,id=pxe,connect=:{socket_port}",
        "-device",
        f"virtio-net,netdev=pxe,mac={cfg['client_mac']},bootindex=1",
    ]
    return subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ---------- HTTP helpers ---------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """``/ui/login`` returns 303; we want the Set-Cookie header from
    that response, not the redirect target. Disable urllib's default
    redirect-follow."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


def _login(host, port, password):
    """Drive ``POST /ui/login`` with the appliance password and capture
    the ``bty-token`` cookie from the Set-Cookie header. Same flow a
    browser does when the operator submits the login form."""
    import http.cookies
    import urllib.parse

    body = urllib.parse.urlencode({"password": password}).encode("utf-8")
    req = urllib.request.Request(
        f"http://{host}:{port}/ui/login",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        resp = opener.open(req, timeout=10)
        status = resp.status
        set_cookie = resp.headers.get("Set-Cookie", "")
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


def _put_file(host, port, token, base_path, src_path, name):
    """``PUT /<base>/<name>`` with the file as the body. Streams to
    avoid loading large squashfs / kernel artefacts into memory."""
    url = f"http://{host}:{port}{base_path}/{name}"
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


def _put_assignment(host, port, token, cfg):
    body = json.dumps(
        {
            "image": cfg["machine_image"],
            "provisioning_mode": "none",
            "boot_policy": "flash",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"http://{host}:{port}/machines/{cfg['client_mac']}",
        data=body,
        method="PUT",
        headers={
            "Cookie": f"bty-token={token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status != 200:
            raise RuntimeError(f"PUT /machines returned {resp.status}")


# ---------- chain markers --------------------------------------------------


def _build_markers(cfg):
    """Return ``[(key, needle), ...]`` from config, with the per-MAC
    chain marker derived from the configured client MAC.

    iPXE prints fetched URLs with the MAC in ``${net0/mac:hexhyp}``
    form (hyphenated, e.g. ``52-54-00-11-22-33``), not the canonical
    colon form. Build the marker accordingly so the assertion
    matches what shows up on the serial console.
    """
    out = []
    for entry in cfg.get("chain_markers", []):
        out.append((entry["key"], entry["needle"]))
    mac_hyphen = cfg["client_mac"].replace(":", "-")
    out.append(("ipxe-fetch-permac", f"/pxe/{mac_hyphen}"))
    return out


def _wait_for_chain_markers(log_path, markers, timeout):
    seen = {key: False for key, _ in markers}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not all(seen.values()):
        if log_path.exists():
            body = log_path.read_text(errors="replace")
            for key, needle in markers:
                if not seen[key] and needle in body:
                    log.info(f"  + {key}: matched {needle!r}")
                    seen[key] = True
        if all(seen.values()):
            break
        time.sleep(2)
    return seen


# ---------- helpers --------------------------------------------------------


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_until(predicate, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(2)
    log.error(f"timed out after {timeout:.0f}s waiting for: {what}")
    return False


def _http_ready(host, port):
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=2):
            return True
    except Exception:
        return False


def _ssh_ready(host, port):
    """Quick check that sshd is accepting connections on ``port``.

    Doesn't authenticate - just opens a socket and reads the server
    banner. Used as the wait predicate before we try password auth.
    """
    try:
        with socket.create_connection((host, port), timeout=2) as sock:
            banner = sock.recv(64)
        return banner.startswith(b"SSH-")
    except OSError:
        return False


def _ssh_setup_test_dhcp(host, port, cfg):
    """SSH in as ``odus``, drop a test-only dnsmasq full-DHCP config,
    restart dnsmasq.

    bty's production helper writes a proxy-DHCP config (because we
    don't want an operator accidentally turning bty into a rogue
    DHCP source on a real LAN). The chain test's PXE segment is a
    synthesised ``-netdev socket`` with nothing else on it, so we
    need full DHCP - injected entirely from the test side.
    """
    import paramiko

    pxe_iface = f"{cfg.get('nic_prefix', 'ens')}{int(cfg['pxe_nic_slot'])}"

    # Static IP for the PXE NIC. dnsmasq's ``bind-dynamic`` only
    # binds to interfaces that have an address, so without this the
    # full-DHCP block sits idle and the client's PXE ROM gets no
    # answer. Drop a higher-priority systemd-networkd file alongside
    # the production ``10-bty-default.network`` (which DHCPs
    # everything) so just this one NIC is pinned.
    # ``ConfigureWithoutCarrier=yes`` makes networkd assign the
    # static IP even when the link has no carrier. The chain test's
    # PXE socket-net only gains carrier once the CLIENT VM connects
    # to it, which happens AFTER this SSH-setup step. Without this
    # flag, networkd waits for carrier, dnsmasq's ``bind-dynamic``
    # finds no IP to bind to on ``ens4``, and the client's PXE
    # DHCPDISCOVER goes unanswered (iPXE prints
    # ``ipxe.org/040ee119`` and gives up).
    pxe_network = (
        "[Match]\n"
        f"Name={pxe_iface}\n"
        "\n"
        "[Link]\n"
        "RequiredForOnline=no\n"
        "\n"
        "[Network]\n"
        f"Address={cfg['server_pxe_ip']}/24\n"
        "DHCP=no\n"
        "ConfigureWithoutCarrier=yes\n"
    )

    overlay = (
        "# Test-only full-DHCP overlay written by pxe_run_chain_test.\n"
        "# bty itself does not configure full DHCP - this comes from the\n"
        "# test, not from bty-web-activate-pxe.\n"
        "\n"
        "bind-dynamic\n"
        f"interface={pxe_iface}\n"
        f"dhcp-range={cfg['dhcp_range_lo']},{cfg['dhcp_range_hi']},"
        f"{cfg['pxe_netmask']},1h\n"
        "\n"
        "dhcp-match=set:bios,option:client-arch,0\n"
        "dhcp-match=set:efi,option:client-arch,7\n"
        "dhcp-match=set:efi,option:client-arch,9\n"
        "dhcp-userclass=set:ipxe,iPXE\n"
        "\n"
        "dhcp-boot=tag:!ipxe,tag:bios,undionly.kpxe\n"
        "dhcp-boot=tag:!ipxe,tag:efi,ipxe.efi\n"
        "dhcp-boot=tag:ipxe,http://${next-server}:8080/pxe-bootstrap.ipxe\n"
    )

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    # ``allow_agent=False`` and ``look_for_keys=False`` keep paramiko
    # from picking up the dev's ssh-agent / id_ed25519 by accident.
    client.connect(
        host,
        port=port,
        username="odus",
        password="odus",
        allow_agent=False,
        look_for_keys=False,
        timeout=15,
    )
    try:
        # Drop both the static-IP .network file and the dnsmasq
        # overlay, then ``networkctl reload`` (picks up the new
        # .network without restarting networkd) and restart dnsmasq
        # so it sees the now-bound interface.
        # ``networkctl reload`` picks up new .network files, but it
        # does NOT re-match existing links against the new files -
        # ``ens4`` was already bound to the catch-all
        # 10-bty-default.network at boot. ``networkctl reconfigure
        # <link>`` forces networkd to re-evaluate which .network
        # applies (now picking the higher-priority 20-bty-pxe-test
        # we just dropped) and re-apply it. Then a brief settle so
        # the address shows up before dnsmasq restarts.
        cmd = (
            f"sudo -n install -d -m 0755 /etc/dnsmasq.d /etc/systemd/network && "
            f"echo {_quote_for_shell(pxe_network)} | "
            f"sudo -n tee /etc/systemd/network/05-bty-pxe-test.network > /dev/null && "
            f"echo {_quote_for_shell(overlay)} | "
            f"sudo -n tee /etc/dnsmasq.d/test-fulldhcp.conf > /dev/null && "
            f"sudo -n networkctl reload && "
            f"sudo -n networkctl reconfigure {pxe_iface} && "
            f"for i in 1 2 3 4 5; do "
            f"  ip -4 -br addr show {pxe_iface} | grep -q 192.168.99.1/ && break; "
            f"  sleep 1; "
            f"done && "
            f"sudo -n systemctl restart dnsmasq.service"
        )
        _stdin, stdout, stderr = client.exec_command(cmd, timeout=30)
        rc = stdout.channel.recv_exit_status()
        if rc != 0:
            err = stderr.read().decode("utf-8", "replace")
            raise RuntimeError(f"dnsmasq overlay failed (rc={rc}): {err}")
    finally:
        client.close()


def _quote_for_shell(text):
    """Single-quote ``text`` for safe interpolation into a shell string."""
    return "'" + text.replace("'", "'\\''") + "'"


def _dump_tail(path, lines):
    if not path.is_file():
        log.error(f"{path}: file does not exist")
        return
    body = path.read_text(errors="replace")
    log.error(f"--- last {lines} lines of {path} ---")
    for line in body.splitlines()[-lines:]:
        log.error(line)
