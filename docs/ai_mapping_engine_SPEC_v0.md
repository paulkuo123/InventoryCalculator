# 蝦皮 × 1688 AI Mapping Engine SPEC v0

**狀態：** 唯讀盤點＋規格。本文件**不**實作引擎、**不**改 schema、**不**寫資料。  
**日期：** 2026-09-20  
**基準：** `main` @ `3c36203`（本機核對與 `origin/main` 相同）  
**產品頁：** Notion 已有（協調者持有連結；本 SPEC 對齊週五最終目標 B）  
**前一版脈絡：** Know-how Engine v1（TASK 1–9 已合 main：#51＋#60）；交接摘要見 uploads 的 `sku-mapping-knowhow-engine-SPEC-v1`；完整 Know-how 規格預期在 `/workspace/_handoff/sku-mapping-knowhow-engine-SPEC-v1-FULL-20260912.md`（本 VM 未掛上）。

> 本檔同時是 PR 正文來源。協調者應另存一份到  
> `/workspace/_handoff/ai-mapping-engine-SPEC-v0-20260920.md`  
> （內容須與本檔一致；`_handoff/` 不進 git）。

---

## 0. 一句話

Mapping Engine **不是**「用大模型比誰長得像」。第一優先是：**人已經核過的對應**＋**Know-how 規則／同義詞／負例**；模型只當輔助排序與說明。高信心以後才談自動過關；難的留給人；人核過的結果要寫回知識庫，讓下一輪更準。

Golden 全表工程（#41–#46）**仍然暫停**。本階段只准文件與隔離匯入設計，不准碰生產資料。

---

## 1. 目標與非目標

### 1.1 要對齊的最終狀態（目標 B，長期）

1. 歷史人工核准對應是最高優先的真相。
2. Mapping Know-how（硬／軟規則、顏色等同詞、負例原因、歷史正例）先於純語意相似度。
3. 高信心列日後可自動過關；難列進人工佇列。
4. 人工結果回寫知識庫（正例、負例、原因），再餵回 `SkuMappingService`。
5. 第 1 層（蝦皮型號 → 1688 offer）與第 2 層（已知 offer → 1688 SKU）分開；第 1 層先讀隔離種子，不要先做站內搜尋。

### 1.2 本階段（v0）只做什麼

| 做 | 不做 |
|---|---|
| 盤點十種知識庫物件現在落在哪 | 改 `golden_table.json`（含 backup） |
| 寫 Canonical Product／Mapping Record／正負例規格 | 把隔離種子灌進 live `procurement.db` |
| 訂唯讀匯入路徑（Golden＋隔離 KB → 隔離 KB） | `auto_approve.enabled = true` |
| 講清楚怎麼接到既有 Know-how／`sku_mapping_service` | 合併 #41–#46、重啟全表補完 |
| 分階段：先 schema＋唯讀匯入，再候選／AI／規則／信心 | 購物車、付款、入庫 SoT、大重構 |

### 1.3 與既有文件的關係

本 SPEC **不取代**下列文件，只把它們接到「Mapping Engine 知識庫」這一條線：

| 文件 | 角色 |
|---|---|
| [`sku_mapping_knowhow_engine.md`](sku_mapping_knowhow_engine.md) | 第 2 層 Know-how 已落地總覽；**不是**第二套引擎 |
| [`mapping_eval.md`](mapping_eval.md) | 離線評估 CLI；不寫 Golden、不 auto-approve |
| [`1688_purchase_history_kb.md`](1688_purchase_history_kb.md) | 歷史採購 `kb_*` schema；Golden 最高優先、只抄不回寫 |
| [`offer_discovery_spike.md`](offer_discovery_spike.md) | 第 1 層設計 only；先讀隔離種子 |
| [`golden_ai_automation_review.md`](golden_ai_automation_review.md) | 目標 B 審查；#41–#46 暫停約束 |

Know-how Engine **已在 main**。Mapping Engine v0 是在它上面加「可匯入、可回寫的知識庫契約」，不是另做 matcher。

---

## 2. 本機盤點（2026-09-20，唯讀）

盤點方式：讀 tracked 檔、讀 schema 原始碼、對 `golden_table.json` 做 SHA-256 與列數統計。**沒有**對 live DB 做寫入，也**沒有**呼叫 `SkuMappingService` 建構（避免誤觸 repair／backfill）。

### 2.1 `golden_table.json`（人工核准真相；schema 不變）

| 項目 | 本機數字 |
|---|---|
| SHA-256 | `8a95064fbe116f9e6ead24837ced1511ce34a9f3ac69358d9c2e0fb585ab3d2e`（與暫停時文件一致） |
| 商品 | 355 |
| 型號列 | 5918 |
| `1688_mapping_status=approved` | 3800 |
| `missing` | 1832 |
| `discontinued` | 236 |
| `pending` | 48 |
| `stale` | 2 |
| 有 URL 或 `1688_offer_id` | 4973 |
| 有 `1688_sku_id` | 1330 |

商品層欄位：`商品名稱`、`商品圖片網址`、`型號[]`。  
型號層對應欄位：`型號名稱`、`規格ID`、`型號圖片網址`、`阿里巴巴商品名稱`、`阿里巴巴商品URL`、`1688_offer_id`、`1688_sku_id`、`1688_sku_name`、`1688_sku_second_name`、`1688_spec_text`、`1688_mapping_status`、`1688_mapping_source`、`1688_verified_at`、`1688_offer_fingerprint`，以及採購輔助欄（`1688_last_price_cny`、`1688_min_order_qty`、`1688_package_multiple`、`1688_dimension_count`、`1688_mapping_fingerprint`）。

**語意：** 這是補貨／入庫現在吃的真相檔。人工核准列＝正例 Ground Truth。本階段**不得**改檔、不得改 schema。

### 2.2 `mapping_knowledge_pack/`（Know-how 檔案包）

| 檔 | 現況 |
|---|---|
| `config.json` | `auto_approve.enabled=false`；`score_weights`＝feature 0.30／historical 0.30／rule 0.20／llm 0.20；**沒有** `semantic` |
| `aliases.json` | 顏色等同詞，`source=seed_from_COLOR_SYNONYMS`；`python -m mapping_knowledge seed-aliases` 匯出，不要手抄 |
| `rules.json` | RULE-0001…0004（尺寸／手機代數／英數代碼／括號噪音）；`impl` 指向既有函式，不是 DSL |
| `categories.json` | `phone_case`／`watch`／`socks`／`charm` 關鍵字 |

`mapping_knowledge.knowledge_version()`＝該目錄檔案的穩定 SHA-256。缺檔回退內建預設，不中斷審核。

### 2.3 `procurement.db` 的 `sku_mapping_*`（執行期工作表）

`SkuMappingService._init_db()` 會建（或加欄）這些表。它們是**審核工作狀態**，不是 Master KB：

| 表 | 角色 |
|---|---|
| `sku_mapping_runs` | 掃描／健康檢查 job |
| `alibaba_url_health_checks` | URL 探活紀錄 |
| `alibaba_offer_snapshots` | 已知 offer 的 SKU 快照（第 2 層分母） |
| `sku_mapping_suggestions` | 每型號一列建議：status、decision、`confidence`、`final_score`、`score_breakdown_json`、`knowledge_version`、`evidence_json`、`review_tier` |
| `sku_mapping_candidates` | 候選 SKU：`parts_json`、`deterministic_score`、`evidence_json` |
| `sku_mapping_reviews` | 人工動作稽核（approve／replace／no_match／reject_candidate／discontinued…） |
| `mapping_rule_hits` | 規則命中（含 `rule_type='negative'`） |
| `mapping_negative_examples` | 負例 Ground Truth（原因代碼 SPEC 5.3） |

**本 VM 沒有** `procurement.db`（已 gitignore）。操作者本機才有 live 檔。依既有文件與程式契約：**live 檔目前不應有 `kb_*` 表**——`kb_*` 只在 `PurchaseHistoryStore.init_db()` 對指定 `--db-path` 建立；`PHASE3_HISTORY_IMPORT_ENABLED = False`，對 live 路徑直接拒絕寫入。`historical_support()` 若同檔有 `kb_mappings`＋`kb_skus` 會讀，**缺表就忽略**。

### 2.4 隔離種子 KB（本 VM 看不見）

預期路徑（不進 git，比照 cookies／`.env.local`）：

```text
/workspace/_handoff/kb_excel_success_isolated_20260912.db
```

本 Cloud Agent 工作區**沒有** `/workspace/_handoff/`，也找不到該 `.db`。數字與 schema 依已合 main 的文件（[`offer_discovery_spike.md`](offer_discovery_spike.md)、[`ads_cost_margin_design.md`](ads_cost_margin_design.md)、[`1688_purchase_history_kb.md`](1688_purchase_history_kb.md)）轉述，**實作前必須用唯讀 `PRAGMA`／`SELECT COUNT` 核對，不可把轉述數字當生產常數**：

| 預期 | 來源文件 |
|---|---|
| 約 **3309** 筆交易成功訂單 | offer discovery／廣告成本設計 |
| 約 **3.9 萬** 行明細，`price_cny` 已填 | 廣告成本設計 |
| 表＝`purchase_history_store.KB_TABLES` | `kb_schema_meta`、`kb_products`、`kb_skus`、`kb_orders`、`kb_order_items`、`kb_purchase_history`、`kb_mappings`、`kb_crawl_state`、`kb_errors` |

小型 fixture（可進 git、可測匯入契約）：`tests/fixtures/historical_kb/`。CI **不要**提交真實 `.db`。

### 2.5 Know-how 程式接合（已在 main）

```text
golden_table.json（核准列）
        │ 唯讀
        ▼
SkuMappingService.generate_candidates()
  硬規則剔除 → 完整匹配 → 嚴格字面 → historical_support（Golden ＋ 可選 kb_mappings）
  同型號負例剔除
  final_score = Σ weight × {feature, historical, rule, llm}   ← 只排序
        │
        ▼
classify_review_tier()  綠／黃／紅
        │ 可選 AI judge（PROMPT_VERSION=2026-09-v2）
        ▼
_save_suggestion() → sku_mapping_* + evidence_json
        │
        ▼
人工核准 → _write_approved_mapping → golden_table.json
```

`GET /api/sku-mapping/summary`、`/queue` 是審核 UI 讀徑。`explain`、`negative-examples` API 存在，UI 尚未呼叫。

---

## 3. 十種知識庫物件：已有／缺口

庭安要的十種（產品／正例對應／負例對應／規則／別名／特徵／人工審核／AI 結果／信心／原因）對照現況。  
「已有」＝資料或 schema 已在 main；「缺口」＝Mapping Engine 要當第一類公民時還缺的契約。

| # | 物件 | 已有（檔／表） | 缺口 |
|---|---|---|---|
| 1 | **Canonical Product**（蝦皮商品＋型號；1688 offer＋SKU） | 蝦皮側：`golden_table.json` 商品 key＋`型號[]`。1688 側：隔離 KB 的 `kb_products`／`kb_skus`；執行期 `alibaba_offer_snapshots` | **沒有**獨立、可匯入的「正規商品」表。`kb_products` 以 `offer_id` 為 PK，不是蝦皮 `product_id`。兩側身分沒有統一 ID |
| 2 | **正例 Mapping Record** | Golden `approved` 列（3800）；`PurchaseHistoryStore.copy_golden_approved_snapshots()` 可抄到 `kb_mappings`（需 `offer_id`＋`sku_id`）；`historical_support()` 讀核准列 | `kb_mappings` **未**在 live DB。抄寫條件偏嚴（沒有 `1688_sku_id` 的核准列抄不進去；本機約 3800 vs 1330）。沒有「對應紀錄」版本／來源優先序表 |
| 3 | **負例 Mapping / Ground Truth** | `mapping_negative_examples`＋`NEGATIVE_REASON_CODES`；origin＝`no_match`／`chose_other_candidate`／`explicit_reject` | 負例只活在執行期 DB，**沒有**從 Golden／隔離 KB 匯入的路徑。本 VM 無法量 live 列數 |
| 4 | **規則 Rules** | `mapping_knowledge_pack/rules.json`；`mapping_rule_hits` | 規則不是 KB 列（不能從歷史審核長出新規則）。沒有「人核過的例外」回寫規則包的契約 |
| 5 | **別名 Alias** | `aliases.json`（目前幾乎只有 `field=color`） | 沒有尺寸／包裝／型號前綴別名。沒有從核准對（`型號名稱`↔`1688_sku_name`）自動提案別名的路徑 |
| 6 | **特徵 Features** | 執行期：`source_parts`、`parts_json`、`deterministic_score`、`complete`／`exact`／`strict_exact`；複合分的 `feature` 分量＝`deterministic_score/100` | **沒有**獨立特徵庫。顏色／尺寸／手機代數是函式內啟發式，不是可匯入列 |
| 7 | **人工審核 Human review** | `sku_mapping_reviews`；Golden 的 `1688_mapping_source`（`manual`／`ai_reviewed`）＋`1688_verified_at`；`/sku-mapping.html` | 審核結果沒有正規化回 KB（只有寫 Golden）。`explain` UI 未接。暫停期間核准路徑也不准重開全表寫入 |
| 8 | **AI 結果** | `suggestions.evidence_json.ai`（provider／model／effort／decision／selected_*）；`PROMPT_VERSION=2026-09-v2`；payload 已含 historical／negative／applied_rules | AI 列不是 KB。沒有「模型建議 vs 人最終選擇」的長期對照表（只有當次 `chose_other_candidate` 負例） |
| 9 | **信心 Confidence** | `suggestions.confidence`（AI）；`final_score`＋`score_breakdown_json`；綠燈門檻 0.95／0.90 | 信心是建議欄，不是 KB 物件。`final_score` **不**決定綠色。沒有「KB 信心」與「建議信心」的分層 |
| 10 | **原因 Reason** | 負例 `reason_code`／`reason_text`；`review_reason`；explain 的 `why[]`（rule／alias／historical／negative／feature／llm） | 原因代碼只服務負例。正例沒有「為何核准」結構化原因。`OTHER` 必須填文字——這點要保留 |

**結論：** 十種物件在系統裡**幾乎都有碎片**，但分散在 JSON 檔、知識包、執行期 SQLite、以及本機才有的隔離種子。Mapping Engine 第一步要做的是：**在隔離 DB 把 1–3（商品／正例／負例）收成可讀的 KB 表，其餘先唯讀參照既有檔／欄，不准發明第二套 matcher。**

---

## 4. Canonical Product（正規商品）

### 4.1 為什麼要獨立出來

現在「商品」混了三層：

1. **蝦皮販售單位：** `golden_table.json` 的 `product_id`（檔案 key）＋`規格ID`（型號）。
2. **1688 供應單位：** `offer_id`＋`sku_id`／`sku_key`（隔離 KB、快照、購物車）。
3. **執行期快照：** `alibaba_offer_snapshots`（同一 offer 會隨 fingerprint 變多列）。

Mapping Engine 要對的是「蝦皮型號 ↔ 1688 SKU」，兩邊都要有穩定身分，不能靠品名模糊合併。

### 4.2 身分規則（v0 契約，尚未建表）

**蝦皮側**

| 欄 | 規則 |
|---|---|
| `shopee_product_id` | Golden 外層 key，正規化後不可空 |
| `shopee_model_id` | 優先 `規格ID`；沒有規格 ID 時，僅當該商品內 `型號名稱` 唯一才可用名稱當臨時 id（與 `product_catalog.find_model_by_identity` 一致） |
| `product_name`／`model_name` | 顯示與特徵切分用，**不是**主鍵 |
| `category_id` | 由 `detect_category(product_name)` 或 `categories.json` 推導；可空＝`other` |

**1688 側**

| 欄 | 規則 |
|---|---|
| `offer_id` | 從 URL 解析或既有 `1688_offer_id`；沒有 offer 就還在第 1 層，不進第 2 層 |
| `sku_key` | 有真實 `sku_id` 就用 `sku_id`；否則 `unresolved_id`（前綴約定見 `purchase_history_store`）。**禁止**用名稱／圖片把 unresolved 併成真實 id |
| `sku_name`／`second_name`／`spec_text` | 第 2 層比對用；名稱是耐久身分（Know-how 已如此） |
| `product_url` | 正規化後的 `detail.1688.com/offer/{id}.html` |

### 4.3 與現有表的對應（先不新建 production 表）

| 正規概念 | 現在讀哪裡 | v0 匯入後放哪（隔離 DB only） |
|---|---|---|
| 蝦皮商品 | Golden 外層 | 建議未來 `kb_canonical_products`（本階段**只設計**；實作另開任務） |
| 蝦皮型號 | Golden `型號[]` | 建議未來 `kb_canonical_models` |
| 1688 offer | `kb_products`、`alibaba_offer_snapshots`、Golden URL | 沿用 `kb_products`（隔離檔） |
| 1688 SKU | `kb_skus`、snapshot `skus_json` | 沿用 `kb_skus` |

v0 **不**要求立刻建 `kb_canonical_*`。第一步能做到的是：匯入清冊把「蝦皮型號列」與「1688 offer／SKU」對上既有欄位，並標來源。建新表屬於下一刀 schema PR，且必須走隔離 `--db-path`。

### 4.4 明確禁止

- 用語意相似度把兩個 `product_id` 合成一個 Canonical Product。
- 跨不同蝦皮 `product_id` 複製 offer（兄弟檔只限**同一商品**的其他型號）。
- 把 snapshot 的暫時 `sku` 列升成 Golden 真相。

---

## 5. Mapping Record（對應紀錄）

一筆 Mapping Record 的最小意義：

```text
(shopee_product_id, shopee_model_id)  ↔  (offer_id, sku_key 或 名稱組合)
來源、狀態、時間、是否可當 Ground Truth
```

### 5.1 現況三種紀錄（優先序固定）

| 優先 | 來源 | 狀態欄 | 可否當 Ground Truth | 本階段可寫？ |
|---|---|---|---|---|
| 1 | `golden_table.json` 型號列 | `1688_mapping_status` | **只有 `approved`** | **否** |
| 2 | `kb_mappings` | `source` ∈ {`golden_approved`,`inbound_exact`,`manual`}＋`mapping_status` 快照 | `golden_approved`／`inbound_exact` 可當歷史支持；`manual` 且 Golden 沒有對應 → 待人確認 | 只准寫**隔離** DB |
| 3 | `sku_mapping_suggestions` | `status`／`review_tier` | 否（建議不是真相） | 不在本階段範圍 |

衝突時：**Golden 已核准列贏。** KB 只能抄，不能改 Golden。這與 `docs/1688_purchase_history_kb.md` 完全一致。

### 5.2 建議欄位（文件契約；尚未 ALTER）

| 欄 | 說明 |
|---|---|
| `shopee_product_id`／`shopee_model_id` | 蝦皮身分 |
| `offer_id` | 1688 offer |
| `sku_id` | 可空；空不代表未核准（main 上有核准但無 sku_id 的列） |
| `sku_key` | 有 id 用 id；否則 unresolved，不可自動合併 |
| `sku_name`／`second_name`／`spec_text` | 名稱組合；Know-how 比對主鍵之一 |
| `polarity` | `positive`／`negative` |
| `status` | 對齊 Golden：`approved`／`missing`／`pending`／`discontinued`／`stale`… |
| `source` | `golden_approved`／`inbound_exact`／`manual`／`kb_seed`／`human_review` |
| `verified_at` | 人工核准時間；舊 `1688_verified_at` 單獨不算信任（#41 提案未合 main） |
| `confidence` | 見第 9 節；匯入列可空 |
| `reason_code`／`reason_text` | 負例必填代碼；`OTHER` 必填文字 |
| `copied_at` | KB 抄寫時間，不是核准時間 |

`copy_golden_approved_snapshots()` 今天**跳過**沒有 `1688_sku_id` 的核准列。這是已知缺口：那批列在 Golden 仍是真相，只是抄不進 `kb_mappings`。v0 匯入必須**另外保留「名稱組合正例」**，不得假裝它們不存在，也不得為了抄進 KB 去發明 sku_id。

### 5.3 正例怎麼進 judging（已有，保持）

`SkuMappingService.historical_support()`：

- **Same-offer：** 同一 offer 上**其他**已核准型號的 `model_name ↔ 1688_sku_name`。
- **Cross-offer：** `normalize_text(model_name)` 相同的過去核准 `1688_sku_name`。
- 當前列自己的答案不算（避免評估洩漏）。
- 可選讀同 DB `kb_mappings`（`source='golden_approved'`）。

v0 匯入的目的是讓隔離評估也能看到歷史正例，**不是**改這套公式。

---

## 6. Ground Truth 與 Negative

### 6.1 正例 Ground Truth

**唯一定義（本階段）：** Golden 型號列 `1688_mapping_status == "approved"`。

評估（`mapping_eval`）另外要求該 `1688_offer_id` 在 `alibaba_offer_snapshots` 有 `status='ok'`，真相是 `1688_sku_name`＋`1688_sku_second_name`（或以 `1688_sku_id` 對上候選）。沒有快照的核准列**仍是產品真相**，只是不進 scorable 分母。

### 6.2 負例 Ground Truth

**唯一定義：** `mapping_negative_examples` 的列。

| 代碼 | 中文 |
|---|---|
| `MODEL_MISMATCH` | 型號不符 |
| `SIZE_MISMATCH` | 尺寸不符 |
| `COLOR_MISMATCH` | 顏色不符 |
| `VERSION_MISMATCH` | 版本不符 |
| `PACKAGE_QTY_MISMATCH` | 包裝數量不符 |
| `LOOKALIKE_DIFFERENT` | 外觀相似但不同商品 |
| `DISCONTINUED` | 已停售／下架 |
| `OTHER` | 其他（必填說明） |

寫入 origin（已落地，不要另發明）：

- `no_match`：該建議全部候選
- `chose_other_candidate`：核准／取代但與 AI 建議不同
- `explicit_reject`：只否決單一候選

閘門：同一 `(product_id, model_id, offer_id, candidate_key)` 命中 → 剔除。同一 `(offer_id, candidate_key)` 但**不同型號**不剔除，只進 prompt。

### 6.3 不是 Ground Truth 的東西

| 資料 | 為什麼不是 |
|---|---|
| 綠燈／`final_score` 高 | 綠色是審核分級，不是寫檔授權 |
| AI `decision=match` | 沒有人核過就不能當真相 |
| 隔離種子「買過這個 offer」 | 只能當第 1 層建議；買過 ≠ 這個蝦皮型號該對這個 SKU |
| #41–#46 的候選 CSV／靜態審核 HTML | 暫停中的建議備料，**不是**核准 |
| `kb_mappings.source=manual` 且 Golden 無對應 | 待人確認 |

---

## 7. 匯入路徑（Golden ＋ 隔離 KB → KB）

本階段唯一允許的方向：**讀** Golden 與隔離種子，**寫**到另一個隔離 SQLite（或 dry-run 報告）。  
**禁止**反方向（KB → Golden、種子 → live `procurement.db`）。

```text
golden_table.json          ────唯讀抄寫────►  隔離 KB（--db-path，非 live）
   approved 列                              kb_mappings (source=golden_approved)
   名稱組合（可無 sku_id）                    另列／報告，不發明 sku_id

/workspace/_handoff/
  kb_excel_success_isolated_20260912.db
        │ 唯讀 attach（sqlite mode=ro 或 PurchaseHistoryStore(db_path=...)）
        ▼
   同一隔離工作檔（或第三個 temp）
        kb_orders / kb_order_items / kb_products / kb_skus / kb_purchase_history
        （複製或 ATTACH 查詢，不改種子檔本身）

live procurement.db        ────本階段完全不碰────
PHASE3_HISTORY_IMPORT_ENABLED  維持 False
```

### 7.1 路徑 A：Golden → 隔離 `kb_mappings`

既有函式：`PurchaseHistoryStore.copy_golden_approved_snapshots()`。

v0 實作時（**另開任務，本 PR 不做**）必須：

1. `--db-path` 指向新檔或明確隔離檔，預設 dry-run。
2. 統計三桶：已抄（有 offer＋sku_id）、名稱正例保留（核准但無 sku_id）、略過（非 approved）。
3. 不寫 `golden_table.json`、不寫 live `procurement.db`。
4. 冪等：同一 `(offer_id, sku_key, shopee_product_id, shopee_model_id, source)` upsert。

### 7.2 路徑 B：隔離種子 → 工作 KB（唯讀）

種子檔預期已有 `kb_*`。正確用法：

1. 唯讀打開，核對表名與列數（是否接近 3309／約 3.9 萬行）。
2. 第 1 層建議用 `kb_order_items.offer_id`、`kb_products`、`kb_purchase_history`。
3. `kb_mappings` 只採 `golden_approved`／`inbound_exact`；`manual` 單列待審。
4. **不要** `import` 進 live，不要開 `PHASE3_*`，不要 crawl。

既有閘門（保持）：

```text
python -m purchase_history_import dry-run --input <fixture> --allow-order-ids …
python -m purchase_history_import import … --db-path <隔離> --allow-order-ids … --i-approve-kb-import
python -m purchase_history_import crawl     # 永遠拒絕
```

對 live `procurement.db` 未開 Phase 3 一律拒絕。

### 7.3 路徑 C：執行期負例／審核 → KB（本階段只設計）

`mapping_negative_examples` 與 `sku_mapping_reviews` 日後應能匯出到隔離 KB，讓歷史否決在新環境還在。  
v0 **不實作**這條寫入。沒有 live DB 也無從匯出。文件先訂：匯出是複本，Source of Truth 仍是執行期表＋Golden。

### 7.4 匯入成功長什麼樣子（驗收，尚未做）

| 檢查 | 通過條件 |
|---|---|
| 種子檔 SHA／mtime | 匯入前後種子檔不變 |
| Golden SHA | 仍為 `8a95064f…585ab3d2e` |
| live `procurement.db` | 不存在或未開啟；若操作者本機有檔，mtime／`kb_*` 表數不變 |
| 隔離工作檔 | 可重跑、可刪；不進 git |
| `auto_approve.enabled` | 仍為 false |

---

## 8. 與 Know-how／`sku_mapping_service` 怎麼接

**原則：不發明第二套引擎。** 候選生成、綠黃紅、AI judge、評估 CLI 全部繼續走現有模組。

### 8.1 讀徑（v0 之後實作時）

```text
隔離 KB（正例／可選負例複本）
        │  只讀
        ▼
SkuMappingService.historical_support()     已支援同 DB kb_mappings
SkuMappingService._apply_negative_gate()   讀 mapping_negative_examples
mapping_knowledge_pack/*                   規則／別名／門檻
        │
        ▼
generate_candidates → classify_review_tier → 可選 AI → _save_suggestion
```

若 KB 在**另一個檔**（隔離種子），不要把表合併進 live。正確做法是評估／建議行程用 `--db-path` 指向「附有 kb_mappings 複本的隔離工作檔」，或唯讀 ATTACH。**Attach 進 live 也不行。**

### 8.2 分數與分級（不要改）

| 東西 | 現況契約 | Mapping Engine 可否改 |
|---|---|---|
| `final_score` | 只排序、排黃燈佇列 | 否（本階段） |
| 綠色條件 | 唯一完整匹配或高信心 AI；見 `classify_review_tier` | 否 |
| `score_weights` | 無 `semantic` | 不得加語意權重當主通道 |
| `auto_approve` | `enabled=false`；反事實可算 | 不得設 true、不得做 UI 開關 |
| `PROMPT_VERSION` | `2026-09-v2`：歷史人工核准優先於相似度；負例不得選 | 保持 |

### 8.3 第 1 層 vs 第 2 層

| 層 | 輸入 | 輸出 | 現況 |
|---|---|---|---|
| 1 | 蝦皮型號（常缺 URL） | 建議 `offer_id`／URL | 設計 only；`_scope_models()` 沒 URL 就 skip |
| 2 | 已知 offer 快照 | 綠／黃／紅 SKU 候選 | **已落地** |

隔離種子優先服務第 1 層（我們是否買過某個 offer）。Golden 兄弟檔是第 1 層來源 2。兩層 top offer 不同 → 衝突，進人工，**禁止自動消解**。

### 8.4 人工回寫（長期，本階段不開寫入）

理想閉環：

```text
人在 /sku-mapping.html 核准或否決
  → Golden（正例；現有 _write_approved_mapping）
  → mapping_negative_examples（負例；現有 _apply_decision）
  → 之後再抄到隔離 kb_mappings／kb 負例複本
  → 下一輪 historical_support／負例閘門變準
```

在庭安解暫停之前：連 Golden 寫入都不准當「引擎任務」重開。本 SPEC 只把回寫畫清楚，方便下一階段接。

---

## 9. 階段

### 階段 0 — 本文件（進行中）

- 唯讀盤點十種物件。
- 凍結禁令與路徑。
- 開 docs-only PR，**不合併也不碰資料**。

### 階段 1 — KB schema ＋ 唯讀匯入（下一刀，需另核准）

範圍小、可測、可刪：

1. 隔離 `--db-path` 工作檔（可用既有 `kb_*`，必要時**另開 PR** 加「名稱正例」存放，不加進 live）。
2. Dry-run：Golden approved 分桶（有 sku_id／僅名稱／非 approved）。
3. 唯讀核對種子 DB 表與列數。
4. Fixture 測試覆蓋 copy／拒絕 live／拒絕無 allowlist。
5. 報告寫 `data/` 或 `/tmp`（gitignore），不提交 `.db`。

**仍禁止：** 寫 Golden、seed→live、`auto_approve=true`、合併 #41–#46。

### 階段 2 — 候選／AI／規則／信心（更後面）

只有階段 1 的隔離 KB 能被 `historical_support`／評估穩定讀到之後才做：

1. 第 1 層建議：種子 offer → 兄弟檔 offer →（仍不做站內搜）。
2. 第 2 層沿用 Know-how；評估看 Top-1、Green Precision、FN、反事實 `n_would_pass`。
3. 信心分層：KB 來源可靠度 ≠ 建議 `final_score` ≠ 綠燈 ≠ 可自動寫。
4. 規則／別名仍走知識包；要用人工結果長新規則，另開任務。

### 階段 3 — 高信心自動（未排程）

與 Know-how 文件「為何仍然關閉」相同：真實 DB 上「會過關集合非空且精度 100%＋可稽核＋一鍵撤回」之前，**不開** `auto_approve.enabled`。官方 fixture 目前反事實是 **0/0**。

### 解暫停之後才談（不是 Mapping Engine v0）

見 [`golden_ai_automation_review.md`](golden_ai_automation_review.md) 階段 R→W→E→X。#41–#46 維持開著不當真相。AI-6 批次寫 Golden **不做**。

---

## 10. 本階段禁令（寫進 PR，避免「順便做」）

下列在庭安另下一句核准前，文件 PR 也不准建議當本任務實作：

1. **不得寫** `golden_table.json` 或任何 `golden_table.json.backup*`。
2. **不得**把隔離種子／fixture **灌進** live `procurement.db`（seed→live）。
3. **不得** `auto_approve.enabled=true`，不得加 UI toggle。
4. **不得**合併或 cherry-pick #41、#42、#43、#44、#45、#46 當真相。
5. **不得**重啟 Golden 全表補完、AI-4 站內搜、AI-2 全表探活。
6. **不得**開 `PHASE3_HISTORY_IMPORT_ENABLED`、對 1688 打 list／search、新寫搜尋爬蟲。
7. **不得**購物車／付款／把歷史單當入庫 SoT。
8. **不得**為了「收成 KB」做大重構或第二套 matcher。
9. **不得**用名稱／圖片相似度合併 `unresolved_id`。
10. **不得**讓綠燈或高 `final_score` 等於可採購／certain。

本 PR 允許的唯一 repo 變更：新增本 SPEC 檔。

---

## 11. 信心、AI、原因（文件定義，供階段 2 使用）

為避免之後把三個數字混在一起：

| 名稱 | 現在在哪 | 意思 | 可否寫 Golden |
|---|---|---|---|
| AI `confidence` | `suggestions.confidence`、`evidence_json.ai` | 模型對「選中這個候選」的把握 | 否 |
| `final_score` | 建議／候選 | 四分量加權，只排序 | 否 |
| 綠／黃／紅 | `review_tier` | 給人看的批次分級 | 綠仍要人核 |
| KB 來源可靠度（未來） | 尚未建 | 例如 `golden_approved`＞`inbound_exact`＞種子買過＞兄弟檔＞搜尋 | 否 |

原因（reason）分兩類：

- **負例原因：** 已有代碼表，保留。
- **正例原因（未來）：** 建議最少紀錄 `source`＋`reviewer`＋`verified_at`＋可選 `why[]`；不要強迫人填長文才能核准。

---

## 12. 交接與複本路徑

| 位置 | 誰放 | 進 git？ |
|---|---|---|
| `docs/ai_mapping_engine_SPEC_v0.md` | 本 PR | 是 |
| `/workspace/_handoff/ai-mapping-engine-SPEC-v0-20260920.md` | **協調者**把本檔原文貼上（本 VM 無 `_handoff/`） | 否 |
| `/workspace/_handoff/kb_excel_success_isolated_20260912.db` | 營運機既有種子；本 VM 未掛 | 否 |
| `/workspace/_handoff/sku-mapping-knowhow-engine-SPEC-v1-FULL-20260912.md` | 舊 Know-how 全文（若還在營運機） | 否 |
| Notion 產品頁 | 協調者已有 | — |

協調者放置指令（在有 `_handoff/` 的箱子上）：

```bash
mkdir -p /workspace/_handoff
cp docs/ai_mapping_engine_SPEC_v0.md \
  /workspace/_handoff/ai-mapping-engine-SPEC-v0-20260920.md
# 或：把本 PR 正文／本檔全文寫入該路徑。內容必須一致。
```

---

## 13. 短結：已有／缺口

| 已有（可直接當地基） | 缺口（下一刀才准做，且隔離） |
|---|---|
| Golden 355／5918；核准 3800 是正例 SoT；SHA 已凍結 | 本環境無 live DB、無隔離種子檔，無法核對 `kb_*` 實列 |
| Know-how 包：規則 4 條、顏色別名、類別、門檻；auto_approve 關 | 沒有正規 Canonical Product 表；蝦皮／1688 身分未統一 |
| 第 2 層 matcher＋綠黃紅＋explain＋eval＋反事實 | 第 1 層 offer 建議未實作 |
| `kb_*` schema＋gated import stub＋fixture | live 無 `kb_*`；Golden→`kb_mappings` 會丢掉無 sku_id 的核准列 |
| 負例表＋原因代碼＋歷史正例通道 | 負例／審核沒有匯入隔離 KB 的現成 CLI |
| 複合分四分量（feature／historical／rule／llm） | 特徵不是獨立 KB；信心三層未產品化 |
| 暫停文件把 #41–#46 與寫入閘門講清楚 | 全表補完仍暫停；不得當本任務順手做 |

**v0 一句話收尾：** 知識已經散落在 Golden、知識包、執行期表、以及營運機種子庫；Mapping Engine 第一步是**收成可讀的隔離 KB，而且只讀**。人核過的對應與 Know-how 優先，模型排後面。在隔離匯入通過、且庭安解暫停之前，Golden、live DB、auto_approve、#41–#46 全部維持不動。
