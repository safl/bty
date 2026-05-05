"""
Run the PXE chain test against the customised server qcow2
============================================================

Boots two QEMU VMs sharing an L2 segment via ``-netdev socket``:

- Server: dual NIC. The mgmt NIC is QEMU user-mode with a host
  port-forward so the script can hit ``/healthz`` and PUT the
  machine assignment via plain HTTP. The PXE NIC opens a socket
  listener for the client to dial in.
- Client: single NIC, joined to the server's PXE socket. PXE-boot
  enabled. Blank virtio disk attached as the flash target. The
  customise step bakes a 1 MiB dummy qcow2 into
  ``/var/lib/bty/images/`` so the live env's bty-flash-on-boot
  script can pull it, run ``bty flash --yes`` against /dev/vda,
  and reach the "flash complete; rebooting" marker.

Asserts the chain progresses by tailing the client's serial-console
log for marker strings configured in ``[test.pxe.chain_markers]``.
On success: returns 0 and leaves the serial logs in the run dir for
the cijoe report. On failure: returns non-zero and prints which
markers were missed plus the last 200 lines of the client log.

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

HEALTHZ_TIMEOUT = 180  # seconds for bty-web to come up
CHAIN_TIMEOUT = 600  # seconds total for all markers to appear


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
    if not server_qcow2.is_file():
        log.error(f"customised server qcow2 missing: {server_qcow2}")
        log.error("did pxe_customize_server run?")
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

        log.info(f"PUT /machines/{cfg['client_mac']} (boot_policy=flash)")
        _put_assignment("127.0.0.1", mgmt_port, cfg)

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


def _put_assignment(host, port, cfg):
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
            "Authorization": f"Bearer {cfg['token']}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status != 200:
            raise RuntimeError(f"PUT /machines returned {resp.status}")


def _dump_tail(path, lines):
    if not path.is_file():
        log.error(f"{path}: file does not exist")
        return
    body = path.read_text(errors="replace")
    log.error(f"--- last {lines} lines of {path} ---")
    for line in body.splitlines()[-lines:]:
        log.error(line)
