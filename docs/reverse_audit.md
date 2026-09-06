# 反向補貨查核（reverse_audit）

以 **watchlist ∩ shopee_products ∩ golden_table** 算出應補集合，反向核對 1688 採購車與待付款／待發貨／待收貨。流程固定為：**freeze → dry-run → 人工核准 → mutate**。

## 每次預覽請重抓四池

採購車與待付款／待發貨／待收貨是**動態**的。每次要看最新預覽，請先重抓四池，**不要**沿用舊的 `live_*.json` 當「最新」。

一鍵（freeze 四池 → 離線 dry-run）：

```bash
python -m reverse_audit refresh --date YYYYMMDD
```

等同別名：`python -m reverse_audit dry-run --date YYYYMMDD --refreeze`

需要本機已登入的 Chrome remote debugging / CDP（既有 freeze 腳本）。**不是** Grok Bot computer-use。freeze／mutate **不會**清除購物車或結束 Chrome。

`--cdp`、`--sources-only`（只凍本地來源、不跑 CDP）可傳給 `refresh`／`dry-run --refreeze`。freeze 之後預設仍會從 repo 重拷 `sources/`；若要沿用 freeze 剛寫入的 sources，加 `--no-refreeze-sources`。

## 進入點

```bash
python -m reverse_audit refresh --date YYYYMMDD
python -m reverse_audit freeze --date YYYYMMDD
python -m reverse_audit dry-run --date YYYYMMDD
python -m reverse_audit dry-run --date YYYYMMDD --refreeze   # 等同 refresh
# 加車／改量／刪除各走獨立核准旗標（互不隱含；缺旗標立即拒絕）
python -m reverse_audit mutate --date YYYYMMDD --i-approve-mutate
python -m reverse_audit mutate --date YYYYMMDD --i-approve-set-qty
python -m reverse_audit mutate --date YYYYMMDD --i-approve-remove
```

也可用 `--dir reports/reverse_audit_YYYYMMDD` 覆寫路徑。未給 `--date`／`--dir` 時，日期預設為 **Asia/Taipei 今天**（`YYYYMMDD`）。

輸出一律落在：`reports/reverse_audit_YYYYMMDD/`。

## 如何改數量

dry-run 後若有 `qty_shortfall.csv`（車內不足）或 `qty_excess.csv`（車內超量），用 **獨立** 旗標設量到 expected（絕對設總數，不是累加）：

```bash
python -m reverse_audit mutate --dir reports/reverse_audit_YYYYMMDD --i-approve-set-qty
```

- shortfall：**往上補**到 `expected_qty`
- excess：**往下砍**到 `expected_qty`／`target_qty`
- 同 key 多 `cartId` → 整 key 跳過（不拆量）
- **不**會順便加車或刪除；只要改量不必帶 `--i-approve-mutate`
- 有 shortfall 時，僅加車仍會被擋；同一指令同時帶 `--i-approve-set-qty` 且改量步驟成功後，加車才可解鎖

## 如何刪除

預設**絕不刪**。庭安要真的說「刪」，並明確帶 `--i-approve-remove`，才會刪 `unexpected_in_cart.csv` 裡 `removable=true` 的列：

```bash
python -m reverse_audit mutate --dir reports/reverse_audit_YYYYMMDD --i-approve-remove
```

- 執行時會再驗：訂單池／uncertain／skip／ambiguous／手改 `removable=true` 仍拒
- 不受 shortfall PAUSE 擋（與補量正交）
- **不**會順便加車或改量

## dry-run 人工交付（請先看這份）

**主交付檔：`補貨比對結果.csv`**（UTF-8-SIG，Excel 可直接開）

每個**蝦皮型號／規格一列**（概念鍵：`蝦皮商品id + 蝦皮規格id + 型號`），**不是**把同 1688 SKU 的多個型號用 `|` 串在同一列再加總應補數量。

| 欄位 | 說明 |
|---|---|
| 類型 | 車裡缺少（建議加）／車裡數量不足／車裡數量過多／車裡多出來（可能可刪｜先不要刪） |
| 蝦皮商品id／蝦皮規格id／型號 | **單一**型號的 id／規格／名稱（不是 pipe 聚合）。非預期列（沒有對上蝦皮型號）商品 id／規格 id 空白，型號用車內規格文字 |
| 蝦皮商品名稱 | 該型號所屬商品名稱 |
| 應補數量 | **該型號** `suggested_qty`（不是同 SKU 加總） |
| 車內數量 | 該 1688 `(offer,sku)` 的車內量；多個蝦皮型號對到同一 SKU 時，各列顯示**同一**車內量（見備註） |
| 差額說明 | 缺少：建議加 N（該型號）；不足／過多：少 N／多 N（該 1688 SKU 層級差額）；非預期：「車內 N，不在應補清單」 |
| 1688網址／1688_offer／1688_sku／備註 | 對帳與人工判斷用 |

列順序：缺少（應補量大者優先）→ 不足 → 過多 → 多出來可刪 → 多出來先不要刪。**不含**已覆蓋（covered）列。

其餘英文檔名 CSV（`missing_to_add.csv`、`qty_shortfall.csv`、`qty_excess.csv`、`unexpected_in_cart.csv`、`covered.csv`、`expected_*.csv`、`ambiguous.csv`）為**機器用／內部**：仍可依 `(offer,sku)` 聚合（pipe 串接型號、加總應補數量）。加車讀 `missing_to_add.csv`、改量讀 shortfall／excess、刪除讀 unexpected；一般人工先看整合表即可。

## 參數

| 參數 | 說明 |
|---|---|
| `freeze` | 透過 CDP 唯讀擷取採購車＋三個訂單池，寫入 `live_*.json`／`snapshot_meta.json` |
| `refresh` | **一鍵**：先 freeze 四池再 dry-run（每次預覽請用這個） |
| `dry-run` | **離線**：讀既有 `live_*.json` 與凍結來源，產出整合表＋機器 CSV／報告；**不加車、不改量、不刪除** |
| `dry-run --refreeze` | 等同 `refresh` |
| `mutate` | 改車；**至少**一個核准旗標，否則 fail-closed 拒絕 |
| `--date YYYYMMDD` | 報告目錄日期戳 |
| `--dir PATH` | 直接指定報告目錄（優先於 `--date`） |
| `--refreeze` | dry-run 前先 freeze 四池 |
| `--no-refreeze-sources` | dry-run 時沿用既有 `sources/`，不重拷 repo 根目錄來源 |
| `--i-approve-mutate` | **只加車**（`missing_to_add.csv`）；不隱含改量／刪除 |
| `--i-approve-set-qty` | **只改量**到 expected（shortfall 上補＋excess 下砍）；不隱含加車／刪除 |
| `--i-approve-remove` | **只刪** `removable=true`；庭安須真的說刪；預設不刪 |
| `--sources-only` | freeze／refresh 只凍本地來源，不跑 CDP |
| `--cdp URL` | 可選，設 `ALIBABA_RESTOCK_CDP` |

## 水位與範圍

- 範圍：watchlist（套用 exclusions）∩ `shopee_products.json` ∩ `golden_table.json`
- 水位：`restock_rules.target_months_for_product` — **手機殼／手机壳 = 3 個月**，其餘 **4 個月**（吊飾／掛繩／明確加購除外）
- 同 1688 `(offerId, skuId)` 加總各蝦皮型號建議量（**機器 CSV／mutate** 用此聚合；人工 `補貨比對結果.csv` 仍一型號一列）
- certain：`approved` + 有效 URL +（有 `skuId` **或** 可用 name/spec）→ 進 qty diff／mutate 邊界
- 缺 `skuId` 但有 name/spec：仍進 certain；聚合時若車內 **唯一** name/spec 對上（同加車腳本 `cart_line_matches_item`）→ 用該 live `skuId` 做 qty 對帳（**不回寫** golden）
- name/spec **歧義**（一列對多車，或一車對多筆 distinct name/spec）→ fail closed：不進 mutate／delete
- uncertain／缺欄：**只記不猜**，不發明 URL／skuId（無 approved／URL，或既無 `skuId` 也無可用 name/spec）
- skip：停售／已知售完等

## 覆蓋、shortfall、超量與非預期

- 訂單三池任一出現同 `(offerId, skuId)` → 視為已覆蓋（不問在途量是否 ≥ 應補）
- 車內數量 ≥ 應補 → 覆蓋；若 `cart > expected`（且非 order-covered）另寫 `qty_excess.csv`（**只列出，不 PAUSE、不改量**）
- 車內 `0 < qty < 應補` → **PAUSED**：完整列入 `qty_shortfall.csv`，**不自動改量**
- 四處皆無 → `missing_to_add.csv`（僅 dry-run 建議；mutate 需核准旗標）
- 反向掃車 → `unexpected_in_cart.csv`：以 **certain**（含唯一 name/spec 解析後的 key）為界；uncertain／skip／訂單池同 key → `removable=false`；歧義 name/spec → `ambiguous_name_spec`（不可刪）；訂單已覆蓋但仍佔車 → 可選清車（`order_covered_still_in_cart`，不可刪）。不再使用 `name_spec_protected` uncertain 保護路徑
- 同 `(offerId, skuId)` 多 cart 列 → 整 key fail，列入 `ambiguous.csv`（`multi_cart_lines`）
- 範圍：全車 certain；**不理**正向書包 cutoff
- 旗標**互不隱含**：`--i-approve-mutate`＝加車；`--i-approve-set-qty`＝改量；`--i-approve-remove`＝刪除
- 多旗標同跑時順序：`set-qty → remove → add`；remove **不受** shortfall PAUSE 擋；add 在 shortfall 非空時仍鎖，除非同跑已核准並完成 set-qty

## 成功標準

- dry-run **人工主檔**：`補貨比對結果.csv`（UTF-8-SIG；每個蝦皮型號／規格一列）
- dry-run 另產出機器用：`expected_*.csv`、`covered.csv`、`missing_to_add.csv`、`qty_shortfall.csv`、`qty_excess.csv`、`unexpected_in_cart.csv`、`ambiguous.csv`、`dry_run_summary.json`、`dry_run_report.md`（機器檔可維持 offer+sku 聚合）
- `refresh`／`dry-run --refreeze` 先 freeze 四池再 dry-run
- `dry_run_summary.json` 的 `status` 為 `READY_FOR_APPROVAL` 或 `PAUSED`；`diff` 含 `qty_excess`／`unexpected_*` 計數；`outputs` 標示主交付為整合表
- mutate 未帶任何核准旗標時立即非零退出且不碰購物車；錯旗標／缺對應旗標 fail-closed
- certain 列不得靠猜測補 URL／skuId；uncertain／訂單池保護 key 不得進入 remove
- 四池 `complete!=true` 時 dry-run 拒絕執行

## 實作備註

- `refresh` 等於 freeze 四池再 dry-run；每次預覽請用這個，不要沿用舊 live dump
- `freeze` 先凍 `sources/`；完整模式再跑 CDP。缺腳本時退回 sources-only（不假裝已抓 live）。
- `freeze`／`mutate` 為 CDP 腳本的薄封裝：
  - `scripts/freeze_reverse_audit_pools_20260905.py`
  - `scripts/mutate_add_missing_cdp_20260906.py`（加車）
  - `scripts/mutate_set_qty_cdp.py`（改量）
  - `scripts/mutate_remove_cdp.py`（刪除）
- 需本機已登入的 Chrome remote debugging；freeze／mutate **不會**清除購物車或結束 Chrome
- 勿把 cookies、chrome profile、live dump 提交進 git

## 相關文件

- 方案：`/workspace/_handoff/reverse-restock-audit-plan-20260905.md`（若存在）
- 購物車核對：`docs/cart-reconciliation.md`
