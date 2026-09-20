# 第 1 層 offer 建議（Mapping Engine 2.2）

對**沒有／缺少 1688 URL** 的蝦皮型號，從**歷史**提出最可能的 offer／URL 清單。  
**唯讀。****不**搜 1688 站內、**不**開 Chrome、**不**寫 `golden_table.json`、**不**當第 2 層 matcher。

契約：[`ai_mapping_engine_SPEC_v0.md`](ai_mapping_engine_SPEC_v0.md) §8.3／階段 2。設計依據：[`offer_discovery_spike.md`](offer_discovery_spike.md)（先讀隔離種子）。KB 指向沿用 2.1：[`mapping_kb_isolated_import.md`](mapping_kb_isolated_import.md)。

## 來源優先序（固定）

| 序 | `source` | 讀哪裡 | 何時當建議 |
|---|---|---|---|
| 1 | `seed_history` | 隔離 Mapping KB（`--kb-db`／`MAPPING_KB_DB`）的成功採購 | `kb_mappings`／名稱正例／清冊 **exact** 此蝦皮型號；否則 `normalize_text` 品名命中 `kb_products.title`，可再加規格命中 `kb_order_items.raw_specs`／`kb_skus.raw_specs`。同分用 `order_count`／`last_ordered_at` |
| 2 | `golden_sibling` | 同蝦皮 `product_id` 其他型號的 Golden `1688_offer_id`／URL | 恰好一個 distinct 核准 offer → 高分；多個 → 列出、標需人工。**不**跨不同 `product_id` |
| 3 | 1688 站內搜尋 | — | **禁止**（本模組沒有搜尋客戶端） |

取消／關閉／退款單不計入種子。缺 KB 檔 → `seed_history` 為空、**不中斷**；兄弟檔仍可回。

## 與第 2 層的衝突

第 2 層（Know-how）吃的是**已知 offer 快照**。若本列已有 URL／`sku_mapping_suggestions.offer_id`，且與第 1 層 top offer 不同 → `conflict=true`、`conflictReasons` 含 `layer2_mismatch`、`needsHuman=true`。

種子 top 與兄弟檔 top 不同 → `seed_vs_sibling`。兩種都**只標記、不自動消解、不寫 Golden**。

## 呼叫

```bash
# 單一型號（缺檔當停用種子）
python -m offer_discovery suggest \
  --product-id <蝦皮 product_id> \
  --model-id <規格ID> \
  --kb-db /workspace/_handoff/mapping_kb_isolated_20260920.db

# 或環境變數（與 2.1 相同）
export MAPPING_KB_DB=/workspace/_handoff/mapping_kb_isolated_20260920.db
python -m offer_discovery suggest --product-id <id> --model-name <型號名稱>

# 缺 URL 型號批次（仍唯讀）
python -m offer_discovery suggest --missing-url --limit 50 --kb-db "$MAPPING_KB_DB"
```

程式：`offer_discovery.suggest_offers(...)`；`SkuMappingService.suggest_layer1_offers(...)`。  
唯讀 HTTP：`GET /api/sku-mapping/offer-suggestions?productId=&modelId=`（可選 `modelName`）。沒有寫入路徑，也沒有審核 UI。

每筆建議含 `offerId`、`url`、`source`（`seed_history`｜`golden_sibling`）、`score`、`reason`。同一 offer 被兩源命中時 `source` 仍是種子（較高優先），`sources` 會列兩個。

## 測試

```bash
python -m unittest tests.test_offer_discovery
```

CI 用 `tests/fixtures/offer_discovery/`（tiny Golden＋種子訂單 JSON）。不要提交營運 `.db`。雲端 VM 可以沒有 `/workspace/_handoff/mapping_kb_isolated_20260920.db`。

## 不做

寫 Golden／backup；種子灌進 live `procurement.db`；`auto_approve.enabled=true`；合併 #41–#46；站內搜／list crawl／開 Chrome；購物車／付款；發明 `sku_id`；用第 1 層取代第 2 層 matcher。
