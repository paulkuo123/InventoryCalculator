(() => {
  const $ = id => document.getElementById(id);
  const state = { items: [], loading: false, selectedIds: new Set() };
  const message = (text, type = '') => { $('message').textContent = text || ''; $('message').className = `message ${type}`.trim(); };
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
  const fmt = value => Number(value || 0).toLocaleString('zh-TW');

  function renderSummary(data) {
    const counts = data.counts || {};
    const tiles = [
      ['全部有 1688 URL 型號', data.urlModels],
      ['待人工核准', data.pending], ['已核准', data.approved],
      ['綠色：唯一精確', data.green], ['黃色：人工比較', data.yellow],
      ['紅色：阻擋', data.red], ['需重新掃描', data.stale], ['失效／無匹配', (counts.error || 0) + (counts.no_match || 0) + (counts.discontinued || 0)]
    ];
    $('summary').innerHTML = tiles.map(([label, value]) => `<div class="metric"><strong>${fmt(value)}</strong><span>${esc(label)}</span></div>`).join('');
  }

  function candidateCard(item, candidate, rank) {
    const suggested = String(item.suggested_sku_id || '') === String(candidate.sku_id || '');
    const selected = item.review_tier === 'green' && suggested;
    return `<div class="candidate ${selected ? 'selected' : ''} ${suggested ? 'suggested' : ''}" data-sku="${esc(candidate.sku_id)}" data-item="${esc(item.id)}">
      <span class="rank">${rank}</span>${candidate.image_url ? `<img src="${esc(candidate.image_url)}" loading="lazy" alt="">` : ''}
      <strong>${esc(candidate.sku_name || '未命名規格')}</strong><small>SKU ID：${esc(candidate.sku_id)}</small><small>${esc(candidate.spec_text)}</small><small>價格：${esc(candidate.price ?? '—')}　庫存：${esc(candidate.stock ?? '—')}</small><small>規則分數：${esc(candidate.deterministic_score)}</small>${suggested ? '<small class="suggested-label">系統建議</small>' : ''}</div>`;
  }

  function card(item) {
    const candidates = item.candidates || [];
    const evidence = item.evidence || {};
    const ai = evidence.ai || {};
    const sourceImage = item.modelImageUrl || item.productImageUrl || '';
    const needsRescan = ['stale', 'error', 'waiting_for_login', 'missing', 'legacy_pending_id', 'empty'].includes(String(item.status || ''));
    const manualHint = item.status === 'stale'
      ? '這筆目前只有舊快照候選；請先重新掃描 1688，確認最新 SKU ID 與完整規格後再核准。'
      : item.existing_sku_id
      ? '人工從完整 SKU 清單選擇的規格優先級最高；核准後會直接更新目前 mapping。'
      : (candidates.length ? '候選僅供參考；為避免誤配，也可以從完整 SKU 清單重新選擇。' : '先重新掃描此 1688 商品；若規則仍無法配對，可從完整 SKU 清單手動指定。');
    const manualTools = `<div class="manual-tools">
      <div class="manual-hint">${manualHint}</div>
      <div class="actions">${needsRescan ? '<button class="primary rescan" data-action="rescan">重新掃描此商品</button>' : ''}<button data-action="load_catalog">從完整 SKU 清單選擇</button></div>
      <div class="catalog-picker" hidden><select class="catalog-select"><option value="">尚未載入，請先按上方按鈕</option></select><button data-action="approve_catalog" class="approve" disabled>核准手動選擇</button></div>
    </div>`;
    const existing = item.existing_sku_id
      ? `<div class="existing-mapping"><strong>現有 mapping</strong><br>SKU ID：${esc(item.existing_sku_id)}<br>${esc(item.existing_sku_name || item.existing_second_name || '未命名規格')}<br><span>${esc(item.mapping_status || 'approved')}</span></div>`
      : (item.existing_sku_name ? `<div class="existing-mapping legacy"><strong>舊名稱紀錄（尚非正式 mapping）</strong><br>${esc(item.existing_sku_name)}</div>` : '<div class="existing-mapping empty-mapping">目前沒有正式 mapping</div>');
    return `<article class="card tier-card-${esc(item.review_tier)}" data-id="${esc(item.id)}" data-tier="${esc(item.review_tier)}">
      <label class="select-row"><input type="checkbox" class="select-item"> 批次處理</label>
      <div class="source">${sourceImage ? `<img src="${esc(sourceImage)}" loading="lazy" alt="">` : '<div class="source-placeholder">無圖片</div>'}
        <div><h2>${esc(item.model_name || '未命名型號')}</h2><p>${esc(item.product_name || '')}</p><p>商品 ID：${esc(item.product_id)}　規格 ID：${esc(item.model_id)}</p><p>型號月銷量：<strong>${fmt(item.monthlySales)}</strong>　商品月銷量：<strong>${fmt(item.productMonthlySales)}</strong></p><span class="badge ${esc(item.status)}">${esc(item.status)}</span><span class="tier-badge tier-${esc(item.review_tier)}">${esc(item.review_tier === 'green' ? '綠色：唯一精確' : item.review_tier === 'yellow' ? '黃色：人工比較' : item.review_tier === 'red' && item.status === 'stale' ? '紅色：需重新掃描' : item.review_tier === 'red' ? '紅色：阻擋' : '已核准')}</span>${item.offer_id ? `<a class="open-1688" href="${esc(item.product_url || `https://detail.1688.com/offer/${item.offer_id}.html`)}" target="_blank" rel="noopener">開啟 1688 ↗</a>` : ''}${existing}</div></div>
      <div><div class="candidates">${candidates.length ? candidates.map((candidate, index) => candidateCard(item, candidate, index + 1)).join('') : '<div class="reason">尚未取得可通過規則的 SKU 候選；不代表 1688 沒有這個 SKU。</div>'}</div>${manualTools}
      <div class="reason"><strong>${esc(item.review_reason || '等待人工確認')}</strong>${(ai.evidence || []).length ? `<br>${esc(ai.evidence.join('；'))}` : ''}${evidence.error ? `<br>${esc(evidence.error)}` : ''}</div>
      <div class="actions"><button class="approve" data-action="approve">核准選取 SKU</button><button data-action="defer">稍後處理</button><button data-action="no_match">標記無匹配</button><button data-action="discontinued">標記停售</button></div></div></article>`;
  }

  async function loadSummary() {
    const response = await fetch('/api/sku-mapping/summary');
    const data = await response.json();
    if (!response.ok || data.status !== 'success') throw new Error(data.message || '摘要載入失敗');
    renderSummary(data);
  }

  async function loadQueue() {
    const params = new URLSearchParams({ status: $('status').value, tier: $('tier').value, query: $('query').value, pageSize: '200' });
    const response = await fetch(`/api/sku-mapping/queue?${params}`);
    const data = await response.json();
    if (!response.ok || data.status !== 'success') throw new Error(data.message || '待審核清單載入失敗');
    state.items = data.items || [];
    $('queue').innerHTML = state.items.map(card).join('');
    document.querySelectorAll('.queue .card').forEach(cardElement => {
      const checkbox = cardElement.querySelector('.select-item');
      if (checkbox) checkbox.checked = state.selectedIds.has(String(cardElement.dataset.id));
    });
    $('empty').hidden = state.items.length > 0;
  }

  async function reload() {
    try { await Promise.all([loadSummary(), loadQueue()]); message(`已載入 ${state.items.length} 筆 mapping。`, 'success'); }
    catch (error) { message(error.message, 'error'); }
  }

  async function scan(scope, target = null) {
    if (state.loading) return;
    state.loading = true;
    $('scanAll').disabled = true;
    try {
      const body = { scope, force: target ? true : $('forceScan').checked, useAi: $('useAi').checked };
      if (target) { body.productId = target.product_id; body.modelId = target.model_id; body.offerId = target.offer_id; }
      const response = await fetch('/api/sku-mapping/scans', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
      const data = await response.json();
      if (!response.ok || data.status !== 'success') throw new Error(data.message || '掃描啟動失敗');
      message(target ? `已啟動單筆重掃 ${data.jobId}，完成後會更新這個型號。` : `已啟動 ${data.jobId}，掃描會在背景執行。`, 'success');
      pollJob(data.jobId);
    } catch (error) { message(error.message, 'error'); }
    finally { state.loading = false; $('scanAll').disabled = false; }
  }

  async function pollJob(jobId) {
    const response = await fetch(`/api/sku-mapping/jobs/${encodeURIComponent(jobId)}`);
    const data = await response.json();
    $('jobStatus').textContent = `${data.status || ''} ${data.completed || 0}/${data.total || 0} ${data.message || ''}`;
    if (data.status === 'waiting_for_login') { message('1688 需要登入或人工驗證；完成後請重新掃描。', 'error'); return; }
    if (['completed','error'].includes(data.status)) { await reload(); return; }
    setTimeout(() => pollJob(jobId), 2500);
  }

  async function loadCatalog(cardElement) {
    const item = state.items.find(row => String(row.id) === String(cardElement.dataset.id));
    if (!item) return;
    const picker = cardElement.querySelector('.catalog-picker');
    const select = cardElement.querySelector('.catalog-select');
    const button = cardElement.querySelector('[data-action="load_catalog"]');
    if (!picker || !select) return;
    if (button) button.disabled = true;
    select.disabled = true;
    select.innerHTML = '<option value="">讀取完整 SKU 清單中…</option>';
    try {
      const params = new URLSearchParams({ productId: item.product_id, modelId: item.model_id });
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      let response;
      try {
        response = await fetch(`/api/sku-mapping/catalog?${params}`, { signal: controller.signal });
      } finally {
        clearTimeout(timeout);
      }
      const data = await response.json();
      if (!response.ok || data.status !== 'success') throw new Error(data.message || '完整 SKU 清單載入失敗');
      const skus = data.skus || [];
      if (!skus.length) {
        throw new Error(data.catalogStatus === 'not_scanned' ? '目前沒有可用快照，請先按「重新掃描此商品」。' : '1688 快照中沒有 SKU，請確認登入、驗證或商品狀態。');
      }
      select.innerHTML = `<option value="">請選擇要核准的 SKU（共 ${skus.length} 個）</option>` + skus.map(sku => `<option value="${esc(sku.sku_id)}">${esc(sku.spec_text || sku.sku_name || sku.sku_id)}｜SKU ID：${esc(sku.sku_id)}</option>`).join('');
      select.disabled = false;
      picker.hidden = false;
      message('已載入完整 SKU 清單。請確認完整規格後再核准，系統只接受此快照中存在的 SKU ID。', 'success');
    } catch (error) {
      picker.hidden = false;
      const detail = error.name === 'AbortError' ? '完整 SKU 清單載入逾時，請重新掃描此商品。' : error.message;
      select.innerHTML = `<option value="">${esc(detail)}</option>`;
      select.disabled = true;
      message(detail, 'error');
    } finally {
      if (button) button.disabled = false;
    }
  }

  function selectedSku(cardElement) {
    const selected = cardElement.querySelector('.candidate.selected');
    if (selected) return selected;
    return cardElement.querySelector('.candidate');
  }

  async function decide(cardElement, action, explicitSkuId = '', triggerButton = null) {
    const item = state.items.find(row => String(row.id) === String(cardElement.dataset.id));
    if (!item) return;
    const candidate = selectedSku(cardElement);
    const skuId = explicitSkuId || candidate?.dataset.sku || '';
    if (['approve','replace'].includes(action) && !skuId) { message('請先選擇一個 SKU 候選，或從完整 SKU 清單手動指定。', 'error'); return; }
    if (['replace','discontinued','no_match'].includes(action) && !window.confirm(`確定要${action === 'replace' ? '取代既有 mapping' : action === 'discontinued' ? '標記停售' : '標記無匹配'}嗎？`)) return;
    if (triggerButton) triggerButton.disabled = true;
    message(action === 'approve' ? '正在儲存 SKU mapping…' : '正在更新 mapping 狀態…');
    try {
      const response = await fetch('/api/sku-mapping/decisions', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ items: [{ productId: item.product_id, modelId: item.model_id, action, skuId, version: item.version }] }) });
      const data = await response.json();
      if (response.status === 409) { await reload(); message('這筆資料剛重新掃描，頁面版本已更新；請使用重新載入後的候選再操作。', 'error'); return; }
      if (!response.ok || !['success'].includes(data.status)) throw new Error(data.message || '儲存 mapping 失敗');
      state.selectedIds.delete(String(cardElement.dataset.id));
      await reload();
    } catch (error) { message(error.message, 'error'); }
    finally { if (triggerButton && document.body.contains(triggerButton)) triggerButton.disabled = false; }
  }

  async function batchApprove() {
    const cards = [...document.querySelectorAll('.queue .card')].filter(card => card.querySelector('.select-item')?.checked);
    if (!cards.length) { message('請先勾選要批次核准的型號。', 'error'); return; }
    const rows = cards.map(card => state.items.find(item => String(item.id) === String(card.dataset.id))).filter(Boolean);
    if (rows.length !== cards.length) { message('清單資料已更新，請先重新載入再批次核准。', 'error'); return; }
    const selections = cards.map(card => {
      const row = state.items.find(item => String(item.id) === String(card.dataset.id));
      const candidate = selectedSku(card);
      const manualSku = card.querySelector('.catalog-select')?.value || '';
      return { row, candidate, manualSku, skuId: manualSku || candidate?.dataset.sku || '' };
    });
    if (selections.some(selection => !selection.skuId)) {
      message('勾選的型號中有項目尚未選擇 SKU；請先選候選，或從完整 SKU 清單手動指定。', 'error');
      return;
    }
    const items = selections.map(selection => ({
      productId: selection.row.product_id,
      modelId: selection.row.model_id,
      action: 'approve',
      skuId: selection.skuId,
      version: selection.row.version,
    }));
    if (!items.length) { message('請先勾選有候選 SKU 的型號。', 'error'); return; }
    const manualCount = selections.filter(selection => Boolean(selection.manualSku)).length;
    const defaultCount = selections.filter(selection => !selection.manualSku && !selection.candidate?.classList.contains('selected')).length;
    const warning = [
      manualCount ? `${manualCount} 筆使用完整 SKU 清單中的手動選擇。` : '',
      defaultCount ? `${defaultCount} 筆未手動指定，將使用候選第 1 號。` : '',
    ].filter(Boolean).join('\n') || '每筆都已手動選擇候選。';
    if (!window.confirm(`確定核准 ${items.length} 筆 SKU mapping？\n${warning}`)) return;
    try {
      const response = await fetch('/api/sku-mapping/decisions', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ items, batch: true }) });
      const data = await response.json();
      if (response.status === 409) { message('部分資料剛重新掃描，頁面版本已更新，正在重新載入。', 'error'); await reload(); return; }
      if (!response.ok || data.status !== 'success') throw new Error(data.message || '批次核准失敗');
      cards.forEach(card => state.selectedIds.delete(String(card.dataset.id)));
      await reload();
    } catch (error) { message(error.message, 'error'); }
  }

  $('queue').addEventListener('click', event => {
    const candidate = event.target.closest('.candidate');
    if (candidate) { const parent = candidate.closest('.card'); parent.querySelectorAll('.candidate').forEach(node => node.classList.remove('selected')); candidate.classList.add('selected'); return; }
    const button = event.target.closest('button[data-action]');
    if (!button) return;
    const cardElement = button.closest('.card');
    if (button.dataset.action === 'load_catalog') return loadCatalog(cardElement);
    if (button.dataset.action === 'rescan') {
      const item = state.items.find(row => String(row.id) === String(cardElement?.dataset.id));
      if (item) return scan('all', item);
    }
    if (button.dataset.action === 'approve_catalog') {
      const selected = cardElement?.querySelector('.catalog-select')?.value || '';
      if (!selected) { message('請先從完整 SKU 清單選擇一個規格。', 'error'); return; }
      return decide(cardElement, 'approve', selected, button);
    }
    decide(cardElement, button.dataset.action, '', button);
  });
  $('queue').addEventListener('change', event => {
    const catalogSelect = event.target.closest('.catalog-select');
    if (catalogSelect) {
      const cardElement = catalogSelect.closest('.card');
      const approveButton = cardElement?.querySelector('[data-action="approve_catalog"]');
      if (approveButton) approveButton.disabled = !catalogSelect.value;
      return;
    }
    const checkbox = event.target.closest('.select-item');
    if (!checkbox) return;
    const cardElement = checkbox.closest('.card');
    if (!cardElement) return;
    if (checkbox.checked) state.selectedIds.add(String(cardElement.dataset.id));
    else state.selectedIds.delete(String(cardElement.dataset.id));
  });
  $('scanAll').addEventListener('click', () => scan('all'));
  $('selectGreen').addEventListener('click', () => {
    const cards = [...document.querySelectorAll('.queue .card')];
    let selected = 0;
    cards.forEach(card => {
      const checkbox = card.querySelector('.select-item');
      const safe = card.dataset.tier === 'green' && card.querySelector('.candidate');
      if (checkbox) checkbox.checked = Boolean(safe);
      if (safe) state.selectedIds.add(String(card.dataset.id));
      else state.selectedIds.delete(String(card.dataset.id));
      if (safe) selected += 1;
    });
    message(selected ? `已選取本頁 ${selected} 筆綠色唯一精確項目，請再逐項確認後批次核准。` : '本頁沒有可安全批次核准的綠色項目。', selected ? 'success' : '');
  });
  $('batchApprove').addEventListener('click', batchApprove);
  $('reload').addEventListener('click', reload);
  $('status').addEventListener('change', reload);
  $('tier').addEventListener('change', reload);
  let queryTimer;
  $('query').addEventListener('input', () => { clearTimeout(queryTimer); queryTimer = setTimeout(reload, 350); });
  document.addEventListener('keydown', event => {
    const active = document.querySelector('.card:hover');
    if (!active || ['INPUT','SELECT','TEXTAREA'].includes(document.activeElement.tagName)) return;
    if (/^[1-5]$/.test(event.key)) active.querySelectorAll('.candidate')[Number(event.key) - 1]?.click();
    if (event.key === 'Enter') active.querySelector('[data-action="approve"]')?.click();
  });
  reload();
})();
