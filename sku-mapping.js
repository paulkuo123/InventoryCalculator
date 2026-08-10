(() => {
  const $ = id => document.getElementById(id);
  const state = { items: [], loading: false, activeJobId: '', batchBusy: false, selectedIds: new Set(), itemCache: new Map(), selections: new Map(), page: 1, pageSize: 200, total: 0, latestJobShown: '', inventorySource: null };
  const urlState = { groups: [], activeGroup: null, preview: null, busy: false, healthBusy: false, healthJobId: '', loadRequest: 0 };
  const baseTitle = document.title;
  let operationDismissTimer = null;
  let operationFadeTimer = null;
  const uiText = value => String(value ?? '').replace(/\bGemini\b/gi, 'AI');
  const displayText = value => esc(uiText(value));
  const message = (text, type = '') => { $('message').textContent = uiText(text); $('message').className = `message ${type}`.trim(); };
  const cancelOperationDismiss = () => {
    if (operationDismissTimer) window.clearTimeout(operationDismissTimer);
    if (operationFadeTimer) window.clearTimeout(operationFadeTimer);
    operationDismissTimer = null;
    operationFadeTimer = null;
  };
  const dismissOperationStatus = () => {
    const node = $('operationStatus');
    if (!node) return;
    cancelOperationDismiss();
    node.classList.add('is-dismissing');
    operationFadeTimer = window.setTimeout(() => {
      node.hidden = true;
      node.classList.remove('is-dismissing');
      operationFadeTimer = null;
    }, 180);
  };
  const operationStatus = (text, type = 'running', details = {}) => {
    const node = $('operationStatus');
    if (!node) return;
    cancelOperationDismiss();
    const completed = Math.max(0, Number(details.completed || 0));
    const total = Math.max(0, Number(details.total || 0));
    const percent = total ? Math.min(100, Math.round((completed / total) * 100)) : 0;
    const title = details.title || (type === 'running' ? '背景工作執行中' : type === 'success' ? '背景工作已完成' : '背景工作需要注意');
    const badge = type === 'running' ? '執行中' : type === 'success' ? '已完成' : '失敗／暫停';
    const closeButton = type === 'running' ? '' : '<button type="button" class="operation-close" aria-label="關閉完成提示">×</button>';
    node.hidden = false;
    node.className = `operation-status ${type}`.trim();
    node.innerHTML = `<div class="operation-heading">${type === 'running' ? '<span class="operation-spinner" aria-hidden="true"></span>' : ''}<strong>${displayText(title)}</strong><span class="operation-badge">${badge}</span>${closeButton}</div>
      <div class="operation-message">${displayText(text)}</div>
      ${type === 'running' ? `<div class="operation-progress-row"><div class="operation-progress" role="progressbar" aria-label="背景工作進度" aria-valuemin="0" aria-valuemax="${total || 0}" aria-valuenow="${completed}"><i style="width:${total ? percent : 8}%"></i></div><b>${total ? `${completed} / ${total}（${percent}%）` : '準備中…'}</b></div><small>本頁會每幾秒自動更新；即使重新整理，也會自動接回這項工作。</small>` : '<small>清單已重新讀取；你可以繼續審核。</small>'}`;
    document.title = type === 'running' ? `${total ? `${completed}/${total}` : '執行中'}｜${baseTitle}` : baseTitle;
    if (type !== 'running') {
      const delay = type === 'error' ? 8000 : 6000;
      operationDismissTimer = window.setTimeout(dismissOperationStatus, delay);
    }
  };
  const setBatchBusy = (busy, text = '') => {
    state.batchBusy = busy;
    ['batchApprove', 'batchDefer', 'batchNoMatch', 'batchDiscontinued'].forEach(id => {
      const button = $(id);
      if (button) button.disabled = busy;
    });
    updateSelectAllState();
    if (busy) operationStatus(text, 'running');
  };
  const operationDone = (text, type = 'success', details = {}) => { operationStatus(text, type, details); message(text, type); };
  const setJobBusy = busy => {
    ['scanAll', 'scanVisiblePage', 'reanalyzeExisting', 'reanalyzeExistingAi'].forEach(id => {
      const button = $(id);
      if (button) button.disabled = busy;
    });
  };
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
  const cleanLegacyName = value => {
    const raw = String(value ?? '').trim();
    return /&(?:gt|lt);?$/.test(raw) ? raw.replace(/&(?:gt|lt);?$/i, '').trim() : raw;
  };
  const fmt = value => Number(value || 0).toLocaleString('zh-TW');
  const clearSelections = () => {
    state.selectedIds.clear();
    state.selections.clear();
    updateSelectAllState();
  };
  async function urlApiJson(response, action) {
    const text = await response.text();
    try { return JSON.parse(text); }
    catch (_) {
      if (/<!doctype|<html/i.test(text)) throw new Error(`${action}失敗：後端尚未載入 URL 管理功能，請重新啟動庫存系統。`);
      throw new Error(`${action}失敗：伺服器回傳格式不正確。`);
    }
  }

  function urlChangeErrorMessage(error) {
    const detail = String(error?.message || error || '').trim();
    if (/ego(?:-lite)?|nodejs.*process exited/i.test(detail)) {
      return '1688 商品頁暫時無法讀取，請稍後再按「檢查新連結」；Golden Table 尚未修改。';
    }
    return detail || '新連結檢查失敗；Golden Table 尚未修改。';
  }
  function updateSelectAllState() {
    const master = $('selectAllItems');
    if (!master) return;
    const checkboxes = [...document.querySelectorAll('#queue .select-item')];
    const selectedCount = checkboxes.filter(checkbox => checkbox.checked).length;
    master.checked = checkboxes.length > 0 && selectedCount === checkboxes.length;
    master.indeterminate = selectedCount > 0 && selectedCount < checkboxes.length;
    master.disabled = !checkboxes.length || state.batchBusy;
    master.setAttribute('aria-label', checkboxes.length
      ? `全選本頁型號（${selectedCount}/${checkboxes.length}）`
      : '全選本頁型號（目前沒有項目）');
  }
  function setCurrentPageSelection(selected) {
    document.querySelectorAll('#queue .card').forEach(cardElement => {
      const checkbox = cardElement.querySelector('.select-item');
      if (!checkbox) return;
      checkbox.checked = selected;
      const id = String(cardElement.dataset.id);
      if (selected) state.selectedIds.add(id);
      else state.selectedIds.delete(id);
    });
    updateSelectAllState();
  }

  function renderSummary(data) {
    const counts = data.counts || {};
    const tiles = [
      ['全部有 1688 URL 型號', data.urlModels],
      ['目前需要補貨', data.restockModels],
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
    if (source === 'openai' || source === 'grok' || source === 'deepseek' || source === 'gemini') {
      const decision = ai.decision === 'match' && selected
        ? `建議候選 #${candidates.indexOf(selected) + 1}（${selected.sku_name}${selected.second_name ? ` → ${selected.second_name}` : ''}）`
        : '暫不判定，保留候選供人工確認';
      const detail = [...evidence, ...warnings].join('；');
      const providerLabel = source === 'gemini' ? 'AI 初判' : source === 'deepseek' ? 'DeepSeek AI 初判' : source === 'grok' ? 'Grok AI 初判' : 'OpenAI 初判';
      return `<div class="ai-summary ai-openai"><strong>${providerLabel}</strong>：${esc(decision)}${esc(confidenceText)}${detail ? `<br>${displayText(detail)}` : ''}</div>`;
    }
    if (source === 'error' || /_(error)$/.test(source)) {
      const providerLabel = source.startsWith('gemini') ? 'AI' : source.startsWith('deepseek') ? 'DeepSeek' : source.startsWith('grok') ? 'Grok' : source.startsWith('openai') ? 'OpenAI' : 'AI';
      const forced = ai.force_match === true ? '強制最接近模式' : '初判';
      return `<div class="ai-summary ai-warning"><strong>${providerLabel} ${forced}未完成</strong>：${displayText(warnings.join('；') || 'API 呼叫失敗')}；請改用完整 SKU 清單人工確認。</div>`;
    }
    if (source === 'rules' && warnings.length) {
      const providerLabel = ai.provider === 'gemini' ? 'AI' : ai.provider === 'deepseek' ? 'DeepSeek' : ai.provider === 'grok' ? 'Grok' : 'AI';
      const fallback = ai.provider === 'gemini' || ai.provider === 'deepseek' || ai.provider === 'grok' || ai.fallback === 'rules' ? `${providerLabel} 初判失敗，已回退規則` : 'AI 初判未執行';
      return `<div class="ai-summary ai-warning"><strong>${fallback}</strong>：${displayText(warnings.join('；'))}；目前顯示規則候選。</div>`;
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
    if (item.has_url === false) {
      const sourceImage = item.modelImageUrl || item.productImageUrl || '';
      return `<article class="card missing-url-card" data-id="${esc(item.id)}" data-tier="red">
        <div class="source">${sourceImage ? `<img src="${esc(sourceImage)}" loading="lazy" alt="">` : '<div class="source-placeholder">無圖片</div>'}
          <div><h2>${esc(item.model_name || '未命名型號')}</h2><p>${esc(item.product_name || '')}</p><p>商品 ID：${esc(item.product_id)}　規格 ID：${esc(item.model_id)}</p><p>型號月銷量：<strong>${fmt(item.monthlySales)}</strong>　商品月銷量：<strong>${fmt(item.productMonthlySales)}</strong></p><span class="badge missing-url">沒有 URL</span></div>
        </div>
        <div class="missing-url-callout"><div><strong>尚未設定 1688 URL</strong><p>這個型號目前不能掃描 SKU 或進入採購。設定連結前會先預覽完整 SKU，確認後才會寫入。</p></div><button class="primary change-url-inline" type="button" data-url-change="true">設定連結</button></div>
      </article>`;
    }
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
    const canForceRerunAi = Boolean(item.snapshot_id) && String(item.status || '') !== 'discontinued';
    const canRerunAi = canForceRerunAi && String(item.status || '') !== 'approved';
    const forceAiButton = item.offer_id
      ? `<button class="primary rerun-ai-forced" data-action="rerun-ai-forced"${canForceRerunAi ? '' : ' disabled title="目前沒有可用快照，請先重新掃描此商品"'}>用現有 SKU 清單重判（AI 強制選最接近）</button>`
      : '';
    const manualHint = ['stale', 'suspected_discontinued', 'discontinued'].includes(String(item.status || ''))
      ? (item.status === 'suspected_discontinued'
        ? '掃描結果疑似商品已下架，但尚未經人工確認；請重新掃描，或確認後按「標記停售」。'
        : item.status === 'discontinued'
        ? '這筆是人工標記停售；若商品仍可供應，請選擇目前有效的完整 SKU，再按「核准選取 SKU」恢復 mapping。'
        : '這筆目前只有舊快照候選；請先重新掃描 1688，確認最新完整規格名稱後再核准。')
      : item.existing_sku_name
      ? '人工從完整 SKU 清單選擇的規格優先級最高；點擊下方清單載入後，按綠色按鈕核准即可更新目前 mapping。'
      : (candidates.length ? '候選僅供參考；點擊下方完整 SKU 清單即可載入並重新選擇。' : '先重新掃描此 1688 商品；也可以點擊下方清單載入完整 SKU 手動指定。');
    const manualTools = `<div class="manual-tools">
      <div class="manual-hint">${manualHint}</div>
      <div class="actions">${canRescan ? '<button class="primary rescan" data-action="rescan">重新掃描此商品</button>' : ''}${canRerunAi ? '<button class="primary rerun-ai" data-action="rerun-ai">用現有 SKU 清單重新判斷（規則→AI）</button>' : ''}${forceAiButton}</div>
      <div class="catalog-picker"><select class="catalog-select"><option value="">尚未載入；點擊後讀取完整 SKU</option></select></div>
    </div>`;
    const hasExistingName = Boolean(item.existing_sku_name || item.existing_second_name);
    const existingApproved = String(item.mapping_status || '') === 'approved';
    const existing = hasExistingName
      ? `<div class="existing-mapping${existingApproved ? '' : ' legacy'}"><strong>${existingApproved ? '現有 mapping' : '舊名稱紀錄'}</strong><br>${esc(cleanLegacyName(item.existing_sku_name || '—'))}${item.existing_second_name ? ` → ${esc(cleanLegacyName(item.existing_second_name))}` : ''}<br><small>SKU ID（輔助）：${esc(item.existing_sku_id || '—')}</small><br><span>${esc(item.mapping_status || (existingApproved ? 'approved' : 'missing'))}</span></div>`
      : '<div class="existing-mapping empty-mapping">目前沒有正式 mapping</div>';
    return `<article class="card tier-card-${esc(item.review_tier)}" data-id="${esc(item.id)}" data-tier="${esc(item.review_tier)}">
      <label class="select-row"><input type="checkbox" class="select-item"> 批次處理</label>
      <div class="source">${sourceImage ? `<img src="${esc(sourceImage)}" loading="lazy" alt="">` : '<div class="source-placeholder">無圖片</div>'}
        <div><h2>${esc(item.model_name || '未命名型號')}</h2><p>${esc(item.product_name || '')}</p><p>商品 ID：${esc(item.product_id)}　規格 ID：${esc(item.model_id)}</p><p>即時庫存：<strong>${item.liveInventoryAvailable ? fmt(item.currentStock) : '尚未更新'}</strong>　建議補貨：<strong>${item.liveInventoryAvailable ? fmt(item.restockQty) : '尚未更新'}</strong></p><p>型號月銷量：<strong>${item.liveInventoryAvailable ? fmt(item.monthlySales) : '尚未更新'}</strong>　商品月銷量：<strong>${item.liveInventoryAvailable ? fmt(item.productMonthlySales) : '尚未更新'}</strong></p><span class="badge ${esc(item.status)}">${esc(item.status === 'suspected_discontinued' ? '疑似下架' : item.status)}</span><span class="tier-badge tier-${esc(item.review_tier)}">${esc(item.review_tier === 'green' ? '綠色：唯一精確' : item.review_tier === 'yellow' ? '黃色：人工比較' : item.review_tier === 'red' && ['stale', 'suspected_discontinued'].includes(item.status) ? '紅色：需重新掃描' : item.review_tier === 'red' ? '紅色：阻擋' : '已核准')}</span>${item.offer_id ? `<a class="open-1688" href="${esc(item.product_url || `https://detail.1688.com/offer/${item.offer_id}.html`)}" target="_blank" rel="noopener">開啟 1688 ↗</a>` : ''}<button class="change-url-inline" type="button" data-url-change="true">更換連結</button>${existing}</div></div>
      <div><div class="candidates">${candidates.length ? candidates.map((candidate, index) => candidateCard(item, candidate, index + 1)).join('') : '<div class="reason">尚未取得可通過規則的 SKU 候選；不代表 1688 沒有這個 SKU。</div>'}</div>${aiSummary(item, candidates)}${manualTools}
      <div class="reason"><strong>${displayText(item.review_reason || '等待人工確認')}</strong>${(ai.evidence || []).length ? `<br>${displayText(ai.evidence.join('；'))}` : ''}${evidence.error ? `<br>${displayText(evidence.error)}` : ''}</div>
      <div class="actions"><button class="approve" data-action="approve">核准選取 SKU</button><button data-action="defer">稍後處理</button><button data-action="no_match">標記無匹配</button><button data-action="discontinued">標記停售</button></div></div></article>`;
  }

  async function loadSummary() {
    const response = await fetch(`/api/sku-mapping/summary?_=${Date.now()}`, { cache: 'no-store' });
    const data = await response.json();
    if (!response.ok || data.status !== 'success') throw new Error(data.message || '摘要載入失敗');
    renderSummary(data);
    return data;
  }

  async function loadQueue() {
    const params = new URLSearchParams({ status: $('status').value, tier: $('tier').value, urlPresence: $('urlPresence').value, restockOnly: String($('restockOnly').checked), query: $('query').value, page: String(state.page), pageSize: String(state.pageSize), _: String(Date.now()) });
    const response = await fetch(`/api/sku-mapping/queue?${params}`, { cache: 'no-store' });
    const data = await response.json();
    if (!response.ok || data.status !== 'success') throw new Error(data.message || '待審核清單載入失敗');
    state.items = data.items || [];
    state.total = Number(data.total || 0);
    state.inventorySource = data.inventorySource || null;
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
    updateSelectAllState();
    $('empty').hidden = state.items.length > 0;
    renderPagination();
  }

  async function refreshLatestData(maxAttempts = 3) {
    let error = null;
    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      try {
        await Promise.all([loadSummary(), loadQueue()]);
        return { ok: true, error: null };
      } catch (currentError) {
        error = currentError;
        if (attempt < maxAttempts) await new Promise(resolve => setTimeout(resolve, 800 * attempt));
      }
    }
    return { ok: false, error };
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
        operationStatus('偵測到尚未完成的工作，正在接回最新進度。', 'running', {
          title: latest.scope === 'url_health_visible' ? '連結健康檢查' : latest.scope === 'visible_page' ? '本頁 1688 掃描' : latest.scope === 'existing_snapshots' ? '現有 SKU 清單重判' : '1688 SKU 掃描',
          completed: latest.completed,
          total: latest.total,
        });
        if (latest.scope === 'url_health_visible') pollUrlHealthJob(latestJobId);
        else pollJob(latestJobId);
      } else if (latestJobId && ['completed', 'error', 'waiting_for_login'].includes(String(latest.status || '')) && state.latestJobShown !== latestJobId) {
        const updatedAt = Number(latest.updated_at || latest.updatedAt || 0);
        if (!updatedAt || (Date.now() / 1000) - updatedAt < 600) {
          state.latestJobShown = latestJobId;
          if (latest.scope === 'url_health_visible') {
            pollUrlHealthJob(latestJobId);
            message('偵測到最近的連結健康檢查，正在載入最新結果。', 'success');
            return true;
          }
          operationDone(
            latest.status === 'completed' ? `${latest.message || '最近一項背景工作已完成'}；目前清單已重新載入。` : `${latest.message || '最近一項背景工作未完成'}，請查看提示後重試。`,
            latest.status === 'completed' ? 'success' : 'error',
            { title: latest.status === 'completed' ? '最近背景工作已完成' : '最近背景工作需要注意' },
          );
        }
      }
      if ($('restockOnly').checked && state.inventorySource && !state.inventorySource.available) {
        message(`${state.inventorySource.message}；目前沒有使用 Golden Table 的舊補貨數字。`, 'error');
      } else {
        message(state.activeJobId ? `已載入 ${state.items.length} 筆 mapping；背景工作 ${latest.completed || 0}/${latest.total || 0} 執行中。` : `已載入 ${state.items.length} 筆 mapping。`, 'success');
      }
      return true;
    }
    catch (error) { message(error.message, 'error'); return false; }
  }

  function batchRemaining(rows, action) {
    // In the default 待處理 view, completed, no-match, and deferred rows must
    // leave the queue.  This guard makes a stale/failed refresh visible instead
    // of silently leaving the old card on screen and making a completed batch
    // look as if it came back.
    if ($('status').value !== 'review' || !['approve', 'defer', 'no_match', 'discontinued'].includes(action)) return 0;
    const ids = new Set(rows.map(row => String(row.id)));
    return state.items.filter(row => ids.has(String(row.id))).length;
  }

  async function scan(scope, target = null, rebuild = true, targets = null) {
    if (state.loading) return;
    state.loading = true;
    setJobBusy(true);
    const visiblePage = scope === 'visible_page';
    const initialTotal = target ? 1 : visiblePage && Array.isArray(targets) ? targets.length : 0;
    operationStatus('正在建立背景工作，請稍候。', 'running', {
      title: target ? '單筆 1688 重新掃描' : visiblePage ? '本頁 1688 掃描' : '1688 SKU 掃描',
      completed: 0,
      total: initialTotal,
    });
    try {
      const body = { scope, force: target ? true : $('forceScan').checked, useAi: $('useAi').checked, rebuild };
      if (target) { body.productId = target.product_id; body.modelId = target.model_id; body.offerId = target.offer_id; }
      if (Array.isArray(targets)) body.targets = targets;
      const response = await fetch('/api/sku-mapping/scans', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
      const data = await response.json();
      if (!response.ok || data.status !== 'success') throw new Error(data.message || '掃描啟動失敗');
      state.activeJobId = data.jobId || '';
      const visiblePage = data.scope === 'visible_page';
      operationStatus(data.message || '工作已排入，等待開始。', 'running', {
        title: target ? '單筆 1688 重新掃描' : visiblePage ? '本頁 1688 掃描' : '1688 SKU 掃描',
        completed: data.completed,
        total: data.total || data.targetCount || initialTotal,
      });
      message(target
        ? `已啟動單筆重掃 ${data.jobId}，完成後會更新這個型號。`
        : visiblePage
        ? `已啟動 ${data.jobId}；只會掃描目前頁面顯示的 ${data.targetCount || targets?.length || 0} 筆型號。`
        : `已啟動 ${data.jobId}，掃描會在背景執行。`, 'success');
      pollJob(data.jobId);
    } catch (error) {
      try {
        const summary = await loadSummary();
        const latest = summary?.latestRun || {};
        const latestJobId = latest.job_id || latest.jobId || '';
        if (latestJobId && ['queued', 'running'].includes(String(latest.status || ''))) {
          state.activeJobId = latestJobId;
          operationStatus('已有背景工作執行中，已自動接回最新進度。', 'running', {
            title: latest.scope === 'visible_page' ? '本頁 1688 掃描' : '1688 SKU 掃描',
            completed: latest.completed,
            total: latest.total,
          });
          pollJob(latestJobId);
          return;
        }
      } catch (_) { /* 保留原始啟動錯誤 */ }
      operationDone(`背景工作未能啟動：${error.message}`, 'error');
    }
    finally { state.loading = false; if (!state.activeJobId) setJobBusy(false); }
  }

  function scanVisiblePage() {
    if (state.loading) return;
    const targets = [];
    const seen = new Set();
    state.items.forEach(item => {
      const productId = String(item.product_id || '').trim();
      const modelId = String(item.model_id || '').trim();
      if (item.has_url === false || !productId || !modelId) return;
      const key = `${productId}\u0000${modelId}`;
      if (seen.has(key)) return;
      seen.add(key);
      targets.push({productId, modelId});
    });
    if (!targets.length) {
      message('目前頁面沒有可掃描的型號；請先載入或搜尋清單。', 'error');
      return;
    }
    scan('visible_page', null, true, targets);
  }

  async function reanalyzeExisting(aiOnly = false) {
    if (state.loading) return;
    const targets = state.items.filter(item => item.has_url !== false).map(item => ({productId: item.product_id, modelId: item.model_id})).filter(item => item.productId && item.modelId);
    if (!targets.length) { message('目前畫面沒有可重判的型號；請先載入或搜尋清單。', 'error'); return; }
    state.loading = true;
    operationStatus('正在建立背景工作，請稍候。', 'running', {
      title: aiOnly ? 'AI 強制最接近重判' : '現有快照規則→AI 重判',
      completed: 0,
      total: targets.length,
    });
    const button = $(aiOnly ? 'reanalyzeExistingAi' : 'reanalyzeExisting');
    if (button) button.disabled = true;
    try {
      const response = await fetch('/api/sku-mapping/reanalyze-existing', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({useAi: aiOnly ? true : $('useAi').checked, rebuild: true, aiOnly, targets}),
      });
      const data = await response.json();
      if (!response.ok || data.status !== 'success') throw new Error(data.message || '現有快照重判啟動失敗');
      state.activeJobId = data.jobId || '';
      setJobBusy(true);
      operationStatus(data.message || '工作已排入，等待開始。', 'running', {
        title: aiOnly ? 'AI 強制最接近重判' : '現有快照規則→AI 重判',
        completed: data.completed,
        total: data.total || data.targetCount || targets.length,
      });
      message(aiOnly
        ? `已啟動 ${data.jobId}；會把目前顯示的 ${targets.length} 筆型號之完整 SKU 清單交給 AI 強制選出最接近項目，不會開啟 1688。`
        : `已啟動 ${data.jobId}；只會使用目前顯示的 ${targets.length} 筆型號快照，先規則後 AI，不會開啟 1688。`, 'success');
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
      operationDone(`背景工作未能啟動：${error.message}`, 'error');
    }
    finally { state.loading = false; if (!state.activeJobId) { setJobBusy(false); if (button) button.disabled = false; } }
  }

  async function pollJob(jobId) {
    state.activeJobId = jobId;
    let data;
    try {
      const response = await fetch(`/api/sku-mapping/jobs/${encodeURIComponent(jobId)}`);
      data = await response.json();
      if (!response.ok) throw new Error(data.message || '背景工作狀態讀取失敗');
    } catch (error) {
      $('jobStatus').textContent = `背景工作 ${jobId}：暫時無法讀取進度，正在重試…`;
      operationStatus(`暫時無法讀取進度，系統會自動重試。工作編號：${jobId}`, 'running', { title: '背景工作仍在執行' });
      setJobBusy(true);
      setTimeout(() => pollJob(jobId), 3000);
      return;
    }
    const completed = Number(data.completed || 0);
    const total = Number(data.total || 0);
    const progress = total ? `${completed}/${total}` : `${completed}/準備中`;
    const aiOnly = data.aiOnly === true || /AI.*強制最接近/.test(String(data.message || ''));
    const visiblePage = data.scope === 'visible_page';
    $('jobStatus').textContent = uiText(`${data.status || ''} ${progress} ${data.message || ''}`);
    if (data.status === 'waiting_for_login') {
      state.activeJobId = '';
      setJobBusy(false);
      operationDone('1688 需要登入或人工驗證；背景工作已暫停，完成後請重新掃描。', 'error', { title: '背景工作已暫停' });
      return;
    }
    if (['completed','error'].includes(data.status)) {
      state.activeJobId = '';
      setJobBusy(false);
      const doneMessage = aiOnly
        ? (data.message || '現有快照 AI 強制最接近重判完成；未連線 1688，仍需人工核准。')
        : data.scope === 'existing_snapshots'
        ? (data.message || '現有快照規則→AI 重判完成；未連線 1688。')
        : visiblePage
        ? (data.message || `本頁 1688 掃描完成；只處理目前顯示的 ${data.targetCount || 0} 筆型號，仍需人工核准。`)
        : '掃描完成；先用規則判定，只有規則無候選的項目才使用 AI。';
      // The backend keeps a stable machine-readable error code in `error`
      // and the actionable provider detail in `message`.  Prefer the detail;
      // otherwise users only see "ai_provider_error" and cannot tell whether
      // the cause was balance, rate limit, or an invalid response.
      const failureCode = String(data.error || '').trim();
      const failureMessage = String(data.message || '').trim();
      const genericMessages = new Set(['現有快照重判失敗', 'SKU mapping 掃描失敗']);
      const failureDetail = failureMessage && failureMessage !== failureCode && !genericMessages.has(failureMessage)
        ? failureMessage
        : (failureCode || failureMessage || '請查看該筆狀態');
      if (data.status === 'error') {
        operationDone(`背景工作失敗：${failureDetail}`, 'error', { title: '背景工作執行失敗' });
        return;
      }
      operationStatus('背景工作已完成，正在重新讀取最新清單。', 'running', {
        title: '正在更新畫面',
        completed,
        total: total || completed,
      });
      const refreshResult = await refreshLatestData(3);
      if (!refreshResult.ok) {
        operationDone(`背景工作已完成，但畫面更新失敗：${refreshResult.error?.message || '請按「重新載入」'}。`, 'error', { title: '工作完成，但畫面尚未更新' });
        return;
      }
      state.latestJobShown = jobId;
      operationDone(`${doneMessage}；最新清單已更新。`, 'success', { title: '背景工作已完成' });
      return;
    }
    setJobBusy(true);
    operationStatus(data.message || '背景執行中', 'running', {
      title: aiOnly ? 'AI 強制最接近重判' : data.scope === 'existing_snapshots' ? '現有快照規則→AI 重判' : visiblePage ? '本頁 1688 掃描' : '1688 SKU 掃描',
      completed,
      total,
    });
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
      const refreshResult = await refreshLatestData(3);
      if (!refreshResult.ok) {
        operationDone(`資料已儲存，但畫面更新失敗：${refreshResult.error?.message || '請按「重新載入」'}。`, 'error', { title: '儲存完成，但畫面尚未更新' });
        return;
      }
      message(action === 'approve' ? 'SKU mapping 已儲存，清單已更新。' : 'mapping 狀態已更新。', 'success');
    } catch (error) { message(error.message, 'error'); }
    finally { if (triggerButton && document.body.contains(triggerButton)) triggerButton.disabled = false; }
  }

  async function rerunAi(cardElement, triggerButton, forceMatch = false) {
    const item = state.items.find(row => String(row.id) === String(cardElement.dataset.id));
    if (!item || !item.snapshot_id) { message('目前沒有可用 SKU 快照，請先重新掃描此商品。', 'error'); return; }
    if (triggerButton) triggerButton.disabled = true;
    operationStatus(forceMatch ? '正在重新判斷：AI 會從完整 SKU 清單強制選最接近項目…' : '正在重新判斷：先跑規則，無規則候選時才呼叫 AI…', 'running');
    message(forceMatch ? '正在重新判斷：AI 會強制選出最接近候選…' : '正在重新判斷：先跑規則，無候選時才呼叫 AI…');
    try {
      const response = await fetch('/api/sku-mapping/ai-reviews', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({productId: item.product_id, modelId: item.model_id, forceMatch}),
      });
      const data = await response.json();
      if (!response.ok || data.status !== 'success') throw new Error(data.message || 'AI 初判失敗');
      const refreshResult = await refreshLatestData(3);
      if (!refreshResult.ok) {
        operationDone(`重新判斷已完成，但畫面更新失敗：${refreshResult.error?.message || '請按「重新載入」'}。`, 'error', { title: '工作完成，但畫面尚未更新' });
        return;
      }
      const doneMessage = forceMatch
        ? 'AI 強制最接近重判完成；已從完整 SKU 清單選出最接近候選，仍需人工核准。'
        : data.usedAi === false
        ? '規則重判完成；已有規則候選，因此未呼叫 AI。仍需人工核准。'
        : `AI 初判完成；已保留最多 ${data.candidateCount || 4} 張候選卡，結果已更新。仍需人工核准。`;
      operationDone(`${doneMessage}；最新清單已更新。`, 'success');
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

  function switchWorkbench(mode) {
    const showUrls = mode === 'urls';
    $('skuReviewView').hidden = showUrls;
    $('urlManagerView').hidden = !showUrls;
    $('skuReviewTab').classList.toggle('active', !showUrls);
    $('urlManagerTab').classList.toggle('active', showUrls);
    $('skuReviewTab').setAttribute('aria-selected', String(!showUrls));
    $('urlManagerTab').setAttribute('aria-selected', String(showUrls));
    return showUrls ? loadUrlGroups() : Promise.resolve();
  }

  const urlStatusLabel = value => ({
    ok: '正常', pending: '待重新核准', stale: '待重新掃描',
    suspected_discontinued: '疑似失效', missing: '缺少 URL',
  }[value] || value || '未知');

  const urlLinkStatusLabel = (group) => {
    if (!group?.productUrl) return '缺少 URL';
    if (group.linkCheckExpired) return '結果已過期';
    return ({
      valid: '正常連結', invalid: '確認失效', unchecked: '尚未檢查',
      needs_attention: '需要登入／驗證', error: '檢查失敗', missing: '缺少 URL',
    }[group.linkStatus] || '尚未檢查');
  };

  function renderUrlHealthMetrics(data) {
    const node = $('urlHealthMetrics');
    if (!node) return;
    const counts = data?.linkCounts || {};
    const tiles = [
      ['不同連結', data?.uniqueLinkTotal || 0],
      ['確認失效', counts.invalid || 0],
      ['正常連結', counts.valid || 0],
      ['尚未檢查', counts.unchecked || 0],
      ['需要處理', (counts.needs_attention || 0) + (counts.error || 0)],
      ['結果已過期', counts.expired || 0],
    ];
    node.innerHTML = tiles.map(([label, value]) => `<div class="metric"><strong>${fmt(value)}</strong><span>${esc(label)}</span></div>`).join('');
    node.hidden = false;
  }

  function formatCheckedAt(value) {
    const timestamp = Number(value || 0);
    if (!timestamp) return '尚未檢查';
    return new Date(timestamp * 1000).toLocaleString('zh-TW', {hour12: false});
  }

  function renderUrlGroups() {
    const products = new Map();
    urlState.groups.forEach(group => {
      const productId = String(group.productId || '');
      if (!products.has(productId)) products.set(productId, {...group, links: []});
      products.get(productId).links.push(group);
    });
    $('urlGroupList').innerHTML = [...products.values()].map(product => `
      <article class="url-group-card" data-product="${esc(product.productId)}">
        <div class="url-group-product">
          ${product.productImageUrl ? `<img src="${esc(product.productImageUrl)}" loading="lazy" alt="">` : '<div class="source-placeholder">無圖片</div>'}
          <div><h3>${esc(product.productName || '未命名商品')}</h3><small>商品 ID：${esc(product.productId)}</small><small>商品月銷量：${fmt(product.productMonthlySales)}</small><small>1688 連結：${fmt(product.links.length)} 組</small></div>
        </div>
        <div class="url-group-links">
          ${product.links.map(group => `<section class="url-group-link-row" data-group="${esc(group.groupId)}">
            <div class="url-group-link">
              ${group.productUrl ? `<a href="${esc(group.productUrl)}" target="_blank" rel="noopener">${esc(group.productUrl)}</a>` : '<strong>目前未設定 1688 URL</strong>'}
              <div class="url-group-meta"><span>使用型號：<strong>${fmt(group.modelCount)}</strong> 個</span><span class="url-group-status ${esc(group.status)}">Mapping：${esc(urlStatusLabel(group.status))}</span><span class="url-group-status link-health ${esc(group.linkStatus || 'unchecked')} ${group.linkCheckExpired ? 'expired' : ''}">連結：${esc(urlLinkStatusLabel(group))}</span><small>最近檢查：${esc(formatCheckedAt(group.linkCheckedAt || group.lastCheckedAt))}</small>${group.linkReason ? `<small>原因：${esc(group.linkReason)}</small>` : ''}</div>
            </div>
            <div class="url-group-actions">
              ${group.productUrl ? `<a href="${esc(group.productUrl)}" target="_blank" rel="noopener">開啟 1688</a>` : ''}
              ${group.productUrl ? '<button type="button" class="check-url-health">重新檢查</button>' : ''}
              <button type="button" class="change-url-button">${group.productUrl ? '更換連結' : '設定連結'}</button>
            </div>
          </section>`).join('')}
        </div>
      </article>`).join('');
    $('urlGroupEmpty').hidden = urlState.groups.length > 0;
  }

  async function loadUrlGroups() {
    const requestId = ++urlState.loadRequest;
    $('urlManagerMessage').textContent = '正在載入 Golden Table URL 群組…';
    $('urlManagerMessage').className = 'message';
    try {
      const params = new URLSearchParams({query: $('urlGroupQuery').value, mappingStatus: $('urlGroupStatus').value, linkStatus: $('urlLinkStatus').value, _: String(Date.now())});
      const response = await fetch(`/api/sku-mapping/url-groups?${params}`, {cache: 'no-store'});
      const data = await urlApiJson(response, '載入 URL 群組');
      if (requestId !== urlState.loadRequest) return null;
      if (!response.ok || data.status !== 'success') throw new Error(data.message || 'URL 群組載入失敗');
      urlState.groups = data.groups || [];
      renderUrlHealthMetrics(data);
      renderUrlGroups();
      $('urlManagerMessage').textContent = `已載入 ${data.productTotal || 0} 個商品（${data.total || 0} 組 URL，共 ${data.uniqueLinkTotal || 0} 個不同連結）。`;
      return data;
    } catch (error) {
      if (requestId !== urlState.loadRequest) return null;
      $('urlManagerMessage').textContent = error.message;
      $('urlManagerMessage').className = 'message error';
      return null;
    }
  }

  function urlHealthTargets(group = null) {
    const rows = group ? [group] : urlState.groups;
    const targets = [];
    const seen = new Set();
    rows.forEach(row => {
      if (!row?.productUrl || !row.productId || !row.modelId) return;
      const key = String(row.offerId || row.productUrl);
      if (seen.has(key)) return;
      seen.add(key);
      targets.push({productId: row.productId, modelId: row.modelId});
    });
    return targets;
  }

  async function checkUrlHealth(group = null) {
    if (urlState.healthBusy) return;
    const targets = urlHealthTargets(group);
    if (!targets.length) {
      $('urlManagerMessage').textContent = group ? '這組資料沒有可檢查的 1688 URL。' : '目前清單沒有可檢查的 1688 URL。';
      $('urlManagerMessage').className = 'message error';
      return;
    }
    urlState.healthBusy = true;
    const button = $('checkUrlHealth');
    if (button) button.disabled = true;
    operationStatus(`正在建立連結健康檢查，會依不同 offer 去重；共 ${targets.length} 個連結。`, 'running', {title: group ? '單筆連結健康檢查' : '連結健康檢查', completed: 0, total: targets.length});
    try {
      const response = await fetch('/api/sku-mapping/url-health-checks', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({targets}),
      });
      const data = await response.json();
      if (!response.ok || data.status !== 'success') throw new Error(data.message || '連結健康檢查啟動失敗');
      urlState.healthJobId = data.jobId || '';
      operationStatus(data.message || '連結健康檢查已排入背景工作。', 'running', {title: group ? '單筆連結健康檢查' : '連結健康檢查', completed: data.completed || 0, total: data.total || data.targetCount || targets.length});
      $('urlManagerMessage').textContent = `已啟動 ${data.jobId}；只會檢查目前清單的 ${data.targetCount || targets.length} 個不同連結。`;
      $('urlManagerMessage').className = 'message success';
      pollUrlHealthJob(data.jobId);
    } catch (error) {
      urlState.healthBusy = false;
      if (button) button.disabled = false;
      operationDone(`連結健康檢查未能啟動：${error.message}`, 'error');
    }
  }

  async function pollUrlHealthJob(jobId) {
    urlState.healthJobId = jobId;
    try {
      const response = await fetch(`/api/sku-mapping/jobs/${encodeURIComponent(jobId)}`, {cache: 'no-store'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.message || '連結健康檢查狀態讀取失敗');
      const completed = Number(data.completed || 0);
      const total = Number(data.total || 0);
      if (data.status === 'waiting_for_login') {
        urlState.healthBusy = false;
        urlState.healthJobId = '';
        $('checkUrlHealth').disabled = false;
        operationDone('1688 需要登入或人工驗證；已保存已完成的連結結果，請處理後重新檢查。', 'error', {title: '連結檢查已暫停'});
        await loadUrlGroups();
        return;
      }
      if (['completed', 'error'].includes(String(data.status || ''))) {
        urlState.healthBusy = false;
        urlState.healthJobId = '';
        $('checkUrlHealth').disabled = false;
        if (data.status === 'error') {
          operationDone(`連結健康檢查失敗：${data.message || data.error || '請稍後重試'}`, 'error', {title: '連結檢查失敗'});
          return;
        }
        operationStatus('連結檢查完成，正在重新載入最新結果。', 'running', {title: '正在更新連結狀態', completed, total: total || completed});
        const refreshed = await loadUrlGroups();
        if (!refreshed) {
          operationDone('連結檢查已完成，但最新結果載入失敗；請按「重新載入」。', 'error', {title: '檢查完成但畫面尚未更新'});
          return;
        }
        operationDone(data.message || '連結健康檢查完成；最新結果已更新。', 'success', {title: '連結檢查完成'});
        return;
      }
      operationStatus(data.message || '連結健康檢查執行中…', 'running', {title: '連結健康檢查', completed, total});
      window.setTimeout(() => pollUrlHealthJob(jobId), 2500);
    } catch (error) {
      operationStatus(`暫時無法讀取連結檢查進度，系統會自動重試。工作編號：${jobId}`, 'running', {title: '連結檢查仍在執行'});
      window.setTimeout(() => pollUrlHealthJob(jobId), 3000);
    }
  }

  function openUrlChange(group) {
    if (!group) return;
    urlState.activeGroup = group;
    urlState.preview = null;
    const modal = $('urlChangeModal');
    $('urlChangeProduct').textContent = `${group.productName || '未命名商品'}｜${group.productId}`;
    $('urlChangeAffected').textContent = `同商品、同舊連結的 ${group.modelCount || 1} 個型號`;
    $('urlChangeOldLink').textContent = group.productUrl || '目前未設定';
    if (group.productUrl) $('urlChangeOldLink').href = group.productUrl;
    else $('urlChangeOldLink').removeAttribute('href');
    $('urlChangeInput').value = '';
    $('urlChangeStatus').textContent = '';
    $('urlChangeStatus').className = 'url-change-status';
    $('urlChangePreview').hidden = true;
    $('urlChangeRows').innerHTML = '';
    $('urlChangeSummary').textContent = '請先檢查新連結';
    $('commitUrlChange').disabled = true;
    $('commitUrlChange').textContent = '確認更新';
    $('clearUrlChange').hidden = !group.productUrl;
    modal.hidden = false;
    document.body.style.overflow = 'hidden';
    setTimeout(() => $('urlChangeInput').focus(), 0);
  }

  async function openUrlChangeFromItem(item) {
    let group = urlState.groups.find(row => String(row.productId) === String(item.product_id) && String(row.offerId || '') === String(item.offer_id || ''));
    if (!group) {
      try {
        const response = await fetch(`/api/sku-mapping/url-groups?query=${encodeURIComponent(item.product_id)}&status=all&_=${Date.now()}`, {cache: 'no-store'});
        const data = await urlApiJson(response, '載入此商品 URL');
        if (!response.ok || data.status !== 'success') throw new Error(data.message || 'URL 群組載入失敗');
        group = (data.groups || []).find(row => String(row.productId) === String(item.product_id) && String(row.offerId || '') === String(item.offer_id || ''));
      } catch (error) { message(error.message, 'error'); return; }
    }
    openUrlChange(group || {
      groupId: `${item.product_id}|||${item.offer_id || '__missing__'}`,
      productId: item.product_id, productName: item.product_name, modelId: item.model_id,
      productUrl: item.product_url || '', offerId: item.offer_id || '', modelCount: 1,
    });
  }

  function closeUrlChange() {
    if (urlState.busy) return;
    $('urlChangeModal').hidden = true;
    document.body.style.overflow = '';
    urlState.activeGroup = null;
    urlState.preview = null;
  }

  const catalogOption = candidate => {
    const name = [candidate.sku_name, candidate.second_name].filter(Boolean).join(' → ') || candidate.spec_text || '未命名規格';
    const meta = [`價格 ${candidate.price ?? '—'}`, `庫存 ${candidate.stock ?? '—'}`].join('｜');
    return `<option value="${esc(candidate.candidate_key || '')}" data-image="${esc(candidate.image_url || '')}">${esc(name)}｜${esc(meta)}</option>`;
  };

  function renderUrlChangePreview(data) {
    urlState.preview = data;
    const isClear = data.mode === 'clear';
    $('urlChangePreview').hidden = false;
    $('urlChangeNewProductName').textContent = isClear ? '清除目前 1688 商品連結' : (data.snapshot?.productName || '新 1688 商品');
    $('urlChangeNewMeta').textContent = isClear ? '所有選取型號將改為缺少 URL，並阻擋採購。' : `Offer ID：${data.snapshot?.offerId || '—'}｜SKU：${data.snapshot?.skuCount || 0} 個｜剛剛讀取`;
    $('urlChangeNewLink').hidden = isClear;
    if (!isClear) $('urlChangeNewLink').href = data.snapshot?.productUrl || $('urlChangeInput').value.trim();
    const catalog = data.catalog || [];
    $('urlChangeRows').innerHTML = (data.targets || []).map(target => {
      const status = isClear ? 'missing' : target.matchStatus;
      const label = isClear ? '清除後阻擋採購' : status === 'exact' ? '唯一精確，可核准' : status === 'ambiguous' ? '需要人工選擇' : '更新後會阻擋採購';
      const oldName = [target.existingSkuName, target.existingSecondName].filter(Boolean).join(' → ') || '未設定 SKU';
      const options = `<option value="">${status === 'missing' ? '找不到對應；更新後待處理' : '不核准；更新後待處理'}</option>` + catalog.map(catalogOption).join('');
      return `<div class="url-change-row is-${esc(status)}" data-model-id="${esc(target.modelId)}">
        <input class="url-target-check" type="checkbox" checked aria-label="更新 ${esc(target.modelName)}">
        <div class="url-change-model"><strong>${esc(target.modelName || '未命名型號')}</strong><small>${esc(target.modelId)}</small></div>
        <div class="url-change-old"><small>舊 mapping</small><strong>${esc(oldName)}</strong></div>
        ${isClear ? '<div>將清除 URL 與 SKU mapping</div>' : `<div class="url-new-choice"><img class="url-candidate-image" alt="新 SKU 圖片" hidden><select class="url-candidate-select" aria-label="選擇新 SKU">${options}</select></div>`}
        <span class="url-change-result ${esc(status)}">${esc(label)}</span>
      </div>`;
    }).join('');
    if (!isClear) {
      (data.targets || []).forEach(target => {
        const row = [...$('urlChangeRows').querySelectorAll('.url-change-row')].find(node => node.dataset.modelId === String(target.modelId));
        if (row && target.selectedCandidateKey) row.querySelector('.url-candidate-select').value = target.selectedCandidateKey;
        if (row) updateUrlCandidateImage(row);
      });
    }
    $('commitUrlChange').disabled = false;
    updateUrlChangeSummary();
  }

  function selectedUrlChangeModels() {
    return [...$('urlChangeRows').querySelectorAll('.url-change-row')].filter(row => row.querySelector('.url-target-check')?.checked).map(row => ({
      modelId: row.dataset.modelId || '', selected: true,
      candidateKey: row.querySelector('.url-candidate-select')?.value || '',
    }));
  }

  function updateUrlCandidateImage(row) {
    const option = row?.querySelector('.url-candidate-select')?.selectedOptions?.[0];
    const image = row?.querySelector('.url-candidate-image');
    if (!image) return;
    const url = option?.dataset?.image || '';
    image.hidden = !url;
    if (url) image.src = url;
    else image.removeAttribute('src');
  }

  function updateUrlChangeSummary() {
    const selected = selectedUrlChangeModels();
    const approved = selected.filter(row => row.candidateKey).length;
    const pending = selected.length - approved;
    const isClear = urlState.preview?.mode === 'clear';
    $('urlChangeSummary').textContent = isClear
      ? `將清除 ${selected.length} 個型號的 URL，全部改為缺少連結`
      : `將更新 ${selected.length} 個型號｜直接核准 ${approved} 個｜待處理 ${pending} 個`;
    $('commitUrlChange').disabled = !urlState.preview || urlState.busy || selected.length === 0;
    $('commitUrlChange').textContent = isClear ? `確認清除 ${selected.length} 個型號` : `確認更新 ${selected.length} 個型號`;
  }

  async function previewUrlChange(mode = 'replace') {
    const group = urlState.activeGroup;
    if (!group || urlState.busy) return;
    const newUrl = $('urlChangeInput').value.trim();
    if (mode === 'replace' && !newUrl) {
      $('urlChangeStatus').textContent = '請先貼上新的 1688 商品 URL。';
      $('urlChangeStatus').className = 'url-change-status error';
      return;
    }
    urlState.busy = true;
    $('previewUrlChange').disabled = true;
    $('commitUrlChange').disabled = true;
    $('urlChangeStatus').textContent = mode === 'clear' ? '正在準備清除範圍…' : '正在開啟新商品並讀取完整 SKU，請稍候…';
    $('urlChangeStatus').className = 'url-change-status';
    try {
      const response = await fetch('/api/sku-mapping/url-changes/preview', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({productId: group.productId, modelId: group.modelId, newUrl, mode}),
      });
      const data = await urlApiJson(response, '檢查新連結');
      if (!response.ok || data.status !== 'success') throw new Error(data.message || '新連結檢查失敗');
      renderUrlChangePreview(data);
      $('urlChangeStatus').textContent = mode === 'clear' ? '清除範圍已確認；請檢查型號後按下確認。' : '新連結讀取成功；請確認每個型號的新 SKU。';
      $('urlChangeStatus').className = 'url-change-status success';
    } catch (error) {
      urlState.preview = null;
      $('urlChangePreview').hidden = true;
      $('urlChangeStatus').textContent = urlChangeErrorMessage(error);
      $('urlChangeStatus').className = 'url-change-status error';
    } finally {
      urlState.busy = false;
      $('previewUrlChange').disabled = false;
      updateUrlChangeSummary();
    }
  }

  async function commitUrlChange() {
    const preview = urlState.preview;
    const group = urlState.activeGroup;
    const models = selectedUrlChangeModels();
    if (!preview || !group || !models.length || urlState.busy) return;
    const isClear = preview.mode === 'clear';
    const approved = models.filter(row => row.candidateKey).length;
    const pending = models.length - approved;
    const prompt = isClear
      ? `確定清除 ${models.length} 個型號的 1688 URL？清除後全部會阻擋採購。`
      : `確定更新 ${models.length} 個型號？\n${approved} 個會直接核准，${pending} 個會進入待處理並阻擋採購。`;
    if (!window.confirm(prompt)) return;
    urlState.busy = true;
    updateUrlChangeSummary();
    $('previewUrlChange').disabled = true;
    $('clearUrlChange').disabled = true;
    $('urlChangeStatus').textContent = '正在同步 Golden Table、SKU mapping 與採購資料…';
    $('urlChangeStatus').className = 'url-change-status';
    try {
      const response = await fetch('/api/sku-mapping/url-changes/commit', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          productId: preview.productId, modelId: preview.modelId, sourceVersion: preview.sourceVersion,
          newUrl: $('urlChangeInput').value.trim(), snapshotFingerprint: preview.snapshot?.fingerprint || '',
          mode: preview.mode, models,
        }),
      });
      const data = await urlApiJson(response, '更新 URL');
      if (response.status === 409) throw new Error(data.message || '資料已更新，請重新預覽');
      if (!response.ok || data.status !== 'success') throw new Error(data.message || 'URL 更新失敗');
      urlState.busy = false;
      closeUrlChange();
      await Promise.all([loadUrlGroups(), reload()]);
      $('urlManagerMessage').className = 'message success';
      $('urlManagerMessage').innerHTML = `${esc(data.message || 'URL 已更新。')}${data.pendingCount ? ' <button type="button" id="showUrlPending">查看待處理項目</button>' : ''}`;
    } catch (error) {
      $('urlChangeStatus').textContent = error.message;
      $('urlChangeStatus').className = 'url-change-status error';
    } finally {
      urlState.busy = false;
      $('previewUrlChange').disabled = false;
      $('clearUrlChange').disabled = false;
      updateUrlChangeSummary();
    }
  }

  $('queue').addEventListener('click', event => {
    const urlButton = event.target.closest('[data-url-change="true"]');
    if (urlButton) {
      const cardElement = urlButton.closest('.card');
      const item = state.items.find(row => String(row.id) === String(cardElement?.dataset.id));
      if (item) openUrlChangeFromItem(item);
      return;
    }
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
    if (button.dataset.action === 'rerun-ai') return rerunAi(cardElement, button, false);
    if (button.dataset.action === 'rerun-ai-forced') return rerunAi(cardElement, button, true);
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
    updateSelectAllState();
  });
  $('selectAllItems').addEventListener('change', event => {
    setCurrentPageSelection(event.target.checked);
  });
  $('scanAll').addEventListener('click', () => scan('all', null, true));
  $('scanVisiblePage').addEventListener('click', scanVisiblePage);
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
    updateSelectAllState();
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
  ['status', 'tier', 'urlPresence', 'restockOnly'].forEach(id => $(id).addEventListener('change', () => { clearSelections(); state.page = 1; reload(); }));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !$('urlChangeModal').hidden) {
      closeUrlChange();
      return;
    }
    const active = document.querySelector('.card:hover');
    if (!active || ['INPUT','SELECT','TEXTAREA'].includes(document.activeElement.tagName)) return;
    if (/^[1-5]$/.test(event.key)) active.querySelectorAll('.candidate')[Number(event.key) - 1]?.click();
    if (event.key === 'Enter') active.querySelector('[data-action="approve"]')?.click();
  });
  $('skuReviewTab').addEventListener('click', () => switchWorkbench('sku'));
  $('urlManagerTab').addEventListener('click', () => switchWorkbench('urls'));
  $('reloadUrlGroups').addEventListener('click', loadUrlGroups);
  $('urlGroupStatus').addEventListener('change', loadUrlGroups);
  $('urlLinkStatus').addEventListener('change', loadUrlGroups);
  $('checkUrlHealth').addEventListener('click', () => checkUrlHealth());
  let urlQueryTimer;
  $('urlGroupQuery').addEventListener('input', () => {
    clearTimeout(urlQueryTimer);
    urlQueryTimer = setTimeout(loadUrlGroups, 300);
  });
  $('urlGroupList').addEventListener('click', event => {
    const healthButton = event.target.closest('.check-url-health');
    if (healthButton) {
      const linkRow = healthButton.closest('.url-group-link-row');
      const group = urlState.groups.find(row => String(row.groupId) === String(linkRow?.dataset.group));
      if (group) checkUrlHealth(group);
      return;
    }
    const button = event.target.closest('.change-url-button');
    if (!button) return;
    const linkRow = button.closest('.url-group-link-row');
    const group = urlState.groups.find(row => String(row.groupId) === String(linkRow?.dataset.group));
    openUrlChange(group);
  });
  $('urlManagerMessage').addEventListener('click', event => {
    if (event.target.id !== 'showUrlPending') return;
    $('status').value = 'pending';
    switchWorkbench('sku');
    clearSelections();
    state.page = 1;
    reload();
  });
  $('urlChangeModal').addEventListener('click', event => {
    if (event.target.dataset.urlClose === 'true') closeUrlChange();
  });
  $('previewUrlChange').addEventListener('click', () => previewUrlChange('replace'));
  $('clearUrlChange').addEventListener('click', () => {
    if (!window.confirm('要準備清除這組舊連結嗎？下一步仍會列出受影響型號供你最後確認。')) return;
    previewUrlChange('clear');
  });
  $('commitUrlChange').addEventListener('click', commitUrlChange);
  $('urlChangeRows').addEventListener('change', event => {
    updateUrlCandidateImage(event.target.closest('.url-change-row'));
    updateUrlChangeSummary();
  });
  $('urlChangeInput').addEventListener('input', () => {
    urlState.preview = null;
    $('urlChangePreview').hidden = true;
    $('urlChangeSummary').textContent = 'URL 已變更，請重新檢查新連結';
    $('commitUrlChange').disabled = true;
  });
  $('operationStatus').addEventListener('click', event => {
    if (event.target.closest('.operation-close')) dismissOperationStatus();
  });
  reload();
  const initialParams = new URLSearchParams(window.location.search);
  if (initialParams.get('mode') === 'urls') {
    switchWorkbench('urls').then(() => {
      const productId = initialParams.get('productId') || '';
      const modelId = initialParams.get('modelId') || '';
      const group = urlState.groups.find(row => String(row.productId) === productId && (!modelId || (row.models || []).some(model => String(model.modelId) === modelId)));
      if (!group) return;
      openUrlChange(group);
      const newUrl = initialParams.get('newUrl') || '';
      if (newUrl) $('urlChangeInput').value = newUrl;
    });
  }
})();
