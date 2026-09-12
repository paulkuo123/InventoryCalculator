# Golden AI-4 續跑 #2 RESULT（resume2，2026-09-09）

## 任務
庭安第二次交還桌面（活頁 `740525848630`）後，只重跑仍為 `blocked_captcha` 的列。建議序：保護貼 `9969182845` → 其餘 P1 → AI2_dead／off → P3。slow nav ≥3–5s。再撞 captcha → STOP。

## 約束（皆遵守）
- **未** merge main
- **未** 修改 `golden_table.json`
- **未** 加車／改購物車／點購買
- 驗證碼：**STOP**，未自幹繞過；resume 已更新
- 原有 `with_candidate` 89／`no_candidate` 44 **未改寫**（只動原本 blocked 列；本輪實際無狀態變更）

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

## resume2 計數差

| | 續跑前 | 續跑後 | 差 |
|---|---:|---:|---:|
| with_candidate | 89 | **89** | **0** |
| no_candidate | 44 | **44** | **0** |
| blocked_captcha | 120 | **120** | **0** |
| 高 | 19 | **19** | **0** |
| 中 | 25 | **25** | **0** |
| 低 | 45 | **45** | **0** |
| 合計 | 253 | 253 | 0 |

CSV 主檔：`/workspace/_handoff/golden_ai4_candidates_20260909.csv`  
快照：`/workspace/_handoff/golden_ai4_candidates_resume2_20260909.csv`  
續跑前備份：`/workspace/_handoff/golden_ai4_candidates_20260909_pre_resume2.csv`

## 本輪發生什麼
1. 連上 CDP，確認活頁非 punish。
2. 佇列 48 商品／120 列 blocked；優先從保護貼 `9969182845`（9 列／應補 170）重試。
3. 3B 搜「iphone 钢化膜 防窥膜」**第 1 次導航**即進 `_____tmd_____/punish`。
4. 依規則 **整批 STOP**；未嘗試繞過；分頁導回活頁 `740525848630`。
5. `processed_pids=[]`；無新候選、無 no_candidate 新增。

## 是否碰驗證碼
**有（resume2 又撞）。** `captcha_stopped=yes`／`CAPTCHA_AGAIN=yes`

- URL：`https://s.1688.com/selloffer/offer_search.htm/_____tmd_____/punish`
- 商品：`9969182845` iPhone 保護貼
- query：`iphone 钢化膜 防窥膜`
- navs：1
- resume：`/workspace/_handoff/golden_ai4_resume_20260909.json`

## 可併進 AI-5
- **本輪新高／中：0 筆**（沒跑出任何新候選）
- 既有可併：杯套／愛心熊 12 筆（resume1）仍建議進 AI-5；總 with_candidate 仍 **89**（高 19／中 25／低 45）

## 產檔
- CSV／SUMMARY／本 RESULT／resume JSON／`_ai4_cdp_resume2.log`
- 腳本：`_ai4_cdp_resume.py`（resume2 優先序＋slow_nav）

## Docs PR（保持 OPEN，不合 main）
- 追加 PR #45：https://github.com/paulkuo123/InventoryCalculator/pull/45
- Branch：`docs/golden-ai4-candidates-20260909`
- Commit：`40ef651ff377e3d29ea5b88fa4e1f54d13619af8`
- State：**OPEN**（**未 merge**）

## 下一刀建議
1. 庭安需再清一次 1688 驗證碼；清完後若立刻再搜仍 punish，建議**先歇一段時間／換關鍵字策略**（例如先走有 old_offer 的 3c，或略過保護貼先試安全褲／充電線）。
2. 本輪證明「剛清完 captcha、slow nav 仍可能第一槍就擋」——站点風控偏緊。
3. 杯套 12 筆高／中可進 AI-5；**先不要寫 golden**。
4. blocked 仍 120／48 商品，無進度屬預期（STOP 規則優先）。

## EXIT

```
WITH_CANDIDATE=89
STILL_BLOCKED=120
BLOCKED_DELTA=0
AI5_NEW_HIGH_MID=0
CAPTCHA_AGAIN=yes
PR=https://github.com/paulkuo123/InventoryCalculator/pull/45
SHA=40ef651ff377e3d29ea5b88fa4e1f54d13619af8
GOLDEN_SHA=8a95064fbe116f9e6ead24837ced1511ce34a9f3ac69358d9c2e0fb585ab3d2e
EXIT=2
```
