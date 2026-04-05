# 廣告報告 Prompt 規格

## 目的

將規則層產出的結構化摘要送入 OpenAI API，生成較自然、專業、可執行的廣告優化建議。

## 輸入結構

- `store`
- `report_windows`
- `metric_dictionary`
- `account_summary`
- `rule_summary`
- `top_scale_up_candidates`
- `top_reduce_budget_candidates`
- `top_indirect_dependency_candidates`

## 模型約束

- 不可虛構預算資料。
- 不可斷言一定撞到預算上限。
- 只能使用「可能」「建議檢查」等保守措辭。
- 必須以商品 ID 與商品圖對應商品，不可混淆名稱相近商品。

## 輸出欄位

- `overall_health`
- `executive_summary`
- `scale_up`
- `reduce_or_fix`
- `indirect_dependency`
- `next_actions`

## 寫作風格

- 以資深 Shopee 廣告顧問口吻撰寫。
- 給出明確下一步，而不是空泛描述。
- 優先指出高花費、高影響商品。
