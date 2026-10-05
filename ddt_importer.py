import base64
import hashlib
import json
import os
import re
from io import BytesIO
from pathlib import Path

from openai import OpenAI

from db import get_connection
from storage_service import store_document_bytes
from xml_importer import ensure_product, ensure_supplier


MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
SUPPORTED_DDT_SUFFIXES = {".jpg", ".jpeg", ".png", ".pdf"}


DDT_EXTRACTION_INSTRUCTIONS = """Sei un estrattore dati per documenti di trasporto (DDT) italiani.
Leggi ESCLUSIVAMENTE il DDT fornito dall'utente e restituisci SOLO JSON valido, senza markdown.

Schema obbligatorio:
{
  "supplier": {"name": "", "vat": ""},
  "ddt_number": "",
  "ddt_date": "YYYY-MM-DD",
  "lines": [
    {
      "line_number": 1,
      "supplier_code": "",
      "description": "",
      "quantity": 0.0,
      "unit": "pz"
    }
  ],
  "uncertain_fields": []
}

REGOLE:
- Non inventare mai dati mancanti o poco leggibili: usa stringa vuota o 0 e indica il campo in uncertain_fields.
- Il documento deve essere un DDT/documento di trasporto o documento di consegna equivalente.
- Estrai numero e data del DDT, non il numero di un eventuale ordine cliente.
- Per ogni riga merce estrai codice articolo se presente, descrizione e quantita consegnata.
- Il prezzo non e richiesto e non deve essere inventato se assente.
- Non inserire trasporto, note, colli o testi descrittivi come righe prodotto, salvo che siano chiaramente articoli consegnati.
- Se una quantita non e leggibile usa 0 e segnala il campo come incerto.
"""


def normalize_ddt_number(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _num(value, default=0.0):
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "")
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", ".")
    try:
        return float(text)
    except Exception:
        return default


def _extract_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise ValueError("L'AI non ha restituito dati JSON validi per il DDT.")


def _normalise_ddt(data):
    if not isinstance(data, dict):
        raise ValueError("Il DDT non contiene dati utilizzabili.")

    supplier = data.get("supplier") or {}
    data["supplier"] = {
        "name": str(supplier.get("name") or "").strip(),
        "vat": str(supplier.get("vat") or "").replace(" ", "").strip(),
    }
    data["ddt_number"] = str(data.get("ddt_number") or "").strip()
    data["ddt_date"] = str(data.get("ddt_date") or "").strip()
    data["uncertain_fields"] = [
        str(x).strip() for x in (data.get("uncertain_fields") or []) if str(x).strip()
    ]

    normalised_lines = []
    for idx, raw in enumerate(data.get("lines") or [], start=1):
        if not isinstance(raw, dict):
            continue
        description = str(raw.get("description") or "").strip()
        code = str(raw.get("supplier_code") or "").strip()
        quantity = _num(raw.get("quantity"), 0.0)
        if not description and not code:
            continue
        normalised_lines.append({
            "line_number": int(_num(raw.get("line_number"), idx) or idx),
            "supplier_code": code,
            "description": description,
            "quantity": quantity,
            "unit": str(raw.get("unit") or "pz").strip() or "pz",
        })
    data["lines"] = normalised_lines
    return data


def _quality_reasons(parsed):
    reasons = []
    if not parsed["supplier"]["name"] and not parsed["supplier"]["vat"]:
        reasons.append("Fornitore non leggibile")
    if not parsed["ddt_number"]:
        reasons.append("Numero DDT non leggibile")
    if not parsed["ddt_date"]:
        reasons.append("Data DDT non leggibile")
    if not parsed["lines"]:
        reasons.append("Nessuna riga prodotto leggibile")
    for line in parsed["lines"]:
        if float(line["quantity"] or 0) <= 0:
            reasons.append(f"Quantita riga {line['line_number']} da verificare")
    reasons.extend(parsed.get("uncertain_fields") or [])
    return list(dict.fromkeys(reasons))


def parse_ddt_with_ai(file_name, file_bytes):
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY non configurata nel file .env.")

    suffix = Path(file_name).suffix.lower()
    if suffix not in SUPPORTED_DDT_SUFFIXES:
        raise ValueError("Formato DDT non supportato. Usa JPG, PNG oppure PDF.")

    client = OpenAI(api_key=api_key)
    uploaded = None
    try:
        if suffix == ".pdf":
            buffer = BytesIO(file_bytes)
            buffer.name = file_name or "ddt.pdf"
            uploaded = client.files.create(file=buffer, purpose="user_data")
            content = [
                {"type": "input_file", "file_id": uploaded.id},
                {"type": "input_text", "text": "Leggi questo DDT e restituisci i dati richiesti."},
            ]
        else:
            mime = "image/png" if suffix == ".png" else "image/jpeg"
            encoded = base64.b64encode(file_bytes).decode("ascii")
            content = [
                {"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"},
                {"type": "input_text", "text": "Leggi la foto di questo DDT e restituisci i dati richiesti."},
            ]

        response = client.responses.create(
            model=MODEL,
            instructions=DDT_EXTRACTION_INSTRUCTIONS,
            input=[{"role": "user", "content": content}],
        )
        return _normalise_ddt(_extract_json(response.output_text))
    finally:
        if uploaded is not None:
            try:
                client.files.delete(uploaded.id)
            except Exception:
                pass


def store_invoice_ddt_references(invoice_document_id, supplier_id, references, conn=None):
    own_conn = conn is None
    conn = conn or get_connection()
    try:
        for ref in references or []:
            number = str((ref or {}).get("number") or "").strip()
            date = str((ref or {}).get("date") or "").strip()
            number_norm = normalize_ddt_number(number)
            if not number_norm:
                continue
            existing = conn.execute(
                """
                SELECT id FROM invoice_ddt_references
                WHERE invoice_document_id=? AND reference_number_norm=?
                  AND COALESCE(reference_date,'')=COALESCE(?,'')
                """,
                (invoice_document_id, number_norm, date or None),
            ).fetchone()
            if not existing:
                conn.execute(
                    """
                    INSERT INTO invoice_ddt_references(
                        invoice_document_id,reference_number,reference_number_norm,reference_date,status
                    ) VALUES(?,?,?,?, 'NON_TROVATO')
                    """,
                    (invoice_document_id, number, number_norm, date or None),
                )

        _link_invoice_references(conn, invoice_document_id, supplier_id)
        _rebuild_invoice_ddt_checks(conn, invoice_document_id)
        if own_conn:
            conn.commit()
    finally:
        if own_conn:
            conn.close()


def _link_invoice_references(conn, invoice_document_id, supplier_id):
    refs = conn.execute(
        """
        SELECT * FROM invoice_ddt_references
        WHERE invoice_document_id=?
        ORDER BY id
        """,
        (invoice_document_id,),
    ).fetchall()
    for ref in refs:
        params = [supplier_id, ref["reference_number_norm"]]
        date_clause = ""
        if ref["reference_date"]:
            date_clause = " AND (ddt_date=? OR ddt_date IS NULL OR ddt_date='')"
            params.append(ref["reference_date"])
        note = conn.execute(
            f"""
            SELECT id FROM delivery_notes
            WHERE supplier_id=? AND ddt_number_norm=? {date_clause}
            ORDER BY id DESC LIMIT 1
            """,
            tuple(params),
        ).fetchone()
        if note:
            conn.execute(
                """
                UPDATE invoice_ddt_references
                SET delivery_note_id=?, status='COLLEGATO'
                WHERE id=?
                """,
                (note["id"], ref["id"]),
            )
            conn.execute(
                "UPDATE delivery_notes SET status='COLLEGATO' WHERE id=? AND status<>'DA_VERIFICARE'",
                (note["id"],),
            )


def reconcile_delivery_note(delivery_note_id, conn=None):
    own_conn = conn is None
    conn = conn or get_connection()
    try:
        note = conn.execute(
            "SELECT * FROM delivery_notes WHERE id=?",
            (delivery_note_id,),
        ).fetchone()
        if not note or not note["supplier_id"] or not note["ddt_number_norm"]:
            return

        refs = conn.execute(
            """
            SELECT r.id,r.invoice_document_id,r.reference_date
            FROM invoice_ddt_references r
            JOIN documents d ON d.id=r.invoice_document_id
            WHERE d.supplier_id=? AND r.reference_number_norm=?
              AND (r.reference_date IS NULL OR r.reference_date='' OR r.reference_date=? OR ?='')
            """,
            (note["supplier_id"], note["ddt_number_norm"], note["ddt_date"], note["ddt_date"] or ""),
        ).fetchall()

        affected = set()
        for ref in refs:
            conn.execute(
                "UPDATE invoice_ddt_references SET delivery_note_id=?,status='COLLEGATO' WHERE id=?",
                (delivery_note_id, ref["id"]),
            )
            affected.add(ref["invoice_document_id"])

        if affected and note["status"] != "DA_VERIFICARE":
            conn.execute("UPDATE delivery_notes SET status='COLLEGATO' WHERE id=?", (delivery_note_id,))

        for invoice_id in affected:
            _rebuild_invoice_ddt_checks(conn, invoice_id)
        if own_conn:
            conn.commit()
    finally:
        if own_conn:
            conn.close()


def build_quantity_checks(invoice_qty, ddt_qty):
    checks = []
    for product_id in sorted(set(invoice_qty) | set(ddt_qty)):
        inv = float(invoice_qty.get(product_id, 0.0) or 0.0)
        ddt = float(ddt_qty.get(product_id, 0.0) or 0.0)
        difference = round(inv - ddt, 4)
        if abs(difference) <= 0.0001:
            status = "OK"
        elif inv == 0:
            status = "SOLO_DDT"
        elif ddt == 0:
            status = "SOLO_FATTURA"
        else:
            status = "QUANTITA_DIFFERENTE"
        checks.append({
            "product_id": product_id,
            "invoice_quantity": inv,
            "ddt_quantity": ddt,
            "quantity_difference": difference,
            "status": status,
        })
    return checks


def _rebuild_invoice_ddt_checks(conn, invoice_document_id):
    conn.execute("DELETE FROM invoice_ddt_checks WHERE invoice_document_id=?", (invoice_document_id,))
    linked = conn.execute(
        """
        SELECT COUNT(*) AS c FROM invoice_ddt_references
        WHERE invoice_document_id=? AND delivery_note_id IS NOT NULL
        """,
        (invoice_document_id,),
    ).fetchone()["c"]
    if not linked:
        return

    invoice_rows = conn.execute(
        """
        SELECT product_id,SUM(quantity) AS qty
        FROM document_lines
        WHERE document_id=? AND product_id IS NOT NULL
          AND COALESCE(line_type,'PRODUCT')='PRODUCT'
        GROUP BY product_id
        """,
        (invoice_document_id,),
    ).fetchall()
    ddt_rows = conn.execute(
        """
        SELECT l.product_id,SUM(l.quantity) AS qty
        FROM invoice_ddt_references r
        JOIN delivery_note_lines l ON l.delivery_note_id=r.delivery_note_id
        WHERE r.invoice_document_id=? AND r.delivery_note_id IS NOT NULL
          AND l.product_id IS NOT NULL
        GROUP BY l.product_id
        """,
        (invoice_document_id,),
    ).fetchall()

    invoice_qty = {r["product_id"]: float(r["qty"] or 0) for r in invoice_rows}
    ddt_qty = {r["product_id"]: float(r["qty"] or 0) for r in ddt_rows}
    for check in build_quantity_checks(invoice_qty, ddt_qty):
        conn.execute(
            """
            INSERT INTO invoice_ddt_checks(
                invoice_document_id,product_id,invoice_quantity,ddt_quantity,quantity_difference,status
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                invoice_document_id,
                check["product_id"],
                check["invoice_quantity"],
                check["ddt_quantity"],
                check["quantity_difference"],
                check["status"],
            ),
        )


def import_ddt_file(file_name, file_bytes):
    suffix = Path(file_name).suffix.lower()
    if suffix not in SUPPORTED_DDT_SUFFIXES:
        raise ValueError("Formato DDT non supportato. Usa JPG, PNG oppure PDF.")

    file_hash = hashlib.sha256(file_bytes).hexdigest()
    conn = get_connection()
    duplicate = conn.execute(
        "SELECT id FROM delivery_notes WHERE file_hash=?",
        (file_hash,),
    ).fetchone()
    if duplicate:
        conn.close()
        return {
            "ok": False,
            "duplicate": True,
            "status": "DUPLICATO",
            "message": f"DDT gia importato (ID {duplicate['id']}).",
        }

    parsed = parse_ddt_with_ai(file_name, file_bytes)
    reasons = _quality_reasons(parsed)

    supplier_id = None
    supplier_created = False
    supplier_name = parsed["supplier"]["name"]
    if supplier_name or parsed["supplier"]["vat"]:
        supplier_id, supplier_created = ensure_supplier(conn, {
            "name": supplier_name or "Fornitore non identificato",
            "vat": parsed["supplier"]["vat"],
        })

    stored_path = store_document_bytes(file_name, file_hash, file_bytes)
    status = "DA_VERIFICARE" if reasons else "IN_ATTESA_FATTURA"
    source = "PDF_AI" if suffix == ".pdf" else "IMAGE_AI"
    cur = conn.execute(
        """
        INSERT INTO delivery_notes(
            file_name,file_hash,supplier_id,ddt_number,ddt_number_norm,ddt_date,
            source,status,stored_path,review_reason
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            file_name, file_hash, supplier_id, parsed["ddt_number"],
            normalize_ddt_number(parsed["ddt_number"]), parsed["ddt_date"],
            source, status, stored_path, "\n".join(reasons) if reasons else None,
        ),
    )
    delivery_note_id = cur.lastrowid

    created_products = 0
    for line in parsed["lines"]:
        product_id = None
        match_method = "FORNITORE_DA_VERIFICARE"
        if supplier_id:
            product_id, match_method, created = ensure_product(conn, supplier_id, line)
            created_products += 1 if created else 0
        conn.execute(
            """
            INSERT INTO delivery_note_lines(
                delivery_note_id,line_number,supplier_code,original_description,
                product_id,match_method,quantity,unit
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                delivery_note_id, line["line_number"], line["supplier_code"],
                line["description"], product_id, match_method, line["quantity"], line["unit"],
            ),
        )

    reconcile_delivery_note(delivery_note_id, conn=conn)
    conn.commit()
    conn.close()
    return {
        "ok": True,
        "duplicate": False,
        "delivery_note_id": delivery_note_id,
        "status": status,
        "supplier": supplier_name,
        "supplier_created": supplier_created,
        "ddt_number": parsed["ddt_number"],
        "ddt_date": parsed["ddt_date"],
        "lines_count": len(parsed["lines"]),
        "created_products": created_products,
        "review_reason": "\n".join(reasons) if reasons else None,
    }


def invoice_ddt_summary(invoice_document_id, conn=None):
    own_conn = conn is None
    conn = conn or get_connection()
    try:
        refs = conn.execute(
            """
            SELECT r.id,r.reference_number,r.reference_date,r.status,r.delivery_note_id,
                   n.ddt_number,n.ddt_date,n.file_name
            FROM invoice_ddt_references r
            LEFT JOIN delivery_notes n ON n.id=r.delivery_note_id
            WHERE r.invoice_document_id=?
            ORDER BY COALESCE(r.reference_date,''),r.id
            """,
            (invoice_document_id,),
        ).fetchall()
        checks = conn.execute(
            """
            SELECT c.*,p.name AS product_name
            FROM invoice_ddt_checks c
            LEFT JOIN products p ON p.id=c.product_id
            WHERE c.invoice_document_id=?
            ORDER BY CASE WHEN c.status='OK' THEN 1 ELSE 0 END,p.name
            """,
            (invoice_document_id,),
        ).fetchall()

        missing_refs = sum(1 for r in refs if not r["delivery_note_id"])
        differences = sum(1 for c in checks if c["status"] != "OK")
        if not refs:
            overall = "NESSUN_RIFERIMENTO"
        elif missing_refs:
            overall = "DDT_MANCANTI"
        elif differences:
            overall = "DIFFERENZE"
        else:
            overall = "OK"

        return {
            "status": overall,
            "references_count": len(refs),
            "linked_count": len(refs) - missing_refs,
            "missing_count": missing_refs,
            "differences_count": differences,
            "references": [dict(r) for r in refs],
            "checks": [dict(c) for c in checks],
        }
    finally:
        if own_conn:
            conn.close()
