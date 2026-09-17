# 1688 mtop mutate（Phase 2：加車／改量／刪列）

**Phase 2 = 帶簽章的 H5 HTTP mutate 骨架。預設 dry-run，只組 payload、不 POST。**

要真的打 1688，必須由操作者在**已登入的本機**加上對應的 **one-op 核准旗標**。CI／cloud Linux **必須保持離線**（`--dry-run` 形狀即可；單元測試用假 transport）。

庭安已 OK 開這個 mutate HTTP PR；**請不要自行 merge**（改車爆破半徑大，等 Grok 跟庭安確認）。

## 硬門檻

| 會 | 不會 |
|---|---|
| 預設只組／印**遮罩後** payload | 沒有核准旗標就 POST |
| 加車一次：`addcargo/1.0` | 批次加車、觀察清單洗車 |
| 改量一次／刪列一次：同一個 Ultron `astoreservice.async/1.0` | 結算、付款、清空整車 |
| Session／sign **重用 Phase 1**（`mtop_session`／`mtop_sign`；1688 cookie domain 優先） | 跟 Open Platform AOP stub（`alibaba_client.py`）混用 |
| skuId→specId：唯讀解析詳情頁 HTML `skuMapOriginal` | 寫 `golden_table`、auto_approve、Golden #41–#46 |
| 改量／刪列：從 Phase 1 **render 整包** Ultron model clone（`endpoint`／`operator`／`linkage`／`data`／`hierarchy`），只 patch 目標 `item_{cartId}` | 憑空發明 endpoint／linkage／hierarchy；只送一顆 item 的最小包（live 會 `SYSTEM_ERROR::null`） |
| `--dry-run`／`--fixture` 離線（CI） | 把 cookie／token／sign 寫進 git、fixture、未遮罩 log |

**核准旗標（互不隱含；一次一操）：**

| 旗標 | 作用 |
|------|------|
| `--i-approve-add-one` | POST **一筆** addcargo |
| `--i-approve-set-qty` | POST **一筆** Ultron 改量 |
| `--i-approve-remove-one` | POST **一筆** Ultron `deleteClick` |

沒帶旗標 = dry-run（離開碼 0，`posted=false`）。帶了旗標但沒有 `--cdp`／`--cookie-jar` = 拒絕 POST（不假裝已登入）。

這支 **不是** `python -m reverse_audit mutate`（那條仍是 CDP 批次路 B）。Phase 2 是獨立的 signed HTTP one-op client。

## 對照 API（與 recon 欄位表對齊）

見 [`docs/1688_cart_network_recon.md`](1688_cart_network_recon.md)。Phase 1 唯讀讀車：[`docs/1688_mtop_read_cart.md`](1688_mtop_read_cart.md)。

| 流程 | API | Phase 2 |
|------|-----|---------|
| 加車 | `com.alibaba.china.buy.service.purchase.mtoppurchaseservice.addcargo/1.0` | dry-run 預設；`--i-approve-add-one` 才 POST 一次 |
| 改量 | `mtop.1688.buycenter.mtoppurchaseastoreservice.async/1.0`（`operator=item_{cartId}`，`fields.quantity`＝新量） | dry-run 預設；`--i-approve-set-qty` 才 POST 一次 |
| 刪列 | **同一個** `.async`（`events.deleteClick[]`：`actived=true`，`deleteItem`） | dry-run 預設；`--i-approve-remove-one` 才 POST 一次 |
| 讀整車 | `render`／`asyncload` | 改量／刪列時由 Phase 1 client 讀 **render 整包** Ultron model；不在本 CLI 當主路徑 |
| 結算／付款／清空車 | — | **禁止** |

## 怎麼跑（dry-run；CI／離線）

不需瀏覽器、不需 1688。印遮罩後 payload，**不會 POST**。

```bash
# 2a 加車：offerId + specId + qty
python -m reverse_audit.mtop_mutate add \
  --offer-id 752797767076 --spec-id spec-aaa --qty 1

# 2a 加車：只有 skuId 時，唯讀解析詳情頁 HTML skuMapOriginal
python -m reverse_audit.mtop_mutate add \
  --offer-id 752797767076 --sku-id 5228880725920 \
  --detail-html tests/fixtures/1688_mtop_mutate/detail_sku_map.html --qty 1

# 2b 改量：從 Phase 1 render 整包 Ultron model clone，只改目標 item 的 fields.quantity
python -m reverse_audit.mtop_mutate set-qty \
  --cart-id 9001 --qty 9 \
  --fixture tests/fixtures/1688_mtop_mutate/ultron_render_model.json

# 2b 刪列：同一份 render model，只把目標 item 的 deleteClick.actived 打開
python -m reverse_audit.mtop_mutate remove \
  --cart-id 9001 \
  --fixture tests/fixtures/1688_mtop_mutate/ultron_render_model.json
```

別名：`python -m reverse_audit.mtop_addcargo …`（等同 `add`）；`python -m reverse_audit.mtop_ultron_mutate set-qty|remove …`。

可選：`--json` 印同一份遮罩 JSON；`--out PATH` 寫檔（仍不含 cookie／token／sign）。

## 怎麼跑（本機一次 live；文件 only，CI 禁止）

前置：本機已登入 1688 的 Chrome + remote debugging（埠＝你正在點的 profile；computerUse 常見 `:9227`），或 git 外面的 cookie-jar。Session 規則與 Phase 1 相同（1688 domain 優先於 taobao 複本）。**不要**把 cookie／`addressId` 提交進 git。

```bash
# 加車一次（請先 dry-run 看 payload）
python -m reverse_audit.mtop_mutate add \
  --offer-id '<offerId>' --spec-id '<specId>' --qty 1 \
  --cdp http://127.0.0.1:9227 --i-approve-add-one

# 改量一次：先 live 讀 render 整包 Ultron model，只 patch 目標 item，再 POST async
python -m reverse_audit.mtop_mutate set-qty \
  --cart-id '<cartId>' --qty '<newQty>' \
  --cdp http://127.0.0.1:9227 --i-approve-set-qty

# 刪列一次
python -m reverse_audit.mtop_mutate remove \
  --cart-id '<cartId>' \
  --cdp http://127.0.0.1:9227 --i-approve-remove-one
```

可改 `--cookie-jar /tmp/1688.cookie-jar`。若改量／刪列要離線組包、只在 POST 時用 session，可同時帶 `--fixture`（須含完整 Ultron model：endpoint／linkage／hierarchy／data；Phase 1 `bundle.json` 不夠）。

skuId 而沒有 specId、又想 live 解：`--fetch-detail` 會 **GET** `https://detail.1688.com/offer/<id>.html`（唯讀，不加車）。CI 請用 `--detail-html`。`--offer-url 'https://detail.1688.com/offer/<id>.html'` 可代替 `--offer-id`。

Grok 遠端 CDP 一次 smoke（庭安已授權；**先 dry-run 再帶旗標**；cloud CI 不要跑）：

```bash
# 0) 先讀車（Phase 1 唯讀）拿 cartId
python -m reverse_audit.mtop_mutate read --cdp http://127.0.0.1:9227

# 1) add / set-qty / remove：先不要旗標看 payload，再加對應 --i-approve-*
```

**2026-09-17 再 smoke（addcargo 已 PASS；同一測線）：** cartId=`7023719468331`，offer `703961968928`，當時 qty=1。先改量 1→2，成功後再刪列。**不要** checkout／clear／Golden／auto_approve。

```bash
# 先 dry-run 確認 params 有 endpoint / linkage / hierarchy / data（不是只有 item_*）
python -m reverse_audit.mtop_mutate set-qty \
  --cart-id 7023719468331 --qty 2 \
  --cdp http://127.0.0.1:9227

python -m reverse_audit.mtop_mutate set-qty \
  --cart-id 7023719468331 --qty 2 \
  --cdp http://127.0.0.1:9227 --i-approve-set-qty

python -m reverse_audit.mtop_mutate remove \
  --cart-id 7023719468331 \
  --cdp http://127.0.0.1:9227 --i-approve-remove-one
```

若失敗，stderr 會以「卡在登入／簽章／風控／業務參數／Ultron 包」開頭。**停，不要重試洗車。**

離開碼：`0` 成功（含 dry-run）；`1` 用法／安全拒絕；`2` 連不上 CDP；`3` 未登入；`4` 簽章失敗；`5` 其他 mtop 錯（含風控／業務參數／Ultron 包；看「卡在…」）。

## Payload 形狀

### addcargo

`data.goodsParams` 是**字串化**後的一陣列（不是 JSON 陣列欄位）。解碼後恰好一筆：

| 欄位 | 說明 |
|------|------|
| `specId` | SKU 規格 id（加購關鍵；**沒有 skuId**） |
| `offerId` | Offer（數字） |
| `quantity` | 本次加購量 |
| `flow` | `general` |
| `ext.sceneCode` | `""` |
| `selectedTradeServices` | `[]` |

外層：`client=pc`、`purchaseType=""`、`attributes={}`。不寫死 `fromkv`（session／trace）。

### Ultron set-qty / delete

mtop `data={"params":{...}}`。`params` **必須**含 live 成功包那組鍵：`endpoint`、`operator`、`linkage`、`data`（整車 nodes）、`hierarchy`。來源是 Phase 1 **render 回包裡的完整 Ultron model**（不要發明缺的鍵）。clone 後只 patch 目標 `item_{cartId}`，`operator=item_{cartId}`。

- 改量：覆寫 `fields.quantity`＝新量；若 render 上已有 `selectedQuantity` 一併改。不發明 `modifySku`
- 刪列：既有 `events.deleteClick[]` 裡 `deleteItem` 設 `actived=true`（沒有才補一筆 documented 形狀）；其他 events／nodes 原樣保留
- dry-run 可印摘要（`ultronKeys`／`itemKeys`）；**核准 POST 的 body 必須是整包**，不是 `{operator, data:{item_X}}` 最小包（那包 live 會 `SYSTEM_ERROR::null`）
- 安全閘：禁止結算／清空車／batch-add **API 名稱**。clone 包裡的 UI 標籤（`batchAddItemLabel`、畫面上的「结算」）**不是**那些 API，`operator=item_*` 的 async one-op 不以 payload 子字串擋它們。addcargo 仍掃我們自己組的 body。

分類時 deleteClick 優先於 `fields.quantity`（與 recon 相同）。

## Session／簽章

與 Phase 1 相同：`token = _m_h5_tk` 第一個 `_` 之前；`sign = md5(token + "&" + t + "&" + appKey + "&" + data)`；`appKey=12574478`。同名 cookie 優先 `.1688.com`。**永遠不要**把真實 cookie、token、sign 貼進文件、PR 或 fixture。

## 測試

```bash
python -m unittest tests.test_mtop_mutate tests.test_mtop_read_cart
```

不需 Playwright、不需 1688。Fixture 在 `tests/fixtures/1688_mtop_mutate/`（合成 addcargo／**完整 Ultron render model**）。Phase 1 `bundle.json` 沒有 endpoint／linkage／hierarchy，不能當 Ultron POST 來源。

## 與現有路徑的關係

- Phase 1 `python -m reverse_audit.mtop_read_cart` 仍**唯讀**，繼續拒絕 addcargo／async mutate。
- `python -m reverse_audit mutate --i-approve-*` 仍是 **CDP 批次**路 B，沒改成這支 HTTP client。
- `alibaba_client.py` 仍是 Open Platform AOP stub，**不要**接到這條 H5 路徑。
- 整頁補貨／restocker 執行層現況仍是 DOM／CDP；本 PR 只提供「帶旗標的 HTTP one-op」。

## 水位（這台 cloud 通到哪／卡在哪）

**已通（離線，可重放）：** addcargo／Ultron set-qty／deleteClick 的 payload 形狀（整包 endpoint／linkage／hierarchy／data）、Phase 1 sign 重用、沒旗標不 POST、skuMapOriginal 合成 HTML、禁止結算／清空車。unittest 綠。

**卡在 live smoke（這台 Linux cloud，2026-09-17）：**

| 檢查 | 結果 |
|------|------|
| CDP `:9227`／`:9223`／`:9232` | `ConnectionRefusedError`（沒有已登入 Chrome） |
| cookie-jar / `_m_h5_tk` | 環境沒有（也不該有） |
| 公開詳情頁 GET（`--fetch-detail`，不加車） | `detail.1688.com` 回 **x5 punish** 小頁（約 1KB，無 `skuMapOriginal`）。雲端 IP 被攔，不是 parser 錯。 |

遠端 CDP 第一次 smoke：**addcargo PASS**（車 23→24，新 cartId=`7023719468331` qty=1）。**set-qty／remove** 先 FAIL `SYSTEM_ERROR::null`（最小 item 包），改 clone 整包後 dry-run 曾被 UI 標籤 `batchAddItemLabel` 誤擋。本修：`operator=item_*` 的 async 不再用 payload 子字串擋 clone 標籤。**請 Grok 再跑上面 1→2 再刪列**；這台 cloud 仍無法 POST。
