# Golden Table Mapping Phase 0 — 三層狀態 schema（2026-09-07）

本 schema 只用於**盤點與報告**。Phase 0 分類器（`golden_mapping_phase0.py`）對 `golden_table.json` **唯讀**，不回寫任何正式欄位。

## 硬性規則

1. **不發明 mapping**：不猜測 SKU、不替換 URL、不把狀態改成核准。
2. **既有核准不是終局**：`1688_mapping_status = approved` 一律標成「既有核准，待補驗證」（或其不完整／衝突子類），**不可**被 reverse_audit／補貨流程在本階段當成已完成鑑定。
3. 三層獨立：連結可能正常但來源錯；來源對但 SKU 錯；三層都「看起來有值」仍可能是歷史誤配。

## 列粒度

一列 = 一個蝦皮商品 × 一個型號（`product_id` + `規格ID`／型號名稱）。

全量第一版 CSV：`docs/golden_mapping_phase0/full_table_pass_20260907.csv`  
計數：`docs/golden_mapping_phase0/full_table_counts_20260907.json`

## 1. 連結／URL 層 `link_status`

判斷「這個型號有沒有一張可開啟的 1688 商品頁」。

| code | 中文 | 判定 |
|---|---|---|
| `url_missing` | 缺連結 | `阿里巴巴商品URL` 空白 |
| `url_malformed` | 連結格式不正確 | 非 `http(s)`、非 1688、或解析不到 `/offer/{id}.html` |
| `url_present_valid` | 有連結，健康檢查通過 | 有 URL 且 `alibaba_url_health_checks.status=valid`（最新一筆） |
| `url_present_invalid` | 有連結，健康檢查失效 | 最新健康檢查為 `invalid` |
| `url_present_needs_attention` | 有連結，需登入／驗證 | 健康檢查 `needs_attention`／`waiting_for_login` |
| `url_present_error` | 有連結，健康檢查失敗 | 健康檢查 `error` |
| `url_present_expired` | 有連結，健康檢查已過期 | 健康檢查 `expired`／`stale` |
| `url_present_unchecked` | 有連結，尚未健康檢查 | 有合法 1688 URL，但沒有可用健康檢查 |

Phase 0 現況：健康檢查幾乎沒跑（`procurement.db` 僅 2 筆），因此大多數有 URL 的列落在 `url_present_unchecked`。

## 2. 來源層 `source_status`

判斷「這個 offer 是否為該型號應採購的供應來源」。Phase 0 **不做來源鑑定**，只標可觀測狀態。

| code | 中文 | 判定 |
|---|---|---|
| `source_missing` | 無來源（無 offer） | 無 URL 也無 `1688_offer_id` |
| `source_single_unverified` | 單一來源，尚未鑑定 | 該商品所有有 URL 的型號共用同一個 offer |
| `source_multi_unverified` | 同商品多來源，尚未鑑定 | 同一 `product_id` 出現 2 個以上 offer（可能是刻意分流，也可能是污染） |
| `source_page_suspect` | 來源頁疑似失效／下架 | `sku_mapping_suggestions.status=suspected_discontinued` |

**沒有** `source_confirmed`：那是 Phase 1 人工鑑定後才允許出現的狀態。

## 3. SKU 層 `sku_status`

判斷「規格對應是否存在、是否完整、是否互相衝突」。**即使已核准也不當最終。**

| code | 中文 | 判定 |
|---|---|---|
| `sku_approved_unverified` | 既有核准，待補驗證 | `mapping_status=approved` 且看起來完整（有 sku_id，沒有同商品共用 sku_id 衝突） |
| `sku_approved_incomplete` | 既有核准，欄位不完整（待補驗證） | approved 但缺 `1688_sku_id`，或同商品其他型號有第二規格而本列沒有 |
| `sku_approved_conflict` | 既有核准，疑似衝突（待補驗證） | approved 且同一 `1688_sku_id` 被同商品兩個不同型號名稱共用 |
| `sku_pending` | 待人工審核 | `pending` |
| `sku_missing` | 尚未對應 SKU | `missing` 或無 sku_name |
| `sku_discontinued` | 停售 | `discontinued` |
| `sku_stale` | 快照失效 | `stale` |
| `sku_no_match` | 無匹配 | `no_match`（golden 目前為 0；suggestion 表另有列） |
| `sku_other` | 其他 SKU 狀態 | 未列舉的 raw status |

## 4. 主問題類型 `primary_problem_type`

一列只選一個主桶，方便第一版全量清單分堆。優先序：

1. **LINK** — `url_missing`／`url_malformed`／`url_present_invalid`
2. **SOURCE** — `source_multi_unverified`／`source_page_suspect`
3. **SKU** — 缺／待審／停售／失效／不完整／衝突
4. **EXISTING_APPROVAL** — 其餘已核准且三層「看起來有值」→ 仍標「既有核准，待補驗證」

一列可另帶 `flags`（`|` 分隔），例如 `multi_source`、`packaging`、`same_image_diff_name`、`approved_conflict`。`same_image_diff_name` 在手機殼很常見（同色圖給不同機型），**不代表**一定是同圖異材；異材需人工看型號名稱。

## 5. 與現有 raw 欄位的關係

| Golden 欄位 | 角色 |
|---|---|
| `阿里巴巴商品URL`、`1688_offer_id` | 連結／來源輸入 |
| `1688_sku_id` / `1688_sku_name` / `1688_sku_second_name` / `1688_spec_text` | SKU 輸入 |
| `1688_mapping_status` | raw 審核狀態；**approved ≠ 已完成鑑定** |
| `1688_mapping_source` | 誰寫入（`legacy_user_approved`、`manual`、`ai_reviewed`…） |
| `1688_mapping_fingerprint` | 名稱組合指紋 |
| `1688_offer_fingerprint` | 快照指紋；常缺 |

Phase 1 若要寫回，應新增獨立的鑑定欄位（例如 `link_review_status`／`source_review_status`／`sku_review_status`），**不要**把「待補驗證」直接改寫成現有 `approved`。
