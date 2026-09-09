# Golden AI-4 續跑 #4 RESULT（resume4，跳過保護貼＋鏡子，2026-09-09）

## 任務
庭安確認桌面活頁（非 captcha／punish）後，只重跑仍為 `blocked_captcha` 的列。**整商品跳過** 保護貼 `9969182845` 與鏡子 `9791809096`。slow nav ≥4s。再撞 captcha → STOP。

## 約束（皆遵守）
- **未** merge main
- **未** 修改 `golden_table.json`
- **未** 加車／改購物車／點購買
- 驗證碼：**STOP**，未自幹繞過；resume 已更新
- 原有 `with_candidate`／`no_candidate` **未改寫**（只動原本 blocked 列）
- 保護貼 9 列、鏡子 5 列仍為 `blocked_captcha`（未強搜）

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
- 執行：本機 CDP 腳本 `_ai4_cdp_resume.py`（resume4／SKIP_PIDS 保護貼＋鏡子；模型路徑 cursor-grok-4.6-high 備註）

## resume4 計數差

| | 續跑前 | 續跑後 | 差 |
|---|---:|---:|---:|
| with_candidate | 100 | **101** | **+1** |
| no_candidate | 51 | **55** | **+4** |
| blocked_captcha | 102 | **97** | **-5** |
| 高 | 22 | **22** | 0 |
| 中 | 29 | **29** | 0 |
| 低 | 49 | **50** | **+1** |
| 合計 | 253 | 253 | 0 |

CSV 主檔：`/workspace/_handoff/golden_ai4_candidates_20260909.csv`  
快照：`/workspace/_handoff/golden_ai4_candidates_resume4_20260909.csv`  
續跑前備份：`/workspace/_handoff/golden_ai4_candidates_20260909_pre_resume4.csv`

## 本輪發生什麼
1. 連上 CDP，確認活頁非 punish；佇列 **41 商品／88 列**（已排除保護貼 9＋鏡子 5 列）。
2. 成功處理：
   - `15323119538` 卡通泰迪狗 Airpods 殼 → 3 列 no_candidate（有搜到殼但款名／SKU 對不上）
   - `22272486965` 刺繡小熊化妝包 → 1 列 no_candidate
   - `23244459752` 帆布袋剩餘 3 blocked 型號 → 空搜頁（未見「没有找到」）→ **仍標 blocked**（未強行改 no）
   - `24514999862` 多國旅行充電器 → **1 低** with_candidate（顏色對上，offer `998206849175`）
3. 下一檔奶咖殼 `15007585064` 3B 搜「iphone 手机壳 保护壳软壳 奶咖色 11」撞 `_____tmd_____/punish` → **整批 STOP**（navs≈11）。
4. 保護貼 `9969182845`、鏡子 `9791809096` **全程跳過**，14 列仍 blocked。

## 是否碰驗證碼
**有（resume4 又撞）。** `captcha_stopped=yes`／`CAPTCHA_AGAIN=yes`

- URL：`https://s.1688.com/selloffer/offer_search.htm/_____tmd_____/punish`
- 商品：`15007585064` iPhone 奶咖色手機殼（型號列 奶咖色,11）
- query：`iphone 手机壳 保护壳软壳 奶咖色 11`
- navs：11
- resume：`/workspace/_handoff/golden_ai4_resume_20260909.json`

## 可併進 AI-5（本輪新高／中：0）

本輪 **無** 新高／中。僅 1 筆新低信心：

| 信心 | product_id | 型號 | offer | SKU 摘要 |
|---|---|---|---|---|
| 低 | 24514999862 | 白色充電器 | 998206849175 | 多國旅行充／顏色對上（備選，不強制 AI-5） |

既有 resume1–3 高／中仍建議進 AI-5。總 with_candidate **101**（高 22／中 29／低 50）。

## 產檔
- CSV／SUMMARY／本 RESULT／resume JSON／`_ai4_cdp_resume4.log`
- 腳本：`_ai4_cdp_resume.py`（resume4 SKIP 保護貼＋鏡子＋slow_nav）

## Docs PR（保持 OPEN，不合 main）
- 追加 PR #45：https://github.com/paulkuo123/InventoryCalculator/pull/45
- Branch：`docs/golden-ai4-candidates-20260909`
- State：**OPEN**（**未 merge**）

## 下一刀建議
1. 庭安再清 1688 驗證碼後可 resume5；繼續跳過保護貼＋鏡子，從奶咖殼 `15007585064` 起，或先歇。
2. 本輪無新高／中；充電器低信心可當備選。
3. **先不要寫 golden**。
4. blocked 97／40 商品（含保護貼 9＋鏡子 5 列）。

## EXIT

```
WITH_CANDIDATE=101
STILL_BLOCKED=97
BLOCKED_DELTA=-5
AI5_NEW_HIGH_MID=0
CAPTCHA_AGAIN=yes
SKIP_FILM=9969182845
SKIP_MIRROR=9791809096
PR=https://github.com/paulkuo123/InventoryCalculator/pull/45
SHA=8241f741a3bb9c1433bd090a833446d34448026b
GOLDEN_SHA=8a95064fbe116f9e6ead24837ced1511ce34a9f3ac69358d9c2e0fb585ab3d2e
EXIT=2
```
