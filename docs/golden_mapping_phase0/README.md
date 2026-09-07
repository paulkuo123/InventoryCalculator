# Golden Table Mapping Phase 0（2026-09-07）

唯讀盤點與基準。**未修改** `golden_table.json` 正式內容。

## 產物

| 檔案 | 內容 |
|---|---|
| [schema.md](schema.md) | 連結／來源／SKU 三層狀態 |
| [workbench_inventory.md](workbench_inventory.md) | 現有工作台複用分類 |
| [write_path_risk.md](write_path_risk.md) | 正式寫入路徑與風險 |
| [procurement_image_search.md](procurement_image_search.md) | 歷史採購／圖搜依賴 |
| [representative_cases.md](representative_cases.md) | 約 20 個代表案例 |
| [full_table_pass_20260907.csv](full_table_pass_20260907.csv) | 5918 型號全量分類 |
| [full_table_counts_20260907.json](full_table_counts_20260907.json) | 分桶計數 |
| `golden_mapping_phase0.py` | 唯讀分類函式庫 |
| `scripts/golden_mapping_phase0_inventory.py` | 產出 CSV／JSON；結束前核對 golden SHA-256 |

重跑（不寫 golden）：

```bash
python3 scripts/golden_mapping_phase0_inventory.py
python3 -m unittest tests.test_golden_mapping_phase0_inventory -q
```

## 基準數字（本機 main @ 本 PR）

- 商品 355／型號 5918
- golden SHA-256：`8a95064fbe116f9e6ead24837ced1511ce34a9f3ac69358d9c2e0fb585ab3d2e`
- raw `approved` 3800，但 **0** 筆視為最終鑑定
- 主問題：LINK 959、SOURCE 1437、SKU 2520、EXISTING_APPROVAL 1002
