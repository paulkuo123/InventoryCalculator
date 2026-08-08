# 蝦皮庫存管理系統 (莉莉安蝦皮庫存管理系統)

## 簡介
蝦皮庫存管理系統是一個整合了蝦皮商品爬蟲、銷售數據分析和庫存計算功能的工具，幫助賣家更有效地管理庫存和分析銷售數據。

## 主要功能
- **蝦皮商品爬蟲**：自動抓取蝦皮商品資訊和銷售數據（使用 Playwright）
- **銷售數據分析**：分析商品銷售趨勢和表現
- **庫存計算**：根據銷售數據計算最佳庫存量
- **1688 補貨草稿**：將建議補貨量轉為 1688 採購草稿，支援 SKU 綁定、MOQ/包裝倍數調整與在途庫存追蹤
- **1688 到貨入庫蝦皮**：匯入 1688 訂單、核對實收與蝦皮規格，預覽後安全增加蝦皮庫存
- **Excel 數據解析**：解析蝦皮後台匯出的 Excel 檔案
- **Shopee 廣告報表匯出**：自動依序下載過去一個月、昨天，以及過去 4 週滾動周報，共 6 份
- **Shopee 廣告 AI 分析**：使用現有的昨天 / 最近一週（week_01）/ 過去一個月與過去 4 週趨勢資料，輸出 HTML 廣告調整報告
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

   同時會設定品質優先的預設值：

   ```text
   OPENAI_MODEL=gpt-5.6-sol
   OPENAI_REASONING_EFFORT=xhigh
   ```

3. 程式讀取順序如下：
   - `OPENAI_API_KEY` 環境變數
   - 專案本地 `.env.local`
   - `~/.zshrc` / `~/.bashrc`（舊方式備援）

> `.env.local` 已加入 `.gitignore`，不會推上 GitHub。若只想提供格式範例，可參考 `.env.example`。

廣告頁面可直接切換模型：

- `gpt-5.6-sol`：品質優先，適合正式週報與重要預算調整。
- `gpt-5.6-terra`：品質、速度與成本平衡，適合較頻繁的例行分析。
- `gpt-5.6-luna`：速度與成本優先，適合初步篩選。

推理強度支援 `medium`、`high`、`xhigh`、`max`；目前預設 `xhigh`。`max` 只建議用於少數高風險決策，耗時與費用通常更高。

### 設置 1688 開放平台憑證（補貨 API 用）
第一版會先建立補貨草稿與待付款建單流程，但在取得 1688 開放平台下單 API 權限前，不會真的送出 1688 訂單。

在 `.env.local` 中加入：
```bash
ALIBABA_APP_KEY=
ALIBABA_APP_SECRET=
ALIBABA_ACCESS_TOKEN=
ALIBABA_REFRESH_TOKEN=
SHOPEE_INBOUND_WRITE_ENABLED=false
```

目前系統會檢查憑證狀態、建立採購草稿、保存 Shopee 型號與 1688 offer/sku 綁定，並用 `procurement.db` 追蹤草稿、訂單與在途庫存。正式建單端點需等 1688 AppKey/AppSecret 與下單 API 權限確認後，在 `alibaba_client.py` 補上實際簽名與呼叫。

### 執行主程式
```bash
python main.py
```
執行後會自動開啟網頁介面。

- 庫存首頁：可進行商品搜尋、庫存儀表板與補貨判讀
- 廣告工作台：可進行 Shopee 廣告報表匯出與 AI 分析
- 到貨入庫工作台：可匯入 1688 訂單、處理分批／不良品，並預覽蝦皮庫存更新

目前庫存與廣告功能已拆成不同頁面：
- `/`：庫存首頁
- `/ads.html`：廣告工作台
- `/inbound.html`：1688 到貨入庫蝦皮

### 1688 到貨入庫

1. 在 `/inbound.html` 貼上 1688 訂單編號或訂單詳情連結。
2. 若專用瀏覽器尚未登入 1688，先在開啟的視窗完成登入或驗證。
3. 確認每筆實收、不良、蝦皮增加量與商品規格對照；同一實體品若有多個蝦皮刊登，需自行分配數量。
4. 建立到貨單後先執行「讀取即時庫存」。系統此時只讀取，不會修改蝦皮。
5. 按單規格或「此商品全部入庫」後，系統會先核對「目前庫存 + 增加量 = 更新後庫存」，再顯示一次最後確認。
6. 確認後會在蝦皮「我的商品」以商品 ID 搜尋，開啟「設定庫存」視窗；同一商品的多個規格一次填寫、一次更新，並回到商品列重新驗證。
7. 正式寫入需在 `.env.local` 設定 `SHOPEE_INBOUND_WRITE_ENABLED=true` 並重新啟動。成功項目有本地防重複紀錄；按下更新後若結果不明，不會自動重試。

入庫紀錄保存在 `procurement.db`。`golden_table.json` 只會在蝦皮儲存並重新讀取驗證成功後同步庫存快取。

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
2. 昨天
3. 近第 1 週
4. 近第 2 週
5. 近第 3 週
6. 近第 4 週

每個範圍都會先等待 Shopee 產出檔案，再下載 CSV 到 `ads_exports/`。

### 廣告分析
當 `ads_exports/` 內已有廣告 CSV 後，可手動執行分析：

```bash
python3 ads_analysis.py --include-ai true
```

預設行為：
1. 讀取 `ads_exports/` 中各視窗最新一份 CSV
2. 使用昨天 / 最近一週（week_01）/ 過去一個月做即時判讀
3. 使用過去 4 週滾動近 7 天窗口做趨勢分析
4. 再統一產出 HTML / Markdown / JSON 報告

主要輸出：
- `ads_analysis_latest.json`
- `ads_history.json`
- `ads_analysis_report.html`
- `ads_analysis_report.md`

分析原則：
- 以「昨天」作為主要決策基準
- 「最近一週（week_01）」與「過去一個月」作為穩定性驗證視窗
- 額外納入「過去 4 週滾動 7 天窗口」做趨勢判讀
- 以 `ROAS >= 3` 作為店內基準
- 除了 `ROAS`，也會一起考慮 `直接 ROAS / CTR / CVR / CPC / CPA / 直接成交占比`
- 趨勢判斷會特別看：
  - 連續幾週回收走弱
  - CTR 連續下滑是否疑似素材疲勞
  - CVR 長期偏低是否疑似商品頁問題
  - 花費提升但回收未同步改善
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
- `/廣告匯出`：會回傳昨天、過去一個月與過去 4 週的廣告 CSV，共 6 份
- `/廣告分析`：會使用現有 CSV 做 OpenAI 分析，並回傳 HTML 廣告分析報告
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
├── sku_mapping_service.py # SKU 快照、候選、AI 與審核服務
├── sku-mapping.html     # 1688 SKU mapping 工作台
├── sku-mapping.js       # mapping 審核互動
├── sku-mapping.css      # mapping 工作台樣式
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

## 1688 SKU Mapping 工作台

`/sku-mapping.html` 是整個賣場的 1688 SKU 對照獨立審核頁。按「重建全部名稱 mapping（掃描 1688）」後，系統會以 1688 offer 分組取得結構化 SKU 快照，先做繁簡／顏色／尺寸／手機型號等規則比對；只要規則找到候選，就不呼叫 AI，直接交給人工確認。只有規則完全找不到候選時，才會把完整 SKU 清單交給設定的 Gemini 初判；之後在頁面逐筆或批次核准。清單預設依商品總月銷量由高到低排序，同一商品的不同型號會集中在一起，再依型號月銷量排列。未核准、失效、停售、名稱組合不存在或 live 目錄無法驗證的資料會被購物車流程阻擋。

- 工作台目前以所有已有 1688 URL 的型號為範圍，不再以「是否需要補貨」作為顯示或掃描條件；補貨數量欄位仍保留在庫存與購物車流程中。
- 清單每頁最多顯示 200 筆，使用頁面底部的上一頁／下一頁瀏覽全部 URL 型號；跨頁勾選仍會保留，批次動作會一次套用所有已勾選項目。
- 若規則沒有產生候選，卡片可先「重新掃描此商品」；仍無法判定時可開啟該 offer 的完整 SKU 清單手動選擇。手動核准必須選擇目前 live 快照中存在的完整名稱組合；SKU ID 只作輔助，不接受任意輸入不存在的名稱或 ID。
- 完整 SKU 清單可在任何卡片開啟；若先手動選好 SKU，再勾選卡片的「批次處理」，批次核准會優先使用手動選擇，沒有手動選擇的卡片才會提示並使用候選第 1 號。

- 「重建全部名稱 mapping（掃描 1688）」是 1688 live 掃描流程：未勾選強制重抓時會優先使用 7 天內快照，但快照不存在或過期仍可能開啟 1688。若只想用目前資料庫已有快照重新跑規則／Gemini，請按「用現有快照重建名稱 mapping（不連 1688）」；沒有快照的型號會略過，不會觸發瀏覽器。

- 審核分級：綠色代表「唯一候選、所有規格維度精確匹配、live 快照有效」；黃色仍需人工點選候選；紅色只能保留、標記無匹配或停售，禁止猜測。月銷量只影響排列順序，不影響綠／黃／紅分級。名稱組合是主要判定依據，SKU ID 與快照 fingerprint 只作輔助證據；因此 SKU ID 改變但兩個名稱仍唯一存在時，不會因 ID 變更而否定 mapping。若單一候選仍是紅色，畫面會明確標示「需重新掃描」；通常是 live 快照狀態失效、名稱組合消失或擷取失敗，不能直接核准。批次核准仍由使用者勾選控制，未手動選候選時會明確提示並使用第 1 個候選。
- 快照是某次從 1688 讀到的 SKU、規格、價格與庫存目錄，用來保存當時的證據並比對商品是否變更。若資料庫已有較新的 live 快照，系統會自動重新驗證舊 mapping；只要原本的 `1688_sku_name`／`1688_sku_second_name` 名稱組合仍唯一存在，就算 SKU ID 改變也可保留並更新輔助資料，不必重複人工改名。只有沒有可用快照、名稱組合消失或商品下架時，才要求重新掃描或人工處理。
- 批次核准 API 必須帶 `batch=true`；使用者勾選的項目才會送出，未手動點候選的項目會在確認提示後使用第 1 個候選，沒有候選的項目則整批拒絕。
- 工作台會顯示既有 SKU mapping。人工從完整 SKU 清單選擇並核准時，該選擇就是最高優先，會直接寫入並覆蓋既有 mapping；不再另外提供容易混淆的「取代既有 mapping」按鈕。

- golden table 只保存已核准的 mapping；快照、候選、AI 判定、版本與人工稽核紀錄保存於 `procurement.db`。
- 規則完全找不到候選時，SKU mapping 預設使用官方 Google Gemini API（`GEMINI_API_KEY`、模型 `gemini-3.5-flash-lite`）做初判；Gemini 額度／速率限制（429）、API 錯誤或沒有 Key 時，會明確記錄原因並維持規則層的 no-match，不會假裝成 AI 結果。可執行 `python3 setup_gemini_key.py` 以隱藏輸入方式設定 Key；xAI Grok 與 OpenAI 仍可透過 `SKU_MAPPING_AI_PROVIDER` 明確選用。卡片上的「用現有 SKU 清單重跑 AI」是明確的人工覆核動作，可在已有候選時強制呼叫目前設定的 AI；所有 AI 結果仍只進入人工審核，不會直接寫入 golden table。
- 工作台 API：`POST /api/sku-mapping/scans`、`GET /api/sku-mapping/jobs/{id}`、`GET /api/sku-mapping/summary`、`GET /api/sku-mapping/queue`、`POST /api/sku-mapping/decisions`。
- 1688 登入、滑塊或驗證碼需要使用者在持久化 Chrome profile 完成；系統不會付款或送出正式訂單。

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

### 廣告匯出 / 分析目前的 scenario 是什麼？
- `/廣告匯出`：負責準備完整分析資料集，會依序匯出「過去一個月、昨天、近第 1 週到近第 4 週」，共 6 份
- `/廣告分析`：不再重新抓 Shopee 後台，而是直接讀取 `ads_exports/` 內各視窗最新一份 CSV
- 若同一視窗下載多次，分析器只會取該視窗最新一份，不會把舊檔全部混在一起算

### 廣告分析沒有使用 OpenAI？
1. 先執行 `python3 setup_openai_key.py`
2. 確認專案根目錄已有 `.env.local`
3. 檢查 `OPENAI_API_KEY` 是否可用
4. 可用 `python3 inspect_openai_status.py` 測試目前設定是否能正常呼叫 OpenAI API
5. 廣告頁面會明確顯示 API 是否已設定、請求模型、實際回應模型、推理強度與耗時；缺少 Key 或 API 呼叫失敗時會直接報錯，不會假裝成 AI 報告

### Telegram 收到的 HTML 圖片顯示不出來？
廣告分析與庫存報表都會盡量將圖片轉成 Base64 內嵌在 HTML 中。若仍看不到圖片，通常是當次圖片網址抓取失敗，可重新執行一次分析。

### 打包後執行檔無法運行？
確保已將 `cookies.json` 和執行檔放在同一目錄。
