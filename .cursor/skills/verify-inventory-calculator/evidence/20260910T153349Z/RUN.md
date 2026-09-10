# Proof run 20260910T153349Z

Second end-to-end pass after doctor stopped calling `GET /api/sku-mapping/summary`.

```bash
.cursor/skills/verify-inventory-calculator/scripts/control-inventory launch
.cursor/skills/verify-inventory-calculator/scripts/control-inventory doctor
.cursor/skills/verify-inventory-calculator/scripts/control-inventory drive inventory-dashboard
.cursor/skills/verify-inventory-calculator/scripts/control-inventory http GET /api/sku-mapping/summary  # refused
.cursor/skills/verify-inventory-calculator/scripts/control-inventory cleanup
```

| Check | Result |
|---|---|
| Launch | pid 3321 at `http://127.0.0.1:8080` |
| Doctor | `"healthy": true`; `goldenTable.unchanged: true` (sha256 `8a95064fbe116f9e6ead24837ced1511ce34a9f3ac69358d9c2e0fb585ab3d2e`) |
| Drive | empty inventory dashboard; `#overallStatusText` 尚未分析 |
| Refused GET | `http GET /api/sku-mapping/summary` exited with the golden-write guard |
| Cleanup | pid gone; port 8080 free; this directory kept |
| `golden_table.json` | SHA-256 identical before launch and after cleanup |

Did not click live Shopee/1688 mutation controls. No secrets in these files.
