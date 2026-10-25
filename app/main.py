
import os
import uuid
import hashlib
import logging
from datetime import datetime
from typing import Optional

import httpx
import psycopg
from fastapi import (
    FastAPI,
    UploadFile,
    File,
    Form,
    Header,
    HTTPException,
)
from fastapi.responses import StreamingResponse
from psycopg.rows import dict_row

# --------------------------------------------------
# Logging: Fehler werden in Render Logs ausgegeben
# --------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("multiloader-api")

app = FastAPI(
    title="Multiloader Release API",
    version="1.0.1",
)


# --------------------------------------------------
# Konfiguration
# --------------------------------------------------

def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise HTTPException(
            status_code=503,
            detail=f"Server configuration missing: {name}",
        )
    return value


def database_url() -> str:
    return required_env("DATABASE_URL")


def supabase_url() -> str:
    return required_env("SUPABASE_URL").rstrip("/")


def supabase_key() -> str:
    return required_env("SUPABASE_SERVICE_ROLE_KEY")


def supabase_bucket() -> str:
    return os.getenv("SUPABASE_BUCKET", "releases").strip()


PRODUCTS = {
    "fivem": "FiveM",
    "roblox": "Roblox",
    "fortnite": "Fortnite",
    "cs2": "CS2",
    "mega-chameleon": "Mega Chameleon",
    "war-dogs": "War Dogs",
    "rust": "Rust",
    "gta5-enhanced": "GTA 5 Enhanced",
}


# --------------------------------------------------
# Datenbank
# --------------------------------------------------

def db():
    try:
        return psycopg.connect(
            database_url(),
            connect_timeout=10,
            row_factory=dict_row,
        )
    except Exception:
        logger.exception("Datenbankverbindung fehlgeschlagen")
        raise


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS releases (
                id UUID PRIMARY KEY,
                product_id TEXT NOT NULL,
                version TEXT NOT NULL,
                filename TEXT NOT NULL,
                object_path TEXT NOT NULL UNIQUE,
                sha256 TEXT NOT NULL,
                size_bytes BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(product_id, version)
            )
        """)
        conn.commit()


# Die API soll auch dann starten, wenn die Datenbank
# vorübergehend nicht erreichbar ist. Der Fehler wird
# im Render-Log angezeigt und DB-Endpunkte melden 503.
@app.on_event("startup")
def startup():
    logger.info("API startet")
    try:
        init_db()
        logger.info("Datenbank initialisiert")
    except Exception:
        logger.exception(
            "Datenbankinitialisierung fehlgeschlagen. "
            "Prüfe DATABASE_URL und Supabase-Verbindung."
        )


# --------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------

def require_key(received: Optional[str], env_name: str):
    expected = required_env(env_name)
    if not received or received != expected:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key",
        )


def product_name(product_id: str) -> str:
    if product_id not in PRODUCTS:
        raise HTTPException(
            status_code=404,
            detail="Unknown product category",
        )
    return PRODUCTS[product_id]


def storage_headers():
    key = supabase_key()
    return {
        "Authorization": f"Bearer {key}",
        "apikey": key,
    }


def db_error():
    logger.exception("Datenbankoperation fehlgeschlagen")
    raise HTTPException(
        status_code=503,
        detail="Database temporarily unavailable. Check server logs.",
    )


# --------------------------------------------------
# Health Check
# --------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {
        "name": "Multiloader Release API",
        "status": "running",
        "docs": "/docs",
    }


# --------------------------------------------------
# Produkte
# --------------------------------------------------

@app.get("/api/v1/products")
def list_products(
    x_client_key: Optional[str] = Header(default=None),
):
    require_key(x_client_key, "CLIENT_API_KEY")
    return [
        {"id": key, "name": value}
        for key, value in PRODUCTS.items()
    ]


# --------------------------------------------------
# Neueste Version
# --------------------------------------------------

@app.get("/api/v1/products/{product_id}/latest")
def latest_release(
    product_id: str,
    x_client_key: Optional[str] = Header(default=None),
):
    require_key(x_client_key, "CLIENT_API_KEY")
    product_name(product_id)

    try:
        with db() as conn:
            row = conn.execute(
                """
                SELECT id, product_id, version, filename,
                       sha256, size_bytes, created_at
                FROM releases
                WHERE product_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (product_id,),
            ).fetchone()
    except HTTPException:
        raise
    except Exception:
        db_error()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="No release published",
        )

    return {
        "id": str(row["id"]),
        "product_id": row["product_id"],
        "version": row["version"],
        "filename": row["filename"],
        "sha256": row["sha256"],
        "size_bytes": row["size_bytes"],
        "created_at": row["created_at"].isoformat(),
    }


# --------------------------------------------------
# Upload einer neuen Version
# --------------------------------------------------

@app.post("/api/v1/admin/products/{product_id}/releases")
async def upload_release(
    product_id: str,
    version: str = Form(...),
    file: UploadFile = File(...),
    x_admin_key: Optional[str] = Header(default=None),
):
    require_key(x_admin_key, "ADMIN_API_KEY")
    product_name(product_id)

    version = version.strip()
    if not version or len(version) > 40:
        raise HTTPException(
            status_code=400,
            detail="Invalid version",
        )

    filename = os.path.basename(file.filename or "release.bin")
    if not filename or len(filename) > 180:
        raise HTTPException(
            status_code=400,
            detail="Invalid filename",
        )

    # Größenlimit: 100 MB
    content = await file.read(100 * 1024 * 1024 + 1)
    if not content:
        raise HTTPException(
            status_code=400,
            detail="Empty file",
        )
    if len(content) > 100 * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail="Maximum upload size is 100 MB",
        )

    digest = hashlib.sha256(content).hexdigest()
    release_id = uuid.uuid4()
    object_path = f"{product_id}/{version}/{release_id}-{filename}"

    upload_url = (
        f"{supabase_url()}/storage/v1/object/"
        f"{supabase_bucket()}/{object_path}"
    )

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                upload_url,
                headers={
                    **storage_headers(),
                    "Content-Type": (
                        file.content_type
                        or "application/octet-stream"
                    ),
                    "x-upsert": "false",
                },
                content=content,
            )

        if response.status_code >= 300:
            logger.error(
                "Supabase Upload fehlgeschlagen: HTTP %s",
                response.status_code,
            )
            raise HTTPException(
                status_code=502,
                detail="Storage upload failed. Check bucket and server credentials.",
            )

    except HTTPException:
        raise
    except Exception:
        logger.exception("Supabase Upload fehlgeschlagen")
        raise HTTPException(
            status_code=502,
            detail="Could not connect to file storage",
        )

    try:
        with db() as conn:
            conn.execute(
                """
                INSERT INTO releases
                    (id, product_id, version, filename,
                     object_path, sha256, size_bytes)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    release_id,
                    product_id,
                    version,
                    filename,
                    object_path,
                    digest,
                    len(content),
                ),
            )
            conn.commit()

    except Exception:
        logger.exception("Release-Metadaten konnten nicht gespeichert werden")
        raise HTTPException(
            status_code=409,
            detail=(
                "Could not save release metadata. "
                "The version may already exist or the database may be unavailable."
            ),
        )

    return {
        "id": str(release_id),
        "product_id": product_id,
        "version": version,
        "filename": filename,
        "sha256": digest,
        "size_bytes": len(content),
    }


# --------------------------------------------------
# Download einer veröffentlichten Version
# --------------------------------------------------

@app.get("/api/v1/releases/{release_id}/download")
async def download_release(
    release_id: uuid.UUID,
    x_client_key: Optional[str] = Header(default=None),
):
    require_key(x_client_key, "CLIENT_API_KEY")

    try:
        with db() as conn:
            row = conn.execute(
                """
                SELECT filename, object_path
                FROM releases
                WHERE id = %s
                """,
                (release_id,),
            ).fetchone()
    except Exception:
        db_error()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="Release not found",
        )

    filename = row["filename"]
    object_path = row["object_path"]
    download_url = (
        f"{supabase_url()}/storage/v1/object/"
        f"{supabase_bucket()}/{object_path}"
    )

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.get(
                download_url,
                headers=storage_headers(),
            )

        if response.status_code >= 300:
            logger.error(
                "Supabase Download fehlgeschlagen: HTTP %s",
                response.status_code,
            )
            raise HTTPException(
                status_code=502,
                detail="Storage download failed",
            )

    except HTTPException:
        raise
    except Exception:
        logger.exception("Download aus Supabase fehlgeschlagen")
        raise HTTPException(
            status_code=502,
            detail="Could not connect to file storage",
        )

    return StreamingResponse(
        iter([response.content]),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )
