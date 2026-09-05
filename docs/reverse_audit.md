# 反向補貨查核（reverse_audit）

以 **watchlist ∩ shopee_products ∩ golden_table** 算出應補集合，反向核對 1688 採購車與待付款／待發貨／待收貨。流程固定為：**freeze → dry-run → 人工核准 → mutate**。

## 進入點

```bash
python -m reverse_audit freeze --date YYYYMMDD
python -m reverse_audit dry-run --date YYYYMMDD
python -m reverse_audit mutate --date YYYYMMDD --i-approve-mutate
```

也可用 `--dir reports/reverse_audit_YYYYMMDD` 覆寫路徑。未給 `--date`／`--dir` 時，日期預設為 **Asia/Taipei 今天**（`YYYYMMDD`）。

輸出一律落在：`reports/reverse_audit_YYYYMMDD/`。

## 參數

| 參數 | 說明 |
|---|---|
| `freeze` | 透過 CDP 唯讀擷取採購車＋三個訂單池，寫入 `live_*.json`／`snapshot_meta.json` |
| `dry-run` | **離線**：讀既有 `live_*.json` 與凍結來源，產出 expected／diff／報告；**不加車、不改量** |
| `mutate` | 依 `missing_to_add.csv` 加車；**必須**加 `--i-approve-mutate`，否則 fail-closed 拒絕 |
| `--date YYYYMMDD` | 報告目錄日期戳 |
| `--dir PATH` | 直接指定報告目錄（優先於 `--date`） |
| `--no-refreeze-sources` | dry-run 時沿用既有 `sources/`，不重拷 repo 根目錄來源 |
| `--i-approve-mutate` | mutate 專用危險旗標；預設永不改車 |
| `--sources-only` | freeze 只凍本地來源，不跑 CDP |
| `--cdp URL` | 可選，設 `ALIBABA_RESTOCK_CDP` |

## 水位與範圍

- 範圍：watchlist（套用 exclusions）∩ `shopee_products.json` ∩ `golden_table.json`
- 水位：`restock_rules.target_months_for_product` — **手機殼／手机壳 = 3 個月**，其餘 **4 個月**（吊飾／掛繩／明確加購除外）
- 同 1688 `(offerId, skuId)` 加總各蝦皮型號建議量
- certain：`approved` + 有效 URL + 有 `skuId`
- uncertain／缺欄：**只記不猜**，不發明 URL／skuId
- skip：停售／已知售完等

## 覆蓋與 shortfall

- 訂單三池任一出現同 `(offerId, skuId)` → 視為已覆蓋（不問在途量是否 ≥ 應補）
- 車內數量 ≥ 應補 → 覆蓋
- 車內 `0 < qty < 應補` → **PAUSED**：完整列入 `qty_shortfall.csv`，**不自動改量**
- 四處皆無 → `missing_to_add.csv`（僅 dry-run 建議；mutate 需核准旗標）

## 成功標準

- dry-run 產出：`expected_*.csv`、`covered.csv`、`missing_to_add.csv`、`qty_shortfall.csv`、`ambiguous.csv`、`dry_run_summary.json`、`dry_run_report.md`
- `dry_run_summary.json` 的 `status` 為 `READY_FOR_APPROVAL` 或 `PAUSED`
- mutate 未帶 `--i-approve-mutate` 時立即非零退出且不碰購物車
- certain 列不得靠猜測補 URL／skuId；uncertain 不得進入 mutate
- 四池 `complete!=true` 時 dry-run 拒絕執行

## 實作備註

- `freeze` 先凍 `sources/`；完整模式再跑 CDP。缺腳本時退回 sources-only（不假裝已抓 live）。
- `freeze`／`mutate` 為既有 CDP 腳本的薄封裝：
  - `scripts/freeze_reverse_audit_pools_20260905.py`
  - `scripts/mutate_add_missing_cdp_20260906.py`
- 需本機已登入的 Chrome remote debugging；freeze／mutate **不會**清除購物車或結束 Chrome
- 勿把 cookies、chrome profile、live dump 提交進 git

## 相關文件

- 方案：`/workspace/_handoff/reverse-restock-audit-plan-20260905.md`（若存在）
- 購物車核對：`docs/cart-reconciliation.md`
