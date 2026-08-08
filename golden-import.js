(() => {
  const $ = id => document.getElementById(id);
  const state = { candidates: [], previews: new Map(), busy: new Set() };
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
  const message = (text, type = '') => { $('message').textContent = text || ''; $('message').className = `message ${type}`.trim(); };
  const fmt = value => Number(value || 0).toLocaleString('zh-TW');

  function render() {
    $('summary').textContent = `蝦皮快取 ${fmt(state.sourceCount)} 筆，Golden Table ${fmt(state.goldenCount)} 筆，待加入 ${fmt(state.candidates.length)} 筆`;
    $('empty').hidden = state.candidates.length > 0;
    $('candidates').innerHTML = state.candidates.map(productCard).join('');
  }

  function productCard(product) {
    const preview = state.previews.get(product.productId);
    const image = product.productImageUrl ? `<img src="${esc(product.productImageUrl)}" loading="lazy" alt="">` : '<div class="source-placeholder">無圖片</div>';
    return `<article class="card" data-product-id="${esc(product.productId)}">
      <div class="product-head">${image}<div><h2>${esc(product.productName || '未命名商品')}</h2><div class="meta">商品 ID：${esc(product.productId)}<br>規格：${fmt(product.modelCount)} 個　庫存：${fmt((product.models || []).reduce((sum, m) => sum + Number(m.stock || 0), 0))}</div></div></div>
      <div class="form-row"><input class="alibaba-url" type="url" placeholder="貼上 1688 商品網址 https://detail.1688.com/offer/..." value="${esc(preview?.inputUrl || '')}"><button class="preview-btn primary" data-action="preview" ${state.busy.has(product.productId) ? 'disabled' : ''}>${state.busy.has(product.productId) ? '讀取中…' : '讀取 1688 SKU'}</button></div>
      ${preview ? previewCard(product, preview) : ''}
    </article>`;
  }

  function candidateLabel(candidate) {
    const second = candidate.second_name ? ` → ${candidate.second_name}` : '';
    const manual = candidate.evidence?.manual_only ? '（請人工確認）' : '';
    return `${candidate.sku_name || '未命名'}${second}｜SKU ${candidate.sku_id || '—'}${manual}`;
  }

  function previewCard(product, preview) {
    if (preview.error) return `<div class="warning">${esc(preview.error)}</div>`;
    const rows = (preview.models || []).map(model => {
      const options = (model.candidates || []).map(candidate => `<option value="${esc(candidate.candidate_key)}" data-sku-id="${esc(candidate.sku_id)}" ${candidate.candidate_key === model.suggestedCandidateKey ? 'selected' : ''}>${esc(candidateLabel(candidate))}</option>`).join('');
      return `<div class="model-row"><div class="model-info"><strong>${esc(model.modelName)}</strong><small>規格 ID：${esc(model.modelId)}　庫存：${esc(model.stock)}</small></div><select class="model-select" data-model-id="${esc(model.modelId)}"><option value="">請選擇 1688 規格</option>${options}</select></div>`;
    }).join('');
    const hasManual = (preview.models || []).some(model => (model.candidates || []).some(candidate => candidate.evidence?.manual_only));
    return `<div class="preview"><div class="snapshot">1688：${esc(preview.snapshot.productName || '未命名商品')}<br>Offer ID：${esc(preview.snapshot.offerId)}　SKU：${esc(preview.snapshot.skuCount)} 個</div>${hasManual ? '<div class="warning">部分規格沒有自動配對，請逐項人工確認；系統不會替你猜測。</div>' : ''}${rows}<div class="preview-actions"><button class="commit" data-action="commit">確認並寫入 Golden Table</button></div></div>`;
  }

  async function load() {
    try {
      const response = await fetch('/api/golden-table/import/candidates');
      const data = await response.json();
      if (!response.ok || data.status !== 'success') throw new Error(data.message || '候選清單載入失敗');
      state.sourceCount = data.sourceCount || 0;
      state.goldenCount = data.goldenCount || 0;
      state.candidates = data.candidates || [];
      message(data.message || '', data.candidateCount ? '' : 'success');
      render();
    } catch (error) { message(error.message, 'error'); }
  }

  async function preview(product, card) {
    const url = card.querySelector('.alibaba-url').value.trim();
    if (!url) { message('請先貼上 1688 商品網址', 'error'); return; }
    state.busy.add(product.productId); render();
    try {
      const response = await fetch('/api/golden-table/import/preview', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({productId:product.productId, alibabaProductUrl:url})});
      const data = await response.json();
      if (!response.ok || data.status !== 'success') throw new Error(data.message || '1688 SKU 讀取失敗');
      data.inputUrl = url;
      state.previews.set(product.productId, data);
      message(`已讀取 ${product.productId} 的 1688 SKU，請檢查每個規格配對。`, 'success');
    } catch (error) { state.previews.set(product.productId, {inputUrl:url, error:error.message}); message(error.message, 'error'); }
    finally { state.busy.delete(product.productId); render(); }
  }

  async function commit(product, card) {
    const preview = state.previews.get(product.productId);
    if (!preview || preview.error) return;
    const mappings = [...card.querySelectorAll('.model-select')].map(select => ({modelId:select.dataset.modelId, candidateKey:select.value, skuId:select.selectedOptions[0]?.dataset.skuId || ''}));
    if (mappings.some(item => !item.candidateKey)) { message('還有規格尚未選擇 1688 SKU', 'error'); return; }
    const button = card.querySelector('.commit'); button.disabled = true;
    try {
      const response = await fetch('/api/golden-table/import/commit', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({productId:product.productId, offerId:preview.snapshot.offerId, fingerprint:preview.snapshot.fingerprint, mappings, alibabaProductName:preview.snapshot.productName, alibabaProductUrl:preview.snapshot.productUrl})});
      const data = await response.json();
      if (!response.ok || data.status !== 'success') throw new Error(data.message || '寫入失敗');
      state.candidates = state.candidates.filter(item => item.productId !== product.productId);
      state.previews.delete(product.productId); render();
      message(`商品 ${product.productId} 已寫入，已建立備份檔。`, 'success');
    } catch (error) { button.disabled = false; message(error.message, 'error'); }
  }

  $('reload').addEventListener('click', load);
  $('candidates').addEventListener('click', event => {
    const button = event.target.closest('button[data-action]'); if (!button) return;
    const card = button.closest('[data-product-id]'); const product = state.candidates.find(item => item.productId === card.dataset.productId); if (!product) return;
    if (button.dataset.action === 'preview') preview(product, card);
    if (button.dataset.action === 'commit') commit(product, card);
  });
  load();
})();
