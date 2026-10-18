"""C3.2 - riferimenti prezzo versionati.

Il riferimento prezzo non viene mai sovrascritto: ogni variazione accettata crea
una nuova riga. I listini storici restano intatti e le fatture possono creare
una base iniziale quando non esiste ancora alcun listino/riferimento.
"""


PRICE_TOLERANCE = 0.005


def _to_float(value):
    return float(value) if value is not None else None


def current_price_reference(conn, supplier_id, product_id):
    """Return the current reference price, preferring the C3.2 history."""
    row = conn.execute(
        """
        SELECT h.id,h.net_price,h.list_price,h.discount_1,h.discount_2,h.unit,
               h.source_type,h.effective_from,h.previous_net_price,
               h.variation_value,h.variation_pct,h.created_at
        FROM price_reference_history h
        WHERE h.supplier_id=? AND h.product_id=?
        ORDER BY h.id DESC
        LIMIT 1
        """,
        (supplier_id, product_id),
    ).fetchone()
    if row:
        result = dict(row)
        for key in (
            "net_price", "list_price", "discount_1", "discount_2",
            "previous_net_price", "variation_value", "variation_pct",
        ):
            result[key] = _to_float(result.get(key))
        return result

    # Compatibilità con i listini caricati prima della C3.2.
    row = conn.execute(
        """
        SELECT pll.net_price,pll.list_price,pll.discount_1,pll.discount_2,pll.unit,
               pl.imported_at AS effective_from
        FROM price_list_lines pll
        JOIN price_lists pl ON pl.id=pll.price_list_id
        WHERE pl.supplier_id=? AND pll.product_id=? AND pll.net_price IS NOT NULL
        ORDER BY pl.imported_at DESC, pl.id DESC, pll.id DESC
        LIMIT 1
        """,
        (supplier_id, product_id),
    ).fetchone()
    if not row:
        return None

    result = dict(row)
    for key in ("net_price", "list_price", "discount_1", "discount_2"):
        result[key] = _to_float(result.get(key))
    result.update({
        "id": None,
        "source_type": "LISTINO_LEGACY",
        "previous_net_price": None,
        "variation_value": None,
        "variation_pct": None,
        "created_at": result.get("effective_from"),
    })
    return result


def record_price_reference(
    conn,
    *,
    supplier_id,
    product_id,
    net_price,
    list_price=None,
    discount_1=0,
    discount_2=0,
    unit=None,
    source_type,
    source_price_list_id=None,
    source_price_list_line_id=None,
    source_document_id=None,
    source_document_line_id=None,
    effective_from=None,
    user=None,
    note=None,
    previous_reference=None,
    force_record=False,
):
    """Append a reference price change without deleting the previous value."""
    net_price = _to_float(net_price)
    if net_price is None or net_price <= 0:
        return None

    if source_price_list_line_id is not None:
        existing = conn.execute(
            """
            SELECT id FROM price_reference_history
            WHERE source_type=? AND source_price_list_line_id=?
            """,
            (source_type, source_price_list_line_id),
        ).fetchone()
        if existing:
            return existing["id"]

    if source_document_line_id is not None:
        existing = conn.execute(
            """
            SELECT id FROM price_reference_history
            WHERE source_type=? AND source_document_line_id=?
            """,
            (source_type, source_document_line_id),
        ).fetchone()
        if existing:
            return existing["id"]

    previous = previous_reference
    if previous is None:
        previous = current_price_reference(conn, supplier_id, product_id)

    previous_net = _to_float(previous.get("net_price")) if previous else None

    # Una riga nuova serve solo quando nasce il riferimento o cambia il netto.
    if (not force_record) and previous_net is not None and abs(net_price - previous_net) <= PRICE_TOLERANCE:
        return None

    variation_value = None
    variation_pct = None
    if previous_net is not None:
        variation_value = round(net_price - previous_net, 4)
        if abs(previous_net) > 0.0000001:
            variation_pct = round((variation_value / previous_net) * 100, 4)

    user = user or {}
    cur = conn.execute(
        """
        INSERT INTO price_reference_history(
            supplier_id,product_id,net_price,list_price,discount_1,discount_2,unit,
            source_type,source_price_list_id,source_price_list_line_id,
            source_document_id,source_document_line_id,effective_from,
            previous_net_price,variation_value,variation_pct,
            created_by_user_id,created_by_name,created_by_email,note
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            supplier_id,
            product_id,
            net_price,
            _to_float(list_price),
            _to_float(discount_1) or 0,
            _to_float(discount_2) or 0,
            unit,
            source_type,
            source_price_list_id,
            source_price_list_line_id,
            source_document_id,
            source_document_line_id,
            effective_from,
            previous_net,
            variation_value,
            variation_pct,
            user.get("id"),
            str(user.get("full_name") or "").strip() or None,
            str(user.get("email") or "").strip() or None,
            str(note or "").strip() or None,
        ),
    )
    return cur.lastrowid


def establish_invoice_baselines(conn, document_id, user=None):
    """Use a validated invoice as the initial price base where no reference exists."""
    doc = conn.execute(
        """
        SELECT id,supplier_id,document_date,document_type
        FROM documents
        WHERE id=?
        """,
        (document_id,),
    ).fetchone()
    if not doc or not doc["supplier_id"]:
        return []

    if doc["document_type"] in ("TD04", "TD08", "TD09"):
        return []

    lines = conn.execute(
        """
        SELECT id,product_id,unit,invoice_list_price,invoice_discount_1,
               invoice_discount_2,invoice_net_price,price_check_status
        FROM document_lines
        WHERE document_id=?
          AND COALESCE(line_type,'PRODUCT')='PRODUCT'
          AND product_id IS NOT NULL
          AND invoice_net_price IS NOT NULL
          AND price_check_status='SENZA_LISTINO'
        ORDER BY line_number,id
        """,
        (document_id,),
    ).fetchall()

    created = []
    established_products = set()
    for row in lines:
        product_id = row["product_id"]
        if product_id in established_products:
            continue

        existing = current_price_reference(conn, doc["supplier_id"], product_id)
        if existing is not None:
            continue

        history_id = record_price_reference(
            conn,
            supplier_id=doc["supplier_id"],
            product_id=product_id,
            net_price=row["invoice_net_price"],
            list_price=row["invoice_list_price"],
            discount_1=row["invoice_discount_1"],
            discount_2=row["invoice_discount_2"],
            unit=row["unit"],
            source_type="FATTURA_BASE",
            source_document_id=document_id,
            source_document_line_id=row["id"],
            effective_from=doc["document_date"],
            user=user,
            note="Base prezzo creata dalla prima fattura validata.",
            previous_reference=None,
        )
        if history_id:
            created.append(history_id)
            established_products.add(product_id)
            conn.execute(
                """
                UPDATE document_lines
                SET expected_net_price=invoice_net_price,
                    price_variance_unit=0,
                    price_variance_total=0,
                    price_check_status='BASE_PREZZO'
                WHERE id=?
                """,
                (row["id"],),
            )

    return created


def accept_invoice_price_update(conn, document_id, document_line_id, user=None, note=None):
    """Promote the invoice price to the new current reference after user approval."""
    row = conn.execute(
        """
        SELECT d.id AS document_id,d.supplier_id,d.document_date,d.document_type,
               dl.id AS line_id,dl.product_id,dl.unit,dl.invoice_list_price,
               dl.invoice_discount_1,dl.invoice_discount_2,dl.invoice_net_price,
               dl.expected_net_price,dl.price_check_status
        FROM documents d
        JOIN document_lines dl ON dl.document_id=d.id
        WHERE d.id=? AND dl.id=?
        """,
        (document_id, document_line_id),
    ).fetchone()
    if not row:
        raise ValueError("Riga prezzo non trovata.")
    if row["document_type"] in ("TD04", "TD08", "TD09"):
        raise ValueError("Le note di credito non possono aggiornare il riferimento prezzo.")
    if row["price_check_status"] != "DIFFERENZA":
        raise ValueError("La riga non contiene una differenza prezzo da aggiornare.")
    if not row["supplier_id"] or not row["product_id"] or row["invoice_net_price"] is None:
        raise ValueError("Dati prezzo insufficienti per aggiornare il riferimento.")

    previous = {
        "net_price": _to_float(row["expected_net_price"])
    } if row["expected_net_price"] is not None else current_price_reference(
        conn, row["supplier_id"], row["product_id"]
    )

    return record_price_reference(
        conn,
        supplier_id=row["supplier_id"],
        product_id=row["product_id"],
        net_price=row["invoice_net_price"],
        list_price=row["invoice_list_price"],
        discount_1=row["invoice_discount_1"],
        discount_2=row["invoice_discount_2"],
        unit=row["unit"],
        source_type="FATTURA_AGGIORNAMENTO",
        source_document_id=document_id,
        source_document_line_id=document_line_id,
        effective_from=row["document_date"],
        user=user,
        note=note or "Nuovo prezzo concordato e accettato.",
        previous_reference=previous,
    )


def backfill_price_list_history(conn):
    """Migrate pre-C3.2 price lists into ordered version history once."""
    existing_count = conn.execute(
        "SELECT COUNT(*) AS c FROM price_reference_history"
    ).fetchone()["c"]
    if existing_count:
        return 0

    rows = conn.execute(
        """
        SELECT pll.id AS line_id,pll.price_list_id,pl.supplier_id,pl.imported_at,
               pll.product_id,pll.net_price,pll.list_price,pll.discount_1,
               pll.discount_2,pll.unit
        FROM price_list_lines pll
        JOIN price_lists pl ON pl.id=pll.price_list_id
        WHERE pll.product_id IS NOT NULL
          AND pll.net_price IS NOT NULL
        ORDER BY pl.imported_at,pl.id,pll.id
        """
    ).fetchall()

    last_by_product = {}
    added = 0
    for row in rows:
        key = (row["supplier_id"], row["product_id"])
        previous_net = last_by_product.get(key)
        previous = {"net_price": previous_net} if previous_net is not None else None
        history_id = record_price_reference(
            conn,
            supplier_id=row["supplier_id"],
            product_id=row["product_id"],
            net_price=row["net_price"],
            list_price=row["list_price"],
            discount_1=row["discount_1"],
            discount_2=row["discount_2"],
            unit=row["unit"],
            source_type="LISTINO",
            source_price_list_id=row["price_list_id"],
            source_price_list_line_id=row["line_id"],
            effective_from=row["imported_at"],
            note="Storico ricostruito da listino importato.",
            previous_reference=previous,
            force_record=True,
        )
        if history_id:
            added += 1
        last_by_product[key] = _to_float(row["net_price"])
    return added
