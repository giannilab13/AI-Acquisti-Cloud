from pathlib import Path
import os
import mimetypes
import json

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
    update_company_identity,
    user_has_company,
)
from xml_importer import import_xml_file
from pdf_importer import import_pdf_file
from ddt_importer import cleanup_optional_ddt_review_flags, import_ddt_file, invoice_ddt_summary
from spreadsheet_importer import import_price_list_file
from ai_service import ask_ai
from storage_service import read_document_bytes
from anomaly_workflow import load_document_anomalies, open_document_anomalies, verify_document_anomaly
from price_reference import establish_invoice_baselines

load_dotenv()

app = FastAPI(title="AI Acquisti Cloud", version="1.4C3.2")
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
    company_vat = str(payload.get("vat_number") or "").strip()

    if not admin_email:
        raise HTTPException(400, "Inserisci l'email dell'amministratore aziendale.")
    if len(admin_password) < 8:
        raise HTTPException(400, "La password dell'amministratore aziendale deve avere almeno 8 caratteri.")
    if not company_vat:
        raise HTTPException(400, "Inserisci la P.IVA dell'azienda: serve per verificare i documenti importati.")

    try:
        company = create_company(
            name=payload.get("name"),
            vat_number=company_vat,
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


@app.get("/api/platform/company-profile")
def platform_company_profile(request: Request):
    return get_company(request.state.company_id)


@app.put("/api/platform/company-profile")
def platform_update_company_profile(payload: dict, request: Request):
    if request.state.company_role != "ADMIN":
        raise HTTPException(403, "Solo un amministratore puo modificare i dati dell'azienda.")
    try:
        return update_company_identity(
            request.state.company_id,
            name=payload.get("name"),
            vat_number=payload.get("vat_number"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


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
    pending_ddt = cur.execute(
        """
        SELECT COUNT(*) AS c
        FROM delivery_notes
        WHERE status IN ('IN_ATTESA_FATTURA','DA_VERIFICARE')
        """
    ).fetchone()["c"]
    pending += pending_ddt
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




def _supplier_anomaly_items(conn, supplier_id):
    """
    Restituisce lo storico anomalie del fornitore.
    Unisce le anomalie correnti con le verifiche storiche, così una voce
    verificata resta consultabile anche se in seguito il controllo corrente cambia.
    """
    documents = conn.execute(
        """
        SELECT id,document_number,document_date,total_amount,status,workflow_status
        FROM documents
        WHERE supplier_id=?
        ORDER BY document_date DESC,id DESC
        """,
        (supplier_id,),
    ).fetchall()

    items = {}
    for document in documents:
        doc = dict(document)
        for anomaly in load_document_anomalies(conn, doc["id"]):
            item = {
                **anomaly,
                "document_id": doc["id"],
                "document_number": doc["document_number"],
                "document_date": doc["document_date"],
                "document_total": euro(doc["total_amount"]),
                "document_status": doc["status"],
                "workflow_status": doc["workflow_status"],
            }
            items[(doc["id"], anomaly["key"])] = item

    historical_rows = conn.execute(
        """
        SELECT av.document_id,av.anomaly_key,av.anomaly_type,av.source_id,
               av.original_status,av.summary,av.invoice_value,av.reference_value,
               av.difference_value,av.details_json,av.verified_at,
               av.verified_by_user_id,av.verified_by_name,av.verified_by_email,av.note,
               d.document_number,d.document_date,d.total_amount,d.status,d.workflow_status
        FROM anomaly_verifications av
        JOIN documents d ON d.id=av.document_id
        WHERE d.supplier_id=?
        ORDER BY av.verified_at DESC,av.document_id DESC
        """,
        (supplier_id,),
    ).fetchall()

    for row in historical_rows:
        r = dict(row)
        key = (r["document_id"], r["anomaly_key"])
        verification = {
            "anomaly_key": r["anomaly_key"],
            "anomaly_type": r["anomaly_type"],
            "source_id": r["source_id"],
            "original_status": r["original_status"],
            "summary": r["summary"],
            "invoice_value": float(r["invoice_value"]) if r["invoice_value"] is not None else None,
            "reference_value": float(r["reference_value"]) if r["reference_value"] is not None else None,
            "difference_value": float(r["difference_value"]) if r["difference_value"] is not None else None,
            "details_json": r["details_json"],
            "verified_at": r["verified_at"],
            "verified_by_user_id": r["verified_by_user_id"],
            "verified_by_name": r["verified_by_name"],
            "verified_by_email": r["verified_by_email"],
            "note": r["note"],
        }

        if key in items:
            items[key]["verified"] = True
            items[key]["verification"] = verification
            continue

        details = {}
        try:
            details = json.loads(r["details_json"] or "{}")
        except Exception:
            details = {}

        items[key] = {
            "key": r["anomaly_key"],
            "type": r["anomaly_type"],
            "source_id": r["source_id"],
            "summary": r["summary"],
            "product_name": details.get("product_name"),
            "invoice_value": (
                float(r["invoice_value"]) if r["invoice_value"] is not None else details.get("invoice_value")
            ),
            "reference_value": (
                float(r["reference_value"]) if r["reference_value"] is not None else details.get("reference_value")
            ),
            "difference_value": (
                float(r["difference_value"]) if r["difference_value"] is not None else details.get("difference_value")
            ),
            "difference_unit": details.get("difference_unit"),
            "quantity": details.get("quantity"),
            "unit": details.get("unit"),
            "original_status": r["original_status"] or "DA_VERIFICARE",
            "verified": True,
            "verification": verification,
            "document_id": r["document_id"],
            "document_number": r["document_number"],
            "document_date": r["document_date"],
            "document_total": euro(r["total_amount"]),
            "document_status": r["status"],
            "workflow_status": r["workflow_status"],
        }

    result = list(items.values())
    result.sort(
        key=lambda item: (
            str(item.get("document_date") or ""),
            int(item.get("document_id") or 0),
            str(item.get("key") or ""),
        ),
        reverse=True,
    )
    return result


def _supplier_anomaly_stats(conn, supplier_id, document_count=None):
    anomalies = _supplier_anomaly_items(conn, supplier_id)
    if document_count is None:
        document_count = conn.execute(
            "SELECT COUNT(*) AS c FROM documents WHERE supplier_id=?",
            (supplier_id,),
        ).fetchone()["c"]

    anomaly_documents = len({item["document_id"] for item in anomalies})
    open_count = sum(1 for item in anomalies if not item.get("verified"))
    verified_count = sum(1 for item in anomalies if item.get("verified"))
    price_items = [item for item in anomalies if item.get("type") == "PRICE"]
    impact_total = round(
        sum(float(item.get("difference_value") or 0) for item in price_items),
        2,
    )
    extra_cost_total = round(
        sum(max(float(item.get("difference_value") or 0), 0) for item in price_items),
        2,
    )
    rate = round((anomaly_documents / document_count * 100), 1) if document_count else 0

    return {
        "anomaly_count": len(anomalies),
        "anomaly_documents": anomaly_documents,
        "open_anomalies": open_count,
        "verified_anomalies": verified_count,
        "anomaly_rate_pct": rate,
        "impact_total": impact_total,
        "extra_cost_total": extra_cost_total,
        "anomalies": anomalies,
    }


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

        anomaly_stats = _supplier_anomaly_stats(
            conn,
            supplier["id"],
            document_count=int(docs["document_count"] or 0),
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
            "anomaly_count": anomaly_stats["anomaly_count"],
            "anomaly_documents": anomaly_stats["anomaly_documents"],
            "open_anomalies": anomaly_stats["open_anomalies"],
            "verified_anomalies": anomaly_stats["verified_anomalies"],
            "anomaly_rate_pct": anomaly_stats["anomaly_rate_pct"],
            "impact_total": anomaly_stats["impact_total"],
            "extra_cost_total": anomaly_stats["extra_cost_total"],
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
        SELECT id,document_type,document_number,document_date,total_amount,status,workflow_status
        FROM documents
        WHERE supplier_id=?
        ORDER BY document_date DESC,id DESC
        LIMIT 50
        """,
        (supplier_id,),
    ).fetchall()

    document_count = conn.execute(
        "SELECT COUNT(*) AS c FROM documents WHERE supplier_id=?",
        (supplier_id,),
    ).fetchone()["c"]

    anomaly_stats = _supplier_anomaly_stats(
        conn,
        supplier_id,
        document_count=int(document_count or 0),
    )

    conn.close()
    return {
        "supplier": dict(supplier),
        "stats": dict(stats),
        "document_count": int(document_count or 0),
        "products": [dict(r) for r in products],
        "monthly": [dict(r) for r in monthly],
        "documents": [dict(r) for r in documents],
        "anomaly_stats": {
            key: value
            for key, value in anomaly_stats.items()
            if key != "anomalies"
        },
        "anomalies": anomaly_stats["anomalies"],
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
          AND NOT EXISTS (
              SELECT 1
              FROM anomaly_verifications av
              WHERE av.document_id=d.id
                AND av.anomaly_key=('PRICE:' || dl.id::TEXT)
          )
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
    invoice_rows = conn.execute(
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
    ddt_rows = conn.execute(
        """
        SELECT
            n.id,
            n.ddt_number AS document_number,
            n.ddt_date AS document_date,
            n.status,
            n.source,
            n.review_reason,
            n.imported_at,
            s.name AS supplier
        FROM delivery_notes n
        LEFT JOIN suppliers s ON s.id=n.supplier_id
        WHERE n.status IN ('IN_ATTESA_FATTURA','DA_VERIFICARE')
        ORDER BY n.imported_at DESC, n.id DESC
        LIMIT 20
        """
    ).fetchall()
    conn.close()

    rows = []
    for row in invoice_rows:
        item = dict(row)
        item["kind"] = "INVOICE"
        rows.append(item)
    for row in ddt_rows:
        item = dict(row)
        item.update({
            "kind": "DDT",
            "document_type": "DDT",
            "total_amount": None,
            "workflow_status": "DA_GESTIRE",
        })
        rows.append(item)

    rows.sort(key=lambda x: (x.get("imported_at") or "", x.get("id") or 0), reverse=True)
    return rows[:20]


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
    """Return up to 5 invoices with anomalies that still require verification."""
    conn = get_connection()
    docs = conn.execute(
        """
        SELECT d.id AS document_id,d.document_number,d.document_date,s.name AS supplier
        FROM documents d
        LEFT JOIN suppliers s ON s.id=d.supplier_id
        WHERE d.status='CON_ANOMALIE'
          AND d.document_type NOT IN ('TD04','TD08','TD09')
          AND COALESCE(d.workflow_status,'DA_GESTIRE')='DA_GESTIRE'
        ORDER BY d.id DESC
        LIMIT 50
        """
    ).fetchall()

    result = []
    for row in docs:
        doc = dict(row)
        open_items = [
            item
            for item in load_document_anomalies(conn, doc["document_id"])
            if not item["verified"]
        ]
        if not open_items:
            continue

        price_items = [item for item in open_items if item["type"] == "PRICE"]
        ddt_items = [item for item in open_items if item["type"] != "PRICE"]
        impact = round(
            sum(float(item.get("difference_value") or 0) for item in price_items),
            2,
        )

        if price_items:
            if len(price_items) == 1:
                anomaly_text = price_items[0]["summary"]
            else:
                names = ", ".join(
                    (item.get("product_name") or "Prodotto")
                    for item in price_items[:2]
                )
                if len(price_items) > 2:
                    names += f" + altri {len(price_items) - 2}"
                anomaly_text = f"{len(price_items)} anomalie prezzo da verificare: {names}"
            if ddt_items:
                anomaly_text += f" · DDT: {ddt_items[0]['summary']}"
        else:
            anomaly_text = f"DDT: {ddt_items[0]['summary']}"
            if len(ddt_items) > 1:
                anomaly_text += f" + altre {len(ddt_items) - 1}"

        result.append(
            {
                **doc,
                "anomaly": anomaly_text,
                "impact": impact,
                "kind": "PRICE" if price_items else "DDT",
                "open_anomalies": len(open_items),
                "status": "DA_VERIFICARE",
            }
        )
        if len(result) >= 5:
            break

    conn.close()
    return result


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
    line_items = [dict(r) for r in lines]
    anomalies = load_document_anomalies(conn, document_id)
    open_anomaly_count = sum(1 for item in anomalies if not item["verified"])
    verified_anomaly_count = sum(1 for item in anomalies if item["verified"])
    product_lines = [r for r in line_items if r.get("line_type", "PRODUCT") == "PRODUCT"]
    price_differences = sum(1 for r in product_lines if r.get("price_check_status") == "DIFFERENZA")
    price_ok = sum(1 for r in product_lines if r.get("price_check_status") == "OK")
    without_price_list = sum(1 for r in product_lines if r.get("price_check_status") == "SENZA_LISTINO")

    if open_anomaly_count:
        overall_status = "ANOMALIE"
        overall_label = f"{open_anomaly_count} anomalie da verificare"
    elif anomalies:
        if ddt.get("status") == "NESSUN_RIFERIMENTO" or without_price_list:
            overall_status = "PARZIALE"
            overall_label = "Anomalie verificate · controllo parziale"
        else:
            overall_status = "VERIFICATE"
            overall_label = "Controllo completato – anomalie verificate"
    elif ddt.get("status") == "NESSUN_RIFERIMENTO" or without_price_list:
        overall_status = "PARZIALE"
        overall_label = "Controllo parziale"
    else:
        overall_status = "OK"
        overall_label = "Ciclo controllato: tutto OK"

    purchase_control = {
        "status": overall_status,
        "label": overall_label,
        "ddt_status": ddt.get("status"),
        "price_ok_count": price_ok,
        "price_difference_count": price_differences,
        "without_price_list_count": without_price_list,
        "open_anomaly_count": open_anomaly_count,
        "verified_anomaly_count": verified_anomaly_count,
        "anomaly_count": len(anomalies),
    }
    conn.close()
    return {
        "document": dict(doc),
        "lines": line_items,
        "ddt_reconciliation": ddt,
        "purchase_control": purchase_control,
        "anomalies": anomalies,
    }


def _original_response(file_name, stored_path):
    try:
        content = read_document_bytes(stored_path)
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except Exception:
        raise HTTPException(404, "Documento originale non disponibile.")
    safe_name = Path(file_name or "documento").name.replace('"', '')
    media_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{safe_name}"'},
    )


@app.get("/api/documents/{document_id}/original")
def document_original(document_id: int):
    conn = get_connection()
    row = conn.execute(
        "SELECT file_name,stored_path FROM documents WHERE id=?",
        (document_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Documento non trovato.")
    return _original_response(row["file_name"], row["stored_path"])


@app.get("/api/ddt")
def delivery_notes():
    conn = get_connection()
    cleanup_optional_ddt_review_flags(conn)
    rows = conn.execute(
        """
        SELECT n.id,n.file_name,n.ddt_number,n.ddt_date,n.source,n.status,
               n.review_reason,n.imported_at,s.name AS supplier,
               (SELECT COUNT(*) FROM delivery_note_lines l WHERE l.delivery_note_id=n.id) AS lines_count
        FROM delivery_notes n
        LEFT JOIN suppliers s ON s.id=n.supplier_id
        WHERE n.status IN ('IN_ATTESA_FATTURA','DA_VERIFICARE')
        ORDER BY n.id DESC
        LIMIT 200
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/ddt/{delivery_note_id}")
def delivery_note_detail(delivery_note_id: int):
    conn = get_connection()
    cleanup_optional_ddt_review_flags(conn)
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


@app.get("/api/ddt/{delivery_note_id}/original")
def delivery_note_original(delivery_note_id: int):
    conn = get_connection()
    row = conn.execute(
        "SELECT file_name,stored_path FROM delivery_notes WHERE id=?",
        (delivery_note_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "DDT non trovato.")
    return _original_response(row["file_name"], row["stored_path"])


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





@app.post("/api/documents/{document_id}/verify-anomaly")
def verify_anomaly(document_id: int, payload: dict, request: Request):
    anomaly_key = str(payload.get("anomaly_key") or "").strip()
    if not anomaly_key:
        raise HTTPException(400, "Anomalia non specificata.")

    conn = get_connection()
    doc = conn.execute(
        "SELECT id,status,workflow_status FROM documents WHERE id=?",
        (document_id,),
    ).fetchone()
    if not doc:
        conn.close()
        raise HTTPException(404, "Documento non trovato.")

    if doc["status"] != "CON_ANOMALIE":
        conn.close()
        raise HTTPException(400, "Il documento non contiene anomalie da verificare.")

    try:
        result = verify_document_anomaly(
            conn,
            document_id,
            anomaly_key,
            request.state.user,
            payload.get("note", ""),
            payload.get("resolution"),
        )
        conn.commit()
    except ValueError as exc:
        conn.rollback()
        conn.close()
        raise HTTPException(400, str(exc))
    except Exception:
        conn.rollback()
        conn.close()
        raise

    conn.close()
    return {"ok": True, **result}


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

    if doc["status"] == "CON_ANOMALIE":
        open_items = open_document_anomalies(conn, document_id)
        if open_items:
            conn.close()
            raise HTTPException(
                400,
                "Prima di chiudere la fattura devi verificare tutte le anomalie."
            )

    baseline_history_ids = establish_invoice_baselines(
        conn, document_id, user=None
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
        "baseline_prices_created": len(baseline_history_ids),
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

    # C3.2: se manca un listino/riferimento, la prima fattura validata
    # diventa automaticamente la base di confronto per le successive.
    baseline_history_ids = establish_invoice_baselines(
        conn, document_id, user=None
    )

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
        "baseline_prices_created": len(baseline_history_ids),
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


@app.get("/api/price-history")
def price_history():
    conn = get_connection()

    stats = conn.execute(
        """
        SELECT
            COUNT(DISTINCT supplier_id || '|' || product_id) AS monitored_products,
            COALESCE(SUM(CASE WHEN previous_net_price IS NOT NULL THEN 1 ELSE 0 END),0) AS changes_total,
            COALESCE(SUM(CASE WHEN variation_value > 0.005 THEN 1 ELSE 0 END),0) AS increases,
            COALESCE(SUM(CASE WHEN variation_value < -0.005 THEN 1 ELSE 0 END),0) AS decreases,
            COALESCE(SUM(CASE WHEN source_type='FATTURA_BASE' THEN 1 ELSE 0 END),0) AS invoice_bases
        FROM price_reference_history
        """
    ).fetchone()

    rows = conn.execute(
        """
        SELECT h.id,h.supplier_id,s.name AS supplier,h.product_id,p.name AS product,
               h.net_price,h.previous_net_price,h.variation_value,h.variation_pct,
               h.list_price,h.discount_1,h.discount_2,h.unit,h.source_type,
               h.effective_from,h.created_at,h.note,
               d.document_number,d.document_date,
               pl.file_name AS price_list_file
        FROM price_reference_history h
        JOIN suppliers s ON s.id=h.supplier_id
        JOIN products p ON p.id=h.product_id
        LEFT JOIN documents d ON d.id=h.source_document_id
        LEFT JOIN price_lists pl ON pl.id=h.source_price_list_id
        ORDER BY h.id DESC
        LIMIT 500
        """
    ).fetchall()
    conn.close()

    def serialize(row):
        item = dict(row)
        for key in (
            "net_price","previous_net_price","variation_value","variation_pct",
            "list_price","discount_1","discount_2",
        ):
            if item.get(key) is not None:
                item[key] = float(item[key])
        return item

    return {
        "stats": dict(stats),
        "rows": [serialize(row) for row in rows],
    }


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
