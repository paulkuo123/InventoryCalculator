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
    
    const progressBar = document.getElementById('progressBar');
    const progressText = document.getElementById('progressText');
    const statusMessage = document.getElementById('statusMessage');
    // 保存當前狀態變量
    window.currentSearchResults = null; // 保存原始搜尋結果
    window.currentAdvancedKeyword = ''; // 保存進階搜尋關鍵字
    window.currentSearchOption = 'product'; // 預設搜尋選項為商品名稱
    window.isPieChartVisible = false; // 圓餅圖顯示狀態
    window.alibabaLinks = {}; // 型號ID → 阿里巴巴連結映射

    // 載入阿里巴巴連結映射
    function loadAlibabaLinks() {
        if (Object.keys(window.alibabaLinks).length > 0) return; // 已載入過
        fetch('/api/alibaba-links')
            .then(r => r.json())
            .then(data => {
                window.alibabaLinks = data;
            })
            .catch(err => console.warn('載入阿里巴巴連結失敗:', err));
    }

    // 依序根據規格ID、商品名稱+型號名稱取得阿里巴巴連結
    function getAlibabaLink(specId, productName, modelName) {
        if (!window.alibabaLinks) return '';
        if (specId && window.alibabaLinks[String(specId)]) {
            return window.alibabaLinks[String(specId)];
        }
        if (productName && modelName) {
            return window.alibabaLinks[`${productName}|||${modelName}`] || '';
        }
        return '';
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
        
        // 獲取過濾條件
        const inventoryMonth = document.getElementById('inventoryMonth')?.value || '4';
        const filterMode = document.getElementById('filterMode')?.checked || false;
        
        // 檢查 products 是否有效
        if (!products || typeof products !== 'object' || Object.keys(products).length === 0) {
            productList.innerHTML = `
                <tr>
                    <td colspan="3">
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
                                
                                // 計算建議補貨 = 預期庫存 - 當前庫存
                                const suggestedRestock = effectiveExpectedStock - currentStock;
                                
                                // 只有當建議補貨為正數時才顯示
                                if (suggestedRestock > 0) {
                                    // 添加空格
                                    modelText.appendChild(document.createTextNode(' '));
                                    
                                    // 建議補貨標籤
                                    const restockBadge = document.createElement('span');
                                    restockBadge.className = 'badge badge-danger';
                                    restockBadge.textContent = `建議補貨: ${suggestedRestock}`;
                                    if (isEstimated) {
                                        restockBadge.textContent += ' (預估)';
                                        restockBadge.title = `基於歷史佔比法預估 (預估月銷量: ${effectiveMonthlyRate})`;
                                    }
                                    restockBadge.style.color = 'red';
                                    restockBadge.style.fontWeight = 'bold';
                                    modelText.appendChild(restockBadge);
                                }
                            }

                            // 阿里巴巴連結按鈕
                            const specId = modelData.規格ID || '';
                            const alibabaLink = getAlibabaLink(specId, product.商品名稱 || '', modelData.型號名稱 || '');
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

                            modelItem.appendChild(modelText);
                            modelsDiv.appendChild(modelItem);
                        } catch (modelError) {
                            console.error(`處理商品 ${productId} 的型號 ${index} 時出錯:`, modelError);
                        }
                    });
                    
                    // 只有當有可見型號時才添加到DOM
                    if (visibleModelCount > 0) {
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
                    <td colspan="3">
                        <div class="empty-state">
                            <i class="fas fa-filter"></i>
                            <p>沒有符合過濾條件的商品</p>
                        </div>
                    </td>
                </tr>
            `;
        // console.log removed
        }
        
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
                    displayProducts(data, '', 'product'); // 傳遞正確的參數
                    
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
                alert('搜尋時發生錯誤，請稍後再試');
            })
            .finally(() => {
                window.crawlerRunning = false;
            });
    }

    // 綁定搜尋按鈕點擊事件
    searchButton.addEventListener('click', performSearch);
    
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
