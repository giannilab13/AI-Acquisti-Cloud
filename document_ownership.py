import re

from db import get_active_company_id, get_company


def normalize_vat(value):
    value = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    if value.startswith("IT") and len(value) > 2:
        value = value[2:]
    return value


def normalize_company_name(value):
    text = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    for suffix in ("SOCIETAPERAZIONI", "SOCIETARESPONSABILITALIMITATA", "SRL", "SPA", "SNC", "SAS"):
        if text.endswith(suffix):
            text = text[:-len(suffix)]
            break
    return text


def validate_recipient(company, recipient):
    company = company or {}
    recipient = recipient or {}

    company_vat = normalize_vat(company.get("vat_number"))
    if not company_vat:
        return False, "P.IVA dell'azienda non configurata"

    recipient_vat = normalize_vat(recipient.get("vat"))
    if recipient_vat:
        if recipient_vat == company_vat:
            return True, "P.IVA destinatario corrispondente"
        return False, "P.IVA destinatario diversa dall'azienda selezionata"

    company_name = normalize_company_name(company.get("name"))
    recipient_name = normalize_company_name(recipient.get("name"))
    if company_name and recipient_name and company_name == recipient_name:
        return True, "Ragione sociale destinatario corrispondente"

    if not recipient_name:
        return False, "Destinatario del documento non leggibile"
    return False, "Destinatario diverso dall'azienda selezionata"


def assert_document_belongs_to_active_company(recipient):
    company = get_company(get_active_company_id())
    ok, reason = validate_recipient(company, recipient)
    if ok:
        return company

    company_name = (company or {}).get("name") or "azienda selezionata"
    if reason == "P.IVA dell'azienda non configurata":
        raise ValueError(
            f"Configura la P.IVA di {company_name} prima di importare fatture o DDT."
        )
    raise ValueError(
        f"Documento non appartenente a {company_name}: {reason}. Importazione bloccata."
    )
