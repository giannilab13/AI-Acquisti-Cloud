(function(){
  function esc(value){
    return String(value == null ? '' : value)
      .replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')
      .replaceAll('"','&quot;').replaceAll("'",'&#039;');
  }

  function anomalyValues(a){
    if(a.type==='PRICE'){
      const unitDiff=Number(a.difference_unit||0);
      const totalDiff=Number(a.difference_value||0);
      return 'Fattura ' + fmtEuro(a.invoice_value) +
        ' · Listino ' + fmtEuro(a.reference_value) +
        ' · Diff. unit.: ' + (unitDiff>0?'+':'') + fmtEuro(unitDiff) +
        ' · Impatto totale: ' + (totalDiff>0?'+':'') + fmtEuro(totalDiff);
    }
    if(a.type==='DDT_QUANTITY'){
      return 'Fattura ' + Number(a.invoice_value||0) + ' · DDT ' + Number(a.reference_value||0) + ' · Differenza ' + Number(a.difference_value||0);
    }
    return '';
  }

  function anomalyTypeLabel(type){
    if(type==='PRICE') return 'Prezzo / listino';
    if(type==='DDT_QUANTITY') return 'DDT / quantità';
    if(type==='DDT_MISSING') return 'DDT mancante';
    return 'Anomalia';
  }

  function anomalyResolutionLabel(value){
    if(value==='NUOVO_PREZZO') return 'Nuovo prezzo accettato · riferimento aggiornato';
    if(value==='ERRORE_FORNITORE') return 'Prezzo errato · riferimento invariato';
    if(value==='VERIFICATA') return 'Verificata';
    return value||'';
  }

  function formatAuditDate(value){
    if(!value) return '';
    const d=new Date(value);
    return Number.isNaN(d.getTime()) ? value : d.toLocaleString('it-IT');
  }

  function renderWorkflow(data){
    const d=data.document||{};
    const anomalies=data.anomalies||[];
    const open=anomalies.filter(a=>!a.verified);
    const verified=anomalies.filter(a=>a.verified);
    const detail=document.getElementById('docDetail');
    if(!detail) return;

    detail.querySelector('.anomaly-workflow-c3')?.remove();

    if(anomalies.length){
      const section=document.createElement('section');
      section.className='anomaly-workflow-c3';
      const heading=document.createElement('div');
      heading.className='anomaly-workflow-heading-c3';
      heading.innerHTML='<div><span class="invoice-dossier-kicker">Verifica anomalie</span><h3>' +
        (open.length ? open.length + ' da verificare' : 'Tutte le anomalie sono verificate') +
        '</h3></div><div class="anomaly-workflow-summary-c3"><span class="anomaly-state-c3 open">' + open.length +
        ' aperte</span><span class="anomaly-state-c3 verified">' + verified.length + ' verificate</span></div>';
      section.appendChild(heading);

      const list=document.createElement('div');
      list.className='anomaly-workflow-list-c3';
      anomalies.forEach(a=>{
        const row=document.createElement('div');
        row.className='anomaly-workflow-row-c3 ' + (a.verified?'is-verified':'is-open');
        const copy=document.createElement('div');
        copy.className='anomaly-workflow-copy-c3';
        const v=a.verification||{};
        let audit='';
        if(a.verified){
          audit='<div class="anomaly-audit-c3"><strong>Verificata</strong><span>' +
            esc(formatAuditDate(v.verified_at)) + ' · ' + esc(v.verified_by_name||v.verified_by_email||'Utente') + '</span>' +
            (v.resolution?'<em>'+esc(anomalyResolutionLabel(v.resolution))+'</em>':'') +
            (v.note?'<small>Nota: '+esc(v.note)+'</small>':'') + '</div>';
        }
        const values=anomalyValues(a);
        copy.innerHTML='<div class="anomaly-workflow-state-c3"><span class="anomaly-state-c3 ' +
          (a.verified?'verified':'open') + '">' + (a.verified?'Verificata':'Da verificare') +
          '</span><small>' + anomalyTypeLabel(a.type) + '</small></div><strong>' + esc(a.summary||'Anomalia') +
          '</strong>' + (values?'<span class="anomaly-values-c3">'+esc(values)+'</span>':'') + audit;
        row.appendChild(copy);
        if(!a.verified){
          const actions=document.createElement('div');
          actions.className='anomaly-actions-c32';
          if(a.type==='PRICE'){
            const errorBtn=document.createElement('button');
            errorBtn.type='button';
            errorBtn.className='anomaly-verify-btn-c3 secondary anomaly-price-error-c32';
            errorBtn.textContent='Prezzo errato';
            errorBtn.title='Il prezzo in fattura è errato: il riferimento attuale non cambia.';
            errorBtn.addEventListener('click',()=>verifyAnomalyC3(d.id,a.key,'ERRORE_FORNITORE'));

            const acceptBtn=document.createElement('button');
            acceptBtn.type='button';
            acceptBtn.className='anomaly-verify-btn-c3 anomaly-price-accept-c32';
            acceptBtn.textContent='Accetta nuovo prezzo';
            acceptBtn.title='Il prezzo diventa il nuovo riferimento per le fatture successive.';
            acceptBtn.addEventListener('click',()=>verifyAnomalyC3(d.id,a.key,'NUOVO_PREZZO'));

            actions.appendChild(errorBtn);
            actions.appendChild(acceptBtn);
          }else{
            const btn=document.createElement('button');
            btn.type='button';
            btn.className='anomaly-verify-btn-c3';
            btn.textContent='Segna come verificata';
            btn.addEventListener('click',()=>verifyAnomalyC3(d.id,a.key,'VERIFICATA'));
            actions.appendChild(btn);
          }
          row.appendChild(actions);
        }
        list.appendChild(row);
      });
      section.appendChild(list);
      const dossier=detail.querySelector('.invoice-dossier');
      if(dossier) dossier.insertAdjacentElement('afterend',section);
      else detail.prepend(section);
    }

    const control=data.purchase_control||{};
    const overall=detail.querySelector('.invoice-overall-status');
    if(overall && control.status==='VERIFICATE'){
      overall.className='invoice-overall-status verified';
      overall.innerHTML='<strong>✓</strong><span>'+esc(control.label||'Controllo completato – anomalie verificate')+'</span>';
    }

    const strip=detail.querySelector('.document-status-strip.anomaly');
    if(strip && d.status==='CON_ANOMALIE'){
      strip.innerHTML='<strong>'+(open.length?'Da verificare':'Anomalie verificate')+'</strong><span>' +
        (open.length ? 'Verifica le anomalie riportate qui sotto.' : 'Tutte le anomalie sono state verificate e restano nello storico.') + '</span>';
      if(!open.length){ strip.classList.remove('anomaly'); strip.classList.add('closed'); }
    }

    const anomalyMap=new Map(anomalies.map(a=>[a.key,a]));
    const tables=detail.querySelectorAll('table');
    const invoiceTable=tables.length ? tables[tables.length-1] : null;
    if(invoiceTable){
      const rows=invoiceTable.querySelectorAll('tbody tr');
      (data.lines||[]).forEach((line,i)=>{
        if(line.price_check_status!=='DIFFERENZA' || !rows[i]) return;
        const a=anomalyMap.get('PRICE:'+line.id);
        if(a && a.verified){
          rows[i].classList.remove('invoice-line-anomaly');
          rows[i].classList.add('invoice-line-verified');
          const pill=rows[i].querySelector('.check-pill.bad');
          if(pill){ pill.className='check-pill verified'; pill.textContent='VERIFICATA'; }
          rows[i].querySelector('.variance-cell')?.classList.add('verified');
        }
      });
    }

    const ddtTable=detail.querySelector('.ddt-check-table');
    if(ddtTable){
      const rows=ddtTable.querySelectorAll('tbody tr');
      ((data.ddt_reconciliation||{}).checks||[]).forEach((check,i)=>{
        if(check.status==='OK' || !rows[i]) return;
        const a=anomalyMap.get('DDT_QTY:'+check.product_id);
        if(a && a.verified){
          rows[i].classList.remove('invoice-line-anomaly');
          rows[i].classList.add('invoice-line-verified');
          const pill=rows[i].querySelector('.check-pill.bad');
          if(pill){ pill.className='check-pill verified'; pill.textContent='VERIFICATA'; }
        }
      });
    }

    if(d.status==='CON_ANOMALIE' && (d.workflow_status||'DA_GESTIRE')==='DA_GESTIRE'){
      const finalAction=detail.querySelector('.document-final-action');
      if(finalAction){
        finalAction.innerHTML='<div class="document-final-copy"><strong>'+open.length+' anomalie da verificare</strong>' +
          '<span>Usa “Segna come verificata” per ogni anomalia. La fattura si chiuderà automaticamente quando saranno tutte verificate.</span></div>' +
          '<button class="confirm-document-btn document-closed-btn" disabled>Verifica anomalie</button>';
      }
    }
  }

  async function enhanceDocument(id){
    try{
      const data=await getJSON('/api/documents/'+id);
      renderWorkflow(data);
    }catch(e){
      console.error('Anomaly workflow UI:',e);
    }
  }

  window.verifyAnomalyC3=async function(documentId,anomalyKey,resolution='VERIFICATA'){
    let note='';

    if(resolution==='NUOVO_PREZZO'){
      const ok=window.confirm(
        'Confermi che il prezzo della fattura è corretto e deve diventare il nuovo prezzo di riferimento? Il prezzo precedente resterà nello storico.'
      );
      if(!ok) return;
      const entered=window.prompt(
        'Nota facoltativa (es. “Nuovo prezzo concordato con il fornitore”).',
        ''
      );
      if(entered===null) return;
      note=entered;
    }else if(resolution==='ERRORE_FORNITORE'){
      const entered=window.prompt(
        'Il prezzo viene segnato come errato e il riferimento attuale NON cambia. Puoi aggiungere una nota facoltativa.',
        ''
      );
      if(entered===null) return;
      note=entered;
    }else{
      const entered=window.prompt(
        'Segna questa anomalia come verificata. Puoi aggiungere una nota facoltativa.',
        ''
      );
      if(entered===null) return;
      note=entered;
    }

    try{
      const result=await getJSON('/api/documents/'+documentId+'/verify-anomaly',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          anomaly_key:anomalyKey,
          note:note,
          resolution:resolution
        })
      });
      await loadDocuments();
      await loadDashboard();
      await window.showDocument(documentId);

      if(result.all_verified){
        const extra=result.price_reference_updated
          ? ' Il nuovo prezzo è stato memorizzato come riferimento e il precedente resta nello storico.'
          : '';
        alert('Tutte le anomalie sono state verificate. La fattura è stata chiusa e resta disponibile nello storico.'+extra);
      }else if(result.price_reference_updated){
        alert('Nuovo prezzo salvato come riferimento. Il prezzo precedente resta nello Storico prezzi.');
      }
    }catch(e){
      alert(e.message||e);
    }
  };

  const baseShowDocument=window.showDocument;
  if(typeof baseShowDocument==='function'){
    window.showDocument=async function(id){
      await baseShowDocument(id);
      await enhanceDocument(id);
    };
  }

  function decorateDashboard(){
    const box=document.getElementById('dashboardAnomalies');
    if(!box) return;
    box.querySelectorAll('.anomaly-row-click').forEach(row=>{
      const cells=row.querySelectorAll('td');
      if(cells.length<3 || cells[2].querySelector('.anomaly-inline-state-c3')) return;
      const badge=document.createElement('span');
      badge.className='anomaly-inline-state-c3';
      badge.textContent='Da verificare';
      cells[2].prepend(badge);
    });
  }

  const dashboardBox=document.getElementById('dashboardAnomalies');
  if(dashboardBox){
    new MutationObserver(decorateDashboard).observe(dashboardBox,{childList:true,subtree:true});
    decorateDashboard();
  }
})();
