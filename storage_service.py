import os
import re
from pathlib import Path

import boto3
from botocore.config import Config

from db import BASE_DIR, get_active_company_id


LOCAL_UPLOAD_ROOT = BASE_DIR / "data" / "uploads"


def _safe_filename(filename: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", filename or "documento")


def _s3_config():
    bucket = (os.getenv("BUCKET") or "").strip()
    access_key = (os.getenv("ACCESS_KEY_ID") or "").strip()
    secret_key = (os.getenv("SECRET_ACCESS_KEY") or "").strip()
    endpoint = (os.getenv("ENDPOINT") or "").strip()
    region = (os.getenv("REGION") or "auto").strip() or "auto"

    if all((bucket, access_key, secret_key, endpoint)):
        return {
            "bucket": bucket,
            "access_key": access_key,
            "secret_key": secret_key,
            "endpoint": endpoint,
            "region": region,
        }
    return None


def cloud_storage_enabled() -> bool:
    return _s3_config() is not None


def _client(cfg):
    return boto3.client(
        "s3",
        endpoint_url=cfg["endpoint"],
        aws_access_key_id=cfg["access_key"],
        aws_secret_access_key=cfg["secret_key"],
        region_name=cfg["region"],
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


def store_document_bytes(filename: str, file_hash: str, content: bytes) -> str:
    """Preserva il documento originale.

    In Railway usa il bucket S3-compatible. In locale mantiene il fallback su
    data/uploads, così la stessa build può essere testata senza cloud storage.
    Restituisce un riferimento persistente salvabile in documents.stored_path.
    """
    company_id = get_active_company_id()
    safe_name = _safe_filename(filename)
    object_key = f"company_{company_id:06d}/documents/{file_hash[:12]}_{safe_name}"

    cfg = _s3_config()
    if cfg:
        _client(cfg).put_object(
            Bucket=cfg["bucket"],
            Key=object_key,
            Body=content,
            ContentType="application/octet-stream",
        )
        return f"s3://{cfg['bucket']}/{object_key}"

    target_dir = LOCAL_UPLOAD_ROOT / f"company_{company_id:06d}"
    target_dir.mkdir(parents=True, exist_ok=True)
    stored_path = target_dir / f"{file_hash[:12]}_{safe_name}"
    stored_path.write_bytes(content)
    return str(stored_path)
