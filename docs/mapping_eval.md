# 1688 SKU mapping 離線評估（TASK 1）

`python -m mapping_eval` 是既有 `SkuMappingService` 的離線 baseline，**不是**第二套 mapping 引擎。它只呼叫 `generate_candidates()` 與 `classify_review_tier()`；`--ai` 才會再呼叫現有 AI judge。不會改 `golden_table.json` schema，也不會 auto-approve。

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

資料來源：`golden_table.json` 中 `1688_mapping_status == "approved"`，且該 `1688_offer_id` 在 `alibaba_offer_snapshots` 有 `status='ok'` 的列。Ground truth 是 `1688_sku_name` + `1688_sku_second_name`（或以 `1688_sku_id` 對上候選）。評估時會隱藏答案，只給 `product_name`、`model_name` 與該 offer 的 SKU 清單。

缺少 `procurement.db` 時結束碼為 **2**。

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

`mapping_knowledge/categories.json` 是商品名稱關鍵字對類別的對照（TASK 3）。評估與 alias 類別過濾共用這份對照。檔案支援 `{"rules":[{"category":"socks","keywords":["襪"]}]}` 或 `{"socks":["襪"]}`。缺檔或無效時改用商品名稱啟發式（`手機殼`／`袜`／`錶`／`吊飾` 等，否則 `other`），評估不會失敗。預設檔的類別 id 與 TASK 1 啟發式相同（`phone_case`／`watch`／`socks`／`charm`），因此 fixture 分組標籤不變。

## TASK 2 設定（thresholds／weights）

`mapping_knowledge/config.json` 外置門檻與權重；`mapping_knowledge.py::load_config()` 讀檔。檔案缺失或 JSON 無效時改用 `sku_mapping_service` 既有常數（`AI_GREEN_CONFIDENCE_THRESHOLD=0.95`、`AI_VERIFIED_GREEN_CONFIDENCE_THRESHOLD=0.90`、`MAX_REVIEW_CANDIDATES=4`），並在無效 JSON 時記 warning，不中斷評估或審核。`score_weights` 沒有 `semantic`；`auto_approve.enabled` 維持 `false`（本任務不啟用 auto-approve）。

預設設定必須與 TASK 1 fixture 結果 bit-for-bit 相同。覆核：

```bash
python -m mapping_eval run \
  --fixture tests/fixtures/mapping_eval \
  --out /tmp/mapping_eval_task2_defaults
python -m unittest tests.test_mapping_knowledge tests.test_mapping_eval tests.test_sku_mapping_service
```

比對 `/tmp/mapping_eval_task2_defaults/metrics.json` 與 TASK 1 fixture 基線：`n_cases=5`、`n_scorable=4`、Top-1 `50.0%` (2/4)、Top-3 `75.0%` (3/4)、FN `25.0%` (1/4)、Green precision `100.0%` (2/2)。

## TASK 3 規則／同義詞資料化

- `mapping_knowledge/aliases.json`：由 `python -m mapping_knowledge seed-aliases` 從 `COLOR_SYNONYMS` 匯出（`category: "*"`，不得手抄）。`_synonym_equal()`／`_color_match_rank()` 讀 alias（含類別過濾）；缺檔回退 `COLOR_SYNONYMS`。
- `mapping_knowledge/rules.json`：登錄既有函式（RULE-0001 size／RULE-0002 phone／RULE-0003 alphanumeric code／RULE-0004 parenthetical noise）。`impl` 指向既有函式名，不是 DSL。`status: "disabled"` 的規則不會生效。
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

API：`POST /api/sku-mapping/decisions` 接受 `reasonCode`／`reasonText`；OTHER 無說明 → 400。`GET /api/sku-mapping/negative-examples?productId&modelId` 或 `?offerId`。審核 `after_json` 含 `negative_example_ids`。

## TASK 5 歷史正負例進入 judging

`SkuMappingService.historical_support()` 把 Golden 已核准列（可選 `kb_mappings`）餵進既有 `generate_candidates()`／AI judge，**不是**第二套引擎、不改 ranking 公式、不啟用 auto-approve。

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

權重來自 `mapping_knowledge/config.json` 的 `score_weights`。硬閘門剔除的候選不計分。`final_score` **只**用來排候選與黃燈佇列，**不**改 `classify_review_tier` 的綠色條件。

`sku_mapping_suggestions` 以 `ALTER ADD` 補 `final_score`、`score_breakdown_json`。`SkuMappingService.explain(product_id, model_id)` 與 `GET /api/sku-mapping/explain?productId&modelId` 回傳 `decision`、`selected_candidate`、`why[]`（`rule`／`alias`／`historical`／`negative`／`feature`／`llm`）、`score_breakdown`、`knowledge_version`。審核卡可展開「為什麼」。

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

總覽與架構見 [`sku_mapping_knowhow_engine.md`](sku_mapping_knowhow_engine.md)。本任務只重跑 fixture 評估、用 `compare` 對 TASK 1 baseline，以及**報告** SPEC 5.1 auto-approve 精度。`auto_approve.enabled` 維持 `false`，沒有 UI toggle，不改 Golden schema，不做 TASK 9。

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
```

數字來自 `tests/fixtures/mapping_eval/cases.json` 的規則層結果；本機再跑 fixture 指令即可覆核。
