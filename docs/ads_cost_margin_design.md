# 廣告分析引用 1688 歷史成本:設計文件

狀態:**設計階段,尚未實作**。本文件只定義資料契約、彙總規則與導入節奏;
不改 `ads_analysis.py` 的任何門檻,不把種子庫接進 live pipeline。
商業前提依 2026-09-13 決議:**成本是加碼/降預算的毛利輔助訊號,不是每週自動硬閘門**;
自動寫進報告需 **庭安明確核准** 後才開。

相關文件:
[`ads_analysis_rules.md`](ads_analysis_rules.md)、
[`ads_metrics_dictionary.md`](ads_metrics_dictionary.md)、
[`ads_report_prompt_spec.md`](ads_report_prompt_spec.md)、
[`ads_weekly_pipeline.md`](ads_weekly_pipeline.md)、
[`1688_purchase_history_kb.md`](1688_purchase_history_kb.md)。

## 1. 問題

現行廣告判讀只有回收面:店內及格線 `ROAS >= 3` 且 `直接 ROAS >= 3`
(`ads_analysis.py` 的 `_classify_product`,規則見 `ads_analysis_rules.md`)。
`_build_ai_payload` 的 `business_context` 明確標記 `gross_margin_available: False`,
並約束模型「缺毛利資料不得宣稱獲利」——所以今天的報告只能回答「回收有沒有達標」,
回答不了兩個實際決策問題:

1. **ROAS 3 對這顆 SKU 到底夠不夠賺?** 低成本殼類 ROAS 3 可能毛利很厚;
   高成本品 ROAS 3 可能已經貼著保本線。
2. **氣囊/吊飾這種「總 ROAS 高、直接 ROAS 低」的間接依賴商品,
   間接轉換帶來的訂單究竟有沒有毛利撐住廣告費?**
   這是目前「依賴間接轉換」桶最缺的一塊證據。

我們手上已有 3,309 筆 1688 成功訂單(約 3.9 萬行明細)的隔離種子庫,
每行都有 `price_cny`(人民幣單價,已全數填妥),可以回推歷史採購成本。

## 2. 資料契約

### 2.1 來源:隔離種子庫(不進 Git、不進 live)

- 檔案:`kb_excel_success_isolated_20260912.db`(**營運機檔案,絕不提交進 Git**,
  比照 `cookies.json`/`.env.local` 的處理方式)。
- 內容:3,309 筆 1688 成功訂單、約 39,000 行明細。
- schema 與 `purchase_history_store.py` 的 `kb_*` 表一致
  (見 [`1688_purchase_history_kb.md`](1688_purchase_history_kb.md)):
  - `kb_orders`:`alibaba_order_id`(PK)、`status`、`ordered_at` / `ordered_at_ts`、`seller`。
  - `kb_order_items`:`(alibaba_order_id, source_line_id)` unique;
    `offer_id`、`sku_id`、`unresolved_id`、`sku_key`、`qty`、`price_cny`、`line_total_cny`。
  - `kb_purchase_history`:依 `(offer_id, sku_key)` 彙總的
    `order_count`、`total_qty`、`last_price_cny`、`last_ordered_at`。
  - `kb_mappings`:`(offer_id, sku_key)` ↔ `(shopee_product_id, shopee_model_id)`,
    `source ∈ {golden_approved, inbound_exact, manual}`。
- 它**不在** live `procurement.db` 裡;Golden 暫停、`auto_approve` 關閉的現狀不因本設計改變。

### 2.2 欄位語意(實作時不得再猜)

| 欄位 | 語意 | 缺值處理 |
|---|---|---|
| `price_cny` | 人民幣**單價**,種子庫內全數已填 | 理論上不缺;若真遇到 NULL,該行**跳過不計**,不可填 0 |
| `line_total_cny` | 行小計,種子庫內**多為空** | 一律用 `qty × price_cny` 回算,不等 `line_total_cny` 補齊 |
| `qty` | 行數量 | NULL 時該行只能貢獻單價樣本,不可進加權平均 |
| `ordered_at_ts` | 下單時間(來自 `kb_orders`) | 新舊排序與「最近一次採購」都以此為準 |
| `sku_key` | `sku_id` 存在時等於真實 sku_id;否則為 `unresolved_id` | `unresolved_id` 開頭的 key **不參與**自動對應(見 3.3) |

同一 `(offer_id, sku_key)` 出現多個歷史價是常態(調價、談價、活動價),
彙總規則見第 4 節,**不可**只取任意一行。

## 3. 對應鍵(join keys):蝦皮商品 ↔ 1688 成本

### 3.1 層級落差是本設計的核心難點

- 廣告報表的粒度是**蝦皮商品**(`product_id`,即 `ads_analysis.py` 全程使用的 key)。
- 1688 成本的粒度是**規格**(`offer_id + sku_key`)。
- 中間橋梁是 `golden_table.json`:以 `商品ID` 為 key,每個商品的 `型號` 清單
  (`規格ID` = model_id)帶有 `1688_offer_id`、`1688_sku_id`、`1688_mapping_status`、
  `1688_verified_at`、`1688_last_price_cny` 等欄位(寫入邏輯見 `golden_import.py`)。

### 3.2 標準對應鏈

```text
廣告列 product_id
  → golden_table.json[product_id].型號[*]           (規格ID = shopee_model_id)
  → 每個型號的 (1688_offer_id, 1688_sku_id)
  → sku_key = sku_id                                 (sku_key_for 規則,有 sku_id 就用真實 id)
  → 種子庫 kb_order_items / kb_purchase_history 查 (offer_id, sku_key) 的歷史價
```

備援路徑:種子庫 `kb_mappings` 已存 `(offer_id, sku_key) ↔ (shopee_product_id, shopee_model_id)`
快照。兩條路查得到同一組 key 時視為高信心;只有 `kb_mappings.source = manual` 而
Golden 沒有對應時,列為「待人工確認」,Phase 0/1 不採用。

### 3.3 有把握才對,沒把握就跳過(硬規則)

- **只採用** Golden 已核准(`1688_mapping_status` 為已驗證狀態且有 `1688_verified_at`)
  或 `kb_mappings.source ∈ {golden_approved, inbound_exact}` 的對應。
- **禁止**用商品名稱、圖片、規格文字相似度自動併 key——
  這與 [`1688_purchase_history_kb.md`](1688_purchase_history_kb.md) 的禁止事項一致。
- `sku_key` 為 `unresolved_id` 者一律跳過。
- 對不上的商品在報告中的成本欄位標示「無成本資料」,
  **判讀完全回退到現行純 ROAS 規則,不做任何降級或懲罰**。

### 3.4 商品層彙總(一個商品多個型號)

一個蝦皮商品的各型號成本可能不同,而廣告數字只有商品層。規則:

- **覆蓋率**:`已對應型號數 / 總型號數`。覆蓋率 < 80% 時只出「部分覆蓋」註記,
  不產出單一成本數字,避免用少數型號代表全商品。
- 覆蓋率達標時輸出**成本區間** `[min, max]` 與**加權成本**
  (權重優先用 golden 型號的 `月銷量`;缺月銷量時用種子庫 `total_qty`;都缺就用簡單平均並標注)。
- 報告一律同時呈現區間與加權值,不可只給單一數字假裝精確。

## 4. 成本彙總規則(同一 (offer,sku) 多筆歷史價)

### 4.1 取價順序

1. **最近一次採購價**(`ordered_at_ts` 最大那筆的 `price_cny`)為主值——
   對應 `kb_purchase_history.last_price_cny` 的既有語意。
2. **近期中位數**(建議視窗:最近 180 天內的行,不足 5 行則擴到最近 10 行)為對照值。
3. 兩者落差 > 20% 時標示「近期價格波動大」,判讀取**較高者**(保守,寧可低估毛利)。

### 4.2 幣別換算與到岸成本(cost floor)

```text
到岸成本_TWD = price_cny × 匯率(CNY→TWD)
             + 國際運費攤提(每件估計值)
             + 其他固定費用攤提(每件估計值)
```

- 匯率與運費攤提是**設定參數**,不寫死在程式常數
  (實作時放設定檔,由營運維護;本文件不定死數字)。
- 這是**成本下限(floor)**:未含退貨、包材、倉儲。命名與報告文案都必須講清楚是「下限估計」。

### 4.3 保本 ROAS(把成本翻譯成 ROAS 語言)

判讀不引入新指標體系,而是把成本換算成現行 ROAS 語言:

```text
每 1 元營收的變動成本率 c = (到岸成本_TWD / 售價_TWD) + 平台費率
保本 ROAS = 1 / (1 − c)
```

- 平台費率沿用 `_build_ai_payload` 既有的 `platform_fee_rate_estimate = 0.16`。
- 售價用廣告報表可回推的**客單價**(銷售金額 / 銷售數;直接口徑優先),
  缺樣本時退用商品標價並標注。
- **毛利厚薄的定義**(相對店內及格線 3):
  - `保本 ROAS <= 2.0` → **毛利厚**:ROAS 3 有實質獲利空間。
  - `2.0 < 保本 ROAS < 2.8` → **毛利中等**:ROAS 3 賺但不多。
  - `保本 ROAS >= 2.8` → **毛利薄**:ROAS 3 貼著保本線,擴量風險高。
  - 區間切點是初始建議值,列入開放問題,由試跑數據校準。

## 5. 決策矩陣:成本訊號 × 現行行動桶

**主軸不變**:分類仍由 `ads_analysis_rules.md` 的規則產生
(昨天為主、`week_01` + 過去一個月驗證、直接 ROAS >= 3 及格線)。
成本只在分類之後調整**建議強度與措辭**,不改分類本身:

| 現行分類 | 毛利厚 | 毛利中等 | 毛利薄 | 無成本資料 |
|---|---|---|---|---|
| 立即加碼 | 照現行 SOP 加碼(先 10%~20%,守撞預算檢查) | 照現行 SOP,但第二次上調前先確認毛利未被活動價侵蝕 | **降級為小步試探**:最多 +5%~10%,觀察 2~3 天;建議文案明寫「保本 ROAS ≈ X,接近及格線」 | 照現行 SOP,不因缺成本而扣分 |
| 優先降預算 | 照現行降 10%~30%;若主因是素材/商品頁,修完可較快回測 | 照現行 SOP | **降幅取區間上緣(~30%)**,回收不改善優先關,不戀戰 | 照現行 SOP |
| 依賴間接轉換 | 間接轉換**可能有毛利撐住**:維持觀察,不加碼但也不急砍;要求後續 2~3 天直接 ROAS 驗證 | 照現行 SOP(不加碼、比較自然流量差異) | 間接故事**撐不起薄毛利**:建議偏向微降,觀察期縮短 | 照現行 SOP;**氣囊/吊飾家族優先補齊對應**(見第 7 節) |
| 先觀察(watchlist) | 觀察 2~3 天照舊 | 照舊 | 觀察期內優先排查是否已在賠錢邊緣 | 照舊 |

矩陣鐵則:

- 成本訊號**只能讓建議更保守或提供加碼信心,不能單獨把商品從「降預算」翻成「加碼」**。
- 「昨天單日壞、整週健康」仍走現行軟處理(約 -20% 或觀察 2~3 天),薄毛利只影響觀察的急迫度。
- 任何預算/目標 ROAS 實際變更仍需**庭安核准**;bot 只出建議。

## 6. 分階段導入

### Phase 0:人工離線查(現在就能做,不動程式)

- 營運在隔離庫上手動跑查詢(或用一支**獨立、唯讀**的離線腳本),
  對報告中的加碼/間接依賴候選逐顆查成本,人工寫進週會結論。
- 不改 `ads_analysis.py`、不改報告格式、不建任何自動 join。
- 目的:驗證對應鏈與彙總規則在真實資料上是否可用,累積校準樣本。

### Phase 1:報告註記(唯讀欄位)——**需庭安核准後才開**

- 週報流程新增一個**可選的**成本註記步驟:讀隔離庫、產出
  `product_id → {成本區間, 加權成本, 保本 ROAS, 覆蓋率, 資料日期}` 的旁路 JSON。
- 報告(HTML/JSON)顯示這些欄位,並可放入 `_build_ai_payload` 的 `business_context`
  (把 `gross_margin_available` 對有資料的商品翻成可用,附「下限估計」限制文字)。
- **不改** `_classify_product` 的任何門檻與分類;模型約束仍是「缺資料不得宣稱獲利」,
  只是缺的資料變少了。
- 隔離庫路徑用參數傳入;檔案不存在時整個步驟靜默跳過,週報照常產出。

### Phase 2:軟性閘門(措辭與幅度)——**需另一次明確核准**

- 依第 5 節矩陣,允許成本訊號調整**建議的幅度與觀察期**
  (例:薄毛利的「立即加碼」自動改寫為小步試探)。
- 仍不改分類、不擋報告、不自動執行任何預算變更。
- **硬閘門(例:保本 ROAS >= 3 直接踢出加碼桶)不在本設計範圍**,
  要等 Phase 2 跑穩、庭安看過誤判率後另案討論。

## 7. 焦點商品的優先順序

現行焦點範圍(`ads_session.SCOPE_KEYWORD_GROUPS` + `ads_scope.py` 的 id allowlist):
airpods殼 / 氣囊 / 吊飾家族 + A1 campaign `18025139892`。

- **氣囊、吊飾優先補齊 1688 對應**:這兩個家族最常落在「依賴間接轉換」桶
  (總 ROAS 高、直接 ROAS 低),是「間接轉換有沒有毛利撐」問題的主要受益者。
- Phase 0 的人工查詢就從這兩個家族 + 報告當期的加碼候選開始,不必先做全店覆蓋。

## 8. 不做的事

- **不**把隔離庫接進 live `procurement.db`,不動 Golden、Know-how、live 採購流程。
- **不**改 `ads_analysis.py` 的 ROAS 門檻、分類順序、優先度數字。
- **不**在未經庭安核准前把成本欄位自動寫進週報。
- **不**用名稱/圖片/規格相似度自動對 key;對不上就跳過。
- **不**把成本當硬閘門踢商品出桶(Phase 2 之後另案)。
- **不**把 `kb_excel_success_isolated_20260912.db` 或其匯出提交進 Git。
- **不**在缺 `price_cny` 時填 0 或猜值。

## 9. 開放問題

1. 匯率來源與更新頻率:固定月率(營運維護)還是每次取即期價?誰維護設定檔?
2. 運費/固定費攤提:按件均攤的初始估計值是多少?殼類與氣囊體積差異要不要分層?
3. 保本 ROAS 的厚/中/薄切點(2.0 / 2.8)需要用 Phase 0 的真實樣本回測校準。
4. 「近期中位數」視窗(180 天 / 10 行)對調價頻繁的賣家是否合適?
5. 商品層覆蓋率門檻 80% 是否太嚴/太鬆?低覆蓋但主力型號已對應時要不要放行?
6. 售價口徑:活動檔期的客單價會偏低,導致保本 ROAS 被高估(偏保守);要不要排除大促日?
7. 種子庫是 2026-09-12 的快照;成本更新節奏(重跑匯入的頻率與觸發條件)由誰決定?

## 10. 成功指標

- **對應覆蓋率**:焦點範圍(airpods殼/氣囊/吊飾 + A1)中,可產出高信心成本的商品占比
  (Phase 0 目標:焦點商品 ≥ 70%)。
- **決策改變率**:每期報告中,成本訊號實際改變建議幅度/措辭的商品數——
  太低代表白做,太高代表切點錯了,都要回頭校準。
- **零誤傷**:不得出現「因成本資料缺失或對錯 key 而砍掉健康商品」的案例;
  發生一次即回退該商品的成本註記並記錄根因。
- **主軸不破**:直接 ROAS >= 3 及格線、庭安核准流程、報告只列可動作商品——
  三者在任何 Phase 都不變;抽掉成本資料,報告必須退回與今日完全相同的行為。
