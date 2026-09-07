# Golden Table Mapping Phase 0 — 現有工作台複用盤點（2026-09-07）

分類：**reuse as-is**／**extend**／**replace**／**unused**。Phase 0 不實作 Phase 1 UI。

## 總結

正式 mapping 工作台已經存在，核心是 `/sku-mapping.html` + `sku_mapping_service.py`。缺口不是「從零做審核頁」，而是：

- 沒有**全量三層問題清單**（連結／來源／SKU）
- 既有 `approved` 被下游（reverse_audit、採購車）當成可加車的終局
- 來源鑑定、群組並排、否決回饋、有界重搜尚未產品化
- 另有多條會寫 `golden_table.json` 的捷徑（商品編輯、舊 sku-review apply）

## UI 路由

| 路由 | 檔案 | 現況 | 分類 | 說明 |
|---|---|---|---|---|
| `/sku-mapping.html` SKU 審核 | `sku-mapping.html` / `sku-mapping.js` / `sku-mapping.css` | 掃描 1688、候選卡、AI 初判、綠黃紅、批次核准／稍後／無匹配／停售、圖片並排（蝦皮 vs 候選） | **extend** | Phase 1 主工作台。缺：全量問題清單、來源鑑定、群組審核、既有核准強制待補驗證篩選 |
| `/sku-mapping.html` URL 管理 | 同上（第二 tab） | 依商品分組 URL、健康檢查、預覽後才 commit、可清 URL | **extend** | 對應連結層。健康檢查幾乎沒跑（DB 僅 2 筆）。換 URL 時可順便核准 SKU |
| `/products.html` | `products.html` / `products.js` / `product_catalog.py` | PR #38：搜尋 Golden、編輯單型號、**套用 URL／Offer 到所有規格且不覆蓋 SKU** | **extend**（寫入要閘） | 方便修連結；`mappingApproved = Boolean(skuName)` 會把填了名稱的列直接當核准 |
| `/golden-import.html` | `golden-import.html` / `golden-import.js` / `golden_import.py` | 從 `shopee_products.json` 找未入 golden 的商品，貼 1688 URL 後預覽候選再 commit | **reuse as-is** | 只負責「新品進表」，不是全量覆核 |
| `/inbound.html` | `inbound.html` / `inbound.js` / `inbound_store.py` / `inbound_worker.py` | 1688 訂單 → 對蝦皮規格入庫 | **unused**（mapping 完整化） | 用 binding／golden 對規格；不鑑定來源。本機 `procurement.db` 尚無 inbound_* 表 |
| `/ads.html` | `ads.html` / `ads.js` / `ads_analysis.py` | 廣告匯出與 AI 分析 | **unused** | 與 mapping 無關 |
| `/` 庫存首頁 | `index.html` / `script.js` | 搜尋、補貨判讀、開 1688 採購車 | **unused**（盤點）／下游消費 approved | 未核准不進車（`main.py` 擋 `mapping_status != approved`） |

## 後端服務與資料

| 模組 | 分類 | 說明 |
|---|---|---|
| `sku_mapping_service.py` | **extend** | 正式管線：live scan、快照 `alibaba_offer_snapshots`、候選 `sku_mapping_candidates`、AI 初判、suggestion、`sku_mapping_reviews` 審核紀錄、核准後寫 golden + binding。SQLite 與 JSON 雙寫 |
| `procurement_store.py` `alibaba_bindings` | **extend** | 採購／入庫用 binding。`upsert_binding` 在有 sku_name 時預設 `alibaba_mapping_status=approved` |
| `product_catalog.py` | **reuse as-is** | 唯讀 catalog；`apply_offer_to_models` 只改 URL／offer_id |
| `ego_browser_1688.py` | **reuse as-is** | 1688 商品頁 live snapshot（需 ego-lite）。本機 `ego-browser` 不在 PATH |
| `golden_import.py` | **reuse as-is** | 新品 commit 寫 mapping 欄位 |
| `alibaba_sku_mapper.py` | **unused**／**replace** | 舊 Playwright CLI，襪子規則對色；會寫 golden。已被 `sku_mapping_service` 取代 |
| `alibaba_phone_case_mapper.py` | **unused**／**replace** | 舊手機殼雙規格 CLI，high confidence 才寫 golden |
| `alibaba_review_report.py` | **unused** | 舊襪子 review JSON → CSV／HTML；`/api/alibaba/sku-review*` 仍掛著 |
| `alibaba_sku_mappings.json` | **unused** | 僅 S6 純色棉襪名稱表；與現況 golden 不一致（純白現為 `白色`／eric offer，檔內仍寫 `2349白色`） |
| `reverse_audit/` | **unused**（鑑定）／**消費端** | `certain = approved + URL + (skuId OR name/spec)`。Phase 1 若仍把「待補驗證」當 certain，會把未鑑定核准送進加車 |
| `scripts/reconcile_cart.py` | **unused** | 離線購物車核對，同樣吃 approved |
| `sku_spec.py` | **reuse as-is** | 規格切分 |

## API（`main.py`）

### Mapping 工作台（應沿用）

- `GET /api/sku-mapping/summary|queue|url-groups|catalog|jobs/{id}`
- `POST /api/sku-mapping/scans`（重建候選／live scan）
- `POST /api/sku-mapping/url-health-checks`
- `POST /api/sku-mapping/url-changes/preview|commit`
- `POST /api/sku-mapping/reanalyze-existing`、`/ai-reviews`
- `POST /api/sku-mapping/decisions`（核准／稍後／無匹配／停售）

### 另寫 golden 的路徑（Phase 1 要閘）

- `POST /api/golden-table/model-alibaba`（商品編輯；含 `overwrite_all` API，UI 已不露出）
- `POST /api/golden-table/model-1688-sku`、`/product-1688-skus`
- `POST /api/golden-table/import/preview|commit`
- `POST /api/alibaba/sku-review/apply`（舊報告套用；預設 overwrite、只寫 `1688_sku_name`）
- `POST /api/alibaba/bindings`（**只寫 DB、不寫 golden**）

## 快照／審核紀錄／AI

| 能力 | 位置 | 分類 |
|---|---|---|
| Offer 快照 | `procurement.db` `alibaba_offer_snapshots`（241 筆／221 offer，皆 `ok`） | **reuse as-is** |
| SKU 候選 | `sku_mapping_candidates`（13495） | **extend**（上限 4 張卡；可載入完整 catalog） |
| AI 初判 | OpenAI／Grok／Gemini／Deepseek；`SKU_MAPPING_AI_PROVIDER` | **extend**；本機無 `.env.local`，金鑰未設定 |
| 審核 log | `sku_mapping_reviews`（9273；大量 `legacy_approval_*`） | **reuse as-is** 當稽核；Phase 1 要加來源鑑定／否決原因 |
| URL 健康 | `alibaba_url_health_checks`（2 筆） | **extend**（功能在、資料幾乎空） |
| `debug_snapshots/` | gitignore；舊 mapper 報告 | **unused** 除非還有本機歷史檔 |

## Phase 1 建議複用策略（僅筆記，不實作）

1. **主畫面 extend `sku-mapping.html`**：加三層篩選與「既有核准待補驗證」隊列，不要新開第三套工作台。
2. **URL 管理 extend**：當連結層；來源鑑定另開群組視圖（同 offer／同圖）。
3. **products.html**：保留 URL／Offer 套用（#38），但核准必須走審核閘，不可 `skuName` 自動 approved。
4. **不要**再啟用 `alibaba_sku_mapper.py`／`sku-review/apply` 當正式寫入。
5. reverse_audit 在來源／SKU 未補驗證前，不應把 `approved` 當 certain（屬 Phase 1+ 契約，本階段未改 code）。
