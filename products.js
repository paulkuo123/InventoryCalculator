(function () {
    const searchForm = document.getElementById('productSearchForm');
    const searchInput = document.getElementById('productSearchInput');
    const searchButton = document.getElementById('productSearchButton');
    const searchStatus = document.getElementById('productSearchStatus');
    const results = document.getElementById('productResults');
    const emptyTemplate = document.getElementById('emptyResultsTemplate');
    const modalBackdrop = document.getElementById('modalBackdrop');
    const skuMappingModal = document.getElementById('skuMappingModal');
    const alibabaEditModal = document.getElementById('alibabaEditModal');
    const skuMappingProduct = document.getElementById('skuMappingProduct');
    const skuMappingRows = document.getElementById('skuMappingRows');
    const skuMappingMessage = document.getElementById('skuMappingMessage');
    const saveSkuMappingButton = document.getElementById('saveSkuMappingButton');
    const alibabaEditForm = document.getElementById('alibabaEditForm');
    const alibabaEditIdentity = document.getElementById('alibabaEditIdentity');
    const alibabaEditMessage = document.getElementById('alibabaEditMessage');
    const alibabaEditLink = document.getElementById('alibabaEditLink');
    const saveAlibabaEditButton = document.getElementById('saveAlibabaEditButton');
    const applyUrlOfferToAllButton = document.getElementById('applyUrlOfferToAllButton');

    const state = {
        products: [],
        activeProduct: null,
        activeModel: null,
    };

    function setMessage(element, message, kind = '') {
        element.textContent = message || '';
        element.className = `modal-message ${kind}`.trim();
    }

    function setSearchStatus(message, kind = '') {
        searchStatus.textContent = message || '';
        searchStatus.className = `product-search-status ${kind}`.trim();
    }

    function createImage(url, alt, className, fallbackIcon) {
        const box = document.createElement('div');
        box.className = className;
        const icon = document.createElement('i');
        icon.className = fallbackIcon;
        box.appendChild(icon);
        if (url) {
            const image = document.createElement('img');
            image.src = url;
            image.alt = alt || '';
            image.loading = 'lazy';
            image.addEventListener('error', () => image.remove());
            box.appendChild(image);
        }
        return box;
    }

    function offerIdFromUrl(url) {
        const match = String(url || '').match(/\/offer\/(\d+)/i);
        return match ? match[1] : '';
    }

    function safeText(value, fallback = '—') {
        const text = String(value ?? '').trim();
        return text || fallback;
    }

    function numericValue(value) {
        if (value === '' || value === null || value === undefined) return null;
        const number = Number(value);
        return Number.isFinite(number) ? number : null;
    }

    function createBadge(label, value, type = '') {
        const badge = document.createElement('span');
        const number = numericValue(value);
        badge.className = `catalog-badge ${type}`.trim();
        if (number === 0 && type !== 'orange') badge.classList.add('zero');
        badge.textContent = `${label}: ${number === null ? safeText(value) : number}`;
        return badge;
    }

    function hasSkuMapping(model) {
        return Boolean(String(model.alibabaSkuName || '').trim());
    }

    function mappingButtonText(model) {
        if (!hasSkuMapping(model)) return '＋ 1688 對應型號';
        const primary = safeText(model.alibabaSkuName, '未設定');
        const secondary = String(model.alibabaSkuSecondName || '').trim();
        return secondary ? `1688：${primary}／${secondary}` : `1688：${primary}`;
    }

    function createVariantRow(product, model) {
        const row = document.createElement('article');
        row.className = 'variant-row';
        row.appendChild(createImage(model.modelImageUrl, model.modelName, 'variant-image', 'fas fa-image'));

        const copy = document.createElement('div');
        copy.className = 'variant-copy';
        const name = document.createElement('strong');
        name.textContent = model.modelName || '未命名規格';
        const spec = document.createElement('span');
        spec.textContent = `規格 ID ${model.specId || '—'}`;
        copy.append(name, spec);

        const badges = document.createElement('div');
        badges.className = 'variant-badges';
        badges.append(
            createBadge('庫存', model.stock),
            createBadge('已售', model.soldQty, 'blue'),
            createBadge('月銷', model.monthlySales, 'blue')
        );
        if (numericValue(model.suggestedRestockQty) > 0) {
            badges.appendChild(createBadge('建議補貨', model.suggestedRestockQty, 'orange'));
        }

        const actions = document.createElement('div');
        actions.className = 'variant-actions';
        const mapping = document.createElement('button');
        mapping.type = 'button';
        mapping.className = 'mapping-button';
        mapping.textContent = mappingButtonText(model);
        mapping.addEventListener('click', () => openSkuMappingModal(product));
        const edit = document.createElement('button');
        edit.type = 'button';
        edit.className = 'edit-model-button';
        edit.textContent = '編輯';
        edit.addEventListener('click', () => openAlibabaEditModal(product, model));
        actions.append(mapping, edit);
        row.append(copy, badges, actions);
        return row;
    }

    function renderProducts(products) {
        state.products = products;
        results.replaceChildren();
        if (!products.length) {
            results.appendChild(emptyTemplate.content.cloneNode(true));
            return;
        }
        products.forEach(product => {
            const card = document.createElement('section');
            card.className = 'card product-editor-card';
            const header = document.createElement('div');
            header.className = 'product-editor-header';
            header.appendChild(createImage(product.productImageUrl, product.productName, 'product-editor-image', 'fas fa-box'));
            const heading = document.createElement('div');
            heading.className = 'product-editor-heading';
            const title = document.createElement('h2');
            title.textContent = product.productName || '未命名商品';
            const meta = document.createElement('p');
            const total = Number(product.modelCount || product.models.length);
            meta.textContent = `蝦皮商品 ID ${product.productId}｜顯示 ${product.models.length} / ${total} 個規格`;
            heading.append(title, meta);
            header.appendChild(heading);

            const modelList = document.createElement('div');
            modelList.className = 'product-model-list';
            product.models.forEach(model => modelList.appendChild(createVariantRow(product, model)));
            card.append(header, modelList);
            results.appendChild(card);
        });
    }

    function showModal(modal) {
        modalBackdrop.hidden = false;
        modal.hidden = false;
        document.body.classList.add('modal-open');
        const firstInput = modal.querySelector('input');
        if (firstInput) setTimeout(() => firstInput.focus(), 0);
    }

    function closeModals() {
        modalBackdrop.hidden = true;
        skuMappingModal.hidden = true;
        alibabaEditModal.hidden = true;
        document.body.classList.remove('modal-open');
        state.activeProduct = null;
        state.activeModel = null;
    }

    function createMappingRow(model) {
        const row = document.createElement('div');
        row.className = `mapping-row ${hasSkuMapping(model) ? '' : 'unmapped'}`.trim();
        row.dataset.specId = model.specId || '';
        row.dataset.modelName = model.modelName || '';
        const summary = document.createElement('div');
        summary.className = 'mapping-model-summary';
        summary.appendChild(createImage(model.modelImageUrl, model.modelName, 'mapping-image', 'fas fa-image'));
        const copy = document.createElement('div');
        const name = document.createElement('strong');
        name.textContent = model.modelName || '未命名規格';
        const spec = document.createElement('span');
        spec.textContent = `規格 ID ${model.specId || '—'}`;
        copy.append(name, spec);
        summary.appendChild(copy);

        const primary = document.createElement('input');
        primary.className = 'mapping-input';
        primary.name = 'alibabaSkuName';
        primary.placeholder = '第一規格（例如：黑色）';
        primary.value = model.alibabaSkuName || '';
        primary.autocomplete = 'off';
        const secondary = document.createElement('input');
        secondary.className = 'mapping-input';
        secondary.name = 'alibabaSkuSecondName';
        secondary.placeholder = '第二規格（可留白）';
        secondary.value = model.alibabaSkuSecondName || '';
        secondary.autocomplete = 'off';
        const link = document.createElement('a');
        link.className = 'mapping-link';
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        if (model.alibabaProductUrl) {
            link.href = model.alibabaProductUrl;
            link.textContent = '開啟 1688 商品頁';
        } else {
            link.classList.add('disabled');
            link.textContent = '未設定 1688 URL';
        }
        row.append(summary, primary, secondary, link);
        return row;
    }

    async function fetchFullProduct(productId) {
        const response = await fetch(`/api/golden-table/catalog?query=${encodeURIComponent(productId)}&limit=1`);
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.status !== 'success' || !data.products?.length) {
            throw new Error(data.message || '讀取商品完整規格失敗');
        }
        return data.products.find(product => String(product.productId) === String(productId)) || data.products[0];
    }

    async function openSkuMappingModal(product) {
        setMessage(skuMappingMessage, '正在載入此商品的所有規格…', 'loading');
        skuMappingRows.replaceChildren();
        skuMappingProduct.textContent = `商品：${product.productName || '未命名商品'}（蝦皮商品 ID ${product.productId}）`;
        showModal(skuMappingModal);
        try {
            const fullProduct = await fetchFullProduct(product.productId);
            state.activeProduct = fullProduct;
            skuMappingProduct.textContent = `商品：${fullProduct.productName || '未命名商品'}（蝦皮商品 ID ${fullProduct.productId}）`;
            fullProduct.models.forEach(model => skuMappingRows.appendChild(createMappingRow(model)));
            setMessage(skuMappingMessage, '');
        } catch (error) {
            setMessage(skuMappingMessage, error.message || '讀取完整規格失敗', 'error');
        }
    }

    function updateAlibabaLink(url) {
        const cleanUrl = String(url || '').trim();
        if (cleanUrl) {
            alibabaEditLink.href = cleanUrl;
            alibabaEditLink.classList.remove('disabled');
        } else {
            alibabaEditLink.removeAttribute('href');
            alibabaEditLink.classList.add('disabled');
        }
    }

    function openAlibabaEditModal(product, model) {
        state.activeProduct = product;
        state.activeModel = model;
        alibabaEditIdentity.replaceChildren();
        alibabaEditIdentity.appendChild(createImage(model.modelImageUrl, model.modelName, 'modal-model-image', 'fas fa-image'));
        const copy = document.createElement('div');
        const productName = document.createElement('strong');
        productName.textContent = `商品：${product.productName || '未命名商品'}`;
        const detail = document.createElement('span');
        detail.textContent = `型號：${model.modelName || '未命名規格'}｜規格 ID：${model.specId || '—'}`;
        copy.append(productName, detail);
        alibabaEditIdentity.appendChild(copy);
        const fields = alibabaEditForm.elements;
        fields.alibabaProductName.value = model.alibabaProductName || '';
        fields.alibabaProductUrl.value = model.alibabaProductUrl || '';
        fields.alibabaOfferId.value = model.alibabaOfferId || '';
        fields.alibabaSkuId.value = model.alibabaSkuId || '';
        fields.alibabaSkuName.value = model.alibabaSkuName || '';
        fields.alibabaSkuSecondName.value = model.alibabaSkuSecondName || '';
        fields.alibabaLastPriceCny.value = model.alibabaLastPriceCny ?? '';
        fields.alibabaMinOrderQty.value = model.alibabaMinOrderQty || 1;
        fields.alibabaPackageMultiple.value = model.alibabaPackageMultiple || 1;
        updateAlibabaLink(model.alibabaProductUrl);
        setMessage(alibabaEditMessage, '');
        showModal(alibabaEditModal);
    }

    function patchModel(model, payload) {
        Object.assign(model, {
            alibabaProductName: payload.alibabaProductName,
            alibabaProductUrl: payload.alibabaProductUrl,
            alibabaOfferId: payload.alibabaOfferId,
            alibabaSkuId: payload.alibabaSkuId,
            alibabaSkuName: payload.alibabaSkuName,
            alibabaSkuSecondName: payload.alibabaSkuSecondName,
            alibabaMinOrderQty: Number(payload.alibabaMinOrderQty) || 1,
            alibabaPackageMultiple: Number(payload.alibabaPackageMultiple) || 1,
            alibabaLastPriceCny: payload.alibabaLastPriceCny,
        });
    }

    function renderCurrentSearch() {
        renderProducts(state.products);
    }

    async function saveSkuMapping() {
        const product = state.activeProduct;
        if (!product) return;
        const mappings = Array.from(skuMappingRows.querySelectorAll('.mapping-row')).map(row => ({
            specId: row.dataset.specId,
            modelName: row.dataset.modelName,
            alibabaSkuName: row.querySelector('[name="alibabaSkuName"]').value.trim(),
            alibabaSkuSecondName: row.querySelector('[name="alibabaSkuSecondName"]').value.trim(),
        }));
        saveSkuMappingButton.disabled = true;
        setMessage(skuMappingMessage, '正在寫入 Golden Table…', 'loading');
        try {
            const response = await fetch('/api/golden-table/product-1688-skus', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ productId: product.productId, mappings }),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok || data.status !== 'success') {
                throw new Error(data.message || '儲存對應失敗');
            }
            product.models.forEach(model => {
                const mapping = mappings.find(item => item.specId === model.specId || item.modelName === model.modelName);
                if (mapping) {
                    model.alibabaSkuName = mapping.alibabaSkuName;
                    model.alibabaSkuSecondName = mapping.alibabaSkuSecondName;
                }
            });
            state.products.forEach(searchProduct => {
                if (String(searchProduct.productId) !== String(product.productId)) return;
                searchProduct.models.forEach(model => {
                    const mapping = mappings.find(item => item.specId === model.specId || item.modelName === model.modelName);
                    if (mapping) {
                        model.alibabaSkuName = mapping.alibabaSkuName;
                        model.alibabaSkuSecondName = mapping.alibabaSkuSecondName;
                    }
                });
            });
            renderCurrentSearch();
            setMessage(skuMappingMessage, data.message || '已儲存 1688 對應型號', 'success');
        } catch (error) {
            setMessage(skuMappingMessage, error.message || '儲存對應失敗', 'error');
        } finally {
            saveSkuMappingButton.disabled = false;
        }
    }

    async function saveAlibabaEdit(applyScope = 'single') {
        const product = state.activeProduct;
        const model = state.activeModel;
        if (!product || !model) return;
        const fields = alibabaEditForm.elements;
        const payload = {
            productId: product.productId,
            specId: model.specId,
            modelName: model.modelName,
            alibabaProductName: fields.alibabaProductName.value.trim(),
            alibabaProductUrl: fields.alibabaProductUrl.value.trim(),
            alibabaOfferId: fields.alibabaOfferId.value.trim(),
            alibabaSkuId: fields.alibabaSkuId.value.trim(),
            alibabaSkuName: fields.alibabaSkuName.value.trim(),
            alibabaSkuSecondName: fields.alibabaSkuSecondName.value.trim(),
            alibabaLastPriceCny: fields.alibabaLastPriceCny.value.trim(),
            alibabaMinOrderQty: fields.alibabaMinOrderQty.value || '1',
            alibabaPackageMultiple: fields.alibabaPackageMultiple.value || '1',
            applyScope,
        };
        if (payload.alibabaProductUrl && !/^https?:\/\//i.test(payload.alibabaProductUrl)) {
            setMessage(alibabaEditMessage, '1688 商品 URL 必須以 http:// 或 https:// 開頭', 'error');
            return;
        }
        const offerIdFromProductUrl = offerIdFromUrl(payload.alibabaProductUrl);
        if (offerIdFromProductUrl) {
            payload.alibabaOfferId = offerIdFromProductUrl;
            fields.alibabaOfferId.value = offerIdFromProductUrl;
        }
        const currentUrl = String(model.alibabaProductUrl || '').trim();
        const currentOfferId = String(model.alibabaOfferId || offerIdFromUrl(currentUrl) || '').trim();
        const urlChanged = payload.alibabaProductUrl !== currentUrl || payload.alibabaOfferId !== currentOfferId;
        if (applyScope === 'url_offer_all' || urlChanged) {
            const params = new URLSearchParams({
                mode: 'urls',
                productId: product.productId,
                modelId: model.specId || model.modelName || '',
            });
            if (payload.alibabaProductUrl) params.set('newUrl', payload.alibabaProductUrl);
            window.location.href = `/sku-mapping.html?${params}`;
            return;
        }
        saveAlibabaEditButton.disabled = true;
        applyUrlOfferToAllButton.disabled = true;
        setMessage(alibabaEditMessage, '正在寫入 Golden Table…', 'loading');
        try {
            const response = await fetch('/api/golden-table/model-alibaba', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok || data.status !== 'success') {
                throw new Error(data.message || '儲存失敗');
            }
            state.products.forEach(searchProduct => {
                if (String(searchProduct.productId) !== String(product.productId)) return;
                searchProduct.models.forEach(searchModel => {
                    if (applyScope === 'url_offer_all') {
                        searchModel.alibabaProductUrl = payload.alibabaProductUrl;
                        searchModel.alibabaOfferId = payload.alibabaOfferId;
                    } else if (searchModel.specId === model.specId || searchModel.modelName === model.modelName) {
                        patchModel(searchModel, payload);
                    }
                });
            });
            if (applyScope === 'url_offer_all') {
                product.models.forEach(productModel => {
                    productModel.alibabaProductUrl = payload.alibabaProductUrl;
                    productModel.alibabaOfferId = payload.alibabaOfferId;
                });
            } else {
                patchModel(model, payload);
            }
            renderCurrentSearch();
            setSearchStatus(data.message || '已儲存阿里巴巴資料', 'success');
            closeModals();
        } catch (error) {
            setMessage(alibabaEditMessage, error.message || '儲存失敗', 'error');
        } finally {
            saveAlibabaEditButton.disabled = false;
            applyUrlOfferToAllButton.disabled = false;
        }
    }

    async function searchProducts() {
        const query = searchInput.value.trim();
        searchButton.disabled = true;
        setSearchStatus('正在直接讀取 Golden Table…');
        try {
            const response = await fetch(`/api/golden-table/catalog?query=${encodeURIComponent(query)}&limit=50`);
            const data = await response.json().catch(() => ({}));
            if (!response.ok || data.status !== 'success') {
                throw new Error(data.message || '讀取商品資料失敗');
            }
            renderProducts(data.products || []);
            const displayCount = Array.isArray(data.products) ? data.products.length : 0;
            const suffix = data.totalMatches > displayCount ? `，目前顯示前 ${displayCount} 筆` : '';
            setSearchStatus(`找到 ${data.totalMatches} 個商品${suffix}。`);
        } catch (error) {
            results.replaceChildren();
            setSearchStatus(error.message || '讀取商品資料失敗', 'error');
        } finally {
            searchButton.disabled = false;
        }
    }

    searchForm.addEventListener('submit', event => {
        event.preventDefault();
        searchProducts();
    });
    saveSkuMappingButton.addEventListener('click', saveSkuMapping);
    alibabaEditForm.addEventListener('submit', event => {
        event.preventDefault();
        saveAlibabaEdit('single');
    });
    applyUrlOfferToAllButton.addEventListener('click', () => saveAlibabaEdit('url_offer_all'));
    alibabaEditForm.elements.alibabaProductUrl.addEventListener('input', event => {
        const url = event.target.value.trim();
        const offerId = offerIdFromUrl(url);
        if (offerId) {
            alibabaEditForm.elements.alibabaOfferId.value = offerId;
        }
        updateAlibabaLink(url);
    });
    document.querySelectorAll('[data-close-modal]').forEach(button => button.addEventListener('click', closeModals));
    modalBackdrop.addEventListener('click', event => {
        if (event.target === modalBackdrop) closeModals();
    });
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape' && !modalBackdrop.hidden) closeModals();
    });
    searchProducts();
})();
