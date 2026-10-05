from pathlib import Path
import os

from dotenv import load_dotenv

from fastapi import FastAPI, File, HTTPException, UploadFile, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from db import (
    authenticate_user,
    create_company,
    create_company_user,
    create_first_admin,
    create_session,
    delete_session,
    get_company,
    get_connection,
    get_session_user,
    has_users,
    init_platform_db,
    link_user_to_company,
    list_company_users,
    list_companies,
    list_user_companies,
    next_text_id,
    reset_active_company,
    reset_company_user_password,
    set_active_company,
    set_company_user_status,
    update_company_user_role,
    user_has_company,
)
from xml_importer import import_xml_file
from pdf_importer import import_pdf_file
from ddt_importer import import_ddt_file, invoice_ddt_summary
from spreadsheet_importer import import_price_list_file
from ai_service import ask_ai

load_dotenv()

app = FastAPI(title="AI Acquisti Cloud", version="1.4C2.8")
BASE_DIR = Path(__file__).parent
COOKIE_SECURE = bool(os.getenv("RAILWAY_ENVIRONMENT_ID")) or os.getenv("COOKIE_SECURE", "").lower() in {"1", "true", "yes"}
init_platform_db()



@app.middleware("http")
async def authentication_and_company_context(request: Request, call_next):
    """
    1.4B
    - Le API auth/setup restano pubbliche.
    - Tutte le altre API richiedono una sessione valida.
    - Le API business richiedono anche un'azienda collegata all'utente.
    """
    path = request.url.path

    public_api = (
        path == "/api/auth/status"
        or path == "/api/auth/setup"
        or path == "/api/auth/login"
    )

    is_api = path.startswith("/api/")
    if not is_api or public_api:
        return await call_next(request)

    token = request.cookies.get("ai_acquisti_session_14c")
    user = get_session_user(token)
    if not user:
        return JSONResponse(
            status_code=401,
            content={"detail": "Sessione non valida. Effettua il login."},
        )

    request.state.user = user

    # Le API di autenticazione e amministrazione piattaforma non richiedono X-Company-ID.
    needs_company = not (
        path.startswith("/api/auth/")
        or path.startswith("/api/platform/admin/")
        or path == "/api/platform/companies"
    )

    company_token = None
    if needs_company:
        raw_company_id = request.headers.get("X-Company-ID")
        if not raw_company_id:
            return JSONResponse(
                status_code=400,
                content={"detail": "Seleziona prima un'azienda."},
            )

        try:
            company_id = int(raw_company_id)
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"detail": "Azienda non valida."},
            )

        role = user_has_company(user["id"], company_id)
        if not role:
            return JSONResponse(
                status_code=403,
                content={"detail": "Non hai accesso a questa azienda."},
            )

        request.state.company_id = company_id
        request.state.company_role = role
        company_token = set_active_company(company_id)

    try:
        return await call_next(request)
    finally:
        if company_token is not None:
            reset_active_company(company_token)


@app.get("/api/auth/status")
def auth_status(request: Request):
    configured = has_users()
    user = get_session_user(request.cookies.get("ai_acquisti_session_14c"))
    return {
        "configured": configured,
        "authenticated": bool(user),
        "user": user,
    }


@app.post("/api/auth/setup")
def auth_setup(payload: dict, response: Response):
    if has_users():
        raise HTTPException(400, "Configurazione iniziale già completata.")

    try:
        user = create_first_admin(
            email=payload.get("email"),
            password=payload.get("password"),
            full_name=payload.get("full_name", ""),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    token = create_session(user["id"])
    response.set_cookie(
        "ai_acquisti_session_14c",
        token,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        max_age=7 * 24 * 60 * 60,
    )
    return {"ok": True, "user": user}


@app.post("/api/auth/login")
def auth_login(payload: dict, response: Response):
    user = authenticate_user(
        payload.get("email"),
        payload.get("password"),
    )
    if not user:
        raise HTTPException(401, "Email o password non corretti.")

    token = create_session(user["id"])
    response.set_cookie(
        "ai_acquisti_session_14c",
        token,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        max_age=7 * 24 * 60 * 60,
    )
    return {"ok": True, "user": user}


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    delete_session(request.cookies.get("ai_acquisti_session_14c"))
    response.delete_cookie("ai_acquisti_session_14c")
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(request: Request):
    return request.state.user


@app.get("/api/platform/companies")
def platform_companies(request: Request):
    # Un PLATFORM_ADMIN gestisce i tenant dal pannello dedicato e non entra
    # automaticamente nelle aziende operative.
    if request.state.user.get("is_platform_admin"):
        return []
    return list_user_companies(request.state.user["id"])


@app.post("/api/platform/companies")
def platform_create_company(payload: dict, request: Request):
    raise HTTPException(403, "La creazione delle aziende è riservata al gestore della piattaforma.")


def _require_platform_admin(request: Request):
    if not request.state.user.get("is_platform_admin"):
        raise HTTPException(403, "Accesso riservato al gestore della piattaforma.")


@app.get("/api/platform/admin/companies")
def platform_admin_companies(request: Request):
    _require_platform_admin(request)
    return list_companies()


@app.post("/api/platform/admin/companies")
def platform_admin_create_company(payload: dict, request: Request):
    _require_platform_admin(request)

    admin_email = str(payload.get("admin_email") or "").strip()
    admin_password = str(payload.get("admin_password") or "")
    admin_name = str(payload.get("admin_name") or "").strip()

    if not admin_email:
        raise HTTPException(400, "Inserisci l'email dell'amministratore aziendale.")
    if len(admin_password) < 8:
        raise HTTPException(400, "La password dell'amministratore aziendale deve avere almeno 8 caratteri.")

    try:
        company = create_company(
            name=payload.get("name"),
            vat_number=payload.get("vat_number", ""),
            sector=payload.get("sector", ""),
        )
        company_admin = create_company_user(
            company_id=company["id"],
            email=admin_email,
            password=admin_password,
            full_name=admin_name,
            role="ADMIN",
        )
        return {
            "company": company,
            "company_admin": company_admin,
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/platform/company-users")
def platform_company_users(request: Request):
    if request.state.company_role != "ADMIN":
        raise HTTPException(403, "Solo un amministratore può gestire gli utenti.")
    return list_company_users(request.state.company_id)


@app.post("/api/platform/company-users")
def platform_create_company_user(payload: dict, request: Request):
    if request.state.company_role != "ADMIN":
        raise HTTPException(403, "Solo un amministratore può creare utenti.")

    try:
        return create_company_user(
            company_id=request.state.company_id,
            email=payload.get("email"),
            password=payload.get("password"),
            full_name=payload.get("full_name", ""),
            role=payload.get("role", "OPERATOR"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))




@app.post("/api/platform/company-users/{user_id}/role")
def platform_update_company_user_role(user_id: int, payload: dict, request: Request):
    if request.state.company_role != "ADMIN":
        raise HTTPException(403, "Solo un amministratore può gestire gli utenti.")
    try:
        return update_company_user_role(
            company_id=request.state.company_id,
            user_id=user_id,
            role=payload.get("role"),
            acting_user_id=request.state.user["id"],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/platform/company-users/{user_id}/status")
def platform_set_company_user_status(user_id: int, payload: dict, request: Request):
    if request.state.company_role != "ADMIN":
        raise HTTPException(403, "Solo un amministratore può gestire gli utenti.")
    try:
        return set_company_user_status(
            company_id=request.state.company_id,
            user_id=user_id,
            status=payload.get("status"),
            acting_user_id=request.state.user["id"],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/platform/company-users/{user_id}/reset-password")
def platform_reset_company_user_password(user_id: int, request: Request):
    if request.state.company_role != "ADMIN":
        raise HTTPException(403, "Solo un amministratore può gestire gli utenti.")
    try:
        temporary_password = reset_company_user_password(
            company_id=request.state.company_id,
            user_id=user_id,
        )
        return {"ok": True, "temporary_password": temporary_password}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def euro(v):
    return round(float(v or 0), 2)


@app.get("/api/summary")
def summary():
    conn = get_connection()
    cur = conn.cursor()
    months = [r["month"] for r in cur.execute(
        "SELECT DISTINCT substr(purchase_date,1,7) AS month FROM purchases ORDER BY month"
    ).fetchall()]
    current_month = months[-1] if months else None
    previous_month = months[-2] if len(months) > 1 else None

    def month_total(month):
        row = cur.execute(
            "SELECT COALESCE(SUM(quantity*unit_price),0) AS total FROM purchases WHERE substr(purchase_date,1,7)=?",
            (month,),
        ).fetchone()
        return float(row["total"])

    current_total = month_total(current_month)
    previous_total = month_total(previous_month) if previous_month else 0
    variation = ((current_total - previous_total) / previous_total * 100) if previous_total else 0
    supplier_count = 0
    if current_month:
        supplier_count = cur.execute(
            "SELECT COUNT(DISTINCT supplier_id) AS c FROM purchases WHERE substr(purchase_date,1,7)=?",
            (current_month,),
        ).fetchone()["c"]

    alerts = []
    for p in cur.execute("SELECT id,name FROM products").fetchall():
        rows = cur.execute(
            """
            SELECT substr(purchase_date,1,7) AS month, AVG(unit_price) AS avg_price
            FROM purchases WHERE product_id=?
            GROUP BY substr(purchase_date,1,7) ORDER BY month
            """,
            (p["id"],),
        ).fetchall()
        if len(rows) >= 2:
            prev = float(rows[-2]["avg_price"])
            curr = float(rows[-1]["avg_price"])
            change = ((curr - prev) / prev * 100) if prev else 0
            if abs(change) >= 3:
                alerts.append({
                    "product_id": p["id"],
                    "product": p["name"],
                    "previous_price": euro(prev),
                    "current_price": euro(curr),
                    "change_pct": round(change, 1),
                    "severity": "red" if change >= 8 else "orange" if change >= 3 else "green",
                })
    alerts.sort(key=lambda x: x["change_pct"], reverse=True)

    pending = cur.execute(
        """
        SELECT COUNT(*) AS c
        FROM documents
        WHERE COALESCE(workflow_status,'DA_GESTIRE')='DA_GESTIRE'
        """
    ).fetchone()["c"]
    conn.close()
    return {
        "current_month": current_month,
        "spend": euro(current_total),
        "previous_spend": euro(previous_total),
        "variation_pct": round(variation, 1),
        "supplier_count": supplier_count,
        "products_increase": len([a for a in alerts if a["change_pct"] > 0]),
        "documents_to_check": pending,
        "documents_to_manage": pending,
        "alerts": alerts[:8],
    }


@app.get("/api/spend-by-supplier")
def spend_by_supplier():
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT s.name, ROUND(SUM(p.quantity*p.unit_price),2) AS total
        FROM purchases p JOIN suppliers s ON s.id=p.supplier_id
        WHERE substr(p.purchase_date,1,7)=(SELECT MAX(substr(purchase_date,1,7)) FROM purchases)
        GROUP BY s.id,s.name ORDER BY total DESC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/spend-by-category")
def spend_by_category():
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT pr.category, ROUND(SUM(p.quantity*p.unit_price),2) AS total
        FROM purchases p JOIN products pr ON pr.id=p.product_id
        WHERE substr(p.purchase_date,1,7)=(SELECT MAX(substr(purchase_date,1,7)) FROM purchases)
        GROUP BY pr.category ORDER BY total DESC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/monthly-spend")
def monthly_spend():
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT substr(purchase_date,1,7) AS month, ROUND(SUM(quantity*unit_price),2) AS total
        FROM purchases GROUP BY substr(purchase_date,1,7) ORDER BY month
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/products")
def products():
    conn = get_connection()
    rows = conn.execute("SELECT id,name,category,unit FROM products ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/products-overview")
def products_overview():
    """
    Elenco completo dei prodotti con ultimo acquisto e confronto
    con il prezzo precedente dello stesso prodotto.
    """
    conn = get_connection()
    products_rows = conn.execute(
        "SELECT id,name,category,unit FROM products ORDER BY name"
    ).fetchall()

    result = []
    for product in products_rows:
        purchases = conn.execute(
            """
            SELECT p.id,p.purchase_date,p.quantity,p.unit_price,
                   s.id AS supplier_id,s.name AS supplier
            FROM purchases p
            JOIN suppliers s ON s.id=p.supplier_id
            WHERE p.product_id=?
            ORDER BY p.purchase_date DESC,p.id DESC
            """,
            (product["id"],),
        ).fetchall()

        last = purchases[0] if purchases else None
        previous = purchases[1] if len(purchases) > 1 else None
        change_pct = None
        if last and previous and float(previous["unit_price"]) != 0:
            change_pct = round(
                (float(last["unit_price"]) - float(previous["unit_price"]))
                / float(previous["unit_price"]) * 100,
                1,
            )

        totals = conn.execute(
            """
            SELECT COALESCE(SUM(quantity),0) AS total_qty,
                   COALESCE(SUM(quantity*unit_price),0) AS total_spend,
                   COUNT(*) AS purchase_count
            FROM purchases WHERE product_id=?
            """,
            (product["id"],),
        ).fetchone()

        result.append({
            "id": product["id"],
            "name": product["name"],
            "category": product["category"],
            "unit": product["unit"],
            "last_price": euro(last["unit_price"]) if last else None,
            "previous_price": euro(previous["unit_price"]) if previous else None,
            "change_pct": change_pct,
            "last_supplier": last["supplier"] if last else None,
            "last_purchase": last["purchase_date"] if last else None,
            "total_quantity": round(float(totals["total_qty"] or 0), 2),
            "total_spend": euro(totals["total_spend"]),
            "purchase_count": totals["purchase_count"],
        })

    conn.close()
    return result



@app.get("/api/suppliers-overview")
def suppliers_overview():
    conn = get_connection()
    suppliers = conn.execute(
        "SELECT id,name,vat_number,category FROM suppliers ORDER BY name"
    ).fetchall()

    result = []
    for supplier in suppliers:
        totals = conn.execute(
            """
            SELECT COUNT(*) AS purchase_count,
                   COUNT(DISTINCT product_id) AS product_count,
                   COALESCE(SUM(quantity*unit_price),0) AS total_spend,
                   MAX(purchase_date) AS last_purchase
            FROM purchases
            WHERE supplier_id=?
            """,
            (supplier["id"],),
        ).fetchone()

        docs = conn.execute(
            """
            SELECT COUNT(*) AS document_count
            FROM documents
            WHERE supplier_id=?
            """,
            (supplier["id"],),
        ).fetchone()

        monthly = conn.execute(
            """
            SELECT substr(purchase_date,1,7) AS month,
                   SUM(quantity*unit_price) AS total
            FROM purchases
            WHERE supplier_id=?
            GROUP BY substr(purchase_date,1,7)
            ORDER BY month DESC
            LIMIT 2
            """,
            (supplier["id"],),
        ).fetchall()

        change_pct = None
        if len(monthly) >= 2 and float(monthly[1]["total"] or 0) != 0:
            change_pct = round(
                (float(monthly[0]["total"]) - float(monthly[1]["total"]))
                / float(monthly[1]["total"]) * 100,
                1,
            )

        result.append({
            "id": supplier["id"],
            "name": supplier["name"],
            "vat_number": supplier["vat_number"],
            "category": supplier["category"],
            "purchase_count": totals["purchase_count"],
            "product_count": totals["product_count"],
            "total_spend": euro(totals["total_spend"]),
            "last_purchase": totals["last_purchase"],
            "document_count": docs["document_count"],
            "monthly_change_pct": change_pct,
        })

    conn.close()
    return result


@app.get("/api/suppliers/{supplier_id}/detail")
def supplier_detail(supplier_id: str):
    conn = get_connection()
    supplier = conn.execute(
        "SELECT id,name,vat_number,category FROM suppliers WHERE id=?",
        (supplier_id,),
    ).fetchone()

    if not supplier:
        conn.close()
        raise HTTPException(404, "Fornitore non trovato")

    stats = conn.execute(
        """
        SELECT COUNT(*) AS purchase_count,
               COUNT(DISTINCT product_id) AS product_count,
               ROUND(COALESCE(SUM(quantity*unit_price),0),2) AS total_spend,
               MIN(purchase_date) AS first_purchase,
               MAX(purchase_date) AS last_purchase
        FROM purchases
        WHERE supplier_id=?
        """,
        (supplier_id,),
    ).fetchone()

    products = conn.execute(
        """
        SELECT pr.id,pr.name,pr.category,pr.unit,
               COUNT(*) AS purchases,
               ROUND(SUM(p.quantity),2) AS total_quantity,
               ROUND(SUM(p.quantity*p.unit_price),2) AS total_spend,
               ROUND(AVG(p.unit_price),2) AS average_price,
               MAX(p.purchase_date) AS last_purchase
        FROM purchases p
        JOIN products pr ON pr.id=p.product_id
        WHERE p.supplier_id=?
        GROUP BY pr.id,pr.name,pr.category,pr.unit
        ORDER BY total_spend DESC
        """,
        (supplier_id,),
    ).fetchall()

    monthly = conn.execute(
        """
        SELECT substr(purchase_date,1,7) AS month,
               ROUND(SUM(quantity*unit_price),2) AS total
        FROM purchases
        WHERE supplier_id=?
        GROUP BY substr(purchase_date,1,7)
        ORDER BY month
        """,
        (supplier_id,),
    ).fetchall()

    documents = conn.execute(
        """
        SELECT id,document_type,document_number,document_date,total_amount,status
        FROM documents
        WHERE supplier_id=?
        ORDER BY document_date DESC,id DESC
        LIMIT 50
        """,
        (supplier_id,),
    ).fetchall()

    conn.close()
    return {
        "supplier": dict(supplier),
        "stats": dict(stats),
        "products": [dict(r) for r in products],
        "monthly": [dict(r) for r in monthly],
        "documents": [dict(r) for r in documents],
    }


@app.get("/api/products/{product_id}/detail")
def product_detail(product_id: str):
    conn = get_connection()
    product = conn.execute(
        "SELECT id,name,category,unit FROM products WHERE id=?",
        (product_id,),
    ).fetchone()
    if not product:
        conn.close()
        raise HTTPException(404, "Prodotto non trovato")

    history = conn.execute(
        """
        SELECT p.id,p.purchase_date,s.name AS supplier,p.quantity,p.unit_price,
               ROUND(p.quantity*p.unit_price,2) AS total,
               p.source_document_id
        FROM purchases p
        JOIN suppliers s ON s.id=p.supplier_id
        WHERE p.product_id=?
        ORDER BY p.purchase_date DESC,p.id DESC
        """,
        (product_id,),
    ).fetchall()

    suppliers = conn.execute(
        """
        SELECT s.id,s.name,
               COUNT(*) AS purchases,
               ROUND(SUM(p.quantity),2) AS total_quantity,
               ROUND(SUM(p.quantity*p.unit_price),2) AS total_spend,
               ROUND(AVG(p.unit_price),2) AS average_price,
               MAX(p.purchase_date) AS last_purchase
        FROM purchases p
        JOIN suppliers s ON s.id=p.supplier_id
        WHERE p.product_id=?
        GROUP BY s.id,s.name
        ORDER BY last_purchase DESC
        """,
        (product_id,),
    ).fetchall()

    stats = conn.execute(
        """
        SELECT COUNT(*) AS purchases,
               ROUND(SUM(quantity),2) AS total_quantity,
               ROUND(SUM(quantity*unit_price),2) AS total_spend,
               ROUND(MIN(unit_price),2) AS min_price,
               ROUND(MAX(unit_price),2) AS max_price,
               ROUND(AVG(unit_price),2) AS average_price
        FROM purchases WHERE product_id=?
        """,
        (product_id,),
    ).fetchone()

    conn.close()
    return {
        "product": dict(product),
        "stats": dict(stats),
        "suppliers": [dict(r) for r in suppliers],
        "history": [dict(r) for r in history],
    }


@app.get("/api/products/{product_id}/history")
def product_history(product_id: str):
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT p.purchase_date,s.name AS supplier,p.quantity,p.unit_price,
               ROUND(p.quantity*p.unit_price,2) AS total
        FROM purchases p JOIN suppliers s ON s.id=p.supplier_id
        WHERE p.product_id=? ORDER BY p.purchase_date
        """,
        (product_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]



@app.get("/api/anomalies/summary")
def anomalies_summary():
    conn = get_connection()
    row = conn.execute(
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
          AND d.status='CON_ANOMALIE'
          AND COALESCE(d.workflow_status,'DA_GESTIRE')='DA_GESTIRE'
        """
    ).fetchone()
    conn.close()
    return dict(row)





@app.get("/api/documents/dashboard-inbox")
def dashboard_inbox():
    """
    Coda operativa della Dashboard.
    Ogni documento importato resta qui finché non viene completata
    l'azione richiesta dall'utente.
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT
            d.id,
            d.document_type,
            d.document_number,
            d.document_date,
            d.total_amount,
            d.status,
            d.source,
            d.review_reason,
            d.imported_at,
            d.workflow_status,
            s.name AS supplier
        FROM documents d
        LEFT JOIN suppliers s ON s.id=d.supplier_id
        WHERE COALESCE(d.workflow_status,'DA_GESTIRE')='DA_GESTIRE'
        ORDER BY d.imported_at DESC, d.id DESC
        LIMIT 20
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/documents/review-dashboard")
def review_dashboard():
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT
            d.id,
            d.document_date,
            d.document_number,
            d.total_amount,
            d.review_reason,
            s.name AS supplier
        FROM documents d
        LEFT JOIN suppliers s ON s.id=d.supplier_id
        WHERE d.status='DA_VERIFICARE'
          AND d.document_type NOT IN ('TD04','TD08','TD09')
          AND COALESCE(d.workflow_status,'DA_GESTIRE')='DA_GESTIRE'
        ORDER BY d.imported_at DESC, d.id DESC
        LIMIT 5
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/anomalies/dashboard")
def dashboard_anomalies():
    """
    Restituisce al massimo 5 fatture con anomalie, raggruppate per documento.
    La Dashboard mostra una sola riga per fattura.
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT
            d.id AS document_id,
            d.document_number,
            d.document_date,
            s.name AS supplier,
            COALESCE(p.name, dl.original_description) AS product_name,
            dl.price_variance_unit,
            dl.price_variance_total
        FROM document_lines dl
        JOIN documents d ON d.id=dl.document_id
        JOIN suppliers s ON s.id=d.supplier_id
        LEFT JOIN products p ON p.id=dl.product_id
        WHERE dl.price_check_status='DIFFERENZA'
          AND d.document_type NOT IN ('TD04','TD08','TD09')
          AND d.status='CON_ANOMALIE'
          AND COALESCE(d.workflow_status,'DA_GESTIRE')='DA_GESTIRE'
        ORDER BY d.id DESC, ABS(COALESCE(dl.price_variance_total,0)) DESC
        """
    ).fetchall()
    ddt_rows = conn.execute(
        """
        SELECT d.id AS document_id,d.document_number,d.document_date,s.name AS supplier,
               p.name AS product_name,c.ddt_quantity,c.invoice_quantity,c.quantity_difference,c.status
        FROM invoice_ddt_checks c
        JOIN documents d ON d.id=c.invoice_document_id
        JOIN suppliers s ON s.id=d.supplier_id
        LEFT JOIN products p ON p.id=c.product_id
        WHERE c.status<>'OK'
          AND COALESCE(d.workflow_status,'DA_GESTIRE')='DA_GESTIRE'
        ORDER BY d.id DESC,p.name
        """
    ).fetchall()
    missing_ddt_rows = conn.execute(
        """
        SELECT d.id AS document_id,d.document_number,d.document_date,s.name AS supplier,
               r.reference_number,r.reference_date
        FROM invoice_ddt_references r
        JOIN documents d ON d.id=r.invoice_document_id
        JOIN suppliers s ON s.id=d.supplier_id
        WHERE r.delivery_note_id IS NULL
          AND COALESCE(d.workflow_status,'DA_GESTIRE')='DA_GESTIRE'
        ORDER BY d.id DESC,r.id
        """
    ).fetchall()
    conn.close()

    grouped = {}
    for row in rows:
        r = dict(row)
        doc_id = r["document_id"]
        if doc_id not in grouped:
            grouped[doc_id] = {
                "document_id": doc_id,
                "document_number": r["document_number"],
                "document_date": r["document_date"],
                "supplier": r["supplier"],
                "items": [],
                "impact": 0.0,
            }

        variance_unit = float(r["price_variance_unit"] or 0)
        variance_total = float(r["price_variance_total"] or 0)
        grouped[doc_id]["impact"] += variance_total
        grouped[doc_id]["items"].append({
            "product": r["product_name"] or "Prodotto",
            "variance_unit": variance_unit,
            "variance_total": variance_total,
        })

    result = []
    for item in grouped.values():
        details = item.pop("items")
        count = len(details)

        if count == 1:
            detail = details[0]
            sign = "+" if detail["variance_unit"] > 0 else ""
            anomaly_text = (
                f'{detail["product"]}: {sign}€{detail["variance_unit"]:.2f}/unità '
                f'rispetto al listino'
            )
        else:
            names = ", ".join(d["product"] for d in details[:2])
            if count > 2:
                names += f" + altri {count - 2}"
            anomaly_text = f"{count} anomalie rilevate: {names}"

        item["anomaly"] = anomaly_text
        item["impact"] = round(item["impact"], 2)
        result.append(item)

    by_document = {item["document_id"]: item for item in result}

    def add_ddt_message(row, message):
        r = dict(row)
        doc_id = r["document_id"]
        if doc_id in by_document:
            current = by_document[doc_id]
            if "DDT:" not in current["anomaly"]:
                current["anomaly"] += f" · DDT: {message}"
            return
        item = {
            "document_id": doc_id,
            "document_number": r["document_number"],
            "document_date": r["document_date"],
            "supplier": r["supplier"],
            "anomaly": f"DDT: {message}",
            "impact": 0.0,
            "kind": "DDT",
        }
        by_document[doc_id] = item
        result.append(item)

    for row in ddt_rows:
        r = dict(row)
        add_ddt_message(
            r,
            f"{r['product_name'] or 'Prodotto'}: consegnati {float(r['ddt_quantity'] or 0):g}, "
            f"fatturati {float(r['invoice_quantity'] or 0):g}",
        )

    for row in missing_ddt_rows:
        r = dict(row)
        add_ddt_message(
            r,
            f"DDT {r['reference_number']} non ancora acquisito",
        )

    result.sort(key=lambda x: ("DDT:" in x["anomaly"], abs(x["impact"])), reverse=True)
    return result[:5]


@app.get("/api/anomalies")
def anomalies():
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT
            dl.id,
            d.id AS document_id,
            d.document_number,
            d.document_date,
            s.name AS supplier,
            COALESCE(p.name, dl.original_description) AS product_name,
            dl.quantity,
            dl.unit,
            dl.invoice_net_price,
            dl.expected_net_price,
            dl.price_variance_unit,
            dl.price_variance_total
        FROM document_lines dl
        JOIN documents d ON d.id=dl.document_id
        JOIN suppliers s ON s.id=d.supplier_id
        LEFT JOIN products p ON p.id=dl.product_id
        WHERE dl.price_check_status='DIFFERENZA'
          AND d.status='CON_ANOMALIE'
        ORDER BY ABS(COALESCE(dl.price_variance_total,0)) DESC,
                 d.document_date DESC,
                 d.id DESC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/documents")
def documents():
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT d.id,d.file_name,d.document_type,d.document_number,d.document_date,
               d.total_amount,d.status,d.source,d.review_reason,d.confirmed_at,
               d.workflow_status,d.handled_at,d.imported_at,s.name AS supplier
        FROM documents d LEFT JOIN suppliers s ON s.id=d.supplier_id
        ORDER BY d.id DESC LIMIT 100
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/documents/{document_id}")
def document_detail(document_id: int):
    conn = get_connection()
    doc = conn.execute(
        """
        SELECT d.*,s.name AS supplier,s.vat_number
        FROM documents d LEFT JOIN suppliers s ON s.id=d.supplier_id
        WHERE d.id=?
        """,
        (document_id,),
    ).fetchone()
    if not doc:
        conn.close()
        raise HTTPException(404, "Documento non trovato")
    lines = conn.execute(
        """
        SELECT dl.*,p.name AS product_name
        FROM document_lines dl LEFT JOIN products p ON p.id=dl.product_id
        WHERE dl.document_id=? ORDER BY dl.line_number
        """,
        (document_id,),
    ).fetchall()
    ddt = invoice_ddt_summary(document_id, conn=conn)
    conn.close()
    return {
        "document": dict(doc),
        "lines": [dict(r) for r in lines],
        "ddt_reconciliation": ddt,
    }


@app.get("/api/ddt")
def delivery_notes():
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT n.id,n.file_name,n.ddt_number,n.ddt_date,n.source,n.status,
               n.review_reason,n.imported_at,s.name AS supplier,
               (SELECT COUNT(*) FROM delivery_note_lines l WHERE l.delivery_note_id=n.id) AS lines_count
        FROM delivery_notes n
        LEFT JOIN suppliers s ON s.id=n.supplier_id
        ORDER BY n.id DESC
        LIMIT 200
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/ddt/{delivery_note_id}")
def delivery_note_detail(delivery_note_id: int):
    conn = get_connection()
    note = conn.execute(
        """
        SELECT n.*,s.name AS supplier,s.vat_number
        FROM delivery_notes n
        LEFT JOIN suppliers s ON s.id=n.supplier_id
        WHERE n.id=?
        """,
        (delivery_note_id,),
    ).fetchone()
    if not note:
        conn.close()
        raise HTTPException(404, "DDT non trovato")
    lines = conn.execute(
        """
        SELECT l.*,p.name AS product_name
        FROM delivery_note_lines l
        LEFT JOIN products p ON p.id=l.product_id
        WHERE l.delivery_note_id=?
        ORDER BY l.line_number
        """,
        (delivery_note_id,),
    ).fetchall()
    linked = conn.execute(
        """
        SELECT r.invoice_document_id,d.document_number,d.document_date
        FROM invoice_ddt_references r
        JOIN documents d ON d.id=r.invoice_document_id
        WHERE r.delivery_note_id=?
        ORDER BY d.document_date,d.id
        """,
        (delivery_note_id,),
    ).fetchall()
    conn.close()
    return {
        "delivery_note": dict(note),
        "lines": [dict(r) for r in lines],
        "linked_invoices": [dict(r) for r in linked],
    }


@app.post("/api/ddt/upload")
async def upload_delivery_note(file: UploadFile = File(...)):
    filename = file.filename or "ddt"
    suffix = Path(filename).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".pdf"}:
        raise HTTPException(400, "Formato non supportato. Usa un DDT JPG, PNG oppure PDF.")
    content = await file.read()
    if len(content) > 15_000_000:
        raise HTTPException(400, "File troppo grande (massimo 15 MB).")
    try:
        return import_ddt_file(filename, content)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f"Importazione DDT non riuscita: {exc}")


@app.post("/api/documents/upload")
async def upload_document(file: UploadFile = File(...)):
    filename = file.filename or "documento"
    suffix = Path(filename).suffix.lower()
    if suffix not in {".xml", ".pdf"}:
        raise HTTPException(400, "Formato non supportato. Usa una fattura XML oppure PDF.")
    content = await file.read()
    if len(content) > 15_000_000:
        raise HTTPException(400, "File troppo grande per il prototipo (massimo 15 MB).")
    try:
        if suffix == ".xml":
            result = import_xml_file(filename, content)
            result["source"] = "XML"
        else:
            result = import_pdf_file(filename, content)
        if result.get("duplicate"):
            result["status"] = "DUPLICATA"
            return result
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f"Importazione non riuscita: {exc}")





@app.post("/api/documents/{document_id}/mark-handled")
def mark_document_handled(document_id: int):
    conn = get_connection()
    doc = conn.execute(
        """
        SELECT id,status,workflow_status
        FROM documents
        WHERE id=?
        """,
        (document_id,),
    ).fetchone()

    if not doc:
        conn.close()
        raise HTTPException(404, "Documento non trovato.")

    if doc["status"] == "DA_VERIFICARE":
        conn.close()
        raise HTTPException(
            400,
            "Prima di chiudere questo documento devi confermare i dati letti."
        )

    conn.execute(
        """
        UPDATE documents
        SET workflow_status='GESTITO',
            handled_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (document_id,),
    )
    conn.commit()
    conn.close()

    return {
        "ok": True,
        "document_id": document_id,
        "workflow_status": "GESTITO",
    }


@app.post("/api/documents/{document_id}/confirm")
def confirm_document(document_id: int):
    """
    Conferma una fattura DA_VERIFICARE.
    I movimenti entrano nello storico solo dopo questa conferma.
    """
    conn = get_connection()
    doc = conn.execute(
        """
        SELECT d.*,s.name AS supplier
        FROM documents d
        LEFT JOIN suppliers s ON s.id=d.supplier_id
        WHERE d.id=?
        """,
        (document_id,),
    ).fetchone()

    if not doc:
        conn.close()
        raise HTTPException(404, "Fattura non trovata.")

    if doc["status"] != "DA_VERIFICARE":
        conn.close()
        raise HTTPException(400, "Questa fattura non è nello stato Da verificare.")

    missing = []
    if not doc["supplier_id"]:
        missing.append("fornitore")
    if not (doc["document_number"] or "").strip():
        missing.append("numero fattura")
    if not (doc["document_date"] or "").strip():
        missing.append("data fattura")

    lines = conn.execute(
        """
        SELECT *
        FROM document_lines
        WHERE document_id=?
        ORDER BY line_number
        """,
        (document_id,),
    ).fetchall()

    product_lines = [
        r for r in lines
        if (r["line_type"] or "PRODUCT") == "PRODUCT"
    ]
    if not product_lines:
        missing.append("righe prodotto")

    for r in product_lines:
        if not r["product_id"]:
            missing.append(f"prodotto riga {r['line_number']}")
        if float(r["quantity"] or 0) <= 0:
            missing.append(f"quantità riga {r['line_number']}")
        if float(r["unit_price"] or 0) <= 0:
            missing.append(f"prezzo riga {r['line_number']}")

    if missing:
        conn.close()
        raise HTTPException(
            400,
            "Non è ancora possibile confermare: mancano " + ", ".join(missing) + "."
        )

    # Idempotenza: rimuove eventuali vecchi movimenti collegati prima di ricrearli.
    conn.execute("DELETE FROM purchases WHERE source_document_id=?", (document_id,))

    if doc["document_type"] not in ("TD04", "TD08", "TD09"):
        for r in product_lines:
            conn.execute(
                """
                INSERT INTO purchases(
                    purchase_date,supplier_id,product_id,quantity,unit_price,source_document_id
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    doc["document_date"], doc["supplier_id"], r["product_id"],
                    r["quantity"], r["unit_price"], document_id,
                ),
            )

    anomaly_count = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM document_lines
        WHERE document_id=? AND price_check_status='DIFFERENZA'
        """,
        (document_id,),
    ).fetchone()["c"]

    new_status = "CON_ANOMALIE" if anomaly_count else "IMPORTATA"
    new_workflow = "DA_GESTIRE" if new_status == "CON_ANOMALIE" else "GESTITO"
    conn.execute(
        """
        UPDATE documents
        SET status=?,
            review_reason=NULL,
            confirmed_at=CURRENT_TIMESTAMP,
            workflow_status=?,
            handled_at=CASE WHEN ?='GESTITO' THEN CURRENT_TIMESTAMP ELSE NULL END
        WHERE id=?
        """,
        (new_status, new_workflow, new_workflow, document_id),
    )
    conn.commit()
    conn.close()

    return {
        "ok": True,
        "document_id": document_id,
        "status": new_status,
        "anomaly_count": anomaly_count,
    }



@app.post("/api/suppliers")
def create_supplier_manual(payload: dict):
    name = str(payload.get("name") or "").strip()
    vat_number = str(payload.get("vat_number") or "").strip().upper()
    category = str(payload.get("category") or "").strip()

    if not name:
        raise HTTPException(400, "Inserisci il nome del fornitore.")

    conn = get_connection()

    # Prima controlliamo eventuale P.IVA già presente.
    if vat_number:
        existing = conn.execute(
            """
            SELECT id,name,vat_number,category
            FROM suppliers
            WHERE UPPER(COALESCE(vat_number,''))=UPPER(?)
            """,
            (vat_number,),
        ).fetchone()
        if existing:
            conn.close()
            raise HTTPException(
                400,
                f"Esiste già un fornitore con questa P.IVA: {existing['name']}."
            )

    # Evita anche duplicati evidenti sul nome.
    existing_name = conn.execute(
        """
        SELECT id,name,vat_number,category
        FROM suppliers
        WHERE LOWER(TRIM(name))=LOWER(TRIM(?))
        """,
        (name,),
    ).fetchone()
    if existing_name:
        conn.close()
        raise HTTPException(
            400,
            f"Il fornitore '{existing_name['name']}' esiste già."
        )

    supplier_id = next_text_id(conn, "suppliers", "S")
    conn.execute(
        """
        INSERT INTO suppliers(id,name,vat_number,category)
        VALUES(?,?,?,?)
        """,
        (
            supplier_id,
            name,
            vat_number or None,
            category or None,
        ),
    )
    conn.commit()

    row = conn.execute(
        """
        SELECT id,name,vat_number,category
        FROM suppliers
        WHERE id=?
        """,
        (supplier_id,),
    ).fetchone()
    conn.close()

    return dict(row)


@app.get("/api/suppliers-simple")
def suppliers_simple():
    conn = get_connection()
    rows = conn.execute(
        "SELECT id,name,vat_number FROM suppliers ORDER BY name"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/price-lists")
def price_lists():
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT pl.id,pl.file_name,pl.imported_at,pl.status,pl.rows_count,
               s.id AS supplier_id,s.name AS supplier
        FROM price_lists pl
        JOIN suppliers s ON s.id=pl.supplier_id
        ORDER BY pl.id DESC
        LIMIT 100
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/price-lists/{price_list_id}")
def price_list_detail(price_list_id: int):
    conn = get_connection()
    header = conn.execute(
        """
        SELECT pl.*,s.name AS supplier,s.vat_number
        FROM price_lists pl
        JOIN suppliers s ON s.id=pl.supplier_id
        WHERE pl.id=?
        """,
        (price_list_id,),
    ).fetchone()
    if not header:
        conn.close()
        raise HTTPException(404, "Listino non trovato")

    lines = conn.execute(
        """
        SELECT pll.*,p.name AS product_name
        FROM price_list_lines pll
        LEFT JOIN products p ON p.id=pll.product_id
        WHERE pll.price_list_id=?
        ORDER BY pll.row_number
        """,
        (price_list_id,),
    ).fetchall()
    conn.close()
    return {"price_list": dict(header), "lines": [dict(r) for r in lines]}


@app.post("/api/price-lists/upload")
async def upload_price_list(supplier_id: str, file: UploadFile = File(...)):
    extension = Path(file.filename).suffix.lower()
    if extension not in {".csv", ".xlsx"}:
        raise HTTPException(400, "Nella versione 0.5 accettiamo solo file CSV o XLSX.")

    content = await file.read()
    if len(content) > 15_000_000:
        raise HTTPException(400, "File troppo grande per il prototipo.")

    try:
        result = import_price_list_file(
            file_name=file.filename,
            file_bytes=content,
            supplier_id=supplier_id,
        )
        if result.get("duplicate"):
            raise HTTPException(409, result["message"])
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Importazione listino non riuscita: {e}")


@app.post("/api/assistant")
def assistant(payload: dict):
    question = (payload.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "Scrivi una domanda.")
    try:
        return ask_ai(question)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(500, f"Errore AI: {e}")


@app.get("/")
def home():
    return FileResponse(BASE_DIR / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
