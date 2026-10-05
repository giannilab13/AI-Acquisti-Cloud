import hashlib
import json
import os
import re
from io import BytesIO

from openai import OpenAI
from pypdf import PdfReader

from db import get_connection
from storage_service import store_document_bytes
from document_quality import assess_invoice_quality, choose_invoice_status
from xml_importer import CREDIT_NOTE_TYPES, ensure_product, ensure_supplier, latest_expected_net_price

MODEL = os.getenv('OPENAI_MODEL', 'gpt-5.6-luna')


def _num(value, default=0.0):
    if value is None or value == '':
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace('€', '').replace(' ', '')
    if ',' in text and '.' in text:
        text = text.replace('.', '').replace(',', '.')
    else:
        text = text.replace(',', '.')
    try:
        return float(text)
    except Exception:
        return default


def extract_pdf_text(pdf_bytes: bytes):
    reader = PdfReader(BytesIO(pdf_bytes))
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ''
        pages.append(f'\n--- PAGINA {i} ---\n{text}')
    full_text = '\n'.join(pages).strip()
    meaningful = re.sub(r'\s+', '', full_text)
    if len(meaningful) < 120:
        raise ValueError(
            "Il PDF sembra una scansione o un'immagine e non contiene abbastanza testo selezionabile. "
            "Questa versione gestisce PDF digitali; l'OCR verrà aggiunto nel passaggio successivo."
        )
    return full_text


def _extract_json(text: str):
    text = (text or '').strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.I)
        text = re.sub(r'\s*```$', '', text)
    try:
        return json.loads(text)
    except Exception:
        start = text.find('{')
        end = text.rfind('}')
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise ValueError("L'AI non ha restituito dati JSON validi per la fattura.")


INVOICE_EXTRACTION_INSTRUCTIONS = """Sei un estrattore dati per fatture di acquisto italiane.
Leggi ESCLUSIVAMENTE la fattura fornita dall'utente e restituisci SOLO JSON valido, senza markdown.

Schema obbligatorio:
{
  "supplier": {"name": "", "vat": ""},
  "document_type": "TD01",
  "document_date": "YYYY-MM-DD",
  "document_number": "",
  "currency": "EUR",
  "total_amount": 0.0,
  "delivery_notes": [
    {"number": "", "date": "YYYY-MM-DD"}
  ],
  "lines": [
    {
      "line_number": 1,
      "supplier_code": "",
      "description": "",
      "line_type": "PRODUCT",
      "quantity": 1.0,
      "unit": "pz",
      "list_price": 0.0,
      "discount_1": 0.0,
      "discount_2": 0.0,
      "net_price": 0.0,
      "line_total": 0.0,
      "vat_rate": 0.0
    }
  ]
}

REGOLE:
- Non inventare dati mancanti. Usa stringa vuota o 0 quando non leggibile.
- Usa TD01 per fattura ordinaria e TD04 per nota di credito quando riconoscibile.
- In delivery_notes inserisci tutti i DDT esplicitamente richiamati dalla fattura, con numero e data. Se non ci sono riferimenti DDT usa [].
- list_price = prezzo unitario prima degli sconti, se indicato.
- discount_1 e discount_2 sono percentuali, non importi.
- Gli sconti concatenati restano separati.
- net_price = prezzo unitario effettivo dopo gli sconti.
- Se il documento mostra direttamente un netto senza sconti, list_price e net_price possono coincidere.
- NON inserire IVA, imponibile o totale documento come righe prodotto.
- Trasporto, consegna, imballo, contributi e costi accessori: line_type="ANCILLARY".
- Articoli/servizi acquistati: line_type="PRODUCT".
"""


def _normalise_ai_invoice(data):
    if not isinstance(data, dict) or not isinstance(data.get("lines"), list) or not data["lines"]:
        raise ValueError("Non sono state individuate righe di fattura utilizzabili nel PDF.")

    for idx, line in enumerate(data["lines"], start=1):
        line["line_number"] = int(_num(line.get("line_number"), idx))
        line["supplier_code"] = str(line.get("supplier_code") or "").strip()
        line["description"] = str(line.get("description") or "").strip()
        line["line_type"] = str(line.get("line_type") or "PRODUCT").upper().strip()
        if line["line_type"] not in {"PRODUCT", "ANCILLARY"}:
            line["line_type"] = "PRODUCT"
        line["quantity"] = _num(line.get("quantity"), 1.0) or 1.0
        line["unit"] = str(line.get("unit") or "pz").strip()
        line["list_price"] = _num(line.get("list_price"), 0.0)
        line["discount_1"] = _num(line.get("discount_1"), 0.0)
        line["discount_2"] = _num(line.get("discount_2"), 0.0)
        line["net_price"] = _num(line.get("net_price"), 0.0)
        line["line_total"] = _num(line.get("line_total"), 0.0)
        line["vat_rate"] = _num(line.get("vat_rate"), 0.0)

        if not line["net_price"] and line["line_total"] and line["quantity"]:
            line["net_price"] = line["line_total"] / line["quantity"]
        if not line["net_price"] and line["list_price"]:
            line["net_price"] = (
                line["list_price"]
                * (1 - line["discount_1"] / 100.0)
                * (1 - line["discount_2"] / 100.0)
            )
        if not line["list_price"]:
            line["list_price"] = line["net_price"]
        if not line["line_total"]:
            line["line_total"] = line["quantity"] * line["net_price"]

        line["unit_price"] = line["net_price"]

    supplier = data.get("supplier") or {}
    data["supplier"] = {
        "name": str(supplier.get("name") or "Fornitore non identificato").strip(),
        "vat": str(supplier.get("vat") or "").replace(" ", "").strip(),
    }
    data["document_type"] = str(data.get("document_type") or "TD01").strip().upper()
    data["document_date"] = str(data.get("document_date") or "").strip()
    data["document_number"] = str(data.get("document_number") or "").strip()
    data["currency"] = str(data.get("currency") or "EUR").strip().upper()
    data["total_amount"] = _num(data.get("total_amount"), 0.0)
    delivery_notes = []
    for ref in data.get("delivery_notes") or []:
        if not isinstance(ref, dict):
            continue
        number = str(ref.get("number") or "").strip()
        date = str(ref.get("date") or "").strip()
        if number:
            delivery_notes.append({"number": number, "date": date})
    data["delivery_notes"] = delivery_notes
    return data


def parse_invoice_pdf_with_ai(pdf_text: str):
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY non configurata nel file .env.")

    if len(pdf_text) > 60000:
        pdf_text = pdf_text[:60000]

    client = OpenAI(api_key=api_key)
    response = client.responses.create(
        model=MODEL,
        instructions=INVOICE_EXTRACTION_INSTRUCTIONS,
        input="TESTO PDF FATTURA:\n" + pdf_text,
    )
    return _normalise_ai_invoice(_extract_json(response.output_text))


def parse_scanned_pdf_with_ai(file_name: str, pdf_bytes: bytes):
    """Fallback per PDF scansione/immagine tramite input_file multimodale."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY non configurata nel file .env.")

    client = OpenAI(api_key=api_key)
    uploaded = None
    try:
        from io import BytesIO
        buffer = BytesIO(pdf_bytes)
        buffer.name = file_name or "fattura_scansione.pdf"

        uploaded = client.files.create(
            file=buffer,
            purpose="user_data",
        )

        response = client.responses.create(
            model=MODEL,
            instructions=INVOICE_EXTRACTION_INSTRUCTIONS,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_file", "file_id": uploaded.id},
                        {
                            "type": "input_text",
                            "text": (
                                "Analizza visivamente questa fattura PDF anche se deriva da una scansione. "
                                "Estrai tutti i dati richiesti nello schema JSON."
                            ),
                        },
                    ],
                }
            ],
        )
        return _normalise_ai_invoice(_extract_json(response.output_text))
    finally:
        if uploaded is not None:
            try:
                client.files.delete(uploaded.id)
            except Exception:
                pass

def import_pdf_file(file_name: str, pdf_bytes: bytes):
    file_hash = hashlib.sha256(pdf_bytes).hexdigest()
    conn = get_connection()
    duplicate = conn.execute('SELECT id FROM documents WHERE file_hash=?', (file_hash,)).fetchone()
    if duplicate:
        conn.close()
        return {'ok': False, 'duplicate': True, 'message': f"Documento già importato (ID {duplicate['id']})."}

    extraction_mode = "PDF_TEXT_AI"
    try:
        pdf_text = extract_pdf_text(pdf_bytes)
        parsed = parse_invoice_pdf_with_ai(pdf_text)
    except ValueError as exc:
        if "scansione" not in str(exc).lower() and "testo selezionabile" not in str(exc).lower():
            conn.close()
            raise
        extraction_mode = "PDF_VISION_AI"
        parsed = parse_scanned_pdf_with_ai(file_name, pdf_bytes)

    supplier_id, supplier_created = ensure_supplier(conn, parsed['supplier'])

    stored_path = store_document_bytes(file_name, file_hash, pdf_bytes)

    status = 'IN_ELABORAZIONE'
    cur = conn.execute(
        '''INSERT INTO documents(file_name,file_hash,document_type,supplier_id,document_number,
            document_date,currency,total_amount,source,status,stored_path)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
        (file_name, file_hash, parsed['document_type'], supplier_id, parsed['document_number'],
         parsed['document_date'], parsed['currency'], parsed['total_amount'], 'PDF_AI', status, stored_path),
    )
    document_id = cur.lastrowid
    created_products = 0
    ancillary_lines = 0
    anomaly_count = 0

    for line in parsed['lines']:
        if line.get('line_type') == 'ANCILLARY':
            ancillary_lines += 1
            conn.execute(
                '''INSERT INTO document_lines(
                    document_id,line_number,supplier_code,original_description,product_id,match_method,
                    quantity,unit,unit_price,line_total,vat_rate,invoice_list_price,invoice_discount_1,
                    invoice_discount_2,invoice_net_price,expected_net_price,price_variance_unit,
                    price_variance_total,price_check_status,line_type)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (document_id,line['line_number'],line['supplier_code'],line['description'],None,'SPESA_ACCESSORIA',
                 line['quantity'],line['unit'],line['net_price'],line['line_total'],line['vat_rate'],line['list_price'],
                 line['discount_1'],line['discount_2'],line['net_price'],None,None,None,'NON_APPLICABILE','ANCILLARY'),
            )
            continue

        product_id, match_method, created = ensure_product(conn, supplier_id, line)
        created_products += 1 if created else 0
        expected_net = latest_expected_net_price(conn, supplier_id, product_id)
        invoice_net = float(line['net_price'])
        variance_unit = variance_total = None
        check_status = 'SENZA_LISTINO'
        if expected_net is not None:
            variance_unit = round(invoice_net - expected_net, 4)
            variance_total = round(variance_unit * float(line['quantity']), 4)
            check_status = 'OK' if abs(variance_unit) <= 0.005 else 'DIFFERENZA'
            if check_status == 'DIFFERENZA':
                anomaly_count += 1

        conn.execute(
            '''INSERT INTO document_lines(
                document_id,line_number,supplier_code,original_description,product_id,match_method,
                quantity,unit,unit_price,line_total,vat_rate,invoice_list_price,invoice_discount_1,
                invoice_discount_2,invoice_net_price,expected_net_price,price_variance_unit,
                price_variance_total,price_check_status,line_type)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (document_id,line['line_number'],line['supplier_code'],line['description'],product_id,match_method,
             line['quantity'],line['unit'],line['unit_price'],line['line_total'],line['vat_rate'],line['list_price'],
             line['discount_1'],line['discount_2'],invoice_net,expected_net,variance_unit,variance_total,
             check_status,'PRODUCT'),
        )
        if parsed['document_type'] not in CREDIT_NOTE_TYPES:
            conn.execute(
                '''INSERT INTO purchases(purchase_date,supplier_id,product_id,quantity,unit_price,source_document_id)
                   VALUES(?,?,?,?,?,?)''',
                (parsed['document_date'],supplier_id,product_id,line['quantity'],line['unit_price'],document_id),
            )

    review_reasons = assess_invoice_quality(parsed, extraction_mode=extraction_mode)
    status = choose_invoice_status(parsed['document_type'], review_reasons, anomaly_count)

    # Finché non viene confermata, una fattura da verificare non entra nello storico.
    if status == 'DA_VERIFICARE':
        conn.execute('DELETE FROM purchases WHERE source_document_id=?', (document_id,))

    conn.execute(
        'UPDATE documents SET status=?, review_reason=? WHERE id=?',
        (status, '\n'.join(review_reasons) if review_reasons else None, document_id),
    )

    from ddt_importer import store_invoice_ddt_references
    store_invoice_ddt_references(
        document_id,
        supplier_id,
        parsed.get('delivery_notes') or [],
        conn=conn,
    )

    conn.commit()
    conn.close()
    return {
        'ok': True, 'duplicate': False, 'document_id': document_id, 'source': 'PDF_AI',
        'extraction_mode': extraction_mode, 'status': status,
        'supplier': parsed['supplier']['name'], 'supplier_created': supplier_created,
        'document_number': parsed['document_number'], 'document_date': parsed['document_date'],
        'document_type': parsed['document_type'], 'total_amount': parsed['total_amount'],
        'lines_count': len(parsed['lines']), 'ancillary_lines': ancillary_lines,
        'created_products': created_products, 'anomaly_count': anomaly_count,
        'ddt_references_count': len(parsed.get('delivery_notes') or []),
        'review_reason': '\n'.join(review_reasons) if review_reasons else None,
    }
