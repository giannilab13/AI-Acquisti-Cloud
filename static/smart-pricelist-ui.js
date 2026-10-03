(function(){
  let installed=false;

  async function smartLoadPriceListSuppliers(){
    const suppliers=await getJSON('/api/suppliers-simple');
    const select=document.getElementById('priceListSupplier');
    if(!select)return;

    const current=select.value;
    select.innerHTML=
      '<option value="">Rileva automaticamente dal file...</option>' +
      suppliers.map(s=>`<option value="${s.id}">${s.name}</option>`).join('');

    if(suppliers.some(s=>String(s.id)===String(current))){
      select.value=current;
    }
  }

  async function smartUploadPriceList(){
    const select=document.getElementById('priceListSupplier');
    const supplierId=select?.value || '';
    const input=document.getElementById('priceListFile');
    const file=input?.files?.[0];
    const result=document.getElementById('priceListUploadResult');
    const btn=document.getElementById('priceListUploadBtn');

    if(!file){
      result.innerHTML='<div class="result-error">Seleziona un file CSV o XLSX.</div>';
      return;
    }

    const fd=new FormData();
    fd.append('file',file);

    btn.disabled=true;
    btn.textContent='Analisi e importazione...';
    result.innerHTML='<div class="multi-import-progress">Analizzo il listino e provo a riconoscere automaticamente il fornitore…</div>';

    try{
      const suffix=supplierId ? `?supplier_id=${encodeURIComponent(supplierId)}` : '';
      const data=await getJSON(`/api/price-lists/smart-upload${suffix}`,{
        method:'POST',
        body:fd
      });

      if(data.needs_supplier){
        result.innerHTML=`<div class="result-error">
          <strong>Fornitore non identificato nel file.</strong><br>
          Seleziona il fornitore dall'elenco qui sopra oppure crealo con <strong>+ Nuovo fornitore</strong>, poi premi di nuovo <strong>Importa listino</strong>.
        </div>`;
        select?.focus();
        return;
      }

      const resolution=data.supplier_resolution;
      const supplierLine=resolution==='detected_created'
        ? `Fornitore creato automaticamente: <strong>${data.supplier}</strong>`
        : resolution==='detected_existing'
          ? `Fornitore riconosciuto automaticamente: <strong>${data.supplier}</strong>`
          : `Fornitore: <strong>${data.supplier}</strong>`;

      result.innerHTML=`<div class="result-ok">
        <strong>Listino importato correttamente.</strong><br>
        ${supplierLine}<br>
        Righe valide: ${data.rows_count}<br>
        Prodotti già riconosciuti: ${data.matched_products}<br>
        Nuovi prodotti creati: ${data.created_products}<br>
        Righe ignorate: ${data.skipped_rows}
      </div>`;

      input.value='';
      if(select) select.value='';

      await smartLoadPriceListSuppliers();
      await loadPriceLists();
      if(typeof loadProductsPage==='function') await loadProductsPage();
      if(typeof loadSuppliersPage==='function') await loadSuppliersPage();
    }catch(e){
      result.innerHTML=`<div class="result-error"><strong>Importazione non riuscita.</strong><br>${e.message||e}</div>`;
    }finally{
      btn.disabled=false;
      btn.textContent='Importa listino';
    }
  }

  function install(){
    if(installed)return;
    const btn=document.getElementById('priceListUploadBtn');
    const select=document.getElementById('priceListSupplier');
    if(!btn || !select)return;
    installed=true;

    const description=document.querySelector('#priceListsView .upload-panel .panel-title .sub');
    if(description){
      description.textContent='Carica il listino: se il file contiene fornitore o P.IVA, AI Acquisti crea o riconosce automaticamente l’anagrafica e importa prodotti, prezzi e sconti.';
    }

    try{
      loadPriceListSuppliers=smartLoadPriceListSuppliers;
    }catch{}

    smartLoadPriceListSuppliers().catch(console.error);

    // L'app precedente aveva già un listener sul pulsante. Il listener in cattura
    // intercetta il click prima di quello vecchio senza modificare app.js.
    btn.addEventListener('click',event=>{
      event.preventDefault();
      event.stopImmediatePropagation();
      smartUploadPriceList();
    },true);
  }

  setTimeout(install,100);
})();
