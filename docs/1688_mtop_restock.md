# 1688 整頁補貨／mutate 改走 mtop HTTP（Phase 3）

**Phase 3 = 把既有路 A 整頁加車、路 B reverse_audit mutate 接到 Phase 1/2 signed mtop。**  
預設仍是 DOM／CDP 點擊（安全後備）。要改走 HTTP，必須**明確打開開關**。

開關**不是核准**。真的 POST 仍須既有 `--i-approve-*`／`--i-approve-watchlist-restock`。

禁止：結算／付款／清空整車／寫 `golden_table`／`auto_approve`。Session 只從已登入 Chrome `connect_over_cdp`（或 restocker 已開著的 page cookies）來；**不新開、不殺掉 Chrome**。

Phase 1 讀車：[`docs/1688_mtop_read_cart.md`](1688_mtop_read_cart.md)。  
Phase 2 one-op CLI：[`docs/1688_mtop_mutate.md`](1688_mtop_mutate.md)。

## 怎麼打開

| 開關 | 預設 | 作用 |
|------|------|------|
| 環境變數 `ALIBABA_RESTOCK_VIA_MTOP=1` | 關 | 路 A restocker 與路 B mutate 改走 mtop |
| CLI `--via-mtop` | 關 | 同上；會寫入環境變數給子行程 |

關掉（回 DOM）：不要帶 `--via-mtop`，並 `unset ALIBABA_RESTOCK_VIA_MTOP`。

`1`／`true`／`yes`／`on` 為開；缺值、`0`、`false`、`off` 為關。

## 核准旗標（仍必帶）

| 路徑 | 仍必帶 | mtop 做什麼 |
|------|--------|-------------|
| 路 A 觀察清單整頁加車 | `--i-approve-watchlist-restock` | 每 SKU 一次 `addcargo`（不要 DOM 點「加采购车」） |
| 路 B 加車 | `--i-approve-mutate` | `missing_to_add.csv` → addcargo |
| 路 B 改量 | `--i-approve-set-qty` | Ultron `async` 設絕對量 |
| 路 B 刪列 | `--i-approve-remove` | Ultron `deleteClick` |

`--via-mtop` **不**隱含上述任何旗標。沒核准就不 POST。

## 指令

### 路 A（整頁補貨）dry／核准

```bash
# 預設：DOM 點擊（開關關）
python scripts/run_watchlist_restock.py --i-approve-watchlist-restock

# 改走 mtop；仍須核准旗標才會 POST /api/alibaba-restock/batches
export ALIBABA_RESTOCK_VIA_MTOP=1
# 或：
python scripts/run_watchlist_restock.py --via-mtop --i-approve-watchlist-restock

# 沒核准：只印摘要，不加車（via-mtop 不會偷加）
python scripts/run_watchlist_restock.py --via-mtop
```

Restocker 子行程繼承環境變數。亦可：

```bash
ALIBABA_RESTOCK_VIA_MTOP=1 python alibaba_restocker.py \
  --input payload.json --output out.json --add-to-cart --via-mtop
```

`--add-to-cart` 仍是 restocker 自己的加車開關；launcher 路 A 要先過 watchlist 核准才會啟動它。

### 路 B（reverse_audit mutate）dry／核准

```bash
# 預設：CDP 腳本（開關關）
python -m reverse_audit mutate --date YYYYMMDD --i-approve-set-qty

# 改走 mtop；沒核准旗標仍立即拒絕、不 POST
python -m reverse_audit mutate --date YYYYMMDD --via-mtop

python -m reverse_audit mutate --date YYYYMMDD --via-mtop --i-approve-mutate
python -m reverse_audit mutate --date YYYYMMDD --via-mtop --i-approve-set-qty
python -m reverse_audit mutate --date YYYYMMDD --via-mtop --i-approve-remove
```

Session：`--cdp` 或 `ALIBABA_RESTOCK_CDP`（常見 `http://127.0.0.1:9227`）。只 attach。

獨立 one-op（Phase 2，一次一筆）仍可用：

```bash
python -m reverse_audit.mtop_mutate add --offer-id ID --spec-id SPEC --qty 1
python -m reverse_audit.mtop_mutate add --offer-id ID --spec-id SPEC --qty 1 \
  --cdp http://127.0.0.1:9227 --i-approve-add-one
```

## 何時走 DOM、何時 fail-closed

| 情況 | 行為 |
|------|------|
| 開關關（預設） | **既有 DOM／CDP**。程式行為與 Phase 3 之前相同。 |
| 開關開，且已核准 | mtop HTTP。加車後用 Phase 1 `read_cart` 對帳。 |
| 開關開，**沒有**核准旗標 | 不 POST。路 B CLI 離開碼 2；路 A 不加車。 |
| 開關開，mtop 錯（未登入／簽章／風控／缺 specId／Ultron 包／1688 API 主機不通） | **fail-closed**：該筆失敗，**不**偷偷改回 DOM 點擊（避免重複加車）。stderr／結果 JSON 會寫「卡在…」。 |
| 要回 DOM | 關掉開關再跑。**不要**指望 mtop 失敗後自動點頁面。 |

### 加車前 API 預檢與「1688 API 主機不通」

- 每次 mtop 加車（`add_items`，路 A 每次送出、路 B `missing_to_add.csv` 整批）在送任何 `addcargo` 之前，先做**一次唯讀**讀採購車（`render`，和 `mtop_read_cart` 同一支 API），結果同時當對帳基線。
- 預檢失敗 → **整批停止、0 筆加車請求**。停止原因（`stoppedReason`／`status`）：
  - `mtop_api_host_unreachable`：分頁內 `fetch` 回 `Failed to fetch`、CORS（缺 `Access-Control-Allow-Origin`）、`net::ERR_*`、逾時，或阿里入口 404 頁。訊息開頭「1688 API 主機不通」。
  - `mtop_api_preflight_failed`：主機有回應但讀車失敗（例如未知回包）。
  - 未登入／風控照舊回 `not_logged_in`／`risk_control`。
  - 這些都已登錄在 `restock_cdp.RESTOCK_STOP_REASONS`／`restock_batch.RESTOCK_RISK_STOP_REASONS`，`restock_batch` 會暫停整批。
- 預檢過了、加車途中**任一筆已送出卻沒成功**（主機不通、逾時、未登入、風控、簽章、業務錯誤）→ 後面的品項**一律不送**（`notSentCount`／`notSentItems`，每筆 `sendAttempted: false`），整批停止：
  - 主機不通／逾時 → `mtop_api_host_unreachable`（逾時另標 `requestOutcomeUnknown: true`）
  - 未登入 → `not_logged_in`；風控 → `risk_control`
  - 簽章／業務／其他 → `status: failed`、`stoppedReason: add_failed`
  - 還沒送出就失敗（例如解析不到 specId）維持原本行為：該筆失敗，繼續下一筆。
- 預檢遇到意料外的例外也乾淨停止（`mtop_api_preflight_failed`），不會拋出。
- 阿里入口 404 也可能是 API 名稱或版本錯誤，訊息會一併提示。
- 主機不通的錯誤**不再**標成「卡在業務參數／風控／Ultron 包」；那個標籤只留給 1688 有回應的業務錯誤。
- **`posted:false` 只代表程式沒走到送出成功那一行，不能證明請求沒送到 1688。** 請看每筆的 `sendAttempted`（已進入送出步驟）與 `requestOutcomeUnknown`（已嘗試送出但沒拿到可用回應，例如 CORS／逾時：那一筆可能已進採購車，以採購車對帳為準）。

skuId→specId：restocker 用當下詳情頁 HTML `skuMapOriginal`；路 B 加車用同一 parser（測試可丟 `--fixture`／本地 HTML）。解析不到就 fail-closed，不猜、不點規格當後備。

## 不會做

- 結算、付款、清空整車、批次洗加 API
- 寫 `golden_table.json`、打開 `auto_approve`
- `launch`／殺掉 Chrome（mtop session 只 `connect_over_cdp`）
- 把 cookie／token／sign 寫進 git 或未遮罩 log

## 測試

```bash
python -m unittest tests.test_mtop_restock tests.test_mtop_mutate tests.test_mtop_read_cart
```

離線、假 transport。CI 不准打 1688。
