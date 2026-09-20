# Mapping KB 隔離 schema ＋ 唯讀匯入（階段 1）

Mapping Engine 第一步：把 Golden 核准正例（與可選隔離種子）**抄到新的隔離 SQLite**。  
**不是**第二套 matcher。**不**寫 `golden_table.json`。**不**寫 live `procurement.db`。**不**開 `auto_approve.enabled`。

契約見 [`ai_mapping_engine_SPEC_v0.md`](ai_mapping_engine_SPEC_v0.md)。既有 `kb_*` 見 [`1688_purchase_history_kb.md`](1688_purchase_history_kb.md)。

**階段 2.1（本文件後半）：** 既有 `SkuMappingService.historical_support` **可選唯讀**隔離 Mapping KB。不重建 matcher、不寫 Golden、不把名稱正例升成核准 sku mapping。

## 做／不做

| 做 | 不做 |
|---|---|
| 在隔離 `--db-path` 沿用並加表：`kb_products`／`kb_skus`／`kb_mappings`＋蝦皮清冊／名稱正例／負例 | 改 Golden（含 backup） |
| 預設 dry-run；寫入須 `--i-approve-kb-import` | 把種子或收成灌進 live `procurement.db` |
| 核准列分三桶：已抄／名稱正例／非核准略過 | `auto_approve.enabled = true` |
| `--source-kb` 唯讀（檔不存在就跳過） | 發明 `sku_id`、合併 `unresolved_id` |
| fixture 測試，不需 67MB 營運種子 | 購物車、付款、#41–#46、大重構 matcher |

## 表（隔離檔）

沿用 `PurchaseHistoryStore` 的 `kb_*`（`KB_SCHEMA_VERSION = 2`）：

| 表 | 角色 |
|---|---|
| `kb_products`／`kb_skus` | 1688 offer／SKU（有真實 `sku_id` 才寫 SKU） |
| `kb_mappings` | 正例：`source=golden_approved`，且 **有 offer + sku_id** |
| `kb_shopee_products`／`kb_shopee_models` | 蝦皮清冊（收成，不是 Canonical 合併） |
| `kb_name_positives` | 核准但無 `sku_id` 的名稱組合正例（**不發明 sku_id**） |
| `kb_negative_examples` | 負例複本（可選 `--negatives` JSON；不讀 live 執行期表） |
| `kb_orders` 等 | 僅當 `--source-kb` 存在時從種子唯讀抄入 |

`copy_golden_approved_snapshots()` 行為不變：沒有 `1688_sku_id` 的核准列**仍不會**進 `kb_mappings`。本 CLI 把它們另存到 `kb_name_positives`，`skip_reason=approved_without_sku_id`。

## 路徑

| 角色 | 路徑 | 進 git？ |
|---|---|---|
| Golden 來源（唯讀） | `golden_table.json` | 是（**本 CLI 只讀**） |
| 營運機種子（可選、唯讀） | `/workspace/_handoff/kb_excel_success_isolated_20260912.db` | 否（約 67MB；雲端 VM 常缺） |
| 建議輸出（營運機） | `/workspace/_handoff/mapping_kb_isolated_YYYYMMDD.db` | 否 |
| 建議輸出（開發／CI） | `reports/mapping_kb_isolated_YYYYMMDD.db` | 否（`reports/` 已 gitignore） |
| 小型 fixture | `tests/fixtures/mapping_kb/` | 是 |

種子檔不存在時 **不要失敗**：dry-run／import 都只收成 Golden。CI 用 fixture，不要提交真實 `.db`。

## 指令

預設 dry-run（不建立輸出檔）：

```bash
python -m mapping_kb_import dry-run \
  --golden golden_table.json \
  --source-kb /workspace/_handoff/kb_excel_success_isolated_20260912.db \
  --db-path reports/mapping_kb_isolated_20260920.db
```

雲端 VM 沒有種子時省略 `--source-kb` 即可。

核准後寫入**新的隔離檔**（必須有旗標與 `--db-path`）：

```bash
python -m mapping_kb_import import \
  --golden golden_table.json \
  --source-kb /workspace/_handoff/kb_excel_success_isolated_20260912.db \
  --db-path /workspace/_handoff/mapping_kb_isolated_20260920.db \
  --i-approve-kb-import
```

可選負例 fixture（不讀 live DB）：

```bash
python -m mapping_kb_import import \
  --golden tests/fixtures/mapping_kb/golden_harvest.json \
  --negatives tests/fixtures/mapping_kb/negatives.json \
  --db-path /tmp/mapping_kb_isolated_fixture.db \
  --i-approve-kb-import
```

沒有 `--i-approve-kb-import`、`--db-path` 指向 live `procurement.db`、或輸出等於 Golden／種子檔 → 非零退出且不寫入。

## 分桶（對帳）

| 桶 | 條件 | 寫入 |
|---|---|---|
| `copied` | `1688_mapping_status=approved` 且有 offer + `1688_sku_id` | `kb_mappings` + `kb_skus` |
| `name_positive` | 核准但缺 `sku_id`（或有 id 無 offer） | `kb_name_positives`（`skip_reason=approved_without_sku_id` 或 `approved_without_offer`） |
| `skipped` | 非 approved、或缺／歧義型號身分 | 只進清冊（有 model id 時） |

蝦皮 `shopee_model_id`：優先 `規格ID`；沒有時僅當該商品內 `型號名稱` 唯一才用名稱當臨時 id（與 `product_catalog.find_model_by_identity` 一致）。名稱不唯一 → `ambiguous_model_identity`，不發明 id。

對真實 `golden_table.json` 跑 `dry-run` 可得到本機對帳數字。SPEC 盤點（2026-09-20）約 3800 核准／1330 有 `1688_sku_id`；差額即「核准但無 sku_id」、不得假裝不存在、也不得為了抄進 `kb_mappings` 去發明 id。

## 測試

```bash
python -m unittest tests.test_mapping_kb_import tests.test_purchase_history_store tests.test_purchase_history_import
```

離線、不需營運種子。Golden SHA 與 `auto_approve.enabled` 在測試前後必須不變。

## 階段 2.1：接到 `historical_support`

隔離 KB 是**另一個檔**，不要合併進 live `procurement.db`，也不要把表 ATTACH 進 live。`SkuMappingService` 用獨立唯讀連線讀它。

| 指向方式 | 說明 |
|---|---|
| 建構子 `kb_db_path=` | 單元測試／程式呼叫 |
| 環境變數 `MAPPING_KB_DB` | HTTP／`SkuMappingService()` 預設會讀；空／`off`／缺檔＝今天的行為 |
| `--kb-db` | `python -m mapping_eval run --kb-db …`；`python -m sku_mapping_service historical-contrast --kb-db …`；`python -m offer_discovery suggest --kb-db …` |

營運機常見路徑（不進 git；雲端 VM 可缺）：

```text
/workspace/_handoff/mapping_kb_isolated_20260920.db
```

```bash
export MAPPING_KB_DB=/workspace/_handoff/mapping_kb_isolated_20260920.db
# 或單次：
python -m mapping_eval run --fixture tests/fixtures/mapping_eval \
  --kb-db /workspace/_handoff/mapping_kb_isolated_20260920.db
python -m sku_mapping_service historical-contrast \
  --kb-db /workspace/_handoff/mapping_kb_isolated_20260920.db
python -m offer_discovery suggest --product-id <id> --model-id <規格ID> \
  --kb-db /workspace/_handoff/mapping_kb_isolated_20260920.db
```

缺檔或未設定時 **不中斷**：`historical_support` 仍只吃 Golden（＋同工作 DB 的 `kb_mappings`，缺表就忽略）。

讀什麼：

| 表 | 進 judging 的角色 |
|---|---|
| `kb_mappings`（`source=golden_approved`） | SKU 正例，與既有同 DB `kb_mappings` 相同（`model_name ↔ 1688_sku_name`） |
| `kb_name_positives` | **只當名稱組合支持訊號**。不發明 `sku_id`，不自動升成核准 sku mapping |

### Fixture 對照（空 Golden，有／無 `--kb-db`）

CI 用 `tests/fixtures/mapping_kb/golden_harvest.json` 收成的小型隔離檔（不需營運 66MB DB）：

| 探針 | 無 KB | 有 KB | 多出來的來源 |
|---|---|---|---|
| offer `111111111111`／候選 `石墨黑;17` | 0 | ≥1 | `kb_mappings`（`m-copied`） |
| offer `333333333333`／候選 `卡其;M`（`sku_id` 空） | 0 | ≥1 | `kb_name_positives`（`name_combo_only`）；`sku_id` 仍空 |

```bash
python -m sku_mapping_service historical-contrast
# 未給 --kb-db 時用 fixture 收成暫存檔；不寫 repo Golden／live DB
python -m unittest tests.test_mapping_kb_historical_support tests.test_mapping_kb_import
```
