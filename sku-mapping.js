(() => {
  const $ = id => document.getElementById(id);
  const state = { items: [], loading: false, activeJobId: '', batchBusy: false, selectedIds: new Set(), itemCache: new Map(), selections: new Map(), page: 1, pageSize: 200, total: 0 };
  const message = (text, type = '') => { $('message').textContent = text || ''; $('message').className = `message ${type}`.trim(); };
  const operationStatus = (text, type = 'running') => {
    const node = $('operationStatus');
    if (!node) return;
    node.hidden = false;
    node.className = `operation-status ${type}`.trim();
    node.innerHTML = type === 'running'
      ? `<span class="operation-spinner" aria-hidden="true"></span><strong>${esc(text)}</strong><span>請不要關閉或重新整理頁面。</span>`
      : `<strong>${esc(text)}</strong>`;
  };
  const setBatchBusy = (busy, text = '') => {
    state.batchBusy = busy;
    ['batchApprove', 'batchDefer', 'batchNoMatch', 'batchDiscontinued'].forEach(id => {
      const button = $(id);
      if (button) button.disabled = busy;
    });
    if (busy) operationStatus(text, 'running');
  };
  const operationDone = (text, type = 'success') => { operationStatus(text, type); message(text, type); };
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
  const cleanLegacyName = value => {
    const raw = String(value ?? '').trim();
    return /&(?:gt|lt);?$/.test(raw) ? raw.replace(/&(?:gt|lt);?$/i, '').trim() : raw;
  };
  const fmt = value => Number(value || 0).toLocaleString('zh-TW');
  const clearSelections = () => { state.selectedIds.clear(); state.selections.clear(); };

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
    const skuName = cleanLegacyName(candidate.sku_name || '');
    const secondName = cleanLegacyName(candidate.second_name || '');
    const approvedFallback = candidate.evidence?.approved_mapping === true;
    const suggested = isSuggestedCandidate(item, candidate);
    const selected = item.review_tier === 'green' && suggested;
    return `<div class="candidate ${selected ? 'selected' : ''} ${suggested ? 'suggested' : ''}" data-key="${esc(candidate.candidate_key || '')}" data-sku="${esc(candidate.sku_id)}" data-name="${esc(skuName)}" data-second="${esc(secondName)}" data-item="${esc(item.id)}">
      <span class="rank">${rank}</span>${candidate.image_url ? `<img src="${esc(candidate.image_url)}" loading="lazy" alt="">` : ''}
      <strong>${esc(skuName || '未命名規格')}${secondName ? ` → ${esc(secondName)}` : ''}</strong><small class="full-spec"><b>完整規格（含型號／第二規格）：</b><br>${esc(candidate.spec_text || [skuName, secondName].filter(Boolean).join(' → ') || skuName || '')}</small><small>SKU ID（輔助）：${esc(candidate.sku_id || '—')}</small><small>價格：${esc(candidate.price ?? '—')}　庫存：${esc(candidate.stock ?? '—')}</small><small>規則分數：${esc(candidate.deterministic_score)}</small>${approvedFallback ? '<small class="suggested-label">目前已核准 mapping（顯示用）</small>' : suggested ? '<small class="suggested-label">系統建議</small>' : ''}</div>`;
  }

  function isSuggestedCandidate(item, candidate) {
    const ai = item.evidence?.ai || {};
    const sameKey = (key, value) => key && String(key) === String(value || '');
    return sameKey(item.suggested_candidate_key, candidate.candidate_key)
      || (!item.suggested_candidate_key && sameKey(item.suggested_sku_id, candidate.sku_id))
      || (ai.decision === 'match' && (
        sameKey(ai.selected_candidate_key, candidate.candidate_key)
        || sameKey(ai.selected_sku_id, candidate.sku_id)
      ));
  }

  function aiSummary(item, candidates) {
    const ai = item.evidence?.ai || {};
    const source = String(ai.source || '');
    const warnings = Array.isArray(ai.warnings) ? ai.warnings.filter(Boolean) : [];
    const evidence = Array.isArray(ai.evidence) ? ai.evidence.filter(Boolean) : [];
    const selected = candidates.find(candidate => String(candidate.candidate_key) === String(ai.selected_candidate_key)) || candidates.find(candidate => String(candidate.sku_id) === String(ai.selected_sku_id));
    const confidence = Number(ai.confidence);
    const confidenceText = Number.isFinite(confidence) && confidence > 0 ? `，信心 ${Math.round(confidence * 100)}%` : '';
    if (source === 'openai' || source === 'grok' || source === 'gemini') {
      const decision = ai.decision === 'match' && selected
        ? `建議候選 #${candidates.indexOf(selected) + 1}（${selected.sku_name}${selected.second_name ? ` → ${selected.second_name}` : ''}）`
        : '暫不判定，保留候選供人工確認';
      const detail = [...evidence, ...warnings].join('；');
      const providerLabel = source === 'gemini' ? 'Gemini AI 初判' : source === 'grok' ? 'Grok AI 初判' : 'OpenAI 初判';
      return `<div class="ai-summary ai-openai"><strong>${providerLabel}</strong>：${esc(decision)}${esc(confidenceText)}${detail ? `<br>${esc(detail)}` : ''}</div>`;
    }
    if (source === 'error') {
      return `<div class="ai-summary ai-warning"><strong>AI 初判未完成</strong>：${esc(warnings.join('；') || 'API 呼叫失敗')}；仍保留規則候選。</div>`;
    }
    if (source === 'rules' && warnings.length) {
      const providerLabel = ai.provider === 'gemini' ? 'Gemini' : ai.provider === 'grok' ? 'Grok' : 'AI';
      const fallback = ai.provider === 'gemini' || ai.provider === 'grok' || ai.fallback === 'rules' ? `${providerLabel} 初判失敗，已回退規則` : 'AI 初判未執行';
      return `<div class="ai-summary ai-warning"><strong>${fallback}</strong>：${esc(warnings.join('；'))}；目前顯示規則候選。</div>`;
    }
    if (candidates.length === 1 && item.review_tier === 'green') {
      return '<div class="ai-summary ai-rules"><strong>規則初判</strong>：唯一且完整精確候選；此情況不另呼叫 AI。</div>';
    }
    if (item.review_tier === 'yellow') {
      return '<div class="ai-summary ai-warning"><strong>AI 尚未判別</strong>：目前是規則找到的多個候選，尚未呼叫 AI；此按鈕會依「規則→AI」順序重新判斷。</div>';
    }
    return '';
  }

  function card(item) {
    const storedCandidates = item.candidates || [];
    // Old approved rows may not have persisted candidate records.  Mirror the
    // current name pair as a display-only card immediately, even before the
    // backend process is restarted for its database migration.  The decision
    // API still checks this pair against the current snapshot before saving.
    const candidates = storedCandidates.length || String(item.mapping_status || '') !== 'approved' || !item.existing_sku_name
      ? storedCandidates
      : [{
        candidate_key: '',
        sku_id: item.existing_sku_id || '',
        sku_name: cleanLegacyName(item.existing_sku_name),
        second_name: cleanLegacyName(item.existing_second_name || ''),
        spec_text: [item.existing_sku_name, item.existing_second_name].filter(Boolean).join(' / '),
        parts: [item.existing_sku_name, item.existing_second_name].filter(Boolean),
        deterministic_score: 0,
        evidence: {approved_mapping: true},
      }];
    candidates.sort((left, right) => Number(isSuggestedCandidate(item, right)) - Number(isSuggestedCandidate(item, left)));
    const evidence = item.evidence || {};
    const ai = evidence.ai || {};
    const sourceImage = item.modelImageUrl || item.productImageUrl || '';
    const canRescan = Boolean(item.offer_id) && !['approved', 'discontinued'].includes(String(item.status || ''));
    const canRerunAi = Boolean(item.snapshot_id) && !['approved', 'discontinued'].includes(String(item.status || ''));
    const manualHint = item.status === 'stale'
      ? '這筆目前只有舊快照候選；請先重新掃描 1688，確認最新完整規格名稱後再核准。'
      : item.existing_sku_name
      ? '人工從完整 SKU 清單選擇的規格優先級最高；點擊下方清單載入後，按綠色按鈕核准即可更新目前 mapping。'
      : (candidates.length ? '候選僅供參考；點擊下方完整 SKU 清單即可載入並重新選擇。' : '先重新掃描此 1688 商品；也可以點擊下方清單載入完整 SKU 手動指定。');
    const manualTools = `<div class="manual-tools">
      <div class="manual-hint">${manualHint}</div>
      <div class="actions">${canRescan ? '<button class="primary rescan" data-action="rescan">重新掃描此商品</button>' : ''}${canRerunAi ? '<button class="primary rerun-ai" data-action="rerun-ai">用現有 SKU 清單重新判斷（規則→AI）</button>' : ''}</div>
      <div class="catalog-picker"><select class="catalog-select"><option value="">尚未載入；點擊後讀取完整 SKU</option></select></div>
    </div>`;
    const hasExistingName = Boolean(item.existing_sku_name || item.existing_second_name);
    const existingApproved = String(item.mapping_status || '') === 'approved';
    const existing = hasExistingName
      ? `<div class="existing-mapping${existingApproved ? '' : ' legacy'}"><strong>${existingApproved ? '現有 mapping' : '舊名稱紀錄（尚未依 v2 核准）'}</strong><br>${esc(cleanLegacyName(item.existing_sku_name || '—'))}${item.existing_second_name ? ` → ${esc(cleanLegacyName(item.existing_second_name))}` : ''}<br><small>SKU ID（輔助）：${esc(item.existing_sku_id || '—')}</small><br><span>${esc(item.mapping_status || 'legacy_pending_name')}</span></div>`
      : '<div class="existing-mapping empty-mapping">目前沒有正式 mapping</div>';
    return `<article class="card tier-card-${esc(item.review_tier)}" data-id="${esc(item.id)}" data-tier="${esc(item.review_tier)}">
      <label class="select-row"><input type="checkbox" class="select-item"> 批次處理</label>
      <div class="source">${sourceImage ? `<img src="${esc(sourceImage)}" loading="lazy" alt="">` : '<div class="source-placeholder">無圖片</div>'}
        <div><h2>${esc(item.model_name || '未命名型號')}</h2><p>${esc(item.product_name || '')}</p><p>商品 ID：${esc(item.product_id)}　規格 ID：${esc(item.model_id)}</p><p>型號月銷量：<strong>${fmt(item.monthlySales)}</strong>　商品月銷量：<strong>${fmt(item.productMonthlySales)}</strong></p><span class="badge ${esc(item.status)}">${esc(item.status)}</span><span class="tier-badge tier-${esc(item.review_tier)}">${esc(item.review_tier === 'green' ? '綠色：唯一精確' : item.review_tier === 'yellow' ? '黃色：人工比較' : item.review_tier === 'red' && item.status === 'stale' ? '紅色：需重新掃描' : item.review_tier === 'red' ? '紅色：阻擋' : '已核准')}</span>${item.offer_id ? `<a class="open-1688" href="${esc(item.product_url || `https://detail.1688.com/offer/${item.offer_id}.html`)}" target="_blank" rel="noopener">開啟 1688 ↗</a>` : ''}${existing}</div></div>
      <div><div class="candidates">${candidates.length ? candidates.map((candidate, index) => candidateCard(item, candidate, index + 1)).join('') : '<div class="reason">尚未取得可通過規則的 SKU 候選；不代表 1688 沒有這個 SKU。</div>'}</div>${aiSummary(item, candidates)}${manualTools}
      <div class="reason"><strong>${esc(item.review_reason || '等待人工確認')}</strong>${(ai.evidence || []).length ? `<br>${esc(ai.evidence.join('；'))}` : ''}${evidence.error ? `<br>${esc(evidence.error)}` : ''}</div>
      <div class="actions"><button class="approve" data-action="approve">核准選取 SKU</button><button data-action="defer">稍後處理</button><button data-action="no_match">標記無匹配</button><button data-action="discontinued">標記停售</button></div></div></article>`;
  }

  async function loadSummary() {
    const response = await fetch('/api/sku-mapping/summary');
    const data = await response.json();
    if (!response.ok || data.status !== 'success') throw new Error(data.message || '摘要載入失敗');
    renderSummary(data);
    return data;
  }

  async function loadQueue() {
    const params = new URLSearchParams({ status: $('status').value, tier: $('tier').value, query: $('query').value, page: String(state.page), pageSize: String(state.pageSize) });
    const response = await fetch(`/api/sku-mapping/queue?${params}`);
    const data = await response.json();
    if (!response.ok || data.status !== 'success') throw new Error(data.message || '待審核清單載入失敗');
    state.items = data.items || [];
    state.total = Number(data.total || 0);
    state.items.forEach(item => state.itemCache.set(String(item.id), item));
    $('queue').innerHTML = state.items.map(card).join('');
    document.querySelectorAll('.queue .card').forEach(cardElement => {
      const checkbox = cardElement.querySelector('.select-item');
      if (checkbox) checkbox.checked = state.selectedIds.has(String(cardElement.dataset.id));
      const saved = state.selections.get(String(cardElement.dataset.id));
      if (saved) {
        const savedCandidate = [...cardElement.querySelectorAll('.candidate')].find(node =>
          (saved.candidateKey && node.dataset.key === saved.candidateKey) ||
          (saved.skuName && node.dataset.name === saved.skuName && node.dataset.second === (saved.skuSecondName || ''))
        );
        if (savedCandidate) savedCandidate.classList.add('selected');
      }
    });
    $('empty').hidden = state.items.length > 0;
    renderPagination();
  }

  function renderPagination() {
    const container = $('pagination');
    if (!container) return;
    const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
    const first = state.total ? ((state.page - 1) * state.pageSize) + 1 : 0;
    const last = Math.min(state.page * state.pageSize, state.total);
    container.innerHTML = `<span>顯示 ${first}-${last}／共 ${state.total} 筆</span><button data-page="prev" ${state.page <= 1 ? 'disabled' : ''}>上一頁</button><span>第 ${state.page}／${pages} 頁</span><button data-page="next" ${state.page >= pages ? 'disabled' : ''}>下一頁</button>`;
  }

  async function reload() {
    try {
      const [summary] = await Promise.all([loadSummary(), loadQueue()]);
      const latest = summary?.latestRun || {};
      const latestJobId = latest.job_id || latest.jobId || '';
      if (latestJobId && ['queued', 'running'].includes(String(latest.status || '')) && state.activeJobId !== latestJobId) {
        pollJob(latestJobId);
      }
      message(state.activeJobId ? `已載入 ${state.items.length} 筆 mapping；背景工作 ${latest.completed || 0}/${latest.total || 0} 執行中。` : `已載入 ${state.items.length} 筆 mapping。`, 'success');
      return true;
    }
    catch (error) { message(error.message, 'error'); return false; }
  }

  function batchRemaining(rows, action) {
    // In the default 待處理 view, approved and discontinued rows must leave
    // the queue.  This guard makes a stale/failed refresh visible instead of
    // silently leaving the old card on screen and making a completed batch
    // look as if it came back.
    if ($('status').value !== 'review' || !['approve', 'discontinued'].includes(action)) return 0;
    const ids = new Set(rows.map(row => String(row.id)));
    return state.items.filter(row => ids.has(String(row.id))).length;
  }

  async function scan(scope, target = null, rebuild = true) {
    if (state.loading) return;
    state.loading = true;
    $('scanAll').disabled = true;
    try {
      const body = { scope, force: target ? true : $('forceScan').checked, useAi: $('useAi').checked, rebuild };
      if (target) { body.productId = target.product_id; body.modelId = target.model_id; body.offerId = target.offer_id; }
      const response = await fetch('/api/sku-mapping/scans', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
      const data = await response.json();
      if (!response.ok || data.status !== 'success') throw new Error(data.message || '掃描啟動失敗');
      message(target ? `已啟動單筆重掃 ${data.jobId}，完成後會更新這個型號。` : `已啟動 ${data.jobId}，掃描會在背景執行。`, 'success');
      pollJob(data.jobId);
    } catch (error) { message(error.message, 'error'); }
    finally { state.loading = false; $('scanAll').disabled = false; }
  }

  async function reanalyzeExisting(aiOnly = false) {
    if (state.loading) return;
    state.loading = true;
    const button = $(aiOnly ? 'reanalyzeExistingAi' : 'reanalyzeExisting');
    if (button) button.disabled = true;
    try {
      const response = await fetch('/api/sku-mapping/reanalyze-existing', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({useAi: aiOnly ? true : $('useAi').checked, rebuild: true, aiOnly}),
      });
      const data = await response.json();
      if (!response.ok || data.status !== 'success') throw new Error(data.message || '現有快照重判啟動失敗');
      state.activeJobId = data.jobId || '';
      message(aiOnly
        ? `已啟動 ${data.jobId}；會把每個現有快照的完整 SKU 清單交給 AI 找最接近項目，不會開啟 1688。`
        : `已啟動 ${data.jobId}；只會使用本機既有快照，先規則後 AI，不會開啟 1688。`, 'success');
      pollJob(data.jobId);
    } catch (error) {
      try {
        const summary = await loadSummary();
        const latest = summary?.latestRun || {};
        const latestJobId = latest.job_id || latest.jobId || '';
        if (latestJobId && ['queued', 'running'].includes(String(latest.status || ''))) {
          state.activeJobId = latestJobId;
          pollJob(latestJobId);
          message(`已有現有快照重判工作執行中，已接續顯示進度：${latest.completed || 0}/${latest.total || 0}。`, 'success');
          return;
        }
      } catch (_) { /* 保留原始錯誤訊息 */ }
      message(error.message, 'error');
    }
    finally { state.loading = false; if (button) button.disabled = false; }
  }

  async function pollJob(jobId) {
    state.activeJobId = jobId;
    const response = await fetch(`/api/sku-mapping/jobs/${encodeURIComponent(jobId)}`);
    const data = await response.json();
    $('jobStatus').textContent = `${data.status || ''} ${data.completed || 0}/${data.total || 0} ${data.message || ''}`;
    if (data.status === 'waiting_for_login') { state.activeJobId = ''; message('1688 需要登入或人工驗證；完成後請重新掃描。', 'error'); return; }
    if (['completed','error'].includes(data.status)) {
      state.activeJobId = '';
      await reload();
      const doneMessage = data.aiOnly
        ? (data.message || '現有快照 AI 重判完成；未連線 1688，仍需人工核准。')
        : data.scope === 'existing_snapshots'
        ? (data.message || '現有快照規則→AI 重判完成；未連線 1688。')
        : '掃描完成；先用規則判定，只有規則無候選的項目才使用 Gemini。';
      message(data.status === 'completed' ? doneMessage : `掃描失敗：${data.error || data.message || '請查看該筆狀態'}`, data.status === 'completed' ? 'success' : 'error');
      return;
    }
    // Suggestions are written one model at a time.  Refresh the visible page
    // while the worker is running so rules/AI results appear incrementally
    // instead of waiting for all thousands of models to finish.
    try {
      await Promise.all([loadSummary(), loadQueue()]);
    } catch (_) {
      // Keep polling even if one intermediate refresh races with a DB write.
    }
    setTimeout(() => pollJob(jobId), 2500);
  }

  async function loadCatalog(cardElement) {
    const item = state.items.find(row => String(row.id) === String(cardElement.dataset.id));
    if (!item) return;
    const picker = cardElement.querySelector('.catalog-picker');
    const select = cardElement.querySelector('.catalog-select');
    if (!picker || !select) return;
    if (select.dataset.loaded === 'true' || select.dataset.loading === 'true') return;
    select.dataset.loading = 'true';
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
      select.innerHTML = `<option value="">請選擇要核准的完整規格（共 ${skus.length} 個）</option>` + skus.map(sku => `<option value="${esc(sku.candidate_key || '')}" data-sku-id="${esc(sku.sku_id || '')}" data-sku-name="${esc(sku.sku_name || '')}" data-second-name="${esc(sku.second_name || '')}">${esc(sku.sku_name || '')}${sku.second_name ? ` → ${esc(sku.second_name)}` : ''}｜${esc(sku.spec_text || '')}</option>`).join('');
      select.disabled = false;
      select.dataset.loaded = 'true';
      message('已載入完整規格清單。請確認第一、第二規格後再核准。', 'success');
    } catch (error) {
      const detail = error.name === 'AbortError' ? '完整 SKU 清單載入逾時，請重新掃描此商品。' : error.message;
      select.innerHTML = `<option value="">${esc(detail)}；移入或點擊此清單重試</option>`;
      select.disabled = false;
      select.dataset.loaded = 'false';
      message(detail, 'error');
    } finally {
      select.dataset.loading = 'false';
    }
  }

  function selectedSku(cardElement) {
    const selected = cardElement.querySelector('.candidate.selected');
    if (selected) return selected;
    return cardElement.querySelector('.candidate');
  }

  function rememberSelection(cardElement) {
    const id = String(cardElement?.dataset.id || '');
    if (id) state.selections.set(id, selectionFromCard(cardElement));
  }

  function selectionFromCard(cardElement) {
    const option = cardElement.querySelector('.catalog-select')?.selectedOptions?.[0];
    if (option && option.value) return { candidateKey: option.value, skuId: option.dataset.skuId || '', skuName: option.dataset.skuName || '', skuSecondName: option.dataset.secondName || '' };
    const candidate = cardElement.querySelector('.candidate.selected');
    if (!candidate) {
      const saved = state.selections.get(String(cardElement?.dataset.id || ''));
      if (saved) return saved;
    }
    const fallback = candidate || selectedSku(cardElement);
    if (!fallback) return { candidateKey: '', skuId: '', skuName: '', skuSecondName: '' };
    return { candidateKey: fallback.dataset.key || '', skuId: fallback.dataset.sku || '', skuName: fallback.dataset.name || '', skuSecondName: fallback.dataset.second || '' };
  }

  async function decide(cardElement, action, explicitSkuId = '', triggerButton = null) {
    const item = state.items.find(row => String(row.id) === String(cardElement.dataset.id));
    if (!item) return;
    const selection = selectionFromCard(cardElement);
    if (explicitSkuId && !selection.skuId) selection.skuId = explicitSkuId;
    if (['approve','replace'].includes(action) && !selection.candidateKey && !selection.skuName && !selection.skuId) { message('請先選擇完整規格名稱，或從完整 SKU 清單手動指定。', 'error'); return; }
    if (['replace','discontinued','no_match'].includes(action) && !window.confirm(`確定要${action === 'replace' ? '取代既有 mapping' : action === 'discontinued' ? '標記停售' : '標記無匹配'}嗎？`)) return;
    if (triggerButton) triggerButton.disabled = true;
    message(action === 'approve' ? '正在儲存 SKU mapping…' : '正在更新 mapping 狀態…');
    try {
      const response = await fetch('/api/sku-mapping/decisions', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ items: [{ productId: item.product_id, modelId: item.model_id, action, candidateKey: selection.candidateKey, skuId: selection.skuId, skuName: selection.skuName, skuSecondName: selection.skuSecondName, version: item.version }] }) });
      const data = await response.json();
      if (response.status === 409) { await reload(); message('這筆資料剛重新掃描，頁面版本已更新；請使用重新載入後的候選再操作。', 'error'); return; }
      if (!response.ok || !['success'].includes(data.status)) throw new Error(data.message || '儲存 mapping 失敗');
      state.selectedIds.delete(String(cardElement.dataset.id));
      state.selections.delete(String(cardElement.dataset.id));
      await reload();
    } catch (error) { message(error.message, 'error'); }
    finally { if (triggerButton && document.body.contains(triggerButton)) triggerButton.disabled = false; }
  }

  async function rerunAi(cardElement, triggerButton) {
    const item = state.items.find(row => String(row.id) === String(cardElement.dataset.id));
    if (!item || !item.snapshot_id) { message('目前沒有可用 SKU 快照，請先重新掃描此商品。', 'error'); return; }
    if (triggerButton) triggerButton.disabled = true;
    operationStatus('正在重新判斷：先跑規則，無規則候選時才呼叫 AI…', 'running');
    message('正在重新判斷：先跑規則，無候選時才呼叫 AI…');
    try {
      const response = await fetch('/api/sku-mapping/ai-reviews', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({productId: item.product_id, modelId: item.model_id}),
      });
      const data = await response.json();
      if (!response.ok || data.status !== 'success') throw new Error(data.message || 'AI 初判失敗');
      await reload();
      const doneMessage = data.usedAi === false
        ? '規則重判完成；已有規則候選，因此未呼叫 AI。仍需人工核准。'
        : `AI 初判完成；已保留最多 ${data.candidateCount || 4} 張候選卡，結果已更新。仍需人工核准。`;
      operationDone(doneMessage, 'success');
    } catch (error) { operationDone(`重新判斷失敗：${error.message}`, 'error'); }
    finally { if (triggerButton && document.body.contains(triggerButton)) triggerButton.disabled = false; }
  }

  async function batchStatusAction(action, label) {
    if (state.batchBusy) return;
    const cards = [...document.querySelectorAll('.queue .card')].filter(card => card.querySelector('.select-item')?.checked);
    if (!cards.length && !state.selectedIds.size) { message('請先勾選要批次處理的型號。', 'error'); return; }
    cards.forEach(card => rememberSelection(card));
    const rows = [...state.selectedIds].map(id => state.itemCache.get(String(id))).filter(Boolean);
    if (!rows.length) { message('清單資料已更新，請先重新載入。', 'error'); return; }
    const prompt = action === 'defer'
      ? `確定將 ${rows.length} 筆移到「稍後處理」？之後可用狀態篩選找回。`
      : action === 'no_match'
      ? `確定將 ${rows.length} 筆標記為「無匹配」？這不會刪除 1688 快照，之後仍可重新掃描或重跑 AI。`
      : `確定將 ${rows.length} 筆標記為「停售」？這些項目會被阻擋採購。`;
    if (!window.confirm(prompt)) return;
    const items = rows.map(row => ({ productId: row.product_id, modelId: row.model_id, action, version: row.version }));
    setBatchBusy(true, `${label}進行中：已送出 ${rows.length} 筆`);
    try {
      const response = await fetch('/api/sku-mapping/decisions', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({items, batch: true}),
      });
      const data = await response.json();
      if (response.status === 409) { await reload(); operationDone('部分資料剛更新，請重新勾選後再試。', 'error'); return; }
      if (!response.ok || data.status !== 'success') throw new Error(data.message || `${label}失敗`);
      rows.forEach(row => { state.selectedIds.delete(String(row.id)); state.selections.delete(String(row.id)); });
      const refreshed = await reload();
      if (!refreshed) {
        operationDone(`後端已${label} ${rows.length} 筆，但清單重新載入失敗；請按「重新載入」確認最新狀態。`, 'error');
        return;
      }
      const remaining = batchRemaining(rows, action);
      if (remaining) {
        operationDone(`後端已回應${label}，但仍有 ${remaining} 筆出現在「待處理」；請重新載入後再檢查。`, 'error');
        return;
      }
      operationDone(`已${label} ${rows.length} 筆。`, 'success');
    } catch (error) {
      // A batch can be partially applied if a later row fails.  Always
      // reconcile the page so the user does not keep looking at stale cards.
      const refreshed = await reload();
      operationDone(`${label}失敗：${error.message}${refreshed ? '；已重新載入目前實際狀態。' : '；重新載入也失敗，請稍後按「重新載入」。'}`, 'error');
    }
    finally { setBatchBusy(false); }
  }

  async function batchApprove() {
    if (state.batchBusy) return;
    const cards = [...document.querySelectorAll('.queue .card')].filter(card => card.querySelector('.select-item')?.checked);
    if (!cards.length && !state.selectedIds.size) { message('請先勾選要批次核准的型號。', 'error'); return; }
    cards.forEach(card => rememberSelection(card));
    const selectedRows = [...state.selectedIds].map(id => state.itemCache.get(String(id))).filter(Boolean);
    if (!selectedRows.length) { message('清單資料已更新，請先重新載入再批次核准。', 'error'); return; }
    const selections = selectedRows.map(row => {
      const saved = state.selections.get(String(row.id));
      const first = row.candidates?.[0];
      return { row, selection: saved || (first ? { candidateKey: first.candidate_key || '', skuId: first.sku_id || '', skuName: first.sku_name || '', skuSecondName: first.second_name || '' } : { candidateKey: '', skuId: '', skuName: '', skuSecondName: '' }) };
    });
    if (selections.some(selection => !selection.selection.candidateKey && !selection.selection.skuName && !selection.selection.skuId)) {
      message('勾選的型號中有項目尚未選擇完整規格；請先選候選或從完整清單指定。', 'error');
      return;
    }
    const items = selections.map(selection => ({
      productId: selection.row.product_id,
      modelId: selection.row.model_id,
      action: 'approve',
      candidateKey: selection.selection.candidateKey,
      skuId: selection.selection.skuId,
      skuName: selection.selection.skuName,
      skuSecondName: selection.selection.skuSecondName,
      version: selection.row.version,
    }));
    if (!items.length) { message('請先勾選有候選 SKU 的型號。', 'error'); return; }
    const manualCount = selections.filter(selection => state.selections.has(String(selection.row.id))).length;
    const defaultCount = selections.filter(selection => !selection.selection.candidateKey).length;
    const warning = [
      manualCount ? `${manualCount} 筆使用完整 SKU 清單中的手動選擇。` : '',
      defaultCount ? `${defaultCount} 筆未手動指定，將使用候選第 1 號。` : '',
    ].filter(Boolean).join('\n') || '每筆都已手動選擇候選。';
    if (!window.confirm(`確定核准 ${items.length} 筆 SKU mapping？\n${warning}`)) return;
    setBatchBusy(true, `批次核准進行中：正在處理 ${items.length} 筆，請稍候…`);
    try {
      const response = await fetch('/api/sku-mapping/decisions', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ items, batch: true }) });
      const data = await response.json();
      if (response.status === 409) { await reload(); operationDone('部分資料剛重新掃描，頁面版本已更新；請重新勾選後再試。', 'error'); return; }
      if (!response.ok || data.status !== 'success') throw new Error(data.message || '批次核准失敗');
      selectedRows.forEach(row => { state.selectedIds.delete(String(row.id)); state.selections.delete(String(row.id)); });
      const refreshed = await reload();
      if (!refreshed) {
        operationDone(`後端已核准 ${selectedRows.length} 筆，但清單重新載入失敗；請按「重新載入」確認最新狀態。`, 'error');
        return;
      }
      const remaining = batchRemaining(selectedRows, 'approve');
      if (remaining) {
        operationDone(`後端已回應批次核准，但仍有 ${remaining} 筆出現在「待處理」；請重新載入後再檢查。`, 'error');
        return;
      }
      operationDone(`批次核准完成：已核准 ${selectedRows.length} 筆。`, 'success');
    } catch (error) {
      const refreshed = await reload();
      operationDone(`批次核准失敗：${error.message}${refreshed ? '；已重新載入目前實際狀態。' : '；重新載入也失敗，請稍後按「重新載入」。'}`, 'error');
    }
    finally { setBatchBusy(false); }
  }

  $('queue').addEventListener('click', event => {
    const catalogSelect = event.target.closest('.catalog-select');
    if (catalogSelect && catalogSelect.dataset.loaded !== 'true' && catalogSelect.dataset.loading !== 'true') {
      loadCatalog(catalogSelect.closest('.card'));
      return;
    }
    const candidate = event.target.closest('.candidate');
    if (candidate) { const parent = candidate.closest('.card'); parent.querySelectorAll('.candidate').forEach(node => node.classList.remove('selected')); candidate.classList.add('selected'); rememberSelection(parent); return; }
    const button = event.target.closest('button[data-action]');
    if (!button) return;
    const cardElement = button.closest('.card');
    if (button.dataset.action === 'load_catalog') return loadCatalog(cardElement);
    if (button.dataset.action === 'rescan') {
      const item = state.items.find(row => String(row.id) === String(cardElement?.dataset.id));
      if (item) return scan('all', item);
    }
    if (button.dataset.action === 'rerun-ai') return rerunAi(cardElement, button);
    decide(cardElement, button.dataset.action, '', button);
  });
  $('queue').addEventListener('change', event => {
    const catalogSelect = event.target.closest('.catalog-select');
    if (catalogSelect) {
      rememberSelection(catalogSelect.closest('.card'));
      message(catalogSelect.value ? '已選擇完整 SKU；請按下方綠色「核准選取 SKU」儲存。' : '請從完整 SKU 清單選擇一個規格。', catalogSelect.value ? 'success' : '');
      return;
    }
    const checkbox = event.target.closest('.select-item');
    if (!checkbox) return;
    const cardElement = checkbox.closest('.card');
    if (!cardElement) return;
    if (checkbox.checked) state.selectedIds.add(String(cardElement.dataset.id));
    else state.selectedIds.delete(String(cardElement.dataset.id));
  });
  $('scanAll').addEventListener('click', () => scan('all', null, true));
  $('reanalyzeExisting').addEventListener('click', () => reanalyzeExisting(false));
  $('reanalyzeExistingAi').addEventListener('click', () => reanalyzeExisting(true));
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
  $('batchDefer').addEventListener('click', () => batchStatusAction('defer', '批次稍後處理'));
  $('batchNoMatch').addEventListener('click', () => batchStatusAction('no_match', '批次標記無匹配'));
  $('batchDiscontinued').addEventListener('click', () => batchStatusAction('discontinued', '批次標記停售'));
  $('reload').addEventListener('click', reload);
  let queryTimer;
  $('query').addEventListener('input', () => { clearTimeout(queryTimer); clearSelections(); state.page = 1; queryTimer = setTimeout(reload, 350); });
  $('pagination').addEventListener('click', event => {
    const button = event.target.closest('button[data-page]');
    if (!button || button.disabled) return;
    const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
    state.page = button.dataset.page === 'next' ? Math.min(pages, state.page + 1) : Math.max(1, state.page - 1);
    loadQueue().catch(error => message(error.message, 'error'));
    window.scrollTo({top: 0, behavior: 'smooth'});
  });
  ['status', 'tier'].forEach(id => $(id).addEventListener('change', () => { clearSelections(); state.page = 1; reload(); }));
  document.addEventListener('keydown', event => {
    const active = document.querySelector('.card:hover');
    if (!active || ['INPUT','SELECT','TEXTAREA'].includes(document.activeElement.tagName)) return;
    if (/^[1-5]$/.test(event.key)) active.querySelectorAll('.candidate')[Number(event.key) - 1]?.click();
    if (event.key === 'Enter') active.querySelector('[data-action="approve"]')?.click();
  });
  reload();
})();
