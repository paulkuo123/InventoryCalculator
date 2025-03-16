document.addEventListener('DOMContentLoaded', function() {
    const searchInput = document.getElementById('searchInput');
    const searchButton = document.getElementById('searchButton');
    const resetButton = document.getElementById('resetButton');
    const productList = document.getElementById('productList');
    const loading = document.getElementById('loading');
    
    const progressBar = document.getElementById('progressBar');
    const progressText = document.getElementById('progressText');
    const statusMessage = document.getElementById('statusMessage');
    
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
    
    // 顯示商品資料
    function displayProducts(products) {
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
        
        console.log('顯示商品數據:', Object.keys(products).length, '個商品');
        
        // 使用文檔片段減少DOM重繪
        const fragment = document.createDocumentFragment();
        let totalVisibleProducts = 0;
        
        // 遍歷商品，key 是商品ID
        Object.entries(products).forEach(([productId, product]) => {
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
                            
                            // 如果過濾模式開啟且當前庫存大於等於預期庫存，則跳過此型號
                            if (filterMode && currentStock >= expectedStock && expectedStock > 0) {
                                return;
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
                            } else if (stockNum > 10) {
                                stockBadge.className = 'badge badge-success'; // 庫存大於 10，使用綠色
                            } else if (stockNum === 0) {
                                stockBadge.className = 'badge badge-danger'; // 庫存為 0，使用紅色
                            } else {
                                stockBadge.className = 'badge badge-warning'; // 庫存 1-10，使用橘色
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
                            if (modelData.月銷量) {
                                // 添加空格
                                modelText.appendChild(document.createTextNode(' '));
                                
                                // 預期庫存標籤
                                const expectedStockBadge = document.createElement('span');
                                expectedStockBadge.className = 'badge badge-secondary';
                                expectedStockBadge.textContent = `預期庫存: ${expectedStock}`;
                                modelText.appendChild(expectedStockBadge);
                                
                                // 計算建議補貨 = 預期庫存 - 當前庫存
                                const suggestedRestock = expectedStock - currentStock;
                                
                                // 只有當建議補貨為正數時才顯示
                                if (suggestedRestock > 0) {
                                    // 添加空格
                                    modelText.appendChild(document.createTextNode(' '));
                                    
                                    // 建議補貨標籤
                                    const restockBadge = document.createElement('span');
                                    restockBadge.className = 'badge badge-danger';
                                    restockBadge.textContent = `建議補貨: ${suggestedRestock}`;
                                    restockBadge.style.color = 'red';
                                    restockBadge.style.fontWeight = 'bold';
                                    modelText.appendChild(restockBadge);
                                }
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
                console.error(`處理商品 ${productId} 時出錯:`, error);
            }
        });
        
        // 一次性更新DOM
        if (totalVisibleProducts > 0) {
            productList.appendChild(fragment);
            console.log(`成功顯示 ${totalVisibleProducts} 個商品`);
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
            console.log('沒有符合過濾條件的商品');
        }
    }
    
    // 綁定過濾模式切換事件 - 當過濾模式改變時重新顯示商品
    const filterModeElement = document.getElementById('filterMode');
    if (filterModeElement) {
        filterModeElement.addEventListener('change', function() {
            // 如果已經有搜尋結果，則重新顯示
            const searchInput = document.getElementById('searchInput');
            if (searchInput && searchInput.value.trim() && window.lastSearchResults) {
                displayProducts(window.lastSearchResults);
            }
        });
    }
    
    // 修改 performSearch 函數，保存最後的搜尋結果
    function performSearch() {
        console.log('執行搜尋...(全局函數)');
        
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
        
        console.log(`搜尋關鍵字: ${keyword}, 顯示瀏覽器: ${showBrowser}, 庫存月份: ${inventoryMonth}`);
        
        if (keyword) {
            // 設置爬蟲運行狀態
            window.crawlerRunning = true;
            console.log('設置爬蟲運行狀態為: true');
            
            // 顯示載入中
            const loading = document.getElementById('loading');
            if (loading) {
                loading.style.display = 'block';
                console.log('顯示載入中元素');
            }
            
            const productList = document.getElementById('productList');
            if (productList) {
                productList.innerHTML = '';
                console.log('清空商品列表');
            }
            
            // 開始進度模擬，使用改進的進度模擬函數
            const progressInterval = startProgressSimulation();
            
            // 發送請求到API，包含顯示瀏覽器參數和庫存月份
            const searchUrl = `/search?keyword=${encodeURIComponent(keyword)}&showBrowser=${showBrowser}&inventoryMonth=${inventoryMonth}`;
            console.log(`發送請求到: ${searchUrl}`);
            
            fetch(searchUrl)
                .then(response => {
                    console.log('收到API回應:', response.status);
                    return response.json();
                })
                .then(data => {
                    // 停止進度模擬
                    clearInterval(progressInterval);
                    
                    // 設置進度為100%
                    progressBar.style.width = '100%';
                    progressText.textContent = '100%';
                    statusMessage.textContent = '爬取完成！';
                    
                    console.log('API回傳數據類型:', typeof data);
                    console.log('API回傳數據結構:', data ? Object.keys(data).length : 'null');
                    
                    // 保存最後的搜尋結果
                    window.lastSearchResults = data;
                    
                    // 短暫延遲後隱藏載入提示
                    setTimeout(() => {
                        if (loading) loading.style.display = 'none';
                        displayProducts(data);
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
                    console.log('爬蟲中斷結果:', data);
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
    
    // 添加頁面關閉事件
    window.addEventListener('beforeunload', function() {
        // 發送關閉請求到伺服器
        navigator.sendBeacon('/shutdown');
    });
});