import os
import uuid
import hashlib
from datetime import datetime, timezone
from typing import Optional

import httpx
import psycopg
from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

DATABASE_URL = os.environ["DATABASE_URL"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "releases")
ADMIN_API_KEY = os.environ["ADMIN_API_KEY"]
CLIENT_API_KEY = os.environ["CLIENT_API_KEY"]

PRODUCTS = {
    "fivem": "FiveM",
    "roblox": "Roblox",
    "fortnite": "Fortnite",
    "cs2": "CS2",
    "mega-chameleon": "Mega Chameleon",
    "war-dogs": "War Dogs",
}

app = FastAPI(title="Multiloader Release API", version="1.0.0")


def db():
    return psycopg.connect(DATABASE_URL, connect_timeout=10)


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


@app.on_event("startup")
def startup():
    init_db()


def require_key(received: Optional[str], expected: str):
    if not received or received != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def product_name(product_id: str):
    if product_id not in PRODUCTS:
        raise HTTPException(status_code=404, detail="Unknown product category")
    return PRODUCTS[product_id]


def storage_headers():
    return {
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/v1/products")
def list_products(x_client_key: Optional[str] = Header(default=None)):
    require_key(x_client_key, CLIENT_API_KEY)
    return [{"id": key, "name": value} for key, value in PRODUCTS.items()]


@app.get("/api/v1/products/{product_id}/latest")
def latest_release(product_id: str, x_client_key: Optional[str] = Header(default=None)):
    require_key(x_client_key, CLIENT_API_KEY)
    product_name(product_id)
    with db() as conn:
        row = conn.execute("""
            SELECT id, product_id, version, filename, sha256, size_bytes, created_at
            FROM releases WHERE product_id = %s
            ORDER BY created_at DESC LIMIT 1
        """, (product_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No release published")
    return {
        "id": str(row[0]), "product_id": row[1], "version": row[2],
        "filename": row[3], "sha256": row[4], "size_bytes": row[5],
        "created_at": row[6].isoformat()
    }


@app.post("/api/v1/admin/products/{product_id}/releases")
async def upload_release(
    product_id: str,
    version: str = Form(...),
    file: UploadFile = File(...),
    x_admin_key: Optional[str] = Header(default=None),
):
    require_key(x_admin_key, ADMIN_API_KEY)
    product_name(product_id)

    if not version.strip() or len(version) > 40:
        raise HTTPException(status_code=400, detail="Invalid version")
    filename = os.path.basename(file.filename or "release.bin")
    if not filename or len(filename) > 180:
        raise HTTPException(status_code=400, detail="Invalid filename")

    # For a simple starter, upload is buffered in memory. Keep individual builds
    # modest in size; use resumable/direct-to-storage uploads for large files.
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(content) > 100 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Max upload size is 100 MB")

    digest = hashlib.sha256(content).hexdigest()
    release_id = uuid.uuid4()
    object_path = f"{product_id}/{version}/{release_id}-{filename}"
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{object_path}"

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            upload_url,
            headers={**storage_headers(), "Content-Type": file.content_type or "application/octet-stream", "x-upsert": "false"},
            content=content,
        )
    if response.status_code >= 300:
        raise HTTPException(status_code=502, detail=f"Storage upload failed: {response.text[:300]}")

    try:
        with db() as conn:
            conn.execute("""
                INSERT INTO releases (id, product_id, version, filename, object_path, sha256, size_bytes)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (release_id, product_id, version, filename, object_path, digest, len(content)))
            conn.commit()
    except Exception as exc:
        # Avoid exposing database internals. Uploaded orphan object can be removed from storage manually.
        raise HTTPException(status_code=409, detail="Could not save release metadata; version may already exist") from exc

    return {
        "id": str(release_id), "product_id": product_id, "version": version,
        "filename": filename, "sha256": digest, "size_bytes": len(content)
    }


@app.get("/api/v1/releases/{release_id}/download")
async def download_release(
    release_id: str,
    x_client_key: Optional[str] = Header(default=None),
):
    require_key(x_client_key, CLIENT_API_KEY)
    with db() as conn:
        row = conn.execute("""
            SELECT filename, object_path FROM releases WHERE id = %s
        """, (release_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Release not found")

    filename, object_path = row
    download_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{object_path}"
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.get(download_url, headers=storage_headers())
    if response.status_code >= 300:
        raise HTTPException(status_code=502, detail="Storage download failed")

    return StreamingResponse(
        iter([response.content]),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )
