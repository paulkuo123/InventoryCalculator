# Golden Table AI 自動化審查（Goal B）

對照庭安要的最終狀態：**AI 找出正確 1688 mapping，並且是真的核過，盡量少用人，把表補完。**  
審查基準是 **main**（本文件寫作時：`340db20`）加上暫停中的文件／候選 PR **#41–#46**。  
**不**提議現在合併那些 PR，也**不**提議 AI-6 寫入，除非庭安先說解暫停。

硬約束（2026-09-09 庭安暫停全表工程，本文件寫作時仍有效）：

- `golden_table.json` SHA-256 仍為 `8a95064fbe116f9e6ead24837ced1511ce34a9f3ac69358d9c2e0fb585ab3d2e`（355 商品／5918 型號列；本機核對：`approved` 3800、有 http URL 4973、有 `1688_sku_id` 1330）。
- 開著的 #41–#46 是文件／候選／閘門草稿，**不要 merge**。
- `mapping_knowledge_pack/config.json` 的 `auto_approve.enabled` 維持 **false**。
- 不重啟 Golden 寫入管線、不改 live `procurement.db`、不發明 URL／skuId。

相關：[`ai_mapping_engine_SPEC_v0.md`](ai_mapping_engine_SPEC_v0.md)、[`sku_mapping_knowhow_engine.md`](sku_mapping_knowhow_engine.md)、[`mapping_eval.md`](mapping_eval.md)、[`offer_discovery.md`](offer_discovery.md)、[`offer_discovery_spike.md`](offer_discovery_spike.md)、[`mapping_kb_isolated_import.md`](mapping_kb_isolated_import.md)、[`1688_purchase_history_kb.md`](1688_purchase_history_kb.md)、[`restock_closed_loop_review.md`](restock_closed_loop_review.md)。

主線後續（本審查寫作之後已合 main，基準 `58ce01b`／#114）：Mapping Engine 階段 0–2.3（#110–#114）。第 1 層建議、隔離 KB harvest、評估四層對帳已落地；**仍不**自動填 URL、**不**寫 Golden。

## 1. 最終目標 vs main 現況管線

```text
最終（低人工、表補完）
  缺 URL 的型號 → 從已買過的 offer／兄弟檔提出候選
       → 人（或極窄閘門）確認 URL
       → live 快照證明 offer 還在
       → Know-how 第 2 層 SKU 綠／黃／紅
       → 人核准（長期）；auto_approve 只有在真實 DB 精度＋可撤回之後才談
       → golden_table.json 成為補貨／入庫真相

main 現況（第 2 層已落地；第 1 層建議已落地 #113，掃描仍跳過無 URL）
  已有 阿里巴巴商品URL 的型號
       → EgoBrowser1688.fetch(已知 URL) 或 7 天內快照
       → SkuMappingService.generate_candidates / classify_review_tier
       → 可選 AI judge（PROMPT_VERSION=2026-09-v2）
       → /sku-mapping.html 人工核准
       → _write_approved_mapping → golden_table.json

  沒有 URL 的型號
       → _scope_models() 直接 continue（第 2 層掃描不變）
       → queue 只顯示 missing_url／紅色「尚未設定 1688 URL」
       → 另有唯讀第 1 層建議（#113）：python -m offer_discovery／GET /api/sku-mapping/offer-suggestions
       → 不自動填 URL、不寫 Golden；人確認 URL 後才進第 2 層
       → Know-how 與 auto-approve 反事實在人填 URL 之前都沒有分母
```

Know-how Engine v1 **已在 main**：知識包、硬／軟規則、歷史正負例、`final_score`、`GET /api/sku-mapping/explain`、`python -m mapping_eval`（含 auto-approve **反事實**）。它**不是**第二套引擎，也**不會**自動寫 golden。fixture 上 SPEC 5.1 全閘 `n_would_pass=0`。

第 1 層（蝦皮商品 → 1688 offer）已在 main 落地（#113）：`offer_discovery.py` ＋唯讀 `GET /api/sku-mapping/offer-suggestions`（測試：`tests/test_offer_discovery.py`）。先讀隔離種子，再讀同商品兄弟檔，不做站內搜尋。契約見 [`offer_discovery.md`](offer_discovery.md)；設計依據仍是 [`offer_discovery_spike.md`](offer_discovery_spike.md)。**不**自動填 URL、**不**寫 Golden。工作台掃描仍跳過無 URL；UI 可能尚未呼叫建議 API。

`AlibabaApiClient` 沒有 `search_offers`。`crawler.py` 是蝦皮賣家中心，不是 1688。

## 2. 暫停時進度（handoff ＋ PR 原文）

這些數字在 **#41–#46 分支**，不在 main 工作樹。主表 SHA 暫停後未變。

| 階段 | PR | 做了什麼 | 暫停時數字 |
|---|---|---|---|
| 刀 1 急迫 54 列 | [#42](https://github.com/paulkuo123/InventoryCalculator/pull/42) | 唯讀分桶＋HTTP（全是 WAF punish，不可當活／死） | `no_url` 20／`url_suspect` 18／`mapping_suspect` 16／`ok_skip` 0 |
| AI-1 全表 | [#43](https://github.com/paulkuo123/InventoryCalculator/pull/43) | 5918 列唯讀分桶，無新 CDP | **5918** = `no_url` 945／`url_suspect` 413／`mapping_suspect` 3264／`ok_skip` 1296 |
| AI-2 | [#44](https://github.com/paulkuo123/InventoryCalculator/pull/44) | 36 個 P2 `url_suspect` 補貨 offer，已登入 CDP，不加車 | **alive 11／dead 23／off_shelf 2**（15 個死頁落到 1688 首頁 `notfound`，不是 `wrongpage.html`） |
| AI-4 | [#45](https://github.com/paulkuo123/InventoryCalculator/pull/45) | 253 列候選備料；3B 全站搜撞 punish 就 STOP | 暫停快照：**with_candidate 101／no_candidate 55／blocked_captcha 97**（101+55+97=253）。PR 正文中間態：初跑 69／26／158 → resume2 89／44／120 → resume4 把 captcha 收到 97 |
| AI-5 | [#46](https://github.com/paulkuo123/InventoryCalculator/pull/46) | 靜態審核 HTML；動作只寫瀏覽器本機／可下載 CSV，**不寫 golden** | 主佇列 **81**（原 69 + 杯套／愛心熊 resume 12）＋附錄 Plan C **7**（`shelved_round1`，非必審）。標題「88」= 81+7。`blocked_captcha`／`no_candidate` **不**進必審主佇列 |
| Phase 1 閘門草稿 | [#41](https://github.com/paulkuo123/InventoryCalculator/pull/41) | `mapping_procurement_gate.py`、`WRITE_GOLDEN` 片語、鎖舊覆寫路徑 | **不在 main**。main 沒有 `mapping_procurement_gate.py`，也沒有 `confirmPhrase=WRITE_GOLDEN` |

Trust gate（#41 正文，2026-09-07 修訂；**未合 main**）：粒度是**型號列**。`approved` + 有效 `1688_sku_id` + URL／offer + 無列級 type conflict → 可採購／certain。**同商品多個 1688 offer 不是自動硬擋。** 缺 sku_id／URL、列級 conflict、rejected、sold-out／discontinued → 不可採購。舊 `1688_verified_at` 單獨不算信任。

main 上實際在用的「可補／certain」比較鬆，見 §4。

## 3. AI 能幫忙 vs 人／CDP 仍必須

### AI／規則已能幫忙（不寫 golden）

- **第 2 層排序與分級：** `generate_candidates`、RULE-0001…0004、顏色 alias、`historical_support`、負例剔除、`classify_review_tier` 綠／黃／紅。
- **說明：** `SkuMappingService.explain`、`GET /api/sku-mapping/explain`。
- **離線評估：** `python -m mapping_eval run|compare|audit`。Green Precision 是「綠燈裡 Top-1 對不對」，**不是**寫檔授權。
- **反事實 auto-approve：** `mapping_eval.assess_auto_approve_counterfactual` — 只報告，不把 `enabled` 設 true。
- **第 1 層建議（#113 已落地、唯讀）：** `offer_discovery.suggest_offers`／`GET /api/sku-mapping/offer-suggestions`：隔離種子 `kb_excel_success_isolated_20260912.db`（約 3309 筆成功單，有 `price_cny`）＋同商品兄弟檔已綁 offer。缺 KB 不中斷。測試：`tests/test_offer_discovery.py`。見 [`offer_discovery.md`](offer_discovery.md)。

### 人（或操作者已登入的 CDP）仍必須

| 動作 | 為什麼不能交給模型單獨做 |
|---|---|
| 確認／首次填 1688 URL | 錯 offer 會讓第 2 層在**錯誤目錄**走出綠色唯一匹配，再進車／入庫 |
| 過驗證碼／滑塊／登入 | `EgoBrowser1688` 停下來等人；AI-4 97／253 卡 captcha；punish 必須 STOP，不能繞 |
| 判死頁、換新 offer | AI-2：36 offer 裡 23 死＋2 下架。死頁常是首頁 `notfound`，HTTP GET 又常是 WAF，**不能**用未登入 HTTP 當健康 |
| 綠／黃／紅之後的核准 | `_apply_decision` → `_write_approved_mapping` 是人工動作。綠燈仍要人（或未來極窄閘門，現在不開） |
| 多 offer 衝突 | 種子歷史 vs 兄弟檔 golden 不一致時並列，**禁止自動消解**（spike §3） |
| 停售／無匹配 | 紅燈只能保留、標無匹配或停售；`OTHER` 負例要文字 |
| 舊核准無稽核 | `_repair_unverified_approvals` 把無 approve 稽核的列打回待審 — 這是安全修復，不是 AI 建議 |

CDP 探活（AI-2 那種）是**證據**，不是寫入。解暫停後寫入仍應走現有 `/api/sku-mapping/decisions`（或 #41 的更嚴閘門，若庭安要合），不要另開「AI 直接 patch JSON」路徑。

## 4. Know-how ＋ 隔離種子 KB ＋ reverse-audit 信任閘該怎麼疊

```text
隔離種子 KB（唯讀 attach，不進 live procurement.db）
    │  第 1 層：這個蝦皮型號我們是否買過某個 offer？
    ▼
同商品 golden 兄弟檔 offer（零瀏覽器）
    │  恰好一個 distinct offer → 高信心建議（仍要人點 URL）
    │  多個 → 黃／紅並列
    ▼
人確認 URL 之後
    │
EgoBrowser1688.fetch(已知 URL)     ← 登入牆停；死頁 health_status=invalid
    ▼
Know-how 第 2 層（main 已有）
    generate_candidates + historical_support + 負例 + explain
    ▼
人工核准 → golden（schema 不變）
    ▼
補貨信任（兩套，解暫停前不要默默改嚴）
    路 A launcher：approved + URL + sku 名（殼要第二規格）；sku_id 可空
    路 B reverse_audit certain：approved + URL + (sku_id 或 name/spec)
    #41 提案（未合）：approved + sku_id + URL／offer；多 offer 非硬擋
```

組成規則（解暫停後也適用）：

1. **Golden 已核准列是最高優先真相。** KB 只能抄到 `kb_mappings`，不可回寫（`docs/1688_purchase_history_kb.md`）。
2. **種子庫只當第 1 層建議。** `PHASE3_HISTORY_IMPORT_ENABLED` 維持 false，且 live crawl 沒有執行路徑，直到另開核准＋訂單 allowlist。
3. **Know-how 只排序、說明、評估。** `final_score` 不改綠色條件；綠色 ≠ 可自動寫。
4. **reverse_audit 不發明欄位。** uncertain 缺 approved／URL／（skuId 與 name/spec 都沒有）就只記。這是補貨安全網，不是 mapping 引擎。
5. **#41 的 sku_id 必填若要合，是產品決策。** main 路 B 允許 name/spec certain。收緊會讓一批已核准但沒 sku_id 的列（本機約 3800 approved vs 1330 有 sku_id）突然不能補 — 必須庭安點頭，且要遷移計畫，不能藏在 AI-6。

廣告成本設計會用同一顆種子庫的 `price_cny` 做毛利輔助（[`ads_cost_margin_design.md`](ads_cost_margin_design.md)）。那是**另一條**、尚未實作的線；mapping 閉環只借用「種子 offer 當第 1 層來源」，不把成本接到 ROAS。

## 5. 驗證碼／死頁／多 offer

### 驗證碼

- 已知 URL 的 `fetch`：標 `waiting_for_login`，保留 ego task space，不自動登入。
- 站內搜尋（AI-4 3B）：比詳情頁容易 punish。PR #45 多次 resume 都是 captcha 就 STOP，留下 resume JSON，**不繞 WAF**。
- 策略（解暫停後仍適用）：搜尋當**最後備援**且預設關閉；captcha 列不進必審主佇列（AI-5 已這樣）；人清牆後再 resume，不當「找不到」。

### 死頁／下架

- 分類器：`wrongpage.html`、文案「商品不存在／已下架」→ `health_status=invalid`。
- AI-2 補充：很多死 offer 改導向 `www.1688.com?spm=…notfound`，舊規則若只認 `wrongpage.html` 會漏。
- 未登入 HTTP 在刀 1 全是 `_____tmd_____/punish` — **不可**當活／死。
- 死頁下一步：第 1 層從種子／兄弟檔另提 offer，**不要**自動改 golden URL。舊列標 `stale`／`suspected_discontinued`，等人選新 URL 再掃第 2 層。

### 多 offer

- 產品層多店**不是**自動擋（#41 與 handoff 一致）。不同型號可訂不同店。
- 同一型號兩個來源（種子 vs 兄弟檔）頂 offer 不同 → `Conflict Rate`，必須進人工（spike 成功指標）。
- 同一 `(offerId, skuId)` 多車列 → `reverse_audit` 整 key 進 `ambiguous.csv`，不拆量。
- 禁止：用名稱／圖片把 `unresolved_id` 併成真實 `sku_id`。

## 6. 解暫停之後的路線圖

必須先有庭安一句「Golden 全表工程可繼續」，再談寫入。下面順序固定。

### 階段 R — 審 88，仍不寫主表

- 用 #46 的 `docs/golden_ai5_20260909/golden_ai5_review.html`（或解暫停後把靜態檔 checkout 出來）。
- 人做核准／駁回／改換／略過／停售；先留在 AI-5 本機紀錄。
- **閘門：** 主表 SHA 不變。captcha／no_candidate 不塞進這批必審。

### 階段 W — 受控寫入（每次少量、可撤回）

- 只把階段 R **人點過**的列，經現有 `POST /api/sku-mapping/decisions` 寫入（必要時先合 #41 的 `WRITE_GOLDEN` 片語 — **另開決策，不當默認**）。
- 每批 allowlist（productId+specId），類似 KB 的 `--allow-order-ids`。
- 寫前備份檔名已有 `golden_table.json.backup_before_sku_review_*`；寫後對 SHA 與 `git diff -- golden_table.json`。
- **不做 AI-6：** 模型不得在無人決策列上寫 URL／skuId。
- **閘門：** `auto_approve.enabled` 仍 false。

### 階段 E — 評估迴圈

- 對新核准列跑 `python -m mapping_eval`（本機 `procurement.db`，報告在 gitignore 的 `data/mapping_eval/`）。
- 看 Green Precision、FN、反事實 `n_would_pass`。官方 fixture 仍是 5 案；真實 DB 才是能不能談開閘的分母。
- 負例寫進 `mapping_negative_examples`（原因代碼 SPEC 5.3）。
- **閘門：** 沒有「會過關集合非空且精度 100%＋一鍵撤回」以前，不開 auto-approve（Know-how 文件 §為何仍然關閉）。

### 階段 X — 擴大（建議層已落地；仍要人確認 URL）

來源 1（唯讀種子）與來源 2（兄弟檔）已在 #113 落地：`python -m offer_discovery`／`GET /api/sku-mapping/offer-suggestions`（CI fixture：`tests/fixtures/offer_discovery/`，不提交 `.db`）。見 [`offer_discovery.md`](offer_discovery.md)。

1. **人確認 URL。** 第 1 層只出建議；**不要**自動填 `阿里巴巴商品URL`、**不要**寫 Golden。
2. 只對 `no_url`（945）與 AI-2 確認死掉的 `url_suspect` 出建議，等人選 URL。
3. 站內搜尋維持不做或極窄備援。
4. 再回到階段 R→W，批次放大（仍須庭安解暫停）。

### 永遠由人做

- 第一次採用某顆 offer URL。
- 驗證碼／登入牆。
- 種子 vs 兄弟檔衝突。
- 死頁要換店（不是同一 offer 換 sku）。
- 標停售、標無匹配、`OTHER` 負例說明。
- 任何 `auto_approve.enabled=true`（產品契約；沒有 UI 開關）。
- 付款、清車、把歷史單當入庫 SoT。

## 7. 暫停期間的非目標

在庭安解暫停之前，下列**不要做**（文件 PR 也不要建議「順便做」）：

- 合併 #41、#42、#43、#44、#45、#46，或 cherry-pick 它們的候選 CSV 當真相。
- AI-6 或任何機器人批次寫 `golden_table.json`／`golden_table.json.backup*`。
- 把 `auto_approve.enabled` 設成 true，或加 UI toggle。
- 重跑／接續 AI-4 站內搜尋、AI-2 全表探活、AI-1 之後的寫入。
- 把 `kb_excel_success_isolated_20260912.db` 的 3309 筆灌進 live `procurement.db`。
- 開 `PHASE3_*`、對 1688 打 list／search POST。
- 新寫 production 1688 搜尋爬蟲。
- 改 golden schema、用相似度合併 `unresolved_id`。
- 讓 Know-how 綠燈直接等於補貨 certain。
- 為了「完成表」而放寬 reverse_audit「不發明 URL／skuId」。

暫停期間**可以**做（與本審查同類）：文件、fixture 評估、Know-how 規則層不寫 golden 的測試、Goal A 唯讀應補摘要、廣告成本**設計**（不接 live、不動 ROAS）。

## 8. 與 Goal A 的交界

補貨閉環（[`restock_closed_loop_review.md`](restock_closed_loop_review.md)）吃的是 **already-approved** 列。Golden 暫停表示：閉環可以對 certain 列加車／對帳，但**不能**靠 AI 把 945 筆 `no_url` 變可補。錯 mapping 的風險在 Goal A §5；解法是階段 W 的受控寫入，不是在 restock 路徑猜 SKU。
