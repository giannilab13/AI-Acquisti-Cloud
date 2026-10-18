import os, json, zipfile, hashlib, shutil, base64
from pathlib import Path
from datetime import datetime, date, time, timezone
from decimal import Decimal

import boto3
from botocore.config import Config
import psycopg
from psycopg import sql
from psycopg.rows import dict_row

ROOT = Path("/tmp/aiacquisti_backup")
ZIP = Path("/tmp/AI-Acquisti-C3.0-COMPLETO.zip")

def encode_value(v):
    if isinstance(v, Decimal):
        return {"__type__": "decimal", "value": str(v)}
    if isinstance(v, (datetime, date, time)):
        return {"__type__": type(v).__name__, "value": v.isoformat()}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"__type__": "bytes", "base64": base64.b64encode(bytes(v)).decode("ascii")}
    return v

def dump_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=encode_value), encoding="utf-8")

shutil.rmtree(ROOT, ignore_errors=True)
(ROOT / "database").mkdir(parents=True, exist_ok=True)
(ROOT / "documents").mkdir(parents=True, exist_ok=True)
(ROOT / "source").mkdir(parents=True, exist_ok=True)

# 1) Copy frozen source tree from this deployment.
src = Path("/app")
for p in src.rglob("*"):
    rel = p.relative_to(src)
    if any(part in {".venv", ".git", "__pycache__", "data"} for part in rel.parts):
        continue
    if p.is_file():
        dst = ROOT / "source" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)

# 2) Export every PostgreSQL table from public + company schemas.
conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
conn.autocommit = True
with conn.cursor() as cur:
    cur.execute("""
        SELECT n.nspname AS schema_name, c.relname AS table_name
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r','p')
          AND n.nspname NOT IN ('pg_catalog','information_schema')
          AND n.nspname NOT LIKE 'pg_toast%'
        ORDER BY n.nspname, c.relname
    """)
    tables = [(r["schema_name"], r["table_name"]) for r in cur.fetchall()]

db_manifest = []
for schema_name, table_name in tables:
    out_dir = ROOT / "database" / schema_name
    out_dir.mkdir(parents=True, exist_ok=True)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT column_name, data_type, udt_name, is_nullable, column_default,
                   is_identity, identity_generation, ordinal_position
            FROM information_schema.columns
            WHERE table_schema=%s AND table_name=%s
            ORDER BY ordinal_position
        """, (schema_name, table_name))
        columns = [dict(r) for r in cur.fetchall()]
        cur.execute(
            sql.SQL("SELECT * FROM {}.{}").format(
                sql.Identifier(schema_name), sql.Identifier(table_name)
            )
        )
        rows = cur.fetchall()

    with (out_dir / f"{table_name}.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(dict(row), ensure_ascii=False, default=encode_value) + "\n")

    db_manifest.append({
        "schema": schema_name,
        "table": table_name,
        "rows": len(rows),
        "columns": columns,
    })

with conn.cursor() as cur:
    cur.execute("""
        SELECT schemaname, sequencename, start_value, min_value, max_value,
               increment_by, cycle, cache_size, last_value
        FROM pg_sequences
        WHERE schemaname NOT IN ('pg_catalog','information_schema')
        ORDER BY schemaname, sequencename
    """)
    sequences = [dict(r) for r in cur.fetchall()]
conn.close()

dump_json(ROOT / "database" / "manifest.json", {
    "exported_at_utc": datetime.now(timezone.utc).isoformat(),
    "tables": db_manifest,
    "sequences": sequences,
})

# 3) Copy every original uploaded document from the S3-compatible bucket.
cfg = Config(signature_version="s3v4", s3={"addressing_style": "virtual"})
s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["ENDPOINT"],
    aws_access_key_id=os.environ["ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["SECRET_ACCESS_KEY"],
    region_name=os.environ.get("REGION") or "auto",
    config=cfg,
)
bucket = os.environ["BUCKET"]
objects = []
token = None
while True:
    args = {"Bucket": bucket}
    if token:
        args["ContinuationToken"] = token
    page = s3.list_objects_v2(**args)

    for item in page.get("Contents", []):
        key = item["Key"]
        if key.startswith("backups/"):
            continue
        data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        dst = ROOT / "documents" / key
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        objects.append({
            "key": key,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })

    if not page.get("IsTruncated"):
        break
    token = page.get("NextContinuationToken")

dump_json(ROOT / "documents" / "manifest.json", {
    "objects": objects,
    "count": len(objects),
    "total_bytes": sum(x["size"] for x in objects),
})

# 4) Add reconstruction notes and configuration inventory.
(ROOT / "LEGGIMI_RIPRISTINO.txt").write_text(
    """AI Acquisti C3.0 - Backup completo\n\n"""
    """Contenuto:\n"""
    """- source/: codice sorgente congelato C3.0\n"""
    """- database/: tutte le tabelle PostgreSQL esportate in JSONL + metadati\n"""
    """- documents/: copia degli originali presenti nel bucket\n"""
    """- CONFIGURAZIONE.txt: nomi delle variabili richieste\n"""
    """- SHA256SUMS.txt: checksum dei file del backup\n\n"""
    """Per un ripristino su un nuovo ambiente occorrono nuove credenziali PostgreSQL, bucket S3 e OpenAI.\n"""
    """Le credenziali segrete non vengono scritte dentro questo archivio.\n""",
    encoding="utf-8",
)

(ROOT / "CONFIGURAZIONE.txt").write_text(
    "\n".join([
        "DATABASE_URL",
        "ENDPOINT",
        "BUCKET",
        "REGION",
        "ACCESS_KEY_ID",
        "SECRET_ACCESS_KEY",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
    ]) + "\n",
    encoding="utf-8",
)

checks = []
for p in sorted(ROOT.rglob("*")):
    if p.is_file():
        checks.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(ROOT).as_posix()}")
(ROOT / "SHA256SUMS.txt").write_text("\n".join(checks) + "\n", encoding="utf-8")

if ZIP.exists():
    ZIP.unlink()
with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
    for p in ROOT.rglob("*"):
        if p.is_file():
            zf.write(p, p.relative_to(ROOT))

backup_key = "backups/AI-Acquisti-C3.0-COMPLETO.zip"
s3.upload_file(str(ZIP), bucket, backup_key)

print("BACKUP_READY")
print("BUCKET_KEY=" + backup_key)
print("ZIP_BYTES=" + str(ZIP.stat().st_size))
print("ZIP_SHA256=" + hashlib.sha256(ZIP.read_bytes()).hexdigest())
print("DB_TABLES=" + str(len(tables)))
print("DOCUMENTS=" + str(len(objects)))
