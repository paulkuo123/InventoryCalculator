document.addEventListener('DOMContentLoaded', function() {
    const searchInput = document.getElementById('searchInput');
    const searchButton = document.getElementById('searchButton');
    const resetButton = document.getElementById('resetButton');
    const productList = document.getElementById('productList');
    const loading = document.getElementById('loading');
    
    const progressBar = document.getElementById('progressBar');
    const progressText = document.getElementById('progressText');
    const statusMessage = document.getElementById('statusMessage');
    
    // 定義爬蟲運行狀態變數
    let crawlerRunning = false;
    
    // 模擬進度更新
    function startProgressSimulation() {
        let progress = 0;
        progressBar.style.width = '0%';
        progressText.textContent = '0%';
        
        const progressInterval = setInterval(() => {
            // 緩慢增加進度，但不到100%
            if (progress < 90) {
                progress += Math.random() * 2;
                progress = Math.min(progress, 90);
                progressBar.style.width = progress + '%';
                progressText.textContent = Math.round(progress) + '%';
                
                // 根據進度更新狀態消息
                if (progress < 20) {
                    statusMessage.textContent = '正在載入頁面...';
                } else if (progress < 40) {
                    statusMessage.textContent = '正在展開商品型號...';
                } else if (progress < 60) {
                    statusMessage.textContent = '正在提取商品資訊...';
                } else if (progress < 80) {
                    statusMessage.textContent = '正在處理商品圖片...';
                } else {
                    statusMessage.textContent = '即將完成...';
                }
            }
        }, 1000);
        
        return progressInterval;
    }
    
    // 搜尋功能
    function performSearch() {
        const keyword = searchInput.value.trim();
        const showBrowser = document.getElementById('headlessMode').checked;
        const stockMonths = document.getElementById('stockMonths').value;
        
        if (keyword) {
            // 設置爬蟲運行狀態
            crawlerRunning = true;
            
            // 顯示載入中
            loading.style.display = 'block';
            productList.innerHTML = '';
            
            // 開始進度模擬
            const progressInterval = startProgressSimulation();
            
            // 發送請求到API，包含顯示瀏覽器參數和庫存月份
            fetch(`/search?keyword=${encodeURIComponent(keyword)}&showBrowser=${showBrowser}&stockMonths=${stockMonths}`)
                .then(response => response.json())
                .then(data => {
                    // 停止進度模擬
                    clearInterval(progressInterval);
                    
                    // 設置進度為100%
                    progressBar.style.width = '100%';
                    progressText.textContent = '100%';
                    statusMessage.textContent = '爬取完成！';
                    
                    // 更新爬蟲運行狀態
                    crawlerRunning = false;
                    
                    // 短暫延遲後隱藏載入提示
                    setTimeout(() => {
                        loading.style.display = 'none';
                        displayProducts(data);
                    }, 500);
                })
                .catch(error => {
                    // 停止進度模擬
                    clearInterval(progressInterval);
                    
                    // 更新爬蟲運行狀態
                    crawlerRunning = false;
                    
                    loading.style.display = 'none';
                    console.error('搜尋出錯:', error);
                    alert('搜尋時發生錯誤，請稍後再試');
                });
        }
    }
    
    // 顯示商品資料
    function displayProducts(products) {
        const productList = document.getElementById('productList');
        productList.innerHTML = '';

        // 檢查 products 是否有效
        if (!products || typeof products !== 'object' || Object.keys(products).length === 0) {
            productList.innerHTML = `
                <tr>
                    <td colspan="4">
                        <div class="empty-state">
                            <i class="fas fa-search"></i>
                            <p>沒有找到符合的商品</p>
                        </div>
                    </td>
                </tr>
            `;
            console.log('無商品資料或資料格式錯誤');
            return;
        }

        // 遍歷商品，key 是商品ID
        Object.entries(products).forEach(([productId, product]) => {
            try {
                const row = document.createElement('tr');

                // 商品名稱和圖片
                const nameCell = document.createElement('td');
                const nameDiv = document.createElement('div');
                nameDiv.className = 'product-name';

                const img = document.createElement('img');
                img.className = 'product-image';
                img.src = product.商品圖片網址 && product.商品圖片網址 !== '未找到' 
                    ? product.商品圖片網址 
                    : 'https://via.placeholder.com/60?text=無圖片';
                img.alt = product.商品名稱 || '未知商品';
                img.onerror = function() { 
                    this.src = 'https://via.placeholder.com/60?text=無圖片'; 
                    this.onerror = null; 
                };
                nameDiv.appendChild(img);

                const nameText = document.createElement('span');
                nameText.textContent = product.商品名稱 || '未知商品名稱';
                nameDiv.appendChild(nameText);
                nameCell.appendChild(nameDiv);

                // 型號資訊 - 處理數組形式的型號數據
                if (product.型號 && Array.isArray(product.型號) && product.型號.length > 0) {
                    const modelsDiv = document.createElement('div');
                    modelsDiv.className = 'model-info';

                    product.型號.forEach((modelData, index) => {
                        try {
                            const modelItem = document.createElement('div');
                            modelItem.className = 'model-item';

                            // 型號圖片
                            if (modelData.型號圖片網址 && modelData.型號圖片網址 !== '未找到') {
                                const modelImg = document.createElement('img');
                                modelImg.src = modelData.型號圖片網址;
                                modelImg.className = 'model-image';
                                modelImg.onerror = function() { 
                                    this.src = 'https://via.placeholder.com/30?text=無圖片'; 
                                    this.onerror = null; 
                                };
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
                            stockBadge.className = 
                        }
                    });
                }
            }
        });
    }
}); 