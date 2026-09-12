# 1688 SKU Mapping Know-how Engine v1

本文件是 TASK 1–7 落地後的總覽，以及 **TASK 8** 的評估對照與 auto-approve **反事實**精度。  
**不是**第二套 mapping 引擎。**不**改 `golden_table.json` schema。**不**啟用 `auto_approve.enabled`。

**TASK 9**（第 1 層：Shopee product → 1688 offer）是設計 spike，見 [`offer_discovery_spike.md`](offer_discovery_spike.md)。結論：值得做建議層，但**先讀隔離種子歷史 Offer，不要先做站內搜尋**；本文件與本分支都不實作 crawler。

CLI 細節見 [`mapping_eval.md`](mapping_eval.md)。知識包載入見 `mapping_knowledge.py`。

## 架構

Know-how Engine 把「門檻、同義詞、硬／軟規則、類別、負例原因、歷史正負例、複合分數、建議證據」外置或可稽核，但候選生成與綠／黃／紅分級仍走既有 `SkuMappingService`。

```text
Shopee 型號 (product_name / model_name)
        │
        ▼
mapping_knowledge/          知識包（檔案 + SHA-256 knowledge_version）
  config.json               門檻、score_weights、auto_approve 閘門（enabled=false）
  aliases.json              顏色等同詞（seed_from_COLOR_SYNONYMS）
  rules.json                RULE-0001…0004 登錄（impl → 既有函式）
  categories.json           商品名稱 → 類別關鍵字
        │
        ▼
SkuMappingService.generate_candidates()
  硬規則剔除（尺寸／手機代數／英數代碼／同型號負例）
  完整匹配優先，再優先嚴格字面（strict_exact）
  historical_support（同 offer 其他核准／跨 offer 同名核准）
  final_score = Σ weight_i × component_i   ← 只排序，不改綠色條件
        │
        ▼
classify_review_tier()      綠 / 黃 / 紅（人工審核）
        │
        ├─ 可選既有 AI judge（PROMPT_VERSION=2026-09-v2）
        │
        ▼
_save_suggestion()          evidence_json 必填欄（TASK 7）
        │
        ▼
人工核准 → golden_table.json
```

離線評估 `python -m mapping_eval` 用暫存目錄建構服務，**不會寫**真實 Golden，也**不會**把建議標成已核准。

| 層 | 模組 | 角色 |
|---|---|---|
| 規則／分級 | `sku_mapping_service.py` | 唯一 matcher；`generate_candidates` / `classify_review_tier` |
| 知識包 | `mapping_knowledge/` + `mapping_knowledge.py` | 設定、aliases、規則登錄、類別、原因代碼 |
| 評估 | `mapping_eval.py` | fixture／DB baseline、`compare`、`audit`、auto-approve **反事實** |
| 審核 UI | `/sku-mapping.html` | 綠黃紅卡片、`GET /api/sku-mapping/explain` |
| 真相 | `golden_table.json` | 僅人工核准列；schema 不變 |

`auto_approve.enabled` 讀自 `config.json`，預設與現況皆為 `false`。沒有 UI 開關。TASK 8 只計算「若啟用，有多少會過閘、其中多少正確」。

## 知識檔格式

目錄：`mapping_knowledge/`。`knowledge_version()` 是該目錄檔案的穩定 SHA-256（略過 `__pycache__`／點檔）。缺檔或無效 JSON 時回退內建預設，**不中斷**審核或評估。

### `config.json`

```json
{
  "version": 1,
  "thresholds": {
    "ai_green_confidence": 0.95,
    "ai_verified_green_confidence": 0.90,
    "max_review_candidates": 4
  },
  "score_weights": {
    "feature": 0.30,
    "historical": 0.30,
    "rule": 0.20,
    "llm": 0.20
  },
  "auto_approve": {
    "enabled": false,
    "require_unique_complete_strict_match": true,
    "require_no_hard_rule_violation": true,
    "require_no_negative_example": true,
    "require_snapshot_status_ok": true,
    "min_historical_support": 1
  }
}
```

- `score_weights` **沒有** `semantic`。
- `auto_approve.enabled` 必須維持 `false`（本任務不啟用、不提供 UI toggle）。

### `aliases.json`

陣列。由 `python -m mapping_knowledge seed-aliases` 從 `COLOR_SYNONYMS` 匯出，不要手抄。

| 欄位 | 說明 |
|---|---|
| `alias_id` | 例如 `ALIAS-0001` |
| `field` | 目前為 `color` |
| `category` | `"*"` 或類別 id（過濾） |
| `terms` | 等同詞 |
| `source` | `seed_from_COLOR_SYNONYMS` |
| `status` | `active`／`disabled` |

`_synonym_equal()` 讀 alias；缺檔回退 `COLOR_SYNONYMS`。

### `rules.json`

陣列。`impl` 指向既有函式名，不是 DSL。`status: "disabled"` 不生效。

| `rule_id` | type | field | impl | 效果 |
|---|---|---|---|---|
| RULE-0001 | hard | size | `explicit_size_compatible` | 明確 mm 尺寸不同 → 剔除 |
| RULE-0002 | hard | phone_model | `phone_mismatch` | 代數／Pro／Air 衝突 → 剔除 |
| RULE-0003 | hard | product_code | `alphanumeric_code_mismatch` | 非手機英數代碼衝突 → 剔除 |
| RULE-0004 | soft | name | `ignore_parenthetical_noise` | 括號噪音 → penalty，不剔除 |

命中寫入候選 `evidence.applied_rules` 與 `mapping_rule_hits`。

### `categories.json`

物件：`{ "socks": ["襪", "袜"], ... }` 或 `{ "rules": [{"category":"socks","keywords":["襪"]}] }`。  
預設 id：`phone_case`／`watch`／`socks`／`charm`（與 TASK 1 啟發式相同）。缺檔用商品名稱啟發式，評估不失敗。

## 原因代碼表（SPEC 5.3）

`mapping_negative_examples`（SPEC 5.2）由 `SkuMappingService._init_db()` 建立。共用目錄在 `mapping_knowledge.NEGATIVE_REASON_CODES`。`OTHER` 必須填 `reasonText`，否則 API 回 400。

| 代碼 | 中文 |
|---|---|
| `MODEL_MISMATCH` | 型號不符 |
| `SIZE_MISMATCH` | 尺寸不符 |
| `COLOR_MISMATCH` | 顏色不符 |
| `VERSION_MISMATCH` | 版本不符 |
| `PACKAGE_QTY_MISMATCH` | 包裝數量不符 |
| `LOOKALIKE_DIFFERENT` | 外觀相似但不同商品 |
| `DISCONTINUED` | 已停售／下架 |
| `OTHER` | 其他（必填說明） |

寫入 origin：`no_match`（該建議全部候選）、`chose_other_candidate`（核准／取代但與 AI 建議不同）、`explicit_reject`（只否決單一候選）。

## 評估指標定義

`python -m mapping_eval run` 隱藏 Golden 答案，只給 `product_name`、`model_name` 與該 offer 的 SKU 清單，再呼叫既有 `generate_candidates()`／`classify_review_tier()`。

| 指標 | 定義 | 分母 |
|---|---|---|
| **Top-1** | 排名第 1 的候選等於真相（sku_id 或正規化名稱對） | scorable |
| **Top-3** | 真相在前 3 名 | scorable |
| **FN** | 真相在 snapshot 裡，但被硬規則／候選生成剔除（`rank is None`） | scorable |
| **Green Precision** | 被分成綠色的案例中，Top-1 正確的比例（核心安全指標） | green 案例數 |
| Yellow truth coverage | 黃燈案例中真相仍在候選裡 | yellow |
| Red truth coverage | 紅燈案例中真相仍在候選裡 | red |
| Auto-approve Precision | **反事實**：若啟用 SPEC 5.1 閘門，會自動過關的案例中有多少正確 | would-pass（0 → n/a） |

**scorable** = snapshot 含真相。`charm-bear` 的真相 SKU 不在清單 → truth absent，不進 Top-1／FN 分母。

Green 是審核分級，**不是**寫入 Golden。綠色仍要人工／批次核准。

凍結的 TASK 1 fixture 指標在 `tests/fixtures/mapping_eval/task1_baseline/metrics.json`（PR #51 規則層數字；不含 `auto_approve.*`）。

```bash
python -m mapping_eval run \
  --fixture tests/fixtures/mapping_eval \
  --out /tmp/mapping_eval_task8_current

python -m mapping_eval compare \
  --baseline tests/fixtures/mapping_eval/task1_baseline \
  --candidate /tmp/mapping_eval_task8_current
```

## Baseline vs 現況（TASK 1 vs TASK 5/6/7 堆疊）

資料：`tests/fixtures/mapping_eval/cases.json`（5 案；規則層、無 AI）。  
TASK 1 風格 baseline = 凍結的 PR #51 fixture 數字。現況 = 本分支（TASK 5 歷史正負例 + TASK 6 複合分數 + TASK 7 證據）在同一 fixture 上的 `mapping_eval run`。

| 指標 | TASK 1 baseline | 現況（TASK 5/6/7） | delta |
|---|---:|---:|---:|
| n_cases / n_scorable | 5 / 4 | 5 / 4 | 0 |
| Top-1 | **50.0%** (2/4) | **50.0%** (2/4) | 0 |
| Top-3 | 75.0% (3/4) | 75.0% (3/4) | 0 |
| FN | 25.0% (1/4) | 25.0% (1/4) | 0 |
| **Green Precision** | **100.0%** (2/2) | **100.0%** (2/2) | **0** |
| Yellow truth coverage | 100.0% (1) | 100.0% (1) | 0 |
| Red truth coverage | 0.0% (2) | 0.0% (2) | 0 |

約束：FN ≤ baseline、Top-1 ≥ baseline、Green ≥ baseline。本輪三者皆持平，**無回歸**。

逐案（與 TASK 1 `FixtureParityTests` 相同）：

| model_id | 類別 | tier | Top-1 | 說明 |
|---|---|---|---|---|
| sock-white | socks | green | 是 | 唯一完整嚴格匹配「白色」 |
| sock-khaki | socks | green | 是 | 唯一完整，但是同義／前綴，非 strict_exact |
| case-17-graphite | phone_case | yellow | 否（rank 2） | 黑色 vs 石墨黑 兩個完整候選 |
| watch-blue-45 | watch | red | FN | RULE-0001 剔除 45mm vs 49mm |
| charm-bear | charm | red | truth absent | snapshot 只有「小貓」 |

isolated eval 的 Golden／負例是空的，所以 TASK 5 歷史通道在 CI fixture 上不改變規則層數字。TASK 6 不改綠色條件。TASK 7 只寫證據。

## Auto-approve 條件與量測精度（只報告、不啟用）

SPEC 5.1 閘門即 `mapping_knowledge/config.json` 的 `auto_approve`（`enabled` 除外）。反事實 helper：`mapping_eval.assess_auto_approve_counterfactual()`。`run` 會寫 `auto_approve.json` 並在 `summary.md` 列出。**不會**把 `enabled` 設成 true，也**不會**寫 Golden。

| 條件 | 現況 | 反事實判定 |
|---|---|---|
| `enabled` | **false** | 只記錄；不參與「會不會過」 |
| `require_unique_complete_strict_match` | true | 恰好 1 個候選，且 `complete` 且 `strict_exact >= required` |
| `require_no_hard_rule_violation` | true | 真相未被硬規則剔除；剩餘候選沒有 hard reject |
| `require_no_negative_example` | true | 剩餘候選沒有同型號負例 |
| `require_snapshot_status_ok` | true | snapshot `status == "ok"`（fixture 評估固定 ok） |
| `min_historical_support` | 1 | 該唯一候選的 `historical_support_count >= 1` |

**Auto-approve Precision** = `n_correct / n_would_pass`。  
`n_would_pass` = 除 `enabled` 外全部閘門通過的案例數。  
`n_correct` = 其中唯一候選等於真相（Top-1）。

### Fixture 量測（官方 5.1，含歷史支持）

| | 數字 |
|---|---|
| `auto_approve.enabled` | **false** |
| 評估案例 | 5 |
| 若啟用會自動過關 | **0** |
| 其中正確 | **0** |
| **Auto-approve Precision** | **n/a** (0/0) |

未過閘計數：`require_unique_complete_strict_match` 4、`require_no_hard_rule_violation` 1（`watch-blue-45`）、`min_historical_support` 2（`sock-white`、`sock-khaki` 各有唯一候選但歷史支持為 0）。

`sock-white` 是唯一「unique + complete + strict」且 Top-1 正確的案例；它只卡在 `min_historical_support`（isolated fixture 沒有歷史核准）。`sock-khaki` 是綠色但不嚴格，不得 auto-pass。

### 診斷（非正式；忽略歷史支持）

若暫時拿掉 `min_historical_support`，只有 `sock-white` 會過，且正確 → 1/1 = 100%。這**不是**官方精度：樣本只有 1、沒有歷史證據，不能拿來開閘。

## 為何 auto-approve 仍然關閉

1. **產品契約**：v1 明確規定 `auto_approve.enabled` 維持 `false`，沒有 UI toggle，核准仍寫入 Golden 且只能人工。
2. **官方 fixture 精度是 0/0**：5.1 全閘下沒有任何案例會自動過。開啟等於「現在不會自動寫、但門已經打開」，之後一有歷史支持就可能寫入 Golden。
3. **歷史支持是安全核心**：isolated／CI fixture 的 Golden 是空的，測不到 `min_historical_support`。真實 DB 上還沒有本任務 commitable 的精度報告。
4. **綠色 ≠ 可自動寫入**：Green Precision 100% 只表示「標成綠色的都 Top-1 正確」，綠色仍是給人看的批次審核，不是寫檔授權。`sock-khaki` 是綠色但不是嚴格匹配。
5. **FN 仍然存在**：`watch-blue-45` 被 RULE-0001 剔除。自動過關若誤開，硬規則漏網或別名過寬都會直接污染 Golden。
6. **沒有回滾契約**：一旦寫入 `golden_table.json`，購物車與入庫會當真相用。在真實 DB 上量到「會過關的集合非空且精度 100%、且有稽核／一鍵撤回」之前，不開閘。

之後若要重評（仍不要開閘）：

```bash
python -m mapping_eval run --fixture tests/fixtures/mapping_eval --out /tmp/mapping_eval_task8_current
# 看 auto_approve.json：enabled 必須是 false；n_would_pass / precision 是反事實
```

本機有 `procurement.db` 時可用真實 DB run 再看反事實數字；報告在 `data/mapping_eval/`（gitignore），不要提交 DB、key 或報告。

第 1 層 offer 從哪裡來（種子庫 → 同商品 Golden → 最後才站內搜尋）不在本評估範圍，見 [`offer_discovery_spike.md`](offer_discovery_spike.md)。
