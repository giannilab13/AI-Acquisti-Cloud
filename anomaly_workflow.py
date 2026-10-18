import json

from price_reference import accept_invoice_price_update, establish_invoice_baselines


def _verification_map(conn, document_id):
    rows = conn.execute(
        """
        SELECT anomaly_key,anomaly_type,source_id,original_status,summary,
               invoice_value,reference_value,difference_value,details_json,
               verified_at,verified_by_user_id,verified_by_name,verified_by_email,note,resolution
        FROM anomaly_verifications
        WHERE document_id=?
        """,
        (document_id,),
    ).fetchall()
    return {row["anomaly_key"]: dict(row) for row in rows}


def load_document_anomalies(conn, document_id):
    """Return every current anomaly with its verification/audit state."""
    verified = _verification_map(conn, document_id)
    anomalies = []

    price_rows = conn.execute(
        """
        SELECT dl.id,dl.line_number,dl.original_description,dl.product_id,
               COALESCE(p.name,dl.original_description) AS product_name,
               dl.quantity,dl.unit,dl.invoice_net_price,dl.expected_net_price,
               dl.price_variance_unit,dl.price_variance_total
        FROM document_lines dl
        LEFT JOIN products p ON p.id=dl.product_id
        WHERE dl.document_id=? AND dl.price_check_status='DIFFERENZA'
        ORDER BY dl.line_number,dl.id
        """,
        (document_id,),
    ).fetchall()

    for row in price_rows:
        r = dict(row)
        key = f"PRICE:{r['id']}"
        invoice_value = float(r["invoice_net_price"] or 0)
        reference_value = float(r["expected_net_price"] or 0)
        difference_value = float(r["price_variance_total"] or 0)
        variance_unit = float(r["price_variance_unit"] or 0)
        sign = "+" if variance_unit > 0 else ""
        summary = (
            f"{r['product_name'] or 'Prodotto'}: {sign}€{variance_unit:.2f}/unità "
            "rispetto al riferimento prezzo"
        )
        item = {
            "key": key,
            "type": "PRICE",
            "source_id": str(r["id"]),
            "summary": summary,
            "product_name": r["product_name"] or "Prodotto",
            "invoice_value": invoice_value,
            "reference_value": reference_value,
            "difference_value": difference_value,
            "difference_unit": variance_unit,
            "quantity": float(r["quantity"] or 0),
            "unit": r["unit"],
            "original_status": "DA_VERIFICARE",
            "verified": key in verified,
            "verification": verified.get(key),
        }
        anomalies.append(item)

    ddt_rows = conn.execute(
        """
        SELECT c.product_id,p.name AS product_name,c.ddt_quantity,
               c.invoice_quantity,c.quantity_difference,c.status
        FROM invoice_ddt_checks c
        LEFT JOIN products p ON p.id=c.product_id
        WHERE c.invoice_document_id=? AND c.status<>'OK'
        ORDER BY p.name,c.product_id
        """,
        (document_id,),
    ).fetchall()

    for row in ddt_rows:
        r = dict(row)
        source_id = str(r["product_id"])
        key = f"DDT_QTY:{source_id}"
        ddt_quantity = float(r["ddt_quantity"] or 0)
        invoice_quantity = float(r["invoice_quantity"] or 0)
        difference_value = float(r["quantity_difference"] or 0)
        summary = (
            f"{r['product_name'] or 'Prodotto'}: consegnati {ddt_quantity:g}, "
            f"fatturati {invoice_quantity:g}"
        )
        item = {
            "key": key,
            "type": "DDT_QUANTITY",
            "source_id": source_id,
            "summary": summary,
            "product_name": r["product_name"] or "Prodotto",
            "invoice_value": invoice_quantity,
            "reference_value": ddt_quantity,
            "difference_value": difference_value,
            "original_status": "DA_VERIFICARE",
            "verified": key in verified,
            "verification": verified.get(key),
        }
        anomalies.append(item)

    missing_rows = conn.execute(
        """
        SELECT id,reference_number,reference_date
        FROM invoice_ddt_references
        WHERE invoice_document_id=? AND delivery_note_id IS NULL
        ORDER BY id
        """,
        (document_id,),
    ).fetchall()

    for row in missing_rows:
        r = dict(row)
        key = f"DDT_MISSING:{r['id']}"
        summary = f"DDT {r['reference_number']} non ancora acquisito"
        if r.get("reference_date"):
            summary += f" ({r['reference_date']})"
        item = {
            "key": key,
            "type": "DDT_MISSING",
            "source_id": str(r["id"]),
            "summary": summary,
            "product_name": None,
            "invoice_value": None,
            "reference_value": None,
            "difference_value": None,
            "original_status": "DA_VERIFICARE",
            "verified": key in verified,
            "verification": verified.get(key),
        }
        anomalies.append(item)

    return anomalies


def open_document_anomalies(conn, document_id):
    return [item for item in load_document_anomalies(conn, document_id) if not item["verified"]]


def verify_document_anomaly(conn, document_id, anomaly_key, user, note="", resolution=None):
    anomalies = load_document_anomalies(conn, document_id)
    target = next((item for item in anomalies if item["key"] == anomaly_key), None)
    if not target:
        raise ValueError("Anomalia non trovata o non più presente.")

    if target["verified"]:
        raise ValueError("Questa anomalia risulta già verificata.")

    resolution = str(resolution or "").strip().upper()
    reference_history_id = None

    if target["type"] == "PRICE":
        if resolution not in {"ERRORE_FORNITORE", "NUOVO_PREZZO"}:
            raise ValueError(
                "Per una differenza prezzo scegli se si tratta di un errore del fornitore "
                "oppure di un nuovo prezzo concordato."
            )
        if resolution == "NUOVO_PREZZO":
            reference_history_id = accept_invoice_price_update(
                conn,
                document_id,
                int(target["source_id"]),
                user=user,
                note=note,
            )
    else:
        resolution = resolution or "VERIFICATA"

    details = {
        key: value
        for key, value in target.items()
        if key not in {"verified", "verification"}
    }
    details["resolution"] = resolution
    if reference_history_id:
        details["reference_history_id"] = reference_history_id

    user = user or {}
    verified_name = str(user.get("full_name") or "").strip() or str(user.get("email") or "").strip()

    conn.execute(
        """
        INSERT INTO anomaly_verifications(
            document_id,anomaly_key,anomaly_type,source_id,original_status,summary,
            invoice_value,reference_value,difference_value,details_json,
            verified_at,verified_by_user_id,verified_by_name,verified_by_email,note,resolution
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,?,?,?,?,?)
        """,
        (
            document_id,
            target["key"],
            target["type"],
            target.get("source_id"),
            target.get("original_status") or "DA_VERIFICARE",
            target["summary"],
            target.get("invoice_value"),
            target.get("reference_value"),
            target.get("difference_value"),
            json.dumps(details, ensure_ascii=False),
            user.get("id"),
            verified_name or None,
            user.get("email"),
            str(note or "").strip() or None,
            resolution,
        ),
    )

    refreshed = load_document_anomalies(conn, document_id)
    open_count = sum(1 for item in refreshed if not item["verified"])
    verified_count = sum(1 for item in refreshed if item["verified"])

    baseline_history_ids = []
    if refreshed and open_count == 0:
        baseline_history_ids = establish_invoice_baselines(conn, document_id, user=user)
        conn.execute(
            """
            UPDATE documents
            SET workflow_status='GESTITO',
                handled_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (document_id,),
        )

    return {
        "document_id": document_id,
        "anomaly_key": anomaly_key,
        "resolution": resolution,
        "price_reference_updated": bool(reference_history_id),
        "reference_history_id": reference_history_id,
        "baseline_prices_created": len(baseline_history_ids),
        "open_count": open_count,
        "verified_count": verified_count,
        "all_verified": bool(refreshed) and open_count == 0,
    }
