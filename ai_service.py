import json
import os
from datetime import date

from openai import OpenAI

from db import get_connection


MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def build_business_snapshot():
    """
    Costruisce un contesto controllato e di sola lettura.
    L'AI non riceve accesso SQL diretto e non può modificare il database.
    """
    conn = get_connection()
    try:
        months = [
            r["month"] for r in conn.execute(
                """
                SELECT DISTINCT substr(purchase_date,1,7) AS month
                FROM purchases
                WHERE purchase_date IS NOT NULL
                ORDER BY month
                """
            ).fetchall()
            if r["month"]
        ]
        current_month = months[-1] if months else None
        previous_month = months[-2] if len(months) > 1 else None

        totals = {}
        for month in [m for m in (current_month, previous_month) if m]:
            row = conn.execute(
                """
                SELECT ROUND(COALESCE(SUM(quantity*unit_price),0),2) AS total
                FROM purchases
                WHERE substr(purchase_date,1,7)=?
                """,
                (month,),
            ).fetchone()
            totals[month] = float(row["total"] or 0)

        anomaly_summary = dict(conn.execute(
            """
            SELECT
                COUNT(DISTINCT CASE WHEN dl.price_check_status='DIFFERENZA' THEN d.id END)
                    AS invoices_with_differences,
                SUM(CASE WHEN dl.price_check_status='DIFFERENZA' THEN 1 ELSE 0 END)
                    AS anomaly_lines,
                ROUND(COALESCE(SUM(CASE
                    WHEN dl.price_check_status='DIFFERENZA'
                     AND dl.price_variance_total > 0
                    THEN dl.price_variance_total ELSE 0 END),0),2)
                    AS extra_cost_total,
                ROUND(COALESCE(SUM(CASE
                    WHEN dl.price_check_status='DIFFERENZA'
                     AND dl.price_variance_total < 0
                    THEN ABS(dl.price_variance_total) ELSE 0 END),0),2)
                    AS savings_total
            FROM documents d
            JOIN document_lines dl ON dl.document_id=d.id
            WHERE d.document_type NOT IN ('TD04','TD08','TD09')
            """
        ).fetchone())

        anomalies = _rows(
            conn,
            """
            SELECT
                d.document_number,
                d.document_date,
                s.name AS supplier,
                COALESCE(p.name, dl.original_description) AS product,
                dl.quantity,
                dl.unit,
                ROUND(dl.invoice_net_price,4) AS invoice_net_price,
                ROUND(dl.expected_net_price,4) AS expected_net_price,
                ROUND(dl.price_variance_unit,4) AS variance_unit,
                ROUND(dl.price_variance_total,2) AS variance_total
            FROM document_lines dl
            JOIN documents d ON d.id=dl.document_id
            JOIN suppliers s ON s.id=d.supplier_id
            LEFT JOIN products p ON p.id=dl.product_id
            WHERE dl.price_check_status='DIFFERENZA'
            ORDER BY ABS(COALESCE(dl.price_variance_total,0)) DESC
            LIMIT 50
            """
        )

        supplier_spend = _rows(
            conn,
            """
            SELECT s.name AS supplier,
                   ROUND(SUM(p.quantity*p.unit_price),2) AS spend,
                   COUNT(*) AS purchase_lines
            FROM purchases p
            JOIN suppliers s ON s.id=p.supplier_id
            GROUP BY s.id,s.name
            ORDER BY spend DESC
            LIMIT 50
            """
        )

        product_stats = _rows(
            conn,
            """
            SELECT p.id,p.name,p.category,
                   ROUND(COALESCE(SUM(x.quantity*x.unit_price),0),2) AS spend,
                   ROUND(COALESCE(AVG(x.unit_price),0),4) AS avg_price,
                   ROUND(COALESCE(MIN(x.unit_price),0),4) AS min_price,
                   ROUND(COALESCE(MAX(x.unit_price),0),4) AS max_price,
                   MAX(x.purchase_date) AS last_purchase
            FROM products p
            LEFT JOIN purchases x ON x.product_id=p.id
            GROUP BY p.id,p.name,p.category
            ORDER BY spend DESC
            LIMIT 100
            """
        )

        recent_purchases = _rows(
            conn,
            """
            SELECT x.purchase_date,
                   s.name AS supplier,
                   p.name AS product,
                   x.quantity,
                   x.unit_price,
                   ROUND(x.quantity*x.unit_price,2) AS total
            FROM purchases x
            JOIN suppliers s ON s.id=x.supplier_id
            JOIN products p ON p.id=x.product_id
            ORDER BY x.purchase_date DESC,x.id DESC
            LIMIT 100
            """
        )

        price_lists = _rows(
            conn,
            """
            SELECT pl.imported_at,
                   s.name AS supplier,
                   p.name AS product,
                   pll.list_price,
                   pll.discount_1,
                   pll.discount_2,
                   pll.net_price
            FROM price_list_lines pll
            JOIN price_lists pl ON pl.id=pll.price_list_id
            JOIN suppliers s ON s.id=pl.supplier_id
            LEFT JOIN products p ON p.id=pll.product_id
            ORDER BY pl.imported_at DESC,pll.id DESC
            LIMIT 100
            """
        )

        documents = _rows(
            conn,
            """
            SELECT d.document_date,d.document_number,d.document_type,
                   s.name AS supplier,d.total_amount,d.status
            FROM documents d
            JOIN suppliers s ON s.id=d.supplier_id
            ORDER BY d.document_date DESC,d.id DESC
            LIMIT 50
            """
        )

        return {
            "snapshot_date": date.today().isoformat(),
            "latest_purchase_month": current_month,
            "previous_purchase_month": previous_month,
            "monthly_totals": totals,
            "invoice_anomaly_summary": anomaly_summary,
            "invoice_anomalies": anomalies,
            "supplier_spend_all_periods": supplier_spend,
            "product_statistics_all_periods": product_stats,
            "recent_purchases": recent_purchases,
            "latest_price_list_rows": price_lists,
            "recent_documents": documents,
        }
    finally:
        conn.close()


def ask_ai(question: str):
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY non configurata. Inserisci la chiave API nel file .env "
            "oppure come variabile di ambiente e riavvia il programma."
        )

    snapshot = build_business_snapshot()

    client = OpenAI(api_key=api_key)
    instructions = """
Sei l'assistente acquisti integrato nel gestionale AI Acquisti.

REGOLE OBBLIGATORIE:
1. Rispondi in italiano, in modo chiaro e concreto.
2. Usa esclusivamente i dati presenti nel BUSINESS_SNAPSHOT.
3. Non inventare mai numeri, fornitori, prodotti, fatture o periodi.
4. Se i dati non bastano per rispondere, dillo esplicitamente.
5. Distingui tra prezzo di listino, sconti, prezzo netto e prezzo realmente fatturato.
6. Per le anomalie di fattura usa i campi expected_net_price,
   invoice_net_price e variance_total.
7. Se variance_total è positivo, è un maggior costo; se negativo è un risparmio.
8. Non modificare dati e non affermare di aver eseguito azioni.
9. Quando utile, mostra calcoli sintetici e indica il periodo a cui si riferiscono.
10. Non usare conoscenza esterna per completare dati aziendali mancanti.
"""

    user_input = (
        "BUSINESS_SNAPSHOT:\n"
        + json.dumps(snapshot, ensure_ascii=False, default=str)
        + "\n\nDOMANDA DELL'UTENTE:\n"
        + question
    )

    response = client.responses.create(
        model=MODEL,
        instructions=instructions,
        input=user_input,
    )

    answer = (response.output_text or "").strip()
    if not answer:
        answer = "Non sono riuscito a generare una risposta dai dati disponibili."

    return {
        "answer": answer,
        "model": MODEL,
        "data_source": "database_azienda",
    }
