# 廣告分析規則

本文件對齊 `ads_analysis.py` 的 `_classify_product`、`_analyze_weekly_trends` 與 `_derive_signals`；
門檻數字以程式為準，改程式時請同步更新這裡。判準總覽與寫法要求見
[`skills/ads-analysis/SKILL.md`](../skills/ads-analysis/SKILL.md)。

## 共同基準

- 店內及格線：`ROAS >= 3` 且 `直接 ROAS >= 3`（單一門檻，沒有另一組 2.5 / 1.5）。
- 主要決策視窗是「昨天」；`week_01`（最近一週）與「過去一個月」用來驗證穩定性。
- 趨勢層是 `week_01` ~ `week_04` 四個滾動 7 天視窗（只計花費 > 0 的週）：
  - `stable_strong_weeks`：ROAS >= 3 且直接 ROAS >= 3 的週數
  - `weak_weeks`：ROAS < 3 或直接 ROAS < 3 的週數
  - `indirect_dependency_weeks`：ROAS >= 3 但直接 ROAS < 3 的週數
  - `roas_decline` / `direct_roas_decline` / `ctr_decline`：最近三週連續走弱
  - `cvr_long_term_weak`：最近三週 CVR 平均 < 2.5%
  - `spend_up_no_return`：最近一週花費 > 前兩週平均 × 1.15，但 ROAS 反而低於前兩週平均
  - `high_volatility`：四週 ROAS 極差 >= 1.5
- 分類依下列順序逐條判斷，命中即停；全部未命中則為「忽略」，不輸出。

## 立即加碼（優先度 100，擴量優先）

- 昨天花費 >= 500
- 昨天 ROAS >= 3 且 昨天直接 ROAS >= 3
- `week_01` ROAS >= 3 且 `week_01` 直接 ROAS >= 2.8
- 過去一個月 ROAS >= 3
- `stable_strong_weeks >= 3`，且沒有 `roas_decline`
- 建議措辭：先檢查最近 3 天是否常碰日預算上限；有撞上限先加 10% ~ 20%，連看 2 天直接 ROAS 仍 >= 3 再第二次上調。

## 優先降預算（優先度 95，控制花費）

- 昨天花費 >= 200
- 昨天 ROAS < 3 或 昨天直接 ROAS < 3
- 且至少一項成立：`weak_weeks >= 3`、`roas_decline`、`spend_up_no_return`、
  `week_01` ROAS < 3、過去一個月 ROAS < 3、`week_01` 直接 ROAS < 3、過去一個月直接 ROAS < 3
- 主因判定：`ctr_decline` 或昨天 CTR < 1.2% → 素材吸引力不足；否則 `cvr_long_term_weak` 或昨天 CVR < 2.0% → 商品頁或價格轉換偏弱；其餘 → 成本過高
- 建議措辭：先降預算 10% ~ 30%；CTR 低先換主圖／文案，CTR 不差但直接 ROAS 低先查商品頁、價格與競品。

## 依賴間接轉換（優先度 88，檢查真實回收）

- 昨天花費 >= 200
- 昨天 ROAS >= 3 但 昨天直接 ROAS < 3
- 且至少一項成立：`week_01` 直接 ROAS < 3、過去一個月直接 ROAS < 3、`indirect_dependency_weeks >= 2`
- 建議措辭：不因總 ROAS 好看就加預算；比較自然流量與廣告流量的直接成交差異；接下來 2 ~ 3 天直接 ROAS 仍 < 3 就維持或微降。

## 立即加碼（優先度 72，候選擴量）

- 昨天點擊 >= 60
- 昨天、`week_01`、過去一個月 ROAS 皆 >= 3
- 昨天銷售相對近月日均 >= +20%
- `stable_strong_weeks >= 2`，且沒有 `high_volatility`
- 建議措辭：先小幅加 5% ~ 10%，再看 2 天；昨天與本週直接 ROAS 都守住 3 才升級為主力擴量。

## 先觀察（優先度 60，短期觀察；報告中歸入 watchlist）

- 昨天 ROAS < 3 或 昨天直接 ROAS < 3
- `weak_weeks < 3` 且沒有 `roas_decline`
- 建議措辭：連續觀察 2 ~ 3 天，先不大調預算；同步檢查活動、價格或評價干擾；持續偏弱再轉降預算。

## 忽略

- 以上皆未命中；不進報告。

## 趨勢週數不足時

兩條「立即加碼」路徑分別需要 `stable_strong_weeks >= 3` / `>= 2`。
若本趟載入的滾動週報少於 2 週，規則層不可能產出加碼建議；
rule summary 與 HTML 報告會加註「規則層本趟不評估加碼」。

## 補充訊號（`_derive_signals`，只做標記，不改分類）

- 曝光 >= 1000 且 CTR < 1.2% → 素材吸引力偏弱
- 點擊 >= 30 且 CVR < 2.0% → 點進來但不下單，優先檢查商品頁
- ROAS >= 3 且直接 ROAS < 3 → 總回收達標但直接成交不足
- CPC > 12 且 ROAS < 3 → 流量成本偏高
- 昨天／`week_01`／過去一個月 ROAS 皆 >= 3 → 短中期回收穩定；皆 < 3 → 短中期回收持續不達標
- 直接成交占比 < 45% 且有銷售 → 直接成交占比偏低
- 另附趨勢層 flag：近幾週持續達標／持續不達標、回收連續走弱、CTR 連續下滑、CVR 長期偏弱、花費提升但回收未同步改善、間接依賴。
