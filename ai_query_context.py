import re
import unicodedata
from datetime import date

from db import get_connection


STOPWORDS = {
    "a", "ad", "al", "alla", "alle", "allo", "anche", "che", "chi", "con", "da", "dal",
    "dalla", "dalle", "dei", "del", "della", "delle", "di", "e", "ed", "gli", "ha", "hai",
    "ho", "i", "il", "in", "io", "l", "la", "le", "lo", "mi", "ne", "nel", "nella", "nelle",
    "o", "per", "piu", "qual", "quale", "quali", "quando", "quanto", "quanta", "quanti", "quante",
    "questo", "questa", "questi", "queste", "su", "tra", "un", "una", "uno", "ultima", "ultimo",
    "ultimi", "ultime", "volta", "vorrei", "voglio", "fammi", "mostrami", "cerca", "cercami",
    "acquistato", "acquistata", "acquistati", "acquistate", "acquisto", "acquisti", "comprare", "comprato",
    "prodotto", "prodotti", "fornitore", "fornitori", "prezzo", "prezzi", "listino", "listini"
}


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _plain(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _tokens(value):
    return {
        token for token in _plain(value).split()
        if len(token) >= 3 and token not in STOPWORDS
    }


def _score_alias(question_plain, question_tokens, alias, is_code=False):
    alias_plain = _plain(alias)
    if not alias_plain:
        return 0

    alias_tokens = {t for t in alias_plain.split() if len(t) >= 2}
    if not alias_tokens:
        return 0

    if is_code:
        if alias_plain in question_plain.split():
            return 180
        if alias_plain and alias_plain in question_plain:
            return 150

    score = 0
    if len(alias_plain) >= 3 and alias_plain in question_plain:
        score += 120 + min(len(alias_plain), 40)

    overlap = alias_tokens.intersection(question_tokens)
    if overlap:
        score += len(overlap) * 25
        score += int(40 * len(overlap) / max(len(alias_tokens), 1))
        if alias_tokens.issubset(question_tokens):
            score += 40

    return score


def _product_candidates(conn, question):
    q_plain = _plain(question)
    q_tokens = _tokens(question)
    products = _rows(
        conn,
        "SELECT id,name,normalized_name,category,unit FROM products ORDER BY name",
    )
    aliases = _rows(
        conn,
        """
        SELECT product_id,supplier_code,original_description
        FROM supplier_products
        """,
    )

    by_product = {}
    for row in aliases:
        by_product.setdefault(str(row["product_id"]), []).append(row)

    scored = []
    for product in products:
        score = max(
            _score_alias(q_plain, q_tokens, product.get("name")),
            _score_alias(q_plain, q_tokens, product.get("normalized_name")),
        )
        matched_on = product.get("name") or ""

        for alias in by_product.get(str(product["id"]), []):
            code_score = _score_alias(
                q_plain,
                q_tokens,
                alias.get("supplier_code"),
                is_code=True,
            )
            desc_score = _score_alias(
                q_plain,
                q_tokens,
                alias.get("original_description"),
            )
            if code_score > score:
                score = code_score
                matched_on = alias.get("supplier_code") or matched_on
            if desc_score > score:
                score = desc_score
                matched_on = alias.get("original_description") or matched_on

        if score > 0:
            scored.append({**product, "match_score": score, "matched_on": matched_on})

    scored.sort(key=lambda item: (-item["match_score"], str(item["name"])))
    if not scored:
        return []

    top = scored[0]["match_score"]
    threshold = max(45, int(top * 0.62))
    return [item for item in scored if item["match_score"] >= threshold][:5]


def _supplier_candidates(conn, question):
    q_plain = _plain(question)
    q_tokens = _tokens(question)
    suppliers = _rows(
        conn,
        "SELECT id,name,vat_number,category FROM suppliers ORDER BY name",
    )

    scored = []
    for supplier in suppliers:
        score = _score_alias(q_plain, q_tokens, supplier.get("name"))
        vat_score = _score_alias(
            q_plain,
            q_tokens,
            supplier.get("vat_number"),
            is_code=True,
        )
        score = max(score, vat_score)
        if score > 0:
            scored.append({**supplier, "match_score": score})

    scored.sort(key=lambda item: (-item["match_score"], str(item["name"])))
    if not scored:
        return []

    top = scored[0]["match_score"]
    threshold = max(45, int(top * 0.62))
    return [item for item in scored if item["match_score"] >= threshold][:5]


def _global_context(conn):
    counts = {}
    for table in ("suppliers", "products", "price_lists", "documents", "purchases"):
        counts[table] = int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"] or 0)

    spend = dict(conn.execute(
        """
        SELECT
            ROUND(COALESCE(SUM(quantity*unit_price),0),2) AS total_spend,
            MIN(purchase_date) AS first_purchase,
            MAX(purchase_date) AS last_purchase
        FROM purchases
        """
    ).fetchone())

    monthly = _rows(
        conn,
        """
        SELECT substr(purchase_date,1,7) AS month,
               ROUND(SUM(quantity*unit_price),2) AS spend
        FROM purchases
        WHERE purchase_date IS NOT NULL
        GROUP BY substr(purchase_date,1,7)
        ORDER BY month
        """,
    )

    anomaly_summary = dict(conn.execute(
        """
        SELECT
            COUNT(DISTINCT CASE WHEN dl.price_check_status='DIFFERENZA' THEN d.id END)
                AS invoices_with_differences,
            SUM(CASE WHEN dl.price_check_status='DIFFERENZA' THEN 1 ELSE 0 END)
                AS anomaly_lines,
            ROUND(COALESCE(SUM(CASE
                WHEN dl.price_check_status='DIFFERENZA' AND dl.price_variance_total > 0
                THEN dl.price_variance_total ELSE 0 END),0),2) AS extra_cost_total,
            ROUND(COALESCE(SUM(CASE
                WHEN dl.price_check_status='DIFFERENZA' AND dl.price_variance_total < 0
                THEN ABS(dl.price_variance_total) ELSE 0 END),0),2) AS savings_total
        FROM documents d
        JOIN document_lines dl ON dl.document_id=d.id
        WHERE d.document_type NOT IN ('TD04','TD08','TD09')
        """
    ).fetchone())

    supplier_spend = _rows(
        conn,
        """
        SELECT s.id,s.name,
               COUNT(*) AS purchase_lines,
               ROUND(SUM(p.quantity*p.unit_price),2) AS spend,
               MAX(p.purchase_date) AS last_purchase
        FROM purchases p
        JOIN suppliers s ON s.id=p.supplier_id
        GROUP BY s.id,s.name
        ORDER BY spend DESC
        LIMIT 30
        """,
    )

    latest_purchases = _rows(
        conn,
        """
        SELECT x.purchase_date,s.name AS supplier,p.name AS product,
               x.quantity,x.unit_price,
               ROUND(x.quantity*x.unit_price,2) AS total
        FROM purchases x
        JOIN suppliers s ON s.id=x.supplier_id
        JOIN products p ON p.id=x.product_id
        ORDER BY x.purchase_date DESC,x.id DESC
        LIMIT 25
        """,
    )

    return {
        "database_counts": counts,
        "purchase_period_and_total": spend,
        "monthly_spend_all_periods": monthly,
        "invoice_anomaly_summary_all_periods": anomaly_summary,
        "top_supplier_spend": supplier_spend,
        "latest_purchases_global": latest_purchases,
    }


def _product_context(conn, product):
    product_id = product["id"]

    stats = dict(conn.execute(
        """
        SELECT
            COUNT(*) AS purchase_lines,
            ROUND(COALESCE(SUM(quantity),0),4) AS total_quantity,
            ROUND(COALESCE(SUM(quantity*unit_price),0),2) AS total_spend,
            ROUND(COALESCE(AVG(unit_price),0),4) AS average_price,
            ROUND(COALESCE(MIN(unit_price),0),4) AS minimum_price,
            ROUND(COALESCE(MAX(unit_price),0),4) AS maximum_price,
            MIN(purchase_date) AS first_purchase,
            MAX(purchase_date) AS last_purchase
        FROM purchases
        WHERE product_id=?
        """,
        (product_id,),
    ).fetchone())

    latest_purchase = conn.execute(
        """
        SELECT x.purchase_date,s.id AS supplier_id,s.name AS supplier,
               x.quantity,x.unit_price,
               ROUND(x.quantity*x.unit_price,2) AS total,
               x.source_document_id
        FROM purchases x
        JOIN suppliers s ON s.id=x.supplier_id
        WHERE x.product_id=?
        ORDER BY x.purchase_date DESC,x.id DESC
        LIMIT 1
        """,
        (product_id,),
    ).fetchone()

    supplier_purchase_summary = _rows(
        conn,
        """
        SELECT s.id AS supplier_id,s.name AS supplier,
               COUNT(*) AS purchase_lines,
               ROUND(SUM(x.quantity),4) AS total_quantity,
               ROUND(SUM(x.quantity*x.unit_price),2) AS total_spend,
               ROUND(AVG(x.unit_price),4) AS average_price,
               ROUND(MIN(x.unit_price),4) AS minimum_price,
               ROUND(MAX(x.unit_price),4) AS maximum_price,
               MAX(x.purchase_date) AS last_purchase
        FROM purchases x
        JOIN suppliers s ON s.id=x.supplier_id
        WHERE x.product_id=?
        GROUP BY s.id,s.name
        ORDER BY last_purchase DESC,s.name
        """,
        (product_id,),
    )

    recent_purchase_history = _rows(
        conn,
        """
        SELECT x.purchase_date,s.name AS supplier,x.quantity,x.unit_price,
               ROUND(x.quantity*x.unit_price,2) AS total,
               x.source_document_id
        FROM purchases x
        JOIN suppliers s ON s.id=x.supplier_id
        WHERE x.product_id=?
        ORDER BY x.purchase_date DESC,x.id DESC
        LIMIT 25
        """,
        (product_id,),
    )

    supplier_codes = _rows(
        conn,
        """
        SELECT sp.supplier_code,sp.original_description,s.name AS supplier
        FROM supplier_products sp
        JOIN suppliers s ON s.id=sp.supplier_id
        WHERE sp.product_id=?
        ORDER BY s.name,sp.supplier_code
        """,
        (product_id,),
    )

    all_price_rows = _rows(
        conn,
        """
        SELECT pl.id AS price_list_id,pl.imported_at,s.id AS supplier_id,s.name AS supplier,
               pll.supplier_code,pll.list_price,pll.discount_1,pll.discount_2,pll.net_price
        FROM price_list_lines pll
        JOIN price_lists pl ON pl.id=pll.price_list_id
        JOIN suppliers s ON s.id=pl.supplier_id
        WHERE pll.product_id=?
        ORDER BY pl.imported_at DESC,pl.id DESC,pll.id DESC
        """,
        (product_id,),
    )

    latest_by_supplier = []
    seen_suppliers = set()
    for row in all_price_rows:
        sid = str(row.get("supplier_id"))
        if sid in seen_suppliers:
            continue
        seen_suppliers.add(sid)
        latest_by_supplier.append(row)

    anomaly_summary = dict(conn.execute(
        """
        SELECT
            COUNT(*) AS anomaly_lines,
            ROUND(COALESCE(SUM(CASE WHEN dl.price_variance_total>0 THEN dl.price_variance_total ELSE 0 END),0),2)
                AS extra_cost_total,
            ROUND(COALESCE(SUM(CASE WHEN dl.price_variance_total<0 THEN ABS(dl.price_variance_total) ELSE 0 END),0),2)
                AS savings_total
        FROM document_lines dl
        JOIN documents d ON d.id=dl.document_id
        WHERE dl.product_id=? AND dl.price_check_status='DIFFERENZA'
          AND d.document_type NOT IN ('TD04','TD08','TD09')
        """,
        (product_id,),
    ).fetchone())

    anomaly_details = _rows(
        conn,
        """
        SELECT d.document_date,d.document_number,s.name AS supplier,
               dl.quantity,dl.unit,dl.invoice_net_price,dl.expected_net_price,
               dl.price_variance_unit,dl.price_variance_total
        FROM document_lines dl
        JOIN documents d ON d.id=dl.document_id
        JOIN suppliers s ON s.id=d.supplier_id
        WHERE dl.product_id=? AND dl.price_check_status='DIFFERENZA'
        ORDER BY d.document_date DESC,d.id DESC
        LIMIT 25
        """,
        (product_id,),
    )

    return {
        "product": {
            "id": product["id"],
            "name": product["name"],
            "category": product.get("category"),
            "unit": product.get("unit"),
            "matched_on": product.get("matched_on"),
            "match_score": product.get("match_score"),
        },
        "purchase_statistics_full_history": stats,
        "latest_purchase_exact": dict(latest_purchase) if latest_purchase else None,
        "all_suppliers_used_for_product": supplier_purchase_summary,
        "recent_purchase_history": recent_purchase_history,
        "supplier_codes": supplier_codes,
        "latest_price_list_per_supplier": latest_by_supplier,
        "price_list_suppliers_count": len(latest_by_supplier),
        "anomaly_summary_full_history": anomaly_summary,
        "recent_anomaly_details": anomaly_details,
    }


def _supplier_context(conn, supplier):
    supplier_id = supplier["id"]

    stats = dict(conn.execute(
        """
        SELECT COUNT(*) AS purchase_lines,
               COUNT(DISTINCT product_id) AS products_purchased,
               ROUND(COALESCE(SUM(quantity*unit_price),0),2) AS total_spend,
               MIN(purchase_date) AS first_purchase,
               MAX(purchase_date) AS last_purchase
        FROM purchases
        WHERE supplier_id=?
        """,
        (supplier_id,),
    ).fetchone())

    product_rows = _rows(
        conn,
        """
        SELECT p.id,p.name,p.unit,
               COUNT(*) AS purchase_lines,
               ROUND(SUM(x.quantity),4) AS total_quantity,
               ROUND(SUM(x.quantity*x.unit_price),2) AS total_spend,
               ROUND(AVG(x.unit_price),4) AS average_price,
               MAX(x.purchase_date) AS last_purchase
        FROM purchases x
        JOIN products p ON p.id=x.product_id
        WHERE x.supplier_id=?
        GROUP BY p.id,p.name,p.unit
        ORDER BY total_spend DESC,p.name
        LIMIT 200
        """,
        (supplier_id,),
    )

    total_products = int(conn.execute(
        "SELECT COUNT(DISTINCT product_id) AS c FROM purchases WHERE supplier_id=?",
        (supplier_id,),
    ).fetchone()["c"] or 0)

    recent_purchases = _rows(
        conn,
        """
        SELECT x.purchase_date,p.name AS product,x.quantity,x.unit_price,
               ROUND(x.quantity*x.unit_price,2) AS total,x.source_document_id
        FROM purchases x
        JOIN products p ON p.id=x.product_id
        WHERE x.supplier_id=?
        ORDER BY x.purchase_date DESC,x.id DESC
        LIMIT 30
        """,
        (supplier_id,),
    )

    latest_list = conn.execute(
        """
        SELECT id,file_name,imported_at,status,rows_count
        FROM price_lists
        WHERE supplier_id=?
        ORDER BY imported_at DESC,id DESC
        LIMIT 1
        """,
        (supplier_id,),
    ).fetchone()

    return {
        "supplier": {
            "id": supplier["id"],
            "name": supplier["name"],
            "vat_number": supplier.get("vat_number"),
            "category": supplier.get("category"),
            "match_score": supplier.get("match_score"),
        },
        "purchase_statistics_full_history": stats,
        "products_purchased_ranked": product_rows,
        "products_purchased_total": total_products,
        "products_list_truncated": total_products > len(product_rows),
        "recent_purchases": recent_purchases,
        "latest_price_list": dict(latest_list) if latest_list else None,
    }


def build_question_context(question):
    """
    Costruisce un contesto mirato interrogando il database aziendale completo.
    I calcoli aggregati e la ricerca delle entità usano l'intero database; solo
    gli elenchi di dettaglio molto lunghi vengono limitati per non saturare il prompt.
    """
    conn = get_connection()
    try:
        products = _product_candidates(conn, question)
        suppliers = _supplier_candidates(conn, question)

        context = {
            "snapshot_date": date.today().isoformat(),
            "query_scope": {
                "entity_search": "database completo",
                "aggregations": "database completo",
                "write_access": False,
                "detail_lists_note": (
                    "Le statistiche, ultimo acquisto, minimo, massimo, medie, totali e fornitori del prodotto "
                    "sono calcolati sull'intero database. Alcuni elenchi recenti sono limitati solo per compattezza."
                ),
            },
            "global": _global_context(conn),
            "matched_products": [
                {"id": p["id"], "name": p["name"], "match_score": p["match_score"], "matched_on": p["matched_on"]}
                for p in products
            ],
            "matched_suppliers": [
                {"id": s["id"], "name": s["name"], "match_score": s["match_score"]}
                for s in suppliers
            ],
            "product_details": [_product_context(conn, p) for p in products],
            "supplier_details": [_supplier_context(conn, s) for s in suppliers],
        }
        return context
    finally:
        conn.close()
