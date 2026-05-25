"""Integration tests for the recovery-mode bty-web (v0.32.0).

When ``bty.web._db.check_db`` returns a needs-recovery result,
``create_app`` builds a minimal FastAPI app from
``bty.web._recovery.build_recovery_app`` instead of the full app.
These tests pin the wizard's contract end-to-end via Starlette's
``TestClient``: the recovery page renders, ``/ui/recovery/status``
returns a faithful snapshot, ``POST /ui/recovery/wipe`` removes
``state.db`` (the ``os._exit(0)`` after-response is patched out so
the test process survives), and ``POST /ui/recovery/wipe-and-import``
loads a v2 bundle into a fresh DB.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bty.web import _db, _recovery


@pytest.fixture
def _no_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recovery POST actions schedule ``os._exit(0)`` to fire
    after the response flushes; obviously fatal in a test process.
    Replace the scheduler with a no-op so the test client keeps
    running."""
    monkeypatch.setattr(_recovery, "_schedule_exit_after_response", lambda: None)


def _stand_up_pre_versioning_db(state_path: Path) -> None:
    """Seed a state.db that looks like one created by a bty release
    predating the ``bty_version`` marker (v0.30.x and earlier).
    """
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(state_path) as conn:
        conn.execute(
            "CREATE TABLE machines (mac TEXT PRIMARY KEY, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO machines VALUES (?, ?, ?)",
            ("aa:bb:cc:dd:ee:ff", "2026-05-01T00:00:00+00:00", "2026-05-01T00:00:00+00:00"),
        )
        # Catalog + events for the at-risk summary.
        conn.execute(
            "CREATE TABLE catalog_entries (bty_image_ref TEXT PRIMARY KEY, "
            "src TEXT, disk_image_sha TEXT, name TEXT, format TEXT, added_at TEXT)"
        )
        conn.execute(
            "INSERT INTO catalog_entries (bty_image_ref, src, name, format, added_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("ref-demo", "https://example.invalid/demo.img.gz", "demo", "img.gz", "2026"),
        )
        conn.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, summary TEXT)"
        )
        conn.execute(
            "INSERT INTO events (ts, kind, summary) VALUES (?, ?, ?)",
            ("2026-05-01T00:00:00+00:00", "machine.discovered", "..."),
        )
        conn.commit()


def _build_app(tmp_path: Path) -> TestClient:
    state_path = tmp_path / "state.db"
    _stand_up_pre_versioning_db(state_path)
    db_check = _db.check_db(state_path)
    assert db_check.needs_recovery, "fixture: state.db must trip recovery mode"
    app = _recovery.build_recovery_app(
        state_path=state_path,
        image_root=tmp_path / "images",
        backups_root=tmp_path / "backups",
        secret_key="test-secret",
        service_user="test-user",
        db_check=db_check,
    )
    return TestClient(app)


# ---------------------------------------------------------------------
# Wizard page renders + carries the operator-facing context
# ---------------------------------------------------------------------


def test_root_redirects_to_recovery_wizard(tmp_path: Path) -> None:
    """``GET /`` lands on the wizard. Operator's browser bookmark on
    the appliance URL works without an extra click."""
    client = _build_app(tmp_path)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/recovery"


def test_recovery_wizard_renders_with_at_risk_counts(tmp_path: Path) -> None:
    """The wizard surfaces (a) why bty-web is in recovery, (b) the
    stored vs running versions, and (c) the at-risk counts the
    operator is about to wipe -- so they make an informed call."""
    client = _build_app(tmp_path)
    r = client.get("/ui/recovery")
    assert r.status_code == 200
    body = r.text
    # Banner copy.
    assert "recovery mode" in body.lower()
    assert "pre-versioning" in body
    # At-risk summary: counts from _stand_up_pre_versioning_db (1
    # machine, 1 catalog entry, 1 event).
    assert "1 machines" in body
    assert "1 catalog entries" in body
    assert "1 audit events" in body
    # Strategy buttons present (the operator-actionable bits).
    assert "btn-wipe" in body  # strategy A button id
    # No backups in this fixture -> empty-state copy renders.
    assert "No backup bundles found" in body


def test_recovery_wizard_lists_available_backups(tmp_path: Path) -> None:
    """When ``backups_root`` has v2 bundles, the wizard renders the
    picker with each bundle's ``backup_id`` + machine/file counts +
    the bty version that created it."""
    backups_root = tmp_path / "backups"
    bundle = backups_root / "2026-05-25T08-00-00Z"
    (bundle / "files").mkdir(parents=True)
    (bundle / "files" / "demo.img.gz").write_bytes(b"\0" * 16)
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "bty_export_version": 2,
                "exported_at": "2026-05-25T08:00:00+00:00",
                "exported_by_bty_version": "0.31.0",
                "machines": [{"mac": "11:22:33:44:55:66"}, {"mac": "aa:bb:cc:dd:ee:ff"}],
            }
        )
    )
    # Don't go through _build_app -- it wants state.db in tmp_path
    # not under backups/, but we need the same tmp_path for both.
    state_path = tmp_path / "state.db"
    _stand_up_pre_versioning_db(state_path)
    db_check = _db.check_db(state_path)
    app = _recovery.build_recovery_app(
        state_path=state_path,
        image_root=tmp_path / "images",
        backups_root=backups_root,
        secret_key="test",
        service_user="test",
        db_check=db_check,
    )
    client = TestClient(app)
    body = client.get("/ui/recovery").text
    assert "2026-05-25T08-00-00Z" in body
    assert "bty v0.31.0" in body
    assert "2 machines, 1 files" in body
    assert "btn-import" in body
    assert "No backup bundles found" not in body


# ---------------------------------------------------------------------
# /ui/recovery/status: the polling endpoint
# ---------------------------------------------------------------------


def test_status_returns_needs_recovery_for_pre_versioning_db(tmp_path: Path) -> None:
    """The wizard's JS polls this endpoint to detect when the wipe
    has completed (i.e. ``needs_recovery`` flips False or the
    endpoint stops existing because the normal app is up)."""
    client = _build_app(tmp_path)
    r = client.get("/ui/recovery/status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "pre_versioning"
    assert body["needs_recovery"] is True
    assert body["stored_version"] is None
    assert body["at_risk"]["machines"] == 1


# ---------------------------------------------------------------------
# POST /ui/recovery/wipe
# ---------------------------------------------------------------------


def test_wipe_deletes_state_db_and_signals_restart(tmp_path: Path, _no_exit: None) -> None:
    """``POST /ui/recovery/wipe`` unlinks ``state.db`` (+ any sqlite
    sidecars), responds 200, and schedules a process exit (mocked
    out in tests). systemd then re-launches bty-web against an
    empty directory; ``init_db`` stamps a clean version on first
    call. Sidecar wipe matters: a leftover ``state.db-journal``
    would surface as a sqlite ``Database is malformed`` on the
    next start."""
    state_path = tmp_path / "state.db"
    _stand_up_pre_versioning_db(state_path)
    # Drop a fake -journal sidecar so we can assert it's swept too.
    (state_path.parent / "state.db-journal").write_bytes(b"j")

    db_check = _db.check_db(state_path)
    app = _recovery.build_recovery_app(
        state_path=state_path,
        image_root=tmp_path / "images",
        backups_root=tmp_path / "backups",
        secret_key="test",
        service_user="test",
        db_check=db_check,
    )
    client = TestClient(app)
    r = client.post("/ui/recovery/wipe")
    assert r.status_code == 200
    assert not state_path.exists(), "state.db must be unlinked"
    assert not (state_path.parent / "state.db-journal").exists(), (
        "sqlite -journal sidecar must be unlinked too"
    )


# ---------------------------------------------------------------------
# POST /ui/recovery/wipe-and-import
# ---------------------------------------------------------------------


def test_wipe_and_import_loads_bundle_into_fresh_db(tmp_path: Path, _no_exit: None) -> None:
    """The "wipe + import from backup" path: wipes state.db, then
    runs ``_portability.import_bundle`` against the selected
    bundle. After this returns, state.db is a freshly-init'd DB
    (current version stamped) carrying the bundle's machines +
    image files. Operator re-binds; hardware identity survived
    the wipe."""
    state_path = tmp_path / "state.db"
    image_root = tmp_path / "images"
    backups_root = tmp_path / "backups"
    _stand_up_pre_versioning_db(state_path)

    # Stage a v2 bundle to import.
    bundle = backups_root / "2026-05-25T07-00-00Z"
    (bundle / "files").mkdir(parents=True)
    (bundle / "files" / "operator-typed.img.gz").write_bytes(b"\xff" * 64)
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "bty_export_version": 2,
                "exported_at": "2026-05-25T07:00:00+00:00",
                "exported_by_bty_version": "0.31.1",
                "machines": [
                    {
                        "mac": "0c:bf:b4:c0:4b:42",
                        "hw_lshw": '{"id": "system", "vendor": "GMKtec"}',
                        "known_disks": '[{"path": "/dev/sda", "serial": "ABC123"}]',
                        "known_disks_at": "2026-05-25T06:00:00+00:00",
                        "hw_lshw_at": "2026-05-25T06:00:00+00:00",
                    }
                ],
            }
        )
    )

    db_check = _db.check_db(state_path)
    app = _recovery.build_recovery_app(
        state_path=state_path,
        image_root=image_root,
        backups_root=backups_root,
        secret_key="test",
        service_user="test",
        db_check=db_check,
    )
    client = TestClient(app)

    r = client.post(
        "/ui/recovery/wipe-and-import",
        json={"backup_id": "2026-05-25T07-00-00Z"},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["status"] == "imported"
    assert payload["machines"] == 1
    assert payload["files"] == 1

    # Verify the new state.db carries the imported machine + the
    # fresh version stamp.
    with sqlite3.connect(state_path) as conn:
        machines = conn.execute("SELECT mac, hw_lshw FROM machines").fetchall()
        version = conn.execute("SELECT version FROM bty_version").fetchone()
    assert len(machines) == 1
    assert machines[0][0] == "0c:bf:b4:c0:4b:42"
    assert "GMKtec" in machines[0][1]
    import bty as _bty_pkg

    assert version is not None and version[0] == _bty_pkg.__version__

    # Image files landed in image_root.
    assert (image_root / "operator-typed.img.gz").read_bytes() == b"\xff" * 64


def test_wipe_and_import_rejects_unknown_backup_id(tmp_path: Path, _no_exit: None) -> None:
    """A backup_id that doesn't resolve to a directory under
    ``backups_root`` returns 404 with a clear message -- and the
    wipe doesn't fire, so state.db is preserved for the operator
    to try a different recovery."""
    state_path = tmp_path / "state.db"
    _stand_up_pre_versioning_db(state_path)
    db_check = _db.check_db(state_path)
    app = _recovery.build_recovery_app(
        state_path=state_path,
        image_root=tmp_path / "images",
        backups_root=tmp_path / "backups",
        secret_key="test",
        service_user="test",
        db_check=db_check,
    )
    client = TestClient(app)
    r = client.post(
        "/ui/recovery/wipe-and-import",
        json={"backup_id": "2099-12-31T00-00-00Z"},
    )
    assert r.status_code == 404
    assert state_path.exists(), "wipe must NOT fire when bundle is missing"


def test_wipe_and_import_rejects_path_traversal(tmp_path: Path, _no_exit: None) -> None:
    """The backup_id is joined onto backups_root unsanitised after
    the validator; the validator must reject any segment containing
    path separators / dots / NUL so the operator can't accidentally
    point the importer at ``/etc/passwd``-like paths."""
    state_path = tmp_path / "state.db"
    _stand_up_pre_versioning_db(state_path)
    db_check = _db.check_db(state_path)
    app = _recovery.build_recovery_app(
        state_path=state_path,
        image_root=tmp_path / "images",
        backups_root=tmp_path / "backups",
        secret_key="test",
        service_user="test",
        db_check=db_check,
    )
    client = TestClient(app)
    for bad in ("..", ".", "../etc", "with/slash", "with\\backslash"):
        r = client.post("/ui/recovery/wipe-and-import", json={"backup_id": bad})
        assert r.status_code == 400, f"should reject {bad!r}, got {r.status_code}"
        assert state_path.exists()


# ---------------------------------------------------------------------
# Other routes return 503 with a redirect hint
# ---------------------------------------------------------------------


def test_other_routes_return_503_redirecting_to_wizard(tmp_path: Path) -> None:
    """Operators who hit a bookmarked normal-mode URL while bty-web
    is in recovery should see a 503 page that points them back at
    the wizard. The HTML meta-refresh + visible link cover both
    "I forgot to look at the page" and "my CI script is hitting an
    endpoint" cases."""
    client = _build_app(tmp_path)
    for path in ("/ui/dashboard", "/ui/machines", "/ui/images", "/ui/settings"):
        r = client.get(path)
        assert r.status_code == 503, path
        assert "/ui/recovery" in r.text, path


def test_healthz_returns_503_in_recovery_mode(tmp_path: Path) -> None:
    """``/healthz`` is a stable probe endpoint (CI, ops dashboards);
    in recovery mode it must report unhealthy + tell the prober why.
    Distinct from the catchall 503 because automated probers want
    JSON, not HTML."""
    client = _build_app(tmp_path)
    r = client.get("/healthz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "recovery"
    assert "/ui/recovery" in body["reason"]
