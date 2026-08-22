document.addEventListener('DOMContentLoaded', function() {
    const searchInput = document.getElementById('searchInput');
    const searchButton = document.getElementById('searchButton');
    const resetButton = document.getElementById('resetButton');
    const productList = document.getElementById('productList');
    const loading = document.getElementById('loading');
    const advancedSearchCard = document.getElementById('advancedSearchCard');
    const advancedSearchInput = document.getElementById('advancedSearchInput');
    const advancedSearchButton = document.getElementById('advancedSearchButton');
    const clearAdvancedSearchButton = document.getElementById('clearAdvancedSearchButton');
    const searchOptionProduct = document.getElementById('searchOptionProduct');
    const searchOptionModel = document.getElementById('searchOptionModel');
    const searchOptionBoth = document.getElementById('searchOptionBoth');
    const createProcurementDraftButton = document.getElementById('createProcurementDraftButton');
    const syncProcurementOrdersButton = document.getElementById('syncProcurementOrdersButton');
    const alibabaAuthStatus = document.getElementById('alibabaAuthStatus');
    const skuReviewStatus = document.getElementById('skuReviewStatus');
    const skuReviewReportSelect = document.getElementById('skuReviewReportSelect');
    const skuReviewFilterSelect = document.getElementById('skuReviewFilterSelect');
    const loadSkuReviewButton = document.getElementById('loadSkuReviewButton');
    const applySelectedSkuReviewButton = document.getElementById('applySelectedSkuReviewButton');
    const applyHighConfidenceButton = document.getElementById('applyHighConfidenceButton');
    const skuReviewOverwriteCheckbox = document.getElementById('skuReviewOverwriteCheckbox');
    const skuReviewSummary = document.getElementById('skuReviewSummary');
    const skuReviewMessage = document.getElementById('skuReviewMessage');
    const skuReviewList = document.getElementById('skuReviewList');
    const cookieImportText = document.getElementById('cookieImportText');
    const cookieImportButton = document.getElementById('cookieImportButton');
    const cookieImportClearButton = document.getElementById('cookieImportClearButton');
    const cookieImportStatus = document.getElementById('cookieImportStatus');
    const batchRestockToolbar = document.getElementById('batchRestockToolbar');
    const batchRestockToolbarSummary = document.getElementById('batchRestockToolbarSummary');
    const openBatchRestockButton = document.getElementById('openBatchRestockButton');
    
    const progressBar = document.getElementById('progressBar');
    const progressText = document.getElementById('progressText');
    const statusMessage = document.getElementById('statusMessage');
    // 保存當前狀態變量
    window.currentSearchResults = null; // 保存原始搜尋結果
    window.currentAdvancedKeyword = ''; // 保存進階搜尋關鍵字
    window.currentSearchOption = 'product'; // 預設搜尋選項為商品名稱
    window.isPieChartVisible = false; // 圓餅圖顯示狀態
    window.alibabaLinks = {}; // 型號ID → 阿里巴巴商品URL 映射
    window.alibabaBindings = {}; // 商品ID+型號ID → 1688 下單資料
    window.alibabaAuth = null;
    window.currentRestockProducts = [];
    window.currentRestockAdjustment = null;
    window.currentBatchRestockSelection = null;
    window.skuReviewState = {
        reports: [],
        currentReport: '',
        currentFilter: 'actionable',
        currentGroups: []
    };

    function escapeHtml(value) {
        return String(value ?? '').replace(/[&<>"']/g, char => ({
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#039;'
        }[char]));
    }

    // 載入阿里巴巴商品URL 映射
    function loadAlibabaLinks() {
        if (Object.keys(window.alibabaLinks).length > 0) return; // 已載入過
        fetch('/api/alibaba-links')
            .then(r => r.json())
            .then(data => {
                window.alibabaLinks = data;
            })
            .catch(err => console.warn('載入阿里巴巴連結失敗:', err));
    }

    function loadAlibabaBindings(force = false) {
        if (!force && Object.keys(window.alibabaBindings).length > 0) {
            return Promise.resolve(window.alibabaBindings);
        }
        return fetch('/api/alibaba/bindings')
            .then(r => r.json())
            .then(data => {
                window.alibabaBindings = data || {};
                return window.alibabaBindings;
            })
            .catch(err => {
                console.warn('載入 1688 綁定失敗:', err);
                window.alibabaBindings = {};
                return {};
            });
    }

    function loadAlibabaAuthStatus() {
        return fetch('/api/alibaba/auth/status')
            .then(r => r.json())
            .then(data => {
                window.alibabaAuth = data;
                if (alibabaAuthStatus) {
                    alibabaAuthStatus.textContent = data.message || '1688 API 狀態未知';
                    alibabaAuthStatus.className = data.can_create_order ? 'auth-ready' : 'auth-missing';
                }
                return data;
            })
            .catch(err => {
                window.alibabaAuth = { can_create_order: false, message: '無法檢查 1688 API 狀態' };
                if (alibabaAuthStatus) {
                    alibabaAuthStatus.textContent = window.alibabaAuth.message;
                    alibabaAuthStatus.className = 'auth-missing';
                }
                console.warn('檢查 1688 API 狀態失敗:', err);
            });
    }

    function setSkuReviewMessage(text, type = '') {
        if (!skuReviewMessage) return;
        skuReviewMessage.textContent = text || '';
        skuReviewMessage.className = `sku-review-message ${type}`.trim();
    }

    function setCookieImportStatus(text, type = '') {
        if (!cookieImportStatus) return;
        cookieImportStatus.textContent = text || '';
        cookieImportStatus.className = `cookie-import-status ${type}`.trim();
    }

    async function importShopeeCookies() {
        if (!cookieImportText || !cookieImportButton) return;
        const cookiesText = cookieImportText.value.trim();
        if (!cookiesText) {
            setCookieImportStatus('請先貼上 Cookie JSON。', 'error');
            cookieImportText.focus();
            return;
        }

        cookieImportButton.disabled = true;
        setCookieImportStatus('正在驗證並儲存 Cookies...');
        try {
            const response = await fetch('/api/cookies/import', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ cookiesText })
            });
            const data = await response.json();
            if (!response.ok || data.status !== 'success') {
                throw new Error(data.message || 'Cookie 匯入失敗');
            }
            const domains = (data.domains || []).join('、');
            setCookieImportStatus(`已儲存 ${data.count} 筆 Cookies（${domains}）。`, 'success');
            cookieImportText.value = '';
        } catch (error) {
            setCookieImportStatus(error.message || 'Cookie 匯入失敗，請檢查貼上的內容。', 'error');
        } finally {
            cookieImportButton.disabled = false;
        }
    }

    function skuReviewReportLabel(report) {
        const validText = report.valid ? '可用' : '不可用';
        const createdAt = report.createdAt || new Date((report.mtime || 0) * 1000).toLocaleString();
        return `${report.fileName} | ${validText} | high ${report.highConfidenceCount || 0} / review ${report.needsReviewCount || 0} | ${createdAt}`;
    }

    function loadSkuReviewReports() {
        if (!skuReviewReportSelect) return Promise.resolve();
        return fetch('/api/alibaba/sku-review/reports')
            .then(r => r.json())
            .then(data => {
                if (data.status !== 'success') {
                    throw new Error(data.message || '載入 report 失敗');
                }
                window.skuReviewState.reports = data.reports || [];
                skuReviewReportSelect.innerHTML = '';

                if (!window.skuReviewState.reports.length) {
                    const option = document.createElement('option');
                    option.value = '';
                    option.textContent = '找不到 mapping report';
                    skuReviewReportSelect.appendChild(option);
                    if (skuReviewStatus) skuReviewStatus.textContent = '找不到可用的 1688 SKU mapping report。';
                    return data;
                }

                window.skuReviewState.reports.forEach(report => {
                    const option = document.createElement('option');
                    option.value = report.fileName;
                    option.textContent = skuReviewReportLabel(report);
                    option.disabled = !report.valid;
                    if (report.fileName === data.recommendedReport) {
                        option.selected = true;
                    }
                    skuReviewReportSelect.appendChild(option);
                });

                window.skuReviewState.currentReport = skuReviewReportSelect.value || data.recommendedReport || '';
                const recommended = window.skuReviewState.reports.find(report => report.fileName === window.skuReviewState.currentReport);
                if (skuReviewStatus && recommended) {
                    skuReviewStatus.textContent = recommended.valid
                        ? `目前建議使用 ${recommended.fileName}，high confidence ${recommended.highConfidenceCount} 筆。`
                        : `目前 report 不可用：${recommended.invalidReason || '原因未知'}`;
                }
                return data;
            })
            .catch(error => {
                console.warn('載入 SKU review reports 失敗:', error);
                setSkuReviewMessage(error.message || '載入 report 失敗', 'error');
            });
    }

    function renderSkuReviewSummary(data) {
        if (!skuReviewSummary) return;
        const summary = data.summary || {};
        const report = data.report || {};
        skuReviewSummary.innerHTML = '';

        [
            ['目前顯示', summary.shown || 0],
            ['High confidence', summary.high || 0],
            ['需人工確認', summary.review || 0],
            ['A 類可選', summary.actionable || 0],
            ['C 類錯誤', summary.error || 0],
        ].forEach(([label, value]) => {
            const item = document.createElement('div');
            item.className = 'sku-review-summary-item';
            const strong = document.createElement('strong');
            strong.textContent = value;
            const span = document.createElement('span');
            span.textContent = label;
            item.appendChild(strong);
            item.appendChild(span);
            skuReviewSummary.appendChild(item);
        });

        if (!report.valid) {
            const warning = document.createElement('div');
            warning.className = 'sku-review-summary-warning';
            warning.textContent = `此 report 標記為不可用：${report.invalidReason || '原因未知'}`;
            skuReviewSummary.appendChild(warning);
        }
    }

    function rowSelectedSku(rowElement) {
        const input = rowElement.querySelector('.sku-review-sku-input');
        return input ? input.value.trim() : '';
    }

    function buildSkuReviewRow(row) {
        const rowElement = document.createElement('div');
        const categoryClass = row.reviewCategory === 'High confidence'
            ? 'high'
            : row.reviewCategory.startsWith('A.')
                ? 'actionable'
                : row.reviewCategory.startsWith('C.')
                    ? 'error'
                    : 'review';
        rowElement.className = `sku-review-row ${categoryClass}`;
        rowElement.dataset.productId = row.productId || '';
        rowElement.dataset.specId = row.specId || '';
        rowElement.dataset.modelName = row.modelName || '';
        rowElement.dataset.suggestedSkuName = row.suggestedSkuName || '';

        const top = document.createElement('div');
        top.className = 'sku-review-row-top';

        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'sku-review-select';
        checkbox.checked = row.reviewCategory === 'High confidence';

        const model = document.createElement('div');
        model.className = 'sku-review-model';
        const modelName = document.createElement('strong');
        modelName.textContent = row.modelName || '未命名型號';
        const modelMeta = document.createElement('span');
        modelMeta.textContent = `規格ID: ${row.specId || '未找到'} | ${row.reviewCategory || ''}`;
        model.appendChild(modelName);
        model.appendChild(modelMeta);

        const current = document.createElement('div');
        current.className = 'sku-review-current';
        current.innerHTML = `<span>目前 SKU</span><strong>${escapeHtml(row.existingSkuName || '空')}</strong>`;

        const inputWrap = document.createElement('label');
        inputWrap.className = 'sku-review-input-wrap';
        const inputLabel = document.createElement('span');
        inputLabel.textContent = '要寫入的 1688 SKU';
        const input = document.createElement('input');
        input.className = 'sku-review-sku-input';
        input.type = 'text';
        input.value = row.reviewCategory === 'High confidence' ? (row.suggestedSkuName || '') : '';
        input.placeholder = row.suggestedSkuName || '點選下方選項或手動輸入';
        inputWrap.appendChild(inputLabel);
        inputWrap.appendChild(input);

        const applyButton = document.createElement('button');
        applyButton.type = 'button';
        applyButton.className = 'btn-outline sku-review-apply-row';
        applyButton.textContent = '套用此列';
        applyButton.addEventListener('click', () => {
            applySkuReviewItems([{
                productId: row.productId,
                specId: row.specId,
                modelName: row.modelName,
                skuName: rowSelectedSku(rowElement)
            }]);
        });

        top.appendChild(checkbox);
        top.appendChild(model);
        top.appendChild(current);
        top.appendChild(inputWrap);
        top.appendChild(applyButton);
        rowElement.appendChild(top);

        const chips = document.createElement('div');
        chips.className = 'sku-review-chips';
        if (row.suggestedSkuName) {
            const suggested = document.createElement('button');
            suggested.type = 'button';
            suggested.className = 'sku-review-chip suggested';
            suggested.textContent = `建議: ${row.suggestedSkuName}`;
            suggested.addEventListener('click', () => {
                input.value = row.suggestedSkuName;
                checkbox.checked = true;
            });
            chips.appendChild(suggested);
        }
        (row.colorOptions || []).forEach(optionText => {
            const chip = document.createElement('button');
            chip.type = 'button';
            chip.className = 'sku-review-chip';
            chip.textContent = optionText;
            chip.addEventListener('click', () => {
                input.value = optionText;
                checkbox.checked = true;
            });
            chips.appendChild(chip);
        });
        if (!chips.childNodes.length) {
            const empty = document.createElement('span');
            empty.className = 'sku-review-empty-options';
            empty.textContent = '沒有抓到可用 1688 選項，建議等 1688 不擋後重抓。';
            chips.appendChild(empty);
        }
        rowElement.appendChild(chips);

        const rowMeta = document.createElement('div');
        rowMeta.className = 'sku-review-row-meta';
        const reason = document.createElement('span');
        reason.textContent = row.reason || row.urlMessage || '';
        rowMeta.appendChild(reason);
        if (row.url) {
            const link = document.createElement('a');
            link.href = row.url;
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            link.textContent = '開啟 1688';
            rowMeta.appendChild(link);
        }
        rowElement.appendChild(rowMeta);
        return rowElement;
    }

    function renderSkuReview(data) {
        if (!skuReviewList) return;
        window.skuReviewState.currentGroups = data.groups || [];
        renderSkuReviewSummary(data);
        skuReviewList.innerHTML = '';

        if (!data.groups || data.groups.length === 0) {
            skuReviewList.innerHTML = `
                <div class="empty-state compact">
                    <i class="fas fa-check-circle"></i>
                    <p>此分類沒有需要顯示的 SKU 對應。</p>
                </div>
            `;
            return;
        }

        data.groups.forEach(group => {
            const details = document.createElement('details');
            details.className = 'sku-review-product';
            details.open = data.groups.length <= 3;

            const summary = document.createElement('summary');
            const title = document.createElement('span');
            title.textContent = `${group.productId} | ${group.productName || '未命名商品'}`;
            const counts = document.createElement('em');
            counts.textContent = `${group.counts.total} 筆 | high ${group.counts.high} | A ${group.counts.actionable} | C ${group.counts.error}`;
            summary.appendChild(title);
            summary.appendChild(counts);
            details.appendChild(summary);

            const rows = document.createElement('div');
            rows.className = 'sku-review-product-rows';
            (group.rows || []).forEach(row => {
                rows.appendChild(buildSkuReviewRow(row));
            });
            details.appendChild(rows);
            skuReviewList.appendChild(details);
        });
    }

    function loadSkuReview() {
        if (!skuReviewReportSelect || !skuReviewFilterSelect) return Promise.resolve();
        const report = skuReviewReportSelect.value || 'latest';
        const filter = skuReviewFilterSelect.value || 'actionable';
        window.skuReviewState.currentReport = report;
        window.skuReviewState.currentFilter = filter;
        setSkuReviewMessage('載入 SKU review 中...');
        return fetch(`/api/alibaba/sku-review?report=${encodeURIComponent(report)}&filter=${encodeURIComponent(filter)}`)
            .then(r => r.json().then(data => ({ ok: r.ok, data })))
            .then(({ ok, data }) => {
                if (!ok || data.status !== 'success') {
                    throw new Error(data.message || '載入 SKU review 失敗');
                }
                renderSkuReview(data);
                setSkuReviewMessage(`已載入 ${data.report.fileName}，目前顯示 ${data.summary.shown} 筆。`, 'success');
                return data;
            })
            .catch(error => {
                console.warn('載入 SKU review 失敗:', error);
                setSkuReviewMessage(error.message || '載入 SKU review 失敗', 'error');
            });
    }

    function collectSelectedSkuReviewItems() {
        if (!skuReviewList) return [];
        return Array.from(skuReviewList.querySelectorAll('.sku-review-row'))
            .filter(row => row.querySelector('.sku-review-select')?.checked)
            .map(row => ({
                productId: row.dataset.productId || '',
                specId: row.dataset.specId || '',
                modelName: row.dataset.modelName || '',
                skuName: rowSelectedSku(row)
            }))
            .filter(item => item.productId && (item.specId || item.modelName) && item.skuName);
    }

    function collectHighConfidenceItems() {
        const items = [];
        (window.skuReviewState.currentGroups || []).forEach(group => {
            (group.rows || []).forEach(row => {
                if (row.reviewCategory === 'High confidence' && row.suggestedSkuName) {
                    items.push({
                        productId: row.productId,
                        specId: row.specId,
                        modelName: row.modelName,
                        skuName: row.suggestedSkuName
                    });
                }
            });
        });
        return items;
    }

    function applySkuReviewItems(items) {
        if (!items.length) {
            setSkuReviewMessage('沒有可套用的 SKU，請先勾選並填入 1688 SKU。', 'error');
            return Promise.resolve();
        }
        setSkuReviewMessage(`寫入 ${items.length} 筆 SKU 對應中...`);
        return fetch('/api/alibaba/sku-review/apply', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                overwrite: skuReviewOverwriteCheckbox ? skuReviewOverwriteCheckbox.checked : true,
                items
            })
        })
            .then(r => r.json().then(data => ({ ok: r.ok, data })))
            .then(({ ok, data }) => {
                if (!ok || data.status !== 'success') {
                    throw new Error(data.message || '寫入失敗');
                }
                setSkuReviewMessage(`${data.message}${data.backupPath ? `；備份：${data.backupPath}` : ''}`, 'success');
                return loadSkuReviewReports().then(() => loadSkuReview());
            })
            .catch(error => {
                console.error('寫入 SKU review 失敗:', error);
                setSkuReviewMessage(error.message || '寫入失敗', 'error');
            });
    }

    // 依序根據 golden_table、規格ID、商品名稱+型號名稱取得阿里巴巴連結
    function getAlibabaLink(modelData, productName, modelName) {
        const modelUrl = (modelData && modelData.阿里巴巴商品URL) ? String(modelData.阿里巴巴商品URL).trim() : '';
        if (modelUrl) return modelUrl;
        if (!window.alibabaLinks) return '';
        const specId = modelData ? modelData.規格ID || '' : '';
        if (specId && window.alibabaLinks[String(specId)]) {
            return window.alibabaLinks[String(specId)];
        }
        if (productName && modelName) {
            return window.alibabaLinks[`${productName}|||${modelName}`] || '';
        }
        return '';
    }

    function normalizeId(value) {
        return String(value || '').trim();
    }

    function parseAlibabaOfferId(url) {
        const match = String(url || '').match(/\/offer\/(\d+)\.html/);
        return match ? match[1] : '';
    }

    function getAlibabaBinding(productId, modelData, productName = '', modelName = '') {
        const modelId = normalizeId(modelData ? modelData.規格ID : '');
        const key = `${normalizeId(productId)}|||${modelId}`;
        const stored = window.alibabaBindings && window.alibabaBindings[key] ? window.alibabaBindings[key] : {};
        const url = stored.alibabaProductUrl || modelData.阿里巴巴商品URL || getAlibabaLink(modelData, productName, modelName);
        const offerId = stored.alibabaOfferId || modelData['1688_offer_id'] || parseAlibabaOfferId(url);
        const skuId = stored.alibabaSkuId || modelData['1688_sku_id'] || '';
        const skuName = stored.alibabaSkuName || modelData['1688_sku_name'] || '';
        const price = stored.alibabaLastPriceCny ?? modelData['1688_last_price_cny'] ?? null;
        const status = offerId && skuName ? 'ready' : (offerId || skuId || skuName ? 'partial' : 'missing');
        return {
            alibabaProductName: stored.alibabaProductName || modelData.阿里巴巴商品名稱 || '',
            alibabaProductUrl: url || '',
            alibabaOfferId: offerId || '',
            alibabaSkuId: skuId || '',
            alibabaSkuName: skuName,
            alibabaSkuSecondName: stored.alibabaSkuSecondName || modelData['1688_sku_second_name'] || '',
            alibabaMinOrderQty: parseInt(stored.alibabaMinOrderQty || modelData['1688_min_order_qty'] || '1', 10) || 1,
            alibabaPackageMultiple: parseInt(stored.alibabaPackageMultiple || modelData['1688_package_multiple'] || '1', 10) || 1,
            alibabaLastPriceCny: price === null || price === '' || price === undefined ? null : Number(price),
            alibabaBindingStatus: stored.alibabaBindingStatus || status
        };
    }

    function getProductAlibabaFallbackUrl(product, productName) {
        const models = Array.isArray(product.型號) ? product.型號 : [];
        for (const model of models) {
            const modelName = String(model.型號名稱 || '').trim();
            const binding = getAlibabaBinding('', model, productName, modelName);
            const url = binding.alibabaProductUrl || getAlibabaLink(model, productName, modelName);
            if (url) return url;
        }
        return '';
    }

    function requiresAlibabaSecondSku(productName, modelName) {
        const parts = String(modelName || '').split(/[,，]/).map(part => part.trim()).filter(Boolean);
        if (parts.length < 2) return false;
        return /(手機殼|手机壳|iphone|ipad)/i.test(String(productName || '')) &&
            /^(?:iphone)?(?:\d{1,2}|x(?:r|s(?:\s*max)?)?|se\d*)/i.test(parts[1]);
    }

    function isAlibabaSkuDiscontinued(value) {
        return ['停售', '已停售', '以後不賣了', '以后不卖了'].includes(String(value || '').trim());
    }

    function hasCompleteAlibabaSkuSelection(binding, productName, modelName) {
        return Boolean(binding.alibabaSkuName) && !isAlibabaSkuDiscontinued(binding.alibabaSkuName) &&
            (!requiresAlibabaSecondSku(productName, modelName) || Boolean(binding.alibabaSkuSecondName));
    }

    function roundRestockQty(quantity) {
        const parsed = Number(quantity) || 0;
        if (parsed <= 0) return 0;
        return Math.round(parsed / 10) * 10;
    }

    const MAX_ALIBABA_CART_SKUS = 200;

    function getRestockItemQty(item) {
        const value = Object.prototype.hasOwnProperty.call(item || {}, 'adjustedQty')
            ? item.adjustedQty
            : item?.restockQty;
        return Math.max(0, Math.floor(Number(value) || 0));
    }

    function setRestockMessage(target, message, type = '') {
        if (!target) return;
        target.textContent = message || '';
        target.className = `alibaba-edit-message${type ? ` ${type}` : ''}`;
    }

    function groupRestockReportItems(items) {
        const groups = new Map();
        (Array.isArray(items) ? items : []).forEach(item => {
            const productName = String(item.productName || `商品 ${item.productId || ''}`).trim() || '未命名商品';
            if (!groups.has(productName)) groups.set(productName, []);
            groups.get(productName).push(String(item.modelName || item.alibabaSkuName || '未命名型號'));
        });
        return Array.from(groups, ([productName, models]) => ({ productName, models }));
    }

    function renderRestockReportList(items, emptyText) {
        const groups = groupRestockReportItems(items);
        if (groups.length === 0) return `<p class="restock-result-empty">${escapeHtml(emptyText)}</p>`;
        return `<ul class="restock-result-list">${groups.map(group => `
            <li>
                <strong>${escapeHtml(group.productName)}</strong>
                <span>${escapeHtml(group.models.join('、'))}</span>
            </li>
        `).join('')}</ul>`;
    }

    function ensureRestockResultModal() {
        let modal = document.getElementById('restockResultModal');
        if (modal) return modal;
        modal = document.createElement('div');
        modal.id = 'restockResultModal';
        modal.className = 'alibaba-modal hidden';
        modal.innerHTML = `
            <div class="alibaba-modal-backdrop" data-restock-result-close="true"></div>
            <div class="alibaba-modal-panel restock-result-panel" role="dialog" aria-modal="true" aria-labelledby="restockResultTitle">
                <div class="alibaba-modal-header">
                    <h3 id="restockResultTitle">1688 補貨結果</h3>
                    <button type="button" class="alibaba-modal-close" data-restock-result-close="true" aria-label="關閉">×</button>
                </div>
                <p class="restock-result-message" id="restockResultMessage"></p>
                <div class="restock-result-counts" id="restockResultCounts"></div>
                <div class="restock-result-sections" id="restockResultSections"></div>
                <div class="alibaba-modal-actions">
                    <button type="button" class="btn-primary" data-restock-result-close="true">知道了</button>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
        modal.addEventListener('click', event => {
            if (event.target.dataset.restockResultClose === 'true') modal.classList.add('hidden');
        });
        return modal;
    }

    function showRestockResult(workerResult, fallbackMessage = '') {
        const modal = ensureRestockResultModal();
        const summary = workerResult?.summary || {};
        const succeeded = Array.isArray(summary.succeeded) ? summary.succeeded : [];
        const unverified = Array.isArray(summary.unverified) ? summary.unverified : [];
        const cartFull = Array.isArray(summary.cartFull) ? summary.cartFull : [];
        const unprocessed = Array.isArray(summary.unprocessed) ? summary.unprocessed : [];
        const stoppedByLimit = workerResult?.stoppedReason === 'cart_limit_reached' || workerResult?.status === 'cart_limit_reached';
        modal.querySelector('#restockResultTitle').textContent = stoppedByLimit
            ? '1688 採購車已達上限'
            : '1688 補貨流程結果';
        modal.querySelector('#restockResultMessage').textContent = workerResult?.message || fallbackMessage || '補貨流程已結束。';
        modal.querySelector('#restockResultCounts').innerHTML = `
            <span class="is-success">已確認加入 <strong>${succeeded.length}</strong> 型號</span>
            <span class="is-warning">結果未確認 <strong>${unverified.length}</strong> 型號</span>
            <span class="is-error">未加入 <strong>${cartFull.length + unprocessed.length}</strong> 型號</span>
        `;
        modal.querySelector('#restockResultSections').innerHTML = `
            <section>
                <h4>已成功加入採購車</h4>
                ${renderRestockReportList(succeeded, '沒有已確認成功的型號。')}
            </section>
            ${unverified.length ? `<section><h4>已按下加入，但 1688 未回傳明確結果</h4>${renderRestockReportList(unverified, '')}</section>` : ''}
            ${cartFull.length ? `<section class="is-error"><h4>這一組因採購車已滿，未成功加入</h4>${renderRestockReportList(cartFull, '')}</section>` : ''}
            ${unprocessed.length ? `<section class="is-error"><h4>偵測到上限後，尚未執行</h4>${renderRestockReportList(unprocessed, '')}</section>` : ''}
        `;
        modal.classList.remove('hidden');
    }

    function monitorAlibabaRestockJob(jobId, statusTarget) {
        if (!jobId) return;
        const poll = () => fetch(`/api/alibaba-restock/jobs/${encodeURIComponent(jobId)}`)
            .then(async response => {
                const data = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error(data.message || '讀取補貨結果失敗');
                if (data.status === 'running') {
                    window.setTimeout(poll, 1000);
                    return;
                }
                if (data.status === 'failed') {
                    setRestockMessage(statusTarget, data.message || '1688 補貨流程失敗', 'error');
                    showRestockResult({}, data.message || '1688 補貨流程失敗');
                    return;
                }
                const workerResult = data.result || {};
                const stoppedByLimit = workerResult.stoppedReason === 'cart_limit_reached' || workerResult.status === 'cart_limit_reached';
                setRestockMessage(
                    statusTarget,
                    workerResult.message || '1688 補貨流程已完成',
                    stoppedByLimit ? 'error' : 'success'
                );
                if (stoppedByLimit || (workerResult.summary?.unverified || []).length > 0) {
                    showRestockResult(workerResult);
                }
            })
            .catch(error => {
                console.error('讀取 1688 補貨結果失敗:', error);
                setRestockMessage(statusTarget, error.message || '讀取補貨結果失敗', 'error');
            });
        window.setTimeout(poll, 1000);
    }

    function startAlibabaRestock(productId, product, items, options = {}) {
        const addToCart = Boolean(options.addToCart);
        const skippedCount = Math.max(0, Number(options.skippedCount) || 0);
        const statusTarget = options.statusTarget || null;
        const restockItems = (Array.isArray(items) ? items : [])
            .map(item => ({
                ...item,
                restockQty: getRestockItemQty(item)
            }))
            .filter(item => item.alibabaUrl && item.restockQty > 0);
        if (restockItems.length === 0) {
            const message = '沒有可補貨的 1688 型號。請先確認型號有 1688 連結且數量大於 0。';
            if (statusTarget) setRestockMessage(statusTarget, message, 'error');
            else alert(message);
            return Promise.resolve(false);
        }
        if (restockItems.length > MAX_ALIBABA_CART_SKUS) {
            const message = `本次共 ${restockItems.length} 個型號，超過 1688 採購車單次上限 ${MAX_ALIBABA_CART_SKUS} 個。`;
            if (statusTarget) setRestockMessage(statusTarget, message, 'error');
            else alert(message);
            return Promise.resolve(false);
        }

        const totalQty = restockItems.reduce((sum, item) => sum + item.restockQty, 0);
        const finalActionText = addToCart ? '程式會依序處理每個 1688 商品頁並加入採購車，不會送出訂單。' : '只會填數量，不會送出訂單。';
        const skippedText = skippedCount > 0
            ? `另有 ${skippedCount} 個型號未完成 1688 對應，將自動略過。\n\n`
            : '';
        if (!options.skipConfirmation) {
            const confirmed = confirm(`將開啟 1688 補貨流程，嘗試填入 ${restockItems.length} 個型號，共 ${totalQty} 件。\n\n${skippedText}${finalActionText}\n是否繼續？`);
            if (!confirmed) return Promise.resolve(false);
        }

        setRestockMessage(statusTarget, `正在啟動 ${restockItems.length} 個型號的 1688 補貨流程...`);

        return fetch('/api/alibaba-restock', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                productId: String(productId || ''),
                productName: product?.商品名稱 || '',
                addToCart,
                items: restockItems
            })
        })
            .then(async response => {
                const data = await response.json().catch(() => ({}));
                if (!response.ok || data.status !== 'success') {
                    throw new Error(data.message || '啟動 1688 補貨流程失敗');
                }
                setRestockMessage(statusTarget, `已啟動：${data.itemCount || restockItems.length} 個型號，共 ${data.totalQty || totalQty} 件。`, 'success');
                monitorAlibabaRestockJob(data.jobId, statusTarget);
                return data;
            })
            .catch(error => {
                console.error('1688 補貨流程失敗:', error);
                if (statusTarget) setRestockMessage(statusTarget, error.message || '啟動失敗', 'error');
                else alert(`1688 補貨流程失敗：${error.message}`);
                return false;
            });
    }

    function ensureRestockAdjustmentModal() {
        let modal = document.getElementById('restockAdjustmentModal');
        if (modal) return modal;
        modal = document.createElement('div');
        modal.id = 'restockAdjustmentModal';
        modal.className = 'alibaba-modal hidden';
        modal.innerHTML = `
            <div class="alibaba-modal-backdrop" data-restock-adjust-close="true"></div>
            <div class="alibaba-modal-panel restock-adjustment-panel" role="dialog" aria-modal="true" aria-labelledby="restockAdjustmentTitle">
                <div class="alibaba-modal-header">
                    <h3 id="restockAdjustmentTitle">微調 1688 補貨數量</h3>
                    <button type="button" class="alibaba-modal-close" data-restock-adjust-close="true" aria-label="關閉">×</button>
                </div>
                <div class="restock-modal-product-name" id="restockAdjustmentProductName"></div>
                <div class="restock-adjustment-list" id="restockAdjustmentList"></div>
                <div class="restock-modal-summary" id="restockAdjustmentSummary"></div>
                <div class="alibaba-edit-message" id="restockAdjustmentMessage" aria-live="polite"></div>
                <div class="alibaba-modal-actions restock-modal-actions">
                    <button type="button" class="btn-secondary" data-restock-adjust-close="true">取消</button>
                    <button type="button" class="btn-primary" id="submitRestockAdjustmentButton">一鍵補貨加採購車</button>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
        modal.addEventListener('click', event => {
            if (event.target.dataset.restockAdjustClose === 'true') {
                modal.classList.add('hidden');
                window.currentRestockAdjustment = null;
            }
        });
        modal.querySelector('#restockAdjustmentList').addEventListener('input', event => {
            if (!event.target.matches('[data-restock-adjust-index]')) return;
            const state = window.currentRestockAdjustment;
            const index = Number(event.target.dataset.restockAdjustIndex);
            if (!state || !state.items[index]) return;
            state.items[index].adjustedQty = Math.max(0, Math.floor(Number(event.target.value) || 0));
            updateRestockAdjustmentSummary();
        });
        modal.querySelector('#submitRestockAdjustmentButton').addEventListener('click', submitRestockAdjustment);
        return modal;
    }

    function updateRestockAdjustmentSummary() {
        const modal = ensureRestockAdjustmentModal();
        const state = window.currentRestockAdjustment;
        if (!state) return;
        const activeItems = state.items.filter(item => getRestockItemQty(item) > 0);
        const totalQty = activeItems.reduce((sum, item) => sum + getRestockItemQty(item), 0);
        modal.querySelector('#restockAdjustmentSummary').innerHTML =
            `<strong>${activeItems.length}</strong> 個型號會加入採購車，共 <strong>${totalQty}</strong> 件。數量設為 0 會略過該型號。`;
        modal.querySelector('#submitRestockAdjustmentButton').disabled = activeItems.length === 0;
    }

    function openRestockAdjustmentModal(productId, product, items, skippedCount = 0) {
        const modal = ensureRestockAdjustmentModal();
        const stateItems = (Array.isArray(items) ? items : []).map(item => ({
            ...item,
            adjustedQty: getRestockItemQty(item)
        }));
        window.currentRestockAdjustment = { productId, product, items: stateItems, skippedCount };
        modal.querySelector('#restockAdjustmentProductName').textContent = product?.商品名稱 || '未命名商品';
        modal.querySelector('#restockAdjustmentList').innerHTML = stateItems.map((item, index) => `
            <label class="restock-adjustment-row">
                <span class="restock-adjustment-name">
                    <strong>${escapeHtml(item.modelName || `型號 ${index + 1}`)}</strong>
                    <small>1688：${escapeHtml(item.alibabaSkuName || '未對應')}${item.alibabaSkuSecondName ? ` / ${escapeHtml(item.alibabaSkuSecondName)}` : ''}</small>
                </span>
                <span class="restock-original-qty">建議 ${getRestockItemQty(item)}</span>
                <input type="number" min="0" step="1" inputmode="numeric" value="${getRestockItemQty(item)}" data-restock-adjust-index="${index}" aria-label="${escapeHtml(item.modelName || '型號')} 補貨數量">
            </label>
        `).join('');
        setRestockMessage(modal.querySelector('#restockAdjustmentMessage'), '');
        updateRestockAdjustmentSummary();
        modal.classList.remove('hidden');
    }

    function submitRestockAdjustment() {
        const modal = ensureRestockAdjustmentModal();
        const state = window.currentRestockAdjustment;
        if (!state) return;
        const submitButton = modal.querySelector('#submitRestockAdjustmentButton');
        const message = modal.querySelector('#restockAdjustmentMessage');
        submitButton.disabled = true;
        startAlibabaRestock(state.productId, state.product, state.items, {
            addToCart: true,
            skippedCount: state.skippedCount,
            skipConfirmation: true,
            statusTarget: message
        }).then(started => {
            if (started) {
                modal.classList.add('hidden');
                window.currentRestockAdjustment = null;
            } else {
                updateRestockAdjustmentSummary();
            }
        });
    }

    function updateBatchRestockToolbar() {
        if (!batchRestockToolbar || !batchRestockToolbarSummary || !openBatchRestockButton) return;
        const products = Array.isArray(window.currentRestockProducts) ? window.currentRestockProducts : [];
        const readyProducts = products.filter(entry => entry.items.length > 0);
        const skuCount = readyProducts.reduce((sum, entry) => sum + entry.items.length, 0);
        batchRestockToolbar.hidden = readyProducts.length === 0;
        openBatchRestockButton.disabled = readyProducts.length === 0;
        batchRestockToolbarSummary.textContent = readyProducts.length > 0
            ? `當前結果有 ${readyProducts.length} 個商品可補貨，共 ${skuCount} 個型號。可取消勾選後再依序處理。`
            : '當前搜尋結果沒有已完成 1688 對應的補貨型號。';
    }

    function ensureBatchRestockModal() {
        let modal = document.getElementById('batchRestockModal');
        if (modal) return modal;
        modal = document.createElement('div');
        modal.id = 'batchRestockModal';
        modal.className = 'alibaba-modal hidden';
        modal.innerHTML = `
            <div class="alibaba-modal-backdrop" data-batch-restock-close="true"></div>
            <div class="alibaba-modal-panel batch-restock-panel" role="dialog" aria-modal="true" aria-labelledby="batchRestockTitle">
                <div class="alibaba-modal-header">
                    <h3 id="batchRestockTitle">選擇批次補貨商品</h3>
                    <button type="button" class="alibaba-modal-close" data-batch-restock-close="true" aria-label="關閉">×</button>
                </div>
                <div class="batch-restock-controls">
                    <span>預設已勾選當前搜尋結果的所有可補貨商品</span>
                    <div>
                        <button type="button" class="btn-outline" id="selectAllBatchRestockButton">全選</button>
                        <button type="button" class="btn-outline" id="clearAllBatchRestockButton">全部取消</button>
                    </div>
                </div>
                <div class="batch-restock-list" id="batchRestockList"></div>
                <div class="restock-modal-summary" id="batchRestockSummary"></div>
                <div class="batch-restock-limit-note">注意：1688 採購車最多 200 個型號；若購物車原本已有商品，實際可用名額會更少。</div>
                <div class="alibaba-edit-message" id="batchRestockMessage" aria-live="polite"></div>
                <div class="alibaba-modal-actions restock-modal-actions">
                    <button type="button" class="btn-secondary" data-batch-restock-close="true">取消</button>
                    <button type="button" class="btn-primary" id="submitBatchRestockButton">依序補貨並加採購車</button>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
        modal.addEventListener('click', event => {
            if (event.target.dataset.batchRestockClose === 'true') {
                modal.classList.add('hidden');
                window.currentBatchRestockSelection = null;
            }
        });
        modal.querySelector('#batchRestockList').addEventListener('change', event => {
            if (!event.target.matches('[data-batch-restock-index]')) return;
            const state = window.currentBatchRestockSelection;
            const index = Number(event.target.dataset.batchRestockIndex);
            if (!state || !state.products[index]) return;
            state.products[index].selected = event.target.checked;
            updateBatchRestockSummary();
        });
        modal.querySelector('#selectAllBatchRestockButton').addEventListener('click', () => setAllBatchRestockProducts(true));
        modal.querySelector('#clearAllBatchRestockButton').addEventListener('click', () => setAllBatchRestockProducts(false));
        modal.querySelector('#submitBatchRestockButton').addEventListener('click', submitBatchRestock);
        return modal;
    }

    function renderBatchRestockProducts() {
        const modal = ensureBatchRestockModal();
        const state = window.currentBatchRestockSelection;
        if (!state) return;
        modal.querySelector('#batchRestockList').innerHTML = state.products.map((entry, index) => {
            const qty = entry.items.reduce((sum, item) => sum + getRestockItemQty(item), 0);
            const disabled = entry.items.length === 0;
            const blockerText = entry.blockerCount > 0 ? `・${entry.blockerCount} 型號未完成對應` : '';
            const productName = entry.product?.商品名稱 || `商品 ${entry.productId}`;
            const imageUrl = processShopeeImageUrl(entry.product?.商品圖片網址 || '');
            return `
                <label class="batch-restock-product${disabled ? ' is-disabled' : ''}">
                    <input type="checkbox" data-batch-restock-index="${index}" ${entry.selected ? 'checked' : ''} ${disabled ? 'disabled' : ''}>
                    <span class="batch-restock-product-image${imageUrl ? '' : ' is-missing'}">
                        ${imageUrl
                            ? `<img src="${escapeHtml(imageUrl)}" alt="${escapeHtml(productName)}" loading="lazy">`
                            : '<i class="fas fa-image" aria-hidden="true"></i>'}
                    </span>
                    <span class="batch-restock-product-copy">
                        <strong>${escapeHtml(productName)}</strong>
                        <small>可補 ${entry.items.length} 型號・共 ${qty} 件${blockerText}</small>
                    </span>
                </label>
            `;
        }).join('');
        modal.querySelectorAll('.batch-restock-product-image img').forEach(image => {
            image.addEventListener('error', () => {
                const frame = image.closest('.batch-restock-product-image');
                if (!frame) return;
                frame.classList.add('is-missing');
                frame.innerHTML = '<i class="fas fa-image" aria-hidden="true"></i>';
            }, { once: true });
        });
    }

    function updateBatchRestockSummary() {
        const modal = ensureBatchRestockModal();
        const state = window.currentBatchRestockSelection;
        if (!state) return;
        const selected = state.products.filter(entry => entry.selected && entry.items.length > 0);
        const skuCount = selected.reduce((sum, entry) => sum + entry.items.length, 0);
        const totalQty = selected.reduce((sum, entry) =>
            sum + entry.items.reduce((itemSum, item) => itemSum + getRestockItemQty(item), 0), 0);
        const summary = modal.querySelector('#batchRestockSummary');
        const submitButton = modal.querySelector('#submitBatchRestockButton');
        summary.classList.toggle('is-warning', skuCount >= 180 && skuCount <= MAX_ALIBABA_CART_SKUS);
        summary.classList.toggle('is-over-limit', skuCount > MAX_ALIBABA_CART_SKUS);
        summary.innerHTML = `已選 <strong>${selected.length}</strong> 個商品，<strong>${skuCount} / ${MAX_ALIBABA_CART_SKUS}</strong> 個型號，共 <strong>${totalQty}</strong> 件。`;
        if (skuCount > MAX_ALIBABA_CART_SKUS) {
            summary.innerHTML += ` <span>超過上限 ${skuCount - MAX_ALIBABA_CART_SKUS} 個，請取消部分商品。</span>`;
        } else if (skuCount >= 180) {
            summary.innerHTML += ' <span>已接近 200 個上限。</span>';
        }
        submitButton.disabled = selected.length === 0 || skuCount > MAX_ALIBABA_CART_SKUS;
    }

    function setAllBatchRestockProducts(selected) {
        const state = window.currentBatchRestockSelection;
        if (!state) return;
        state.products.forEach(entry => {
            entry.selected = Boolean(selected && entry.items.length > 0);
        });
        renderBatchRestockProducts();
        updateBatchRestockSummary();
    }

    function openBatchRestockModal() {
        const source = Array.isArray(window.currentRestockProducts) ? window.currentRestockProducts : [];
        const products = source.map(entry => ({
            ...entry,
            items: entry.items.map(item => ({ ...item })),
            selected: entry.items.length > 0
        }));
        if (!products.some(entry => entry.items.length > 0)) return;
        window.currentBatchRestockSelection = { products };
        const modal = ensureBatchRestockModal();
        setRestockMessage(modal.querySelector('#batchRestockMessage'), '');
        renderBatchRestockProducts();
        updateBatchRestockSummary();
        modal.classList.remove('hidden');
    }

    function submitBatchRestock() {
        const modal = ensureBatchRestockModal();
        const state = window.currentBatchRestockSelection;
        if (!state) return;
        const selected = state.products.filter(entry => entry.selected && entry.items.length > 0);
        const items = selected.flatMap(entry => entry.items.map(item => ({
            ...item,
            productId: entry.productId,
            productName: entry.product?.商品名稱 || ''
        })));
        const skippedCount = selected.reduce((sum, entry) => sum + entry.blockerCount, 0);
        const submitButton = modal.querySelector('#submitBatchRestockButton');
        const message = modal.querySelector('#batchRestockMessage');
        submitButton.disabled = true;
        startAlibabaRestock('batch', {
            商品名稱: `批次補貨（${selected.length} 個商品）`
        }, items, {
            addToCart: true,
            skippedCount,
            skipConfirmation: true,
            statusTarget: message
        }).then(started => {
            if (started) {
                modal.classList.add('hidden');
                window.currentBatchRestockSelection = null;
            } else {
                updateBatchRestockSummary();
            }
        });
    }

    if (openBatchRestockButton) {
        openBatchRestockButton.addEventListener('click', openBatchRestockModal);
    }

    function rerenderCurrentProducts() {
        if (!window.lastSearchResults) return;
        displayProducts(
            window.lastSearchResults,
            window.currentAdvancedKeyword || '',
            window.currentSearchOption || 'product'
        );
    }

    function ensureAlibabaEditModal() {
        let modal = document.getElementById('alibabaEditModal');
        if (modal) return modal;

        modal = document.createElement('div');
        modal.id = 'alibabaEditModal';
        modal.className = 'alibaba-modal hidden';
        modal.innerHTML = `
            <div class="alibaba-modal-backdrop" data-alibaba-close="true"></div>
            <div class="alibaba-modal-panel" role="dialog" aria-modal="true" aria-labelledby="alibabaEditTitle">
                <div class="alibaba-modal-header">
                    <h3 id="alibabaEditTitle">編輯阿里巴巴資料</h3>
                    <button type="button" class="alibaba-modal-close" data-alibaba-close="true" aria-label="關閉">×</button>
                </div>
                <div class="alibaba-model-summary">
                    <div><strong>商品：</strong><span id="alibabaEditProductName"></span></div>
                    <div><strong>型號：</strong><span id="alibabaEditModelName"></span></div>
                    <div><strong>規格 ID：</strong><span id="alibabaEditSpecId"></span></div>
                </div>
                <form id="alibabaEditForm" class="alibaba-edit-form">
                    <label for="alibabaProductNameInput">阿里巴巴商品名稱</label>
                    <input type="text" id="alibabaProductNameInput" autocomplete="off">

                    <label for="alibabaProductUrlInput">阿里巴巴商品URL</label>
                    <input type="url" id="alibabaProductUrlInput" placeholder="https://detail.1688.com/offer/...html">

                    <div class="alibaba-field-grid">
                        <div>
                            <label for="alibabaOfferIdInput">1688 offerId</label>
                            <input type="text" id="alibabaOfferIdInput" autocomplete="off" placeholder="可由商品URL自動解析">
                        </div>
                        <div>
                            <label for="alibabaSkuIdInput">1688 skuId</label>
                            <input type="text" id="alibabaSkuIdInput" autocomplete="off">
                        </div>
                        <div>
                            <label for="alibabaSkuNameInput">1688 SKU 名稱</label>
                            <input type="text" id="alibabaSkuNameInput" autocomplete="off">
                        </div>
                        <div>
                            <label for="alibabaPriceInput">單價 CNY</label>
                            <input type="number" id="alibabaPriceInput" min="0" step="0.01">
                        </div>
                        <div>
                            <label for="alibabaMinOrderQtyInput">MOQ</label>
                            <input type="number" id="alibabaMinOrderQtyInput" min="1" step="1" value="1">
                        </div>
                        <div>
                            <label for="alibabaPackageMultipleInput">包裝倍數</label>
                            <input type="number" id="alibabaPackageMultipleInput" min="1" step="1" value="1">
                        </div>
                    </div>

                    <label for="alibabaApplyScopeInput">套用範圍</label>
                    <select id="alibabaApplyScopeInput">
                        <option value="single">只更新此型號</option>
                        <option value="fill_missing">套用到此商品缺漏型號</option>
                        <option value="overwrite_all">覆蓋此商品全部型號</option>
                        <option value="selected_models">自選型號</option>
                    </select>

                    <div id="alibabaSelectedModelsPanel" class="alibaba-selected-models hidden">
                        <div class="alibaba-selected-models-header">
                            <span>選擇要套用的型號</span>
                            <div class="alibaba-selected-models-actions">
                                <button type="button" class="btn-outline" id="alibabaSelectAllModelsButton">全選</button>
                                <button type="button" class="btn-outline" id="alibabaClearModelsButton">清除</button>
                            </div>
                        </div>
                        <div id="alibabaSelectedModelsList" class="alibaba-selected-models-list"></div>
                    </div>

                    <div id="alibabaEditMessage" class="alibaba-edit-message" aria-live="polite"></div>

                    <div class="alibaba-modal-actions">
                        <button type="button" class="btn-secondary" data-alibaba-close="true">取消</button>
                        <button type="submit" class="btn-primary" id="alibabaEditSaveButton">保存</button>
                    </div>
                </form>
            </div>
        `;
        document.body.appendChild(modal);

        modal.addEventListener('click', function(e) {
            if (e.target.dataset.alibabaClose === 'true') {
                closeAlibabaEditor();
            }
        });

        modal.querySelector('#alibabaEditForm').addEventListener('submit', saveAlibabaEdit);
        modal.querySelector('#alibabaApplyScopeInput').addEventListener('change', function() {
            toggleAlibabaSelectedModelsPanel(modal);
        });
        modal.querySelector('#alibabaSelectAllModelsButton').addEventListener('click', function() {
            modal.querySelectorAll('#alibabaSelectedModelsList input[type="checkbox"]').forEach(checkbox => {
                checkbox.checked = true;
            });
        });
        modal.querySelector('#alibabaClearModelsButton').addEventListener('click', function() {
            modal.querySelectorAll('#alibabaSelectedModelsList input[type="checkbox"]').forEach(checkbox => {
                checkbox.checked = false;
            });
        });
        return modal;
    }

    function toggleAlibabaSelectedModelsPanel(modal) {
        const scopeInput = modal.querySelector('#alibabaApplyScopeInput');
        const panel = modal.querySelector('#alibabaSelectedModelsPanel');
        if (!scopeInput || !panel) return;
        panel.classList.toggle('hidden', scopeInput.value !== 'selected_models');
    }

    function isSameAlibabaModel(model, specId, modelName) {
        const currentSpecId = String(specId || '').trim();
        const currentModelName = String(modelName || '').trim();
        const modelSpecId = String(model.規格ID || '').trim();
        const modelModelName = String(model.型號名稱 || '').trim();
        return (currentSpecId && modelSpecId === currentSpecId) ||
            (!currentSpecId && currentModelName && modelModelName === currentModelName);
    }

    function renderAlibabaSelectedModels(modal, product, activeModelData) {
        const list = modal.querySelector('#alibabaSelectedModelsList');
        if (!list) return;
        list.innerHTML = '';

        const productName = product.商品名稱 || '';
        const models = Array.isArray(product.型號) ? product.型號 : [];
        if (models.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'alibaba-selected-models-empty';
            empty.textContent = '此商品沒有可選型號';
            list.appendChild(empty);
            return;
        }

        models.forEach(model => {
            const modelName = String(model.型號名稱 || '').trim();
            const specId = String(model.規格ID || '').trim();
            const hasAlibabaUrl = Boolean(getAlibabaLink(model, productName, modelName));
            const isActive = isSameAlibabaModel(model, activeModelData.規格ID || '', activeModelData.型號名稱 || '');

            const option = document.createElement('label');
            option.className = 'alibaba-selected-model-option';

            const checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.dataset.specId = specId;
            checkbox.dataset.modelName = modelName;
            checkbox.checked = isActive;

            const details = document.createElement('span');
            details.className = 'alibaba-selected-model-details';

            const title = document.createElement('span');
            title.className = 'alibaba-selected-model-name';
            title.textContent = modelName || '未命名型號';

            const meta = document.createElement('span');
            meta.className = 'alibaba-selected-model-meta';
            meta.textContent = `規格 ID: ${specId || '未找到'}`;

            const status = document.createElement('span');
            status.className = `alibaba-selected-model-status ${hasAlibabaUrl ? 'has-url' : 'missing-url'}`;
            status.textContent = hasAlibabaUrl ? '已有連結' : '缺漏';

            details.appendChild(title);
            details.appendChild(meta);
            option.appendChild(checkbox);
            option.appendChild(details);
            option.appendChild(status);
            list.appendChild(option);
        });
    }

    function getSelectedAlibabaModels(modal) {
        return Array.from(modal.querySelectorAll('#alibabaSelectedModelsList input[type="checkbox"]:checked')).map(checkbox => ({
            specId: checkbox.dataset.specId || '',
            modelName: checkbox.dataset.modelName || ''
        }));
    }

    function openAlibabaEditor(productId, product, modelData) {
        const modal = ensureAlibabaEditModal();
        const productName = product.商品名稱 || '';
        const modelName = modelData.型號名稱 || '';
        const specId = modelData.規格ID || '';
        const binding = getAlibabaBinding(productId, modelData, productName, modelName);
        const effectiveUrl = binding.alibabaProductUrl;

        window.currentAlibabaEdit = {
            productId,
            productName,
            modelName,
            specId,
            originalOfferId: binding.alibabaOfferId || parseAlibabaOfferId(modelData.阿里巴巴商品URL || effectiveUrl || '')
        };

        modal.querySelector('#alibabaEditProductName').textContent = productName || '未知商品';
        modal.querySelector('#alibabaEditModelName').textContent = modelName || '未知型號';
        modal.querySelector('#alibabaEditSpecId').textContent = specId || '未找到';
        modal.querySelector('#alibabaProductNameInput').value = binding.alibabaProductName || '';
        modal.querySelector('#alibabaProductUrlInput').value = modelData.阿里巴巴商品URL || effectiveUrl || '';
        modal.querySelector('#alibabaOfferIdInput').value = binding.alibabaOfferId || '';
        modal.querySelector('#alibabaSkuIdInput').value = binding.alibabaSkuId || '';
        modal.querySelector('#alibabaSkuNameInput').value = binding.alibabaSkuName || '';
        modal.querySelector('#alibabaPriceInput').value = binding.alibabaLastPriceCny || '';
        modal.querySelector('#alibabaMinOrderQtyInput').value = binding.alibabaMinOrderQty || 1;
        modal.querySelector('#alibabaPackageMultipleInput').value = binding.alibabaPackageMultiple || 1;
        modal.querySelector('#alibabaApplyScopeInput').value = 'single';
        renderAlibabaSelectedModels(modal, product, modelData);
        toggleAlibabaSelectedModelsPanel(modal);
        modal.querySelector('#alibabaEditMessage').textContent = '';
        modal.classList.remove('hidden');
        modal.querySelector('#alibabaProductUrlInput').focus();

        modal.querySelector('#alibabaProductUrlInput').oninput = function() {
            const parsedOfferId = parseAlibabaOfferId(this.value);
            const offerInput = modal.querySelector('#alibabaOfferIdInput');
            if (parsedOfferId && !offerInput.value.trim()) {
                offerInput.value = parsedOfferId;
            }
        };
    }

    function closeAlibabaEditor() {
        const modal = document.getElementById('alibabaEditModal');
        if (modal) modal.classList.add('hidden');
        window.currentAlibabaEdit = null;
    }

    function ensure1688SkuEditor() {
        let modal = document.getElementById('skuMappingEditModal');
        if (modal) return modal;

        modal = document.createElement('div');
        modal.id = 'skuMappingEditModal';
        modal.className = 'alibaba-modal hidden';
        modal.innerHTML = `
            <div class="alibaba-modal-backdrop" data-sku-mapping-close="true"></div>
            <div class="alibaba-modal-panel sku-mapping-modal-panel" role="dialog" aria-modal="true" aria-labelledby="skuMappingEditTitle">
                <div class="alibaba-modal-header">
                    <h3 id="skuMappingEditTitle">編輯 1688 對應型號</h3>
                    <button type="button" class="alibaba-modal-close" data-sku-mapping-close="true" aria-label="關閉">×</button>
                </div>
                <div class="alibaba-model-summary">
                    <div><strong>商品：</strong><span id="skuMappingProductName"></span></div>
                    <div><strong>使用方式：</strong>第一規格通常是顏色；手機殼等雙規格商品請再填第二規格（例如 iPhone 型號）。</div>
                </div>
                <form id="skuMappingEditForm" class="alibaba-edit-form">
                    <div id="skuMappingRows" class="sku-mapping-rows"></div>
                    <p class="sku-mapping-help">補貨時會依序點選第一規格、第二規格，再輸入數量與加入採購車。第二規格留白表示此商品只有一段規格。</p>
                    <div id="skuMappingEditMessage" class="alibaba-edit-message" aria-live="polite"></div>
                    <div class="alibaba-modal-actions">
                        <button type="button" class="btn-secondary" data-sku-mapping-close="true">取消</button>
                        <button type="submit" class="btn-primary" id="skuMappingSaveButton">儲存對應</button>
                    </div>
                </form>
            </div>
        `;
        document.body.appendChild(modal);

        modal.addEventListener('click', function(e) {
            if (e.target.dataset.skuMappingClose === 'true') {
                close1688SkuEditor();
            }
        });
        modal.querySelector('#skuMappingEditForm').addEventListener('submit', save1688SkuMapping);
        return modal;
    }

    function open1688SkuEditor(productId, product, modelData, alibabaLink) {
        const modal = ensure1688SkuEditor();
        window.current1688SkuEdit = {
            productId: String(productId || ''),
            product
        };
        const renderRows = function() {
            modal.querySelector('#skuMappingProductName').textContent = product.商品名稱 || '未知商品';
            render1688SkuMappingRows(modal, productId, product, modelData, alibabaLink);
        };
        renderRows();
        modal.querySelector('#skuMappingEditMessage').textContent = '';
        modal.classList.remove('hidden');
        // 搜尋結果是爬蟲當次的快照；開窗時強制載入 golden_table.json 的最新對應。
        loadAlibabaBindings(true)
            .then(renderRows)
            .catch(error => console.warn('重新載入 1688 對應失敗:', error));
    }

    function render1688SkuMappingRows(modal, productId, product, activeModelData, activeAlibabaLink) {
        const container = modal.querySelector('#skuMappingRows');
        const models = Array.isArray(product.型號) ? product.型號 : [];
        const activeSpecId = String(activeModelData.規格ID || '').trim();
        container.innerHTML = '';
        models.forEach(modelData => {
            const modelName = String(modelData.型號名稱 || '').trim();
            const specId = String(modelData.規格ID || '').trim();
            const binding = getAlibabaBinding(productId, modelData, product.商品名稱 || '', modelName);
            const url = binding.alibabaProductUrl || ((specId && specId === activeSpecId) ? activeAlibabaLink : '') || getProductAlibabaFallbackUrl(product, product.商品名稱 || '');
            const row = document.createElement('div');
            row.className = `sku-mapping-row ${specId === activeSpecId ? 'is-active' : ''}`;
            const label = document.createElement('label');
            label.className = 'sku-mapping-model-name';
            label.textContent = modelName || `規格 ${specId}`;
            const input = document.createElement('input');
            input.type = 'text';
            input.className = 'sku-mapping-input';
            input.autocomplete = 'off';
            input.placeholder = '例如：2349黑色';
            input.value = binding.alibabaSkuName || '';
            input.dataset.skuSpecId = specId;
            input.dataset.skuModelName = modelName;
            const secondInput = document.createElement('input');
            secondInput.type = 'text';
            secondInput.className = 'sku-mapping-input sku-mapping-second-input';
            secondInput.autocomplete = 'off';
            secondInput.placeholder = '第二規格（例如：16 Pro，可留白）';
            secondInput.value = binding.alibabaSkuSecondName || '';
            secondInput.dataset.skuSpecId = specId;
            secondInput.dataset.skuModelName = modelName;
            secondInput.dataset.skuSecond = 'true';
            const link = document.createElement('a');
            link.className = 'sku-mapping-product-link';
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            link.textContent = url ? '1688 商品頁' : '未設定 1688 URL';
            if (url) link.href = url;
            else link.classList.add('disabled');
            row.append(label, input, secondInput, link);
            container.appendChild(row);
        });
    }

    function close1688SkuEditor() {
        const modal = document.getElementById('skuMappingEditModal');
        if (modal) modal.classList.add('hidden');
        window.current1688SkuEdit = null;
    }

    function save1688SkuMapping(e) {
        e.preventDefault();
        const modal = ensure1688SkuEditor();
        const context = window.current1688SkuEdit;
        if (!context) return;

        const message = modal.querySelector('#skuMappingEditMessage');
        const saveButton = modal.querySelector('#skuMappingSaveButton');
        const mappings = Array.from(modal.querySelectorAll('.sku-mapping-input:not([data-sku-second="true"])')).map(input => {
            const secondInput = modal.querySelector(`.sku-mapping-second-input[data-sku-spec-id="${CSS.escape(input.dataset.skuSpecId || '')}"]`);
            return {
                specId: input.dataset.skuSpecId || '',
                modelName: input.dataset.skuModelName || '',
                alibabaSkuName: input.value.trim(),
                alibabaSkuSecondName: secondInput ? secondInput.value.trim() : ''
            };
        });
        saveButton.disabled = true;
        message.textContent = `正在寫入 ${mappings.length} 個型號至 golden_table.json...`;
        message.className = 'alibaba-edit-message';

        fetch('/api/golden-table/product-1688-skus', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                productId: context.productId,
                mappings
            })
        })
            .then(async response => {
                const data = await response.json().catch(() => ({}));
                if (!response.ok || data.status !== 'success') {
                    throw new Error(data.message || '儲存 1688 對應型號失敗');
                }
                return data;
            })
            .then(data => {
                applyAlibabaUpdatesToCurrentResults(context.productId, data.updatedModels || []);
                return loadAlibabaBindings(true).then(() => {
                    close1688SkuEditor();
                    rerenderCurrentProducts();
                });
            })
            .catch(error => {
                console.error('儲存 1688 對應型號失敗:', error);
                message.textContent = error.message || '儲存失敗';
                message.className = 'alibaba-edit-message error';
            })
            .finally(() => {
                saveButton.disabled = false;
            });
    }

    function applyAlibabaUpdatesToCurrentResults(productId, updatedModels) {
        const product = window.lastSearchResults && window.lastSearchResults[productId];
        if (!product || !Array.isArray(product.型號) || !Array.isArray(updatedModels)) return;

        updatedModels.forEach(updatedModel => {
            const updatedSpecId = String(updatedModel.規格ID || '').trim();
            const updatedModelName = String(updatedModel.型號名稱 || '').trim();
            const localModel = product.型號.find(model => {
                const localSpecId = String(model.規格ID || '').trim();
                const localModelName = String(model.型號名稱 || '').trim();
                return (updatedSpecId && localSpecId === updatedSpecId) ||
                    (!updatedSpecId && updatedModelName && localModelName === updatedModelName);
            });
            if (localModel) {
                localModel.阿里巴巴商品名稱 = updatedModel.阿里巴巴商品名稱 || '';
                localModel.阿里巴巴商品URL = updatedModel.阿里巴巴商品URL || '';
                localModel['1688_offer_id'] = updatedModel['1688_offer_id'] || '';
                localModel['1688_sku_id'] = updatedModel['1688_sku_id'] || '';
                localModel['1688_sku_name'] = updatedModel['1688_sku_name'] || '';
                localModel['1688_sku_second_name'] = updatedModel['1688_sku_second_name'] || '';
                localModel['1688_min_order_qty'] = updatedModel['1688_min_order_qty'] || 1;
                localModel['1688_package_multiple'] = updatedModel['1688_package_multiple'] || 1;
                localModel['1688_last_price_cny'] = updatedModel['1688_last_price_cny'] || null;
            }
        });
    }

    function saveAlibabaEdit(e) {
        e.preventDefault();
        const modal = ensureAlibabaEditModal();
        const context = window.currentAlibabaEdit;
        if (!context) return;

        const nameInput = modal.querySelector('#alibabaProductNameInput');
        const urlInput = modal.querySelector('#alibabaProductUrlInput');
        const offerInput = modal.querySelector('#alibabaOfferIdInput');
        const skuIdInput = modal.querySelector('#alibabaSkuIdInput');
        const skuNameInput = modal.querySelector('#alibabaSkuNameInput');
        const priceInput = modal.querySelector('#alibabaPriceInput');
        const minOrderInput = modal.querySelector('#alibabaMinOrderQtyInput');
        const packageMultipleInput = modal.querySelector('#alibabaPackageMultipleInput');
        const scopeInput = modal.querySelector('#alibabaApplyScopeInput');
        const message = modal.querySelector('#alibabaEditMessage');
        const saveButton = modal.querySelector('#alibabaEditSaveButton');
        const alibabaProductUrl = urlInput.value.trim();
        const alibabaOfferId = offerInput.value.trim() || parseAlibabaOfferId(alibabaProductUrl);
        const applyScope = scopeInput.value;
        const selectedModels = applyScope === 'selected_models' ? getSelectedAlibabaModels(modal) : [];

        if (alibabaProductUrl && alibabaOfferId !== String(context.originalOfferId || '')) {
            const params = new URLSearchParams({
                mode: 'urls',
                productId: context.productId,
                modelId: context.specId || context.modelName || '',
                newUrl: alibabaProductUrl
            });
            window.location.href = `/sku-mapping.html?${params}`;
            return;
        }

        if (alibabaProductUrl && !/^https?:\/\//i.test(alibabaProductUrl)) {
            message.textContent = 'URL 必須以 http:// 或 https:// 開頭';
            message.className = 'alibaba-edit-message error';
            return;
        }

        if (applyScope === 'selected_models' && selectedModels.length === 0) {
            message.textContent = '請至少選擇一個要套用的型號';
            message.className = 'alibaba-edit-message error';
            return;
        }

        saveButton.disabled = true;
        message.textContent = '保存中...';
        message.className = 'alibaba-edit-message';

        fetch('/api/golden-table/model-alibaba', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                productId: context.productId,
                specId: context.specId,
                modelName: context.modelName,
                alibabaProductName: nameInput.value.trim(),
                alibabaProductUrl,
                alibabaOfferId,
                alibabaSkuId: skuIdInput.value.trim(),
                alibabaSkuName: skuNameInput.value.trim(),
                alibabaLastPriceCny: priceInput.value.trim(),
                alibabaMinOrderQty: minOrderInput.value.trim() || '1',
                alibabaPackageMultiple: packageMultipleInput.value.trim() || '1',
                applyScope,
                selectedModels
            })
        })
            .then(async response => {
                const data = await response.json();
                if (!response.ok) {
                    throw new Error(data.message || '保存失敗');
                }
                return data;
            })
            .then(data => {
                applyAlibabaUpdatesToCurrentResults(context.productId, data.updatedModels);
                window.alibabaLinks = {};
                window.alibabaBindings = {};
                loadAlibabaLinks();
                loadAlibabaBindings(true).then(() => {
                    closeAlibabaEditor();
                    rerenderCurrentProducts();
                });
            })
            .catch(error => {
                message.textContent = error.message || '保存失敗';
                message.className = 'alibaba-edit-message error';
            })
            .finally(() => {
                saveButton.disabled = false;
            });
    }

    function calculateModelRestock(product, modelData, months) {
        const monthlyRate = parseInt(modelData.月銷量, 10) || 0;
        const currentStock = parseInt(modelData.商品庫存, 10) || 0;
        let effectiveMonthlyRate = monthlyRate;
        let isEstimated = false;

        if (currentStock === 0 && monthlyRate === 0) {
            const modelHistoricalSales = parseInt(modelData.已售出數量, 10) || 0;
            const productTotalHistoricalSales = parseInt(product.已售出總數量, 10) || 0;
            const productTotalMonthlySales = parseInt(product.總月銷量, 10) || 0;
            if (modelHistoricalSales > 0 && productTotalHistoricalSales > 0 && productTotalMonthlySales > 0) {
                effectiveMonthlyRate = Math.round((productTotalMonthlySales * (modelHistoricalSales / productTotalHistoricalSales)) * 10) / 10;
                isEstimated = true;
            }
        }

        const targetStock = Math.round(effectiveMonthlyRate * months);
        const rawSuggestedQty = Math.max(0, targetStock - currentStock);
        const suggestedQty = roundRestockQty(rawSuggestedQty);
        return {
            monthlySales: Math.round(effectiveMonthlyRate),
            currentStock,
            targetStock,
            rawSuggestedQty,
            suggestedQty,
            isEstimated
        };
    }

    function collectProcurementItems() {
        const products = window.lastSearchResults || {};
        const months = parseInt(document.getElementById('inventoryMonth')?.value || '4', 10) || 4;
        const items = [];
        Object.entries(products).forEach(([productId, product]) => {
            if (!Array.isArray(product.型號)) return;
            const productName = product.商品名稱 || '';
            const fallbackUrl = String(productId || '') === '16790492139'
                ? getProductAlibabaFallbackUrl(product, productName)
                : '';
            product.型號.forEach(modelData => {
                const restock = calculateModelRestock(product, modelData, months);
                if (restock.suggestedQty <= 0) return;
                const binding = getAlibabaBinding(productId, modelData, productName, modelData.型號名稱 || '');
                items.push({
                    productId,
                    modelId: modelData.規格ID || '',
                    productName,
                    modelName: modelData.型號名稱 || '',
                    monthlySales: restock.monthlySales,
                    currentStock: restock.currentStock,
                    targetStock: restock.targetStock,
                    suggestedQty: restock.suggestedQty,
                    alibabaProductUrl: binding.alibabaProductUrl || fallbackUrl,
                    alibabaOfferId: binding.alibabaOfferId,
                    alibabaSkuId: binding.alibabaSkuId,
                    alibabaSkuName: binding.alibabaSkuName,
                    alibabaSkuSecondName: binding.alibabaSkuSecondName
                });
            });
        });
        return { months, items };
    }

    function ensureProcurementDraftModal() {
        let modal = document.getElementById('procurementDraftModal');
        if (modal) return modal;
        modal = document.createElement('div');
        modal.id = 'procurementDraftModal';
        modal.className = 'alibaba-modal hidden';
        modal.innerHTML = `
            <div class="alibaba-modal-backdrop" data-procurement-close="true"></div>
            <div class="alibaba-modal-panel procurement-modal-panel" role="dialog" aria-modal="true" aria-labelledby="procurementDraftTitle">
                <div class="alibaba-modal-header">
                    <h3 id="procurementDraftTitle">1688 補貨草稿</h3>
                    <button type="button" class="alibaba-modal-close" data-procurement-close="true" aria-label="關閉">×</button>
                </div>
                <div id="procurementDraftSummary" class="procurement-summary"></div>
                <div id="procurementDraftContent" class="procurement-draft-content"></div>
                <div id="procurementDraftMessage" class="alibaba-edit-message" aria-live="polite"></div>
                <div class="alibaba-modal-actions">
                    <button type="button" class="btn-secondary" data-procurement-close="true">取消</button>
                    <button type="button" class="btn-secondary" id="startAlibabaCartButton">開啟 1688 加採購車</button>
                    <button type="button" class="btn-primary" id="submitProcurementDraftButton">確認建立 1688 訂單</button>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
        modal.addEventListener('click', function(e) {
            if (e.target.dataset.procurementClose === 'true') {
                modal.classList.add('hidden');
            }
        });
        modal.querySelector('#startAlibabaCartButton').addEventListener('click', startAlibabaCartFromCurrentDraft);
        modal.querySelector('#submitProcurementDraftButton').addEventListener('click', submitCurrentProcurementDraft);
        return modal;
    }

    function renderProcurementDraft(draft) {
        const modal = ensureProcurementDraftModal();
        window.currentProcurementDraft = draft;
        const summary = modal.querySelector('#procurementDraftSummary');
        const content = modal.querySelector('#procurementDraftContent');
        const submitButton = modal.querySelector('#submitProcurementDraftButton');
        const cartButton = modal.querySelector('#startAlibabaCartButton');
        const message = modal.querySelector('#procurementDraftMessage');
        const readyLines = draft.lines.filter(line => line.status === 'ready');
        const blockedLines = draft.lines.filter(line => line.status !== 'ready');
        const cartLines = draft.lines.filter(line => {
            const qty = Number(line.adjusted_qty || line.suggested_qty || 0);
            return qty > 0 && line.alibaba_product_url;
        });

        summary.innerHTML = `
            <div><strong>草稿 #${draft.id}</strong></div>
            <div>可下單 ${readyLines.length} 項，阻擋 ${blockedLines.length} 項</div>
            <div>調整後數量 ${draft.total_adjusted_qty}，預估 CNY ${Number(draft.total_amount_cny || 0).toFixed(2)}</div>
        `;

        content.innerHTML = draft.offer_groups.map(group => `
            <div class="procurement-offer-group">
                <div class="procurement-offer-header">offerId: ${group.alibaba_offer_id}</div>
                ${group.lines.map(line => `
                    <div class="procurement-line ${line.status !== 'ready' ? 'blocked' : ''}">
                        <div>
                            <strong>${line.shopee_product_name}</strong>
                            <span>${line.shopee_model_name}</span>
                        </div>
                        <div>建議 ${line.suggested_qty} → 採購 ${line.adjusted_qty}</div>
                        <div>MOQ ${line.alibaba_min_order_qty} / 倍數 ${line.alibaba_package_multiple}</div>
                        <div>${line.alibaba_sku_name || line.alibaba_sku_id || '瀏覽器將用型號名稱匹配'}</div>
                        <div>${line.alibaba_product_url ? '有 1688 URL' : '缺 1688 URL'}</div>
                        <div>${line.status === 'ready' ? `CNY ${Number(line.line_amount_cny || 0).toFixed(2)}` : line.blocker_reason}</div>
                    </div>
                `).join('')}
            </div>
        `).join('');

        if (draft.has_blockers) {
            message.textContent = '草稿仍有缺少 SKU 綁定或價格待確認的項目，不能送出。';
            message.className = 'alibaba-edit-message error';
            submitButton.disabled = true;
        } else if (!window.alibabaAuth || !window.alibabaAuth.can_create_order) {
            message.textContent = (window.alibabaAuth && window.alibabaAuth.message) || '1688 API 尚未授權，不能建立訂單。';
            message.className = 'alibaba-edit-message error';
            submitButton.disabled = true;
        } else {
            message.textContent = '確認後會建立 1688 待付款訂單。';
            message.className = 'alibaba-edit-message';
            submitButton.disabled = false;
        }
        if (cartButton) {
            cartButton.disabled = cartLines.length === 0;
            cartButton.title = cartLines.length > 0
                ? `開啟 1688 並嘗試加入 ${cartLines.length} 個型號到採購車，不會付款`
                : '缺少 1688 URL 或補貨數量，不能啟動採購車流程';
        }
        modal.classList.remove('hidden');
    }

    function createProcurementDraft() {
        if (!window.lastSearchResults) {
            alert('請先搜尋商品');
            return;
        }
        const { months, items } = collectProcurementItems();
        if (items.length === 0) {
            alert('目前沒有需要補貨的型號');
            return;
        }
        createProcurementDraftButton.disabled = true;
        createProcurementDraftButton.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 建立中';
        fetch('/api/procurement/drafts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                months,
                keyword: document.getElementById('searchInput')?.value || '',
                items
            })
        })
            .then(async response => {
                const data = await response.json();
                if (!response.ok) throw new Error(data.message || '建立草稿失敗');
                return data;
            })
            .then(data => renderProcurementDraft(data.draft))
            .catch(error => alert(error.message || '建立草稿失敗'))
            .finally(() => {
                createProcurementDraftButton.disabled = false;
                createProcurementDraftButton.innerHTML = '<i class="fas fa-file-invoice"></i> 建立補貨草稿';
            });
    }

    function submitCurrentProcurementDraft() {
        const draft = window.currentProcurementDraft;
        if (!draft) return;
        const modal = ensureProcurementDraftModal();
        const submitButton = modal.querySelector('#submitProcurementDraftButton');
        const message = modal.querySelector('#procurementDraftMessage');
        submitButton.disabled = true;
        message.textContent = '正在送出 1688 建單請求...';
        message.className = 'alibaba-edit-message';
        fetch(`/api/procurement/drafts/${draft.id}/submit`, { method: 'POST' })
            .then(async response => {
                const data = await response.json();
                if (!response.ok) throw new Error(data.message || '送出失敗');
                return data;
            })
            .then(data => {
                message.textContent = `已建立 1688 待付款訂單：${data.order.alibaba_order_id || data.order.id}`;
                message.className = 'alibaba-edit-message success';
                loadAlibabaBindings(true).then(rerenderCurrentProducts);
            })
            .catch(error => {
                message.textContent = error.message || '送出失敗';
                message.className = 'alibaba-edit-message error';
                submitButton.disabled = false;
            });
    }

    function startAlibabaCartFromCurrentDraft() {
        const draft = window.currentProcurementDraft;
        if (!draft) return;
        const modal = ensureProcurementDraftModal();
        const cartButton = modal.querySelector('#startAlibabaCartButton');
        const message = modal.querySelector('#procurementDraftMessage');
        cartButton.disabled = true;
        message.textContent = '正在啟動 1688 瀏覽器採購車流程...';
        message.className = 'alibaba-edit-message';
        fetch('/api/alibaba-restock', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                draftId: draft.id,
                addToCart: true,
                pauseSeconds: 300
            })
        })
            .then(async response => {
                const data = await response.json();
                if (!response.ok) throw new Error(data.message || '啟動 1688 採購車流程失敗');
                return data;
            })
            .then(data => {
                message.textContent = data.message || '已啟動 1688 採購車流程，請檢查開啟的瀏覽器。';
                message.className = 'alibaba-edit-message success';
            })
            .catch(error => {
                message.textContent = error.message || '啟動 1688 採購車流程失敗';
                message.className = 'alibaba-edit-message error';
                cartButton.disabled = false;
            });
    }

    function syncProcurementOrders() {
        syncProcurementOrdersButton.disabled = true;
        fetch('/api/procurement/orders/sync', { method: 'POST' })
            .then(r => r.json())
            .then(data => alert(data.message || `同步完成，更新 ${data.updated || 0} 筆`))
            .catch(error => alert(error.message || '同步失敗'))
            .finally(() => {
                syncProcurementOrdersButton.disabled = false;
            });
    }
    
    // 整體庫存水位統計計算函數
    function calculateInventoryStatistics(products, advancedKeyword = '', searchOption = 'product') {
        if (!products || typeof products !== 'object' || Object.keys(products).length === 0) {
            return {
                totalProducts: 0,
                totalModels: 0,
                totalStock: 0,
                totalMonthlySales: 0,
                inventoryLevels: [],
                avgLevel: 0,
                stdDev: 0,
                minLevel: 0,
                maxLevel: 0,
                overallLevel: 0,
                levelDistribution: { low: 0, medium: 0, high: 0 },
                modelsNeedingRestock: 0
            };
        }
        
        // 過濾商品（如果有進階搜尋）
        let filteredProducts = products;
        if (advancedKeyword && advancedKeyword.trim() !== '') {
            const keyword = advancedKeyword.trim().toLowerCase();
            filteredProducts = {};
            
            Object.entries(products).forEach(([productId, product]) => {
                let shouldInclude = false;
                
                if (searchOption === 'product' || searchOption === 'both') {
                    if (product.商品名稱 && product.商品名稱.toLowerCase().includes(keyword)) {
                        shouldInclude = true;
                    }
                }
                
                if ((searchOption === 'model' || searchOption === 'both') && !shouldInclude) {
                    if (product.型號 && Array.isArray(product.型號)) {
                        for (const model of product.型號) {
                            if (model.型號名稱 && model.型號名稱.toLowerCase().includes(keyword)) {
                                shouldInclude = true;
                                break;
                            }
                        }
                    }
                }
                
                if (shouldInclude) {
                    filteredProducts[productId] = product;
                }
            });
        }
        
        // 獲取過濾條件
        const inventoryMonth = document.getElementById('inventoryMonth')?.value || '4';
        const filterMode = document.getElementById('filterMode')?.checked || false;
        
        let totalProducts = 0;
        let totalModels = 0;
        let totalStock = 0;
        let totalMonthlySales = 0;
        let inventoryLevels = [];
        let modelsNeedingRestock = 0;
        let criticalModels = 0;    // 庫存水位 < 1.5 個月（即將斷貨）
        let zeroStockModels = 0;   // 庫存 = 0 且有月銷量（已缺貨）
        let totalActiveModels = 0; // 有月銷量的型號總數（作為比例分母）
        
        // 計算統計數據
        Object.entries(filteredProducts).forEach(([productId, product]) => {
            if (product.型號 && Array.isArray(product.型號)) {
                let productHasVisibleModels = false;
                
                product.型號.forEach((modelData) => {
                    let monthlyRate = parseInt(modelData.月銷量, 10) || 0;
                    const months = parseInt(inventoryMonth, 10) || 0;
                    const currentStock = parseInt(modelData.商品庫存, 10) || 0;
                    
                    // 歷史佔比法預估月銷量
                    if (currentStock === 0 && monthlyRate === 0) {
                        const modelHistoricalSales = parseInt(modelData.已售出數量, 10) || 0;
                        const productTotalHistoricalSales = parseInt(product.已售出總數量, 10) || 0;
                        const productTotalMonthlySales = parseInt(product.總月銷量, 10) || 0;
                        
                        if (modelHistoricalSales > 0 && productTotalHistoricalSales > 0 && productTotalMonthlySales > 0) {
                            const historicalRatio = modelHistoricalSales / productTotalHistoricalSales;
                            monthlyRate = Math.round(productTotalMonthlySales * historicalRatio * 10) / 10;
                        }
                    }
                    
                    const expectedStock = Math.round(monthlyRate * months);
                    
                    // 過濾邏輯（與 displayProducts 相同）：
                    // 1. 有月銷量時：若庫存 >= 預期庫存，代表充足，隱藏
                    // 2. 月銷量為 0 時：若庫存 > 0，代表不需補貨，隱藏；庫存也為 0 則顯示
                    if (filterMode) {
                        if (expectedStock > 0 && currentStock >= expectedStock) {
                            return;
                        }
                        if (expectedStock === 0 && currentStock > 0) {
                            return;
                        }
                    }
                    if (currentStock < expectedStock && expectedStock > 0) {
                        modelsNeedingRestock++;
                    }

                    // 統計危急型號
                    if (monthlyRate > 0) {
                        totalActiveModels++;
                        const stockLevel = currentStock / monthlyRate; // 庫存可支撐月數
                        if (currentStock === 0) {
                            zeroStockModels++;
                            criticalModels++; // 零庫存必定是危急
                        } else if (stockLevel < 1.5) {
                            criticalModels++; // 不到 1.5 個月庫存
                        }
                    }
                    
                    productHasVisibleModels = true;
                    totalModels++;
                    totalStock += currentStock;
                    totalMonthlySales += monthlyRate;
                    
                    // 計算此型號的庫存水位（月數）
                    if (monthlyRate > 0) {
                        const level = currentStock / monthlyRate;
                        inventoryLevels.push({
                            level: level,
                            weight: monthlyRate, // 以月銷量作為權重
                            modelName: modelData.型號名稱,
                            productName: product.商品名稱
                        });
                    }
                });
                
                if (productHasVisibleModels) {
                    totalProducts++;
                }
            }
        });
        
        // 計算統計指標
        let avgLevel = 0;
        let stdDev = 0;
        let minLevel = 0;
        let maxLevel = 0;
        let overallLevel = 0;
        let levelDistribution = { low: 0, medium: 0, high: 0 };
        
        if (inventoryLevels.length > 0) {
            // 計算加權平均庫存水位
            let weightedSum = 0;
            let totalWeight = 0;
            
            inventoryLevels.forEach(item => {
                weightedSum += item.level * item.weight;
                totalWeight += item.weight;
            });
            
            avgLevel = totalWeight > 0 ? weightedSum / totalWeight : 0;
            
            // 計算標準差
            let variance = 0;
            inventoryLevels.forEach(item => {
                variance += Math.pow(item.level - avgLevel, 2) * item.weight;
            });
            stdDev = totalWeight > 0 ? Math.sqrt(variance / totalWeight) : 0;
            
            // 計算最小值和最大值
            const levels = inventoryLevels.map(item => item.level);
            minLevel = Math.min(...levels);
            maxLevel = Math.max(...levels);
            
            // 計算整體庫存水位（基於總庫存和總月銷量）
            overallLevel = totalMonthlySales > 0 ? totalStock / totalMonthlySales : 0;
            
            // 計算庫存水位分布
            inventoryLevels.forEach(item => {
                if (item.level < 3) {
                    levelDistribution.low += item.weight;
                } else if (item.level < 6) {
                    levelDistribution.medium += item.weight;
                } else {
                    levelDistribution.high += item.weight;
                }
            });
        }
        
        return {
            totalProducts,
            totalModels,
            totalStock,
            totalMonthlySales,
            inventoryLevels,
            avgLevel: Math.round(avgLevel * 10) / 10,
            stdDev: Math.round(stdDev * 10) / 10,
            minLevel: Math.round(minLevel * 10) / 10,
            maxLevel: Math.round(maxLevel * 10) / 10,
            overallLevel: Math.round(overallLevel * 10) / 10,
            levelDistribution,
            modelsNeedingRestock,
            criticalModels,
            zeroStockModels,
            totalActiveModels
        };
    }
    
    // 更新儀表板UI
    function updateDashboardUI(products, advancedKeyword = '', searchOption = 'product') {
        const dashboardSummary = document.getElementById('dashboardSummary');
        if (!dashboardSummary) return;
        
        // 確保儀表板總是可見
        dashboardSummary.style.display = 'block';
        
        const stats = calculateInventoryStatistics(products, advancedKeyword, searchOption);
        
        // 更新指標數據
        document.getElementById('restockModelsCount').textContent = stats.modelsNeedingRestock;
        document.getElementById('dashboardTotalSales').textContent = Math.round(stats.totalMonthlySales);
        document.getElementById('dashboardTotalStock').textContent = stats.totalStock;

        // 需補貨型號 label：顯示 「需補貨 / 共 N 型號」
        const restockLabel = document.getElementById('restockModelsLabel');
        if (restockLabel) {
            restockLabel.textContent = `需補貨 / 共 ${stats.totalModels} 型號`;
        }

        // 平均庫存水位：總庫存 / 總月銷量，帶顏色指示
        const avgLevelEl = document.getElementById('dashboardAvgLevel');
        const avgLevelIcon = document.getElementById('avgLevelIconWrapper');
        if (avgLevelEl && stats.totalMonthlySales > 0) {
            const level = Math.round((stats.totalStock / stats.totalMonthlySales) * 10) / 10;
            avgLevelEl.textContent = `${level} 月`;

            // 顏色：< 1.5月→紅，1.5~3月→橘，> 3月→綠
            if (level < 1.5) {
                avgLevelEl.style.color = '#e74c3c';
                if (avgLevelIcon) avgLevelIcon.className = 'metric-icon alert-icon';
            } else if (level < 3) {
                avgLevelEl.style.color = '#e67e22';
                if (avgLevelIcon) avgLevelIcon.className = 'metric-icon warning-icon';
            } else {
                avgLevelEl.style.color = '';
                if (avgLevelIcon) avgLevelIcon.className = 'metric-icon success-icon';
            }
        } else if (avgLevelEl) {
            avgLevelEl.textContent = '-';
            if (avgLevelIcon) avgLevelIcon.className = 'metric-icon';
        }

        // 判定狀態
        const statusCard = document.getElementById('overallStatusCard');
        const statusIcon = document.getElementById('statusIcon');
        const statusText = document.getElementById('overallStatusText');
        const statusDesc = document.getElementById('overallStatusDesc');
        
        // 移除舊的狀態類別
        statusCard.classList.remove('status-healthy', 'status-warning', 'status-critical');

        // 用 criticalModels（庫存水位 < 1.5 個月）佔有銷量型號的比例判定狀態
        const criticalRatio = stats.totalActiveModels > 0
            ? stats.criticalModels / stats.totalActiveModels
            : 0;
        const criticalPct = Math.round(criticalRatio * 100);

        if (criticalRatio > 0.30) {
            // 危急：超過 30% 的型號即將或已斷貨
            statusCard.classList.add('status-critical');
            statusIcon.className = 'fas fa-times-circle';
            statusText.textContent = '危急';
            const zeroDesc = stats.zeroStockModels > 0 ? `（其中 ${stats.zeroStockModels} 個已缺貨）` : '';
            statusDesc.textContent = `${stats.criticalModels} 個型號庫存不足 1.5 個月（佔 ${criticalPct}%）${zeroDesc}，急需補貨！`;
        } else if (criticalRatio > 0.10) {
            // 需注意：超過 10% 的型號庫存偏低
            statusCard.classList.add('status-warning');
            statusIcon.className = 'fas fa-exclamation-circle';
            statusText.textContent = '需注意';
            statusDesc.textContent = `${stats.criticalModels} 個型號庫存不足 1.5 個月（佔 ${criticalPct}%），建議優先補貨`;
        } else {
            // 健康
            statusCard.classList.add('status-healthy');
            statusIcon.className = 'fas fa-check-circle';
            statusText.textContent = '健康';
            if (stats.criticalModels > 0) {
                statusDesc.textContent = `整體庫存良好，仍有 ${stats.criticalModels} 個型號待留意`;
            } else {
                statusDesc.textContent = `所有型號庫存充足，平均可支撐 ${stats.avgLevel} 個月`;
            }
        }
    }
    

    
    // 修改狀態消息更新邏輯，加入爬取月銷量的階段
    function updateStatusMessage(progress) {
        if (progress < 15) {
            statusMessage.textContent = '正在載入頁面...';
        } else if (progress < 30) {
            statusMessage.textContent = '正在展開商品型號...';
        } else if (progress < 45) {
            statusMessage.textContent = '正在提取商品資訊...';
        } else if (progress < 60) {
            statusMessage.textContent = '正在處理商品圖片...';
        } else if (progress < 75) {
            statusMessage.textContent = '正在爬取月銷量數據...';
        } else if (progress < 85) {
            statusMessage.textContent = '正在分析銷售趨勢...';
        } else {
            statusMessage.textContent = '即將完成...';
        }
    }
    
    // 修改進度模擬函數，使其更準確地反映爬蟲過程
    function startProgressSimulation() {
        let progress = 0;
        progressBar.style.width = '0%';
        progressText.textContent = '0%';
        
        // 使用更平滑的進度增長算法，考慮到爬取月銷量的時間
        const progressInterval = setInterval(() => {
            if (progress < 90) {
                // 調整進度增長速度，在60%-75%區間放慢速度，模擬爬取月銷量的耗時過程
                let increment;
                if (progress < 60) {
                    // 前期進度增長較快
                    increment = (60 - progress) / 12;
                } else if (progress < 75) {
                    // 爬取月銷量階段進度增長較慢
                    increment = (75 - progress) / 25;
                } else {
                    // 後期進度增長恢復正常
                    increment = (90 - progress) / 15;
                }
                
                progress += Math.max(0.3, increment);
                progress = Math.min(progress, 90);
                
                progressBar.style.width = progress + '%';
                progressText.textContent = Math.round(progress) + '%';
                
                // 更新狀態消息
                updateStatusMessage(progress);
            }
        }, 800);
        
        return progressInterval;
    }

    // 添加處理蝦皮圖片URL的函數
    function processShopeeImageUrl(url) {
        if (!url || url === 'undefined' || url === '未找到') {
            return 'https://via.placeholder.com/60?text=無圖片';
        }
        
        // 清理URL
        url = url.trim();
        
        // 如果URL不是以http或https開頭，添加https前綴
        if (!url.startsWith('http://') && !url.startsWith('https://')) {
            // 移除開頭的 //（如果有）
            if (url.startsWith('//')) {
                url = url.substring(2);
            }
            url = 'https://' + url;
        }
        
        // 特殊處理蝦皮圖片URL
        if (url.includes('shopee.tw/file/')) {
            // 確保URL不包含多餘的參數
            const urlParts = url.split('?');
            url = urlParts[0];
            
            // 確保URL包含正確的圖片尺寸標記（如果沒有_tn後綴，添加它）
            if (!url.endsWith('_tn')) {
                url += '_tn';
            }
        }
        
        return url;
    }
    
    // 添加圖片預加載函數
    function preloadImage(url, callback) {
        const processedUrl = processShopeeImageUrl(url);
        
        if (processedUrl === 'https://via.placeholder.com/60?text=無圖片') {
            callback(false);
            return;
        }
        
        const img = new Image();
        img.onload = function() {
            callback(true, processedUrl);
        };
        img.onerror = function() {
            console.error('圖片預加載失敗:', processedUrl);
            callback(false);
        };
        img.src = processedUrl;
    }
    
    // 顯示商品資料（修改為支持進階搜尋和搜尋選項）
    function displayProducts(products, advancedKeyword = '', searchOption = 'product') {
        const productList = document.getElementById('productList');
        productList.innerHTML = '';
        window.currentRestockProducts = [];
        updateBatchRestockToolbar();
        
        // 獲取過濾條件
        const inventoryMonth = document.getElementById('inventoryMonth')?.value || '4';
        const filterMode = document.getElementById('filterMode')?.checked || false;

        if (products && typeof products === 'object' && (products.error || products.status === 'error')) {
            const message = products.error || products.message || '爬蟲執行失敗';
            productList.innerHTML = `
                <tr>
                    <td colspan="5">
                        <div class="empty-state">
                            <i class="fas fa-exclamation-triangle"></i>
                            <p>${escapeHtml(message)}</p>
                        </div>
                    </td>
                </tr>
            `;
            console.error('搜尋失敗', products);
            return;
        }
        
        // 檢查 products 是否有效
        if (!products || typeof products !== 'object' || Object.keys(products).length === 0) {
            productList.innerHTML = `
                <tr>
                    <td colspan="5">
                        <div class="empty-state">
                            <i class="fas fa-search"></i>
                            <p>沒有找到符合的商品</p>
                        </div>
                    </td>
                </tr>
            `;
            console.error('無商品資料或資料格式錯誤', products);
            return;
        }
        
        // 如果有進階搜尋關鍵字，則對商品進行過濾
        let filteredProducts = products;
        let filteredProductCount = Object.keys(products).length;
        
        if (advancedKeyword && advancedKeyword.trim() !== '') {
            const keyword = advancedKeyword.trim().toLowerCase();
            
            // 過濾符合關鍵字的商品
            filteredProducts = {};
            
            Object.entries(products).forEach(([productId, product]) => {
                let shouldInclude = false;
                
                // 根據搜尋選項過濾
                if (searchOption === 'product' || searchOption === 'both') {
                    // 檢查商品名稱是否包含關鍵字
                    if (product.商品名稱 && product.商品名稱.toLowerCase().includes(keyword)) {
                        shouldInclude = true;
                    }
                }
                
                if ((searchOption === 'model' || searchOption === 'both') && !shouldInclude) {
                    // 檢查型號名稱是否包含關鍵字
                    if (product.型號 && Array.isArray(product.型號)) {
                        for (const model of product.型號) {
                            if (model.型號名稱 && model.型號名稱.toLowerCase().includes(keyword)) {
                                shouldInclude = true;
                                break;
                            }
                        }
                    }
                }
                
                if (shouldInclude) {
                    filteredProducts[productId] = product;
                }
            });
            
            filteredProductCount = Object.keys(filteredProducts).length;
            
            // 顯示進階搜尋結果信息
            const tableContainer = document.querySelector('.table-container');
            
            // 移除舊的結果信息（如果有）
            const oldInfo = document.querySelector('.search-results-info');
            if (oldInfo) {
                oldInfo.remove();
            }
            
            // 添加新的結果信息
            const resultsInfo = document.createElement('div');
            resultsInfo.className = 'search-results-info';
            
            // 根據搜尋選項顯示不同的搜尋範圍描述
            let searchScopeText = '';
            if (searchOption === 'product') {
                searchScopeText = '商品名稱';
            } else if (searchOption === 'model') {
                searchScopeText = '型號名稱';
            } else {
                searchScopeText = '商品和型號名稱';
            }
            
            resultsInfo.innerHTML = `
                <i class="fas fa-filter"></i>
                <span>進階搜尋「${advancedKeyword}」(${searchScopeText}): 找到 ${filteredProductCount} 個符合的商品，共 ${Object.keys(products).length} 個</span>
            `;
            
            tableContainer.insertBefore(resultsInfo, tableContainer.firstChild);
        }
        
        // console.log removed
        
        // 按總月銷量從大到小排序商品
        const sortedProducts = Object.entries(filteredProducts).sort(([, productA], [, productB]) => {
            const salesA = parseInt(productA.總月銷量 || '0', 10);
            const salesB = parseInt(productB.總月銷量 || '0', 10);
            return salesB - salesA; // 從大到小排序
        });
        
        // console.log removed
        
        // 使用文檔片段減少DOM重繪
        const fragment = document.createDocumentFragment();
        let totalVisibleProducts = 0;
        
        // 遍歷排序後的商品
        sortedProducts.forEach(([productId, product]) => {
            try {
                // 創建行元素
                const row = document.createElement('tr');

                // 計算商品整體庫存狀態（使用型號總庫存和總月銷量）
                let totalModelStock = 0;
                if (product.型號 && Array.isArray(product.型號)) {
                    product.型號.forEach(model => {
                        totalModelStock += parseInt(model.商品庫存, 10) || 0;
                    });
                }
                const totalMonthlySales = parseInt(product.總月銷量, 10) || 0;
                
                // 判斷狀態
                let statusText = '未知';
                let statusClass = 'badge-secondary';
                
                if (totalModelStock === 0 && totalMonthlySales > 0) {
                    statusText = '已缺貨';
                    statusClass = 'badge-danger';
                } else if (totalMonthlySales <= 0) {
                    if (totalModelStock <= 0) {
                        statusText = '待確認';
                        statusClass = 'badge-secondary';
                    } else {
                        statusText = '低流動';
                        statusClass = 'badge-success';
                    }
                } else {
                    const stockMonths = Math.round(totalModelStock / totalMonthlySales * 10) / 10;
                    if (stockMonths < 1) {
                        statusText = '危急';
                        statusClass = 'badge-danger';
                    } else if (stockMonths < 2) {
                        statusText = '偏低';
                        statusClass = 'badge-warning';
                    } else if (stockMonths < 3) {
                        statusText = '正常';
                        statusClass = 'badge-success';
                    } else {
                        statusText = '充足';
                        statusClass = 'badge-success';
                    }
                }
                
                // 狀態欄位
                const statusCell = document.createElement('td');
                const statusBadge = document.createElement('span');
                statusBadge.className = `badge ${statusClass}`;
                statusBadge.textContent = statusText;
                statusCell.appendChild(statusBadge);
                row.appendChild(statusCell);

                // 商品名稱和圖片
                const nameCell = document.createElement('td');
                const nameDiv = document.createElement('div');
                nameDiv.className = 'product-name';

                const img = document.createElement('img');
                img.className = 'product-image';
                
                // 修正圖片URL處理邏輯
                let imgSrc = processShopeeImageUrl(product.商品圖片網址);
                
                img.src = imgSrc;
                img.alt = product.商品名稱 || '未知商品';
                img.crossOrigin = "anonymous"; // 添加跨域屬性
                img.onerror = function() { 
                    console.error('圖片載入失敗:', this.src);
                    this.src = 'https://via.placeholder.com/60?text=無圖片'; 
                    this.onerror = null; 
                };
                
                // 預加載圖片以確保顯示
                if (imgSrc !== 'https://via.placeholder.com/60?text=無圖片') {
                    preloadImage(product.商品圖片網址, function(success, url) {
                        if (success) {
                            img.src = url; // 使用預加載成功的URL
                        } else {
                            img.src = 'https://via.placeholder.com/60?text=無圖片';
                        }
                    });
                }
                
                nameDiv.appendChild(img);

                const nameText = document.createElement('span');
                nameText.textContent = product.商品名稱 || '未知商品名稱';
                nameDiv.appendChild(nameText);
                nameCell.appendChild(nameDiv);

                // 型號資訊 - 處理數組形式的型號數據
                let visibleModelCount = 0; // 用於計算可見型號數量
                
                if (product.型號 && Array.isArray(product.型號) && product.型號.length > 0) {
                    const modelsDiv = document.createElement('div');
                    modelsDiv.className = 'model-info';
                    const productRestockItems = [];
                    const productRestockBlockers = [];
                    const productAlibabaFallbackUrl = getProductAlibabaFallbackUrl(product, product.商品名稱 || '');

                    product.型號.forEach((modelData, index) => {
                        try {
                            // 計算預期庫存 = 月銷量 * 庫存月份
                            const monthlyRate = parseInt(modelData.月銷量, 10) || 0;
                            const months = parseInt(inventoryMonth, 10) || 0;
                            const expectedStock = monthlyRate * months;
                            
                            // 當前庫存
                            const currentStock = parseInt(modelData.商品庫存, 10) || 0;
                            
                            // 過濾邏輯：
                            // 1. 有月銷量時：若庫存 >= 預期庫存，代表充足，隱藏
                            // 2. 月銷量為 0 時：若庫存 > 0，代表不需補貨，隱藏；庫存也為 0 則顯示
                            if (filterMode) {
                                if (expectedStock > 0 && currentStock >= expectedStock) {
                                    return; // 庫存充足，隱藏
                                }
                                if (expectedStock === 0 && currentStock > 0) {
                                    return; // 月銷量為 0 但有庫存，不需補貨，隱藏
                                }
                            }
                            
                            visibleModelCount++; // 增加可見型號計數
                            
                            const modelItem = document.createElement('div');
                            modelItem.className = 'model-item';
                            
                            // 如果需要補貨，添加特殊樣式
                            if (currentStock < expectedStock && expectedStock > 0) {
                                modelItem.classList.add('restock-needed');
                            }

                            // 型號圖片
                            if (modelData.型號圖片網址 && modelData.型號圖片網址 !== '未找到') {
                                const modelImg = document.createElement('img');
                                
                                // 修正型號圖片URL處理邏輯
                                let modelImgSrc = processShopeeImageUrl(modelData.型號圖片網址);
                                
                                modelImg.src = modelImgSrc;
                                modelImg.className = 'model-image';
                                modelImg.crossOrigin = "anonymous"; // 添加跨域屬性
                                modelImg.onerror = function() { 
                                    console.error('型號圖片載入失敗:', this.src);
                                    this.src = 'https://via.placeholder.com/30?text=無圖片'; 
                                    this.onerror = null; 
                                };
                                
                                // 預加載型號圖片以確保顯示
                                if (modelImgSrc !== 'https://via.placeholder.com/30?text=無圖片') {
                                    preloadImage(modelData.型號圖片網址, function(success, url) {
                                        if (success) {
                                            modelImg.src = url; // 使用預加載成功的URL
                                        } else {
                                            modelImg.src = 'https://via.placeholder.com/30?text=無圖片';
                                        }
                                    });
                                }
                                
                                // 添加圖片到DOM
                                modelItem.appendChild(modelImg);
                            }

                            // 型號文字信息
                            const modelText = document.createElement('span');
                            modelText.textContent = modelData.型號名稱 || `型號 ${index + 1}`;
                            
                            // 添加換行
                            modelText.appendChild(document.createElement('br'));
                            
                            // 庫存標籤
                            const stock = modelData.商品庫存 !== undefined ? modelData.商品庫存 : '未知';
                            const stockNum = parseInt(stock, 10);
                            const stockBadge = document.createElement('span');
                            if (isNaN(stockNum)) {
                                stockBadge.className = 'badge badge-secondary'; // 未知庫存，使用灰色
                            } else if (monthlyRate <= 0) {
                                // 月銷量為 0，無法計算月數，以庫存是否為 0 判斷
                                stockBadge.className = stockNum === 0 ? 'badge badge-danger' : 'badge badge-success';
                            } else if (stockNum < monthlyRate) {
                                stockBadge.className = 'badge badge-danger';   // 不足 1 個月，紅色
                            } else if (stockNum < monthlyRate * 2) {
                                stockBadge.className = 'badge badge-warning';  // 1~2 個月，橘色
                            } else {
                                stockBadge.className = 'badge badge-success';  // 超過 2 個月，綠色
                            }
                            stockBadge.textContent = `庫存: ${stock}`;
                            modelText.appendChild(stockBadge);
                            
                            // 添加空格
                            modelText.appendChild(document.createTextNode(' '));
                            
                            // 銷售量標籤
                            const salesBadge = document.createElement('span');
                            salesBadge.className = 'badge badge-primary';
                            salesBadge.textContent = `已售: ${modelData.已售出數量 || '0'}`;
                            modelText.appendChild(salesBadge);
                            
                            // 添加空格
                            modelText.appendChild(document.createTextNode(' '));
                            
                            // 月銷量標籤
                            const monthlySalesBadge = document.createElement('span');
                            monthlySalesBadge.className = 'badge badge-info';
                            monthlySalesBadge.textContent = `月銷: ${modelData.月銷量 || '0'}`;
                            modelText.appendChild(monthlySalesBadge);
                            
                            let suggestedRestockForAction = 0;

                            // 顯示預期庫存和建議補貨
                            // 特殊處理:當庫存為0且月銷量也為0時,使用歷史佔比法估算
                            let effectiveMonthlyRate = monthlyRate;
                            let isEstimated = false;
                            
                            if (currentStock === 0 && monthlyRate === 0) {
                                // 計算歷史佔比法的預估月銷量
                                const modelHistoricalSales = parseInt(modelData.已售出數量, 10) || 0;
                                const productTotalHistoricalSales = parseInt(product.已售出總數量, 10) || 0;
                                const productTotalMonthlySales = parseInt(product.總月銷量, 10) || 0;
                                
                                // 只有當有歷史銷售數據時才計算
                                if (modelHistoricalSales > 0 && productTotalHistoricalSales > 0 && productTotalMonthlySales > 0) {
                                    // 計算歷史佔比 = 該型號歷史總銷量 / 商品全部型號歷史總銷量
                                    const historicalRatio = modelHistoricalSales / productTotalHistoricalSales;
                                    
                                    // 預估月銷量 = 該商品總月銷量 × 歷史佔比
                                    effectiveMonthlyRate = Math.round(productTotalMonthlySales * historicalRatio * 10) / 10;
                                    isEstimated = true;
                                    
        // console.log removed
                                }
                            }
                            
                            // 如果有有效的月銷量(實際或預估),則顯示預期庫存和建議補貨
                            if (effectiveMonthlyRate > 0 || modelData.月銷量) {
                                // 重新計算預期庫存(使用有效月銷量)
                                const effectiveExpectedStock = Math.round(effectiveMonthlyRate * months);
                                
                                // 添加空格
                                modelText.appendChild(document.createTextNode(' '));
                                
                                // 預期庫存標籤
                                const expectedStockBadge = document.createElement('span');
                                expectedStockBadge.className = 'badge badge-secondary';
                                expectedStockBadge.textContent = `預期庫存: ${effectiveExpectedStock}`;
                                if (isEstimated) {
                                    expectedStockBadge.title = `基於歷史佔比法預估 (預估月銷量: ${effectiveMonthlyRate})`;
                                }
                                modelText.appendChild(expectedStockBadge);
                                
                                // 計算建議補貨並以 10 為單位四捨五入，和送到 1688 的數量保持一致。
                                const rawSuggestedRestock = Math.max(effectiveExpectedStock - currentStock, 0);
                                const suggestedRestock = roundRestockQty(rawSuggestedRestock);
                                suggestedRestockForAction = suggestedRestock;
                                
                                // 只有當建議補貨為正數時才顯示
                                if (suggestedRestock > 0) {
                                    // 添加空格
                                    modelText.appendChild(document.createTextNode(' '));
                                    
                                    // 建議補貨標籤
                                    const restockBadge = document.createElement('span');
                                    restockBadge.className = 'badge badge-danger';
                                    restockBadge.textContent = `建議補貨: ${suggestedRestock}`;
                                    if (rawSuggestedRestock !== suggestedRestock) {
                                        restockBadge.title = `原始建議 ${rawSuggestedRestock}，已依 10 件單位四捨五入`;
                                    }
                                    if (isEstimated) {
                                        restockBadge.textContent += ' (預估)';
                                        restockBadge.title = `${restockBadge.title ? restockBadge.title + '；' : ''}基於歷史佔比法預估 (預估月銷量: ${effectiveMonthlyRate})`;
                                    }
                                    restockBadge.style.color = 'red';
                                    restockBadge.style.fontWeight = 'bold';
                                    modelText.appendChild(restockBadge);
                                }
                            }

                            const binding = getAlibabaBinding(productId, modelData, product.商品名稱 || '', modelData.型號名稱 || '');
                            modelText.appendChild(document.createTextNode(' '));
                            const bindingBadge = document.createElement('span');
                            bindingBadge.className = binding.alibabaBindingStatus === 'ready' ? 'badge badge-success' : 'badge badge-warning';
                            bindingBadge.textContent = binding.alibabaBindingStatus === 'ready' ? '1688 已綁定' : '1688 未完整';
                            bindingBadge.title = binding.alibabaBindingStatus === 'ready'
                                ? `offerId: ${binding.alibabaOfferId}, SKU 名稱：${binding.alibabaSkuName}${binding.alibabaSkuSecondName ? ` → ${binding.alibabaSkuSecondName}` : ''}`
                                : '需補齊 offerId 與 1688 SKU 名稱（第二名稱如有則一併填寫）後才能建立 1688 採購';
                            modelText.appendChild(bindingBadge);

                            if (binding.alibabaLastPriceCny !== null) {
                                modelText.appendChild(document.createTextNode(' '));
                                const priceBadge = document.createElement('span');
                                priceBadge.className = 'badge badge-info';
                                priceBadge.textContent = `CNY ${binding.alibabaLastPriceCny}`;
                                modelText.appendChild(priceBadge);
                            }

                            // 阿里巴巴連結按鈕
                            const specId = modelData.規格ID || '';
                            const alibabaLink = binding.alibabaProductUrl ||
                                getAlibabaLink(modelData, product.商品名稱 || '', modelData.型號名稱 || '') ||
                                productAlibabaFallbackUrl;
                            if (alibabaLink) {
                                // 添加空格
                                modelText.appendChild(document.createTextNode(' '));

                                const alibabaBtn = document.createElement('a');
                                alibabaBtn.href = alibabaLink;
                                alibabaBtn.target = '_blank';
                                alibabaBtn.rel = 'noopener noreferrer';
                                alibabaBtn.className = 'badge badge-alibaba';
                                alibabaBtn.textContent = '🔗 阿里巴巴';
                                alibabaBtn.title = `規格ID: ${specId}`;
                                alibabaBtn.onclick = function(e) {
                                    e.stopPropagation();
                                };
                                modelText.appendChild(alibabaBtn);
                            }

                            modelText.appendChild(document.createTextNode(' '));
                            const skuMappingBtn = document.createElement('button');
                            skuMappingBtn.type = 'button';
                            skuMappingBtn.className = `badge badge-1688-sku ${binding.alibabaSkuName ? 'is-mapped' : 'is-missing'}`;
                            const needsSecondSku = requiresAlibabaSecondSku(
                                product.商品名稱 || '',
                                modelData.型號名稱 || ''
                            );
                            const missingSecondSku = binding.alibabaSkuName && needsSecondSku && !binding.alibabaSkuSecondName;
                            const discontinuedSku = isAlibabaSkuDiscontinued(binding.alibabaSkuName);
                            skuMappingBtn.textContent = binding.alibabaSkuName
                                ? (discontinuedSku
                                    ? `1688：${binding.alibabaSkuName}`
                                    : `1688型號：${binding.alibabaSkuName}${binding.alibabaSkuSecondName ? ` / ${binding.alibabaSkuSecondName}` : ''}${missingSecondSku ? '（缺手機型號）' : ''}`)
                                : '＋ 1688 對應型號';
                            skuMappingBtn.title = binding.alibabaSkuName
                                ? (discontinuedSku
                                    ? '此型號已停售，不會加入 1688 補貨流程'
                                    : (missingSecondSku
                                    ? '已對應第一規格，但 1688 未找到可安全使用的手機型號；不能一鍵補貨'
                                    : '點擊修改寫入 golden_table.json 的 1688 對應型號'))
                                : '點擊新增 1688 對應型號並寫入 golden_table.json';
                            skuMappingBtn.onclick = function(e) {
                                e.stopPropagation();
                                open1688SkuEditor(productId, product, modelData, alibabaLink);
                            };
                            modelText.appendChild(skuMappingBtn);

                            if (suggestedRestockForAction > 0) {
                                if (alibabaLink && hasCompleteAlibabaSkuSelection(binding, product.商品名稱 || '', modelData.型號名稱 || '')) {
                                    productRestockItems.push({
                                        specId: String(modelData.規格ID || ''),
                                        modelName: String(modelData.型號名稱 || ''),
                                        alibabaSkuName: binding.alibabaSkuName,
                                        alibabaSkuSecondName: binding.alibabaSkuSecondName,
                                        restockQty: suggestedRestockForAction,
                                        alibabaUrl: alibabaLink
                                    });
                                } else {
                                    productRestockBlockers.push({
                                        modelName: String(modelData.型號名稱 || ''),
                                        reason: !alibabaLink
                                            ? '缺少 1688 URL'
                                            : (discontinuedSku
                                                ? `1688 ${binding.alibabaSkuName}`
                                                : (requiresAlibabaSecondSku(product.商品名稱 || '', modelData.型號名稱 || '') && !binding.alibabaSkuSecondName
                                                ? '缺少 1688 第二規格（手機型號）'
                                                : '缺少 1688 對應型號'))
                                    });
                                }
                            }

                            modelText.appendChild(document.createTextNode(' '));
                            const alibabaEditBtn = document.createElement('button');
                            alibabaEditBtn.type = 'button';
                            alibabaEditBtn.className = 'badge badge-edit-alibaba';
                            alibabaEditBtn.textContent = '編輯';
                            alibabaEditBtn.title = '編輯此型號的阿里巴巴資料';
                            alibabaEditBtn.onclick = function(e) {
                                e.stopPropagation();
                                openAlibabaEditor(productId, product, modelData);
                            };
                            modelText.appendChild(alibabaEditBtn);

                            modelItem.appendChild(modelText);
                            modelsDiv.appendChild(modelItem);
                        } catch (modelError) {
                            console.error(`處理商品 ${productId} 的型號 ${index} 時出錯:`, modelError);
                        }
                    });
                    
                    // 只有當有可見型號時才添加到DOM
                    if (visibleModelCount > 0) {
                        if (productRestockItems.length > 0 || productRestockBlockers.length > 0) {
                            const batchActions = document.createElement('div');
                            batchActions.className = 'model-batch-actions';

                            const batchRestockBtn = document.createElement('button');
                            batchRestockBtn.type = 'button';
                            batchRestockBtn.className = 'badge badge-alibaba-restock batch-restock-button';
                            const readyCount = productRestockItems.length;
                            const blockerCount = productRestockBlockers.length;
                            batchRestockBtn.textContent = blockerCount > 0
                                ? (readyCount > 0
                                    ? `1688一鍵加購物車（${readyCount} 個可加入；${blockerCount} 型號未完成）`
                                    : `1688一鍵加購物車（${blockerCount} 型號未完成）`)
                                : `1688一鍵補貨加採購車 (${readyCount})`;
                            batchRestockBtn.disabled = readyCount === 0;
                            batchRestockBtn.title = blockerCount > 0
                                ? `會加入 ${readyCount} 個已完成型號；以下 ${blockerCount} 個會略過：\n${productRestockBlockers.map(item => `${item.modelName}：${item.reason}`).join('\n')}`
                                : '開啟 1688，每個型號填入數量後逐一按「加采购车」';
                            batchRestockBtn.onclick = function(e) {
                                e.stopPropagation();
                                startAlibabaRestock(productId, product, productRestockItems, {
                                    addToCart: true,
                                    skippedCount: blockerCount
                                });
                            };
                            batchActions.appendChild(batchRestockBtn);

                            if (readyCount > 0) {
                                const adjustRestockBtn = document.createElement('button');
                                adjustRestockBtn.type = 'button';
                                adjustRestockBtn.className = 'badge badge-restock-adjust';
                                adjustRestockBtn.textContent = '微調數量';
                                adjustRestockBtn.title = '逐一調整每個型號的補貨數量';
                                adjustRestockBtn.onclick = function(e) {
                                    e.stopPropagation();
                                    openRestockAdjustmentModal(
                                        productId,
                                        product,
                                        productRestockItems,
                                        blockerCount
                                    );
                                };
                                batchActions.appendChild(adjustRestockBtn);
                            }
                            modelsDiv.insertBefore(batchActions, modelsDiv.firstChild);

                            window.currentRestockProducts.push({
                                productId,
                                product,
                                items: productRestockItems.map(item => ({ ...item })),
                                blockerCount
                            });
                        }
                        nameCell.appendChild(modelsDiv);
                    }
                }

                // 如果過濾後沒有可見型號，則跳過此商品
                if (product.型號 && Array.isArray(product.型號) && product.型號.length > 0 && visibleModelCount === 0) {
                    return;
                }

                row.appendChild(nameCell);

                // 已售出總數量
                const salesCell = document.createElement('td');
                const salesBadge = document.createElement('span');
                salesBadge.className = 'badge badge-primary';
                salesBadge.textContent = product.已售出總數量 || '0';
                salesCell.appendChild(salesBadge);
                row.appendChild(salesCell);

                // 總月銷量
                const monthlySalesCell = document.createElement('td');
                const monthlySalesBadge = document.createElement('span');
                monthlySalesBadge.className = 'badge badge-purple';
                monthlySalesBadge.textContent = product.總月銷量 || '0';
                monthlySalesCell.appendChild(monthlySalesBadge);
                row.appendChild(monthlySalesCell);

                // 型號數量 - 顯示可見型號數量
                const modelCountCell = document.createElement('td');
                const countBadge = document.createElement('span');
                countBadge.className = 'badge badge-success';
                countBadge.textContent = visibleModelCount;
                modelCountCell.appendChild(countBadge);
                row.appendChild(modelCountCell);

                fragment.appendChild(row);
                totalVisibleProducts++;
            } catch (error) {
                console.error('處理產品時出錯:', error, product);
            }
        });
        
        // 一次性更新DOM
        if (totalVisibleProducts > 0) {
            productList.appendChild(fragment);
        // console.log removed
        } else {
            productList.innerHTML = `
                <tr>
                    <td colspan="5">
                        <div class="empty-state">
                            <i class="fas fa-filter"></i>
                            <p>沒有符合過濾條件的商品</p>
                        </div>
                    </td>
                </tr>
            `;
        // console.log removed
        }
        
        updateBatchRestockToolbar();

        // 更新儀表板UI
        updateDashboardUI(products, advancedKeyword, searchOption);
    }
    
    // 綁定過濾模式切換事件 - 當過濾模式改變時重新顯示商品
    const filterModeElement = document.getElementById('filterMode');
    if (filterModeElement) {
        filterModeElement.addEventListener('change', function() {
            // 如果已經有搜尋結果，則重新顯示
            if (window.lastSearchResults) {
                // 檢查是否有進階搜尋狀態
                if (window.currentAdvancedKeyword && window.currentAdvancedKeyword.trim() !== '') {
                    // 有進階搜尋，使用進階搜尋的關鍵字和選項進行過濾
                    displayProducts(window.lastSearchResults, window.currentAdvancedKeyword, window.currentSearchOption);
                } else {
                    // 沒有進階搜尋，使用原始搜尋結果
                    displayProducts(window.lastSearchResults, '', 'product');
                }
            }
        });
    }
    
    // 綁定庫存月份下拉框變更事件
    const inventoryMonthElement = document.getElementById('inventoryMonth');
    if (inventoryMonthElement) {
        inventoryMonthElement.addEventListener('change', function() {
            // 如果已經有搜尋結果，則重新顯示
            if (window.lastSearchResults) {
                // 檢查是否有進階搜尋狀態
                if (window.currentAdvancedKeyword && window.currentAdvancedKeyword.trim() !== '') {
                    // 有進階搜尋，使用進階搜尋的關鍵字和選項進行過濾
                    displayProducts(window.lastSearchResults, window.currentAdvancedKeyword, window.currentSearchOption);
                } else {
                    // 沒有進階搜尋，使用原始搜尋結果
                    displayProducts(window.lastSearchResults, '', 'product');
                }
            }
        });
    }
    
    // 添加進階搜尋功能
    function performAdvancedSearch() {
        // console.log removed
        
        const keyword = advancedSearchInput.value.trim();
        
        if (!keyword) {
            alert('請輸入進階搜尋關鍵字');
            return;
        }
        
        if (!window.lastSearchResults) {
            alert('請先執行基本搜尋');
            return;
        }
        
        // 獲取當前選擇的搜尋選項
        let searchOption = 'product'; // 預設為商品名稱
        if (searchOptionModel.checked) {
            searchOption = 'model';
        } else if (searchOptionBoth.checked) {
            searchOption = 'both';
        }
        
        // 保存當前進階搜尋關鍵字和選項
        window.currentAdvancedKeyword = keyword;
        window.currentSearchOption = searchOption;
        
        // 使用原始搜尋結果、進階搜尋關鍵字和搜尋選項重新顯示商品
        displayProducts(window.lastSearchResults, keyword, searchOption);
    }
    
    // 清除進階搜尋功能
    function clearAdvancedSearch() {
        // console.log removed
        
        // 清空進階搜尋輸入框
        advancedSearchInput.value = '';
        
        // 重置搜尋選項到預設值（商品名稱）
        searchOptionProduct.checked = true;
        
        // 清除當前進階搜尋關鍵字和選項
        window.currentAdvancedKeyword = '';
        window.currentSearchOption = 'product';
        
        // 如果有搜尋結果，則顯示原始結果，但保持當前的過濾模式設置
        if (window.lastSearchResults) {
            displayProducts(window.lastSearchResults, '', 'product');
            
            // 移除舊的搜尋結果信息（如果有）
            const oldInfo = document.querySelector('.search-results-info');
            if (oldInfo) {
                oldInfo.remove();
            }
        }
    }
    
    // 綁定進階搜尋按鈕點擊事件
    advancedSearchButton.addEventListener('click', performAdvancedSearch);
    
    // 綁定清除進階搜尋按鈕點擊事件
    clearAdvancedSearchButton.addEventListener('click', clearAdvancedSearch);
    
    // 綁定進階搜尋輸入框按下Enter鍵事件
    advancedSearchInput.addEventListener('keypress', function(e) {
        if (e.key === 'Enter') {
            e.preventDefault(); // 防止表單提交
            performAdvancedSearch();
        }
    });
    
    // (已移除圓餅圖相關按鈕事件)
    
    // 修改 performSearch 函數，保存最後的搜尋結果並顯示進階搜尋區塊
    function performSearch() {
        // console.log removed
        
        const searchInput = document.getElementById('searchInput');
        if (!searchInput) {
            console.error('找不到搜尋輸入框元素');
            return;
        }
        
        const keyword = searchInput.value.trim();
        const headlessModeElement = document.getElementById('headlessMode');
        const inventoryMonthElement = document.getElementById('inventoryMonth');
        
        if (!headlessModeElement) {
            console.error('找不到 headlessMode 元素');
            return;
        }
        
        const showBrowser = headlessModeElement.checked;
        const inventoryMonth = inventoryMonthElement ? inventoryMonthElement.value : '4';
        
        // console.log removed
        
        // 移除關鍵字檢查，無論是否有關鍵字都執行以下代碼
        // 重置進階搜尋
        window.currentAdvancedKeyword = '';
        window.currentSearchOption = 'product';
        advancedSearchInput.value = '';
        searchOptionProduct.checked = true;
        
        // 隱藏進階搜尋區塊
        advancedSearchCard.style.display = 'none';
        
        // 移除舊的搜尋結果信息（如果有）
        const oldInfo = document.querySelector('.search-results-info');
        if (oldInfo) {
            oldInfo.remove();
        }
        
        // 設置爬蟲運行狀態
        window.crawlerRunning = true;
        // console.log removed
        
        // 顯示載入中
        const loading = document.getElementById('loading');
        if (loading) {
            loading.style.display = 'block';
        // console.log removed
        }
        
        const productList = document.getElementById('productList');
        if (productList) {
            productList.innerHTML = '';
        // console.log removed
        }
        window.currentRestockProducts = [];
        updateBatchRestockToolbar();
        
        // 開始進度模擬，使用改進的進度模擬函數
        const progressInterval = startProgressSimulation();
        
        // 發送請求到API，包含顯示瀏覽器參數和庫存月份
        const searchUrl = `/search?keyword=${encodeURIComponent(keyword)}&showBrowser=${showBrowser}&inventoryMonth=${inventoryMonth}`;
        // console.log removed
        
        fetch(searchUrl)
            .then(response => {
        // console.log removed
                return response.json();
            })
            .then(data => {
                if (data && typeof data === 'object' && (data.error || data.status === 'error')) {
                    throw new Error(data.error || data.message || '爬蟲執行失敗');
                }

                // 停止進度模擬
                clearInterval(progressInterval);
                
                // 設置進度為100%
                progressBar.style.width = '100%';
                progressText.textContent = '100%';
                statusMessage.textContent = '爬取完成！';
                
        // console.log removed
        // console.log removed
                
                // 保存最後的搜尋結果
                window.lastSearchResults = data;
                
                // 短暫延遲後隱藏載入提示
                setTimeout(() => {
                    if (loading) loading.style.display = 'none';
                    loadAlibabaLinks(); // 載入阿里巴巴連結
                    loadAlibabaBindings(true).then(() => {
                        displayProducts(data, '', 'product'); // 傳遞正確的參數
                    });
                    
                    // 顯示進階搜尋區塊
                    if (data && typeof data === 'object' && Object.keys(data).length > 0) {
                        advancedSearchCard.style.display = 'block';
                    }
                }, 500);
            })
            .catch(error => {
                // 停止進度模擬
                clearInterval(progressInterval);
                
                console.error('搜尋出錯:', error);
                if (loading) loading.style.display = 'none';
                if (productList) {
                    productList.innerHTML = `
                        <tr>
                            <td colspan="5">
                                <div class="empty-state">
                                    <i class="fas fa-exclamation-triangle"></i>
                                    <p>${escapeHtml(error.message || '搜尋時發生錯誤，請稍後再試')}</p>
                                </div>
                            </td>
                        </tr>
                    `;
                }
                alert(error.message || '搜尋時發生錯誤，請稍後再試');
            })
            .finally(() => {
                window.crawlerRunning = false;
            });
    }

    // 綁定搜尋按鈕點擊事件
    searchButton.addEventListener('click', performSearch);
    if (createProcurementDraftButton) {
        createProcurementDraftButton.addEventListener('click', createProcurementDraft);
    }
    if (syncProcurementOrdersButton) {
        syncProcurementOrdersButton.addEventListener('click', syncProcurementOrders);
    }
    if (loadSkuReviewButton) {
        loadSkuReviewButton.addEventListener('click', () => loadSkuReviewReports().then(() => loadSkuReview()));
    }
    if (skuReviewReportSelect) {
        skuReviewReportSelect.addEventListener('change', loadSkuReview);
    }
    if (skuReviewFilterSelect) {
        skuReviewFilterSelect.addEventListener('change', loadSkuReview);
    }
    if (applySelectedSkuReviewButton) {
        applySelectedSkuReviewButton.addEventListener('click', () => {
            applySkuReviewItems(collectSelectedSkuReviewItems());
        });
    }
    if (applyHighConfidenceButton) {
        applyHighConfidenceButton.addEventListener('click', () => {
            const report = skuReviewReportSelect ? (skuReviewReportSelect.value || 'latest') : 'latest';
            setSkuReviewMessage('讀取 High confidence 清單中...');
            fetch(`/api/alibaba/sku-review?report=${encodeURIComponent(report)}&filter=high`)
                .then(r => r.json().then(data => ({ ok: r.ok, data })))
                .then(({ ok, data }) => {
                    if (!ok || data.status !== 'success') {
                        throw new Error(data.message || '載入 High confidence 失敗');
                    }
                    const items = [];
                    (data.groups || []).forEach(group => {
                        (group.rows || []).forEach(row => {
                            if (row.suggestedSkuName) {
                                items.push({
                                    productId: row.productId,
                                    specId: row.specId,
                                    modelName: row.modelName,
                                    skuName: row.suggestedSkuName
                                });
                            }
                        });
                    });
                    if (!items.length) {
                        setSkuReviewMessage('此 report 沒有 High confidence 項目。', 'error');
                        return;
                    }
                    if (!confirm(`確定要寫入 ${items.length} 筆 High confidence SKU 對應嗎？`)) {
                        setSkuReviewMessage('已取消套用 High confidence。');
                        return;
                    }
                    return applySkuReviewItems(items);
                })
                .catch(error => {
                    console.error('套用 High confidence 失敗:', error);
                    setSkuReviewMessage(error.message || '套用 High confidence 失敗', 'error');
                });
        });
    }
    if (cookieImportButton) {
        cookieImportButton.addEventListener('click', importShopeeCookies);
    }
    if (cookieImportClearButton) {
        cookieImportClearButton.addEventListener('click', () => {
            if (cookieImportText) cookieImportText.value = '';
            setCookieImportStatus('');
            if (cookieImportText) cookieImportText.focus();
        });
    }
    loadAlibabaBindings();
    loadSkuReviewReports().then(() => {
        if (skuReviewReportSelect && skuReviewReportSelect.value) {
            loadSkuReview();
        }
    });
    
    // 綁定輸入框按下Enter鍵事件
    searchInput.addEventListener('keypress', function(e) {
        if (e.key === 'Enter') {
            e.preventDefault(); // 防止表單提交
            performSearch();
        }
    });
    
    // 中斷爬蟲功能
    function stopCrawler() {
        if (crawlerRunning) {
            // 顯示中斷中的消息
            statusMessage.textContent = '正在中斷爬蟲...';
            
            // 發送中斷請求
            fetch('/stop_crawler')
                .then(response => response.json())
                .then(data => {
        // console.log removed
                    crawlerRunning = false;
                    loading.style.display = 'none';
                    alert('爬蟲已中斷');
                })
                .catch(error => {
                    console.error('中斷爬蟲出錯:', error);
                    alert('中斷爬蟲時出錯');
                });
        }
    }
    
    // 重設按鈕相關代碼已移除
    
});
