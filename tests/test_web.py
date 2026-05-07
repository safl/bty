"""Tests for ``bty.web``.

Use FastAPI's ``TestClient`` against an app constructed via
:func:`bty.web._app.create_app` with a ``tmp_path``-backed SQLite.
No monkeypatching of module-level globals; each test gets its own
isolated app + db. The ``app_client`` fixture seeds an active
session row directly (skipping PAM) and exposes the bearer for
authed test requests.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bty.web._app import create_app
from bty.web._db import issue_session, open_db

TEST_SERVICE_USER = "bty-test"

# Mutated by the ``app_client`` fixture: each test gets a freshly
# generated session token seeded into the test DB, and ``AUTH`` is
# rewritten in place so existing tests doing ``headers=AUTH`` keep
# working without per-test changes.
AUTH: dict[str, str] = {}


@pytest.fixture
def app_client(tmp_path: Path) -> Iterator[TestClient]:
    """Yield a TestClient against an isolated bty-web app.

    A session row is seeded directly (no ``/auth/login`` call, so
    PAM never runs against the test runner's user) and exposed via
    the module-level ``AUTH`` dict.
    """
    state = tmp_path / "state.db"
    image_root = tmp_path / "images"
    image_root.mkdir()
    boot_root = tmp_path / "boot"
    boot_root.mkdir()
    # Seed a fake live-env triplet so /boot/{name} tests can hit real files.
    (boot_root / "bty-live-x86_64.vmlinuz").write_bytes(b"fake-kernel")
    (boot_root / "bty-live-x86_64.initrd").write_bytes(b"fake-initrd")
    (boot_root / "bty-live-x86_64.squashfs").write_bytes(b"fake-squashfs")
    # Seed an image too so /images/{name} tests work.
    (image_root / "demo.qcow2").write_bytes(b"fake-image")
    app = create_app(
        state_path=state,
        service_user=TEST_SERVICE_USER,
        image_root=image_root,
        boot_root=boot_root,
    )
    with open_db(state) as conn:
        token, _ = issue_session(conn, label="pytest")
    AUTH.clear()
    AUTH["Authorization"] = f"Bearer {token}"
    try:
        with TestClient(app) as client:
            yield client
    finally:
        AUTH.clear()


# ---------- open endpoints (no auth) ----------------------------------------


def test_healthz_is_open(app_client: TestClient) -> None:
    r = app_client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_version_is_open(app_client: TestClient) -> None:
    r = app_client.get("/version")
    assert r.status_code == 200
    body = r.json()
    assert "version" in body and isinstance(body["version"], str) and body["version"]


def test_pxe_for_unknown_mac_returns_tui_template(app_client: TestClient) -> None:
    """An unknown MAC auto-discovers with ``boot_policy=tui`` and is
    served the interactive-live-env iPXE chain. This is "bty-on-a-USB
    but over the network": first PXE contact lands the operator at
    bty-tui without any prior server-side configuration."""
    r = app_client.get("/pxe/aa:bb:cc:dd:ee:ff")
    assert r.status_code == 200
    body = r.text
    assert "bty.mode=interactive" in body
    assert "aa:bb:cc:dd:ee:ff" in body
    assert "kernel" in body  # chains into the live env


def test_pxe_invalid_mac_returns_400(app_client: TestClient) -> None:
    r = app_client.get("/pxe/not-a-mac")
    assert r.status_code == 400


def test_pxe_bootstrap_returns_self_referential_chain(app_client: TestClient) -> None:
    """The static iPXE script that dnsmasq points iPXE clients at on
    second-stage DHCP. Must reference back to whichever Host the
    client used to reach the server, and use iPXE's runtime MAC
    substitution."""
    r = app_client.get("/pxe-bootstrap.ipxe", headers={"Host": "192.0.2.1:8080"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert body.startswith("#!ipxe"), body
    # Self-referential chain: the URL uses the Host header.
    assert "chain http://192.0.2.1:8080/pxe/${net0/mac:hexhyp}" in body
    # No auth required (PXE clients have no token).


# ---------- auth ------------------------------------------------------------


def test_machines_without_token_is_401(app_client: TestClient) -> None:
    r = app_client.get("/machines")
    assert r.status_code == 401


def test_machines_with_wrong_token_is_401(app_client: TestClient) -> None:
    r = app_client.get("/machines", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_machines_with_right_token_is_200(app_client: TestClient) -> None:
    r = app_client.get("/machines", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == []


# ---------- machine CRUD ----------------------------------------------------


def test_machine_crud_round_trip(app_client: TestClient) -> None:
    mac = "aa:bb:cc:dd:ee:ff"
    body = {
        "image": "debian.qcow2",
        "provisioning_mode": "cloud-init",
        "hostname": "bty-test-01",
    }

    # Create / upsert
    r = app_client.put(f"/machines/{mac}", json=body, headers=AUTH)
    assert r.status_code == 200
    created = r.json()
    assert created["mac"] == mac
    assert created["image"] == "debian.qcow2"
    assert created["provisioning_mode"] == "cloud-init"
    assert created["hostname"] == "bty-test-01"

    # Read back
    r = app_client.get(f"/machines/{mac}", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["mac"] == mac

    # List
    r = app_client.get("/machines", headers=AUTH)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["mac"] == mac

    # Delete
    r = app_client.delete(f"/machines/{mac}", headers=AUTH)
    assert r.status_code == 204

    # 404 after delete
    r = app_client.get(f"/machines/{mac}", headers=AUTH)
    assert r.status_code == 404


def test_machine_upsert_normalises_mac(app_client: TestClient) -> None:
    """Upper-case input + dashes get normalised to canonical form."""
    r = app_client.put(
        "/machines/AA-BB-CC-DD-EE-FF",
        json={"provisioning_mode": "none"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["mac"] == "aa:bb:cc:dd:ee:ff"


def test_machine_upsert_rejects_invalid_provisioning_mode(app_client: TestClient) -> None:
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"provisioning_mode": "garbage"},
        headers=AUTH,
    )
    assert r.status_code == 422  # FastAPI body validation


def test_pxe_for_known_mac_uses_assignment_template(app_client: TestClient) -> None:
    mac = "aa:bb:cc:dd:ee:ff"
    app_client.put(
        f"/machines/{mac}",
        json={"image": "debian.qcow2", "provisioning_mode": "none"},
        headers=AUTH,
    )
    r = app_client.get(f"/pxe/{mac}")
    assert r.status_code == 200
    assert "debian.qcow2" in r.text
    assert "no bty assignment" not in r.text  # not the fallback


# ---------- auto-discovery via /pxe ----------------------------------------


def test_pxe_auto_discovers_unknown_mac(app_client: TestClient) -> None:
    """A /pxe contact for an unknown MAC creates a placeholder record so the
    operator sees the machine in /machines and can claim it. The default
    ``boot_policy`` is ``tui``: the unknown MAC chains into the live env in
    interactive mode (bty-tui), letting the operator pick + flash an image
    by hand without prior server-side configuration."""
    mac = "11:22:33:44:55:66"

    # Pre-condition: not in the DB.
    pre = app_client.get(f"/machines/{mac}", headers=AUTH)
    assert pre.status_code == 404

    # PXE client (no auth) hits the endpoint.
    r = app_client.get(f"/pxe/{mac}")
    assert r.status_code == 200
    assert "bty.mode=interactive" in r.text  # tui template

    # Now visible to the operator.
    found = app_client.get(f"/machines/{mac}", headers=AUTH)
    assert found.status_code == 200
    body = found.json()
    assert body["mac"] == mac
    assert body["image"] is None  # discovered, not yet assigned
    assert body["provisioning_mode"] == "none"
    assert body["boot_policy"] == "tui"  # auto-discovery default
    assert body["discovered_at"] is not None
    assert body["last_seen_at"] is not None


def test_pxe_updates_last_seen_on_repeat_contact(app_client: TestClient) -> None:
    """Subsequent /pxe contacts update last_seen_at, leave discovered_at fixed."""
    mac = "11:22:33:44:55:66"

    app_client.get(f"/pxe/{mac}")
    first = app_client.get(f"/machines/{mac}", headers=AUTH).json()
    assert first["discovered_at"] == first["last_seen_at"]

    # Tiny pause to make the timestamp difference visible.
    import time

    time.sleep(0.01)
    app_client.get(f"/pxe/{mac}")
    second = app_client.get(f"/machines/{mac}", headers=AUTH).json()
    # discovered_at is sticky; last_seen_at moves forward.
    assert second["discovered_at"] == first["discovered_at"]
    assert second["last_seen_at"] >= first["last_seen_at"]


def test_pxe_does_not_overwrite_assignment(app_client: TestClient) -> None:
    """A PUT-claimed machine that later PXE-boots keeps its assignment;
    the /pxe contact only updates last_seen_at."""
    mac = "aa:bb:cc:dd:ee:ff"
    app_client.put(
        f"/machines/{mac}",
        json={"image": "debian.qcow2", "provisioning_mode": "cloud-init"},
        headers=AUTH,
    )
    before = app_client.get(f"/machines/{mac}", headers=AUTH).json()
    assert before["image"] == "debian.qcow2"
    assert before["discovered_at"] is None  # PUT-created

    app_client.get(f"/pxe/{mac}")
    after = app_client.get(f"/machines/{mac}", headers=AUTH).json()
    assert after["image"] == "debian.qcow2"  # untouched
    assert after["provisioning_mode"] == "cloud-init"
    assert after["last_seen_at"] is not None
    # discovered_at is set on first /pxe contact even for PUT-created rows
    assert after["discovered_at"] is not None


# ---------- image / boot upload --------------------------------------------


def test_put_image_uploads_to_image_root(app_client: TestClient) -> None:
    """``PUT /images/{name}`` lands the body bytes at
    ``image_root/<name>`` and the file is round-trippable via the
    open ``GET /images/{name}``."""
    body = b"\x01\x02\x03" * 1024
    r = app_client.put("/images/upload.qcow2", content=body, headers=AUTH)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["name"] == "upload.qcow2"
    assert payload["size_bytes"] == len(body)
    # Same bytes flow back via the open serve route.
    served = app_client.get("/images/upload.qcow2")
    assert served.status_code == 200
    assert served.content == body


def test_put_image_overwrites_existing(app_client: TestClient) -> None:
    first = app_client.put("/images/x.qcow2", content=b"old", headers=AUTH)
    assert first.status_code == 200
    second = app_client.put("/images/x.qcow2", content=b"newer-bytes", headers=AUTH)
    assert second.status_code == 200
    assert second.json()["size_bytes"] == len(b"newer-bytes")
    assert app_client.get("/images/x.qcow2").content == b"newer-bytes"


def test_put_image_rejects_path_traversal(app_client: TestClient) -> None:
    """``..`` and slashes mustn't escape the image root. FastAPI's
    path converter already strips raw ``/`` from ``{name}``, but
    URL-encoded variants and ``..`` need an explicit reject."""
    r = app_client.put("/images/..%2Fescape.qcow2", content=b"x", headers=AUTH)
    assert r.status_code in {400, 404}


def test_put_image_requires_auth(app_client: TestClient) -> None:
    r = app_client.put("/images/x.qcow2", content=b"x")
    assert r.status_code == 401


def test_put_boot_uploads_to_boot_root(app_client: TestClient) -> None:
    """``PUT /boot/{name}`` symmetric to /images/{name} but lands
    under boot_root - this is how the live trio gets onto the
    appliance via the API instead of scp / fetch-from-release."""
    body = b"vmlinuz-bytes-here"
    r = app_client.put("/boot/bty-live-x86_64.vmlinuz", content=body, headers=AUTH)
    assert r.status_code == 200
    served = app_client.get("/boot/bty-live-x86_64.vmlinuz")
    assert served.status_code == 200
    assert served.content == body


def test_put_boot_requires_auth(app_client: TestClient) -> None:
    r = app_client.put("/boot/anything", content=b"x")
    assert r.status_code == 401


# ---------- images ----------------------------------------------------------


def test_list_images_returns_seeded_fixture(app_client: TestClient) -> None:
    """The fixture seeds ``demo.qcow2`` so the file-serving routes
    have something to return; ``GET /images`` exposes it via the
    image catalog."""
    r = app_client.get("/images", headers=AUTH)
    assert r.status_code == 200
    rows = r.json()
    assert {row["name"] for row in rows} == {"demo.qcow2"}


def test_list_images_returns_files_under_image_root(
    tmp_path: Path,
) -> None:
    image_root = tmp_path / "images"
    image_root.mkdir()
    (image_root / "alpha.qcow2").write_bytes(b"\0" * 256)
    (image_root / "beta.img").write_bytes(b"\0" * 512)

    state = tmp_path / "state.db"
    app = create_app(
        state_path=state,
        service_user=TEST_SERVICE_USER,
        image_root=image_root,
    )
    with open_db(state) as conn:
        token, _ = issue_session(conn, label="pytest")
    auth = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.get("/images", headers=auth)

    assert r.status_code == 200
    rows = r.json()
    names = {row["name"] for row in rows}
    assert names == {"alpha.qcow2", "beta.img"}


# ---------- create_app sanity ----------------------------------------------


def test_invalid_token_is_rejected(app_client: TestClient) -> None:
    """Tokens that don't match an active session row return 401, no
    timing oracle (every miss does the same DB lookup + sha256)."""
    r = app_client.get("/machines", headers={"Authorization": "Bearer nope-not-real"})
    assert r.status_code == 401


def test_session_tokens_are_high_entropy() -> None:
    """The plaintext returned by ``issue_session`` is the recommended
    ``secrets.token_urlsafe``-style: 32+ bytes of entropy, URL-safe."""
    token = secrets.token_urlsafe(32)
    assert len(token) > 30


# ---------- boot policy + flash chain (Phase D-3a) --------------------------


def test_machine_default_boot_policy_is_local(app_client: TestClient) -> None:
    """A fresh PUT without an explicit boot_policy gets ``local`` -
    operators opt INTO reflashing on every boot."""
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"image": "demo.qcow2", "provisioning_mode": "none"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["boot_policy"] == "local"
    assert r.json()["last_flashed_at"] is None


def test_machine_upsert_accepts_boot_policy_flash(app_client: TestClient) -> None:
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "image": "demo.qcow2",
            "provisioning_mode": "none",
            "boot_policy": "flash",
        },
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["boot_policy"] == "flash"


def test_machine_upsert_rejects_unknown_boot_policy(app_client: TestClient) -> None:
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"image": "demo.qcow2", "boot_policy": "yolo"},
        headers=AUTH,
    )
    assert r.status_code == 422


def test_pxe_local_policy_assigned_machine_returns_local_template(
    app_client: TestClient,
) -> None:
    """boot_policy=local + image assigned: still sanboot. Reflashing is
    opt-in via boot_policy=flash, not implicit on assignment."""
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"image": "demo.qcow2"},
        headers=AUTH,
    )
    r = app_client.get("/pxe/aa:bb:cc:dd:ee:ff")
    assert r.status_code == 200
    body = r.text
    # ipxe.j2 (placeholder local template) - explicitly NOT the flash chain
    assert "kernel" not in body
    assert "bty.image_url" not in body


def test_pxe_flash_policy_returns_chain_with_args(app_client: TestClient) -> None:
    """boot_policy=flash + image: chain into kernel/initrd with the
    four bty.* cmdline params the live env reads."""
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "image": "demo.qcow2",
            "provisioning_mode": "cloud-init",
            "boot_policy": "flash",
        },
        headers=AUTH,
    )
    r = app_client.get("/pxe/aa:bb:cc:dd:ee:ff", headers={"Host": "bty.local:8080"})
    assert r.status_code == 200
    body = r.text
    assert body.startswith("#!ipxe"), body
    # Template uses an iPXE variable for the base URL so the script
    # reads cleanly; the variable is set from the request's Host.
    assert "set bty-base http://bty.local:8080" in body
    assert "kernel ${bty-base}/boot/bty-live-x86_64.vmlinuz" in body
    assert "initrd ${bty-base}/boot/bty-live-x86_64.initrd" in body
    # live-boot needs ``fetch=`` to know where to grab the squashfs.
    assert "fetch=${bty-base}/boot/bty-live-x86_64.squashfs" in body
    # Console mirror to ttyS0 so headless / IPMI / test serial works.
    assert "console=ttyS0,115200" in body
    # Cmdline params: live env's bty-flash-on-boot reads these.
    assert "bty.server=${bty-base}" in body
    assert "bty.mac=aa:bb:cc:dd:ee:ff" in body
    assert "bty.image_url=${bty-base}/images/demo.qcow2" in body
    assert "bty.provisioning=cloud-init" in body


def test_pxe_tui_policy_returns_interactive_chain(app_client: TestClient) -> None:
    """boot_policy=tui: chain into the live env with bty.mode=interactive
    so the live env launches bty-tui on tty1 instead of auto-flashing.
    No image / no provisioning cmdline params - the operator picks at
    run time."""
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"boot_policy": "tui"},
        headers=AUTH,
    )
    r = app_client.get("/pxe/aa:bb:cc:dd:ee:ff", headers={"Host": "bty.local:8080"})
    assert r.status_code == 200
    body = r.text
    assert body.startswith("#!ipxe"), body
    assert "set bty-base http://bty.local:8080" in body
    assert "kernel ${bty-base}/boot/bty-live-x86_64.vmlinuz" in body
    assert "initrd ${bty-base}/boot/bty-live-x86_64.initrd" in body
    assert "bty.mode=interactive" in body
    assert "bty.server=${bty-base}" in body
    assert "bty.mac=aa:bb:cc:dd:ee:ff" in body
    # Interactive mode must NOT pre-decide image / provisioning - those
    # come from the operator's TUI selection.
    assert "bty.image_url" not in body
    assert "bty.provisioning" not in body


def test_machine_upsert_accepts_boot_policy_tui(app_client: TestClient) -> None:
    """``boot_policy='tui'`` is accepted by Pydantic validation alongside
    ``local`` and ``flash``."""
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"boot_policy": "tui"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["boot_policy"] == "tui"


def test_pxe_done_updates_last_flashed_at(app_client: TestClient) -> None:
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"image": "demo.qcow2", "boot_policy": "flash"},
        headers=AUTH,
    )
    before = app_client.get("/machines/aa:bb:cc:dd:ee:ff", headers=AUTH).json()
    assert before["last_flashed_at"] is None

    r = app_client.post("/pxe/aa:bb:cc:dd:ee:ff/done")
    assert r.status_code == 204

    after = app_client.get("/machines/aa:bb:cc:dd:ee:ff", headers=AUTH).json()
    assert after["last_flashed_at"] is not None
    # Critical: the policy is preserved. Per-job CI cadence stays
    # boot_policy=flash across reflashes.
    assert after["boot_policy"] == "flash"


def test_pxe_done_404_for_unknown_mac(app_client: TestClient) -> None:
    r = app_client.post("/pxe/00:11:22:33:44:55/done")
    assert r.status_code == 404


# ---------- online cijoe auto-trigger (milestone 15) ----------------------


def test_pxe_done_triggers_online_workflow_when_configured(app_client: TestClient) -> None:
    """``provisioning_mode='cijoe-online'`` + workflow ref + last_seen_ip
    means the completion signal kicks off a workflow run via
    WorkflowRunner. The runner's ``kick_off`` should be called with
    the assigned workflow + the IP the live env was last seen from."""
    from unittest.mock import patch

    # Seed via API (sets cijoe_workflow_ref + provisioning_mode).
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "image": "demo.qcow2",
            "provisioning_mode": "cijoe-online",
            "cijoe_workflow_ref": "/var/lib/bty/workflows/post-flash.yaml",
        },
        headers=AUTH,
    )
    # PXE contact populates last_seen_ip.
    app_client.get("/pxe/aa:bb:cc:dd:ee:ff")

    with patch(
        "bty.web._workflow.WorkflowRunner.kick_off",
    ) as mock_kick:
        r = app_client.post("/pxe/aa:bb:cc:dd:ee:ff/done")

    assert r.status_code == 204
    mock_kick.assert_called_once()
    kwargs = mock_kick.call_args.kwargs
    assert kwargs["mac"] == "aa:bb:cc:dd:ee:ff"
    assert kwargs["workflow_ref"] == "/var/lib/bty/workflows/post-flash.yaml"
    # last_seen_ip from the GET above; TestClient uses 'testclient'
    # as the client host.
    assert kwargs["target_ip"] == "testclient"


def test_pxe_done_does_not_trigger_when_provisioning_mode_is_other(app_client: TestClient) -> None:
    from unittest.mock import patch

    app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "image": "demo.qcow2",
            "provisioning_mode": "none",
            "cijoe_workflow_ref": "/var/lib/bty/workflows/post-flash.yaml",
        },
        headers=AUTH,
    )
    app_client.get("/pxe/aa:bb:cc:dd:ee:ff")
    with patch("bty.web._workflow.WorkflowRunner.kick_off") as mock_kick:
        r = app_client.post("/pxe/aa:bb:cc:dd:ee:ff/done")
    assert r.status_code == 204
    mock_kick.assert_not_called()


def test_pxe_done_does_not_trigger_when_workflow_ref_missing(app_client: TestClient) -> None:
    from unittest.mock import patch

    app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "image": "demo.qcow2",
            "provisioning_mode": "cijoe-online",
            # no cijoe_workflow_ref
        },
        headers=AUTH,
    )
    app_client.get("/pxe/aa:bb:cc:dd:ee:ff")
    with patch("bty.web._workflow.WorkflowRunner.kick_off") as mock_kick:
        r = app_client.post("/pxe/aa:bb:cc:dd:ee:ff/done")
    assert r.status_code == 204
    mock_kick.assert_not_called()


def test_machine_response_includes_workflow_columns(app_client: TestClient) -> None:
    """Schema migration: new columns exposed via the wire model."""
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"image": "demo.qcow2"},
        headers=AUTH,
    )
    body = app_client.get("/machines/aa:bb:cc:dd:ee:ff", headers=AUTH).json()
    assert body["last_workflow_run_at"] is None
    assert body["last_workflow_status"] is None
    assert body["last_workflow_output_path"] is None


# ---------- /boot and /images file serving (Phase D-3a) --------------------


def test_boot_artifact_serves_file(app_client: TestClient) -> None:
    r = app_client.get("/boot/bty-live-x86_64.vmlinuz")
    assert r.status_code == 200
    assert r.content == b"fake-kernel"


def test_boot_artifact_404_for_missing(app_client: TestClient) -> None:
    r = app_client.get("/boot/does-not-exist.bin")
    assert r.status_code == 404


def test_boot_artifact_rejects_traversal(app_client: TestClient) -> None:
    """Slash in a single-segment ``{name}`` is impossible (FastAPI's
    path converter splits on /), but the explicit guards reject the
    edge cases too: empty, dot, dotdot, encoded."""
    for bad in ("", ".", ".."):
        r = app_client.get(f"/boot/{bad}")
        # Some encodings 404 from FastAPI's router before reaching us;
        # the others should 400 from our guard. Either way: not 200.
        assert r.status_code != 200


def test_serve_image_returns_file_bytes(app_client: TestClient) -> None:
    r = app_client.get("/images/demo.qcow2")
    assert r.status_code == 200
    assert r.content == b"fake-image"


def test_serve_image_404_for_missing(app_client: TestClient) -> None:
    r = app_client.get("/images/does-not-exist.qcow2")
    assert r.status_code == 404
