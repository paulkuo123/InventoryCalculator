# Golden AI-4 候選備料 RESULT（2026-09-09）

## 任務
擴大 C：依優先佇列幫缺連結／死連／mapping 可疑＋應補的型號列找 1688 候選（一型號一個建議，可附 `alt_offer_id`）。主路徑 3B 已登入 CDP；3c 同店／兄弟／Plan C 先於全站搜。

## 約束（皆遵守）
- **未** merge main
- **未** 修改 `golden_table.json`
- **未** 加車／改購物車／點購買
- 驗證碼：**STOP**，未自幹繞過；resume 已寫
- 未把 Plan C 已擱高／中 7 筆當本批必審（範圍內 2 筆標 `shelved_round1`）

## Golden 未動
- 路徑：`golden_table.json`
- SHA-256 跑前：`8a95064fbe116f9e6ead24837ced1511ce34a9f3ac69358d9c2e0fb585ab3d2e`
- SHA-256 跑後：`8a95064fbe116f9e6ead24837ced1511ce34a9f3ac69358d9c2e0fb585ab3d2e`
- `git diff main -- golden_table.json` 空

## 瀏覽器
- CDP：`http://127.0.0.1:9232`（chrome-profile-10，DISPLAY :10）
- Browser：Chrome/151.0.7922.169
- 重用已開 1688 分頁；未新開未登入 Chrome
- 導航間隔約 1.7–3.2s；約 104 次導航後於全站搜撞 punish

## 範圍完成度

| 佇列 | 範圍 | 3c | 3B | 結果 |
|---|---|---|---|---|
| (1) P1_no_url | 103 列 | 做完（多數無兄弟檔） | 充電線兩檔空頁後、安全褲 captcha | 有候選 9／找不到 10／卡驗證碼 84 |
| (2) AI2 off_shelf＋dead | 7+83=90 | 做完 | 未搜（已 STOP） | off：2／1／4；dead：9／14／60 |
| (3) P3 mapping_suspect | 前 **60**（剩餘 **303** 不跑） | 做完多數原檔對 SKU | 10 筆本要搜 → 跟著 blocked | 有候選 49／找不到 1／卡驗證碼 10 |

時間／驗證碼壓力下：(1)(2) 的 **3c 有做完**；(1) 高應補充電線仍待 captcha 後 3B。P3 本批完成 **50 筆有結論（49+1）／剩餘 10 卡驗證碼**；範圍外 P3 還有 303。

口紅化妝包 offer `734419757382` 應補 90：仍 `no_candidate`（下架＋滕尧店列表近空）。

## 計數（交付 CSV）

| | n |
|---|---:|
| 高 | **14** |
| 中 | **18** |
| 低 | **37** |
| with_candidate | **69** |
| no_candidate | **26** |
| blocked_captcha | **158** |
| 合計 | **253** |

## 是否碰驗證碼
**有。** `captcha_stopped=yes`

- URL：`https://s.1688.com/selloffer/offer_search.htm/_____tmd_____/punish`
- 關鍵字：`冰丝无痕波浪安全裤睡裤平口裤打底裤宽松防走光不卷边内搭裤短裤内裤夏日冰感舒适 黑`
- 商品：`24077547688`
- 其前：`19651077286`、`22561129943` 充電線搜尋 0 offer
- resume：`/workspace/_handoff/golden_ai4_resume_20260909.json`
- 分頁已導回活頁 `740525848630`

## 產檔
- CSV：`/workspace/_handoff/golden_ai4_candidates_20260909.csv`
- SUMMARY：`/workspace/_handoff/golden_ai4_SUMMARY_20260909.md`
- 腳本：`/workspace/_handoff/_ai4_cdp_run.py`、`_ai4_match.py`、`_ai4_index_scope.py`
- Docs 目錄：`docs/golden_ai4_20260909/`

## Docs PR（保持 OPEN，不合 main）
- URL：（開 PR 後填）
- Branch：`docs/golden-ai4-candidates-20260909`
- State：**OPEN（未 merge）**

## 下一刀建議
1. **庭安點完 1688 驗證碼** 後，從 resume 續 3B：先充電線（應補 520／150）、安全褲、再 P1 其餘 blocked。
2. P3 高／中（後背包白 400、清潔筆、軍規殼、蝴蝶結殼）可進 AI-5 對圖；**先不要寫 golden**。
3. 已擱 Plan C 高／中 7 筆維持擱。
4. P3 剩餘 303 下一輪； captcha 解掉再決定要不要搜。
5. 低信心不要當補貨主力。
