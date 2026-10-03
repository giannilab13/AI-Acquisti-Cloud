const fmtEuro = v => new Intl.NumberFormat('it-IT',{style:'currency',currency:'EUR'}).format(Number(v||0));
const monthName = m => {
  if(!m)return 'Nessun acquisto registrato';
  const [y,mo]=m.split('-');
  return new Date(Number(y),Number(mo)-1,1).toLocaleDateString('it-IT',{month:'long',year:'numeric'});
};

let activeCompanyId = Number(localStorage.getItem('ai_acquisti_company_id') || 0);
let companiesCache = [];
let currentUser = null;

async function getJSON(url, options={}){
  const finalOptions={...options};
  const headers=new Headers(finalOptions.headers || {});
  const platformWithoutCompany =
    url.startsWith('/api/platform/admin/')
    || url === '/api/platform/companies';
  if(activeCompanyId && !platformWithoutCompany){
    headers.set('X-Company-ID', String(activeCompanyId));
  }
  finalOptions.headers=headers;

  const r=await fetch(url,finalOptions);
  let data={};
  try{data=await r.json()}catch{}
  if(!r.ok) throw new Error(data.detail||'Errore');
  return data;
}

function showCompanyModal(force=false){
  const modal=document.getElementById('companyModal');
  modal.classList.remove('hidden');
  document.getElementById('cancelCompanyBtn').style.display = force ? 'none' : '';
  document.getElementById('companyCreateResult').innerHTML='';
  setTimeout(()=>document.getElementById('companyName').focus(),50);
}

function hideCompanyModal(){
  if(!companiesCache.length)return;
  document.getElementById('companyModal').classList.add('hidden');
}

function activeCompany(){
  return companiesCache.find(c=>Number(c.id)===Number(activeCompanyId)) || null;
}

function updateCompanyUI(){
  const select=document.getElementById('companySelect');
  select.innerHTML=companiesCache.map(c=>`<option value="${c.id}">${c.name}</option>`).join('');
  if(activeCompanyId)select.value=String(activeCompanyId);

  const company=activeCompany();
  document.getElementById('activeCompanyLabel').textContent =
    company ? company.name : 'Nessuna azienda';
}

async function refreshCurrentView(){
  const active=document.querySelector('.view.active-view');
  if(!active)return;
  if(active.id==='dashboardView') await loadDashboard();
  if(active.id==='documentsView') await loadDocuments();
  if(active.id==='priceListsView'){
    await loadPriceListSuppliers();
    await loadPriceLists();
  }
  if(active.id==='usersView') await loadCompanyUsers();
  if(active.id==='productsView') await loadProductsPage();
  if(active.id==='suppliersView') await loadSuppliersPage();
}

async function selectCompany(companyId){
  activeCompanyId=Number(companyId);
  localStorage.setItem('ai_acquisti_company_id',String(activeCompanyId));
  updateCompanyUI();

  // Chiude eventuali dettagli appartenenti alla precedente azienda.
  document.getElementById('docDetailPanel')?.classList.add('hidden');
  document.getElementById('priceListDetailPanel')?.classList.add('hidden');

  await refreshCurrentView();
}

async function loadCompanies(){
  companiesCache=await getJSON('/api/platform/companies');

  if(!companiesCache.length){
    activeCompanyId=0;
    localStorage.removeItem('ai_acquisti_company_id');
    updateCompanyUI();
    showCompanyModal(true);
    return false;
  }

  if(!companiesCache.some(c=>Number(c.id)===Number(activeCompanyId))){
    activeCompanyId=Number(companiesCache[0].id);
    localStorage.setItem('ai_acquisti_company_id',String(activeCompanyId));
  }

  updateCompanyUI();
  return true;
}

async function createCompanyFromModal(){
  const name=document.getElementById('companyName').value.trim();
  const vat=document.getElementById('companyVat').value.trim();
  const sector=document.getElementById('companySector').value.trim();
  const result=document.getElementById('companyCreateResult');
  const btn=document.getElementById('createCompanyBtn');

  if(!name){
    result.innerHTML='<div class="result-error">Inserisci il nome dell’azienda.</div>';
    return;
  }

  btn.disabled=true;
  btn.textContent='Creazione...';

  try{
    const company=await getJSON('/api/platform/companies',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name,vat_number:vat,sector})
    });

    companiesCache=await getJSON('/api/platform/companies');
    activeCompanyId=Number(company.id);
    localStorage.setItem('ai_acquisti_company_id',String(activeCompanyId));
    updateCompanyUI();

    document.getElementById('companyName').value='';
    document.getElementById('companyVat').value='';
    document.getElementById('companySector').value='';
    hideCompanyModal();

    await loadDashboard();
  }catch(e){
    result.innerHTML=`<div class="result-error">${e.message||e}</div>`;
  }finally{
    btn.disabled=false;
    btn.textContent='Crea azienda';
  }
}

async function bootstrapApp(){
  try{
    const status=await getJSON('/api/auth/status');

    if(!status.configured){
      showAuthMode('setup');
      return;
    }

    if(!status.authenticated){
      showAuthMode('login');
      return;
    }

    currentUser=status.user;
    hideAuth();
    updateCurrentUserUI();

    if(Number(currentUser?.is_platform_admin || 0)===1){
      await showPlatformAdmin();
      return;
    }

    hidePlatformAdmin();
    const ready=await loadCompanies();
    if(ready)await loadDashboard();
  }catch(e){
    console.error(e);
    showAuthMode('login');
  }
}


function hidePlatformAdmin(){
  document.getElementById('platformAdminScreen')?.classList.add('hidden');
}

async function showPlatformAdmin(){
  const screen=document.getElementById('platformAdminScreen');
  screen.classList.remove('hidden');
  document.getElementById('platformAdminName').textContent=currentUser?.full_name || currentUser?.email || 'Gestore piattaforma';
  document.getElementById('platformAdminEmail').textContent=currentUser?.email || '';
  await loadPlatformCompanies();
}

async function loadPlatformCompanies(){
  const el=document.getElementById('platformCompaniesTable');
  try{
    const rows=await getJSON('/api/platform/admin/companies');
    if(!rows.length){
      el.innerHTML='<div class="empty-state">Nessuna azienda cliente ancora creata.</div>';
      return;
    }
    el.innerHTML=`<table>
      <thead><tr><th>Azienda</th><th>Partita IVA</th><th>Settore</th><th>Stato</th></tr></thead>
      <tbody>${rows.map(c=>`<tr>
        <td><strong>${c.name}</strong></td>
        <td>${c.vat_number||'—'}</td>
        <td>${c.sector||'—'}</td>
        <td>${c.status==='ACTIVE'?'Attiva':c.status}</td>
      </tr>`).join('')}</tbody>
    </table>`;
  }catch(e){
    el.innerHTML=`<div class="result-error">${e.message||e}</div>`;
  }
}

async function createPlatformCompany(){
  const result=document.getElementById('platformCompanyResult');
  const btn=document.getElementById('platformCreateCompanyBtn');
  btn.disabled=true;
  btn.textContent='Creazione...';
  result.innerHTML='';

  try{
    const data=await getJSON('/api/platform/admin/companies',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        name:document.getElementById('platformCompanyName').value.trim(),
        vat_number:document.getElementById('platformCompanyVat').value.trim(),
        sector:document.getElementById('platformCompanySector').value.trim(),
        admin_name:document.getElementById('platformCompanyAdminName').value.trim(),
        admin_email:document.getElementById('platformCompanyAdminEmail').value.trim(),
        admin_password:document.getElementById('platformCompanyAdminPassword').value
      })
    });

    document.getElementById('platformCompanyName').value='';
    document.getElementById('platformCompanyVat').value='';
    document.getElementById('platformCompanySector').value='';
    document.getElementById('platformCompanyAdminName').value='';
    document.getElementById('platformCompanyAdminEmail').value='';
    document.getElementById('platformCompanyAdminPassword').value='';
    result.innerHTML=`<div class="result-success"><strong>${data.company.name}</strong> creata. Amministratore aziendale: ${data.company_admin.email}</div>`;
    await loadPlatformCompanies();
  }catch(e){
    result.innerHTML=`<div class="result-error">${e.message||e}</div>`;
  }finally{
    btn.disabled=false;
    btn.textContent='Crea azienda cliente';
  }
}

function showAuthMode(mode){
  const screen=document.getElementById('authScreen');
  screen.classList.remove('hidden');
  document.getElementById('setupPanel').classList.toggle('hidden',mode!=='setup');
  document.getElementById('loginPanel').classList.toggle('hidden',mode!=='login');

  if(mode==='setup'){
    setTimeout(()=>document.getElementById('setupName').focus(),50);
  }else{
    setTimeout(()=>document.getElementById('loginEmail').focus(),50);
  }
}

function hideAuth(){
  document.getElementById('authScreen').classList.add('hidden');
}

function updateCurrentUserUI(){
  document.getElementById('currentUserName').textContent=
    currentUser?.full_name || currentUser?.email || 'Utente';
  document.getElementById('currentUserEmail').textContent=
    currentUser?.email || '';
}

async function setupFirstAdmin(){
  const result=document.getElementById('setupResult');
  const btn=document.getElementById('setupBtn');

  btn.disabled=true;
  btn.textContent='Creazione...';

  try{
    await getJSON('/api/auth/setup',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        full_name:document.getElementById('setupName').value.trim(),
        email:document.getElementById('setupEmail').value.trim(),
        password:document.getElementById('setupPassword').value
      })
    });
    await bootstrapApp();
  }catch(e){
    result.innerHTML=`<div class="result-error">${e.message||e}</div>`;
  }finally{
    btn.disabled=false;
    btn.textContent='Crea gestore piattaforma';
  }
}

async function login(){
  const result=document.getElementById('loginResult');
  const btn=document.getElementById('loginBtn');

  btn.disabled=true;
  btn.textContent='Accesso...';

  try{
    await getJSON('/api/auth/login',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        email:document.getElementById('loginEmail').value.trim(),
        password:document.getElementById('loginPassword').value
      })
    });
    document.getElementById('loginPassword').value='';
    await bootstrapApp();
  }catch(e){
    result.innerHTML=`<div class="result-error">${e.message||e}</div>`;
  }finally{
    btn.disabled=false;
    btn.textContent='Accedi';
  }
}

async function logout(){
  try{
    await getJSON('/api/auth/logout',{method:'POST'});
  }catch(e){
    console.error(e);
  }
  currentUser=null;
  hidePlatformAdmin();
  companiesCache=[];
  activeCompanyId=0;
  localStorage.removeItem('ai_acquisti_company_id');
  showAuthMode('login');
}

async function loadCompanyUsers(){
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

async function createCompanyUser(){
  const result=document.getElementById('newUserResult');
  const btn=document.getElementById('createUserBtn');

  btn.disabled=true;
  btn.textContent='Creazione...';

  try{
    await getJSON('/api/platform/company-users',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        full_name:document.getElementById('newUserName').value.trim(),
        email:document.getElementById('newUserEmail').value.trim(),
        password:document.getElementById('newUserPassword').value,
        role:document.getElementById('newUserRole').value
      })
    });

    document.getElementById('newUserName').value='';
    document.getElementById('newUserEmail').value='';
    document.getElementById('newUserPassword').value='';
    document.getElementById('newUserRole').value='OPERATOR';
    result.innerHTML='<div class="result-success">Utente creato correttamente.</div>';
    await loadCompanyUsers();
  }catch(e){
    result.innerHTML=`<div class="result-error">${e.message||e}</div>`;
  }finally{
    btn.disabled=false;
    btn.textContent='Crea utente';
  }
}


document.querySelectorAll('.nav[data-view]').forEach(btn=>{
  btn.addEventListener('click',()=>{
    document.querySelectorAll('.nav[data-view]').forEach(x=>x.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.view').forEach(v=>v.classList.remove('active-view'));
    document.getElementById(btn.dataset.view).classList.add('active-view');
    if(btn.dataset.view==='dashboardView') loadDashboard();
    if(btn.dataset.view==='documentsView') loadDocuments();
    if(btn.dataset.view==='priceListsView'){ loadPriceListSuppliers(); loadPriceLists(); }
    if(btn.dataset.view==='usersView') loadCompanyUsers();
    if(btn.dataset.view==='productsView') loadProductsPage();
    if(btn.dataset.view==='suppliersView') loadSuppliersPage();
  });
});

function drawBars(id,data,labelKey,valueKey){
  const el=document.getElementById(id); const max=Math.max(...data.map(x=>Number(x[valueKey])),1);
  el.innerHTML=data.map(x=>`<div class="bar-row"><div class="bar-label"><span>${x[labelKey]}</span><strong>${fmtEuro(x[valueKey])}</strong></div><div class="bar-track"><div class="bar-fill" style="width:${Math.max(4,Number(x[valueKey])/max*100)}%"></div></div></div>`).join('');
}

async function loadDashboard(){
  loadDashboardInbox().catch(console.error);
  loadDashboardReviews().catch(console.error);

  const [summary, anomalySummary, monthly] = await Promise.all([
    getJSON('/api/summary'),
    getJSON('/api/anomalies/summary'),
    getJSON('/api/monthly-spend')
  ]);

  document.getElementById('period').textContent = monthName(summary.current_month);
  document.getElementById('spend').textContent = fmtEuro(summary.spend);

  const variation = document.getElementById('variation');
  variation.textContent = `${summary.variation_pct>=0?'+':''}${summary.variation_pct}%`;
  variation.style.color = summary.variation_pct>0 ? '#c6362c' : '#168357';

  document.getElementById('anomalyCost').textContent = fmtEuro(anomalySummary.extra_cost_total || 0);
  document.getElementById('docCheck').textContent = summary.documents_to_manage ?? summary.documents_to_check ?? 0;

  drawBars('monthly', monthly, 'month', 'total');
  await loadDashboardAnomalies();
}


function documentTypeLabel(type){
  const labels={
    'TD01':'Fattura',
    'TD02':'Acconto fattura',
    'TD03':'Acconto parcella',
    'TD04':'Nota di credito',
    'TD05':'Nota di debito',
    'TD06':'Parcella',
    'TD16':'Integrazione',
    'TD17':'Integrazione estero',
    'TD18':'Acquisto intracomunitario',
    'TD19':'Integrazione acquisti',
    'TD20':'Autofattura'
  };
  return labels[type] || type || 'Documento';
}

async function loadDashboardInbox(){
  const el=document.getElementById('dashboardInbox');
  if(!el)return;

  const rows=await getJSON('/api/documents/dashboard-inbox');
  const count=document.getElementById('dashboardInboxCount');
  if(count){
    count.textContent=`${rows.length} ${rows.length===1?'documento':'documenti'}`;
  }

  if(!rows.length){
    el.innerHTML=`<div class="dashboard-inbox-empty">
      <strong>Nessun documento da gestire</strong>
      <span>La coda è vuota. I prossimi documenti importati compariranno automaticamente qui.</span>
    </div>`;
    return;
  }

  el.innerHTML=`<div class="dashboard-inbox-table">
    <div class="dashboard-inbox-head">
      <span>Data</span>
      <span>Fornitore</span>
      <span>Tipo</span>
      <span>Documento</span>
      <span>Totale</span>
      <span>Stato</span>
    </div>
    ${rows.map(r=>`<div class="dashboard-inbox-row" onclick="openDashboardDocument(${r.id})" title="Apri il documento">
      <span>${r.document_date||'—'}</span>
      <span><strong>${r.supplier||'—'}</strong></span>
      <span>${documentTypeLabel(r.document_type)}</span>
      <span><strong>${r.document_number||'—'}</strong></span>
      <span>${fmtEuro(r.total_amount)}</span>
      <span>${statusBadge(r.status)}</span>
    </div>`).join('')}
  </div>`;
}

async function loadDashboardAnomalies(){
  const rows = await getJSON('/api/anomalies/dashboard');
  const el = document.getElementById('dashboardAnomalies');

  if(!rows.length){
    el.innerHTML = '<div class="dashboard-empty">Nessuna anomalia rilevata.</div>';
    return;
  }

  el.innerHTML = `<table class="dashboard-anomaly-table">
    <thead>
      <tr>
        <th>Fornitore</th>
        <th>Fattura</th>
        <th>Anomalia</th>
        <th class="impact-col">Impatto</th>
      </tr>
    </thead>
    <tbody>
      ${rows.map(r=>`<tr class="anomaly-row-click" onclick="openAnomalyDocument(${r.document_id})" title="Apri il dettaglio della fattura">
        <td><strong>${r.supplier || '—'}</strong></td>
        <td>
          <strong>${r.document_number || '—'}</strong>
          <small>${r.document_date || ''}</small>
        </td>
        <td>${r.anomaly || 'Differenza rispetto al listino'}</td>
        <td class="impact-col ${Number(r.impact||0)>0 ? 'variance-bad' : 'variance-good'}">
          ${Number(r.impact||0)>0 ? '+' : ''}${fmtEuro(r.impact)}
        </td>
      </tr>`).join('')}
    </tbody>
  </table>`;
}

window.openAnomalyDocument = async id => {
  const btn = document.querySelector('.nav[data-view="documentsView"]');
  if(btn) btn.click();
  await loadDocuments();
  await window.showDocument(id);
  const panel = document.getElementById('docDetailPanel');
  if(panel) panel.scrollIntoView({behavior:'smooth', block:'start'});
};

async function loadProductHistory(id){
  const data=await getJSON(`/api/products/${id}/history`);
  document.getElementById('productHistory').innerHTML=`<table><thead><tr><th>Data</th><th>Fornitore</th><th>Quantità</th><th>Prezzo unitario</th><th>Totale</th></tr></thead><tbody>${data.map(r=>`<tr><td>${r.purchase_date}</td><td>${r.supplier}</td><td>${r.quantity}</td><td>${fmtEuro(r.unit_price)}</td><td>${fmtEuro(r.total)}</td></tr>`).join('')}</tbody></table>`;
}
const dashboardProductSelect=document.getElementById('productSelect'); if(dashboardProductSelect) dashboardProductSelect.addEventListener('change',e=>loadProductHistory(e.target.value));

async function ask(){
  const input=document.getElementById('question');
  const btn=document.getElementById('askBtn');
  const q=input.value.trim();
  if(!q)return;

  const chat=document.getElementById('chat');
  chat.innerHTML+=`<div class="user-message">${q}</div>`;
  input.value='';
  btn.disabled=true;
  btn.textContent='Analizzo...';

  const waitingId='ai-wait-'+Date.now();
  chat.innerHTML+=`<div id="${waitingId}" class="ai-message ai-waiting">Sto leggendo i dati del gestionale…</div>`;
  chat.scrollTop=chat.scrollHeight;

  try{
    const data=await getJSON('/api/assistant',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({question:q})
    });
    const waiting=document.getElementById(waitingId);
    if(waiting) waiting.innerHTML=(data.answer||'Nessuna risposta.').replace(/\n/g,'<br>');
  }catch(err){
    const waiting=document.getElementById(waitingId);
    if(waiting) waiting.innerHTML=`<strong>AI non disponibile.</strong><br>${err.message||err}`;
  }finally{
    btn.disabled=false;
    btn.textContent='Invia';
    input.focus();
    chat.scrollTop=chat.scrollHeight;
  }
}
document.getElementById('askBtn').addEventListener('click',ask);
document.getElementById('question').addEventListener('keydown',e=>{if(e.key==='Enter')ask();});

function statusLabel(status){
  const labels={
    'IMPORTATA':'Importata',
    'CON_ANOMALIE':'Con anomalie',
    'DA_VERIFICARE':'Da verificare',
    'DUPLICATA':'Duplicata',
    'ERRORE':'Errore',
    'IMPORTATO_SENZA_MOVIMENTI':'Importata senza movimenti'
  };
  return labels[status] || status || '—';
}

function statusBadge(status){
  const cls={
    'IMPORTATA':'status-ok',
    'CON_ANOMALIE':'status-anomaly',
    'DA_VERIFICARE':'status-review',
    'DUPLICATA':'status-duplicate',
    'ERRORE':'status-error',
    'IMPORTATO_SENZA_MOVIMENTI':'status-neutral'
  }[status] || 'status-neutral';
  return `<span class="document-status ${cls}">${statusLabel(status)}</span>`;
}

function workflowBadge(workflowStatus){
  const handled=workflowStatus==='GESTITO';
  return `<span class="workflow-badge ${handled?'workflow-done':'workflow-open'}">${handled?'Gestita':'Da gestire'}</span>`;
}

async function uploadXML(){
  const input=document.getElementById('xmlFile');
  const files=[...input.files];
  const result=document.getElementById('uploadResult');

  if(!files.length){
    result.innerHTML='<div class="result-error">Seleziona almeno una fattura XML o PDF.</div>';
    return;
  }

  const btn=document.getElementById('uploadBtn');
  btn.disabled=true;

  const results=[];
  result.innerHTML=`<div class="multi-import-progress">Preparazione di ${files.length} fatture…</div>`;

  for(let i=0;i<files.length;i++){
    const file=files[i];
    btn.textContent=`Importazione ${i+1}/${files.length}…`;

    const fd=new FormData();
    fd.append('file',file);

    try{
      const r=await getJSON('/api/documents/upload',{method:'POST',body:fd});
      results.push({
        file:file.name,
        status:r.status || (r.duplicate?'DUPLICATA':'IMPORTATA'),
        supplier:r.supplier || '',
        document_number:r.document_number || '',
        anomaly_count:r.anomaly_count || 0,
        review_reason:r.review_reason || '',
        message:r.message || ''
      });
    }catch(e){
      results.push({
        file:file.name,
        status:'ERRORE',
        supplier:'',
        document_number:'',
        anomaly_count:0,
        review_reason:'',
        message:e.message || String(e)
      });
    }

    const counts=results.reduce((acc,x)=>{
      acc[x.status]=(acc[x.status]||0)+1;
      return acc;
    },{});

    result.innerHTML=renderImportResults(results, files.length, counts, i+1);
  }

  input.value='';
  btn.disabled=false;
  btn.textContent='Importa fatture';

  await loadDocuments();
  await loadDashboard();
}

function renderImportResults(rows,total,counts,processed){
  return `<div class="multi-import-summary">
    <div class="multi-import-head">
      <strong>Elaborate ${processed} di ${total} fatture</strong>
      <span>
        ${counts.IMPORTATA||0} importate ·
        ${counts.CON_ANOMALIE||0} con anomalie ·
        ${counts.DA_VERIFICARE||0} da verificare ·
        ${counts.DUPLICATA||0} duplicate ·
        ${counts.ERRORE||0} errori
      </span>
    </div>
    <div class="multi-import-list">
      ${rows.map(r=>`<div class="multi-import-row">
        <div class="multi-file">
          <strong>${r.file}</strong>
          <small>${[r.supplier,r.document_number].filter(Boolean).join(' · ')}</small>
        </div>
        <div>${statusBadge(r.status)}</div>
        <div class="multi-note">
          ${r.status==='CON_ANOMALIE'
            ? `${r.anomaly_count} anomalia/e rilevata/e`
            : r.status==='DA_VERIFICARE'
              ? (r.review_reason||'Controllo richiesto')
              : r.message||''}
        </div>
      </div>`).join('')}
    </div>
  </div>`;
}
document.getElementById('uploadBtn').addEventListener('click',uploadXML);
document.getElementById('refreshDocs').addEventListener('click',loadDocuments);

let documentsCache=[];
let currentDocumentFilter='ALL';

async function loadDocuments(){
  documentsCache=await getJSON('/api/documents');

  document.getElementById('filterAllCount').textContent=documentsCache.length;
  document.getElementById('filterAnomalyCount').textContent=
    documentsCache.filter(d=>d.status==='CON_ANOMALIE').length;
  document.getElementById('filterReviewCount').textContent=
    documentsCache.filter(d=>d.status==='DA_VERIFICARE').length;

  renderDocuments();
}

function renderDocuments(){
  const el=document.getElementById('documentsTable');
  const docs=currentDocumentFilter==='ALL'
    ? documentsCache
    : documentsCache.filter(d=>d.status===currentDocumentFilter);

  if(!docs.length){
    const message=currentDocumentFilter==='CON_ANOMALIE'
      ? 'Nessuna fattura con anomalie.'
      : currentDocumentFilter==='DA_VERIFICARE'
        ? 'Nessuna fattura da verificare.'
        : 'Nessuna fattura importata.';
    el.innerHTML=`<div class="empty-state">${message}</div>`;
    return;
  }

  el.innerHTML=`<table>
    <thead><tr>
      <th>Data</th>
      <th>Fornitore</th>
      <th>Fattura</th>
      <th>Origine</th>
      <th>Totale</th>
      <th>Stato / Gestione</th>
    </tr></thead>
    <tbody>${docs.map(d=>`<tr class="clickable" onclick="showDocument(${d.id})">
      <td>${d.document_date||'—'}</td>
      <td>${d.supplier||'—'}</td>
      <td><strong>${d.document_number||'—'}</strong></td>
      <td>${d.source==='PDF_AI'?'PDF AI':'XML'}</td>
      <td>${fmtEuro(d.total_amount)}</td>
      <td><div class="status-stack">${statusBadge(d.status)}${workflowBadge(d.workflow_status)}</div></td>
    </tr>`).join('')}</tbody>
  </table>`;
}

document.querySelectorAll('.doc-filter').forEach(btn=>{
  btn.addEventListener('click',()=>{
    document.querySelectorAll('.doc-filter').forEach(x=>x.classList.remove('active'));
    btn.classList.add('active');
    currentDocumentFilter=btn.dataset.docFilter;
    renderDocuments();
  });
});

window.closeDocumentDetail=()=>{
  const panel=document.getElementById('docDetailPanel');
  if(panel)panel.classList.add('hidden');

  const table=document.getElementById('documentsTable');
  if(table){
    setTimeout(()=>table.scrollIntoView({behavior:'smooth',block:'start'}),50);
  }
};

window.showDocument=async id=>{
  const data=await getJSON(`/api/documents/${id}`), d=data.document, panel=document.getElementById('docDetailPanel'); panel.classList.remove('hidden');
  document.getElementById('docDetailMeta').innerHTML=`<div class="detail-status-meta">${statusBadge(d.status)}${workflowBadge(d.workflow_status)}<span>· ID ${d.id}</span></div>`;
  const isPending=(d.workflow_status||'DA_GESTIRE')==='DA_GESTIRE';

  const reviewBox=d.status==='DA_VERIFICARE' && isPending
    ? `<div class="document-status-strip review">
        <strong>Da verificare</strong>
        <span>${(d.review_reason||'Confermare i dati letti dal sistema.').replace(/\n/g,' · ')}</span>
      </div>`
    : d.status==='CON_ANOMALIE' && isPending
      ? `<div class="document-status-strip anomaly">
          <strong>Con anomalie</strong>
          <span>Controlla le righe evidenziate nella tabella.</span>
        </div>`
      : isPending
        ? `<div class="document-status-strip ready">
            <strong>Importato</strong>
            <span>Controlla il documento e chiudilo quando hai terminato.</span>
          </div>`
        : `<div class="document-status-strip closed">
            <strong>Gestito</strong>
            <span>Il documento resta disponibile nello storico.</span>
          </div>`;

  const finalAction=d.status==='DA_VERIFICARE' && isPending
    ? `<div class="document-final-action">
        <div class="document-final-copy">
          <strong>Operazione documento</strong>
          <span>Conferma prima i dati letti dal sistema.</span>
        </div>
        <button class="confirm-document-btn" onclick="confirmDocument(${d.id})">Conferma dati</button>
      </div>`
    : d.status==='CON_ANOMALIE' && isPending
      ? `<div class="document-final-action">
          <div class="document-final-copy">
            <strong>Analisi completata?</strong>
            <span>Dopo la chiusura la fattura sparirà dalla Dashboard ma resterà nello storico.</span>
          </div>
          <button class="confirm-document-btn close-analysis-btn" onclick="markDocumentHandled(${d.id},'anomaly')">Chiudi fattura analizzata</button>
        </div>`
      : isPending
        ? `<div class="document-final-action">
            <div class="document-final-copy">
              <strong>Controllo completato?</strong>
              <span>Dopo la chiusura il documento sparirà dalla Dashboard ma resterà nello storico.</span>
            </div>
            <button class="confirm-document-btn" onclick="markDocumentHandled(${d.id},'normal')">Chiudi documento</button>
          </div>`
        : `<div class="document-final-action document-final-action-closed">
            <div class="document-final-copy">
              <strong>Documento già chiuso</strong>
              <span>La lavorazione è terminata. Il documento resta disponibile nello storico.</span>
            </div>
            <button class="confirm-document-btn document-closed-btn" disabled>Documento già chiuso</button>
          </div>`;

  document.getElementById('docDetail').innerHTML=`${reviewBox}<div class="meta-grid">
    <div class="meta-box"><span>Fornitore</span><strong>${d.supplier||''}</strong></div>
    <div class="meta-box"><span>P.IVA</span><strong>${d.vat_number||''}</strong></div>
    <div class="meta-box"><span>Documento</span><strong>${d.document_number||''} · ${d.document_date||''}</strong></div>
    <div class="meta-box"><span>Totale</span><strong>${fmtEuro(d.total_amount)}</strong></div>
  </div>
  <table>
    <thead><tr>
      <th>Riga</th>
      <th>Codice articolo fornitore</th>
      <th>Descrizione</th>
      <th>Tipo riga</th>
      <th>Prodotto associato</th>
      <th>Q.tà</th>
      <th>Prezzo listino fattura</th>
      <th>Sconto 1</th>
      <th>Sconto 2</th>
      <th>Netto fattura</th>
      <th>Netto listino</th>
      <th>Differenza totale</th>
      <th>Esito</th>
    </tr></thead>
    <tbody>${data.lines.map(l=>`<tr class="${l.price_check_status==='DIFFERENZA'?'invoice-line-anomaly':''}">
      <td>${l.line_number}</td>
      <td>${l.supplier_code||'—'}</td>
      <td>${l.original_description||''}</td>
      <td>${l.line_type==='ANCILLARY'?'Spesa accessoria':'Prodotto'}</td>
      <td>${l.product_name||l.product_id||'—'}</td>
      <td>${l.quantity} ${l.unit||''}</td>
      <td>${fmtEuro(l.invoice_list_price ?? l.unit_price)}</td>
      <td>${Number(l.invoice_discount_1||0).toFixed(2)}%</td>
      <td>${Number(l.invoice_discount_2||0).toFixed(2)}%</td>
      <td><strong>${fmtEuro(l.invoice_net_price ?? l.unit_price)}</strong></td>
      <td>${l.expected_net_price != null ? fmtEuro(l.expected_net_price) : '—'}</td>
      <td class="${l.price_check_status==='DIFFERENZA'?'variance-cell':''}">${l.price_variance_total != null ? fmtEuro(l.price_variance_total) : '—'}</td>
      <td>${l.price_check_status==='OK'
        ? '<span class="check-pill ok">OK</span>'
        : l.price_check_status==='DIFFERENZA'
          ? '<span class="check-pill bad">DIFFERENZA</span>'
          : (l.price_check_status==='NON_APPLICABILE' || l.line_type==='ANCILLARY')
            ? '<span class="check-pill neutral">ACCESSORIA</span>'
            : '<span class="check-pill neutral">SENZA LISTINO</span>'}</td>
    </tr>`).join('')}</tbody>
  </table>
  ${finalAction}`;
  panel.scrollIntoView({behavior:'smooth'});
};

window.confirmDocument=async id=>{
  const ok=window.confirm(
    'Confermi che i dati letti nella fattura sono corretti? Dopo la conferma entreranno nello storico acquisti.'
  );
  if(!ok)return;

  try{
    const result=await getJSON(`/api/documents/${id}/confirm`,{method:'POST'});
    await loadDocuments();
    await loadDashboard();
    await window.showDocument(id);
    alert(
      result.status==='CON_ANOMALIE'
        ? 'Dati confermati. Il documento resta in Dashboard perché contiene anomalie da verificare.'
        : 'Dati confermati. Il documento è stato gestito e rimosso dalla coda Dashboard.'
    );
  }catch(e){
    alert(e.message||e);
  }
};


window.markDocumentHandled=async (id,mode)=>{
  const message=mode==='anomaly'
    ? 'Confermi di aver terminato l’analisi della fattura? Verrà rimossa dalla Dashboard ma resterà nello storico Documenti.'
    : 'Confermi che questo documento è stato controllato? Verrà rimosso dalla coda Dashboard ma resterà nello storico.';

  if(!window.confirm(message))return;

  try{
    await getJSON(`/api/documents/${id}/mark-handled`,{method:'POST'});
    await loadDocuments();
    await loadDashboard();
    await window.showDocument(id);
  }catch(e){
    alert(e.message||e);
  }
};


async function loadDashboardReviews(){
  const el=document.getElementById('dashboardReviews');
  if(!el)return;

  const rows=await getJSON('/api/documents/review-dashboard');

  if(!rows.length){
    el.innerHTML=`<div class="dashboard-empty-ok">
      <strong>Nessuna fattura da verificare</strong>
      <span>Non ci sono documenti in attesa di conferma.</span>
    </div>`;
    return;
  }

  el.innerHTML=`<div class="dashboard-invoice-list">
    <div class="dashboard-invoice-head">
      <span>Fornitore</span>
      <span>Fattura</span>
      <span>Motivo</span>
      <span>Totale</span>
    </div>
    ${rows.map(r=>`<div class="dashboard-invoice-row review-row" onclick="openDashboardDocument(${r.id})" title="Apri il dettaglio della fattura">
      <span><strong>${r.supplier||'—'}</strong></span>
      <span>${r.document_number||'—'}</span>
      <span class="dashboard-reason">${(r.review_reason||'Conferma richiesta').split('\n')[0]}</span>
      <span>${fmtEuro(r.total_amount)}</span>
    </div>`).join('')}
  </div>`;
}

window.openDashboardDocument=async id=>{
  const nav=document.querySelector('[data-view="documentsView"]');
  if(nav)nav.click();
  await loadDocuments();
  await window.showDocument(id);
  setTimeout(()=>{
    const detail=document.getElementById('docDetail');
    if(detail)detail.scrollIntoView({behavior:'smooth',block:'start'});
  },80);
};


document.getElementById('companySelect').addEventListener('change',e=>{
  if(e.target.value)selectCompany(e.target.value).catch(console.error);
});
document.getElementById('newCompanyBtn').addEventListener('click',()=>showCompanyModal(false));
document.getElementById('cancelCompanyBtn').addEventListener('click',hideCompanyModal);
document.getElementById('createCompanyBtn').addEventListener('click',createCompanyFromModal);
document.getElementById('companyName').addEventListener('keydown',e=>{
  if(e.key==='Enter')createCompanyFromModal();
});


document.getElementById('setupBtn').addEventListener('click',setupFirstAdmin);
document.getElementById('loginBtn').addEventListener('click',login);
document.getElementById('logoutBtn').addEventListener('click',logout);
document.getElementById('createUserBtn').addEventListener('click',createCompanyUser);

document.getElementById('setupPassword').addEventListener('keydown',e=>{
  if(e.key==='Enter')setupFirstAdmin();
});
document.getElementById('loginPassword').addEventListener('keydown',e=>{
  if(e.key==='Enter')login();
});


document.getElementById('newSupplierBtn').addEventListener('click',showSupplierModal);
document.getElementById('cancelSupplierBtn').addEventListener('click',hideSupplierModal);
document.getElementById('createSupplierBtn').addEventListener('click',createSupplierFromModal);
document.getElementById('supplierName').addEventListener('keydown',e=>{
  if(e.key==='Enter')createSupplierFromModal();
});

bootstrapApp();


// ---------------- MVP 0.3 - Pagina Prodotti ----------------
let productsOverviewCache = [];

function formatChange(value){
  if(value === null || value === undefined) return '<span class="price-change flat">—</span>';
  const cls = value > 0.1 ? 'up' : value < -0.1 ? 'down' : 'flat';
  const sign = value > 0 ? '+' : '';
  return `<span class="price-change ${cls}">${sign}${Number(value).toFixed(1)}%</span>`;
}

async function loadProductsPage(){
  productsOverviewCache = await getJSON('/api/products-overview');

  const categories = [...new Set(
    productsOverviewCache.map(p => p.category || 'Da classificare')
  )].sort((a,b)=>a.localeCompare(b,'it'));

  const categorySelect = document.getElementById('productsCategory');
  const currentCategory = categorySelect.value;
  categorySelect.innerHTML =
    '<option value="">Tutte</option>' +
    categories.map(c=>`<option value="${c}">${c}</option>`).join('');
  if(categories.includes(currentCategory)) categorySelect.value = currentCategory;

  document.getElementById('productsTotal').textContent = productsOverviewCache.length;
  document.getElementById('categoriesTotal').textContent = categories.length;
  document.getElementById('productsPurchased').textContent =
    productsOverviewCache.filter(p=>p.purchase_count > 0).length;
  document.getElementById('productsNoPurchases').textContent =
    productsOverviewCache.filter(p=>p.purchase_count === 0).length;

  renderProductsTable();
}

function renderProductsTable(){
  const search = document.getElementById('productsSearch').value.trim().toLowerCase();
  const category = document.getElementById('productsCategory').value;

  const filtered = productsOverviewCache.filter(p=>{
    const haystack = [
      p.name, p.category, p.unit, p.last_supplier, p.id
    ].filter(Boolean).join(' ').toLowerCase();

    return (!search || haystack.includes(search)) &&
           (!category || (p.category || 'Da classificare') === category);
  });

  document.getElementById('productsCountLabel').textContent =
    `${filtered.length} prodotti visualizzati`;

  const el = document.getElementById('productsTable');
  if(!filtered.length){
    el.innerHTML = '<div class="empty-state">Nessun prodotto corrisponde ai filtri.</div>';
    return;
  }

  el.innerHTML = `<table>
    <thead>
      <tr>
        <th>Prodotto</th>
        <th>Categoria</th>
        <th>U.M.</th>
        <th>Ultimo prezzo</th>
        <th>Variazione</th>
        <th>Ultimo fornitore</th>
        <th>Ultimo acquisto</th>
      </tr>
    </thead>
    <tbody>
      ${filtered.map(p=>`
        <tr class="clickable" onclick="showProductDetail('${p.id}')">
          <td class="product-name-cell">
            <strong>${p.name}</strong>
            <small>${p.id}</small>
          </td>
          <td>${p.category || 'Da classificare'}</td>
          <td>${p.unit || ''}</td>
          <td>${p.last_price !== null ? fmtEuro(p.last_price) : '—'}</td>
          <td>${formatChange(p.change_pct)}</td>
          <td>${p.last_supplier || '—'}</td>
          <td>${p.last_purchase || '—'}</td>
        </tr>
      `).join('')}
    </tbody>
  </table>`;
}

window.showProductDetail = async productId => {
  const data = await getJSON(`/api/products/${productId}/detail`);
  const p = data.product, s = data.stats;

  document.getElementById('productDetailTitle').textContent = p.name;
  document.getElementById('productDetailSubtitle').textContent =
    `${p.id} · ${p.category || 'Da classificare'} · U.M. ${p.unit || '—'}`;

  const suppliersTable = data.suppliers.length ? `
    <table>
      <thead><tr><th>Fornitore</th><th>Acquisti</th><th>Prezzo medio</th><th>Spesa</th><th>Ultimo acquisto</th></tr></thead>
      <tbody>${data.suppliers.map(r=>`
        <tr>
          <td>${r.name}</td>
          <td>${r.purchases}</td>
          <td>${fmtEuro(r.average_price)}</td>
          <td>${fmtEuro(r.total_spend)}</td>
          <td>${r.last_purchase || '—'}</td>
        </tr>`).join('')}
      </tbody>
    </table>` : '<div class="empty-state">Nessun fornitore associato.</div>';

  const historyTable = data.history.length ? `
    <table>
      <thead><tr><th>Data</th><th>Fornitore</th><th>Quantità</th><th>Prezzo</th><th>Totale</th></tr></thead>
      <tbody>${data.history.map(r=>`
        <tr>
          <td>${r.purchase_date}</td>
          <td>${r.supplier}</td>
          <td>${r.quantity}</td>
          <td>${fmtEuro(r.unit_price)}</td>
          <td>${fmtEuro(r.total)}</td>
        </tr>`).join('')}
      </tbody>
    </table>` : '<div class="empty-state">Nessun acquisto registrato.</div>';

  document.getElementById('productDetail').innerHTML = `
    <div class="detail-summary">
      <div class="detail-stat"><span>Acquisti registrati</span><strong>${s.purchases || 0}</strong></div>
      <div class="detail-stat"><span>Quantità totale</span><strong>${s.total_quantity || 0} ${p.unit || ''}</strong></div>
      <div class="detail-stat"><span>Spesa totale</span><strong>${fmtEuro(s.total_spend)}</strong></div>
      <div class="detail-stat"><span>Prezzo medio</span><strong>${s.average_price !== null ? fmtEuro(s.average_price) : '—'}</strong></div>
      <div class="detail-stat"><span>Prezzo minimo</span><strong>${s.min_price !== null ? fmtEuro(s.min_price) : '—'}</strong></div>
      <div class="detail-stat"><span>Prezzo massimo</span><strong>${s.max_price !== null ? fmtEuro(s.max_price) : '—'}</strong></div>
    </div>
    <div class="detail-columns">
      <div class="detail-block">
        <h3>Fornitori utilizzati</h3>
        ${suppliersTable}
      </div>
      <div class="detail-block">
        <h3>Storico acquisti</h3>
        ${historyTable}
      </div>
    </div>`;

  const panel = document.getElementById('productDetailPanel');
  panel.classList.remove('hidden');
  panel.scrollIntoView({behavior:'smooth', block:'start'});
};

document.getElementById('productsSearch').addEventListener('input', renderProductsTable);
document.getElementById('productsCategory').addEventListener('change', renderProductsTable);
document.getElementById('closeProductDetail').addEventListener('click', ()=>{
  document.getElementById('productDetailPanel').classList.add('hidden');
});


// ---------------- MVP 0.4 - Pagina Fornitori ----------------
let suppliersOverviewCache = [];

async function loadSuppliersPage(){
  suppliersOverviewCache = await getJSON('/api/suppliers-overview');

  const categories = [...new Set(
    suppliersOverviewCache.map(s => s.category || 'Da classificare')
  )].sort((a,b)=>a.localeCompare(b,'it'));

  const categorySelect = document.getElementById('suppliersCategory');
  const currentCategory = categorySelect.value;
  categorySelect.innerHTML =
    '<option value="">Tutte</option>' +
    categories.map(c=>`<option value="${c}">${c}</option>`).join('');
  if(categories.includes(currentCategory)) categorySelect.value = currentCategory;

  document.getElementById('suppliersTotal').textContent = suppliersOverviewCache.length;
  document.getElementById('suppliersWithPurchases').textContent =
    suppliersOverviewCache.filter(s=>s.purchase_count > 0).length;

  const spendTotal = suppliersOverviewCache.reduce((sum,s)=>sum+Number(s.total_spend||0),0);
  document.getElementById('suppliersSpendTotal').textContent = fmtEuro(spendTotal);

  const docsTotal = suppliersOverviewCache.reduce((sum,s)=>sum+Number(s.document_count||0),0);
  document.getElementById('suppliersDocsTotal').textContent = docsTotal;

  renderSuppliersTable();
}

function renderSuppliersTable(){
  const search = document.getElementById('suppliersSearch').value.trim().toLowerCase();
  const category = document.getElementById('suppliersCategory').value;

  const filtered = suppliersOverviewCache.filter(s=>{
    const haystack = [
      s.name,s.vat_number,s.category,s.id
    ].filter(Boolean).join(' ').toLowerCase();

    return (!search || haystack.includes(search)) &&
           (!category || (s.category || 'Da classificare') === category);
  });

  document.getElementById('suppliersCountLabel').textContent =
    `${filtered.length} fornitori visualizzati`;

  const el = document.getElementById('suppliersTable');
  if(!filtered.length){
    el.innerHTML = '<div class="empty-state">Nessun fornitore corrisponde ai filtri.</div>';
    return;
  }

  el.innerHTML = `<table>
    <thead>
      <tr>
        <th>Fornitore</th>
        <th>Categoria</th>
        <th>Prodotti</th>
        <th>Acquisti</th>
        <th>Spesa totale</th>
        <th>Vs mese precedente</th>
        <th>Ultimo acquisto</th>
        <th>Documenti</th>
      </tr>
    </thead>
    <tbody>
      ${filtered.map(s=>`
        <tr class="clickable" onclick="showSupplierDetail('${s.id}')">
          <td class="supplier-name-cell">
            <strong>${s.name}</strong>
            <small>${s.vat_number || s.id}</small>
          </td>
          <td>${s.category || 'Da classificare'}</td>
          <td>${s.product_count}</td>
          <td>${s.purchase_count}</td>
          <td>${fmtEuro(s.total_spend)}</td>
          <td>${formatChange(s.monthly_change_pct)}</td>
          <td>${s.last_purchase || '—'}</td>
          <td>${s.document_count}</td>
        </tr>
      `).join('')}
    </tbody>
  </table>`;
}

window.showSupplierDetail = async supplierId => {
  const data = await getJSON(`/api/suppliers/${supplierId}/detail`);
  const s = data.supplier, st = data.stats;

  document.getElementById('supplierDetailTitle').textContent = s.name;
  document.getElementById('supplierDetailSubtitle').textContent =
    `${s.id} · ${s.vat_number || 'P.IVA non disponibile'} · ${s.category || 'Da classificare'}`;

  const maxMonth = Math.max(...data.monthly.map(m=>Number(m.total||0)),1);
  const monthlyHtml = data.monthly.length ? `
    <div class="month-list">
      ${data.monthly.map(m=>`
        <div class="month-row">
          <span>${m.month}</span>
          <div class="bar-track">
            <div class="bar-fill" style="width:${Math.max(4,Number(m.total)/maxMonth*100)}%"></div>
          </div>
          <strong>${fmtEuro(m.total)}</strong>
        </div>`).join('')}
    </div>` : '<div class="empty-state">Nessun acquisto registrato.</div>';

  const productsHtml = data.products.length ? `
    <table>
      <thead><tr><th>Prodotto</th><th>Acquisti</th><th>Quantità</th><th>Prezzo medio</th><th>Spesa</th></tr></thead>
      <tbody>${data.products.map(p=>`
        <tr>
          <td>${p.name}</td>
          <td>${p.purchases}</td>
          <td>${p.total_quantity} ${p.unit || ''}</td>
          <td>${fmtEuro(p.average_price)}</td>
          <td>${fmtEuro(p.total_spend)}</td>
        </tr>`).join('')}
      </tbody>
    </table>` : '<div class="empty-state">Nessun prodotto acquistato.</div>';

  const docsHtml = data.documents.length ? `
    <table>
      <thead><tr><th>Data</th><th>Numero</th><th>Tipo</th><th>Totale</th><th>Stato</th></tr></thead>
      <tbody>${data.documents.map(d=>`
        <tr>
          <td>${d.document_date || '—'}</td>
          <td>${d.document_number || '—'}</td>
          <td>${d.document_type || '—'}</td>
          <td>${fmtEuro(d.total_amount)}</td>
          <td>${d.status || '—'}</td>
        </tr>`).join('')}
      </tbody>
    </table>` : '<div class="empty-state">Nessun documento importato per questo fornitore.</div>';

  document.getElementById('supplierDetail').innerHTML = `
    <div class="detail-summary">
      <div class="detail-stat"><span>Acquisti registrati</span><strong>${st.purchase_count || 0}</strong></div>
      <div class="detail-stat"><span>Prodotti diversi</span><strong>${st.product_count || 0}</strong></div>
      <div class="detail-stat"><span>Spesa totale</span><strong>${fmtEuro(st.total_spend)}</strong></div>
      <div class="detail-stat"><span>Primo acquisto</span><strong>${st.first_purchase || '—'}</strong></div>
      <div class="detail-stat"><span>Ultimo acquisto</span><strong>${st.last_purchase || '—'}</strong></div>
      <div class="detail-stat"><span>Documenti importati</span><strong>${data.documents.length}</strong></div>
    </div>

    <div class="supplier-detail-grid">
      <div class="detail-block">
        <h3>Andamento spesa mensile</h3>
        ${monthlyHtml}
      </div>
      <div class="detail-block">
        <h3>Prodotti acquistati</h3>
        ${productsHtml}
      </div>
    </div>

    <div class="detail-block" style="margin-top:18px">
      <h3>Documenti collegati</h3>
      ${docsHtml}
    </div>`;

  const panel = document.getElementById('supplierDetailPanel');
  panel.classList.remove('hidden');
  panel.scrollIntoView({behavior:'smooth', block:'start'});
};

document.getElementById('suppliersSearch').addEventListener('input', renderSuppliersTable);
document.getElementById('suppliersCategory').addEventListener('change', renderSuppliersTable);
document.getElementById('closeSupplierDetail').addEventListener('click', ()=>{
  document.getElementById('supplierDetailPanel').classList.add('hidden');
});


// ---------------- MVP 0.5 - Listini Excel / CSV ----------------

function showSupplierModal(){
  document.getElementById('supplierCreateResult').innerHTML='';
  document.getElementById('supplierModal').classList.remove('hidden');
  setTimeout(()=>document.getElementById('supplierName').focus(),50);
}

function hideSupplierModal(){
  document.getElementById('supplierModal').classList.add('hidden');
}

async function createSupplierFromModal(){
  const name=document.getElementById('supplierName').value.trim();
  const vat=document.getElementById('supplierVat').value.trim();
  const category=document.getElementById('supplierCategory').value.trim();
  const result=document.getElementById('supplierCreateResult');
  const btn=document.getElementById('createSupplierBtn');

  if(!name){
    result.innerHTML='<div class="result-error">Inserisci il nome del fornitore.</div>';
    return;
  }

  btn.disabled=true;
  btn.textContent='Creazione...';

  try{
    const supplier=await getJSON('/api/suppliers',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        name,
        vat_number:vat,
        category
      })
    });

    document.getElementById('supplierName').value='';
    document.getElementById('supplierVat').value='';
    document.getElementById('supplierCategory').value='';

    await loadPriceListSuppliers();
    document.getElementById('priceListSupplier').value=supplier.id;

    hideSupplierModal();

    const uploadResult=document.getElementById('priceListUploadResult');
    uploadResult.innerHTML=`<div class="result-ok">
      Fornitore <strong>${supplier.name}</strong> creato. Ora puoi selezionare il file del listino.
    </div>`;
  }catch(e){
    result.innerHTML=`<div class="result-error">${e.message||e}</div>`;
  }finally{
    btn.disabled=false;
    btn.textContent='Crea fornitore';
  }
}

async function loadPriceListSuppliers(){
  const suppliers = await getJSON('/api/suppliers-simple');
  const select = document.getElementById('priceListSupplier');
  const current = select.value;
  select.innerHTML =
    '<option value="">Seleziona fornitore...</option>' +
    suppliers.map(s=>`<option value="${s.id}">${s.name}</option>`).join('');
  if(suppliers.some(s=>s.id===current)) select.value = current;
}

async function uploadPriceList(){
  const supplierId = document.getElementById('priceListSupplier').value;
  const file = document.getElementById('priceListFile').files[0];
  const result = document.getElementById('priceListUploadResult');
  const btn = document.getElementById('priceListUploadBtn');

  if(!supplierId){
    result.innerHTML = '<div class="result-error">Seleziona prima il fornitore.</div>';
    return;
  }
  if(!file){
    result.innerHTML = '<div class="result-error">Seleziona un file CSV o XLSX.</div>';
    return;
  }

  const fd = new FormData();
  fd.append('file', file);

  btn.disabled = true;
  btn.textContent = 'Importazione...';

  try{
    const data = await getJSON(
      `/api/price-lists/upload?supplier_id=${encodeURIComponent(supplierId)}`,
      {method:'POST',body:fd}
    );

    result.innerHTML = `<div class="result-ok">
      <strong>Listino importato correttamente.</strong><br>
      Fornitore: ${data.supplier}<br>
      Righe valide: ${data.rows_count}<br>
      Prodotti già riconosciuti: ${data.matched_products}<br>
      Nuovi prodotti creati: ${data.created_products}<br>
      Righe ignorate: ${data.skipped_rows}
    </div>`;

    document.getElementById('priceListFile').value = '';
    await loadPriceLists();
    await loadProductsPage();
  }catch(e){
    result.innerHTML = `<div class="result-error"><strong>Importazione non riuscita.</strong><br>${e.message}</div>`;
  }finally{
    btn.disabled = false;
    btn.textContent = 'Importa listino';
  }
}

async function loadPriceLists(){
  const lists = await getJSON('/api/price-lists');
  const el = document.getElementById('priceListsTable');

  if(!lists.length){
    el.innerHTML = '<div class="empty-state">Nessun listino importato.</div>';
    return;
  }

  el.innerHTML = `<table>
    <thead><tr><th>ID</th><th>Fornitore</th><th>File</th><th>Righe</th><th>Importato</th><th>Stato</th></tr></thead>
    <tbody>${lists.map(l=>`
      <tr class="clickable" onclick="showPriceListDetail(${l.id})">
        <td>${l.id}</td>
        <td>${l.supplier}</td>
        <td>${l.file_name}</td>
        <td>${l.rows_count}</td>
        <td>${l.imported_at}</td>
        <td>${l.status}</td>
      </tr>`).join('')}
    </tbody>
  </table>`;
}

window.showPriceListDetail = async id => {
  const data = await getJSON(`/api/price-lists/${id}`);
  const h = data.price_list;

  document.getElementById('priceListDetailMeta').textContent =
    `${h.supplier} · ${h.file_name}`;

  document.getElementById('priceListDetail').innerHTML = `
    <div class="meta-grid">
      <div class="meta-box"><span>Fornitore</span><strong>${h.supplier}</strong></div>
      <div class="meta-box"><span>File</span><strong>${h.file_name}</strong></div>
      <div class="meta-box"><span>Righe</span><strong>${h.rows_count}</strong></div>
      <div class="meta-box"><span>Stato</span><strong>${h.status}</strong></div>
    </div>
    <table>
      <thead><tr><th>Riga</th><th>Codice articolo fornitore</th><th>Descrizione</th><th>Prodotto associato</th><th>Metodo</th><th>U.M.</th><th>Prezzo listino</th><th>Sconto 1</th><th>Sconto 2</th><th>Prezzo netto</th></tr></thead>
      <tbody>${data.lines.map(l=>`
        <tr>
          <td>${l.row_number}</td>
          <td>${l.supplier_code || '—'}</td>
          <td>${l.original_description}</td>
          <td>${l.product_name || l.product_id || '—'}</td>
          <td><span class="match-pill ${l.match_method==='NUOVO_PRODOTTO'?'new':''}">${l.match_method}</span></td>
          <td>${l.unit || '—'}</td>
          <td>${fmtEuro(l.list_price !== null ? l.list_price : l.offered_price)}</td>
          <td>${Number(l.discount_1 || 0).toFixed(2)}%</td>
          <td>${Number(l.discount_2 || 0).toFixed(2)}%</td>
          <td><strong>${fmtEuro(l.net_price !== null ? l.net_price : l.offered_price)}</strong></td>
        </tr>`).join('')}
      </tbody>
    </table>`;

  const panel = document.getElementById('priceListDetailPanel');
  panel.classList.remove('hidden');
  panel.scrollIntoView({behavior:'smooth',block:'start'});
};

document.getElementById('platformCreateCompanyBtn')?.addEventListener('click',createPlatformCompany);
document.getElementById('platformRefreshCompaniesBtn')?.addEventListener('click',loadPlatformCompanies);
document.getElementById('platformLogoutBtn')?.addEventListener('click',logout);

document.getElementById('priceListUploadBtn').addEventListener('click', uploadPriceList);
document.getElementById('refreshPriceLists').addEventListener('click', loadPriceLists);
