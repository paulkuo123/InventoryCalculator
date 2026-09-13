# 廣告報告 Prompt 規格

本文件對齊 `ads_analysis.py` 的 `_build_ai_payload`、`_build_openai_system_prompt`、
`OPENAI_ANALYSIS_SCHEMA` 與 `_validate_openai_analysis`；欄位或約束有異動時以程式為準。

## 目的

將規則層的篩選結果送入 OpenAI Responses API（`background=true`、嚴格 JSON Schema），
由模型獨立驗算並產出可稽核的投放決策；規則層結論只是比較基準，不是答案。

## 輸入結構（`data` 物件）

- `store`：`name`、`store_id`
- `analysis_scope`：目標、決策基準（昨天為主，`week_01` 與過去一個月驗證，`week_01`~`week_04` 判趨勢）、
  `roas_threshold = 3`、`trend_window_count`、重疊視窗警告、預算調整最低證據要求、`selected_metrics`
- `business_context`：幣別 TWD、`store_roas_floor = 3`、平台費率估計 0.16、
  毛利／庫存／預算上限皆標記為不可用，並附「缺資料時不得宣稱獲利或撞預算」的約束
- `report_windows`：本趟載入的每份報表（視窗 key、期間、天數、匯出時間）
- `metric_dictionary`：曝光、點擊、CTR、CVR、CPC、CPA、ROAS、直接 ROAS、轉換、直接轉換、直接成交占比
- `account_summary`：`health`、`current_window`、`windows`、`comparison`
- `rule_screening_summary`：規則層 `overall_health` 與 `overview`，標明「僅供比較」
- `review_coverage`：報表商品總數、候選池大小、實際送入模型筆數、選取邏輯說明
- `candidate_products`：送入模型的商品（含各視窗 decision snapshot、趨勢序列、規則層分類與主因）
- `must_review_products` / `must_review_count`：必看商品清單

### 候選與必看商品的選取

- 候選池 `_should_include_for_llm`：焦點範圍且投放中、規則層已分類為加碼／降預算／間接依賴、
  昨天花費 >= 80、昨天點擊 >= 20、昨天轉換 >= 1、近月花費 >= 500、或至少一個補充訊號。
- 必看 `_build_must_review_products`：焦點範圍且投放中、昨天花費 >= 150 且直接 ROAS < 3、
  點擊 >= 40 且 CTR < 2.2%、點擊 >= 40 且 CVR < 5%、或 ROAS >= 3 但直接 ROAS < 3。
- 焦點範圍商品（airpods／氣囊／吊飾家族）一律納入，且不受 36 件上限裁切；其餘依昨天花費、點擊、近月花費排序補足。

## 模型約束（system prompt）

- 每個商品結論至少引用 2 個時間窗、3 個具體指標，含樣本量與回收品質。
- 同時判讀 ROAS、直接 ROAS、CTR、CVR、CPC、CPA、直接成交占比；不可只看 ROAS。
- 店內 ROAS 及格線為 3；缺毛利／運費／折扣／退貨資料，不得宣稱真正獲利。
- 過去一個月、`week_01` 與昨天是重疊視窗，不得當三票獨立證據。
- 昨天是主訊號；樣本不足或只壞一天優先 `watchlist`。
- `scale_up` 需總 ROAS 與直接 ROAS 都有足夠樣本且趨勢未轉弱；總 ROAS 達標但直接 ROAS 不足時判間接依賴，不可直接擴量。
- CTR 弱且曝光足夠才判素材；CTR 尚可但 CVR 長期弱才判商品頁／價格。
- `budget_change_pct` 限 -30 ~ +20；信心低、資料不足或缺預算上限資訊時必須為 0。
- 不得虛構庫存、毛利、預算上限、活動、競品或自然流量；缺的資料寫進限制與風險。
- 必須獨立驗算規則層；不同意時 `rule_disagreement = true` 並說明原因。
- 繁體中文、結論先行；`evidence` 寫成可查核短句；`direct_actions` 含觀察期限與停止／回復條件。

## 輸出欄位（`OPENAI_ANALYSIS_SCHEMA`，全部必填，`additionalProperties: false`）

- `overall_health`：`強勢` / `穩健` / `偏弱` / `資料不足`
- `executive_summary`
- `account_diagnosis`：`decision`、`confidence`（high/medium/low）、`primary_risk`、`primary_opportunity`、
  `evidence`（2~8 條）、`data_limitations`（<= 8 條）
- `scale_up`、`reduce_or_fix`、`indirect_dependency`、`watchlist`：`product_decision` 陣列
- `next_actions`：3~8 條
- `excluded_but_reviewed_products`：`excluded_product` 陣列
- `reviewed_product_count`：整數
- `analysis_limitations`：<= 10 條

### `product_decision`

- `product_id`
- `primary_issue`：`素材吸引力不足` / `商品頁轉換偏弱` / `成本過高` / `間接轉換占比過高` /
  `回收穩定可擴量` / `需繼續觀察` / `資料不足`
- `confidence`：high / medium / low
- `reason`、`why_not_other_issue`
- `evidence`：2~8 條
- `direct_actions`：2~4 條
- `budget_change_pct`：整數，-30 ~ 20
- `observation_days`：整數，1 ~ 7
- `risk_if_wrong`
- `rule_disagreement`（boolean）、`rule_disagreement_reason`

### `excluded_product`

- `product_id`、`reason`、`evidence`（<= 5 條）

## 程式端驗收（`_validate_openai_analysis`）

Schema 通過後再檢查商業邏輯；未過則帶錯誤訊息重送一次（最多 2 次嘗試）：

- 四個決策陣列的商品都必須在 `candidate_products` 內，且不可重複出現在不同分類。
- `confidence = low` 時 `budget_change_pct` 必須為 0。
- `rule_disagreement = true` 時必須有 `rule_disagreement_reason`。
- `excluded_but_reviewed_products` 不可含未提供的商品。
- 每個 `must_review_products` 都必須出現在四個決策陣列之一或排除清單。
- `reviewed_product_count` 必須等於 `candidate_products` 筆數。

## 報告呈現

- 報告需顯示請求模型、實際回應模型、推理強度、API 耗時與 request ID。
- 商品名稱相近時以商品 ID 對應；商品圖來自 `golden_table.json` 的 `商品圖片網址`。
