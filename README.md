# 蝦皮庫存管理系統 (莉莉安蝦皮庫存管理系統)

## 簡介
蝦皮庫存管理系統是一個整合了蝦皮商品爬蟲、銷售數據分析和庫存計算功能的工具，幫助賣家更有效地管理庫存和分析銷售數據。

## 主要功能
- **蝦皮商品爬蟲**：自動抓取蝦皮商品資訊和銷售數據（使用 Playwright）
- **銷售數據分析**：分析商品銷售趨勢和表現
- **庫存計算**：根據銷售數據計算最佳庫存量
- **Excel 數據解析**：解析蝦皮後台匯出的 Excel 檔案
- **Shopee 廣告報表匯出**：自動依序下載過去一個月、過去一週、昨天與今天的廣告總體報表
- **Shopee 廣告 AI 分析**：整合昨天 / 過去一週 / 過去一個月的表現，輸出 HTML 廣告調整報告
- **Telegram Bot**：支援庫存搜尋、廣告匯出與廣告分析報告回傳

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

### 設置 OpenAI API Key（廣告分析用）
廣告 AI 分析建議使用專案本地設定檔，不要把 Key 寫進程式碼或提交到 Git。

1. 執行初始化腳本：
```bash
python3 setup_openai_key.py
```

2. 腳本會將 Key 寫入專案根目錄下的 `.env.local`

3. 程式讀取順序如下：
   - `OPENAI_API_KEY` 環境變數
   - 專案本地 `.env.local`
   - `~/.zshrc` / `~/.bashrc`（舊方式備援）

> `.env.local` 已加入 `.gitignore`，不會推上 GitHub。若只想提供格式範例，可參考 `.env.example`。

### 執行主程式
```bash
python main.py
```
執行後會自動開啟網頁介面。

- 庫存首頁：可進行商品搜尋、庫存儀表板與補貨判讀
- 廣告工作台：可進行 Shopee 廣告報表匯出與 AI 分析

目前庫存與廣告功能已拆成不同頁面：
- `/`：庫存首頁
- `/ads.html`：廣告工作台

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

### 廣告報表匯出
若要手動執行 Shopee 廣告報表匯出，可直接執行：

```bash
python3 crawler.py --mode ads-export --output ads_export_result.json --headless true
```

流程會固定依序處理：
1. 過去一個月
2. 過去一週
3. 昨天
4. 今天

每個範圍都會先等待 Shopee 產出檔案，再下載 CSV 到 `ads_exports/`。

### 廣告分析
當 `ads_exports/` 內已有廣告 CSV 後，可手動執行分析：

```bash
python3 ads_analysis.py --include-ai true
```

主要輸出：
- `ads_analysis_latest.json`
- `ads_history.json`
- `ads_analysis_report.html`
- `ads_analysis_report.md`

分析原則：
- 以「昨天」作為主要決策基準
- 「過去一週」與「過去一個月」作為穩定性驗證視窗
- 以 `ROAS >= 3` 作為店內基準
- 除了 `ROAS`，也會一起考慮 `直接 ROAS / CTR / CVR / CPC / CPA / 直接成交占比`
- HTML 報告會只列出需要調整的商品，並附上商品圖片與具體建議

### Telegram Bot
若要使用 Telegram Bot：

```bash
python3 telegram_bot.py
```

目前可用指令：

- `/搜尋 產品 月數`
- `/廣告匯出`
- `/廣告分析`
- `/廣告分析 無AI`
- `/refresh`
- `/help`

其中：
- `/廣告匯出`：會回傳已下載的廣告 CSV
- `/廣告分析`：會回傳 HTML 廣告分析報告
- `/廣告分析 無AI`：只使用規則層，不呼叫 OpenAI

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
├── ads_analysis.py      # Shopee 廣告分析器
├── calculator.py        # PyQt5 庫存計算器（GUI）
├── parser.py            # Excel 數據解析器
├── telegram_bot.py      # Telegram Bot（庫存 / 廣告）
├── pw_adapter.py        # Playwright ↔ Selenium 兼容層
├── build.py             # PyInstaller 打包腳本
├── index.html           # Web UI 前端
├── ads.html             # 廣告工作台前端
├── script.js            # 前端 JavaScript 邏輯
├── ads.js               # 廣告工作台 JavaScript
├── styles.css           # 前端樣式
├── ads.css              # 廣告工作台樣式
├── requirements.txt     # Python 依賴套件
├── setup_openai_key.py  # 設定專案本地 OpenAI Key
├── config_loader.py     # 本地設定 / OpenAI Key 載入器
├── .env.example         # 本地設定檔範例
├── golden_table.json    # 參考數據表
├── cookies.json         # 蝦皮登入 Cookies（需自行設置）
├── ads_exports/         # 廣告 CSV 匯出資料夾
└── dist/                # 打包後的執行檔
```

## 技術棧

- **後端**: Python 3.8+
- **網頁爬蟲**: Playwright (Chromium)
- **GUI**: PyQt5（獨立庫存計算器）
- **Web 框架**: 內建 `http.server` 模組
- **數據處理**: pandas（Excel 解析）
- **廣告分析**: OpenAI API + 自訂規則層
- **通知 / 操作**: Telegram Bot API
- **打包工具**: PyInstaller

## 常見問題

### Cookies 設置失敗？
確保您已登入蝦皮賣家中心，並使用 Cookie Editor 正確導出所有 cookies。

### 爬蟲執行失敗？
1. 檢查 `cookies.json` 是否存在且格式正確
2. 確認 Playwright 瀏覽器已安裝：`playwright install chromium`
3. 查看 `debug.log` 了解詳細錯誤訊息

### 廣告分析沒有使用 OpenAI？
1. 先執行 `python3 setup_openai_key.py`
2. 確認專案根目錄已有 `.env.local`
3. 檢查 `OPENAI_API_KEY` 是否可用
4. 可用 `python3 inspect_openai_status.py` 測試目前設定是否能正常呼叫 OpenAI API

### Telegram 收到的 HTML 圖片顯示不出來？
廣告分析與庫存報表都會盡量將圖片轉成 Base64 內嵌在 HTML 中。若仍看不到圖片，通常是當次圖片網址抓取失敗，可重新執行一次分析。

### 打包後執行檔無法運行？
確保已將 `cookies.json` 和執行檔放在同一目錄。
