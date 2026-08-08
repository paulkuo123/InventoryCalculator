(() => {
  const $ = id => document.getElementById(id);
  const state = { items: [], loading: false, selectedIds: new Set() };
  const message = (text, type = '') => { $('message').textContent = text || ''; $('message').className = `message ${type}`.trim(); };
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
  const fmt = value => Number(value || 0).toLocaleString('zh-TW');

  function renderSummary(data) {
    const counts = data.counts || {};
    const tiles = [
      ['全部型號', data.total], ['已有 1688 URL', data.urlModels], ['需補貨型號', data.restockModels],
      ['阻擋補貨數量', data.blockedRestockQty],
      ['待人工核准', data.pending], ['已核准', data.approved],
      ['綠色：唯一精確', data.green], ['黃色：人工比較', data.yellow],
      ['紅色：阻擋', data.red], ['失效／無匹配', (counts.error || 0) + (counts.no_match || 0) + (counts.discontinued || 0)]
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
    const existing = item.existing_sku_id
      ? `<div class="existing-mapping"><strong>現有 mapping</strong><br>SKU ID：${esc(item.existing_sku_id)}<br>${esc(item.existing_sku_name || item.existing_second_name || '未命名規格')}<br><span>${esc(item.mapping_status || 'approved')}</span></div>`
      : (item.existing_sku_name ? `<div class="existing-mapping legacy"><strong>舊名稱紀錄（尚非正式 mapping）</strong><br>${esc(item.existing_sku_name)}</div>` : '<div class="existing-mapping empty-mapping">目前沒有正式 mapping</div>');
    const replaceButton = item.existing_sku_id ? '<button data-action="replace">以選取候選取代</button>' : '';
    return `<article class="card tier-card-${esc(item.review_tier)}" data-id="${esc(item.id)}" data-tier="${esc(item.review_tier)}">
      <label class="select-row"><input type="checkbox" class="select-item"> 批次處理</label>
      <div class="source">${sourceImage ? `<img src="${esc(sourceImage)}" loading="lazy" alt="">` : '<div class="source-placeholder">無圖片</div>'}
        <div><h2>${esc(item.model_name || '未命名型號')}</h2><p>${esc(item.product_name || '')}</p><p>商品 ID：${esc(item.product_id)}　規格 ID：${esc(item.model_id)}</p><p>需補貨：<strong>${fmt(item.restockQty)}</strong></p><span class="badge ${esc(item.status)}">${esc(item.status)}</span><span class="tier-badge tier-${esc(item.review_tier)}">${esc(item.review_tier === 'green' ? '綠色：唯一精確' : item.review_tier === 'yellow' ? '黃色：人工比較' : item.review_tier === 'red' ? '紅色：阻擋' : '已核准')}</span>${item.offer_id ? `<a class="muted" href="${esc(item.product_url || `https://detail.1688.com/offer/${item.offer_id}.html`)}" target="_blank" rel="noopener">開啟 1688</a>` : ''}${existing}</div></div>
      <div><div class="candidates">${candidates.length ? candidates.map((candidate, index) => candidateCard(item, candidate, index + 1)).join('') : '<div class="reason">尚未取得 SKU 候選；可能需要重新登入／驗證或商品已下架。</div>'}</div>
      <div class="reason"><strong>${esc(item.review_reason || '等待人工確認')}</strong>${(ai.evidence || []).length ? `<br>${esc(ai.evidence.join('；'))}` : ''}${evidence.error ? `<br>${esc(evidence.error)}` : ''}</div>
      <div class="actions"><button class="approve" data-action="approve">核准選取 SKU</button>${replaceButton}<button data-action="defer">稍後處理</button><button data-action="no_match">標記無匹配</button><button data-action="discontinued">標記停售</button></div></div></article>`;
  }

  async function loadSummary() {
    const response = await fetch('/api/sku-mapping/summary');
    const data = await response.json();
    if (!response.ok || data.status !== 'success') throw new Error(data.message || '摘要載入失敗');
    renderSummary(data);
  }

  async function loadQueue() {
    const params = new URLSearchParams({ status: $('status').value, tier: $('tier').value, query: $('query').value, restockOnly: $('restockOnly').checked, pageSize: '200' });
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

  async function scan(scope) {
    if (state.loading) return;
    state.loading = true;
    $('scanRestock').disabled = $('scanAll').disabled = true;
    try {
      const response = await fetch('/api/sku-mapping/scans', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ scope, force: $('forceScan').checked, useAi: $('useAi').checked }) });
      const data = await response.json();
      if (!response.ok || data.status !== 'success') throw new Error(data.message || '掃描啟動失敗');
      message(`已啟動 ${data.jobId}，掃描會在背景執行。`, 'success');
      pollJob(data.jobId);
    } catch (error) { message(error.message, 'error'); }
    finally { state.loading = false; $('scanRestock').disabled = $('scanAll').disabled = false; }
  }

  async function pollJob(jobId) {
    const response = await fetch(`/api/sku-mapping/jobs/${encodeURIComponent(jobId)}`);
    const data = await response.json();
    $('jobStatus').textContent = `${data.status || ''} ${data.completed || 0}/${data.total || 0} ${data.message || ''}`;
    if (['completed','error'].includes(data.status)) { await reload(); return; }
    setTimeout(() => pollJob(jobId), 2500);
  }

  function selectedSku(cardElement) {
    const selected = cardElement.querySelector('.candidate.selected');
    if (selected) return selected;
    return cardElement.querySelector('.candidate');
  }

  async function decide(cardElement, action) {
    const item = state.items.find(row => String(row.id) === String(cardElement.dataset.id));
    if (!item) return;
    const candidate = selectedSku(cardElement);
    if (['approve','replace'].includes(action) && !candidate) { message('請先選擇一個 SKU 候選。', 'error'); return; }
    if (['replace','discontinued','no_match'].includes(action) && !window.confirm(`確定要${action === 'replace' ? '取代既有 mapping' : action === 'discontinued' ? '標記停售' : '標記無匹配'}嗎？`)) return;
    try {
      const response = await fetch('/api/sku-mapping/decisions', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ items: [{ productId: item.product_id, modelId: item.model_id, action, skuId: candidate?.dataset.sku || '', version: item.version }] }) });
      const data = await response.json();
      if (!response.ok || !['success'].includes(data.status)) throw new Error(data.message || '儲存 mapping 失敗');
      state.selectedIds.delete(String(cardElement.dataset.id));
      await reload();
    } catch (error) { message(error.message, 'error'); }
  }

  async function batchApprove() {
    const cards = [...document.querySelectorAll('.queue .card')].filter(card => card.querySelector('.select-item')?.checked);
    if (!cards.length) { message('請先勾選要批次核准的型號。', 'error'); return; }
    const rows = cards.map(card => state.items.find(item => String(item.id) === String(card.dataset.id))).filter(Boolean);
    if (rows.length !== cards.length) { message('清單資料已更新，請先重新載入再批次核准。', 'error'); return; }
    if (cards.some(card => !card.querySelector('.candidate'))) { message('勾選的型號中有項目沒有候選 SKU，請取消勾選或先擴大候選清單。', 'error'); return; }
    const items = cards.map(card => {
      const row = state.items.find(item => String(item.id) === String(card.dataset.id));
      const candidate = selectedSku(card);
      return row && candidate ? { productId: row.product_id, modelId: row.model_id, action: 'approve', skuId: candidate.dataset.sku, version: row.version } : null;
    }).filter(Boolean);
    if (!items.length) { message('請先勾選有候選 SKU 的型號。', 'error'); return; }
    const defaultCount = cards.filter(card => !card.querySelector('.candidate.selected')).length;
    const warning = defaultCount ? `其中 ${defaultCount} 筆未手動選擇候選，將使用第 1 號候選。` : '每筆都已手動選擇候選。';
    if (!window.confirm(`確定核准 ${items.length} 筆 SKU mapping？\n${warning}`)) return;
    try {
      const response = await fetch('/api/sku-mapping/decisions', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ items, batch: true }) });
      const data = await response.json();
      if (!response.ok || data.status !== 'success') throw new Error(data.message || '批次核准失敗');
      cards.forEach(card => state.selectedIds.delete(String(card.dataset.id)));
      await reload();
    } catch (error) { message(error.message, 'error'); }
  }

  $('queue').addEventListener('click', event => {
    const candidate = event.target.closest('.candidate');
    if (candidate) { const parent = candidate.closest('.card'); parent.querySelectorAll('.candidate').forEach(node => node.classList.remove('selected')); candidate.classList.add('selected'); return; }
    const button = event.target.closest('button[data-action]');
    if (button) decide(button.closest('.card'), button.dataset.action);
  });
  $('queue').addEventListener('change', event => {
    const checkbox = event.target.closest('.select-item');
    if (!checkbox) return;
    const cardElement = checkbox.closest('.card');
    if (!cardElement) return;
    if (checkbox.checked) state.selectedIds.add(String(cardElement.dataset.id));
    else state.selectedIds.delete(String(cardElement.dataset.id));
  });
  $('scanRestock').addEventListener('click', () => scan('restock'));
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
  $('restockOnly').addEventListener('change', reload);
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
