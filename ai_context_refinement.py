import ai_query_context as base
from db import get_connection


def _fallback_product_candidates(conn, question):
    """Secondo passaggio più permissivo per nomi parziali come 'caffè' o 'guanti'."""
    q_plain = base._plain(question)
    q_tokens = base._tokens(question)
    if not q_tokens:
        return []

    products = base._rows(
        conn,
        "SELECT id,name,normalized_name,category,unit FROM products ORDER BY name",
    )
    aliases = base._rows(
        conn,
        "SELECT product_id,supplier_code,original_description FROM supplier_products",
    )

    by_product = {}
    for row in aliases:
        by_product.setdefault(str(row["product_id"]), []).append(row)

    scored = []
    for product in products:
        score = max(
            base._score_alias(q_plain, q_tokens, product.get("name")),
            base._score_alias(q_plain, q_tokens, product.get("normalized_name")),
        )
        matched_on = product.get("name") or ""

        for alias in by_product.get(str(product["id"]), []):
            code_score = base._score_alias(
                q_plain, q_tokens, alias.get("supplier_code"), is_code=True
            )
            desc_score = base._score_alias(
                q_plain, q_tokens, alias.get("original_description")
            )
            if code_score > score:
                score = code_score
                matched_on = alias.get("supplier_code") or matched_on
            if desc_score > score:
                score = desc_score
                matched_on = alias.get("original_description") or matched_on

        if score >= 30:
            scored.append({**product, "match_score": score, "matched_on": matched_on})

    scored.sort(key=lambda item: (-item["match_score"], str(item["name"])))
    if not scored:
        return []

    top = scored[0]["match_score"]
    threshold = max(30, int(top * 0.62))
    return [item for item in scored if item["match_score"] >= threshold][:5]


def _current_price_lists_for_product(conn, product_id):
    """Restituisce il prodotto solo se è presente nell'ultimo listino di ciascun fornitore."""
    return base._rows(
        conn,
        """
        SELECT pl.id AS price_list_id,pl.imported_at,
               s.id AS supplier_id,s.name AS supplier,
               pll.supplier_code,pll.list_price,
               pll.discount_1,pll.discount_2,pll.net_price
        FROM price_list_lines pll
        JOIN price_lists pl ON pl.id=pll.price_list_id
        JOIN suppliers s ON s.id=pl.supplier_id
        WHERE pll.product_id=?
          AND pl.id=(
              SELECT pl2.id
              FROM price_lists pl2
              WHERE pl2.supplier_id=pl.supplier_id
              ORDER BY pl2.imported_at DESC,pl2.id DESC
              LIMIT 1
          )
        ORDER BY s.name,pll.id
        """,
        (product_id,),
    )


def refine_question_context(question, context):
    conn = get_connection()
    try:
        if not context.get("matched_products"):
            products = _fallback_product_candidates(conn, question)
            if products:
                context["matched_products"] = [
                    {
                        "id": p["id"],
                        "name": p["name"],
                        "match_score": p["match_score"],
                        "matched_on": p["matched_on"],
                    }
                    for p in products
                ]
                context["product_details"] = [
                    base._product_context(conn, product) for product in products
                ]

        for detail in context.get("product_details", []):
            stats = detail.get("purchase_statistics_full_history") or {}
            if int(stats.get("purchase_lines") or 0) == 0:
                stats["average_price"] = None
                stats["minimum_price"] = None
                stats["maximum_price"] = None
                stats["first_purchase"] = None
                stats["last_purchase"] = None

            product_id = detail.get("product", {}).get("id")
            if product_id:
                current_lists = _current_price_lists_for_product(conn, product_id)
                detail["latest_price_list_per_supplier"] = current_lists
                detail["price_list_suppliers_count"] = len(current_lists)
                detail["price_list_scope"] = (
                    "Solo l'ultimo listino importato di ciascun fornitore; "
                    "un fornitore compare solo se il prodotto è ancora presente in quel listino."
                )

        context.setdefault("query_scope", {})["product_name_matching"] = (
            "ricerca completa con supporto a nomi e codici articolo parziali"
        )
        context["query_scope"]["price_list_scope"] = (
            "ultimo listino importato per ciascun fornitore"
        )
        return context
    finally:
        conn.close()
