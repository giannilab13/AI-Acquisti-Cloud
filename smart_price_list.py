import hashlib
import re
from pathlib import Path

from db import get_connection, next_text_id, normalize_basic
from spreadsheet_importer import (
    _detect_columns,
    _find_header_row,
    _read_csv,
    _read_xlsx,
    import_price_list_file,
)


_NAME_KEYS = {
    "fornitore", "nomefornitore", "ragionesociale", "supplier", "suppliername",
    "vendor", "vendorname",
}
_VAT_KEYS = {
    "piva", "partitaiva", "partitaivafornitore", "vat", "vatnumber", "vatid",
}


def _label_key(value):
    text = str(value or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def _clean_vat(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _rows_for_file(file_name, file_bytes):
    extension = Path(file_name).suffix.lower()
    if extension == ".csv":
        return _read_csv(file_bytes)
    if extension == ".xlsx":
        return _read_xlsx(file_bytes)
    raise ValueError("Formato listino non supportato. Usa CSV oppure XLSX.")


def detect_supplier_metadata(file_name, file_bytes):
    """Legge solo dati espliciti del fornitore presenti nel listino."""
    rows = _rows_for_file(file_name, file_bytes)
    if not rows:
        raise ValueError("Il file è vuoto.")

    # Valida anche che il file contenga una vera tabella prodotti.
    header_index = _find_header_row(rows)
    headers = rows[header_index]
    _detect_columns(headers)

    supplier_name = ""
    vat_number = ""

    # 1) Colonne dedicate nella tabella prodotti.
    header_keys = [_label_key(h) for h in headers]
    name_col = next((i for i, key in enumerate(header_keys) if key in _NAME_KEYS), None)
    vat_col = next((i for i, key in enumerate(header_keys) if key in _VAT_KEYS), None)

    if name_col is not None or vat_col is not None:
        for row in rows[header_index + 1 : header_index + 25]:
            if name_col is not None and not supplier_name and name_col < len(row):
                supplier_name = str(row[name_col] or "").strip()
            if vat_col is not None and not vat_number and vat_col < len(row):
                vat_number = str(row[vat_col] or "").strip()
            if supplier_name and (vat_number or vat_col is None):
                break

    # 2) Metadati sopra la tabella, ad esempio "Fornitore | Alfa S.r.l."
    #    oppure "Fornitore: Alfa S.r.l.".
    scan_limit = max(header_index + 1, 15)
    for row in rows[:scan_limit]:
        values = list(row)
        for i, cell in enumerate(values):
            raw = str(cell or "").strip()
            if not raw:
                continue
            key = _label_key(raw)
            next_value = str(values[i + 1] or "").strip() if i + 1 < len(values) else ""

            if not supplier_name and key in _NAME_KEYS and next_value:
                supplier_name = next_value
            if not vat_number and key in _VAT_KEYS and next_value:
                vat_number = next_value

            if not supplier_name:
                match = re.match(
                    r"^\s*(?:fornitore|nome\s+fornitore|ragione\s+sociale|supplier|vendor)\s*[:\-]\s*(.+?)\s*$",
                    raw,
                    flags=re.I,
                )
                if match:
                    supplier_name = match.group(1).strip()

            if not vat_number:
                match = re.match(
                    r"^\s*(?:p\.?\s*iva|partita\s+iva|vat(?:\s+number|\s+id)?)\s*[:\-]\s*(.+?)\s*$",
                    raw,
                    flags=re.I,
                )
                if match:
                    vat_number = match.group(1).strip()

    return {
        "supplier_name": supplier_name,
        "vat_number": _clean_vat(vat_number),
        "header_row": header_index + 1,
    }


def _find_existing_supplier(conn, supplier_name="", vat_number=""):
    suppliers = conn.execute(
        "SELECT id,name,vat_number,category FROM suppliers ORDER BY name"
    ).fetchall()

    clean_vat = _clean_vat(vat_number)
    if clean_vat:
        for row in suppliers:
            if _clean_vat(row["vat_number"]) == clean_vat:
                return row

    normalized_name = normalize_basic(supplier_name)
    if normalized_name:
        for row in suppliers:
            if normalize_basic(row["name"]) == normalized_name:
                return row

    return None


def _resolve_supplier(metadata, selected_supplier_id=None):
    conn = get_connection()
    try:
        if selected_supplier_id:
            row = conn.execute(
                "SELECT id,name,vat_number,category FROM suppliers WHERE id=?",
                (selected_supplier_id,),
            ).fetchone()
            if not row:
                raise ValueError("Fornitore selezionato non valido.")

            # Se il file porta una P.IVA e l'anagrafica scelta non la possiede,
            # la completiamo senza sovrascrivere dati esistenti.
            detected_vat = metadata.get("vat_number") or ""
            if detected_vat and not row["vat_number"]:
                conn.execute(
                    "UPDATE suppliers SET vat_number=? WHERE id=?",
                    (detected_vat, row["id"]),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT id,name,vat_number,category FROM suppliers WHERE id=?",
                    (selected_supplier_id,),
                ).fetchone()
            return dict(row), "selected", False

        supplier_name = str(metadata.get("supplier_name") or "").strip()
        vat_number = _clean_vat(metadata.get("vat_number"))

        existing = _find_existing_supplier(conn, supplier_name, vat_number)
        if existing:
            if vat_number and not existing["vat_number"]:
                conn.execute(
                    "UPDATE suppliers SET vat_number=? WHERE id=?",
                    (vat_number, existing["id"]),
                )
                conn.commit()
                existing = conn.execute(
                    "SELECT id,name,vat_number,category FROM suppliers WHERE id=?",
                    (existing["id"],),
                ).fetchone()
            return dict(existing), "detected_existing", False

        if not supplier_name:
            return None, "missing", False

        supplier_id = next_text_id(conn, "suppliers", "S")
        conn.execute(
            """
            INSERT INTO suppliers(id,name,vat_number,category)
            VALUES(?,?,?,?)
            """,
            (supplier_id, supplier_name, vat_number or None, None),
        )
        conn.commit()
        created = conn.execute(
            "SELECT id,name,vat_number,category FROM suppliers WHERE id=?",
            (supplier_id,),
        ).fetchone()
        return dict(created), "detected_created", True
    finally:
        conn.close()


def smart_import_price_list(file_name, file_bytes, selected_supplier_id=None):
    file_hash = hashlib.sha256(file_bytes).hexdigest()
    conn = get_connection()
    try:
        duplicate = conn.execute(
            """
            SELECT pl.id,s.name AS supplier
            FROM price_lists pl
            JOIN suppliers s ON s.id=pl.supplier_id
            WHERE pl.file_hash=?
            """,
            (file_hash,),
        ).fetchone()
    finally:
        conn.close()

    if duplicate:
        return {
            "ok": False,
            "duplicate": True,
            "message": f"Listino già importato (ID {duplicate['id']}, {duplicate['supplier']}).",
        }

    metadata = detect_supplier_metadata(file_name, file_bytes)
    supplier, source, created = _resolve_supplier(metadata, selected_supplier_id)

    if not supplier:
        return {
            "ok": False,
            "needs_supplier": True,
            "duplicate": False,
            "message": (
                "Il file non contiene dati sufficienti per identificare il fornitore. "
                "Seleziona il fornitore dall'elenco oppure creane uno e ripeti l'importazione."
            ),
            "detected_supplier": metadata,
        }

    result = import_price_list_file(
        file_name=file_name,
        file_bytes=file_bytes,
        supplier_id=supplier["id"],
    )
    result["supplier_resolution"] = source
    result["supplier_created_automatically"] = created
    result["detected_supplier"] = metadata
    result["supplier_id"] = supplier["id"]
    result["supplier_vat_number"] = supplier.get("vat_number")
    return result
