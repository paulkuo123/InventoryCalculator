# Golden 分桶第一刀（54 筆急迫補貨）

日期：2026-09-08  
範圍：補貨急迫清單 54 型號（非整表 945）  
Golden SHA-256：`8a95064fbe116f9e6ead24837ced1511ce34a9f3ac69358d9c2e0fb585ab3d2e`  
HEAD：`90962b237e3e6faa11fe7354317d75ae5624ed44`  
**未修改** `golden_table.json`。

## 分桶（對現況 golden 重判）

| 桶 | 筆數 |
|---|---:|
| `no_url` | 20 |
| `url_suspect` | 18 |
| `mapping_suspect` | 16 |
| `ok_skip` | 0 |
| **合計** | 54 |

原清單：`no_url` 20／`url_suspect` 18／`mapping_suspect` 16。桶變更：0 筆。

桶變更明細：無（54 筆皆與補貨急迫清單原桶一致）

`ok_skip`：無。這 54 筆皆非 `approved` 且同時有 URL + `sku_id`（僅 2 筆有 sku_id，狀態皆為 `discontinued`）。多來源不視為錯誤。

## URL 健康（34 筆有 URL／24 個獨立 offer）

列層級：

| health | 筆數 |
|---|---:|
| `alive` | 0 |
| `dead` | 0 |
| `oid_mismatch` | 0 |
| `unchecked` | 20 |
| `error` | 34 |

獨立 URL：24；探測結果 `error`=24 `alive`=0 `dead`=0 `oid_mismatch`=0。

方法：先對 24 個獨立 URL 做 HTTP GET。全部回 200，但 body／最終位址為 1688 `_____tmd_____/punish`（WAF／驗證碼），**不能**當成活頁或死頁。接著用既有 `alibaba_chrome_profile` Playwright CDP 抽樣（只開頁、不加車）：會跳到淘寶／1688 登入頁，工作階段已過期。Playwright MCP 同樣停在 Captcha Interception。因此有 URL 的 34 列標 `error`（`waf_punish`／login wall），20 筆無 URL 標 `unchecked`。

庫內 `alibaba_url_health_checks` 對這 24 個 offer **沒有**歷史列；部分 offer 在 `alibaba_offer_snapshots` 曾為 `ok`（最舊可到 2026-08／09），那不是本次 live 探測。

儲存欄位 URL↔`1688_offer_id`：0 筆不一致。3 筆 URL 有值但 `1688_offer_id` 空白（可從 URL 解析），仍留在 `mapping_suspect`。

## 建議下一刀（庭安）

1. 用已登入 1688 的瀏覽器重跑這 24 個 offer 的 live 健康（本環境過不了 WAF／登入牆）。
2. 候選備料（計劃 C）優先：`no_url` 20 筆，以及 `mapping_suspect` 裡建議補貨量最高者（口紅化妝包、開心兔、水晶愛心繩、筆套）。
3. `url_suspect` 17 筆 `discontinued` + 1 筆 `stale`：先確認店家是否真下架，再決定找替代店或標停產。

## 檔案

- 型號列 CSV：`urgent_54_rebucket_health_20260908.csv`
- 獨立 URL HTTP 探測：`unique_url_http_probe_20260908.csv`
- 計數 JSON：`counts_20260908.json`
- CDP 抽樣（Captcha／登入牆，不加車）：`cdp_samples_20260908.json`

本目錄會進 PR。同內容也寫在 `reports/golden_triage_20260908/`（該路徑被 `.gitignore` 忽略）以及 `/workspace/_handoff/`。

## CDP live-health 複檢（2026-09-08 UTC／檔名 20260909）

Signed-in Chrome CDP `127.0.0.1:9232`（chrome-profile-10），重用「1688精选货源」分頁，唯讀、不加車。HTTP WAF 已被取代為真實分類。

| health | 獨立 URL |
|---|---:|
| `alive` | 7 |
| `dead` | 17（404×15 + 下架詳情×2） |
| `waf` / `login_wall` / `oid_mismatch` / `error` | 0 |

明細：`cdp24_health_20260909.csv`、`cdp24_SUMMARY_20260909.md`、`cdp24_RESULT_20260909.md`。

## 計劃 C 候選證據包（2026-09-08 UTC／檔名 20260909）

Signed-in CDP 9232，唯讀詳情／店舖（全站搜尋會 captcha）。**未改 golden、未加車。**

| 結果 | 筆數 |
|---|---:|
| `with_candidate` | 13（高 2／中 5／低 6） |
| `no_candidate` | 36 |
| **合計** | **49** |

明細：`planC_candidates_20260909.csv`、`planC_SUMMARY_20260909.md`、`planC_RESULT_20260909.md`。
