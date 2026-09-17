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
| 改量／刪列：從 Phase 1 **render** 回包抄 `item_{cartId}` 再改 quantity 或 `deleteClick` | 憑空發明整棵 Ultron 購物車樹 |
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
| 讀整車 | `render`／`asyncload` | 只在需要 item node 時由 Phase 1 client 讀；不在本 CLI 當主路徑 |
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

# 2b 改量：從 Phase 1 render／bundle 抄 item_{cartId}，改 fields.quantity
python -m reverse_audit.mtop_mutate set-qty \
  --cart-id 9001 --qty 9 \
  --fixture tests/fixtures/1688_mtop_read_cart/bundle.json

# 2b 刪列：同一顆 item node，加上 deleteClick
python -m reverse_audit.mtop_mutate remove \
  --cart-id 9001 \
  --fixture tests/fixtures/1688_mtop_read_cart/bundle.json
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

# 改量一次：先 live 讀 render 拿 item node，再 POST async
python -m reverse_audit.mtop_mutate set-qty \
  --cart-id '<cartId>' --qty '<newQty>' \
  --cdp http://127.0.0.1:9227 --i-approve-set-qty

# 刪列一次
python -m reverse_audit.mtop_mutate remove \
  --cart-id '<cartId>' \
  --cdp http://127.0.0.1:9227 --i-approve-remove-one
```

可改 `--cookie-jar /tmp/1688.cookie-jar`。若改量／刪列要離線組包、只在 POST 時用 session，可同時帶 `--fixture`（item node 來自 fixture，不發明整車）。

skuId 而沒有 specId、又想 live 解：`--fetch-detail` 會 **GET** `https://detail.1688.com/offer/<id>.html`（唯讀，不加車）。CI 請用 `--detail-html`。

離開碼：`0` 成功（含 dry-run）；`1` 用法／安全拒絕；`2` 連不上 CDP；`3` 未登入；`4` 簽章失敗；`5` 其他 mtop 錯。

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

從 Phase 1 render 抄 `item_{cartId}`（含既有 `fields`），**只帶這一顆**，`params.operator=item_{cartId}`。

- 改量：覆寫 `fields.quantity`＝新量（權威欄；不發明整包 `modifySku`）
- 刪列：`events.deleteClick=[{actived:true, eventType/key/type:deleteItem, fields.cartId}]`；`purchaseType` 只有 item 上原本有才帶

分類時 deleteClick 優先於 `fields.quantity`（與 recon 相同）。

## Session／簽章

與 Phase 1 相同：`token = _m_h5_tk` 第一個 `_` 之前；`sign = md5(token + "&" + t + "&" + appKey + "&" + data)`；`appKey=12574478`。同名 cookie 優先 `.1688.com`。**永遠不要**把真實 cookie、token、sign 貼進文件、PR 或 fixture。

## 測試

```bash
python -m unittest tests.test_mtop_mutate tests.test_mtop_read_cart
```

不需 Playwright、不需 1688。Fixture 在 `tests/fixtures/1688_mtop_mutate/`（合成包）＋ Phase 1 `tests/fixtures/1688_mtop_read_cart/bundle.json`（item node）。

## 與現有路徑的關係

- Phase 1 `python -m reverse_audit.mtop_read_cart` 仍**唯讀**，繼續拒絕 addcargo／async mutate。
- `python -m reverse_audit mutate --i-approve-*` 仍是 **CDP 批次**路 B，沒改成這支 HTTP client。
- `alibaba_client.py` 仍是 Open Platform AOP stub，**不要**接到這條 H5 路徑。
- 整頁補貨／restocker 執行層現況仍是 DOM／CDP；本 PR 只提供「帶旗標的 HTTP one-op」。
