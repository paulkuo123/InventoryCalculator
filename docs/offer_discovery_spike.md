# TASK 9：Offer Discovery Spike（第 1 層，設計 only）

本文件是 1688 SKU Mapping Know-how Engine v1 的 **TASK 9** 設計 spike。  
**只討論**「蝦皮商品／型號 → 1688 offer」要不要自動化、先用哪一個來源。  
**不是**實作。**不**寫 crawler／搜尋自動化。**不**開 Chrome。**不**改 `golden_table.json` schema。**不**啟用 `auto_approve.enabled`。

Know-how Engine 總覽見 [`sku_mapping_knowhow_engine.md`](sku_mapping_knowhow_engine.md)。歷史 KB schema 見 [`1688_purchase_history_kb.md`](1688_purchase_history_kb.md)。

## 問題切層

現況 matcher（`SkuMappingService`）是 **第 2 層**：已經有 1688 offer URL／`offer_id`，再對該 offer 的 SKU 清單做綠／黃／紅建議。

```text
第 1 層  Shopee product / model  ──?──►  1688 offer（本 spike）
第 2 層  已知 offer snapshot     ──►    1688 SKU（TASK 1–8，已落地）
```

第 2 層掃描只收「已有 URL」的型號：`_scope_models()` 遇到空的 `阿里巴巴商品URL` 直接 `continue`。沒有 URL 的型號不會進 `start_scan`，queue 只長出唯讀 `missing_url`／紅色「尚未設定 1688 URL」。沒有 offer，第 2 層與 auto-approve 反事實都沒有分母。

所以第 1 層若要做，目標是：**給 missing_url 型號一個可審核的 offer 建議**，再交給既有 `EgoBrowser1688.fetch(已知 URL)` 與 `generate_candidates()`。不是另做一套搜尋引擎，也不是自動寫 Golden。

## 1. 既有可重用能力（讀真實檔，不發明 API）

本機工作區沒有掛上 `_handoff/`（該路徑不在 git、也不應 commit）。下列 API 以 repo 原始碼為準。

### `ego_browser_1688.py` — 已知 offer 頁，唯讀

`EgoBrowser1688` 是 SKU mapping scanner 對 ego-lite 的 subprocess 橋。用途是**打開一張已知 1688 URL**，重用操作者已登入的 task space。

| 符號 | 實際行為 |
|---|---|
| `DEFAULT_TASK_SPACE` | `"InventoryCalculater 1688 live scan"` |
| `EGO_BROWSER_TASK_SPACE` / `EGO_BROWSER_COMMAND` | 環境變數覆寫 task space／指令 |
| `EgoBrowser1688(timeout_seconds=90)` | timeout 下限 10 秒 |
| `fetch(url) -> dict` | `ego-browser nodejs`；`openOrReuseTab(url)`；從 `window.context.result.data.mainPrice.fields.finalPriceModel` 抽 `skuMapOriginal` rows |
| `finish(keep=True)` | `completeTaskSpace`；登入牆時 `keep=True` 留給人 |
| `_classify_page(page)` | 靜態分類，見下表 |

`fetch` 回傳（呼叫端實際使用的欄位）：

| `status` | 其他欄位 | 意義 |
|---|---|---|
| `ok` | `title`, `url`, `body`, `rows`, `health_status`, `health_reason` | 頁面讀到了。`health_status`：`valid`（`*.1688.com` + `/offer/`）、`invalid`（下架／`wrongpage.html`）、`error`（非 offer 終頁） |
| `waiting_for_login` | `health_status=needs_attention`, `health_reason=login_or_verification` | 驗證碼／滑塊／「請先登入」或 URL 含 `login`／`passport`／`member`。**不自動登入** |
| `error` | `error_message` | 找不到指令、timeout、ego 非正常結束、回傳不是 dict |

**沒有的 API：** 關鍵字搜尋、listing 分頁、自動填帳密、cookie 匯入、訂單 list、寫購物車。  
`sku_mapping_service._fetch_live_snapshot()` 只在**已知 URL** 上呼叫 `fetch`；`start_snapshot_reanalysis()` 刻意不開瀏覽器。

可重用結論：第 1 層一旦**已經有 offer URL**，第 2 層快照路徑可原樣用。不能拿 `fetch` 當站內搜尋。

### `alibaba_client.py` — Open Platform 佔位，沒有商品搜尋

`AlibabaApiClient` docstring 寫明是 placeholder：憑證可先填，真正 AOP 端點尚未接入。

| 方法 | 實際行為 |
|---|---|
| `__init__(base_dir)` | 讀環境變數或 `.env.local` 的 `ALIBABA_APP_KEY`／`ALIBABA_APP_SECRET`／`ALIBABA_ACCESS_TOKEN`／`ALIBABA_REFRESH_TOKEN` |
| `auth_status()` | 缺憑證 → `missing_credentials`；有憑證 → `configured_pending_implementation`。**`can_create_order` 與 `can_query_orders` 永遠 `False`** |
| `create_pending_order(lines)` | `PermissionError` 或 `NotImplementedError("1688 建立待付款訂單 API 尚未接入")` |
| `get_order_status(alibaba_order_id)` | 同上，訂單查詢未接入 |
| `load_local_env(base_dir)` | 解析 `.env.local` 的 `KEY=VALUE` |

`main.py` 只用 `auth_status()`／`create_pending_order`／`get_order_status` 做採購草稿與同步。  
**沒有** `search_offers`、`get_product`、`list_orders`。第 1 層不能指望 Open Platform。

### `crawler.py` — 蝦皮賣家中心，不是 1688

唯一公開類別是 `ShopeeCrawler`。Playwright／遠端 CDP 都打 `seller.shopee.tw`。

| 可重用（蝦皮側） | 與 1688 offer discovery **無關** |
|---|---|
| `login()` / `_ensure_remote_seller_session()` | 蝦皮 cookies 或已登入 Chrome CDP |
| `get_monthly_sales(product_name)` / `get_all_products_info()` | 賣家中心月銷、商品列 |
| `export_ads_report()` | 廣告 CSV |
| `_load_golden_table()` | 讀本機 `golden_table.json`，把已有的 `1688_offer_id`／`1688_sku_*` **抄進蝦皮列** |

`get_monthly_sales` 的搜尋框是蝦皮商品名稱，不是 1688。沒有 1688 hostname、沒有 `/offer/`、沒有 1688 cookie。  
可重用結論：第 1 層**不要** fork `ShopeeCrawler` 去爬 1688。它能提供的是蝦皮側 `product_name`／`model_name`，當查詢鍵。

### 相關、但本 spike 不當成搜尋客戶端

| 模組 | 與第 1 層的關係 |
|---|---|
| `purchase_history_store.PurchaseHistoryStore` | 隔離 SQLite 的 `kb_orders`／`kb_order_items`／`kb_products`／`kb_skus`／`kb_purchase_history`。`list_purchase_history()`、`get_order()`、`import_order()` 已存在。`db_path=` 可指向隔離檔 |
| `purchase_history_import` | Phase 2 dry-run／gated stub。`PHASE3_HISTORY_IMPORT_ENABLED = False`、`PHASE3_LIVE_CRAWL_ENABLED = False`。`crawl` 子命令永遠拒絕 |
| `sku_mapping_service.historical_support()` | **第 2 層**分數：同 offer 其他核准、跨 offer 同名核准。**預設 offer 已知**。可選讀同 DB `kb_mappings`（缺表就忽略） |
| `ego_browser_page.EgoBrowserPage` | 補貨 task space 的 Playwright-like adapter（`goto`／`evaluate`），不是搜尋 |

## 2. 1688 站內搜尋的限制

現有三個模組都**沒有**站內搜尋客戶端。若將來當備援，限制如下（來自現況分類與 KB 文件，不是新爬蟲實驗）：

1. **登入牆。** `EgoBrowser1688._classify_page` 已把驗證碼、滑塊、安全驗證、「請先登入／登录后查看」標成 `waiting_for_login`，並停下來等人。搜尋頁比 offer 詳情頁更容易觸發。
2. **Anti-bot／工作階段不穩。** `fetch` timeout 預設 90 秒；timeout 會換 `[agent-retry]` task space。ego 非正常結束只回錯誤字串。Cloud Agent／CI **不准**開操作者 Chrome。
3. **結果不穩。** 站內排序隨登入、地區、廣告、賣家分層變動；同一蝦皮品名多次搜尋不必得到同一 offer。Know-how 評估要可重現，站內搜尋當第一來源會讓 Top-1／Green 分母漂掉。
4. **沒有官方搜尋 API。** `AlibabaApiClient` 連訂單查詢都未接入。
5. **誤綁代價高。** 錯 offer 會讓第 2 層在「錯誤目錄」上走出綠色唯一匹配，污染 Golden，再進購物車／入庫。

因此站內搜尋只能當**最後備援**，且必須人工挑 URL，不得自動寫入。

## 3. 候選來源優先序

第 1 層建議 offer 時，固定這三層。**不得**跳過 1、2 直接搜 1688。

### 來源 1（先做）：隔離種子庫的歷史 Offer

路徑（操作者 handoff，**不進 git**）：

```text
/workspace/_handoff/kb_excel_success_isolated_20260912.db
```

操作者標示：Excel／KB 匯入的**交易成功**歷史，約 **3309** 筆。比站內搜尋穩，因為 offer 來自**已經買過**的訂單，不是搜尋排名。

預期表（與 `purchase_history_store.KB_TABLES` 對齊；本 spike 工作區未掛上該檔，實作前用唯讀 `PRAGMA`／`SELECT COUNT` 核對，不要把數字抄進 production）：

| 表 | 第 1 層用什麼 |
|---|---|
| `kb_orders` | `alibaba_order_id`、`status`、`ordered_at`、`seller`（成功單過濾） |
| `kb_order_items` | `offer_id`、`sku_id`／`sku_key`、`raw_specs`、`qty`、`price_cny` |
| `kb_products` | `offer_id` PK、`product_url`、`title`、`shop_id` |
| `kb_skus` | 該 offer 曾買過的規格；**禁止**用名稱／圖片把 `unresolved_id` 自動併成真實 `sku_id` |
| `kb_purchase_history` | 依 `(offer_id, sku_key)` 彙總：`order_count`、`total_qty`、`last_ordered_at` |
| `kb_mappings` | 僅當列的 `source` 已是 `golden_approved`／`inbound_exact`／`manual` 快照；不可回寫 Golden |

建議查詢鍵（設計，未實作）：

1. 種子列若已有 `shopee_product_id`／`shopee_model_id`（`kb_mappings`）→ exact。
2. 否則用蝦皮 `商品名稱`／`型號名稱` 對 `kb_products.title`、`kb_order_items.raw_specs`／`kb_skus.raw_specs` 做**可稽核**的正規化比對（沿用第 2 層 `normalize_text`／硬規則，不當模糊合併）。
3. 同分時用 `kb_purchase_history.order_count`／`last_ordered_at`、同一 `shop_id` 加權。

閘門（沿用 Phase 2，本任務不改旗標）：

- 種子檔只**唯讀** attach（`PurchaseHistoryStore(db_path=...)` 或 sqlite 唯讀 URI）。
- **不**把 3309 筆灌進 live `procurement.db`。`PHASE3_HISTORY_IMPORT_ENABLED` 維持 `False`。
- **不**開 `PHASE3_LIVE_CRAWL_ENABLED`，不打 1688 list POST。
- 建議只進審核 queue，**不**寫 `golden_table.json`。
- 種子檔與 `_handoff/` 維持 gitignore／本機，CI 用小型 fixture 模擬「歷史 offer 命中」，不要提交 `.db`。

### 來源 2：同蝦皮商品其他型號已綁的 Offer（Golden）

`golden_table.json` 裡，同一 `product_id` 的多個型號常常共用一個 `1688_offer_id`／`阿里巴巴商品URL`。審核 queue 已按 `(product_id, offer_id)` 分組（`groupId = product_id|||offer_id`）。

對 `missing_url` 型號：

1. 收集同商品其他型號已有的 `1688_offer_id`（或 `parse_offer_id(阿里巴巴商品URL)`）。
2. 若**恰好一個** distinct offer → 高信心建議（仍要人確認 URL，再走第 2 層 SKU）。
3. 若多個 offer → 列出，標黃／紅，不自動挑。
4. 不要跨不同蝦皮 `product_id` 複製 offer（那是另一個商品）。

這層今天就有資料、不用瀏覽器、不碰種子庫。排在來源 1 之後，是因為「兄弟型號已綁」可能是人工沿用的舊連結；種子庫的成功交易比較能證明「我們真的買過這個 offer」。兩層都命中且 offer 不同 → 並列給人，**不要**默認覆蓋 Golden。

### 來源 3（最後備援）：1688 站內搜尋

僅當 1、2 都沒有可用 offer。本 spike **不設計、不實作**搜尋客戶端。若以後另開任務，必須：人工挑結果、沿用 `EgoBrowser1688` 的登入牆（不可自動過）、結果當建議不當真相、預設關閉且與 `auto_approve` 無關。

`crawler.py`／`AlibabaApiClient` 都沒有可接的搜尋函式。

## 4. 成功指標

第 1 層與第 2 層分母分開。本 spike 不改 `mapping_eval` fixture，也不開 auto-approve。

只評估 **Golden 上 `missing_url`（或等價：沒有 `阿里巴巴商品URL`／`1688_offer_id`）的型號**。已核准且已有 offer 的列不當成功案例。

| 指標 | 定義 | 分母 | 通過方向 |
|---|---|---|---|
| **Seed Offer Coverage** | 來源 1 提出至少一個 `offer_id` | missing_url | 越高越好；先報絕對數 |
| **Seed Unique-Offer Rate** | 來源 1 恰好一個 distinct offer | 有建議的案例 | 高 → 才值得做半自動填 URL |
| **Sibling Offer Coverage** | 來源 2 提出至少一個已綁 offer | missing_url | 補充來源 1 |
| **Offer Suggestion Precision** | 建議的 top offer 等於後來人工核准寫入 Golden 的 offer | 有人工核准 offer 的 missing_url | **核心安全**；低於「明顯優於亂猜」就不要半自動填 |
| **Conflict Rate** | 來源 1 與來源 2 的 top offer 不同 | 兩層都有建議 | 必須進人工，禁止自動消解 |
| **Site-search Invocation Rate** | 走到來源 3 的比例 | missing_url | v1 目標接近 **0** |
| **Golden writes from layer-1** | 第 1 層程式寫入 Golden 的次數 | — | 必須 **0** |
| **Chrome / live 1688 calls** | 為了「找 offer」而開的瀏覽器或搜尋請求 | — | 來源 1、2 必須 **0** |

建議離線量測（設計，未做）：

1. 唯讀打開種子 DB，計算 distinct `kb_order_items.offer_id`、成功單數（核對是否接近 3309）。
2. 對 Golden 每個 missing_url 型號跑來源 1 → 來源 2，寫出 coverage／unique／conflict（gitignore 報告）。
3. 有歷史已補 URL 的型號可做回溯：藏起 URL，看來源 1／2 是否找回同一 `offer_id`（**不當**成自動核准）。

不把站內搜尋精度列進 v1 通過條件。

## 5. 結論（本 spike 必須回答的兩題）

### 第 1 層（Shopee product → 1688 offer）值得自動化嗎？

**值得做成「建議層」，不值得做成「搜尋爬蟲／自動寫入」。**

理由：沒有 offer，第 2 層掃描與 Know-how 評估都碰不到這些型號（`_scope_models` 跳過、queue 只顯示 `missing_url`）。缺口真實存在。但現有 `ego_browser_1688.fetch`、`AlibabaApiClient`、`crawler.py` **都不能**安全地從品名搜到 offer；站內搜尋有登入、anti-bot、排名不穩、誤綁會污染 Golden。

因此：自動化應停在「從已發生的採購與已綁 Golden 提出 offer 候選 → 人工確認 URL → 既有第 2 層 SKU 掃描」。**不要**自動寫 `golden_table.json`，**不要**開 `auto_approve.enabled`。

### 應該先做哪一個來源？

**先做來源 1：隔離種子庫** `/workspace/_handoff/kb_excel_success_isolated_20260912.db` 裡交易成功歷史的 Offer（`kb_orders`／`kb_order_items`／`kb_products`／`kb_purchase_history`）。**不是** 1688 站內搜尋。

| 順序 | 來源 | v1 |
|---|---|---|
| 1 | 隔離種子歷史 Offer（約 3309 筆成功交易） | **先做**（唯讀查詢 + 建議 + 指標） |
| 2 | 同蝦皮商品其他型號已綁 Golden offer | 緊接，本機即可，零瀏覽器 |
| 3 | 1688 站內搜尋 | **不做**；只保留為備援假設 |

來源 1 比搜尋穩：offer 來自已付款／交易成功紀錄，可用 `PurchaseHistoryStore` 既有表，不必新爬蟲。種子未掛進此工作區也不要改 production 模組去「補」它；下一步若實作，用 `--db-path` 唯讀 attach，並用小型 fixture 覆蓋查詢，不提交 `.db`。

## 刻意不做（本 PR / 本文件）

- 新的 production crawler、搜尋模組、Playwright／ego 搜尋腳本
- 開啟 Chrome 或對 1688 打 list／search
- 改 `golden_table.json` schema
- 把種子庫 3309 筆匯入 live `procurement.db`
- 將 `PHASE3_*` 或 `auto_approve.enabled` 設成 `true`
- 用名稱／圖片相似度合併 `unresolved_id`
