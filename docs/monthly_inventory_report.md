# 每月蝦皮庫存分析

每月 1 號的例行任務，改成跑這支固定腳本，再由人讀結果寫兩三句結論。腳本只讀現成檔案，不重抓蝦皮、不重抓 1688、不加車、不改 `golden_table.json`、不改觀察清單、也不改首頁的月數。

```bash
python3 scripts/monthly_inventory_report.py
```

例行任務的提示可以收成：

> 跑 `python3 scripts/monthly_inventory_report.py`，讀 `reports/monthly_inventory_YYYYMM/monthly_report.md`，用兩三句話摘要危急型號、建議件數，以及第一批該先處理的規格。不要重算、不要改檔。

## 它讀什麼

| 檔案 | 用途 |
|---|---|
| `shopee_products_latest.json` | 庫存與月銷。先找程式庫根目錄，再找 `reports/monthly_inventory_*/sources/` 裡最新的一份 |
| `watchlists/personal_watchlist.json` | 觀察清單 |
| `watchlists/personal_watchlist_exclusions.json` | 排除清單（襪子等） |
| `golden_table.json` | 只讀。停售標記用對照狀態與 1688 規格名 |
| `procurement.db` | 可缺。有的話以唯讀開啟，看綁定與疑似下架 |

開始前會檢查商品檔：必須是有效 JSON、至少有一個商品、修改時間不能太舊。不合用就印出錯誤並結束，不寫報告。

## 參數

- `--max-age-hours N`：商品檔修改時間最久可以幾小時。預設 **48**。超過就停止、不寫報告。確定要用較舊的檔時再調高，例如 `--max-age-hours 72`。
- `--overwrite`：輸出目錄裡已經有月報檔（`monthly_report.md`、`monthly_report.json`、`critical_models.csv`、`priority_batch.csv`、`BLOCKER.md`）時，**預設拒絕覆寫**。只有 `sources/` 不算已有報告，會照常執行，也不會為了備份去複製 `sources/`。加上 `--overwrite` 才重跑，而且只先備份那些月報檔。檔名後面是本機時間與時區偏移，不固定寫成 `+0800`。
- 水位訊號的四個檔（`watchlist_stock_signal.md`、`watchlist_stock_signal_history.json`、`watchlist_stock_signal_priority.csv`、`watchlist_stock_signal_products.csv`）若已在目錄裡，呼叫前會先備份。`history.json` 是累積的趨勢紀錄，備份失敗就整次中止，不覆寫。
- `--month YYYYMM`、`--out`、`--products`、`--skip-stock-signal`：月份、輸出目錄、商品檔路徑，以及要不要順便跑水位訊號。

```bash
python3 scripts/monthly_inventory_report.py --help
```

## 它寫什麼

目錄是 `reports/monthly_inventory_YYYYMM/`（月份用商品檔修改時間，台北時間；可用 `--month YYYYMM` 指定）：

- `monthly_report.md`：給人看的報告。開頭先講大數字（危急件數、補到目標水位的總件數、斷貨、第一批），再分 A～E
- `monthly_report.json`：同一份數字，方便對帳
- `critical_models.csv`：危急型號
- `priority_batch.csv`：第一批（斷貨且建議至少 20 件）
- `BLOCKER.md`：觀察清單有、商品檔沒有的商品 ID。都抓到就不會留這個檔
- `sources/`：這次用到的商品檔與清單副本，加上 `manifest.json`（含 golden 的檢查碼）。不複製 `procurement.db`。目錄裡只有這層時，不會被當成舊月報而拒絕
- 水位訊號（`scripts/watchlist_stock_signal.py` 在的時候）：跟月報同一目錄的四個檔，見上面的備份規則

## 算法（沿用現成函式）

- 範圍：觀察清單，套用 `home_bootstrap` 的排除（排除清單，以及品名有「襪」）。
- 危急：月銷大於 0，而且庫存 ÷ 月銷小於 1.5。庫存 0 又有月銷，算斷貨。缺月銷視同 0，不算有賣出。
- 水位：`restock_rules.target_months_for_product`。名稱有「手機殼」或「手机壳」、後面不是吊飾或掛繩，目標 3 個月；其餘 4 個月。
- 建議量：`restock_rules.calculated_restock_details`。目標用月銷乘月數後四捨五入，減掉庫存，再取整。庫存是 0 時，這支函式可能改用歷史銷量佔比。
- 第一批：斷貨，而且建議量至少 20 件。
- 補到目標水位：觀察清單裡每一個規格都用 `calculated_restock_details` 算建議量，再加總。已經在 3 或 4 個月水位以上的是 0。這個數字包含還沒跌破 1.5 個月、但還沒補滿的規格，所以會大於或等於危急建議件數。
- 分類：`restock_rules.restock_category`。手機殼、吊飾／掛繩、其餘各一列。JSON 的 `phone_case_critical` 與 `categories.phone_case.critical_specs` 是同一個數，三列的危急規格加總等於 `critical_specs`。規格名是「加購」的手機殼仍算在手機殼這一列（所以會進 `phone_case_critical`），目標月數依店規改回 4，不會因為不是 3 個月就被算到「其餘」。
- 停售：`restock_loop.scan.launcher_ineligibility_reasons` 裡的停售規格名與對照狀態。危急名單裡會標出來，**主數字不扣**。上一份手寫月報也沒扣。

## 跟上一份手寫月報對數字時要注意

2026-10 手寫報告（`reports/monthly_inventory_202610/monthly_report.md`）的主數字是：觀察清單抓到 157 個商品、有賣出的型號 1,367、危急 315 個型號／建議 3,190 件、斷貨 126 個型號／1,205 件、第一批 14 個型號／420 件。

這支腳本的危急定義與那份相同（有月銷、可撐不到 1.5 個月），所以危急型號數、斷貨型號數應該對得上。建議件數可能不同，原因有兩個：

1. 手寫報告把「保護殼／軟殼／硬殼／手機套」也算 3 個月，還把「手機殼吊飾」算 3 個月。店規函式只認「手機殼／手机壳」，吊飾與掛繩維持 4 個月。
2. 庫存是 0 時，`calculated_restock_details` 可能用歷史銷量佔比，建議量會比「只用本月銷量」高。

這份程式庫沒有 2026-10 的商品檔與 `procurement.db`，實數要在遠端機器上對。

## 水位訊號

`scripts/watchlist_stock_signal.py` 已在主線。月報跑完會順便呼叫它，產出放在同一個月份目錄。報告裡仍留標記 `OPTIONAL HOOK: PR #135 watchlist_stock_signal`。檔案不在就略過，月報照樣完成。`--skip-stock-signal` 可以關掉這一步，已有的那四個檔就不會被動到。
