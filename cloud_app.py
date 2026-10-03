from fastapi import HTTPException, Request

from app import app
from db import get_platform_connection, hash_password, verify_password


app.version = "1.4C2.3"


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
