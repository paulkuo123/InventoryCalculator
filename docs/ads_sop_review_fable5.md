# 現行廣告 SOP 審閱(Fable 5,2026-09-13)

目的:對照「毛利感知」的做法,逐條審閱現行廣告 SOP,判定**保留 / 調整 / 暫緩**,
並評估 1688 種子成本庫(見 [`ads_cost_margin_design.md`](ads_cost_margin_design.md))
能否嵌入而不破壞「直接 ROAS >= 3」主軸。

審閱依據為 repo 內實際文件與程式,不是通用建議:
[`ads_analysis_rules.md`](ads_analysis_rules.md)、
[`ads_metrics_dictionary.md`](ads_metrics_dictionary.md)、
[`ads_report_prompt_spec.md`](ads_report_prompt_spec.md)、
[`ads_weekly_pipeline.md`](ads_weekly_pipeline.md)、
`ads_analysis.py`(`_classify_product` / `_analyze_weekly_trends` / `_build_ai_payload`)、
`ads_scope.py`、`ads_weekly.py`、`skills/ads-analysis/SKILL.md`。

## 總評

現行 SOP 的骨架是對的:**回收(ROAS)先行、昨天為主、多視窗驗證、人工核准落地**。
最大的結構性缺口是 `business_context.gross_margin_available = False`——
整套系統知道「錢有沒有回來」,不知道「回來的錢夠不夠付貨成本」。
種子成本庫剛好補這個洞,而且可以**只加註記、不動門檻**地嵌入(判定見文末)。
下面逐條審閱。

## 逐條審閱

### 1. 主視窗:昨天為主,`week_01` + 過去一個月驗證(+ 滾動 4×7 天趨勢)

**保留。**
`ads_analysis_rules.md` 與 `_analyze_weekly_trends` 已把「昨天是主訊號、
重疊視窗不當三票獨立證據、趨勢週不足時規則層不評估加碼」寫得很完整;
`ads_report_prompt_spec.md` 也把同樣約束綁進模型 system prompt。
成本資料是**慢變量**(採購價幾週才動一次),不影響視窗設計,兩者天然正交。

### 2. 及格線:ROAS >= 3;加碼需昨天 ROAS >= 3 且直接 ROAS >= 3

**保留(主軸不可動)。**
這是全系統的脊椎:`_classify_product` 的每一桶、prompt 的 `roas_threshold = 3`、
`store_roas_floor = 3` 都掛在上面。成本輔助的角色是回答
「ROAS 3 之上還剩多少毛利」,**不是**提出第二套及格線。
唯一要防的走鐘:毛利厚的商品被拿來當理由放寬 ROAS < 3 的容忍——
設計文件第 5 節矩陣已明文禁止(成本只能更保守或給加碼信心,不能翻桶)。

### 3. 總 ROAS 強、直接 ROAS 弱 → 依賴間接轉換,不加碼

**保留,且是成本資料的最大受益者。**
現行規則(優先度 88 那桶)只能說「不因總 ROAS 好看就加」,然後叫人比較自然流量差異、
再觀察 2~3 天——但**觀察完還是沒有新證據**,因為缺的不是時間,是毛利事實。
風險具體化:

- 間接轉換的歸因是「點廣告後買了**別的**商品」;如果被帶動的商品毛利薄,
  總 ROAS 3+ 可能仍是整體賠錢,現在完全看不見。
- 反向風險同樣存在:氣囊/吊飾若間接帶動的是高毛利品,現在的 SOP 會讓它
  永遠卡在「不加碼」,錯過真實有利可圖的擴量。
- 這桶的商品最容易被「2~3 天再看看」無限展期,變成殭屍預算。

**調整方向**(Phase 1 之後):此桶的報告列出成本註記(本品 + 有資料時的家族毛利概況),
把「間接轉換是否有毛利撐住」從猜測變成可查核欄位。分類邏輯本身不動。

### 4. 預算/目標 ROAS 變更需庭安核准;bot 只出建議

**保留,不可協商。**
`ads_weekly_pipeline.md` 已明文「禁止改預算、出價或任何廣告設定」;
種子庫那邊 `1688_purchase_history_kb.md` 同樣是 fail-closed(dry-run 預設、
`--i-approve-kb-import` + allowlist 才准寫)。兩套系統的核准文化一致,
成本輔助沿用同一原則:**自動寫進報告也要先過庭安**(不只是自動改預算要核准)。

### 5. 報告只列可動作商品:立即加碼 / 優先降預算 / 依賴間接轉換(+ 焦點 SKU 強制入列)

**保留。**
「忽略桶不輸出」與 `ads_scope.py` 的焦點強制入列
(`select_report_products` / `restore_missing_scope_products`,PR #50)運作正常。
成本註記加入後要守住同一紀律:**不要**因為「有成本資料了」就把忽略桶的商品撈回報告;
成本欄位只掛在本來就會出現的商品上。

### 6. 範圍:airpods殼 / 氣囊 / 吊飾(+ A1 `18025139892`)

**保留,並用它排成本對應的優先序。**
`ads_session.SCOPE_KEYWORD_GROUPS` 單一來源 + `DEFAULT_SCOPE_EXTRA_IDS` id 保險網
(9159438193 氣囊 / 22589154150 吊飾)已經修過 2026-09-12 的漏抓。
成本對應工作量有限,先做氣囊/吊飾(間接依賴重災區)再做 airpods殼,不必全店鋪開。

### 7. 昨天單日壞、整週健康 → 軟砍 ~20% 或觀察 2~3 天,不硬殺

**保留。**
與 `_classify_product` 的「先觀察」桶(weak_weeks < 3 且無 roas_decline)
及 prompt 的「只壞一天優先 watchlist」一致。
毛利資料只改變**急迫度**:薄毛利商品的單日壞值得早一天查,厚毛利可以照常等 2~3 天。
不需要改規則,寫進建議措辭即可(Phase 2 範圍)。

### 8. 遠端週抓 + 焦點 SKU 已在 main(PR #49 / #50)

**保留。**
fail-closed 行為(`BLOCKER.md` + 非零退出、禁止沿用上週 CSV)是對的。
成本註記若在 Phase 1 加入,必須遵守同樣紀律的**反向版本**:
成本步驟失敗或隔離庫不存在時,**週報照常產出**(成本是輔助,不是必要視窗),
不可因成本缺席而 STOP——這與「昨天/week_01/過去一個月缺一就 STOP」的必要視窗不同級。

### 9. 規則 HTML 可能漏 SKU → 從 CSV 重算

**保留。**
CSV 是數字的真相來源,HTML 只是呈現層,這個原則同樣適用未來的成本欄位:
成本旁路 JSON 是真相,HTML 漏列時從 JSON 重查,不反過來信 HTML。

## 暫緩(明確不做)

| 項目 | 為什麼暫緩 |
|---|---|
| 成本硬閘門(保本 ROAS 不合格就踢出加碼桶) | 對應覆蓋率與切點都未經真實資料校準,先跑 Phase 0/1 收集誤判率;需庭安另案核准 |
| 改 ROAS 門檻(例如高毛利品降到 2.5) | 動了主軸,所有既有規則、prompt、驗收邏輯要連動改,風險遠大於收益 |
| 種子庫接進 live `procurement.db` / 動 Golden | Golden 暫停、auto_approve 關閉是刻意狀態;`1688_purchase_history_kb.md` 的閘門不因廣告需求繞過 |
| 週報自動讀成本庫 | 等庭安核准 Phase 1;核准前只有人工離線查(Phase 0) |
| 間接轉換的逐單歸因分析 | Shopee 匯出只有商品層彙總(見 `ads_metrics_dictionary.md`),拿不到「間接買了哪顆」的明細,做不了就不假裝能做 |

## 成本輔助放得進來嗎?(結論)

**放得進來,而且不用動脊椎。** 三個理由:

1. **系統已預留插槽。** `_build_ai_payload` 的 `business_context` 本來就有
   `gross_margin_available` 旗標與「缺毛利不得宣稱獲利」約束;
   補上資料是把既有欄位從 False 翻成(部分商品)True,不是新發明一層。
2. **成本與 ROAS 正交。** ROAS 管「回收效率」,成本管「回收品質」;
   決策順序固定為「先過直接 ROAS >= 3,再看毛利決定幅度」,兩者不會打架。
   設計文件的矩陣明訂成本不能翻桶,主軸無風險。
3. **失敗模式安全。** 對不上 key 就跳過、缺庫就跳過、抽掉成本整份報告退回今日行為——
   沒有任何路徑會因成本資料而讓報告變差或流程變脆。

前提條件(缺一不可):對應只信 Golden 已核准/inbound exact;
成本一律標明「下限估計」;每一階段開關都過庭安。
