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
| 開關開，mtop 錯（未登入／簽章／風控／缺 specId／Ultron 包） | **fail-closed**：該筆失敗，**不**偷偷改回 DOM 點擊（避免重複加車）。stderr／結果 JSON 會寫「卡在…」。 |
| 要回 DOM | 關掉開關再跑。**不要**指望 mtop 失敗後自動點頁面。 |

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
