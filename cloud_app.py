from pathlib import Path
from typing import Optional

from fastapi import File, HTTPException, Request, UploadFile

from app import app
from db import get_platform_connection, hash_password, verify_password
from smart_price_list import smart_import_price_list


app.version = "1.4C2.6"


@app.post("/api/auth/change-password")
def change_own_password(payload: dict, request: Request):
    current_password = str(payload.get("current_password") or "")
    new_password = str(payload.get("new_password") or "")
    confirm_password = str(payload.get("confirm_password") or "")

    if not current_password:
        raise HTTPException(400, "Inserisci la password attuale.")
    if len(new_password) < 8:
        raise HTTPException(400, "La nuova password deve contenere almeno 8 caratteri.")
    if new_password != confirm_password:
        raise HTTPException(400, "Le due nuove password non coincidono.")
    if new_password == current_password:
        raise HTTPException(400, "La nuova password deve essere diversa da quella attuale.")

    user_id = int(request.state.user["id"])
    current_token = request.cookies.get("ai_acquisti_session_14c")

    conn = get_platform_connection()
    try:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE id=? AND status='ACTIVE'",
            (user_id,),
        ).fetchone()

        if not row or not verify_password(current_password, row["password_hash"]):
            raise HTTPException(400, "La password attuale non è corretta.")

        conn.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (hash_password(new_password), user_id),
        )

        # Mantiene valida solo la sessione da cui viene effettuato il cambio.
        if current_token:
            conn.execute(
                "DELETE FROM sessions WHERE user_id=? AND token<>?",
                (user_id, current_token),
            )
        else:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "message": "Password aggiornata correttamente.",
    }


@app.post("/api/price-lists/smart-upload")
async def smart_upload_price_list(
    file: UploadFile = File(...),
    supplier_id: Optional[str] = None,
):
    filename = file.filename or "listino"
    extension = Path(filename).suffix.lower()
    if extension not in {".csv", ".xlsx"}:
        raise HTTPException(400, "Formato non supportato. Usa un listino CSV oppure XLSX.")

    content = await file.read()
    if len(content) > 15_000_000:
        raise HTTPException(400, "File troppo grande (massimo 15 MB).")

    selected_supplier_id = str(supplier_id or "").strip() or None

    try:
        result = smart_import_price_list(
            file_name=filename,
            file_bytes=content,
            selected_supplier_id=selected_supplier_id,
        )
        if result.get("duplicate"):
            raise HTTPException(409, result["message"])
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f"Importazione listino non riuscita: {exc}")
