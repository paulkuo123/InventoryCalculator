# Golden AI-4 續跑 #3 RESULT（resume3，跳過保護貼，2026-09-09）

## 任務
庭安交還桌面（活頁 `740525848630`，非 punish）後，只重跑仍為 `blocked_captcha` 的列。**跳過** 保護貼 `9969182845`（及同商品所有型號列）。建議序：其餘 P1 → AI2_dead／off → P3。slow nav ≥3–5s。再撞 captcha → STOP。

## 約束（皆遵守）
- **未** merge main
- **未** 修改 `golden_table.json`
- **未** 加車／改購物車／點購買
- 驗證碼：**STOP**，未自幹繞過；resume 已更新
- 原有 `with_candidate`／`no_candidate` **未改寫**（只動原本 blocked 列）
- 保護貼 9 列仍為 `blocked_captcha`（未強搜）

## Golden 未動
- 路徑：`golden_table.json`
- SHA-256 跑前：`8a95064fbe116f9e6ead24837ced1511ce34a9f3ac69358d9c2e0fb585ab3d2e`
- SHA-256 跑後：`8a95064fbe116f9e6ead24837ced1511ce34a9f3ac69358d9c2e0fb585ab3d2e`
- `git diff` 對 golden 空

## 瀏覽器
- CDP：`http://127.0.0.1:9232`（chrome-profile-10）
- 起始活頁：`https://detail.1688.com/offer/740525848630.html`（非 punish）
- 結束活頁：同上（已從 punish 導回）
- slow_nav：settle ≥4.2s、post_sleep 3.5s
- 執行：本機 CDP 腳本 `_ai4_cdp_resume.py`（resume3／SKIP_PIDS；模型路徑 cursor-grok-4.6-high 備註）

## resume3 計數差

| | 續跑前 | 續跑後 | 差 |
|---|---:|---:|---:|
| with_candidate | 89 | **100** | **+11** |
| no_candidate | 44 | **51** | **+7** |
| blocked_captcha | 120 | **102** | **-18** |
| 高 | 19 | **22** | **+3** |
| 中 | 25 | **29** | **+4** |
| 低 | 45 | **49** | **+4** |
| 合計 | 253 | 253 | 0 |

CSV 主檔：`/workspace/_handoff/golden_ai4_candidates_20260909.csv`  
快照：`/workspace/_handoff/golden_ai4_candidates_resume3_20260909.csv`  
續跑前備份：`/workspace/_handoff/golden_ai4_candidates_20260909_pre_resume3.csv`

## 本輪發生什麼
1. 連上 CDP，確認活頁非 punish；佇列 **47 商品／111 列**（已排除保護貼 9 列）。
2. 成功處理：`18344672033`（充電線→no）、`24077547688`（冰絲安全褲→2 高）、`25811193291`（轉接→1 中）、`18644662056`（快充頭→1 高 1 中）、`11515936363`（吊飾→2 中 4 低／其餘 no）、`23244459752`（帆布袋→空搜 no）。
3. 下一檔鏡子 `9791809096` 3B 搜「镜子化妆镜…」撞 `_____tmd_____/punish` → **整批 STOP**（navs≈20）。
4. 保護貼 `9969182845` **全程跳過**，9 列仍 blocked。

## 是否碰驗證碼
**有（resume3 又撞）。** `captcha_stopped=yes`／`CAPTCHA_AGAIN=yes`

- URL：`https://s.1688.com/selloffer/offer_search.htm/_____tmd_____/punish`
- 商品：`9791809096` ins風 韓版 鏡子
- query：`镜子化妆镜折叠镜随身镜简约可爱女学生 小镜子 2 波点咖啡熊`
- navs：20
- resume：`/workspace/_handoff/golden_ai4_resume_20260909.json`

## 可併進 AI-5（本輪新高／中：7）

| 信心 | product_id | 型號 | offer | SKU 摘要 |
|---|---|---|---|---|
| 高 | 24077547688 | 黑色 | 1039512900763 | 冰丝波浪一【黑色】 |
| 高 | 24077547688 | 白色 | 1039512900763 | 冰丝波浪一【白色】 |
| 高 | 18644662056 | 20W快充頭 | 1001283758278 | 20W快充头+PD线 |
| 中 | 18644662056 | 30W快充頭 | 1001283758278 | （同 offer，功率未 100% 對） |
| 中 | 25811193291 | 10W | 773635969692 | ⚠ 轉接器關鍵字對上但 SKU 名怪異，務必對圖 |
| 中 | 11515936363 | 7. 笑臉牛奶鑰匙圈 | 893147198403 | 款式7 |
| 中 | 11515936363 | 8. 棕色考拉鑰匙圈 | 893147198403 | 款式8 |

另有低信心 4 筆吊飾（同 offer 893147198403）可當備選，不強制進 AI-5。

既有杯套／愛心熊等高／中（resume1）仍建議進 AI-5。總 with_candidate **100**（高 22／中 29／低 49）。

## 產檔
- CSV／SUMMARY／本 RESULT／resume JSON／`_ai4_cdp_resume3.log`
- 腳本：`_ai4_cdp_resume.py`（resume3 SKIP 保護貼＋slow_nav）

## Docs PR（保持 OPEN，不合 main）
- 追加 PR #45：https://github.com/paulkuo123/InventoryCalculator/pull/45
- Branch：`docs/golden-ai4-candidates-20260909`
- Commit：`5d5c346b27a7339126330047483054d120772b1c`
- State：**OPEN**（**未 merge**）

## 下一刀建議
1. 庭安再清 1688 驗證碼後可 resume4；建議繼續跳過保護貼，從鏡子 `9791809096` 起，或先歇一段時間。
2. 本輪 7 筆新高／中可併 AI-5 對圖（轉接器那筆特別可疑）。
3. **先不要寫 golden**。
4. blocked 102／43 商品（含保護貼 9 列）。

## EXIT

```
WITH_CANDIDATE=100
STILL_BLOCKED=102
BLOCKED_DELTA=-18
AI5_NEW_HIGH_MID=7
CAPTCHA_AGAIN=yes
SKIP_FILM=9969182845
PR=https://github.com/paulkuo123/InventoryCalculator/pull/45
SHA=5d5c346b27a7339126330047483054d120772b1c
GOLDEN_SHA=8a95064fbe116f9e6ead24837ced1511ce34a9f3ac69358d9c2e0fb585ab3d2e
EXIT=2
```
