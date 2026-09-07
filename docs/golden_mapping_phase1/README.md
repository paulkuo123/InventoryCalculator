# Golden Table Mapping Phase 1（2026-09-07）

集中鑑定工作台與寫入閘。**未批次修改** `golden_table.json` 正式內容。

| 檔案 | 內容 |
|---|---|
| [locked_paths.md](locked_paths.md) | 已鎖定的舊寫入路徑（410／旗標） |

執行測試：

```bash
python3 -m unittest tests.test_mapping_procurement_gate tests.test_golden_mapping_phase1_locked_paths tests.test_reverse_audit tests.test_sku_mapping_service -q
```
