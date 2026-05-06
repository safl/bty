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

The test uses ONLY production paths to stage the appliance for the
chain - no virt-customize, no DB seeding, no /etc/default baking.
``POST /auth/login`` uses the default credential, then ``PUT
/boot/<name>`` and ``PUT /images/<name>`` upload the artefacts,
``POST /ui/settings/pxe-activate`` (full-DHCP mode) brings up
dnsmasq, and ``PUT /machines/<mac>`` pins the per-MAC plan.
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
import urllib.request
from argparse import ArgumentParser
from pathlib import Path

ARTIFACT_NAMES = (
    "bty-live-x86_64.vmlinuz",
    "bty-live-x86_64.initrd",
    "bty-live-x86_64.squashfs",
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
    server_log = workspace / "server.serial.log"
    client_log = workspace / "client.serial.log"

    server = _start_server_vm(server_qcow2, mgmt_port, pxe_socket_port, server_log, cfg)
    client = None
    try:
        log.info(f"Waiting for bty-web /healthz on 127.0.0.1:{mgmt_port}")
        if not _wait_until(
            lambda: _http_ready("127.0.0.1", mgmt_port),
            HEALTHZ_TIMEOUT,
            "bty-web /healthz",
        ):
            return errno.ETIMEDOUT

        log.info("POST /auth/login (PAM, default appliance credential)")
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

        log.info("POST /ui/settings/pxe-activate (full mode)")
        _post_form(
            "127.0.0.1",
            mgmt_port,
            token,
            "/ui/settings/pxe-activate",
            {
                "interface": f"{cfg.get('nic_prefix', 'ens')}{int(cfg['pxe_nic_slot'])}",
                "subnet": cfg["server_pxe_ip"].rsplit(".", 1)[0] + ".0",
                "mode": "full",
                "range_lo": cfg["dhcp_range_lo"],
                "range_hi": cfg["dhcp_range_hi"],
                "netmask": cfg["pxe_netmask"],
            },
        )

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


def _start_server_vm(qcow2, mgmt_port, socket_port, log_path, cfg):
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
        # Mgmt NIC: user-mode with host port-forward for HTTP-API access.
        "-netdev",
        f"user,id=mgmt,hostfwd=tcp:127.0.0.1:{mgmt_port}-:8080",
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


def _login(host, port, password):
    body = json.dumps({"password": password, "label": "pxe-chain-test"}).encode("utf-8")
    req = urllib.request.Request(
        f"http://{host}:{port}/auth/login",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload["token"]


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
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size),
            },
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            if resp.status != 200:
                raise RuntimeError(f"PUT {url} returned {resp.status}")


def _post_form(host, port, token, path, data):
    body = urllib_urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        f"http://{host}:{port}{path}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status not in (200, 303):
            raise RuntimeError(f"POST {path} returned {resp.status}")


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
            "Authorization": f"Bearer {token}",
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


def _dump_tail(path, lines):
    if not path.is_file():
        log.error(f"{path}: file does not exist")
        return
    body = path.read_text(errors="replace")
    log.error(f"--- last {lines} lines of {path} ---")
    for line in body.splitlines()[-lines:]:
        log.error(line)


def urllib_urlencode(data):
    """Local re-export so the imports stay tidy at the top of the file."""
    from urllib.parse import urlencode

    return urlencode(data)
