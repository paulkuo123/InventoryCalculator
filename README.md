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
- **反向補貨查核**：以 watchlist ∩ 蝦皮庫存 ∩ Golden Table 算出應補集合，反向核對 1688 採購車與待付款／待發貨／待收貨

## 系統架構

本專案可看成四條主線，加上共用資料檔。正向補貨負責「依建議量加車」；反向查核負責「用現況四池核對應補集合」，兩者不要混用同一套指令。

### UI

- 入口：`python main.py` 啟動內建 HTTP 伺服器並開啟瀏覽器。
- 前端頁面：
  - `/`（`index.html` + `script.js` + `styles.css`）：庫存儀表板、搜尋、補貨判讀
  - `/ads.html`（`ads.js` / `ads.css`）：廣告匯出與 AI 分析工作台
  - `/inbound.html`：1688 到貨入庫蝦皮
  - `/sku-mapping.html`：1688 SKU 名稱 mapping 審核
  - `/products.html`、`/golden-import.html`：商品與 Golden Table 匯入
- 後端路由與 API 集中在 `main.py`（標準庫 `http.server`）。

### 正向補貨（alibaba_restocker）

- 核心：`alibaba_restocker.py`（1688 加採購車、SKU 選擇、MOQ／包裝倍數）。
- 數量規則：`restock_rules.py`（手機殼／手机壳 **3 個月**，其餘 **4 個月**；吊飾／掛繩／明確加購除外）。
- 批次與關注清單：`restock_batch.py`、`scripts/run_watchlist_restock.py`。
- SKU 對照：`sku_mapping_service.py` + `/sku-mapping.html`；已核准結果寫入 `golden_table.json`。離線評估 baseline：`python -m mapping_eval`（見 [`docs/mapping_eval.md`](docs/mapping_eval.md)）。門檻設定：`mapping_knowledge/config.json`（`mapping_knowledge.py::load_config()`）。
- 離線購物車數量核對（不連瀏覽器）：`scripts/reconcile_cart.py`，說明見 [`docs/cart-reconciliation.md`](docs/cart-reconciliation.md)。
- 瀏覽器優先 ego-lite，否則 Chrome／Playwright Chromium。

### 反向查核（reverse_audit）

- 套件：`reverse_audit/`，進入點 `python -m reverse_audit …`。
- 固定流程：**freeze → dry-run（離線）→ 人工核准 → mutate**。每次看最新預覽請用 **refresh** 重抓四池，不要沿用舊的 `live_*.json`。
- freeze／mutate 是 CDP 腳本的薄封裝，以 `runpy` 載入 `scripts/` 內現行實作（含日期戳檔名，**不可刪**）。
- 完整說明與成功標準：[`docs/reverse_audit.md`](docs/reverse_audit.md)。

### 1688 歷史採購知識庫（Phase 2 schema）

- SoT：`procurement.db` 的 `kb_*` 表（與採購／入庫同一檔、加表不改舊表）。歷史訂單**不**進 `inbound_orders`。
- 模組：`purchase_history_store.py`、`purchase_history_import.py`。預設 dry-run；寫入須 `--i-approve-kb-import` + `--allow-order-ids` + 隔離 `--db-path`。
- **不做** live crawl／開 Chrome／寫 `golden_table.json`。說明：[`docs/1688_purchase_history_kb.md`](docs/1688_purchase_history_kb.md)。

### 廣告

- 匯出：`crawler.py --mode ads-export`（寫入 `ads_exports/`）。
- 分析：`ads_analysis.py`（規則層 + 可選 OpenAI），工作台為 `ads.html`。
- 文件：[`docs/ads_analysis_rules.md`](docs/ads_analysis_rules.md)、[`docs/ads_metrics_dictionary.md`](docs/ads_metrics_dictionary.md)、[`docs/ads_report_prompt_spec.md`](docs/ads_report_prompt_spec.md)。

### 資料檔

| 檔案／目錄 | 說明 |
|---|---|
| `golden_table.json` | 已核准 1688 mapping 與庫存快取（**納入 git，請保留**） |
| `shopee_products.json` | 蝦皮商品／銷售快照（gitignore，本機資料） |
| `cookies.json` | 蝦皮登入 Cookies（gitignore，**勿提交**） |
| `watchlists/` | 個人關注與排除清單 |
| `reports/` | reverse_audit／廣告等產出（應 gitignore；含 live dump，勿提交） |
| `alibaba_chrome_profile/`、`alibaba_browser_profile/`、`shopee_chrome_profile/` | 本機瀏覽器登入狀態（gitignore，勿提交） |
| `debug_snapshots/` | 除錯快照（gitignore） |
| `data/1688_master/` | 1688 歷史採購 KB 的 JSONL 匯出（gitignore；非正式 SoT） |
| `data/mapping_eval/` | SKU mapping 離線評估報告（gitignore；勿提交） |
| `procurement.db` | 採購／入庫／SKU mapping／`kb_*` 共用 SQLite（gitignore） |

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
1688 SKU live scan 與「開啟 1688 加採購車」都優先透過 **ego-lite / ego-browser** 開啟商品頁，沿用 ego-lite 的登入狀態與各自獨立的 task space。若電腦沒有安裝 `ego-browser`，採購車流程會自動退回 Google Chrome；Chrome 無法啟動時再退回 Playwright Chromium。

**必要條件**：
1. 建議安裝 ego-lite 並確認可由 `ego-browser nodejs` 控制；未安裝仍可使用 Chrome／Playwright Chromium 採購車流程
2. 若 1688 出現登入或滑塊驗證，系統會保留目前選定瀏覽器的頁面，完成後再重新執行

預設 Task Space 為 `InventoryCalculater 1688 live scan`（唯讀掃描）及 `InventoryCalculater 1688 restock`（採購車）；可分別用 `EGO_BROWSER_TASK_SPACE`、`EGO_BROWSER_RESTOCK_TASK_SPACE` 覆寫。

`ALIBABA_RESTOCK_BROWSER=auto` 是預設值；也可設成 `ego` 強制要求 ego-lite，或設成 `playwright` 讓沒有／不使用 ego-lite 的環境固定走 Chrome／Chromium。ego-lite 已被選用後若發生執行、登入或驗證錯誤，不會中途切換瀏覽器。

舊有蝦皮爬蟲仍可使用 Playwright Chromium；這與 1688 SKU live scan 的瀏覽器路徑分開。

### 設置 Cookies
1. 至 Chrome 線上應用程式商店安裝 Cookie Editor (https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm?hl=zh-TW&utm_source=ext_sidebar)
2. 登入蝦皮賣家中心
3. 點擊 Cookie Editor 擴充功能
4. 點擊「Export」輸出 JSON 格式
5. 將內容複製並貼到 `cookies.json` 檔案（專案根目錄）

### 設置 OpenAI API Key（廣告分析與 SKU mapping）
OpenAI 設定建議使用專案本地設定檔，不要把 Key 寫進程式碼或提交到 Git。

1. 執行初始化腳本：
```bash
python3 setup_openai_key.py
```

2. 腳本會將 Key 寫入專案根目錄下的 `.env.local`

   同時會分別設定廣告分析與 SKU mapping：

   ```text
   OPENAI_MODEL=gpt-5.6-sol
   OPENAI_REASONING_EFFORT=xhigh
   SKU_MAPPING_AI_PROVIDER=openai
   OPENAI_SKU_MAPPING_MODEL=gpt-5.6-luna
   OPENAI_SKU_MAPPING_REASONING_EFFORT=low
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
2. 若到貨入庫專用瀏覽器尚未登入 1688，先在開啟的視窗完成登入或驗證。
3. 確認每筆實收、不良、蝦皮增加量與商品規格對照；同一實體品若有多個蝦皮刊登，需自行分配數量。
4. 建立到貨單後先執行「讀取即時庫存」。系統此時只讀取，不會修改蝦皮。
5. 按單規格或「此商品全部入庫」後，系統會先核對「目前庫存 + 增加量 = 更新後庫存」，再顯示一次最後確認。
6. 確認後會在蝦皮「我的商品」以商品 ID 搜尋，開啟「設定庫存」視窗；同一商品的多個規格一次填寫、一次更新，並回到商品列重新驗證。
7. 正式寫入需在 `.env.local` 設定 `SHOPEE_INBOUND_WRITE_ENABLED=true` 並重新啟動。成功項目有本地防重複紀錄；按下更新後若結果不明，不會自動重試。

入庫紀錄保存在 `procurement.db`。`golden_table.json` 只會在蝦皮儲存並重新讀取驗證成功後同步庫存快取。

### 反向查核怎麼跑

詳細參數、水位與成功標準見 [`docs/reverse_audit.md`](docs/reverse_audit.md)。輸出一律落在 `reports/reverse_audit_YYYYMMDD/`。未給 `--date`／`--dir` 時，日期預設為 Asia/Taipei 今天。

**1. 重抓四池再預覽（建議每次都用這個）**

採購車與待付款／待發貨／待收貨是動態的。需要本機已登入的 Chrome remote debugging（CDP）。`--sources-only` 只凍本地來源、不跑 CDP。

```bash
python -m reverse_audit refresh --date YYYYMMDD
# 等同：python -m reverse_audit dry-run --date YYYYMMDD --refreeze
python -m reverse_audit freeze --date YYYYMMDD
```

**2. 離線 dry-run（不加車、不改量、不刪除）**

讀既有 `live_*.json` 與凍結來源，產出人工主檔 `補貨比對結果.csv` 與機器用 CSV。

```bash
python -m reverse_audit dry-run --date YYYYMMDD
```

**3. mutate（改車；須明確核准旗標，互不隱含；缺旗標立即拒絕）**

```bash
# 只加車（missing_to_add.csv）
python -m reverse_audit mutate --date YYYYMMDD --i-approve-mutate
# 只改量到 expected（shortfall 上補、excess 下砍）
python -m reverse_audit mutate --date YYYYMMDD --i-approve-set-qty
# 只刪 unexpected 且 removable=true（預設絕不刪）
python -m reverse_audit mutate --date YYYYMMDD --i-approve-remove
```

freeze／mutate **不會**清除購物車或結束 Chrome。購物車「書包截止」的離線核對流程見 [`docs/cart-reconciliation.md`](docs/cart-reconciliation.md)。

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

**週報（營運顧問）請改跑遠端盒上的 `python3 ads_weekly.py`**，不要再透過 ListMachines 打 Mac。預設連已登入的 Chrome CDP（`http://127.0.0.1:9232` 或 `SHOPEE_ADS_CDP`）。輸出在 `reports/ads_weekly/YYYYMMDD/`。登入牆／驗證碼會寫 `BLOCKER.md` 並失敗結束。見 [`docs/ads_weekly_pipeline.md`](docs/ads_weekly_pipeline.md)。

### 廣告分析
當 `ads_exports/` 內已有廣告 CSV 後，可手動執行分析：

```bash
python3 ads_analysis.py --include-ai true
```

規則、指標定義與 prompt 規格見 [`docs/ads_analysis_rules.md`](docs/ads_analysis_rules.md)、[`docs/ads_metrics_dictionary.md`](docs/ads_metrics_dictionary.md)、[`docs/ads_report_prompt_spec.md`](docs/ads_report_prompt_spec.md)。

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
InventoryCalculator/
├── main.py                 # Web 伺服器入口（庫存 / 廣告 / 入庫 / mapping API）
├── index.html / script.js / styles.css
├── ads.html / ads.js / ads.css
├── inbound.html / products.html / sku-mapping.html / golden-import.html
├── alibaba_restocker.py    # 正向 1688 加採購車
├── restock_batch.py / restock_rules.py
├── sku_mapping_service.py  # SKU 快照、候選、AI 與審核
├── mapping_eval.py         # 離線 mapping 評估 baseline（不改 golden、不 auto-approve）
├── mapping_knowledge.py    # mapping 知識包設定載入（缺失／無效 JSON 回退預設）
├── mapping_knowledge/      # 知識包檔（TASK 2：config.json；尚無 aliases／rules）
├── ads_analysis.py         # Shopee 廣告分析
├── ads_weekly.py           # 週報抓取（預設遠端 CDP，不走 Mac）
├── ads_session.py          # 遠端工作階段／BLOCKER 輔助
├── crawler.py              # 蝦皮爬蟲與廣告匯出
├── reverse_audit/          # 反向查核套件（freeze / refresh / dry-run / mutate）
├── scripts/                # 現行 CDP 輔助腳本（freeze／mutate 以 runpy 載入，勿刪）
│   ├── freeze_reverse_audit_pools_20260905.py
│   ├── mutate_add_missing_cdp_20260906.py
│   ├── mutate_set_qty_cdp.py
│   ├── mutate_remove_cdp.py
│   ├── reconcile_cart.py
│   └── run_watchlist_restock.py
├── purchase_history_store.py / purchase_history_import.py
│                           # 1688 歷史採購 KB（kb_*；Phase 2 schema／dry-run stub）
├── docs/
│   ├── reverse_audit.md
│   ├── 1688_purchase_history_kb.md
│   ├── cart-reconciliation.md
│   ├── ads_analysis_rules.md
│   ├── ads_metrics_dictionary.md
│   ├── ads_report_prompt_spec.md
│   ├── ads_weekly_pipeline.md
│   └── mapping_eval.md
├── tests/                  # pytest（含 reverse_audit 離線安全測試）
├── watchlists/             # 個人關注與排除清單
├── golden_table.json       # 已核准 mapping（納入 git）
├── cookies.json            # 蝦皮 Cookies（gitignore，需自行設置）
├── shopee_products.json    # 蝦皮庫存快照（gitignore）
├── reports/                # 產出目錄（gitignore；含 reverse_audit live dump）
├── calculator.py / parser.py / telegram_bot.py / build.py
└── requirements.txt
```

## 技術棧

## 1688 SKU Mapping 工作台

`/sku-mapping.html` 是整個賣場的 1688 SKU 對照獨立審核頁。按「重建全部名稱 mapping（掃描 1688）」後，系統會以 1688 offer 分組取得結構化 SKU 快照，先做繁簡／顏色／尺寸／手機型號等規則比對；只要規則找到候選，就不呼叫 AI，直接交給人工確認。只有規則完全找不到候選時，才會把完整 SKU 清單交給設定的 Gemini 初判；之後在頁面逐筆或批次核准。清單預設依商品總月銷量由高到低排序，同一商品的不同型號會集中在一起，再依型號月銷量排列。未核准、失效、停售、名稱組合不存在或 live 目錄無法驗證的資料會被購物車流程阻擋。

- 工作台目前以所有已有 1688 URL 的型號為範圍，不再以「是否需要補貨」作為顯示或掃描條件；補貨數量欄位仍保留在庫存與購物車流程中。
- 清單每頁最多顯示 200 筆，使用頁面底部的上一頁／下一頁瀏覽全部 URL 型號；跨頁勾選仍會保留，批次動作會一次套用所有已勾選項目。
- 若規則沒有產生候選，卡片可先「重新掃描此商品」；仍無法判定時可開啟該 offer 的完整 SKU 清單手動選擇。手動核准必須選擇目前 live 快照中存在的完整名稱組合；SKU ID 只作輔助，不接受任意輸入不存在的名稱或 ID。
- 完整 SKU 清單可在任何卡片開啟；若先手動選好 SKU，再勾選卡片的「批次處理」，批次核准會優先使用手動選擇，沒有手動選擇的卡片才會提示並使用候選第 1 號。

- 「重建全部名稱 mapping（掃描 1688）」是 1688 live 掃描流程：未勾選強制重抓時會優先使用 7 天內快照，但快照不存在或過期仍可能開啟 1688。若只想用目前資料庫已有快照重新跑規則／Gemini，請按「用現有快照重建名稱 mapping（不連 1688）」；沒有快照的型號會略過，不會觸發瀏覽器。

- 審核分級：綠色代表「唯一候選、所有規格維度精確匹配、live 快照有效」，或 AI 有效選中目前候選且信心指數達 95% 以上；黃色仍需人工點選候選；紅色只能保留、標記無匹配或停售，禁止猜測。月銷量只影響排列順序，不影響綠／黃／紅分級。名稱組合是主要判定依據，SKU ID 與快照 fingerprint 只作輔助證據；因此 SKU ID 改變但兩個名稱仍唯一存在時，不會因 ID 變更而否定 mapping。若單一候選仍是紅色，畫面會明確標示「需重新掃描」；通常是 live 快照狀態失效、名稱組合消失或擷取失敗，不能直接核准。批次核准仍由使用者勾選控制，未手動選候選時會明確提示並使用第 1 個候選。
- 快照是某次從 1688 讀到的 SKU、規格、價格與庫存目錄，用來保存當時的證據並比對商品是否變更。若資料庫已有較新的 live 快照，系統會自動重新驗證舊 mapping；只要原本的 `1688_sku_name`／`1688_sku_second_name` 名稱組合仍唯一存在，就算 SKU ID 改變也可保留並更新輔助資料，不必重複人工改名。只有沒有可用快照、名稱組合消失或商品下架時，才要求重新掃描或人工處理。
- 批次核准 API 必須帶 `batch=true`；使用者勾選的項目才會送出，未手動點候選的項目會在確認提示後使用第 1 個候選，沒有候選的項目則整批拒絕。
- 工作台會顯示既有 SKU mapping。人工從完整 SKU 清單選擇並核准時，該選擇就是最高優先，會直接寫入並覆蓋既有 mapping；不再另外提供容易混淆的「取代既有 mapping」按鈕。

- 離線評估既有規則／分級（不寫 golden、不 auto-approve）：`python -m mapping_eval run --db-path procurement.db --out data/mapping_eval/baseline/`。CI 用 `--fixture tests/fixtures/mapping_eval`。說明見 [`docs/mapping_eval.md`](docs/mapping_eval.md)。
- golden table 只保存已核准的 mapping；快照、候選、AI 判定、版本與人工稽核紀錄保存於 `procurement.db`。
- 規則完全找不到候選時，SKU mapping 預設使用 OpenAI Responses API（`OPENAI_API_KEY`、模型 `gpt-5.6-luna`、low reasoning）做初判，並以嚴格 JSON Schema 接收結果。執行 `python3 setup_openai_key.py` 會以隱藏輸入方式儲存 Key 並把 provider 切換為 OpenAI。API 額度／速率限制（429）、API 錯誤或沒有 Key 時，會明確記錄原因並維持規則層的 no-match，不會假裝成 AI 結果。卡片上的「用現有 SKU 清單重跑 AI」是明確的人工覆核動作；所有 AI 結果仍只進入人工審核，不會直接寫入 golden table。
- 工作台 API：`POST /api/sku-mapping/scans`、`GET /api/sku-mapping/jobs/{id}`、`GET /api/sku-mapping/summary`、`GET /api/sku-mapping/queue`、`POST /api/sku-mapping/decisions`。
- 1688 登入、滑塊或驗證碼需要使用者在 ego-lite task space 完成；系統不會付款或送出正式訂單。

- **後端**: Python 3.8+
- **網頁爬蟲**: Playwright (Chromium)
- **GUI**: PyQt5（獨立庫存計算器）
- **Web 框架**: 內建 `http.server` 模組
- **數據處理**: pandas（Excel 解析）
- **廣告分析**: OpenAI API + 自訂規則層
- **反向查核**: `python -m reverse_audit`（CDP freeze／mutate + 離線 dry-run）
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
