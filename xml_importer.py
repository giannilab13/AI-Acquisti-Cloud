import hashlib
import re
import unicodedata
from pathlib import Path
from xml.etree import ElementTree as ET

from db import get_connection, next_text_id, normalize_basic
from storage_service import store_document_bytes
from document_quality import assess_invoice_quality, choose_invoice_status

CREDIT_NOTE_TYPES = {"TD04", "TD08", "TD09"}


def lname(tag):
    return tag.split("}")[-1]


def child(node, name):
    if node is None:
        return None
    for c in list(node):
        if lname(c.tag) == name:
            return c
    return None


def children(node, name):
    if node is None:
        return []
    return [c for c in list(node) if lname(c.tag) == name]


def path(node, *names):
    cur = node
    for name in names:
        cur = child(cur, name)
        if cur is None:
            return None
    return cur


def text_at(node, *names, default=""):
    el = path(node, *names)
    if el is None or el.text is None:
        return default
    return el.text.strip()


def to_float(value, default=0.0):
    try:
        return float(str(value).strip().replace(",", "."))
    except Exception:
        return default


def line_discounts(detail):
    discounts = []
    for node in children(detail, "ScontoMaggiorazione"):
        if text_at(node, "Tipo").upper() != "SC":
            continue
        discounts.append(to_float(text_at(node, "Percentuale"), 0.0))
        if len(discounts) >= 2:
            break
    while len(discounts) < 2:
        discounts.append(0.0)
    return discounts[0], discounts[1]


def latest_expected_net_price(conn, supplier_id, product_id):
    row = conn.execute(
        """
        SELECT pll.net_price
        FROM price_list_lines pll
        JOIN price_lists pl ON pl.id=pll.price_list_id
        WHERE pl.supplier_id=? AND pll.product_id=? AND pll.net_price IS NOT NULL
        ORDER BY pl.imported_at DESC, pl.id DESC, pll.id DESC
        LIMIT 1
        """,
        (supplier_id, product_id),
    ).fetchone()
    return float(row["net_price"]) if row and row["net_price"] is not None else None


def normalize_text(value):
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^A-Za-z0-9]+", " ", value).upper()
    return re.sub(r"\s+", " ", value).strip()


def parse_invoice(xml_bytes):
    root = ET.fromstring(xml_bytes)
    header = child(root, "FatturaElettronicaHeader")
    bodies = children(root, "FatturaElettronicaBody")
    if header is None or not bodies:
        raise ValueError("Il file non sembra una FatturaPA valida.")

    supplier_node = child(header, "CedentePrestatore")
    dati_anag = child(supplier_node, "DatiAnagrafici")
    anagrafica = child(dati_anag, "Anagrafica")

    vat_country = text_at(dati_anag, "IdFiscaleIVA", "IdPaese")
    vat_code = text_at(dati_anag, "IdFiscaleIVA", "IdCodice")
    vat = f"{vat_country}{vat_code}" if vat_code else ""

    supplier_name = text_at(anagrafica, "Denominazione")
    if not supplier_name:
        supplier_name = " ".join(
            x for x in [text_at(anagrafica, "Nome"), text_at(anagrafica, "Cognome")] if x
        ).strip()
    supplier_name = supplier_name or "Fornitore non identificato"

    recipient_node = child(header, "CessionarioCommittente")
    recipient_dati = child(recipient_node, "DatiAnagrafici")
    recipient_anagrafica = child(recipient_dati, "Anagrafica")
    recipient_vat_country = text_at(recipient_dati, "IdFiscaleIVA", "IdPaese")
    recipient_vat_code = text_at(recipient_dati, "IdFiscaleIVA", "IdCodice")
    recipient_vat = f"{recipient_vat_country}{recipient_vat_code}" if recipient_vat_code else ""
    recipient_name = text_at(recipient_anagrafica, "Denominazione")
    if not recipient_name:
        recipient_name = " ".join(
            x for x in [text_at(recipient_anagrafica, "Nome"), text_at(recipient_anagrafica, "Cognome")] if x
        ).strip()

    body = bodies[0]
    dati_generali = child(body, "DatiGenerali")
    dgd = child(dati_generali, "DatiGeneraliDocumento")
    document_type = text_at(dgd, "TipoDocumento")
    document_date = text_at(dgd, "Data")
    document_number = text_at(dgd, "Numero")
    currency = text_at(dgd, "Divisa", default="EUR")
    total_amount = to_float(text_at(dgd, "ImportoTotaleDocumento"), 0.0)

    delivery_notes = []
    for ddt in children(dati_generali, "DatiDDT"):
        number = text_at(ddt, "NumeroDDT")
        date = text_at(ddt, "DataDDT")
        if number:
            delivery_notes.append({"number": number, "date": date})

    rows = []
    dati_beni = child(body, "DatiBeniServizi")
    for detail in children(dati_beni, "DettaglioLinee"):
        code = ""
        code_nodes = children(detail, "CodiceArticolo")
        if code_nodes:
            code = text_at(code_nodes[0], "CodiceValore")
        description = text_at(detail, "Descrizione")
        qty = to_float(text_at(detail, "Quantita"), 1.0)
        unit = text_at(detail, "UnitaMisura", default="pz")
        list_price = to_float(text_at(detail, "PrezzoUnitario"), 0.0)
        discount_1, discount_2 = line_discounts(detail)
        line_total = to_float(text_at(detail, "PrezzoTotale"), qty * list_price)
        net_price = (line_total / qty) if qty else list_price
        vat_rate = to_float(text_at(detail, "AliquotaIVA"), 0.0)
        line_no = int(to_float(text_at(detail, "NumeroLinea"), len(rows) + 1))
        rows.append({
            "line_number": line_no,
            "supplier_code": code,
            "description": description,
            "quantity": qty,
            "unit": unit,
            "unit_price": net_price,
            "list_price": list_price,
            "discount_1": discount_1,
            "discount_2": discount_2,
            "net_price": net_price,
            "line_total": line_total,
            "vat_rate": vat_rate,
        })

    if not rows:
        raise ValueError("La fattura non contiene righe articolo leggibili.")

    return {
        "supplier": {"name": supplier_name, "vat": vat},
        "recipient": {
            "name": recipient_name,
            "vat": recipient_vat,
            "tax_code": text_at(recipient_dati, "CodiceFiscale"),
        },
        "document_type": document_type,
        "document_date": document_date,
        "document_number": document_number,
        "currency": currency,
        "total_amount": total_amount,
        "delivery_notes": delivery_notes,
        "lines": rows,
    }


def ensure_supplier(conn, supplier):
    vat = supplier.get("vat") or ""
    if vat:
        row = conn.execute("SELECT id FROM suppliers WHERE vat_number=?", (vat,)).fetchone()
        if row:
            return row["id"], False

    row = conn.execute("SELECT id FROM suppliers WHERE UPPER(name)=UPPER(?)", (supplier["name"],)).fetchone()
    if row:
        return row["id"], False

    supplier_id = next_text_id(conn, "suppliers", "FOR")
    conn.execute(
        "INSERT INTO suppliers(id,name,vat_number,category) VALUES(?,?,?,?)",
        (supplier_id, supplier["name"], vat, "Da classificare"),
    )
    return supplier_id, True


def ensure_product(conn, supplier_id, line):
    code = (line.get("supplier_code") or "").strip()
    description = line.get("description") or ""
    normalized = normalize_basic(description)

    if code:
        row = conn.execute(
            "SELECT product_id FROM supplier_products WHERE supplier_id=? AND supplier_code=?",
            (supplier_id, code),
        ).fetchone()
        if row:
            return row["product_id"], "CODICE_FORNITORE", False

    row = conn.execute(
        "SELECT id FROM products WHERE UPPER(normalized_name)=? ORDER BY id LIMIT 1",
        (normalized,),
    ).fetchone()
    if row:
        product_id = row["id"]
        match_method = "DESCRIZIONE_ESATTA"
        created = False
    else:
        product_id = next_text_id(conn, "products", "PRD")
        conn.execute(
            "INSERT INTO products(id,name,normalized_name,category,unit) VALUES(?,?,?,?,?)",
            (product_id, description.strip() or product_id, normalized, "Da classificare", line.get("unit") or "pz"),
        )
        match_method = "NUOVO_PRODOTTO"
        created = True

    if code:
        conn.execute(
            "INSERT OR IGNORE INTO supplier_products(supplier_id,supplier_code,original_description,product_id) VALUES(?,?,?,?)",
            (supplier_id, code, description, product_id),
        )

    return product_id, match_method, created


def import_xml_file(file_name, xml_bytes):
    file_hash = hashlib.sha256(xml_bytes).hexdigest()
    conn = get_connection()

    duplicate = conn.execute("SELECT id FROM documents WHERE file_hash=?", (file_hash,)).fetchone()
    if duplicate:
        conn.close()
        return {"ok": False, "duplicate": True, "message": f"Documento già importato (ID {duplicate['id']})."}

    parsed = parse_invoice(xml_bytes)
    from document_ownership import assert_document_belongs_to_active_company
    try:
        assert_document_belongs_to_active_company(parsed.get("recipient"))
    except Exception:
        conn.close()
        raise
    supplier_id, supplier_created = ensure_supplier(conn, parsed["supplier"])

    stored_path = store_document_bytes(file_name, file_hash, xml_bytes)

    status = "IN_ELABORAZIONE"
    cur = conn.execute(
        """
        INSERT INTO documents(file_name,file_hash,document_type,supplier_id,document_number,
            document_date,currency,total_amount,source,status,stored_path)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            file_name, file_hash, parsed["document_type"], supplier_id,
            parsed["document_number"], parsed["document_date"], parsed["currency"],
            parsed["total_amount"], "UPLOAD", status, stored_path,
        ),
    )
    document_id = cur.lastrowid

    created_products = 0
    anomaly_count = 0
    for line in parsed["lines"]:
        product_id, match_method, created = ensure_product(conn, supplier_id, line)
        created_products += 1 if created else 0
        expected_net = latest_expected_net_price(conn, supplier_id, product_id)
        invoice_net = float(line["net_price"])
        variance_unit = None
        variance_total = None
        check_status = "SENZA_LISTINO"
        if expected_net is not None:
            variance_unit = round(invoice_net - expected_net, 4)
            variance_total = round(variance_unit * float(line["quantity"]), 4)
            check_status = "OK" if abs(variance_unit) <= 0.005 else "DIFFERENZA"
            if check_status == "DIFFERENZA":
                anomaly_count += 1

        conn.execute(
            """
            INSERT INTO document_lines(
                document_id,line_number,supplier_code,original_description,
                product_id,match_method,quantity,unit,unit_price,line_total,vat_rate,
                invoice_list_price,invoice_discount_1,invoice_discount_2,
                invoice_net_price,expected_net_price,price_variance_unit,
                price_variance_total,price_check_status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                document_id, line["line_number"], line["supplier_code"], line["description"],
                product_id, match_method, line["quantity"], line["unit"], line["unit_price"],
                line["line_total"], line["vat_rate"],
                line["list_price"], line["discount_1"], line["discount_2"],
                invoice_net, expected_net, variance_unit, variance_total, check_status,
            ),
        )

        if parsed["document_type"] not in CREDIT_NOTE_TYPES:
            conn.execute(
                """
                INSERT INTO purchases(purchase_date,supplier_id,product_id,quantity,unit_price,source_document_id)
                VALUES(?,?,?,?,?,?)
                """,
                (
                    parsed["document_date"], supplier_id, product_id,
                    line["quantity"], line["unit_price"], document_id,
                ),
            )

    review_reasons = assess_invoice_quality(parsed, extraction_mode="XML")
    status = choose_invoice_status(parsed["document_type"], review_reasons, anomaly_count)

    # Una fattura da verificare non deve influenzare spesa e storico prezzi.
    if status == "DA_VERIFICARE":
        conn.execute("DELETE FROM purchases WHERE source_document_id=?", (document_id,))

    conn.execute(
        "UPDATE documents SET status=?, review_reason=? WHERE id=?",
        (status, "\n".join(review_reasons) if review_reasons else None, document_id),
    )

    # Collega gli eventuali DDT citati nella FatturaPA e prepara il controllo
    # prodotti/quantita senza interferire con il controllo prezzi-listino.
    from ddt_importer import store_invoice_ddt_references
    store_invoice_ddt_references(
        document_id,
        supplier_id,
        parsed.get("delivery_notes") or [],
        conn=conn,
    )

    conn.commit()
    conn.close()
    return {
        "ok": True,
        "duplicate": False,
        "document_id": document_id,
        "status": status,
        "supplier": parsed["supplier"]["name"],
        "supplier_created": supplier_created,
        "document_number": parsed["document_number"],
        "document_date": parsed["document_date"],
        "document_type": parsed["document_type"],
        "total_amount": parsed["total_amount"],
        "lines_count": len(parsed["lines"]),
        "created_products": created_products,
        "anomaly_count": anomaly_count,
        "ddt_references_count": len(parsed.get("delivery_notes") or []),
        "review_reason": "\n".join(review_reasons) if review_reasons else None,
    }
