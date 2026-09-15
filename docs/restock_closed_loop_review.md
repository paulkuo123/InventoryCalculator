# 補貨閉環審查（Goal A）

對照庭安要的最終流程，審查 **main 現況**（本文件寫作時：`340db20`）。  
只讀程式與既有文件，**不**改 `golden_table.json`、live `procurement.db`、ROAS 門檻、`auto_approve.enabled`，也**不**重啟 Golden 寫入管線。

相關：[`reverse_audit.md`](reverse_audit.md)、[`cart-reconciliation.md`](cart-reconciliation.md)、[`sku_mapping_knowhow_engine.md`](sku_mapping_knowhow_engine.md)、[`golden_ai_automation_review.md`](golden_ai_automation_review.md)。

## 1. 庭安要的最終流程

1. **定期**跑 `python main.py`：搜尋「隔日到貨」、掃觀察清單庫存。
2. 庫存碰到低水位 → **提醒**，並問要不要補貨。
3. 使用者說要 → **自動**對整份觀察清單跑 1688 補貨。
4. 補完 → **核對**應補 vs 實際 1688 採購車，標出不一致。

店規（已對程式核對）：手機殼 3 個月、其餘 4 個月；範圍是 watchlist ∩ `shopee_products.json` ∩ `golden_table.json`；不發明 1688 URL／`skuId`；車內不足要暫停、超量只列出、非預期車列要追蹤。

## 2. 對「已知規則」的訂正

下列是讀完 main 後，對 handoff 敘述的修正。沒寫「訂正」的，與程式一致。

| 敘述 | 判定 | 依據 |
|---|---|---|
| 手機殼 3 個月、其餘 4 個月（吊飾／掛繩／明確加購除外） | **正確**（CLI／反向查核） | `restock_rules.target_months_for_product`、`PHONE_CASE_MONTHS=3`、`DEFAULT_RESTOCK_MONTHS=4` |
| 反向查核：freeze → dry-run → 核准 → mutate；沒旗標 fail-closed | **正確** | `docs/reverse_audit.md`、`reverse_audit/cli.py`、`reverse_audit/mutate.py` |
| 「今天只能加車；set-qty／remove 還沒做完」 | **已過時** | main 已有獨立旗標 `--i-approve-set-qty`、`--i-approve-remove`，以及 CDP 腳本 `scripts/mutate_set_qty_cdp.py`、`scripts/mutate_remove_cdp.py`。沒對應旗標就不會做該動作。短少仍會 **PAUSE 加車**，除非同一次已核准並完成 set-qty |
| 範圍：watchlist ∩ shopee ∩ golden | **正確**（反向查核） | `reverse_audit.dry_run.build_expected`：`watch_ids` 且 `pid in products and pid in golden` |
| 不發明 URL／skuId | **正確** | `dry_run.py` 模組說明；uncertain 只記不猜；mutate 加車列缺 `offer_id`／`sku_id`／http URL 會拒 |
| 不足 → pause；超量只列；非預期車列追蹤 | **正確** | `qty_shortfall.csv` → `status=PAUSED`；`qty_excess.csv` 不 PAUSE；`unexpected_in_cart.csv` |
| 1688 歷史種子庫隔離、Know-how v1 在 main、`auto_approve` 仍 false | **正確** | `docs/1688_purchase_history_kb.md`、`mapping_knowledge_pack/config.json`、`docs/sku_mapping_knowhow_engine.md` |
| 廣告成本 Phase 0 與本閉環無關 | **正確**；本文件不展開 | [`ads_cost_margin_design.md`](ads_cost_margin_design.md) |

額外、容易踩到的現況：

- **首頁／爬蟲的水位月數不是 3／4 分流。** `index.html` 的 `#inventoryMonth` 預設 4；`script.js` 的 `calculateModelRestock`／`calculateInventoryStatistics` 用同一個月數。`crawler.py` 用 `--inventory-month`（預設 4）寫 `建議補貨數量`。真正套用店規的是 `scripts/run_watchlist_restock.build_list_from_files` 與 `reverse_audit.dry_run.build_expected`。低水位提醒改走 **Grok Bot routines**（不再經 Telegram、也不另建應用內推播）。
- **repo 觀察清單不會自動進首頁表格。** `script.js` 寫明 “Project watchlists are never bootstrapped”；表格靠現場搜尋或本機匯入。`GET /api/home/bootstrap`（`home_bootstrap.load_home_bootstrap`）會讀磁碟上的 `shopee_products.json` + `watchlists/personal_watchlist.json`，但驗證技能與原始碼都顯示**首頁不會拿它填表**。
- **襪子不進觀察清單補貨。** `home_bootstrap.is_watchlist_excluded_product_name` 看到「襪／袜」就排除；另有 `personal_watchlist_exclusions.json`（66 個 productId）。現況 watchlist 158 個商品。

## 3. 現況 vs 期望（四步）

### 步驟 1 — 定期搜「隔日到貨」並掃觀察清單

**期望：** 週期性、可重複、掃的是觀察清單庫存。

**現況：**

| 元件 | 實際行為 |
|---|---|
| `python main.py` | 開 HTTP `8080`，**沒有排程**。兩支實例不能並存（埠被佔時拒絕啟動，不會殺掉佔用行程）。 |
| `GET /search?keyword=…` → `InventoryHTTPRequestHandler.run_crawler` | 活的蝦皮爬蟲：`crawler.ShopeeCrawler.run` 先 `get_all_products_info()` 拉賣家中心商品列，再 `get_monthly_sales(keyword)` 用數據中心「搜尋商品」框。關鍵字「隔日到貨」是店內品名慣例，**不是**獨立 API。空關鍵字仍會爬。寫入 gitignore 的 `shopee_products.json`。 |
| 首頁 `#searchButton` | 同上，即時爬蟲。驗證技能列為禁點。 |
| Grok Bot routines | 庫存／低水位提醒走 Grok Bot，**不是** Telegram、也**不是**應用內推播。搜「隔日到貨」仍用首頁／`GET /search` 活爬。 |
| `scripts/run_watchlist_restock.py` | 若 8080 沒人就啟動 `main.py`，等 `GET /api/home/bootstrap` 有商品，開瀏覽器給人看。**預設不加車**。沒有 `--i-approve-watchlist-restock` 時只印任務 1 應補摘要＋「尚未加車」（舊的 `--restock --yes` 不能單獨 POST）。 |

**缺口：** 沒有 cron／timer；首頁不會自動載入觀察清單；「隔日到貨」搜尋掃的是賣家中心關鍵字命中，與磁碟 watchlist 158 筆不必相同；`shopee_products.json` 過期時，後續水位全錯。

### 步驟 2 — 低水位提醒，並問要不要補

**期望：** 系統主動提醒，並等一句「要／不要」。

**現況（半成品）：**

- 首頁儀表板：`script.js` `updateDashboardUI`。危急 = 有銷量型號裡 **>30%** 水位 &lt; 1.5 個月；需注意 = &gt;10%。`#restockModelsCount` 顯示需補型號數。這是**畫面上的狀態**，不是推播，也沒有「要不要補」對話。
- Grok Bot routines：低水位提醒改走 Grok Bot（非 Telegram）。應用內沒有推播，也沒有「回覆是就開 1688」的指令。
- CLI 確認：`run_watchlist_restock.confirm_restock` — 路 A 須 `--i-approve-watchlist-restock` 才會 `POST /api/alibaba-restock/batches`。`--yes` **只**跳過「按 Enter」，不能單獨核准；沒有核准旗標時印任務 1 摘要並提示尚未加車。這是操作者已坐在機器前的閘門，不是遠端提醒，也**不是** `reverse_audit mutate`。
- 首頁整頁補貨：`#openBatchRestockButton` → `POST /api/alibaba-restock/batches/preview` 再手動確認。`restock_batch.build_preview` **不會重算數量**，只凍結畫面上的清單。

**缺口：** 沒有「低水位 → 推播 → 等是／否」狀態機。提醒走 Grok Bot routines（非 Telegram、非應用內頻道）。儀表板月數與店規 3／4 可能不一致，提醒集合會和 `reverse_audit` 應補集合不同。

### 步驟 3 — 說要之後，整份觀察清單自動 1688 補貨

**期望：** 一句「要」就對 **certain 且需補** 的 watchlist 加車（或改量），mapping 不完整的列要擋下來而不是亂猜。

**現況：兩條平行、沒接在一起的路。**

**路 A — 正向加車（首頁／launcher）**

1. `run_watchlist_restock.build_list_from_files` 或畫面上的 `currentRestockProducts`。
2. 可補條件（launcher）：`1688_mapping_status=="approved"` + http URL + 非停售 `1688_sku_name` + 手機殼雙規格要有第二規格。缺的進 `blockerCount`，**不加車**。
3. `POST /api/alibaba-restock/batches` → `restock_batch` 依商品序呼叫 `alibaba_restocker`。
4. 暫停：採購車約 195／200（`CART_SAFE_LIMIT`）、`paused_attention`（mismatch／uncertain／unavailable／failed）、驗證碼／登入。
5. 加車後用 **前後車列增量** 確認（`alibaba_restocker.apply_cart_verification_to_buckets`）。車讀不到或截斷 → 維持 unverified，toast 不當成功。

**路 B — 反向查核 mutate（CLI）**

1. `python -m reverse_audit refresh --date YYYYMMDD`：CDP 凍車＋待付款／待發貨／待收貨，再離線 dry-run。
2. 人工看 `補貨比對結果.csv`。
3. `mutate` 至少一個旗標：`--i-approve-mutate`（只加 `missing_to_add.csv`）、`--i-approve-set-qty`（不足上補＋超量下砍到 expected）、`--i-approve-remove`（只刪 `removable=true`）。旗標互不隱含。
4. 1688 Open Platform **不能**下單：`alibaba_client.AlibabaApiClient.create_pending_order` 未接入；`can_create_order` 永遠 false。閉環停在**採購車**，不是付款。

**缺口：** 「儀表板說要」或 Grok Bot 提醒**不會**自動啟動路 A 或路 B。路 A 是累加式加車＋增量驗證；路 B 是四池對帳＋絕對設量。兩邊的 certain 定義也不完全一樣（見 §5）。沒有一支指令把「提醒 → 核准 → 加車 → 再對帳」串成一條。

### 步驟 4 — 補完後核對應補 vs 實際車

**期望：** 同一輪應補集合，對上實際車（與在途），列出缺／少／多／多出來。

**現況（半成品，要人手再跑）：**

| 工具 | 做得到 | 做不到 |
|---|---|---|
| `alibaba_restocker` 增量驗證 | 這次送出的 SKU，車內增量 ≥ 預期才算確認 | 不管整車該有什麼；不管訂單三池；不管非預期列 |
| `restock_batch` 報告 | 加車前凍 live cart；`completed`／`completed_with_gaps` 後預設 CDP 重抓 after 再 `reverse_audit` **dry-run only**；`report.html` 掛 delta、`dry_run_summary.json` 與 `補貨比對結果.csv` | 不 mutate；沒 CDP 則 sources-only 並註明不假裝 live；`--sources-only` 給 CI |
| `python -m reverse_audit dry-run` | 應補 vs 車＋三池；主檔 `補貨比對結果.csv` | 路 B mutate 仍須人工旗標；批次路徑不會代跑 mutate |
| `scripts/reconcile_cart.py` | 離線、書包截止、絕對設量契約 | 不連瀏覽器；與 `restock_batches/` **分開**；文件寫明不可用 `init` 重跑舊人工日誌 |

**缺口：** 超量在 dry-run **只列不改**；要改量必須另下 `--i-approve-set-qty`。增量驗證過關，整車仍可能短少或有非預期列。#88 後批次預設會在加車前／後做 live freeze 並掛 dry-run；CI／離線才加 `--sources-only`。launcher 的 `--refreeze` 是文件化的相容旗標（no-op），未加也不會停用預設 live freeze。

## 4. 已能用／半成品／缺

### 已能用（main，人工操作）

- 賣家中心爬庫存＋月銷：`crawler.py`、`GET /search`（要 cookies）。提醒走 Grok Bot routines，不再用 Telegram `/搜尋`。
- 店規水位（CLI／反向）：`restock_rules.calculated_restock_details`（含歷史銷量保護、近十、條件最低 5）。
- 觀察清單 ∩ 排除：`watchlists/personal_watchlist.json`（158）+ exclusions（66）+ 品名含「襪」。
- 正向整頁加車（路 A）：`scripts/run_watchlist_restock.py --i-approve-watchlist-restock`、首頁預覽、`restock_batch` 可續跑。`--yes` 只跳過 Enter；沒有核准旗標不會 POST。不是 `reverse_audit mutate`。
- 加車增量驗證與車滿暫停：`alibaba_restocker.restock_count_check`、`VERIFY_CART_COUNTS=True`。
- 反向四池對帳：`python -m reverse_audit refresh` → 整合表＋機器 CSV。
- 批次終態自動 dry-run（任務 3＋before/after）：加車前凍 live cart；`completed`／`completed_with_gaps` 後預設重抓 after 再 `run_dry_run`。`PAUSED` 時寫「車內不足，不要加車，先看 shortfall」。沒 CDP 不開 Chrome、不假裝 live。不 mutate。
- 核准後改車：加車／設量／刪除，缺旗標立即拒絕。
- 不猜 URL／skuId：uncertain／skip 不進 mutate 加車。

### 半成品

- 首頁儀表板低水位（1.5 月危急）— 要先搜尋或匯入，月數用下拉，不是 3／4 店規。
- Grok Bot 提醒 — 不經 Telegram、不經應用內推播；沒有「要就補」閘門。
- launcher 的 Enter／`--yes` — `--yes` 只跳過本機「按 Enter」；加車仍須 `--i-approve-watchlist-restock`。不是遠端提醒，也不等於 mutate 核准。
- 路 A 增量驗證 vs 路 B 四池對帳 — 兩套證據；任務 3 在批次終態把路 B **dry-run** 掛回同一份 `report.html`，仍不自動 mutate。
- `cart-reconciliation` 書包截止 vs `reverse_audit`「不理正向書包 cutoff」— 範圍契約不同，不能混報「已核完全車」。
- mapping 阻擋：launcher 把未核准列算 blocker；`SkuMappingService._scope_models` 沒有 URL 的型號直接 `continue`，不會進掃描。

### 缺（對最終閉環）

- 任何週期排程（誰在哪台機器、多久跑一次 `main.py`／爬蟲）。
- 低水位**推播**＋「要／不要」狀態（Grok Bot routines；非 Telegram、非應用內頻道）。
- 一句「要」就啟動 **watchlist 全集** 補貨（且只補 certain）。
- 首頁自動載入 repo watchlist（現在故意不 bootstrap）。
- 首頁／爬蟲與 `restock_rules` 月數對齊。
- 1688 正式下單／付款（Open Platform 未接入；本閉環也不應做到付款）。

## 5. 風險（對這份 repo）

### 錯 mapping → 加錯車

- 路 A 只要求 `approved` + URL + 規格名（手機殼要第二規格）。`1688_sku_id` **可空**。
- 路 B certain = `approved` + http URL +（`skuId` **或** 可用 name/spec）。缺 skuId 時，若車內 name/spec **唯一**對上，用 live `skuId` 對帳，**不回寫** golden。
- name/spec 歧義 → fail-closed，不進 mutate／刪除。
- 錯的 `approved` 列會被當成真相加車。Know-how 綠燈**不是**寫入授權（`auto_approve.enabled=false`）。
- #76 後建構 `SkuMappingService` 預設不執行 Golden repair；會改寫 Golden 的舊核准修復必須明確執行 `python -m sku_mapping_service repair`，或以 `SKU_MAPPING_REPAIR_GOLDEN=1` 啟動服務。驗證應實際呼叫唯讀 GET（如 `/api/sku-mapping/summary`、`/api/sku-mapping/queue`），並比對前後 `golden_table.json` SHA、確認證據中的 `goldenUnchanged=true`。

### 驗證碼／登入牆

- 蝦皮：cookies 失效 → 爬蟲退出碼 77。
- 1688 掃描：`EgoBrowser1688._classify_page` 把驗證碼／滑塊／「請先登入」標成 `waiting_for_login`，**不自動過**。
- 1688 加車：登入／滑塊時保留瀏覽器，`restock_batch` 進 `paused_attention`。
- 反向 freeze／mutate：要本機已登入 Chrome CDP（預設 9223／9227）。Cloud Agent／CI **不能**當操作者 Chrome。
- AI-4 暫停時 253 列有 **97** 筆卡驗證碼（見 Goal B）— 搜 offer 比開已知 URL 更容易撞 punish。

### 部分觀察清單

- 首頁範圍 = 瀏覽器 localStorage 匯入的清單 ∩ 這次搜尋／匯入結果，**不是**自動用 repo `personal_watchlist.json`。
- launcher 用磁碟 watchlist + 排除；關鍵字 `--keyword` 再切一刀（例如只補「吊飾」）。
- 反向查核再用 exclusions ∩ shopee ∩ golden。三邊交集不同，應補集合就不同。
- 襪子永久排除；排除檔 66 筆。誤把襪子當「沒掃到」會誤判缺貨。
- 搜「隔日到貨」會打到大量襪／充電線等；若沒套 watchlist，儀表板「需補貨」會遠大於可補集合。

### 數量對不上卻被「靜音」

- 路 A：增量 ≥ 預期就當確認。車裡本來就有貨、或同 SKU 多列，可能看起來成功，整車仍不對。
- 路 B：`0 < 車內 < 應補` → **PAUSE**，完整列入 `qty_shortfall.csv`，**不自動改量**。有 shortfall 時，只帶 `--i-approve-mutate` 會被拒（exit 2）。
- 超量：dry-run **只列** `qty_excess.csv`，不 PAUSE。沒人下 `--i-approve-set-qty` 就會一直多。
- 訂單三池同 `(offerId, skuId)` 視為已覆蓋，**不問在途量是否 ≥ 應補**。在途 1、應補 40 會顯示已覆蓋。
- 儀表板／爬蟲建議量仍可能用單一 `--inventory-month`／下拉月數，沒有 `round_calculated_restock_qty` 的 3／4 分流 — 和 launcher／reverse_audit 數字會差一截。
- `restock_batch` 的 `needs_reconciliation` 與 `scripts/reconcile_cart.py` **沒接**。

## 6. 分階段路線圖（每階段都要人工閘門）

**約束（全程）：** 不開 `auto_approve`；不發明 URL／skuId；不改 ROAS；不把隔離種子庫灌進 live `procurement.db`；mutate 維持獨立核准旗標；Golden 全表寫入仍遵 Goal B 暫停，直到庭安解暫停。

```text
階段 0  現況（人工拼）
        爬蟲或匯入 → 人看首頁；提醒走 Grok Bot routines（非 Telegram）
        → 人下 --i-approve-watchlist-restock 或按整頁補貨
        → 人另跑 reverse_audit refresh
        閘門：每一步都是人

階段 1  同一套應補集合（唯讀）     ← 下一輪工程從這裡開始
        唯一計算：restock_rules + build_expected
        產出：需補／blocker／uncertain／skip
        閘門：只出報告，不爬、不加車、不 mutate

階段 2  提醒，但不自動補
        定時（操作者機器）跑階段 1；摘要提醒走 Grok Bot routines（非 Telegram、非應用內推播）
        閘門：沒有「是」就不呼叫 restock 或 mutate

階段 3  明示核准才補（接上路 A 或路 B，不要兩套同時改車）
        建議先路 A 加車（現有批次＋增量驗證）
        旗標建議：--i-approve-watchlist-restock（新，互不隱含）
        閘門：blocker／缺 URL 的列跳過並列出，不猜

階段 4  補完強制對帳（接路 B dry-run）
        批次終態 → 自動 refresh（要 CDP）或要求先 freeze
        把 補貨比對結果.csv 掛進同一 run
        閘門：自動 mutate=禁止；shortfall 維持 PAUSE

階段 5  同一核准面才能改量／刪
        人看整合表後，才下 set-qty／remove／add
        閘門：現有三旗標；預設不刪
```

階段 3 起若 Golden 列是錯的，閉環會穩定加錯車 — 這是 Goal B 的上限，不是本路線能「AI 修掉」的。

## 7. 接下來 3 個工程任務（適合 cloud-agent PR）

都不改 golden、不開 auto_approve、不發明 sku／URL、不碰 live 採購車（測試用 fixture）。政策數字沿用 `restock_rules` 與 `build_expected`，不另定水位。

### 任務 1 — 唯讀「觀察清單應補摘要」CLI

**做：** 新模組（建議 `python -m restock_loop scan --out reports/restock_loop_YYYYMMDD/`）重用 `reverse_audit.dry_run.build_expected` + `home_bootstrap` 排除，讀現有 `shopee_products.json`／watchlist／golden。寫 JSON＋中文摘要：certain 需補、uncertain（缺欄）、skip、blocker（對齊 launcher 的 approved／URL／規格名規則）、與 launcher 可加車列的差集。

**不做：** 不呼叫 `crawler.py`、不開 1688、不啟動 `restock_batch`。提醒不經 Telegram（改走 Grok Bot routines）。

**驗收：** unittest 用暫存 JSON（可仿 `tests/fixtures/reverse_audit/sources/`）；golden SHA 不變；`--help` 寫明「只報告」。

**為何先做：** 把步驟 1–2 的「掃誰、算多少」收斂成一個數字來源，才不會首頁 4 個月、反向 3／4 各算各的。

### 任務 2 — 明示核准才啟動現有整頁補貨

**做：** 在 `run_watchlist_restock.py`（或任務 1 的 CLI）加 `--i-approve-watchlist-restock`。沒這旗標：只印任務 1 摘要＋現有「尚未加車」提示，exit 0。有旗標：才走現有 `build_visible_style_products` → `POST /api/alibaba-restock/batches`。`--yes` **不**再單獨等於核准（避免舊習慣繞過）；`--yes` 只能跳過「按 Enter」，仍要新旗標。文件寫清：這是路 A，不是 `reverse_audit mutate`。

**現況：** 路 A 加車指令是 `python scripts/run_watchlist_restock.py --i-approve-watchlist-restock [--keyword 吊飾] [--yes]`。沒有該旗標（含舊習慣 `--restock --yes`）只跑 `python -m restock_loop scan` 摘要並提示尚未加車，**不會** POST `/api/alibaba-restock/batches`。這不是 `python -m reverse_audit mutate --i-approve-mutate`。

**不做：** 不新做應用內通知頻道（提醒走 Grok Bot routines，非 Telegram）；不自動 `--i-approve-mutate`；不略過 blocker。

**驗收：** 延伸 `tests/test_run_watchlist_restock.py`：沒旗標不 POST；有旗標才組 payload。可用 mock HTTP。

**為何第二：** 對應「說要才補」，但不發明新加車引擎。

### 任務 3 — 批次終態後自動跑反向 dry-run（仍不 mutate）

**狀態：** 已實作（本任務）。

**做：** `restock_batch` 在核准加車前凍 live cart；`finalize_status` 進入 `completed`／`completed_with_gaps` 後預設 CDP 重抓 after 再 `run_dry_run`。`report.html` 列出本次 before/after delta、`dry_run_summary.json` 與 `補貨比對結果.csv`。`status=PAUSED` 時批次訊息明確寫「車內不足，不要加車，先看 shortfall」。CI 用 `--sources-only`。

**不做：** 不呼叫 `run_mutate_actions`；不自動 set-qty／remove；不在 CI 開 Chrome。

**驗收：** `tests/test_restock_batch.py` + 現有 `tests/test_reverse_audit.py` fixture：終態後出現 dry-run 產物；mutate 旗標未傳入。

**為何第三：** 對應步驟 4，且不碰「要不要改車」的產品決策。

---

**刻意不做（本文件／本 PR）：** 排程器實作、應用內通知頻道／自動回覆補貨（提醒走 Grok Bot routines，非 Telegram）、合併路 A／路 B 成單一 mutate、開 `auto_approve`、把 PRs #41–#46 合進 main。
