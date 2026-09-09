# Golden AI-4 續跑 RESULT（resume 後，2026-09-09）

## 任務
庭安交還遠端桌面後，只重跑原本 `blocked_captcha` 列。優先：杯套／愛心熊 → 充電線兩檔 → 安全褲 → 其餘 blocked。主路徑 3B；3c 備援。再撞 captcha → STOP，不繞過。

## 約束（皆遵守）
- **未** merge main
- **未** 修改 `golden_table.json`
- **未** 加車／改購物車／點購買
- 驗證碼：**STOP**，未自幹繞過；resume 已更新
- 原有 `with_candidate` 69／`no_candidate` 26 **未改寫**（只動原本 blocked 列）

## Golden 未動
- 路徑：`golden_table.json`
- SHA-256 跑前：`8a95064fbe116f9e6ead24837ced1511ce34a9f3ac69358d9c2e0fb585ab3d2e`
- SHA-256 跑後：`8a95064fbe116f9e6ead24837ced1511ce34a9f3ac69358d9c2e0fb585ab3d2e`
- `git diff main -- golden_table.json` 空

## 瀏覽器
- CDP：`http://127.0.0.1:9232`（chrome-profile-10）
- 起始活頁：`https://detail.1688.com/offer/740525848630.html`（非 punish）
- 結束活頁：同上（已從 punish 導回，未留在驗證碼頁）
- 搜尋頁改版：結果卡用 `offerId=`／`detail.m.1688.com`，舊 `/offer/` 擷取會回 0；已修 `_planC_cdp_lib.py` SEARCH_EXTRACT_JS

## resume 後計數差

| | 續跑前 | 續跑後 | 差 |
|---|---:|---:|---:|
| with_candidate | 69 | **89** | **+20** |
| no_candidate | 26 | **44** | **+18** |
| blocked_captcha | 158 | **120** | **-38** |
| 高 | 14 | **19** | **+5** |
| 中 | 18 | **25** | **+7** |
| 低 | 37 | **45** | **+8** |
| 合計 | 253 | 253 | 0 |

CSV 主檔已覆寫：`/workspace/_handoff/golden_ai4_candidates_20260909.csv`  
另存快照：`/workspace/_handoff/golden_ai4_candidates_resume_20260909.csv`（與主檔相同）  
續跑前備份：`/workspace/_handoff/golden_ai4_candidates_20260909_pre_resume.csv`

## 優先佇列結果

### 1. 杯套／愛心熊 `19666639659`（12 列／應補 190；含愛心熊應補 60）— **完成**
- 原死檔 `644276542345` 3c 仍無店；3B「杯套 爱心熊」命中同款 offer `977900422023`（龍港市圓碼包裝廠）
- **12／12 with_candidate**（高 5／中 7）
- 愛心熊 SKU `爱心熊`／`5933116172304`，信心 **中**（款名＋SKU 字面命中）
- 可進 AI-5：**12 筆（皆 高／中）**

### 2. 充電線 P1
- `19651077286`：2 with_candidate（皆 **低**，只對上長度）＋ 6 no_candidate
- `22561129943`：6 with_candidate（皆 **低**）；其中 200 公分對到 `0.2m` 明顯不穩，**不要當補貨主力**
- 可進 AI-5：建議 **0 筆高／中**；8 筆低僅供備查／可駁回

### 3. 安全褲 `24077547688`（應補 120）— **仍 blocked**
- 3B「冰丝安全裤」回 0 offer（非 punish 空頁），保留 `blocked_captcha` 待下一輪
- 未標 no_candidate，避免誤殺

### 4. 其餘 blocked
- 日系字母內褲 `25592099386`（12 列／應補 440）：有搜到檔但 SKU 對不上 → `no_candidate`
- 下一檔保護貼 `9969182845` 全站搜 **punish** → **整批 STOP**
- 其餘 48 商品／120 列仍 `blocked_captcha`

## 是否碰驗證碼
**有（resume 後又撞）。** `captcha_stopped=yes`／`CAPTCHA_AGAIN=yes`

- URL：`https://s.1688.com/selloffer/offer_search.htm/_____tmd_____/punish`
- 商品：`9969182845` iPhone 保護貼
- 其前完成：杯套、充電線兩檔、安全褲空頁、字母內褲
- 未自幹繞過；分頁已導回 `740525848630`
- resume：`/workspace/_handoff/golden_ai4_resume_20260909.json`

## 可併進 AI-5
- **新高／中：12 筆**（全是杯套／愛心熊那檔）→ 建議進 AI-5 對圖
- 新低：8 筆充電線 → **不建議當主佇列**（線種／長度不穩）
- 既有 69 筆候選仍在；總 with_candidate 現 **89**（高 19／中 25／低 45）

## 產檔
- CSV：`/workspace/_handoff/golden_ai4_candidates_20260909.csv`
- SUMMARY：`/workspace/_handoff/golden_ai4_SUMMARY_20260909.md`
- 本 RESULT：`/workspace/_handoff/golden-ai4-resume-RESULT-20260909.md`
- resume：`/workspace/_handoff/golden_ai4_resume_20260909.json`
- 腳本：`_ai4_cdp_resume.py`（重用 `_ai4_cdp_run.py`／`_ai4_match.py`／`_planC_cdp_lib.py`）

## Docs PR（保持 OPEN，不合 main）
- 追加 PR #45：https://github.com/paulkuo123/InventoryCalculator/pull/45
- Branch：`docs/golden-ai4-candidates-20260909`
- State：**OPEN**（**未 merge**）

## 下一刀建議
1. 庭安再點一次 1688 驗證碼後，從 resume 續：**安全褲** → Lightning 充電線 `18344672033` → 保護貼 `9969182845` → 其餘 P1 blocked。
2. 杯套 12 筆高／中可進 AI-5 對圖；**先不要寫 golden**。
3. 充電線低信心不要當補貨主力；要準請另搜「Type-A to C／C to C」分開關鍵字。
4. 低信心與空搜 no_candidate 不要當主線。
