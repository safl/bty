"""FastAPI application for bty-web.

``create_app(state_path, bearer_token, image_root)`` returns a fully
wired FastAPI instance. Tests construct one with a tmp_path SQLite +
test token; ``main()`` (in :mod:`bty.web.__init__`) builds one from
environment + defaults and hands it to uvicorn.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.responses import PlainTextResponse
from jinja2 import Environment, FileSystemLoader

import bty
from bty import images
from bty.web import _db, _models
from bty.web._auth import make_token_dep

TEMPLATES_DIR = Path(__file__).parent / "_templates"


def create_app(
    *,
    state_path: Path,
    bearer_token: str,
    image_root: Path | None = None,
) -> FastAPI:
    """Build the FastAPI app. All config flows through this function.

    Production callers pass values resolved from the environment;
    tests pass tmp_path + a test token.
    """
    require_token = make_token_dep(bearer_token)
    resolved_image_root: Path = image_root or images.default_image_root()

    jinja = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=False,  # iPXE scripts are plain text, not HTML
        keep_trailing_newline=True,
    )

    _db.init_db(state_path)

    app = FastAPI(title="bty-web", version=bty.__version__)

    # ----- Open routes (no auth) ------------------------------------------

    @app.get("/healthz", response_model=_models.HealthResponse)
    def healthz() -> _models.HealthResponse:
        return _models.HealthResponse()

    @app.get("/version", response_model=_models.VersionResponse)
    def version() -> _models.VersionResponse:
        return _models.VersionResponse(version=bty.__version__)

    @app.get("/pxe/{mac}", response_class=PlainTextResponse)
    def pxe(mac: str) -> str:
        normalised = _normalise_mac(mac)
        with _db.open_db(state_path) as conn:
            row = conn.execute("SELECT * FROM machines WHERE mac = ?", (normalised,)).fetchone()
        template_name = "ipxe.j2" if row is not None else "ipxe_unknown.j2"
        template = jinja.get_template(template_name)
        return template.render(mac=normalised, machine=dict(row) if row else None)

    @app.post("/bootstrap/{mac}", response_class=PlainTextResponse)
    def bootstrap(mac: str) -> str:
        normalised = _normalise_mac(mac)
        # Stub for milestone 11. The real bootstrap script (live env's
        # post-PXE handoff) is wired up in milestone 14.
        return (
            "#!/bin/sh\n"
            f"# bty-web bootstrap placeholder for {normalised}\n"
            "echo 'milestone 14 wires the real bootstrap'\n"
        )

    # ----- Protected routes (Bearer required) -----------------------------

    @app.get(
        "/machines",
        response_model=list[_models.Machine],
        dependencies=[Depends(require_token)],
    )
    def list_machines() -> list[_models.Machine]:
        with _db.open_db(state_path) as conn:
            rows = conn.execute("SELECT * FROM machines ORDER BY mac").fetchall()
        return [_row_to_machine(r) for r in rows]

    @app.get(
        "/machines/{mac}",
        response_model=_models.Machine,
        dependencies=[Depends(require_token)],
    )
    def get_machine(mac: str) -> _models.Machine:
        normalised = _normalise_mac(mac)
        with _db.open_db(state_path) as conn:
            row = conn.execute("SELECT * FROM machines WHERE mac = ?", (normalised,)).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no machine record for {normalised}",
            )
        return _row_to_machine(row)

    @app.put(
        "/machines/{mac}",
        response_model=_models.Machine,
        dependencies=[Depends(require_token)],
    )
    def upsert_machine(mac: str, body: _models.MachineUpsert) -> _models.Machine:
        normalised = _normalise_mac(mac)
        now = _now_iso()
        with _db.open_db(state_path) as conn:
            existing = conn.execute(
                "SELECT created_at FROM machines WHERE mac = ?", (normalised,)
            ).fetchone()
            created_at = existing["created_at"] if existing is not None else now
            conn.execute(
                """
                INSERT INTO machines
                    (mac, image, provisioning_mode, hostname,
                     cijoe_workflow_ref, last_known_good, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(mac) DO UPDATE SET
                    image              = excluded.image,
                    provisioning_mode  = excluded.provisioning_mode,
                    hostname           = excluded.hostname,
                    cijoe_workflow_ref = excluded.cijoe_workflow_ref,
                    updated_at         = excluded.updated_at
                """,
                (
                    normalised,
                    body.image,
                    body.provisioning_mode,
                    body.hostname,
                    body.cijoe_workflow_ref,
                    created_at,
                    now,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM machines WHERE mac = ?", (normalised,)).fetchone()
        assert row is not None
        return _row_to_machine(row)

    @app.delete(
        "/machines/{mac}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_token)],
    )
    def delete_machine(mac: str) -> Response:
        normalised = _normalise_mac(mac)
        with _db.open_db(state_path) as conn:
            cur = conn.execute("DELETE FROM machines WHERE mac = ?", (normalised,))
            conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no machine record for {normalised}",
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get(
        "/images",
        response_model=list[_models.ImageEntry],
        dependencies=[Depends(require_token)],
    )
    def list_images() -> list[_models.ImageEntry]:
        out: list[_models.ImageEntry] = []
        for img in images.list_images(resolved_image_root):
            out.append(
                _models.ImageEntry(
                    name=img.name,
                    path=str(img.path),
                    format=img.format or "",
                    size_bytes=img.size_bytes,
                )
            )
        return out

    return app


# ---------- helpers -----------------------------------------------------------


def _normalise_mac(raw: str) -> str:
    """Return a canonical lower-case ``aa:bb:cc:dd:ee:ff`` MAC, or 400."""
    cleaned = raw.lower().replace("-", ":")
    parts = cleaned.split(":")
    if len(parts) != 6 or any(
        len(p) != 2 or any(c not in "0123456789abcdef" for c in p) for p in parts
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid MAC: {raw!r}",
        )
    return cleaned


def _row_to_machine(row: object) -> _models.Machine:
    """Decode a sqlite3.Row into a ``_models.Machine``."""
    raw_known_good = row["last_known_good"]  # type: ignore[index]
    last_known_good: dict[str, object] | None = None
    if raw_known_good:
        try:
            decoded = json.loads(raw_known_good)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            last_known_good = decoded
    return _models.Machine(
        mac=row["mac"],  # type: ignore[index]
        image=row["image"],  # type: ignore[index]
        provisioning_mode=row["provisioning_mode"],  # type: ignore[index]
        hostname=row["hostname"],  # type: ignore[index]
        cijoe_workflow_ref=row["cijoe_workflow_ref"],  # type: ignore[index]
        last_known_good=last_known_good,
        created_at=datetime.fromisoformat(row["created_at"]),  # type: ignore[index]
        updated_at=datetime.fromisoformat(row["updated_at"]),  # type: ignore[index]
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
