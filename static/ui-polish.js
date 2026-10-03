(function(){
  const UI_VERSION='1.4C2.5';

  function ensureResponsiveStylesheet(){
    if(document.querySelector('link[href="/static/responsive.css"]')) return;
    const link=document.createElement('link');
    link.rel='stylesheet';
    link.href='/static/responsive.css';
    document.head.appendChild(link);
  }

  function ensureSmartPriceListScript(){
    if(document.querySelector('script[src="/static/smart-pricelist-ui.js"]')) return;
    const script=document.createElement('script');
    script.src='/static/smart-pricelist-ui.js';
    script.defer=true;
    document.head.appendChild(script);
  }

  ensureResponsiveStylesheet();
  ensureSmartPriceListScript();

  const statusLabels={
    IMPORTATA:'Importata',
    IMPORTATO:'Importato',
    CON_ANOMALIE:'Con anomalie',
    DA_VERIFICARE:'Da verificare',
    DUPLICATA:'Duplicata',
    ERRORE:'Errore',
    IMPORTATO_SENZA_MOVIMENTI:'Importata senza movimenti'
  };

  const matchMethodLabels={
    CODICE_FORNITORE:'Codice fornitore',
    NUOVO_PRODOTTO:'Nuovo prodotto',
    DESCRIZIONE:'Descrizione',
    DESCRIZIONE_ESATTA:'Descrizione esatta',
    MANUALE:'Manuale'
  };

  function humanStatus(value){
    const key=String(value||'').trim();
    return statusLabels[key] || key || '—';
  }

  function humanMatchMethod(value){
    const key=String(value||'').trim();
    return matchMethodLabels[key] || key.replaceAll('_',' ').toLowerCase().replace(/^./,c=>c.toUpperCase()) || '—';
  }

  function formatItalianDateTime(value){
    const raw=String(value||'').trim();
    if(!raw)return '—';
    if(!/^\d{4}-\d{2}-\d{2}/.test(raw))return raw;

    let normalized=raw.replace(' ','T');
    normalized=normalized.replace(/(\.\d{3})\d+/, '$1');
    normalized=normalized.replace(/([+-]\d{2})$/, '$1:00');
    const date=new Date(normalized);
    if(Number.isNaN(date.getTime()))return raw;

    return date.toLocaleString('it-IT',{
      day:'2-digit',month:'2-digit',year:'numeric',
      hour:'2-digit',minute:'2-digit'
    });
  }

  function formatEuroHalfUp(value){
    const n=Number(value);
    if(!Number.isFinite(n))return '—';
    const rounded=Math.round((n+1e-9)*100)/100;
    return new Intl.NumberFormat('it-IT',{
      style:'currency',currency:'EUR',minimumFractionDigits:2,maximumFractionDigits:2
    }).format(rounded);
  }

  function applyVersionLabels(){
    document.title=`AI Acquisti - Cloud ${UI_VERSION}`;
    document.querySelectorAll('.version').forEach(el=>{
      el.textContent=`Cloud ${UI_VERSION}`;
    });
    document.querySelectorAll('.status').forEach(el=>{
      const text=el.textContent.trim();
      if(/^Cloud\s+1\.4C/i.test(text)) el.textContent=`Cloud ${UI_VERSION}`;
    });
  }

  function applyRoleUI(){
    const usersNav=document.querySelector('.nav[data-view="usersView"]');
    if(!usersNav)return;

    let role='';
    try{ role=String(activeCompany()?.role||'').toUpperCase(); }catch{}
    const canManageUsers=role==='ADMIN';
    usersNav.hidden=!canManageUsers;

    if(!canManageUsers && document.getElementById('usersView')?.classList.contains('active-view')){
      document.querySelector('.nav[data-view="dashboardView"]')?.click();
    }
  }

  function polishProductPrices(){
    const rows=document.querySelectorAll('#productsTable tbody tr');
    if(!rows.length)return;

    rows.forEach(row=>{
      const id=row.querySelector('.product-name-cell small')?.textContent?.trim();
      if(!id)return;
      let product=null;
      try{ product=productsOverviewCache.find(p=>String(p.id)===id); }catch{}
      if(product && product.last_price!==null && product.last_price!==undefined && row.cells[3]){
        row.cells[3].textContent=formatEuroHalfUp(product.last_price);
      }
    });
  }

  function polishSupplierDetail(){
    document.querySelectorAll('#supplierDetail tbody tr').forEach(row=>{
      if(row.cells.length===5){
        const candidate=row.cells[4]?.textContent?.trim();
        if(statusLabels[candidate]) row.cells[4].textContent=humanStatus(candidate);
      }
    });
  }

  function polishPriceLists(){
    document.querySelectorAll('#priceListsTable tbody tr').forEach(row=>{
      if(row.cells[4]) row.cells[4].textContent=formatItalianDateTime(row.cells[4].textContent);
      if(row.cells[5]) row.cells[5].textContent=humanStatus(row.cells[5].textContent);
    });
  }

  function polishPriceListDetail(){
    document.querySelectorAll('#priceListDetail .meta-box').forEach(box=>{
      const label=box.querySelector('span')?.textContent?.trim();
      const value=box.querySelector('strong');
      if(label==='Stato' && value) value.textContent=humanStatus(value.textContent);
    });
    document.querySelectorAll('#priceListDetail .match-pill').forEach(el=>{
      el.textContent=humanMatchMethod(el.textContent);
    });
  }

  function ensurePasswordModal(){
    if(document.getElementById('changePasswordModal'))return;

    const modal=document.createElement('div');
    modal.id='changePasswordModal';
    modal.className='company-modal hidden';
    modal.innerHTML=`
      <div class="company-modal-card">
        <div>
          <h2>Cambia password</h2>
          <p>Inserisci la password attuale e scegli la nuova password.</p>
        </div>

        <label>
          Password attuale
          <input id="currentPasswordChange" type="password" autocomplete="current-password" placeholder="Password attuale">
        </label>

        <label>
          Nuova password
          <input id="newPasswordChange" type="password" autocomplete="new-password" placeholder="Minimo 8 caratteri">
        </label>

        <label>
          Conferma nuova password
          <input id="confirmPasswordChange" type="password" autocomplete="new-password" placeholder="Ripeti la nuova password">
        </label>

        <div id="changePasswordResult"></div>

        <div class="company-modal-actions">
          <button id="cancelPasswordChangeBtn" class="secondary" type="button">Annulla</button>
          <button id="savePasswordChangeBtn" type="button">Aggiorna password</button>
        </div>
      </div>`;

    document.body.appendChild(modal);
    document.getElementById('cancelPasswordChangeBtn')?.addEventListener('click',closePasswordModal);
    document.getElementById('savePasswordChangeBtn')?.addEventListener('click',submitPasswordChange);
    document.getElementById('confirmPasswordChange')?.addEventListener('keydown',e=>{
      if(e.key==='Enter') submitPasswordChange();
    });
  }

  function openPasswordModal(){
    ensurePasswordModal();
    document.getElementById('currentPasswordChange').value='';
    document.getElementById('newPasswordChange').value='';
    document.getElementById('confirmPasswordChange').value='';
    document.getElementById('changePasswordResult').innerHTML='';
    document.getElementById('changePasswordModal').classList.remove('hidden');
    setTimeout(()=>document.getElementById('currentPasswordChange')?.focus(),50);
  }

  function closePasswordModal(){
    document.getElementById('changePasswordModal')?.classList.add('hidden');
  }

  async function submitPasswordChange(){
    const currentPassword=document.getElementById('currentPasswordChange').value;
    const newPassword=document.getElementById('newPasswordChange').value;
    const confirmPassword=document.getElementById('confirmPasswordChange').value;
    const result=document.getElementById('changePasswordResult');
    const btn=document.getElementById('savePasswordChangeBtn');

    result.innerHTML='';

    if(!currentPassword){
      result.innerHTML='<div class="result-error">Inserisci la password attuale.</div>';
      return;
    }
    if(newPassword.length<8){
      result.innerHTML='<div class="result-error">La nuova password deve contenere almeno 8 caratteri.</div>';
      return;
    }
    if(newPassword!==confirmPassword){
      result.innerHTML='<div class="result-error">Le due nuove password non coincidono.</div>';
      return;
    }
    if(newPassword===currentPassword){
      result.innerHTML='<div class="result-error">La nuova password deve essere diversa da quella attuale.</div>';
      return;
    }

    btn.disabled=true;
    btn.textContent='Aggiornamento...';

    try{
      await getJSON('/api/auth/change-password',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          current_password:currentPassword,
          new_password:newPassword,
          confirm_password:confirmPassword
        })
      });

      document.getElementById('currentPasswordChange').value='';
      document.getElementById('newPasswordChange').value='';
      document.getElementById('confirmPasswordChange').value='';
      result.innerHTML='<div class="result-success"><strong>Password aggiornata.</strong><br>Le altre sessioni di questo account sono state chiuse.</div>';
    }catch(e){
      result.innerHTML=`<div class="result-error">${e.message||e}</div>`;
    }finally{
      btn.disabled=false;
      btn.textContent='Aggiorna password';
    }
  }

  function ensurePasswordButtons(){
    ensurePasswordModal();

    const sidebarUser=document.querySelector('.sidebar-user');
    const logoutBtn=document.getElementById('logoutBtn');
    if(sidebarUser && logoutBtn && !document.getElementById('changePasswordBtn')){
      const btn=document.createElement('button');
      btn.id='changePasswordBtn';
      btn.type='button';
      btn.textContent='Cambia password';
      btn.addEventListener('click',openPasswordModal);
      sidebarUser.insertBefore(btn,logoutBtn);
    }

    const platformUser=document.querySelector('.platform-admin-user');
    const platformLogout=document.getElementById('platformLogoutBtn');
    if(platformUser && platformLogout && !document.getElementById('platformChangePasswordBtn')){
      const btn=document.createElement('button');
      btn.id='platformChangePasswordBtn';
      btn.type='button';
      btn.className='secondary';
      btn.textContent='Cambia password';
      btn.addEventListener('click',openPasswordModal);
      platformUser.insertBefore(btn,platformLogout);
    }
  }

  try{
    const originalUpdateCompanyUI=updateCompanyUI;
    updateCompanyUI=function(){
      originalUpdateCompanyUI();
      applyRoleUI();
      applyVersionLabels();
    };
  }catch{}

  try{
    const originalRenderProductsTable=renderProductsTable;
    renderProductsTable=function(){
      originalRenderProductsTable();
      polishProductPrices();
    };
  }catch{}

  try{
    const originalLoadPriceLists=loadPriceLists;
    loadPriceLists=async function(){
      const result=await originalLoadPriceLists();
      polishPriceLists();
      return result;
    };
  }catch{}

  if(window.showSupplierDetail){
    const originalShowSupplierDetail=window.showSupplierDetail;
    window.showSupplierDetail=async function(...args){
      const result=await originalShowSupplierDetail(...args);
      polishSupplierDetail();
      return result;
    };
  }

  if(window.showPriceListDetail){
    const originalShowPriceListDetail=window.showPriceListDetail;
    window.showPriceListDetail=async function(...args){
      const result=await originalShowPriceListDetail(...args);
      polishPriceListDetail();
      return result;
    };
  }

  document.getElementById('productsSearch')?.addEventListener('input',()=>setTimeout(polishProductPrices,0));
  document.getElementById('productsCategory')?.addEventListener('change',()=>setTimeout(polishProductPrices,0));
  document.getElementById('refreshPriceLists')?.addEventListener('click',()=>setTimeout(polishPriceLists,50));
  document.getElementById('companySelect')?.addEventListener('change',()=>setTimeout(applyRoleUI,50));

  setTimeout(()=>{
    applyVersionLabels();
    applyRoleUI();
    ensurePasswordButtons();
    polishProductPrices();
    polishSupplierDetail();
    polishPriceLists();
    polishPriceListDetail();
  },250);
})();