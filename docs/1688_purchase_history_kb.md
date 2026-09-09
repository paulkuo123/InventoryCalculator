# 1688 歷史採購知識庫（Master KB）

Phase 2 只落地 **schema、store、離線 dry-run／gated 寫入 stub、測試**。  
**不做** 1688 爬取、不開 Chrome、不改採購車、不寫 `golden_table.json`、不把歷史訂單灌進 `inbound_orders`。

人工已核准的 Golden mapping 是最高優先的真相來源；任何 KB 路徑都**不得覆蓋**它。

## 範圍

| 做 | 不做 |
|---|---|
| 在與 `procurement.db` 相同慣例的 SQLite 建 `kb_*` 表 | live list/detail crawl、CDP、開 Chrome |
| 對**隔離** DB（測試 temp 路徑）做冪等 upsert | 把歷史單寫入 `inbound_orders` 當 SoT |
| 從**本地 JSON fixture** dry-run／gated 匯入 | 寫入或改寫 `golden_table.json` |
| `kb_crawl_state` 用 page+pageSize cursor 續跑 | 假設總頁數（例如 185 頁）或在 POST 寫死 `orderStatus` |
| JSONL 匯出到 gitignore 的 `data/1688_master/` | 用名稱／圖片模糊合併 SKU |
| 從 Golden／inbound exact **複製快照**到 `kb_mappings` | 未核准就對 live `procurement.db` 寫歷史 |

預設 DB 檔名仍是 `procurement.db`（與 `procurement_store`／`inbound_store` 同一檔、**加表不改舊表**）。測試與 Phase 2 CLI 寫入必須走 `--db-path` 隔離檔。

## 閘門（freeze → dry-run → 核准）

對齊反向查核的 fail-closed：

```
freeze（Phase 3 才做，本階段沒有）
  → dry-run（預設；只預覽、不寫）
  → 人工看 allowlist
  → import --i-approve-kb-import --allow-order-ids … --db-path <隔離檔>
```

```bash
# 離線預覽（永不寫入）
python -m purchase_history_import dry-run \
  --input tests/fixtures/historical_kb/list_orders_complete.json \
  --allow-order-ids 100000000000000001

# 沒有核准旗標或 allowlist → 拒絕寫入
python -m purchase_history_import import \
  --input tests/fixtures/historical_kb/list_orders_complete.json \
  --db-path /tmp/kb-isolated.db

# Phase 2 允許的唯一寫入：明確旗標 + 訂單 allowlist + 隔離 DB
python -m purchase_history_import import \
  --input tests/fixtures/historical_kb/list_orders_complete.json \
  --db-path /tmp/kb-isolated.db \
  --allow-order-ids 100000000000000001 \
  --i-approve-kb-import

# crawl 子命令永遠拒絕（無網路、無 CDP）
python -m purchase_history_import crawl
```

- 預設 **dry-run**；`import` 未帶 `--i-approve-kb-import` 立即非零退出。
- `--allow-order-ids` 為空 → 拒絕（不隱含「匯入全部歷史」）。
- live `procurement.db` 在 `PHASE3_HISTORY_IMPORT_ENABLED = False` 時拒絕寫入。
- `PHASE3_LIVE_CRAWL_ENABLED` 維持 False；Phase 3 必須另開核准與訂單 allowlist，才能對真實訂單清單做 freeze。
- 旗標**不隱含** crawl、不隱含改 Golden、不隱含入庫。

## 禁止事項

- 爬 1688、開 Chrome、改採購車、對 live 訂單 list POST（含寫死 `orderStatus`）。
- 寫 `golden_table.json` 或任何 `golden_table.json.backup*`。
- 把歷史訂單當入庫 SoT 寫進 `inbound_orders`／`inbound_order_lines`。
- 用商品名稱、圖片、規格相似度把 `unresolved_id` 自動併到真實 `sku_id`。
- 缺欄猜值：價格／數量／圖片／賣家不明就留 `NULL`，不可填 0 或空字串充當已知。
- 未帶 Phase 3 核准 + allowlist 就對生產 DB 做全量歷史匯入。

## 與 Golden／入庫的關係

```
golden_table.json          人工核准 mapping（最高優先，唯讀對 KB）
inbound_orders             到貨入庫作業 SoT（現貨流程，不是歷史 KB）
kb_orders / kb_order_items 歷史採購 Master KB SoT（本模組）
kb_mappings                衍生複本：source = golden_approved | inbound_exact | manual
                           只複製 mapping_status / verified_at 快照，絕不回寫 Golden
```

- Golden 已核准列：KB 可**抄**到 `kb_mappings`，不可改 Golden 檔。
- 入庫 exact 對應：同樣只抄快照，不改 `inbound_*` 寫入路徑。
- 補貨／mutate／restock 路徑本階段不改。

## 表（`kb_` 前綴）

| 表 | 重點 |
|---|---|
| `kb_schema_meta` | `schema_version`（目前常數 `KB_SCHEMA_VERSION = 1`）；舊庫用加欄 migration |
| `kb_products` | PK `offer_id`；url／title／shop_id／raw_json／fingerprint／first／last seen／last_crawled |
| `kb_skus` | `(offer_id, sku_key)` unique；有 `sku_id` 時用真實 id；沒有則 `unresolved_id`，**不**依相似度合併 |
| `kb_orders` | `alibaba_order_id` unique；status／ordered_at／seller／raw_json／schema_version |
| `kb_order_items` | unique `(alibaba_order_id, source_line_id)`；qty／price 可 NULL；provenance：`parser=api\|dom\|list`、source_url、raw_hash |
| `kb_purchase_history` | 依 offer+sku_key 彙總：order_count、total_qty、first／last order id、last_price_cny、last_ordered_at |
| `kb_mappings` | 1688→蝦皮衍生連結；不寫 Golden |
| `kb_crawl_state` | PK `source`；`last_processed_order_id` + `cursor_json`（page／pageSize）；不假設總頁數 |
| `kb_errors` | entity_key、error_class、message、raw_excerpt、created_at、resolved_at |

Phase 1.5 欄位現實（設計對齊，本階段不打 API）：

- List：`mtop.1688.trading.dataline.service` + `OrderListDataLineService.buyerOrderList` 已有 orderId、sourceId（≈offer）、skuId、specId、specItems、price、sumPayment／paidFee、gmtCreate、quantity、status、seller、productName。
- Detail：`mtop.1688.mtoporderservice.queryorder` 對 imageUrl、unitPrice、groupEntriesMap 較完整。
- 分頁：只有 page + pageSize；**不要**在 POST 寫死 `orderStatus`（未證實）。總頁數未知，UI 可能截斷 → 續跑只信 `kb_crawl_state`。

## 模組

- `purchase_history_store.py`：建表、upsert、冪等 `import_order`、彙總、mapping 快照、crawl_state、JSONL 匯出。支援 `db_path=` 隔離檔。
- `purchase_history_import.py`：讀本地 JSON、正規化 list／detail、dry-run 預設、gated 寫入。

測試：`tests/test_purchase_history_store.py`、`tests/test_purchase_history_import.py`，fixture 在 `tests/fixtures/historical_kb/`。

## Phase 3 會怎麼用

1. 人工 freeze（唯讀 CDP／已登入工作階段）把 list／detail JSON 存到本機，**仍不改車**。
2. 對凍結檔 `dry-run`，檢查 allowlist 與缺欄（價格 NULL 等）。
3. 庭安明確核准後：`--i-approve-kb-import --allow-order-ids …`，並打開 `PHASE3_HISTORY_IMPORT_ENABLED`（或等價閘門）才准寫入 live DB。
4. 續跑：讀 `kb_crawl_state.cursor_json` 的 page／lastOrderId，不假設 185 頁。
5. mapping 仍只抄 Golden／inbound exact；衝突以人工核准 Golden 為準。
