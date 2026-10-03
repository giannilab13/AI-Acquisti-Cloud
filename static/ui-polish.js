(function(){
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

  try{
    const originalUpdateCompanyUI=updateCompanyUI;
    updateCompanyUI=function(){
      originalUpdateCompanyUI();
      applyRoleUI();
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
    applyRoleUI();
    polishProductPrices();
    polishSupplierDetail();
    polishPriceLists();
    polishPriceListDetail();
  },250);
})();
