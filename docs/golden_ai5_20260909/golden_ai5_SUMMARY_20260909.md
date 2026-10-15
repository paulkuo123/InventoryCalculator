# Golden AI-5 審核介面 — 摘要（2026-09-09）

給庭安一次看一案的靜態審核工作台。主佇列 **88**＝原 AI-4 有候選 69 筆＋杯套／愛心熊 resume **12** 筆（`source=ai4_resume`，商品 `19666639659`）＋AI-4 resume3 高／中 **7** 筆（`source=ai4_resume3`）＋Plan C 已擱高／中 7 筆附錄。**沒改 `golden_table.json`、沒合 main、沒加採購車。**

Golden SHA-256（做前＝做後）：`8a95064fbe116f9e6ead24837ced1511ce34a9f3ac69358d9c2e0fb585ab3d2e`

## 佇列

| 區塊 | 筆數 | 說明 |
|---|---:|---|
| 主佇列必審 | **88** | 原 69 + 杯套／愛心熊 12 + resume3 7；高 22／中 29／低 37 |
| 其中 `ai4_resume` | **12** | 杯套／愛心熊（`19666639659`）高／中有候選，置頂 |
| 其中 `ai4_resume3` | **7** | 冰絲安全褲／快充頭／轉接器／鑰匙圈，插在杯套後面 |
| 附錄 `shelved_round1` | **7** | Plan C 已擱高 2／中 5，**不是**本批必審 |
| 未進主佇列 | — | AI-4 `blocked_captcha`、`no_candidate` 都沒塞進來 |

排序：**杯套 12 置頂 → resume3 7（高→中、應補量大的在前）→ 其餘高 → 中 → 低**。低信心預設不在第一屏（切「低」分頁才看）。預設分頁是「優先（高／中）」，會跳到第一筆還沒審的。新 7 筆已併進高／中區，沒另外開佇列。

主佇列裡有 2 筆 AI-4 標過 `shelved_round1`（貼貼奶狗、發芽碳球），仍算在主佇列裡；畫面上有「已擱 round1」標籤，庭安可略過。完整 7 筆已擱另放附錄分頁。

## resume3 7 筆（source=ai4_resume3）

| 信心 | product_id | 型號 | 應補 | offer | 備註 |
|---|---|---|---:|---|---|
| 高 | 24077547688 | 黑色 | 80 | 1039512900763 | SKU 冰丝波浪一【黑色】；冰絲安全褲 |
| 高 | 24077547688 | 白色 | 40 | 1039512900763 | SKU 冰丝波浪一【白色】 |
| 高 | 18644662056 | 20W快充頭 | 20 | 1001283758278 | |
| 中 | 18644662056 | 30W快充頭 | 10 | 1001283758278 | 功率對應不確定，需對圖 |
| 中 | 25811193291 | 10W | 30 | 773635969692 | SKU 怪異，必須對圖 |
| 中 | 11515936363 | 7. 笑臉牛奶鑰匙圈 | 5 | 893147198403 | |
| 中 | 11515936363 | 8. 棕色考拉鑰匙圈 | 5 | 893147198403 | |

Offer URL：`https://detail.1688.com/offer/{id}.html`

## 怎麼開

建議用本機靜態伺服（避免少數瀏覽器擋 `file://` 圖）：

```bash
cd /workspace/InventoryCalculator/docs/golden_ai5_20260909
python3 -m http.server 8765
```

瀏覽器開：<http://127.0.0.1:8765/golden_ai5_review.html>

或直接開檔：

- repo：`docs/golden_ai5_20260909/golden_ai5_review.html`
- 備份：`/workspace/_handoff/golden_ai5_review.html`

## 畫面上有什麼

- 左：蝦皮圖／標題／型號／應補量
- 右：1688 連結、SKU 名、規格、信心、理由；有 `alt_offer_id` 會秀出來給改換
- 動作：核准／駁回／改換／略過／停售（快捷鍵 1–5）
- 上一筆／下一筆、跳號、側欄清單
- 分頁：優先（高／中）｜全部必審｜高｜中｜低｜附錄 shelved_round1
- 進度：主佇列已審 N／88，附錄另計 N／7

審核結果只寫瀏覽器 `localStorage`，再下載 CSV／JSON。**不會寫 golden。** 磁碟上空模板：

- `/workspace/_handoff/golden_ai5_decisions_template_20260909.csv`
- `docs/golden_ai5_20260909/golden_ai5_decisions_template_20260909.csv`

欄位：`decision_id,reviewed_at,queue,product_id,spec_id,model_name,confidence,candidate_offer_id,candidate_sku_id,action,swap_offer_id,swap_sku_id,note,shelved_round1`

## 產檔

- UI：`docs/golden_ai5_20260909/golden_ai5_review.html`
- 佇列 JSON：`docs/golden_ai5_20260909/golden_ai5_queue_20260909.json`
- 建置腳本：`docs/golden_ai5_20260909/_ai5_build_review_ui.py`

## 建議庭安怎麼審

1. 先把「優先（高／中）」51 筆對圖（含杯套／愛心熊 12、resume3 7，以及後背包白應補 400、清潔筆、軍規／蝴蝶結殼）。
2. 低信心 37 筆當備選，不要當補貨主力。
3. 附錄 7 筆維持已擱，想回看再切分頁。
4. 下載 CSV 交給後續寫回；**這刀不要動 golden。**

## 2026-09-09 併入杯套排序（steering）
杯套／愛心熊 `source=ai4_resume` **12** 筆已排在主佇列 **最前面**（其內仍高→中、應補量大的在前），方便庭安先審。

## 2026-09-09 併入 resume3 7 筆
AI-4 resume3 高／中 **7** 筆（`source=ai4_resume3`）插在杯套 12 筆後面（index 13–19），其內仍高→中、應補量大的在前。主佇列 **88**（高22／中29／低37）。審核決策與 golden 分開。
