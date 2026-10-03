from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "db.py"
APP = ROOT / "app.py"
JS = ROOT / "static" / "app.js"
INDEX = ROOT / "static" / "index.html"


def replace_once(text, old, new, label):
    if old not in text:
        raise RuntimeError(f"Punto non trovato: {label}")
    return text.replace(old, new, 1)


def apply_db(text):
    text = replace_once(
        text,
        """CREATE TABLE IF NOT EXISTS user_companies (\n    user_id BIGINT NOT NULL,\n    company_id BIGINT NOT NULL,\n    role TEXT NOT NULL DEFAULT 'OPERATOR',\n    created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::TEXT),\n""",
        """CREATE TABLE IF NOT EXISTS user_companies (\n    user_id BIGINT NOT NULL,\n    company_id BIGINT NOT NULL,\n    role TEXT NOT NULL DEFAULT 'OPERATOR',\n    status TEXT NOT NULL DEFAULT 'ACTIVE',\n    created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::TEXT),\n""",
        "user_companies status",
    )

    text = replace_once(
        text,
        """        with raw.cursor() as cur:\n            cur.execute(PLATFORM_DDL)\n        raw.commit()\n""",
        """        with raw.cursor() as cur:\n            cur.execute(PLATFORM_DDL)\n            cur.execute(\n                \"\"\"\n                ALTER TABLE user_companies\n                ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'ACTIVE'\n                \"\"\"\n            )\n        raw.commit()\n""",
        "init_platform_db migration",
    )

    text = replace_once(
        text,
        """            WHERE uc.user_id=? AND c.status='ACTIVE'\n            ORDER BY c.name\n""",
        """            WHERE uc.user_id=?\n              AND c.status='ACTIVE'\n              AND COALESCE(uc.status,'ACTIVE')='ACTIVE'\n            ORDER BY c.name\n""",
        "list_user_companies active membership",
    )

    text = replace_once(
        text,
        """            SELECT role\n            FROM user_companies\n            WHERE user_id=? AND company_id=?\n""",
        """            SELECT role\n            FROM user_companies\n            WHERE user_id=? AND company_id=?\n              AND COALESCE(status,'ACTIVE')='ACTIVE'\n""",
        "user_has_company active membership",
    )

    text = replace_once(
        text,
        """            SELECT u.id,u.email,u.full_name,u.status,u.created_at,uc.role\n            FROM user_companies uc\n            JOIN users u ON u.id=uc.user_id\n            WHERE uc.company_id=?\n""",
        """            SELECT u.id,u.email,u.full_name,\n                   COALESCE(uc.status,'ACTIVE') AS status,\n                   u.created_at,uc.role\n            FROM user_companies uc\n            JOIN users u ON u.id=uc.user_id\n            WHERE uc.company_id=?\n""",
        "list_company_users membership status",
    )

    marker = "\n\ndef list_companies():\n"
    if marker not in text:
        raise RuntimeError("Punto inserimento funzioni utenti non trovato")

    funcs = r'''


def update_company_user_role(company_id, user_id, role, acting_user_id=None):
    role = str(role or "").upper()
    if role not in {"ADMIN", "OPERATOR"}:
        raise ValueError("Ruolo non valido.")

    company_id = int(company_id)
    user_id = int(user_id)
    acting_user_id = int(acting_user_id) if acting_user_id is not None else None

    conn = get_platform_connection()
    try:
        membership = conn.execute(
            """
            SELECT role,COALESCE(status,'ACTIVE') AS status
            FROM user_companies
            WHERE company_id=? AND user_id=?
            """,
            (company_id, user_id),
        ).fetchone()
        if not membership:
            raise ValueError("Utente non appartenente a questa azienda.")

        if acting_user_id == user_id and membership["role"] == "ADMIN" and role != "ADMIN":
            raise ValueError("Non puoi toglierti il ruolo di amministratore.")

        if membership["role"] == "ADMIN" and role != "ADMIN" and membership["status"] == "ACTIVE":
            admins = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM user_companies
                WHERE company_id=? AND role='ADMIN'
                  AND COALESCE(status,'ACTIVE')='ACTIVE'
                """,
                (company_id,),
            ).fetchone()["c"]
            if int(admins or 0) <= 1:
                raise ValueError("Deve rimanere almeno un amministratore attivo.")

        conn.execute(
            "UPDATE user_companies SET role=? WHERE company_id=? AND user_id=?",
            (role, company_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()

    return {"id": user_id, "role": role}


def set_company_user_status(company_id, user_id, status, acting_user_id=None):
    status = str(status or "").upper()
    if status not in {"ACTIVE", "INACTIVE"}:
        raise ValueError("Stato utente non valido.")

    company_id = int(company_id)
    user_id = int(user_id)
    acting_user_id = int(acting_user_id) if acting_user_id is not None else None

    conn = get_platform_connection()
    try:
        membership = conn.execute(
            """
            SELECT role,COALESCE(status,'ACTIVE') AS status
            FROM user_companies
            WHERE company_id=? AND user_id=?
            """,
            (company_id, user_id),
        ).fetchone()
        if not membership:
            raise ValueError("Utente non appartenente a questa azienda.")

        if status == "INACTIVE" and acting_user_id == user_id:
            raise ValueError("Non puoi disattivare il tuo stesso account.")

        if status == "INACTIVE" and membership["role"] == "ADMIN" and membership["status"] == "ACTIVE":
            admins = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM user_companies
                WHERE company_id=? AND role='ADMIN'
                  AND COALESCE(status,'ACTIVE')='ACTIVE'
                """,
                (company_id,),
            ).fetchone()["c"]
            if int(admins or 0) <= 1:
                raise ValueError("Non puoi disattivare l'ultimo amministratore attivo.")

        conn.execute(
            "UPDATE user_companies SET status=? WHERE company_id=? AND user_id=?",
            (status, company_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()

    return {"id": user_id, "status": status}


def reset_company_user_password(company_id, user_id):
    company_id = int(company_id)
    user_id = int(user_id)

    conn = get_platform_connection()
    try:
        membership = conn.execute(
            "SELECT 1 FROM user_companies WHERE company_id=? AND user_id=?",
            (company_id, user_id),
        ).fetchone()
        if not membership:
            raise ValueError("Utente non appartenente a questa azienda.")

        company_count = conn.execute(
            "SELECT COUNT(*) AS c FROM user_companies WHERE user_id=?",
            (user_id,),
        ).fetchone()["c"]
        if int(company_count or 0) > 1:
            raise ValueError(
                "Questo utente è associato a più aziende. Il reset password deve essere eseguito dal Gestore piattaforma."
            )

        temporary_password = secrets.token_urlsafe(9)
        conn.execute(
            "UPDATE users SET password_hash=?, status='ACTIVE' WHERE id=?",
            (hash_password(temporary_password), user_id),
        )
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        conn.commit()
    finally:
        conn.close()

    return temporary_password
'''
    return text.replace(marker, funcs + marker, 1)


def apply_app(text):
    text = replace_once(
        text,
        """    reset_active_company,\n    set_active_company,\n    user_has_company,\n)""",
        """    reset_active_company,\n    reset_company_user_password,\n    set_active_company,\n    set_company_user_status,\n    update_company_user_role,\n    user_has_company,\n)""",
        "app imports",
    )

    text = replace_once(
        text,
        'app = FastAPI(title="AI Acquisti Cloud", version="1.4C2.1")',
        'app = FastAPI(title="AI Acquisti Cloud", version="1.4C2.2")',
        "app version",
    )

    marker = "\n\ndef euro(v):\n"
    if marker not in text:
        raise RuntimeError("Punto inserimento endpoint utenti non trovato")

    endpoints = r'''


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
'''
    return text.replace(marker, endpoints + marker, 1)


def apply_js(text):
    start = text.find("async function loadCompanyUsers(){")
    end = text.find("\nasync function createCompanyUser(){", start)
    if start < 0 or end < 0:
        raise RuntimeError("loadCompanyUsers non trovata")

    block = r'''async function loadCompanyUsers(){
  const el=document.getElementById('companyUsersTable');
  const formBtn=document.getElementById('createUserBtn');

  try{
    const rows=await getJSON('/api/platform/company-users');
    formBtn.disabled=false;

    if(!rows.length){
      el.innerHTML='<div class="empty-state">Nessun utente associato.</div>';
      return;
    }

    el.innerHTML=`<table>
      <thead><tr>
        <th>Nome</th><th>Email</th><th>Ruolo</th><th>Stato</th><th>Azioni</th>
      </tr></thead>
      <tbody>${rows.map(u=>{
        const active=u.status==='ACTIVE';
        const isSelf=Number(u.id)===Number(currentUser?.id);
        return `<tr>
          <td>${u.full_name||'—'}</td>
          <td>${u.email}</td>
          <td>
            <select class="company-user-role" data-user-id="${u.id}" ${isSelf?'disabled':''}>
              <option value="OPERATOR" ${u.role==='OPERATOR'?'selected':''}>Operatore</option>
              <option value="ADMIN" ${u.role==='ADMIN'?'selected':''}>Amministratore</option>
            </select>
          </td>
          <td>${active?'Attivo':'Disattivato'}</td>
          <td>
            <div class="document-detail-actions">
              <button type="button" class="secondary company-user-status-btn"
                data-user-id="${u.id}" data-next-status="${active?'INACTIVE':'ACTIVE'}"
                ${isSelf && active ? 'disabled' : ''}>${active?'Disattiva':'Riattiva'}</button>
              <button type="button" class="secondary company-user-reset-btn" data-user-id="${u.id}">Reset password</button>
            </div>
          </td>
        </tr>`;
      }).join('')}</tbody>
    </table>`;

    el.querySelectorAll('.company-user-role').forEach(sel=>{
      sel.addEventListener('change',()=>updateCompanyUserRole(Number(sel.dataset.userId),sel.value));
    });
    el.querySelectorAll('.company-user-status-btn').forEach(btn=>{
      btn.addEventListener('click',()=>setCompanyUserStatus(Number(btn.dataset.userId),btn.dataset.nextStatus));
    });
    el.querySelectorAll('.company-user-reset-btn').forEach(btn=>{
      btn.addEventListener('click',()=>resetCompanyUserPassword(Number(btn.dataset.userId)));
    });
  }catch(e){
    formBtn.disabled=true;
    el.innerHTML=`<div class="empty-state">${e.message||e}</div>`;
  }
}

async function updateCompanyUserRole(userId,role){
  try{
    await getJSON(`/api/platform/company-users/${userId}/role`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({role})
    });
    await loadCompanyUsers();
  }catch(e){ alert(e.message||e); await loadCompanyUsers(); }
}

async function setCompanyUserStatus(userId,status){
  const action=status==='ACTIVE'?'riattivare':'disattivare';
  if(!confirm(`Confermi di voler ${action} questo utente?`))return;
  try{
    await getJSON(`/api/platform/company-users/${userId}/status`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status})
    });
    await loadCompanyUsers();
  }catch(e){ alert(e.message||e); }
}

async function resetCompanyUserPassword(userId){
  if(!confirm('Generare una nuova password temporanea per questo utente?'))return;
  try{
    const data=await getJSON(`/api/platform/company-users/${userId}/reset-password`,{method:'POST'});
    alert('Password temporanea:\n\n'+data.temporary_password+'\n\nCopiala e comunicala all’utente con un canale sicuro.');
  }catch(e){ alert(e.message||e); }
}
'''
    return text[:start] + block + text[end:]


def apply_index(text):
    return text.replace("Cloud 1.4C2.1", "Cloud 1.4C2.2")


DB.write_text(apply_db(DB.read_text(encoding="utf-8")), encoding="utf-8")
APP.write_text(apply_app(APP.read_text(encoding="utf-8")), encoding="utf-8")
JS.write_text(apply_js(JS.read_text(encoding="utf-8")), encoding="utf-8")
INDEX.write_text(apply_index(INDEX.read_text(encoding="utf-8")), encoding="utf-8")

print("Patch 1.4C2.2 applicata")
