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

停用 RULE-0001 後，同一 fixture 的尺寸 FN 必須可觀測地下降（`watch-blue-45` 不再被剔除）：

```bash
# 見 tests.test_mapping_knowledge.DisabledRuleTests
python -m unittest tests.test_mapping_knowledge.DisabledRuleTests tests.test_mapping_knowledge.FixtureParityTests
```

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
