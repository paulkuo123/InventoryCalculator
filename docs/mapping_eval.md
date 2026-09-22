# 1688 SKU mapping 離線評估（TASK 1）

`python -m mapping_eval` 是既有 `SkuMappingService` 的離線 baseline，**不是**第二套 mapping 引擎。它只呼叫 `generate_candidates()` 與 `classify_review_tier()`；`--ai` 才會再呼叫現有 AI judge。不會改 `golden_table.json` schema，也不會 auto-approve。

**Mapping Engine 2.3（本刀）：** 同一支 CLI 加**信心分層**與**對帳報表**。四層禁止混用（SPEC §11）：KB 來源可靠度 ≠ `final_score` ≠ `review_tier` ≠ 可自動寫 Golden（永遠 false／「不可寫」）。第 2 層指標仍是 Top-1／Top-3／Green Precision／FN／反事實 `n_would_pass`。可選 `--layer1` 對缺 URL 樣本跑 2.2 `offer_discovery`（缺 kb-db 跳過種子層、不中斷）。**不**改 matcher 打分、**不**寫 Golden、**不**灌 live、**不**開 `auto_approve`。死連標註是另刀，不在本報告。

報告寫到 `data/mapping_eval/`（已 gitignore）或 `--out`。**不要提交** `procurement.db`、`.env.local`、cookies 或評估報告。

## 本機 baseline（真實 DB）

從 repo 根目錄、已有本機 `procurement.db` 與 `golden_table.json`：

```bash
python -m mapping_eval run \
  --db-path procurement.db \
  --golden-path golden_table.json \
  --out data/mapping_eval/baseline/ \
  --sample 500 \
  --seed 42
```

可選 AI（會先印預估呼叫次數；互動環境需確認，CI 非互動則只要明確 `--ai` 且遵守 `--ai-limit`，不會卡住）：

```bash
python -m mapping_eval run \
  --db-path procurement.db \
  --out data/mapping_eval/baseline-ai/ \
  --sample 500 \
  --seed 42 \
  --ai \
  --ai-limit 50
```

比對兩次 run：

```bash
python -m mapping_eval compare \
  --baseline data/mapping_eval/A \
  --candidate data/mapping_eval/B
```

資料來源：`golden_table.json` 中 `1688_mapping_status == "approved"`，且該 `1688_offer_id` 在 `alibaba_offer_snapshots` 有 `status='ok'` 的列。Ground truth 是 `1688_sku_name` + `1688_sku_second_name`（或以 `1688_sku_id` 對上候選）。評估時會隱藏答案，只給 `product_name`、`model_name` 與該 offer 的 SKU 清單。名稱比對走共用 `normalize_text`：結尾的 `>`／`&gt`／全形 `＞` 會先轉成逗號再剝掉，避免快照 `奶白綠野千鸟格>` 對不上 Golden `奶白 绿野千鸟格`；中間的 `>` 仍當欄位分隔。裝飾用括號（【】等）、表情符號與結尾句點會去掉；包住的「單殼／裸殼」註記也去掉，但 `45mm裸殼` 這種尺寸保留。顏色／款式另做結構對齊（同一字的簡繁，含髮帶折成发帶、麵包折成面包，以免動到鏡面；邊色色相、刺繡貼前的色名、一字縮寫如海棠粉／棠粉、連字號詞序）。掛繩／組合後綴（含寫在型號欄的 `苹果16+掛繩`，以及款式欄的 `單殼` vs `殼+掛繩`）只在**同一機型 token** 有裸殼兄弟時降權；型號沒點名掛繩才降組合款，點名掛繩則保留組合款。沒有裸殼兄弟的唯一組合不降分，避免唯一候選掉出綠燈。這不改 iPhone base／Pro／Pro Max 層級，也不把 exact `16+掛繩` 降到斜線 `16/16plus` 之下，也不把別名寫成單一商品色名，也不把白邊與黑邊、奶酪白與豆粉色併成同色。

合後兩段驗證（**不寫 Golden、不開 auto_approve**；分母以產出 `run_meta.n_input`／`n_scorable` 為準）：

1. 同一 `--sample 500 --seed 42` 對帳 #116 後基線。
2. **省略 `--sample`**，跑全部核准且 snapshot `ok` 的可評集（Golden 核准且有 URL 約 3800 列；有 ok 快照的才進 `n_input`）。

```bash
python3 -m mapping_eval run \
  --db-path procurement.db \
  --golden-path golden_table.json \
  --kb-db <isolated> \
  --seed 42 \
  --out data/mapping_eval/full-approved/
```

缺少 `procurement.db` 時結束碼為 **2**。

#118 之後「單殼 vs 掛繩／組合」這一簇大約 near 72 + FN 35（約 107 筆；分堆報告不在 repo）。合進 main 之後用同一 seed 重跑（**不寫 Golden、不開 auto_approve**）。`--baseline` 指到 `1540d45` 那次 sample500 與全表目錄；目錄名不同就改路徑。沒有 baseline 就不要宣稱 Top-1／FN／綠燈變好。看這一簇是否減少；若只救 1–2 筆或綠燈變差，縮回兄弟規則。

```bash
python3 -m mapping_eval run \
  --db-path procurement.db \
  --golden-path golden_table.json \
  --sample 500 \
  --seed 42 \
  --out data/mapping_eval/after-bare-shell-sample500/

python3 -m mapping_eval compare \
  --baseline data/mapping_eval/after-118-sample500/ \
  --candidate data/mapping_eval/after-bare-shell-sample500/

python3 -m mapping_eval run \
  --db-path procurement.db \
  --golden-path golden_table.json \
  --seed 42 \
  --out data/mapping_eval/after-bare-shell-full/

python3 -m mapping_eval compare \
  --baseline data/mapping_eval/after-118-full/ \
  --candidate data/mapping_eval/after-bare-shell-full/
```

#119 合進 main 之後，裸殼對掛繩的綠燈合成測通過，但全表 Top-1／FN 相對 #118 **沒有移動**。不要再加寬同一條兄弟降權。#120 已落地（包裝數量／丹尼／毛絨）；現行說明是 #121 的夾片／掛繩套話與獨立尺碼字母，見 [`docs/ai_reviewed_layer2.md`](ai_reviewed_layer2.md)。比較色名時會剝掉 `(一顆)`／`(一粒)` 以及黏在色名後的 `單個`（標籤還在才剝）；這不把 `遠峰藍閃粉` 併成 `黑色閃粉`，也不改機型層級。

## CI／無本機 DB（fixture）

```bash
python -m mapping_eval run \
  --fixture tests/fixtures/mapping_eval \
  --out /tmp/mapping_eval_fixture
```

或指定單一檔：

```bash
python -m mapping_eval run \
  --fixture tests/fixtures/mapping_eval/cases.json \
  --out /tmp/mapping_eval_fixture
```

測試：

```bash
python -m unittest tests.test_mapping_eval tests.test_sku_mapping_service
```

## 類別分組

`mapping_knowledge_pack/categories.json` 是商品名稱關鍵字對類別的對照（TASK 3）。評估與 alias 類別過濾共用這份對照。檔案支援 `{"rules":[{"category":"socks","keywords":["襪"]}]}` 或 `{"socks":["襪"]}`。缺檔或無效時改用商品名稱啟發式（`手機殼`／`袜`／`錶`／`吊飾` 等，否則 `other`），評估不會失敗。預設檔的類別 id 與 TASK 1 啟發式相同（`phone_case`／`watch`／`socks`／`charm`），因此 fixture 分組標籤不變。

## TASK 2 設定（thresholds／weights）

`mapping_knowledge_pack/config.json` 外置門檻與權重；`mapping_knowledge.py::load_config()` 讀檔。檔案缺失或 JSON 無效時改用 `sku_mapping_service` 既有常數（`AI_GREEN_CONFIDENCE_THRESHOLD=0.95`、`AI_VERIFIED_GREEN_CONFIDENCE_THRESHOLD=0.90`、`MAX_REVIEW_CANDIDATES=4`），並在無效 JSON 時記 warning，不中斷評估或審核。`score_weights` 沒有 `semantic`；`auto_approve.enabled` 維持 `false`（本任務不啟用 auto-approve）。

預設設定必須與 TASK 1 fixture 結果 bit-for-bit 相同。覆核：

```bash
python -m mapping_eval run \
  --fixture tests/fixtures/mapping_eval \
  --out /tmp/mapping_eval_task2_defaults
python -m unittest tests.test_mapping_knowledge tests.test_mapping_eval tests.test_sku_mapping_service
```

比對 `/tmp/mapping_eval_task2_defaults/metrics.json` 與 TASK 1 fixture 基線：`n_cases=5`、`n_scorable=4`、Top-1 `50.0%` (2/4)、Top-3 `75.0%` (3/4)、FN `25.0%` (1/4)、Green precision `100.0%` (2/2)。

## TASK 3 規則／同義詞資料化

- `mapping_knowledge_pack/aliases.json`：由 `python -m mapping_knowledge seed-aliases` 從 `COLOR_SYNONYMS` 匯出（`category: "*"`，不得手抄）。`_synonym_equal()`／`_color_match_rank()` 讀 alias（含類別過濾）；缺檔回退 `COLOR_SYNONYMS`。
- `mapping_knowledge_pack/rules.json`：登錄既有函式（RULE-0001 size／RULE-0002 phone／RULE-0003 alphanumeric code／RULE-0004 parenthetical noise）。`impl` 指向既有函式名，不是 DSL。`status: "disabled"` 的規則不會生效。
- `generate_candidates()` 在剔除或命中時把 `(rule_id, effect)` 寫入候選 `evidence.applied_rules`，並在 `_save_suggestion()` 寫入 `mapping_rule_hits`。
- 預設（全部 active + seed aliases）必須與 TASK 1／2 fixture 結果一致。覆核：

```bash
python -m mapping_eval run \
  --fixture tests/fixtures/mapping_eval \
  --out /tmp/mapping_eval_task3_defaults
```

Headline 數字應仍為：`n_cases=5`、`n_scorable=4`、Top-1 `50.0%`、Top-3 `75.0%`、FN `25.0%`、Green precision `100.0%`。其中 `watch-blue-45` 是尺寸 FN（RULE-0001 剔除 45mm vs 49mm）。

## TASK 4 負例與原因代碼

`mapping_negative_examples`（SPEC 5.2）由 `SkuMappingService._init_db()` 建立，不改 `golden_table.json` schema。共用原因代碼（SPEC 5.3）在 `mapping_knowledge.NEGATIVE_REASON_CODES`：`MODEL_MISMATCH`／`SIZE_MISMATCH`／`COLOR_MISMATCH`／`VERSION_MISMATCH`／`PACKAGE_QTY_MISMATCH`／`LOOKALIKE_DIFFERENT`／`DISCONTINUED`／`OTHER`（OTHER 必須填 `reasonText`）。

`_apply_decision()` 寫入三種 origin：

- `no_match`：該 suggestion 的所有候選（`reason_code` 必填；缺省時相容舊呼叫記為 OTHER＋「人工標記無匹配」）
- `approve`／`replace` 且 AI `suggested_candidate_key` ≠ 人工選擇：只寫 AI 建議候選（`chose_other_candidate`）
- `reject_candidate`：只否決單一候選，不改 suggestion 狀態（`explicit_reject`）

API：`POST /api/sku-mapping/decisions` 接受 `reasonCode`／`reasonText`；OTHER 無說明 → 400。`GET /api/sku-mapping/negative-examples?productId&modelId` 或 `?offerId`。審核 `after_json` 含 `negative_example_ids`。negative-examples API 仍可直接使用，但目前審核 UI 不會呼叫；UI 的審核資料走 summary／queue。

## TASK 5 歷史正負例進入 judging

`SkuMappingService.historical_support()` 把 Golden 已核准列（可選同 DB `kb_mappings`，以及可選隔離 Mapping KB：`--kb-db`／`MAPPING_KB_DB`）餵進既有 `generate_candidates()`／AI judge，**不是**第二套引擎、不改 ranking 公式、不啟用 auto-approve。隔離 `kb_name_positives` 只當名稱組合支持訊號，不發明 `sku_id`。指向方式見 [`mapping_kb_isolated_import.md`](mapping_kb_isolated_import.md)。

- **Same-offer**：同一 offer 上**其他**已核准型號的 `model_name ↔ 1688_sku_name` 作為命名慣例證據。
- **Cross-offer**：`normalize_text(model_name)` 相同的過去核准 `1688_sku_name`。
- 每個候選附 `historical_support_count` 與最多 5 筆 example summaries。目前列自己的核准答案不會算進去（避免評估洩漏）。
- **負例閘門**：`(product_id, model_id, offer_id, candidate_key)` 命中 `mapping_negative_examples` → 剔除候選並記 `rule_type='negative'`。同一 `(offer_id, candidate_key)` 但**不同型號**不剔除，只進 prompt context。
- LLM payload（`_request_structured_ai`／Gemini／DeepSeek）新增 `historical_examples`、`negative_examples`、`applied_rules`。`_ai_system_text`：歷史人工核准優先於相似度；負例中的候選不得選。`PROMPT_VERSION = "2026-09-v2"` 寫入 `evidence_json.ai.prompt_version`。

預設 fixture 數字必須 ≥ TASK 1／2／3／4 基線（isolated eval 的 golden／負例是空的，規則層結果不變）：

```bash
python -m mapping_eval run \
  --fixture tests/fixtures/mapping_eval \
  --out /tmp/mapping_eval_task5_defaults
```

Headline：`n_cases=5`、`n_scorable=4`、Top-1 `50.0%` (2/4)、Top-3 `75.0%` (3/4)、FN `25.0%` (1/4)、Green precision `100.0%` (2/2)。

無 API key 時用 dry-run 檢查 prompt 欄位（不呼叫供應商）：

```bash
python -m mapping_eval run \
  --fixture tests/fixtures/mapping_eval \
  --out /tmp/mapping_eval_task5_ai_dry \
  --ai-dry \
  --ai-limit 50
```

產出 `ai_dry_payloads.jsonl`，每列含 `prompt_version`、`historical_examples`、`negative_examples`、`applied_rules`、`historical_support_counts`。本機有 key 時才跑真 AI：

```bash
python -m mapping_eval run \
  --db-path procurement.db \
  --golden-path golden_table.json \
  --out data/mapping_eval/task5-ai/ \
  --sample 50 \
  --seed 42 \
  --ai \
  --ai-limit 50 \
  --yes
```

比對欄位見 `python -m mapping_eval compare`：`top1_accuracy`、`green.precision`、以及（若有 AI）`ai.match_precision`／`ai.abstain_rate`／`ai.ai_wrong_det_right`／`ai.det_wrong_ai_right`。不要把 key 或 `procurement.db` 提交進 git。

## TASK 6 複合分數與 explain

`final_score = Σ weight_i × component_i`，分量皆在 0–1：

- `feature`：`deterministic_score / 100`（截斷）
- `historical`：`historical_support_count` 飽和（3 筆 → 1.0）
- `rule`：沒有 soft-rule penalty 為 1，否則 0
- `llm`：AI 信心；`abstain` 或未選中為 0

權重來自 `mapping_knowledge_pack/config.json` 的 `score_weights`。硬閘門剔除的候選不計分。`final_score` **只**用來排候選與黃燈佇列，**不**改 `classify_review_tier` 的綠色條件。

`sku_mapping_suggestions` 以 `ALTER ADD` 補 `final_score`、`score_breakdown_json`。`SkuMappingService.explain(product_id, model_id)` 與 `GET /api/sku-mapping/explain?productId&modelId` 回傳 `decision`、`selected_candidate`、`why[]`（`rule`／`alias`／`historical`／`negative`／`feature`／`llm`）、`score_breakdown`、`knowledge_version`。explain API 仍可直接使用，但目前審核 UI 不會呼叫；UI 顯示的審核資料來自 `GET /api/sku-mapping/summary` 與 `GET /api/sku-mapping/queue`。

預設 fixture 數字必須 ≥ TASK 1–5 基線（綠色條件不變，應持平）：

```bash
python -m mapping_eval run \
  --fixture tests/fixtures/mapping_eval \
  --out /tmp/mapping_eval_task6_defaults
```

Headline：`n_cases=5`、`n_scorable=4`、Top-1 `50.0%` (2/4)、Top-3 `75.0%` (3/4)、FN `25.0%` (1/4)、Green precision `100.0%` (2/2)。

停用 RULE-0001 後，同一 fixture 的尺寸 FN 必須可觀測地下降（`watch-blue-45` 不再被剔除）：

```bash
# 見 tests.test_mapping_knowledge.DisabledRuleTests
python -m unittest tests.test_mapping_knowledge.DisabledRuleTests tests.test_mapping_knowledge.FixtureParityTests
```

## TASK 7 建議證據可稽核

每次 `_save_suggestion()` 寫入的 `evidence_json` 都必須含：

- `knowledge_version`（知識包 SHA-256；欄位也以 `ALTER ADD` 寫入 `sku_mapping_suggestions.knowledge_version`）
- `prompt_version`
- `ai.provider`／`ai.model`／`ai.effort`（有跑 AI 時寫入實際值；沒跑 AI 時明確寫 `null`）
- `applied_rules`
- `historical_support`
- `negative_hits`
- `score_breakdown`
- `snapshot_id`
- `fingerprint`

舊列可以不完整。稽核只計「缺 key」，`null` 不算缺：

```bash
python -m mapping_eval audit --db-path procurement.db --since 7
```

`--since 0` 掃描全部列。可選 `--out` 寫 `audit.md`／`audit.json`。新寫入必須 `missing any required field: 0`。

```bash
python -m unittest tests.test_mapping_evidence_audit
```

本任務不改 `golden_table.json` schema、不啟用 auto-approve、不做 TASK 8–9。

## TASK 8 對照評估與 auto-approve 反事實（不啟用）

總覽與架構見 [`sku_mapping_knowhow_engine.md`](sku_mapping_knowhow_engine.md)。本任務只重跑 fixture 評估、用 `compare` 對 TASK 1 baseline，以及**報告** SPEC 5.1 auto-approve 精度。`auto_approve.enabled` 維持 `false`，沒有 UI toggle，不改 Golden schema。第 1 層（TASK 9／2.2）預設不進本節的 layer-2 fixture 評估；可選 `--layer1` 對缺 URL 樣本跑 `offer_discovery`（見下方「Mapping Engine 2.3」；預設 fixture 不變）。設計依據仍是 [`offer_discovery_spike.md`](offer_discovery_spike.md)。

凍結的 TASK 1 fixture 指標：`tests/fixtures/mapping_eval/task1_baseline/metrics.json`。

```bash
python -m mapping_eval run \
  --fixture tests/fixtures/mapping_eval \
  --out /tmp/mapping_eval_task8_current
python -m mapping_eval compare \
  --baseline tests/fixtures/mapping_eval/task1_baseline \
  --candidate /tmp/mapping_eval_task8_current
python -m unittest tests.test_mapping_auto_approve_eval
```

Headline 與 TASK 1 持平：`n_cases=5`、`n_scorable=4`、Top-1 `50.0%` (2/4)、Top-3 `75.0%` (3/4)、FN `25.0%` (1/4)、**Green precision `100.0%` (2/2)**。

`run` 另寫 `auto_approve.json`（反事實）。fixture 上 SPEC 5.1 全閘：**0** 案會自動過關，精度 **n/a (0/0)**。`enabled` 仍為 `false`。

## Mapping Engine 2.3：信心分層＋對帳

同一引擎、同一 `run`／`compare`。報告多寫 `confidence_layers.json`（以及可選 `layer1.json`），`summary.md` 有四層對帳表。**不**另做 matcher。

### 怎麼跑

```bash
# 第 2 層 Know-how fixture（CI；含四層信心，不含站內搜）
python -m mapping_eval run \
  --fixture tests/fixtures/mapping_eval \
  --out /tmp/mapping_eval_2_3

# 可選：第 1 層缺 URL 切片（2.2 offer_discovery；缺 kb-db 跳過種子層）
python -m mapping_eval run \
  --fixture tests/fixtures/mapping_eval \
  --layer1 \
  --layer1-golden tests/fixtures/offer_discovery/golden.json \
  --kb-db /workspace/_handoff/mapping_kb_isolated_20260920.db \
  --out /tmp/mapping_eval_2_3_layer1

python -m mapping_eval compare \
  --baseline tests/fixtures/mapping_eval/task1_baseline \
  --candidate /tmp/mapping_eval_2_3

python -m unittest tests.test_mapping_eval tests.test_mapping_eval_layering tests.test_mapping_auto_approve_eval
```

隔離庫路徑僅本機 smoke 提及，可缺：`/workspace/_handoff/mapping_kb_isolated_20260920.db`。CI 用 fixtures，不要提交營運 `.db`。

### 四層信心（禁止混用）

| 層 | 報告欄 | 意思 | 本刀做什麼 |
|---|---|---|---|
| 1. KB 來源可靠度 | `confidence_layers.kb_source_reliability` | 枚舉／rank：`golden_approved`＞`inbound_exact`／`seed_history`＞`golden_sibling`＞… | 只標註。**不**改 `score_weights`／`final_score` 公式 |
| 2. 建議 `final_score` | `confidence_layers.final_score` | 既有四分量加權，**只排序** | 讀 matcher 已算的值；不重算 |
| 3. 綠／黃／紅 | `confidence_layers.review_tier` | `classify_review_tier` 給人看的批次 | 沿用；綠仍要人核 |
| 4. 可自動寫 Golden | `can_auto_write_golden` | 寫入閘門 | **永遠 false／未開**，標「不可寫」。`n_would_pass` 是反事實「若開自動會過幾筆」，不是授權 |

一眼對帳：同一列可以同時是 `golden_approved`＋高 `final_score`＋`review_tier=green`，但第 4 層仍是 **false**。這四個值型別與角色都不同，報告表會並排列出。

Layer-2 fixture 的 `truth_source` 是 `golden_approved`（評估分母＝Golden 已核准列）。隔離服務沒有歷史正例時 `support_source=none`。這不降低真相來源，只說明 matcher 這次沒吃到歷史支持。

### 報表欄位

| 檔 | 內容 |
|---|---|
| `metrics.json` | 既有 Top-1／Top-3／FN／Green Precision，加上 `confidence_layers`、`auto_approve.n_would_pass`（`annotation=不可寫`） |
| `confidence_layers.json` | 四層定義、KB 計數、`final_score` min／mean／max、綠黃紅人數、`can_auto_write_golden.n_true`（必須 0） |
| `auto_approve.json` | 反事實閘門；`enabled=false`；`n_would_pass` |
| `summary.md` | 對帳表＋「不可寫」 |
| `layer1.json` | 僅 `--layer1`：seed vs sibling 命中率、`conflict`／`needsHuman` 比例 |

### 第 1 層切片（可選、便宜）

`--layer1` 對缺 URL 樣本呼叫既有 `offer_discovery.suggest_offers`／`SkuMappingService.suggest_layer1_offers` 同一套函式。

- **不要**站內搜、**不要** Chrome 探活。
- `--kb-db` 缺檔或不可讀 → `kb_skipped=true`、`seed_history` 為空，兄弟檔仍可算，**不中斷**。
- 預設 `--layer1-golden` 用 `tests/fixtures/offer_discovery/golden.json`（若存在）。
- 切片**不**寫 Golden／live `procurement.db`。

## Fixture 報告摘錄（無秘密）

```text
# 1688 SKU mapping evaluation

- cases: **5** (scorable 4, truth absent 1)
- Top-1 accuracy: **50.0%** (2/4)
- Top-3 accuracy: **75.0%** (3/4)
- False-negative rate (truth eliminated by hard rules / candidate generation): **25.0%** (1/4)

## Per-tier

- Green precision (Top-1 among green; core metric): **100.0%** (2/2)
- Yellow truth coverage: **100.0%** (1 cases)
- Red truth coverage: **0.0%** (2 cases)

## Confidence layers (SPEC §11 — four distinct axes)

| Layer | Role | This run |
|---|---|---|
| 1. KB source reliability | provenance enum / rank; **not** a score | golden_approved=5 |
| 2. `final_score` | ranking only | mean=0.440 min=0.410 max=0.500 |
| 3. `review_tier` | human batching | green=2 yellow=1 red=2 |
| 4. 可自動寫 Golden | write gate | **false / 未開**（不可寫）； n_true=0 |

## Auto-approve counterfactual (not enabled)

- would auto-pass if enabled (`n_would_pass`): **0** / 5 — still **不可寫**
```

數字來自 `tests/fixtures/mapping_eval/cases.json` 的規則層結果；本機再跑 fixture 指令即可覆核。四層在表上不相等：KB 是枚舉、分數是 0.41–0.50、分級是綠黃紅、寫入閘門永遠 false。
