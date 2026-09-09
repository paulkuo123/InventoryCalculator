# Golden AI-5 審核介面 RESULT（2026-09-09）

## 任務
做給庭安「一次一案」的審核清單／靜態 HTML：主佇列 AI-4 有候選 69 筆（高／中優先），附錄 Plan C 已擱高／中 7 筆。審核結果寫獨立檔，不寫 golden。

## 約束（皆遵守）
- **未** merge main
- **未** 修改 `golden_table.json`
- **未** 加車／改購物車／點購買
- **未** 把 AI-4 `blocked_captcha`／`no_candidate` 塞進必審主佇列

## Golden 未動
- 路徑：`golden_table.json`
- SHA-256 做前：`8a95064fbe116f9e6ead24837ced1511ce34a9f3ac69358d9c2e0fb585ab3d2e`
- SHA-256 做後：`8a95064fbe116f9e6ead24837ced1511ce34a9f3ac69358d9c2e0fb585ab3d2e`
- `git diff main -- golden_table.json` 空

## 怎麼開 UI
- 路徑（repo）：`docs/golden_ai5_20260909/golden_ai5_review.html`
- 路徑（handoff 備份）：`/workspace/_handoff/golden_ai5_review.html`
- 建議：

```bash
cd /workspace/InventoryCalculator/docs/golden_ai5_20260909
python3 -m http.server 8765
```

開 <http://127.0.0.1:8765/golden_ai5_review.html>

也可直接用 `file://` 開同一個 HTML（資料已內嵌，不需另抓 JSON）。

## 審核結果模板（空檔，僅表頭）
- `/workspace/_handoff/golden_ai5_decisions_template_20260909.csv`
- `docs/golden_ai5_20260909/golden_ai5_decisions_template_20260909.csv`

瀏覽器可另下載填過的 CSV／JSON；**不要**把結果寫進 golden。

## 計數

| | n |
|---|---:|
| 主佇列必審 | **69** |
| 高 | **14** |
| 中 | **18** |
| 低 | **37** |
| 附錄 `shelved_round1` | **7**（高 2／中 5） |

主佇列排序：高 → 中 → 低；預設分頁「優先（高／中）」。低信心可後審。

## 產檔
- UI：`docs/golden_ai5_20260909/golden_ai5_review.html`
- Queue：`docs/golden_ai5_20260909/golden_ai5_queue_20260909.json`
- SUMMARY：`/workspace/_handoff/golden_ai5_SUMMARY_20260909.md`
- 腳本：`/workspace/_handoff/_ai5_build_review_ui.py`

## Docs PR（保持 OPEN，不合 main）
- URL：（push 後填）
- Branch：`docs/golden-ai5-review-20260909`
- Commit：（commit 後填）
- 路徑：`docs/golden_ai5_20260909/`
- State：**OPEN**（**未 merge**）

## 檢查清單
- Touched golden? **no**
- Merged main? **no**
- Added cart? **no**
- EXIT：0（PR 開好後）
