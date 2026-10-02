import re

CREDIT_NOTE_TYPES = {"TD04", "TD08", "TD09"}


def assess_invoice_quality(parsed, extraction_mode=None):
    """Returns human-readable reasons that require manual verification."""
    reasons = []

    supplier = parsed.get("supplier") or {}
    supplier_name = str(supplier.get("name") or "").strip()
    if not supplier_name or "non identificato" in supplier_name.lower():
        reasons.append("Fornitore non identificato con sufficiente sicurezza.")

    document_number = str(parsed.get("document_number") or "").strip()
    if not document_number:
        reasons.append("Numero fattura mancante.")

    document_date = str(parsed.get("document_date") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", document_date):
        reasons.append("Data fattura mancante o non valida.")

    total_amount = parsed.get("total_amount")
    try:
        total_amount = float(total_amount or 0)
    except Exception:
        total_amount = 0
    if total_amount <= 0:
        reasons.append("Totale documento mancante o non valido.")

    lines = parsed.get("lines") or []
    if not lines:
        reasons.append("Nessuna riga di fattura rilevata.")

    for idx, line in enumerate(lines, start=1):
        if str(line.get("line_type") or "PRODUCT").upper() == "ANCILLARY":
            continue

        description = str(line.get("description") or "").strip()
        if not description:
            reasons.append(f"Riga {idx}: descrizione prodotto mancante.")

        try:
            quantity = float(line.get("quantity") or 0)
        except Exception:
            quantity = 0
        if quantity <= 0:
            reasons.append(f"Riga {idx}: quantità mancante o non valida.")

        try:
            net_price = float(line.get("net_price") or line.get("unit_price") or 0)
        except Exception:
            net_price = 0
        if net_price <= 0:
            reasons.append(f"Riga {idx}: prezzo netto mancante o non valido.")

    # A scan can be read successfully, but we intentionally request one human confirmation.
    if extraction_mode == "PDF_VISION_AI":
        reasons.append("PDF scansione letto visivamente dall'AI: confermare i dati prima di registrarli nello storico.")

    # Preserve order, remove duplicates.
    seen = set()
    clean = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            clean.append(reason)
    return clean


def choose_invoice_status(document_type, review_reasons, anomaly_count):
    if str(document_type or "").upper() in CREDIT_NOTE_TYPES:
        return "IMPORTATO_SENZA_MOVIMENTI"
    if review_reasons:
        return "DA_VERIFICARE"
    if int(anomaly_count or 0) > 0:
        return "CON_ANOMALIE"
    return "IMPORTATA"
