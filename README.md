# 蝦皮庫存管理系統 (莉莉安蝦皮庫存管理系統)

## 簡介
蝦皮庫存管理系統是一個整合了蝦皮商品爬蟲、銷售數據分析和庫存計算功能的工具，幫助賣家更有效地管理庫存和分析銷售數據。

## 主要功能
- **蝦皮商品爬蟲**：自動抓取蝦皮商品資訊和銷售數據（使用 Playwright）
- **銷售數據分析**：分析商品銷售趨勢和表現
- **庫存計算**：根據銷售數據計算最佳庫存量
- **Excel 數據解析**：解析蝦皮後台匯出的 Excel 檔案

## 環境搭建

### 必要套件
```bash
pip install -r requirements.txt
```

### 安裝 Playwright 瀏覽器
```bash
playwright install chromium
```

> **注意**: 程式啟動時會自動檢查並安裝缺失的依賴套件和 Playwright 瀏覽器。

## 使用方法

### 下載專案
```bash
git clone https://github.com/paulkuo123/InventoryCalculator.git
```

### 切換至專案資料夾
```bash
cd InventoryCalculator
```

### 瀏覽器設置
本程式使用 **Playwright** 控制 Chromium 瀏覽器進行爬蟲。

**必要條件**：
1. 執行 `playwright install chromium` 安裝 Chromium 瀏覽器
2. 程式會自動啟動內建的 Chromium，無需額外安裝 Chrome

### 設置 Cookies
1. 至 Chrome 線上應用程式商店安裝 Cookie Editor (https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm?hl=zh-TW&utm_source=ext_sidebar)
2. 登入蝦皮賣家中心
3. 點擊 Cookie Editor 擴充功能
4. 點擊「Export」輸出 JSON 格式
5. 將內容複製並貼到 `cookies.json` 檔案（專案根目錄）

### 執行主程式
```bash
python main.py
```
執行後會自動開啟網頁介面，您可以在此進行商品搜尋和數據分析。

### 獨立庫存計算器（GUI 工具）
除了主要的爬蟲功能外，本專案還包含一個獨立的 PyQt5 庫存計算工具：
```bash
python calculator.py
```

### Excel 數據解析器
解析蝦皮後台匯出的 Excel 檔案，整合庫存、銷售和媒體數據：
```bash
python parser.py
```
解析結果將輸出至 `shopee_products.json`

## 應用程式打包 (發布)

如果您希望將程式打包成獨立的執行檔（方便分發給其他電腦使用）：

1. 確保已安裝打包工具：
```bash
pip install -r requirements.txt
```

2. 執行打包腳本：
```bash
python build.py
```

3. 打包完成後，執行檔將位於 `dist/` 資料夾中：
   - 將 `dist/ShopeeCrawler`（或 `ShopeeCrawler.exe`）以及 `cookies.json` 複製到目標電腦即可使用，無需安裝 Python。

## 系統需求
- Python 3.8+
- 作業系統：Windows, macOS, Linux

## Demo

![image](images/main.png)
 
## 專案結構

```
InventoryCalculater/
├── main.py              # Web 伺服器入口點
├── crawler.py           # 蝦皮爬蟲（Playwright）
├── calculator.py        # PyQt5 庫存計算器（GUI）
├── parser.py            # Excel 數據解析器
├── pw_adapter.py        # Playwright ↔ Selenium 兼容層
├── build.py             # PyInstaller 打包腳本
├── index.html           # Web UI 前端
├── script.js            # 前端 JavaScript 邏輯
├── styles.css           # 前端樣式
├── requirements.txt     # Python 依賴套件
├── golden_table.json    # 參考數據表
├── cookies.json         # 蝦皮登入 Cookies（需自行設置）
└── dist/                # 打包後的執行檔
```

## 技術棧

- **後端**: Python 3.8+
- **網頁爬蟲**: Playwright (Chromium)
- **GUI**: PyQt5（獨立庫存計算器）
- **Web 框架**: 內建 `http.server` 模組
- **數據處理**: pandas（Excel 解析）
- **打包工具**: PyInstaller

## 常見問題

### Cookies 設置失敗？
確保您已登入蝦皮賣家中心，並使用 Cookie Editor 正確導出所有 cookies。

### 爬蟲執行失敗？
1. 檢查 `cookies.json` 是否存在且格式正確
2. 確認 Playwright 瀏覽器已安裝：`playwright install chromium`
3. 查看 `debug.log` 了解詳細錯誤訊息

### 打包後執行檔無法運行？
確保已將 `cookies.json` 和執行檔放在同一目錄。
