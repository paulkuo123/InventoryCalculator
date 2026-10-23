# Golden AI-5 審核介面 RESULT（2026-09-09）

## 任務
做給庭安「一次一案」的審核清單／靜態 HTML：主佇列現況 **88** 筆（原 AI-4 有候選 69＋杯套／愛心熊 12＋AI-4 resume3 高／中 7；高／中優先，杯套置頂、resume3 接在其後），附錄 Plan C 已擱高／中 7 筆。審核結果寫獨立檔，不寫 golden。

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
| 主佇列必審 | **88** |
| 高 | **22** |
| 中 | **29** |
| 低 | **37** |
| 附錄 `shelved_round1` | **7**（高 2／中 5） |

主佇列排序：杯套／愛心熊 12 置頂 → resume3 7 → 其餘高 → 中 → 低；預設分頁「優先（高／中）」。低信心可後審。

## 現況更新（resume3 併入後）
- 主佇列必審：**88**＝原 69＋杯套／愛心熊 resume **12**（置頂，`source=ai4_resume`）＋resume3 高／中 **7**（`source=ai4_resume3`，接在杯套後）
- 信心：**高 22／中 29／低 37**（低未動）
- 附錄 `shelved_round1`：**7**（不變）
- 細節見 `golden_ai5_merge_resume3_RESULT_20260909.md`；HTML 已同步

### resume3 7 筆
1. 高 `24077547688` 黑色 應補 80 offer `1039512900763` SKU 冰丝波浪一【黑色】（冰絲安全褲）
2. 高 `24077547688` 白色 應補 40 offer `1039512900763` SKU 冰丝波浪一【白色】
3. 高 `18644662056` 20W快充頭 應補 20 offer `1001283758278`
4. 中 `18644662056` 30W快充頭 應補 10 offer `1001283758278`（功率對應不確定，需對圖）
5. 中 `25811193291` 10W 應補 30 offer `773635969692`（SKU 怪異，必須對圖）
6. 中 `11515936363` 7. 笑臉牛奶鑰匙圈 應補 5 offer `893147198403`
7. 中 `11515936363` 8. 棕色考拉鑰匙圈 應補 5 offer `893147198403`

## 產檔
- UI：`docs/golden_ai5_20260909/golden_ai5_review.html`
- Queue：`docs/golden_ai5_20260909/golden_ai5_queue_20260909.json`
- SUMMARY：`docs/golden_ai5_20260909/golden_ai5_SUMMARY_20260909.md`
- 腳本：`docs/golden_ai5_20260909/_ai5_build_review_ui.py`

## 瀏覽器抽驗
已用本機 `python3 -m http.server 8765` 開 UI：預設優先高／中 51、全部必審 88、高 22／中 29／低 37、附錄 7；點過核准／略過，進度會累加並寫 localStorage。**未**點 1688 購買／加入进货单。

## Docs PR（保持 OPEN，不合 main）
- URL：https://github.com/paulkuo123/InventoryCalculator/pull/46
- Branch：`docs/golden-ai5-review-20260909`
- 路徑：`docs/golden_ai5_20260909/`
- State：**OPEN**（**未 merge**）

## 檢查清單
- Touched golden? **no**
- Merged main? **no**
- Added cart? **no**
- EXIT：`0`
