# Golden 整表唯讀分桶（AI-1｜2026-09-09）

日期：2026-09-09  
範圍：`golden_table.json` **全型號列**（非整表重驗）  
Golden SHA-256（前／後）：`8a95064fbe116f9e6ead24837ced1511ce34a9f3ac69358d9c2e0fb585ab3d2e` / `8a95064fbe116f9e6ead24837ced1511ce34a9f3ac69358d9c2e0fb585ab3d2e`  
HEAD：`90962b237e3e6faa11fe7354317d75ae5624ed44`  
**未修改** `golden_table.json`。未對 approved 列做 HTTP／CDP 整批重驗。

## 分桶

| 桶 | 筆數 | 含義 |
|---|---:|---|
| `no_url` | 945 | 無 URL 且無 offer_id |
| `url_suspect` | 413 | discontinued／stale／URL 格式怪／CDP dead 弱信號／oid 不一致 |
| `mapping_suspect` | 3264 | 有 URL 但缺 sku_id、status 為 missing/pending／規格不全 |
| `ok_skip` | 1296 | `approved` 且有 URL＋sku_id，且非上述嫌疑（**不要**整批重驗） |
| **合計** | 5918 | 須等於全型號列數 |

分桶合計＝型號列：`true`（5918 / 5918）。  
商品數：golden 355、shopee 快照 353。

## 分桶理由

| bucket_reason | 筆數 |
|---|---:|
| `has_url_mapping_incomplete` | 3264 |
| `approved_complete` | 1296 |
| `no_url_and_no_offer_id` | 945 |
| `health_dead` | 265 |
| `status_discontinued` | 145 |
| `status_stale` | 2 |
| `oid_mismatch` | 1 |

## mapping_status

| status | 筆數 |
|---|---:|
| `approved` | 3800 |
| `missing` | 1832 |
| `discontinued` | 236 |
| `pending` | 48 |
| `stale` | 2 |

`1688_mapping_source` 分布（**多來源不算錯**）：`legacy_user_approved` 2505、`legacy_import` 1810、`manual` 1198、`ai_reviewed` 310、`url_change_pending` 41、`legacy_repair` 22、`manual_url_change` 19、`manual_deferred` 7、`manual_soldout_20260906` 4、`manual_live_verify_20260905` 2。

## CDP 死連弱信號（不新開 live probe）

來源：`/workspace/_handoff/golden_triage_cdp24_health_20260909.csv`（載入 24 個 offer；先前 17 dead／7 alive）。  
列對上 `health=dead`：265；對上 `alive`：210。  
`weird_url`：0；儲存欄位 URL↔offer_id 不一致：1。

## 與 shopee_products.json 對齊

shopee 快照 353 筆全部都在 golden。  
golden 有、最新 shopee 快照沒有：2 筆。

- `15848359384` 隔日到貨🔥 登山扣 掛鉤 掛扣 Airpods專用掛鉤 葫蘆鉤 鑰匙掛鉤 登山掛鉤 Airpods保護殼掛鉤（1 型號；approved:1）
- `5002617576` 隔日到貨🔥 韓版ins🍒卡通帆布小方包 B29 印花少女 單肩斜背包 側背包 學生斜背 熱銷款 帆布材質 可愛少女風（4 型號；missing:4）

先前紀錄（2026-09-08）也是 shopee 353 全在 golden、golden 多 2 筆；本次重算仍為 2 筆，商品 ID：`15848359384`、`5002617576`。

## 規則備註

1. 優先序：無 URL＋無 offer → oid 不一致 → CDP dead → discontinued/stale → URL 格式怪 → approved 完整 skip → 其餘 mapping_suspect。
2. `ok_skip` **沒有**做 live 探測；後續 AI-2 只應對 `url_suspect` 與久未驗證列分批探活。
3. 多來源、同商品多 offer **不是**錯誤。

## 檔案

- 全型號列 CSV：`golden_ai1_full_buckets_20260909.csv`
- 計數 JSON：`golden_ai1_counts_20260909.json`
- 蝦皮對齊：`golden_ai1_shopee_alignment_20260909.csv` / `.json`
- 同內容複製到 `/workspace/_handoff/golden_ai1_*_20260909.*`

請保持本 PR 開啟，**不要合進 main**。
