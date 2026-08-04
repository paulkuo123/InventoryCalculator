document.addEventListener('DOMContentLoaded', () => {
    const state = {
        system: { writeEnabled: false, busy: false },
        order: null,
        receipt: null,
        lineStates: {},
        searchResults: {},
    };
    const activeStatuses = new Set(['reading_order', 'awaiting_login', 'applying']);

    const el = id => document.getElementById(id);
    const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
    }[char]));
    const intValue = (value, fallback = 0) => {
        const parsed = Number.parseInt(value, 10);
        return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
    };

    async function api(url, options = {}) {
        const response = await fetch(url, options);
        let data = {};
        try { data = await response.json(); } catch (_) { /* no-op */ }
        if (!response.ok) throw new Error(data.message || `請求失敗 (${response.status})`);
        return data;
    }

    function setJobStatus(message, kind = 'running') {
        const box = el('jobStatus');
        box.className = `job-status ${kind}`;
        box.textContent = message;
    }

    function processImageUrl(url) {
        let value = String(url || '').trim();
        if (!value || value === '未找到' || value === 'undefined') return '';
        if (value.startsWith('//')) value = `https:${value}`;
        if (!/^https?:\/\//i.test(value)) value = `https://${value}`;
        return value.split('?')[0];
    }

    function imageMarkup(url, kind, alt) {
        const source = processImageUrl(url);
        return `<span class="image-box ${kind}"><i class="fas fa-image"></i>${source
            ? `<img src="${escapeHtml(source)}" alt="${escapeHtml(alt)}" referrerpolicy="no-referrer" onerror="this.remove()">`
            : ''}</span>`;
    }

    async function loadSystemStatus() {
        try {
            state.system = await api('/api/inbound/status');
            const box = el('systemStatus');
            box.className = `status-strip ${state.system.writeEnabled ? 'ready' : 'readonly'}`;
            box.innerHTML = `
                <span class="status-dot"></span>
                <div>
                    <strong>${state.system.writeEnabled ? '預覽後寫入已啟用' : '安全唯讀模式'}</strong>
                    <p>${escapeHtml(state.system.message)}</p>
                </div>`;
            if (state.order) renderOrder();
            if (state.receipt) renderReceipt();
        } catch (error) {
            el('systemStatus').innerHTML = `<span class="status-dot"></span><div><strong>狀態讀取失敗</strong><p>${escapeHtml(error.message)}</p></div>`;
        }
    }

    async function pollJob(jobId, onDone) {
        while (true) {
            const job = await api(`/api/inbound/jobs/${jobId}`);
            setJobStatus(job.message || `工作狀態：${job.status}`, activeStatuses.has(job.status) ? 'running' : 'done');
            if (!activeStatuses.has(job.status)) {
                await onDone(job);
                return job;
            }
            await new Promise(resolve => setTimeout(resolve, 1300));
        }
    }

    async function importOrder() {
        const reference = el('orderReference').value.trim();
        if (!reference) {
            setJobStatus('請先輸入 1688 訂單編號或連結', 'error');
            return;
        }
        const button = el('importOrderButton');
        button.disabled = true;
        try {
            const job = await api('/api/inbound/orders/import', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ reference })
            });
            setJobStatus(job.message, 'running');
            await pollJob(job.jobId, async result => {
                if (!result.result?.order) throw new Error(result.message || '訂單匯入失敗');
                initializeOrder(result.result.order);
                setJobStatus(result.message, result.status === 'completed' ? 'done' : 'warning');
            });
        } catch (error) {
            setJobStatus(error.message, 'error');
        } finally {
            button.disabled = false;
            loadSystemStatus();
        }
    }

    function initializeOrder(order) {
        state.order = order;
        state.receipt = null;
        state.lineStates = {};
        state.searchResults = {};
        order.lines.forEach(line => {
            const remaining = intValue(line.remaining_qty);
            const candidate = line.mappingStatus === 'exact' ? line.candidates[0] : null;
            state.lineStates[line.id] = {
                received: remaining,
                damaged: 0,
                shopeeQty: remaining,
                allocations: candidate ? [{ ...candidate, qty: remaining }] : [],
            };
        });
        el('receiptPanel').classList.add('hidden');
        renderOrder();
    }

    function receiptStatusLabel(status) {
        return ({
            draft: '尚未預覽',
            preview_ready: '預覽完成，等待確認',
            partial_failed: '蝦皮更新失敗，可安全重試',
            manual_review: '需人工確認',
            applying: '先前執行中斷',
        })[status] || status;
    }

    function renderOpenReceipts() {
        const box = el('openReceipts');
        const receipts = state.order?.open_receipts || [];
        if (!receipts.length) {
            box.classList.add('hidden');
            box.innerHTML = '';
            return;
        }
        box.classList.remove('hidden');
        box.innerHTML = `
            <strong><i class="fas fa-rotate-right"></i> 此訂單有 ${receipts.length} 張尚未完成的到貨單</strong>
            <div class="open-receipt-list">${receipts.map(receipt => {
                const failed = intValue(receipt.failed_update_count);
                const pending = intValue(receipt.pending_update_count);
                const detail = receipt.can_resume
                    ? `${pending} 個蝦皮規格待處理${failed ? `，其中 ${failed} 個上次明確失敗` : ''}`
                    : '先前可能已送出，為避免重複加庫存，僅能查看紀錄';
                const action = receipt.can_resume
                    ? `<button class="button secondary compact resume-receipt" data-receipt-id="${receipt.id}">
                           <i class="fas fa-rotate-right"></i> ${receipt.status === 'preview_ready' ? '繼續確認' : '載入並重新預覽'}
                       </button>`
                    : `<button class="button compact inspect-receipt" data-receipt-id="${receipt.id}">
                           <i class="fas fa-magnifying-glass"></i> 查看紀錄
                       </button>`;
                return `<div class="open-receipt-card ${receipt.can_resume ? '' : 'blocked'}">
                    <div class="open-receipt-copy">
                        <strong>到貨單 #${receipt.id}</strong>
                        <span>${escapeHtml(receiptStatusLabel(receipt.status))}</span>
                        <small>實收 ${intValue(receipt.total_received_qty)}｜原訂增加蝦皮 ${intValue(receipt.total_shopee_qty)}｜${escapeHtml(detail)}</small>
                    </div>
                    ${action}
                </div>`;
            }).join('')}</div>`;
        box.querySelectorAll('.resume-receipt').forEach(button => {
            button.addEventListener('click', () => resumeReceipt(Number(button.dataset.receiptId), button));
        });
        box.querySelectorAll('.inspect-receipt').forEach(button => {
            button.addEventListener('click', () => loadReceipt(Number(button.dataset.receiptId), button));
        });
    }

    async function loadReceipt(receiptId, button = null) {
        if (button) button.disabled = true;
        try {
            state.receipt = await api(`/api/inbound/receipts/${receiptId}`);
            renderReceipt();
            el('receiptPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
            return state.receipt;
        } catch (error) {
            setJobStatus(error.message, 'error');
            throw error;
        } finally {
            if (button) button.disabled = false;
        }
    }

    async function resumeReceipt(receiptId, button) {
        try {
            const receipt = await loadReceipt(receiptId, button);
            if (receipt.status !== 'preview_ready') await previewReceipt();
        } catch (_) { /* 錯誤已顯示於工作狀態 */ }
    }

    async function refreshOpenReceipts() {
        if (!state.order?.id) return;
        try {
            const order = await api(`/api/inbound/orders/${state.order.id}`);
            state.order.open_receipts = order.open_receipts || [];
            renderOpenReceipts();
        } catch (_) { /* 不影響目前到貨單的處理結果 */ }
    }

    function candidateValue(item) { return `${item.productId}|||${item.modelId}`; }
    function findCandidate(line, value) {
        return (line.candidates || []).find(item => candidateValue(item) === value);
    }
    function targetForLine(line) {
        const current = state.lineStates[line.id];
        if (current.allocations.length === 1) return current.allocations[0];
        if (line.mappingStatus === 'exact' && line.candidates?.length === 1) return line.candidates[0];
        return current.allocations[0] || null;
    }

    function groupedOrderLines() {
        const groups = new Map();
        state.order.lines.forEach(line => {
            const target = targetForLine(line);
            const key = target?.productId ? `shopee:${target.productId}` : `unmapped:${line.alibaba_offer_id || line.id}`;
            if (!groups.has(key)) {
                groups.set(key, {
                    key,
                    target,
                    productId: target?.productId || '',
                    productName: target?.productName || line.alibaba_product_name || '1688 商品',
                    productImage: target?.productImage || '',
                    lines: [],
                });
            }
            const group = groups.get(key);
            group.lines.push(line);
            if (!group.productImage && target?.productImage) group.productImage = target.productImage;
        });
        return [...groups.values()];
    }

    function renderMappingEditor(line) {
        const current = state.lineStates[line.id];
        const results = state.searchResults[line.id] || [];
        const candidates = line.candidates || [];
        const candidateOptions = candidates.map(item =>
            `<option value="${escapeHtml(candidateValue(item))}">${escapeHtml(item.productName)}｜${escapeHtml(item.modelName)} (${escapeHtml(item.productId)}/${escapeHtml(item.modelId)})</option>`
        ).join('');
        const searchOptions = results.map(item =>
            `<option value="${escapeHtml(candidateValue(item))}">${escapeHtml(item.productName)}｜${escapeHtml(item.modelName)} (${escapeHtml(item.productId)}/${escapeHtml(item.modelId)})</option>`
        ).join('');
        const allocationChips = current.allocations.map((allocation, index) => `
            <span class="allocation-chip" data-allocation-index="${index}">
                <span>${escapeHtml(allocation.productName)} · ${escapeHtml(allocation.modelName)}</span>
                <input class="allocation-qty" type="number" min="0" value="${intValue(allocation.qty)}" title="分配數量">
                <button class="remove-allocation" title="移除對照"><i class="fas fa-times"></i></button>
            </span>`).join('');
        const needsAttention = line.mappingStatus !== 'exact' || current.allocations.length !== 1;
        return `
            <tr class="mapping-editor-row">
                <td colspan="7" class="mapping-editor">
                    <details ${needsAttention ? 'open' : ''}>
                        <summary>${needsAttention ? '請選擇或分配蝦皮規格' : '變更對照／分配到其他蝦皮刊登'}</summary>
                        ${candidateOptions ? `<div class="mapping-editor-body"><select class="candidate-select"><option value="">從既有候選選擇</option>${candidateOptions}</select><button class="button secondary compact add-candidate">套用對照</button></div>` : ''}
                        <div class="mapping-editor-body"><input class="model-search" placeholder="搜尋商品名、規格名或 ID"><button class="button secondary compact search-model">搜尋</button></div>
                        ${searchOptions ? `<div class="mapping-editor-body search-result-row"><select class="search-result"><option value="">選擇搜尋結果</option>${searchOptions}</select><button class="button secondary compact add-search-result">套用對照</button></div>` : ''}
                        <div class="allocation-list">${allocationChips || '<span class="empty-copy">尚未選擇蝦皮規格</span>'}</div>
                    </details>
                </td>
            </tr>`;
    }

    function renderVariantRow(line) {
        const current = state.lineStates[line.id];
        const target = targetForLine(line);
        const spec = [line.alibaba_sku_name, line.alibaba_sku_second_name].filter(Boolean).join(' / ') || '未標示規格';
        const badgeText = line.mappingStatus === 'exact' ? '已對照' : line.mappingStatus === 'ambiguous' ? '多個候選' : '待對照';
        const cachedStock = target?.cachedStock;
        const canInbound = line.remaining_qty > 0 && (current.shopeeQty === 0 || current.allocations.length > 0);
        return `
            <tr class="variant-row" data-line-id="${line.id}">
                <td>
                    <div class="variant-cell">
                        ${imageMarkup(target?.modelImage || target?.productImage, 'model', target?.modelName || spec)}
                        <div class="variant-copy">
                            <strong>${escapeHtml(target?.modelName || '尚未選擇蝦皮規格')}</strong>
                            <span>${target ? `規格 ID ${escapeHtml(target.modelId)}` : '完成對照後才能入庫'}</span>
                            ${cachedStock !== null && cachedStock !== undefined && cachedStock !== '' ? `<small class="cached-stock">Golden Table 庫存 ${escapeHtml(cachedStock)}</small>` : ''}
                        </div>
                    </div>
                </td>
                <td><div class="source-spec"><strong>${escapeHtml(spec)}</strong><span>offer ${escapeHtml(line.alibaba_offer_id || '—')}</span></div></td>
                <td><div class="quantity-stack"><strong>${line.ordered_qty}</strong><span>已收 ${line.cumulative_received_qty} · 未收 ${line.remaining_qty}</span></div></td>
                <td><div class="qty-field"><label>實收</label><input class="received-qty" type="number" min="0" max="${line.remaining_qty}" value="${current.received}" ${line.remaining_qty === 0 ? 'disabled' : ''}></div></td>
                <td><div class="qty-field"><label>不良品</label><input class="damaged-qty" type="number" min="0" value="${current.damaged}" ${line.remaining_qty === 0 ? 'disabled' : ''}></div></td>
                <td><div class="qty-field"><label>蝦皮加入</label><input class="shopee-qty" type="number" min="0" value="${current.shopeeQty}" ${line.remaining_qty === 0 ? 'disabled' : ''}></div></td>
                <td class="row-actions">
                    <span class="mapping-badge ${escapeHtml(line.mappingStatus)}">${badgeText}</span><br>
                    <button class="button success compact inbound-one" ${canInbound ? '' : 'disabled'} title="${canInbound ? '建立此規格入庫預覽' : '請先完成對照'}"><i class="fas fa-plus"></i> 此規格入庫</button>
                </td>
            </tr>
            ${renderMappingEditor(line)}`;
    }

    function renderOrder() {
        const order = state.order;
        if (!order) return;
        el('orderPanel').classList.remove('hidden');
        const mappedCount = order.lines.filter(line => state.lineStates[line.id].allocations.length > 0).length;
        el('orderSummary').textContent = `1688 訂單 ${order.alibaba_order_id}｜${order.lines.length} 個規格｜已對照 ${mappedCount} 個`;
        const reviewCount = order.lines.filter(line => !state.lineStates[line.id].allocations.length).length;
        const notice = el('mappingNotice');
        notice.classList.toggle('hidden', reviewCount === 0);
        notice.textContent = reviewCount ? `${reviewCount} 個規格尚未對照，請先在對應列展開「選擇蝦皮規格」。` : '';
        renderOpenReceipts();

        el('orderLines').innerHTML = groupedOrderLines().map(group => {
            const lineIds = group.lines.map(line => line.id);
            const totalQty = group.lines.reduce((sum, line) => sum + intValue(state.lineStates[line.id].shopeeQty), 0);
            const ready = group.lines.some(line => state.lineStates[line.id].received > 0)
                && group.lines.every(line => state.lineStates[line.id].shopeeQty === 0 || state.lineStates[line.id].allocations.length > 0);
            const uniqueOffers = new Set(group.lines.map(line => line.alibaba_offer_id).filter(Boolean));
            return `
                <article class="product-group" data-group-key="${escapeHtml(group.key)}">
                    <div class="product-group-header">
                        <div class="product-summary">
                            ${imageMarkup(group.productImage, 'product', group.productName)}
                            <div class="product-copy">
                                <h3>${escapeHtml(group.productName)}</h3>
                                <p>${group.productId ? `蝦皮商品 ID ${escapeHtml(group.productId)}` : '尚未找到蝦皮商品'}</p>
                                <div class="product-stats">
                                    <span class="mini-badge">${group.lines.length} 個規格</span>
                                    <span class="mini-badge qty">本次加入 ${totalQty}</span>
                                    ${uniqueOffers.size ? `<span class="mini-badge">1688 offer ${escapeHtml([...uniqueOffers].join(', '))}</span>` : ''}
                                </div>
                            </div>
                        </div>
                        <div class="product-actions">
                            <button class="button success inbound-group" data-line-ids="${lineIds.join(',')}" ${ready ? '' : 'disabled'}>
                                <i class="fas fa-boxes-stacked"></i> 此商品全部入庫
                            </button>
                            <small>${state.system.writeEnabled ? '預覽確認後，同商品只開一次編輯頁' : '安全模式：目前只建立預覽，不會改庫存'}</small>
                        </div>
                    </div>
                    <div class="variant-table-wrap">
                        <table class="variant-table">
                            <thead><tr><th>蝦皮規格</th><th>1688 規格</th><th>訂購／在途</th><th>本次實收</th><th>不良</th><th>蝦皮增加</th><th></th></tr></thead>
                            <tbody>${group.lines.map(renderVariantRow).join('')}</tbody>
                        </table>
                    </div>
                </article>`;
        }).join('');
        attachOrderEvents();
    }

    function addAllocation(lineId, item) {
        if (!item) return;
        const current = state.lineStates[lineId];
        if (current.allocations.some(allocation => allocation.productId === item.productId && allocation.modelId === item.modelId)) return;
        const alreadyAllocated = current.allocations.reduce((sum, allocation) => sum + intValue(allocation.qty), 0);
        current.allocations.push({ ...item, qty: Math.max(0, current.shopeeQty - alreadyAllocated) });
        renderOrder();
    }

    function attachOrderEvents() {
        document.querySelectorAll('.variant-row').forEach(row => {
            const lineId = Number(row.dataset.lineId);
            const line = state.order.lines.find(item => item.id === lineId);
            const current = state.lineStates[lineId];
            row.querySelector('.received-qty')?.addEventListener('change', event => {
                current.received = Math.min(intValue(event.target.value), intValue(line.remaining_qty));
                current.damaged = Math.min(current.damaged, current.received);
                current.shopeeQty = Math.max(0, current.received - current.damaged);
                if (current.allocations.length === 1) current.allocations[0].qty = current.shopeeQty;
                renderOrder();
            });
            row.querySelector('.damaged-qty')?.addEventListener('change', event => {
                current.damaged = Math.min(intValue(event.target.value), current.received);
                current.shopeeQty = Math.max(0, current.received - current.damaged);
                if (current.allocations.length === 1) current.allocations[0].qty = current.shopeeQty;
                renderOrder();
            });
            row.querySelector('.shopee-qty')?.addEventListener('change', event => {
                current.shopeeQty = intValue(event.target.value);
                if (current.allocations.length === 1) current.allocations[0].qty = current.shopeeQty;
                renderOrder();
            });
            row.querySelector('.inbound-one')?.addEventListener('click', event => {
                createReceipt([lineId], { autoPreview: true, autoApply: true, triggerButton: event.currentTarget });
            });
        });

        document.querySelectorAll('.mapping-editor-row').forEach(editor => {
            const previousRow = editor.previousElementSibling;
            const lineId = Number(previousRow?.dataset.lineId);
            const line = state.order.lines.find(item => item.id === lineId);
            const current = state.lineStates[lineId];
            editor.querySelector('.add-candidate')?.addEventListener('click', () => {
                addAllocation(lineId, findCandidate(line, editor.querySelector('.candidate-select').value));
            });
            editor.querySelector('.search-model')?.addEventListener('click', async () => {
                const query = editor.querySelector('.model-search').value.trim();
                if (!query) return;
                try {
                    const data = await api(`/api/inbound/shopee-models?query=${encodeURIComponent(query)}`);
                    state.searchResults[lineId] = data.models;
                    renderOrder();
                } catch (error) { setJobStatus(error.message, 'error'); }
            });
            editor.querySelector('.add-search-result')?.addEventListener('click', () => {
                const value = editor.querySelector('.search-result').value;
                addAllocation(lineId, (state.searchResults[lineId] || []).find(item => candidateValue(item) === value));
            });
            editor.querySelectorAll('.allocation-chip').forEach(chip => {
                const index = Number(chip.dataset.allocationIndex);
                chip.querySelector('.allocation-qty')?.addEventListener('change', event => {
                    current.allocations[index].qty = intValue(event.target.value);
                });
                chip.querySelector('.remove-allocation')?.addEventListener('click', () => {
                    current.allocations.splice(index, 1);
                    renderOrder();
                });
            });
        });

        document.querySelectorAll('.inbound-group').forEach(button => {
            button.addEventListener('click', event => {
                const lineIds = event.currentTarget.dataset.lineIds.split(',').map(Number).filter(Number.isFinite);
                createReceipt(lineIds, { autoPreview: true, autoApply: true, triggerButton: event.currentTarget });
            });
        });
    }

    function buildReceiptPayload(selectedLineIds = null) {
        const selected = selectedLineIds ? new Set(selectedLineIds.map(Number)) : null;
        const lines = [];
        for (const orderLine of state.order.lines) {
            if (selected && !selected.has(Number(orderLine.id))) continue;
            const item = state.lineStates[orderLine.id];
            if (item.received <= 0) continue;
            const sellable = Math.max(0, item.received - item.damaged);
            const allocated = item.allocations.reduce((sum, allocation) => sum + intValue(allocation.qty), 0);
            if (item.damaged > item.received) throw new Error(`${orderLine.alibaba_sku_name || orderLine.id} 的不良數不能超過實收數`);
            if (allocated !== item.shopeeQty) throw new Error(`${orderLine.alibaba_sku_name || orderLine.id} 的分配總數需等於蝦皮增加量`);
            if (item.received > orderLine.remaining_qty) throw new Error(`${orderLine.alibaba_sku_name || orderLine.id} 的實收量超過尚未收貨數量`);
            if (item.shopeeQty > 0 && !item.allocations.length) throw new Error(`${orderLine.alibaba_sku_name || orderLine.id} 尚未選擇蝦皮規格`);
            lines.push({
                orderLineId: orderLine.id,
                receivedQty: item.received,
                damagedQty: item.damaged,
                sellableQty: sellable,
                shopeeQty: item.shopeeQty,
                allocations: item.allocations.map(allocation => ({
                    productId: allocation.productId,
                    modelId: allocation.modelId,
                    qty: intValue(allocation.qty),
                    saveBinding: true,
                })),
            });
        }
        if (!lines.length) throw new Error('請至少填寫一筆本次實收數量');
        return { orderId: state.order.id, clientToken: crypto.randomUUID(), lines };
    }

    async function createReceipt(selectedLineIds = null, options = {}) {
        const button = options.triggerButton || el('createReceiptButton');
        button.disabled = true;
        el('receiptValidation').textContent = '';
        try {
            const data = await api('/api/inbound/receipts', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(buildReceiptPayload(selectedLineIds)),
            });
            state.receipt = data.receipt;
            renderReceipt();
            el('receiptPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
            if (options.autoPreview) {
                await previewReceipt();
                if (options.autoApply && state.system.writeEnabled && state.receipt?.status === 'preview_ready') {
                    await applyReceipt();
                }
            }
        } catch (error) {
            el('receiptValidation').textContent = error.message;
            setJobStatus(error.message, 'error');
        } finally {
            button.disabled = false;
        }
    }

    function statusLabel(status) {
        return ({ pending: '待預覽', previewed: '已預覽', success: '更新成功', failed: '失敗', manual_review: '需人工確認' })[status] || status;
    }

    function renderReceipt() {
        const receipt = state.receipt;
        if (!receipt) return;
        el('receiptPanel').classList.remove('hidden');
        el('receiptSummary').textContent = `到貨單 #${receipt.id}｜1688 訂單 ${receipt.alibaba_order_id}`;
        const notice = el('receiptNotice');
        notice.className = 'notice';
        if (receipt.status === 'preview_ready') {
            if (state.system.writeEnabled) {
                notice.classList.add('success');
                notice.textContent = '即時庫存已讀取。請核對每一筆「目前＋增加＝更新後」，再確認更新。';
            } else {
                notice.classList.add('warning');
                notice.textContent = '唯讀預覽完成：目前尚未啟用蝦皮真實寫入，下方「確認並更新蝦皮」不會開放。';
            }
        } else if (receipt.status === 'completed') {
            notice.classList.add('success');
            notice.textContent = '本張到貨單已完成，成功項目不會再次執行。';
        } else if (receipt.status === 'manual_review') {
            notice.classList.add('error');
            notice.textContent = receipt.error_message || '部分結果不明，請先到蝦皮後台人工確認，系統不會自動重試。';
        } else if (receipt.status === 'partial_failed') {
            notice.classList.add('warning');
            notice.textContent = '部分項目在送出前明確失敗，可重新讀取庫存後處理未成功項目。';
        } else {
            notice.textContent = '正在準備讀取蝦皮即時庫存；這個步驟不會修改商品。';
        }
        el('previewRows').innerHTML = receipt.lines.flatMap(line => line.updates.map(update => {
            const before = update.stock_before_preview;
            const target = before === null || before === undefined ? '—' : Number(before) + Number(update.allocated_qty);
            return `<tr>
                <td>${escapeHtml(line.alibaba_sku_name || '—')}${line.alibaba_sku_second_name ? `<br><small>${escapeHtml(line.alibaba_sku_second_name)}</small>` : ''}</td>
                <td><strong>${escapeHtml(update.shopee_product_name)}</strong><br><small>${escapeHtml(update.shopee_model_name)}｜${escapeHtml(update.shopee_product_id)} / ${escapeHtml(update.shopee_model_id)}</small></td>
                <td>+${update.allocated_qty}</td>
                <td>${before ?? '—'}</td>
                <td class="stock-flow">${before ?? '—'} → ${target}</td>
                <td class="state-${escapeHtml(update.status)}">${escapeHtml(statusLabel(update.status))}${update.error_message ? `<br><small>${escapeHtml(update.error_message)}</small>` : ''}</td>
            </tr>`;
        })).join('') || '<tr><td colspan="6">此到貨單沒有需要增加的蝦皮庫存（例如全部為不良品）。</td></tr>';
        const hasUncertainAttempt = receipt.status === 'manual_review' && receipt.lines
            .flatMap(line => line.updates).some(update => update.attempted_at !== null && update.attempted_at !== undefined);
        el('previewButton').disabled = hasUncertainAttempt || !['draft', 'partial_failed', 'manual_review', 'preview_ready'].includes(receipt.status);
        el('previewButton').innerHTML = receipt.status === 'partial_failed'
            ? '<i class="fas fa-rotate-right"></i> 重新讀取失敗項目'
            : '<i class="fas fa-eye"></i> 讀取即時庫存';
        const canApply = receipt.status === 'preview_ready' && state.system.writeEnabled;
        el('applyButton').disabled = !canApply;
        el('applyButton').title = state.system.writeEnabled ? '' : '目前為唯讀模式，尚未啟用真實寫入';
    }

    async function previewReceipt() {
        try {
            const job = await api(`/api/inbound/receipts/${state.receipt.id}/preview`, { method: 'POST' });
            setJobStatus(job.message, 'running');
            return await pollJob(job.jobId, async result => {
                if (!result.result?.receipt) throw new Error(result.message || '庫存預覽失敗');
                state.receipt = result.result.receipt;
                renderReceipt();
                await refreshOpenReceipts();
            });
        } catch (error) {
            setJobStatus(error.message, 'error');
            throw error;
        } finally {
            loadSystemStatus();
        }
    }

    async function applyReceipt() {
        const total = state.receipt.lines.flatMap(line => line.updates).filter(update => update.status !== 'success').reduce((sum, update) => sum + Number(update.allocated_qty), 0);
        if (!window.confirm(`即將把共 ${total} 件庫存增加到蝦皮。\n同一商品的所有規格會在同一個編輯頁完成，是否確認？`)) return;
        el('applyButton').disabled = true;
        try {
            const job = await api(`/api/inbound/receipts/${state.receipt.id}/apply`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ confirmed: true, previewVersion: state.receipt.preview_version }),
            });
            setJobStatus(job.message, 'running');
            await pollJob(job.jobId, async result => {
                if (!result.result?.receipt) throw new Error(result.message || '蝦皮入庫更新失敗');
                state.receipt = result.result.receipt;
                renderReceipt();
                await refreshOpenReceipts();
            });
        } catch (error) { setJobStatus(error.message, 'error'); }
        finally { loadSystemStatus(); }
    }

    el('importOrderButton').addEventListener('click', importOrder);
    el('orderReference').addEventListener('keydown', event => { if (event.key === 'Enter') importOrder(); });
    el('createReceiptButton').addEventListener('click', event => createReceipt(null, { autoPreview: true, triggerButton: event.currentTarget }));
    el('previewButton').addEventListener('click', () => previewReceipt().catch(() => {}));
    el('applyButton').addEventListener('click', applyReceipt);
    loadSystemStatus();
});
