# 1688 採購車 Network 偵察（讀車／加車／改量／刪列）

P0 偵察：**CDP Network recorder + 欄位對照**，不是正式 HTTP 購物車 client。  
目標是說清「讀車／加車／改量／刪列」各走哪些 mtop，之後才有機會少點 DOM。

相關現碼：

- 加車 DOM：`alibaba_restocker.click_add_to_cart`／`add_to_cart_with_retry`
- 讀車 freeze：`scripts/freeze_reverse_audit_pools_20260905.py` `capture_cart`（攔 `mtopPurchaseAstoreService`／buycenter+cart）
- 改量 DOM：`reverse_audit/cart_cdp_ops.set_line_quantity`（CDP attach，禁止 launch Chrome）
- 刪列 DOM：`reverse_audit/cart_cdp_ops.remove_line`（CDP attach，禁止 launch Chrome；recorder 不自動點）

**2026-09-17 live 已確認加車 `addcargo`、改量／刪列 Ultron `astoreservice.async`**（一筆 offer／一筆 sku；未結算、未付款）。`TODO_live` 對加／量／刪已清除。sku-selector 獨立 XHR 精確 path 仍是 candidate（本樣本 sku→specId 走詳情頁 HTML `skuMapOriginal`）。本 PR **不**實作 mutate HTTP client。

## 這支腳本會做／不會做

| 會 | 不會 |
|---|---|
| `connect_over_cdp` 掛上**已經開著**的 1688 Chrome | `launch`／`launch_persistent_context`、殺掉 Chrome |
| 對 **context 內既有分頁＋之後新開的分頁**掛 Network（至少詳情＋採購車） | 只掛單一採購車分頁（會漏詳情頁 `addcargo`） |
| 預設開採購車、錄 Network、可選點「載入更多」（唯讀分頁） | 點「加采购车」、set-qty、刪列、清空車 |
| `--offer-url` 另開 **1 個** offer 分頁觀察 sku-selector／加車 XHR | 批次加觀察清單；在採購車分頁上 `goto` 詳情而拆掉車頁監聽 |
| `--dry-run --fixture` 離線解析（CI／cloud Linux） | 假裝 cloud 已登入 1688 |
| 輸出欄位表（URL／method／headers／body；cookie／sign／umid 已遮罩） | 實作可重放的 production HTTP client |

Live 錄製必須在 **Mac／操作者本機、已登入 1688、已開 remote debugging**。Cloud Linux **沒有** 1688 session；腳本與文件可先合進 `main`，真抓包等操作者本機 Chrome。

## 環境注意（踩過的坑）

- **CDP 埠必須對上操作者實際點擊的 Chrome profile。** computerUse 桌面常見是 **`:9227`（`chrome-profile-5`）**，不是 `:9232`（profile-10）。掛錯埠會看到畫面動、Network 幾乎空。
- 加購 `addcargo` 打在 **詳情頁**；只掛採購車分頁會漏。官方 recorder 必須全 context／新分頁 CDP，不能單頁 attach。
- 未設 `ALIBABA_RESTOCK_CDP` 時依序試 `9227`、`9223`。只 attach，不新開、不殺掉 Chrome。
- 文件與 fixture **不要**貼 cookie、umid、完整 raw `postData`、session token／sign。

## 怎麼跑

CDP 與 freeze／mutate 相同：`ALIBABA_RESTOCK_CDP`（例如 `http://127.0.0.1:9227`），未設時依序試 `9227`、`9223`。只 attach，不新開 Chrome。

```bash
# CI／離線（不需瀏覽器、不需 1688）
python scripts/record_1688_cart_network.py --dry-run \
  --fixture tests/fixtures/1688_cart_network/sample_capture.json \
  --out /tmp/1688_cart_network_dry

# 操作者本機：已登入的 Chrome + 對上「實際在點」的 profile 的 remote debugging
export ALIBABA_RESTOCK_CDP=http://127.0.0.1:9227
python scripts/record_1688_cart_network.py --out reports/1688_cart_network_recon

# 可選：另開 1 個 offer 分頁（仍不點加車）。watch 期間操作者可在詳情頁手動點 1 sku、或在車頁改／刪 1 列。腳本不自動刪。
python scripts/record_1688_cart_network.py \
  --offer-url 'https://detail.1688.com/offer/OFFER_ID.html' \
  --watch-seconds 30 \
  --out reports/1688_cart_network_recon
```

產出（`reports/` 已 gitignore，勿提交 cookie／live dump）：

| 檔 | 說明 |
|---|---|
| `run_meta.json` | CDP、login wall、`didNotClickAddToCart`、`listenAllPages` 等旗標 |
| `capture.json` | 已分類、header／sign 已遮罩的紀錄 |
| `field_table.json`／`field_table.md` | 讀車／加車／改量／刪列對照（含 observed api） |

離開碼：`0` 成功；`2` 連不上 CDP（應改跑本機 Chrome，並核對埠＝實際點擊的 profile）；`3` 登入牆（不發明登入）。

## 欄位對照（freeze + 2026-09-17 live）

下列 URL：讀車來自 freeze 白名單；**加車／改量／刪列為 2026-09-17 live 抓包確認**（樣本 offer／sku／cartId 僅供對照，不要當全域常數）。sku-selector 獨立 XHR 精確 path 仍是 candidate。

| 流程 | 狀態 | 請求 URL / 匹配 | 方法 | 關鍵 headers | body 欄位（offerId, skuId, qty, cartId, …） |
|---|---|---|---|---|---|
| read_cart | **confirmed_from_freeze** | `https://h5api.m.1688.com/h5/mtop.1688.buycenter.mtoppurchaseastoreservice.render/1.0/`；freeze 另存 `.asyncload`（頁面片段）。URL-only `.async` 沒有 Ultron `operator` 時仍當讀車 | GET jsonp 或 POST form（mtop `type=jsonp`／`data=`） | `referer=https://cart.1688.com/cart.htm`；`origin=https://cart.1688.com`；POST 時 `content-type=application/x-www-form-urlencoded`；`cookie`（session，**不要記錄值**） | query/form：`api, v, jsv, appKey, t, sign, data`；response `data.model`（JSON 字串）→ `data.item_<cartId>.fields`：`cartId, offerId, skuId, skuTitle, quantity, effective, sellerId`；`events[].fields.cartId`／`sourceCartId`／`cartIds` |
| add_to_cart | **confirmed_live**（TODO_live 已清） | `com.alibaba.china.buy.service.purchase.mtoppurchaseservice.addcargo/1.0`（詳情頁；`https://h5api.m.1688.com/h5/...`） | POST；`application/x-www-form-urlencoded`；欄位 `data`＝JSON 字串 | `referer=https://detail.1688.com/offer/<offerId>.html`；`origin=https://detail.1688.com`；`cookie`／mtop `sign`／`_m_h5_tk`（**不要記錄值**） | `data.client` 多為 `pc`；`data.goodsParams` 是**字串化後的陣列** `[{specId, offerId, quantity, flow, ext, selectedTradeServices}]`；`flow` 多為 `general`；**skuId 不在 goodsParams**，需先用 sku_selector 把 skuId→specId；`quantity` 是本次加購量（伺服器可能併入既有 `cartId`） |
| change_qty | **confirmed_live**（TODO_live 已清） | `mtop.1688.buycenter.mtoppurchaseastoreservice.async/1.0`（Ultron）。PC 採購車**沒有**獨立的 `updateQuantity` API 名稱 | POST form；Ultron `params` 大包 | `referer=https://cart.1688.com/cart.htm`；`cookie`／sign（**不要記錄值**） | `params.operator=item_{cartId}`；新數量＝`params.data.item_{cartId}.fields.quantity`（**以此為準**）；同包可帶 `fields.cartId`／`offerId`／`skuId`／`specId`。`events.modifySku[0].fields.quantity` 可能仍是舊值，勿單獨依賴 |
| delete_line | **confirmed_live**（TODO_live 已清） | **同一個** `mtop.1688.buycenter.mtoppurchaseastoreservice.async/1.0`（Ultron）。PC 採購車**沒有**獨立的 `deleteItem` API 名稱 | POST form；Ultron `params` 大包 | `referer=https://cart.1688.com/cart.htm`；`cookie`／sign（**不要記錄值**） | `params.operator=item_{cartId}`；`params.data.item_{cartId}.events.deleteClick[]`：`actived=true`，`eventType`／`key`／`type=deleteItem`，`fields.cartId`／`purchaseType`。分類時 **deleteClick 優先於** `fields.quantity`（刪列包也可能帶數量欄） |
| sku_selector（加車輔助） | **candidate** | `wosc.queryofferskuselectormodel` | GET jsonp 或 POST — 精確 path 仍待補 | `referer` 商品頁；`cookie` 遮罩 | `data.skuSelectorBizModel.skuInfoMap[skuId]`：`skuId, specId, specAttrs, price, canBookCount`。**本樣本** sku→specId 來自詳情頁 HTML `skuMapOriginal`；點規格未打此 XHR |

### 加購 `add_to_cart`（live）

| 項目 | 值 |
|------|-----|
| API | `com.alibaba.china.buy.service.purchase.mtoppurchaseservice.addcargo` |
| 版本 | `1.0` |
| Host | `https://h5api.m.1688.com/h5/...` |
| Method | `POST`（`application/x-www-form-urlencoded`，欄位 `data`＝JSON 字串） |
| 成功跡象 | HTTP 200 + UI「加购成功」+ `quickCart`／badge |

`data` 形狀（**不要**把 cookie／umid／完整 raw postData 貼進文件）：

```json
{
  "client": "pc",
  "goodsParams": "[{...}]",
  "purchaseType": "",
  "attributes": {},
  "fromkv": "offerdetail:pc;…（session／trace，不要寫死）"
}
```

`goodsParams` 解碼後：

| 欄位 | 說明 |
|------|------|
| `specId` | SKU 規格 id（加購關鍵） |
| `offerId` | Offer |
| `quantity` | 本次加購量 |
| `flow` | 多為 `general` |
| `ext.sceneCode` | 常為 `""` |
| `selectedTradeServices` | 常為 `[]` |

### 改數量 `change_qty`（live）

| 項目 | 值 |
|------|-----|
| API | `mtop.1688.buycenter.mtoppurchaseastoreservice.async` |
| 版本 | `1.0` |
| Host | `https://h5api.m.1688.com/h5/...` |
| Method | `POST` |
| 協定 | Ultron（`params` 大包；**不是**獨立 `updateQuantity` 名稱） |
| 成功 | `ret: ["SUCCESS::调用成功"]`；回傳 model 內 `quantity`／`selectedQuantity`＝新值 |

| 欄位路徑 | 說明 |
|----------|------|
| `params.operator` | `item_{cartId}` |
| `params.data.item_{cartId}.fields.cartId` | 車內行 id |
| `params.data.item_{cartId}.fields.offerId` | Offer |
| `params.data.item_{cartId}.fields.skuId` | 車內 sku |
| `params.data.item_{cartId}.fields.specId` | 規格 id |
| `params.data.item_{cartId}.fields.quantity` | **新數量（以此為準）** |
| `…events.modifySku[0].fields.quantity` | 可能仍是舊值，勿單獨依賴 |

改量當下也常伴隨：`asyncload`（頁面片段）、tracking `cart_goods_number_edit`。分類時 `.asyncload`／`.render` 仍是 `read_cart`；只有帶 Ultron `operator=item_{cartId}` 且 `fields.quantity`、**且沒有** `deleteClick`／`deleteItem` 的 `.async` 才是 `change_qty`。

### 刪列 `delete_line`（live）

| 項目 | 值 |
|------|-----|
| API | `mtop.1688.buycenter.mtoppurchaseastoreservice.async`（**與改量同一個** Ultron） |
| 版本 | `1.0` |
| Host | `https://h5api.m.1688.com/h5/...` |
| Method | `POST` |
| 協定 | Ultron（`params` 大包；**不是**獨立 `deleteItem` 名稱） |
| 成功 | `ret: ["SUCCESS::调用成功"]`；車內列數減少（本樣本 24→23）；未進結算 |

| 欄位路徑 | 說明 |
|----------|------|
| `params.operator` | `item_{cartId}` |
| `params.data.item_{cartId}.events.deleteClick[]` | 刪列事件陣列 |
| `…deleteClick[].actived` | `true` 才是這次點刪 |
| `…deleteClick[].eventType`／`key`／`type` | `deleteItem` |
| `…deleteClick[].fields.cartId` | 要刪的車內行 id |
| `…deleteClick[].fields.purchaseType` | 採購類型（隨列帶上） |

分類時 **先看 deleteClick／deleteItem，再看 `fields.quantity`**。同一條 `.async` 刪列包也可能帶 `fields.quantity`，那不是改量。`.asyncload`／`.render` 仍是 `read_cart`。Recorder **預設不自動點刪除**。

### 樣本 id（僅一筆 live，當例子）

2026-09-17 遠端抓包用過的一組數字，**只當例子**，不要寫進 client 當預設：

| 項目 | 例子 |
|------|------|
| Offer URL | `https://detail.1688.com/offer/703961968928.html` |
| SKU 標籤 | 黑色／均码 |
| `specId` | `7ccb9e57d1d5f349172254f5c3eb0f3e` |
| `skuId`（車內） | `5131479859350` |
| `cartId`（車內行） | `7023721329524` |
| 改數量觀測 | 8 → 9 |
| 刪列觀測 | 只刪此測試列（黑色／均码）；車 24→23；未結算 |
| sku→specId（本樣本） | 詳情頁 HTML `skuMapOriginal`；點規格未打 `wosc.queryofferskuselectormodel` |

### 與現有 DOM 路徑的落差

- **讀車**：freeze 已經吃 mtop body，不必再 GUI 化。本 recorder 用來固定 URL／header／`data=` 形狀，評估能否改「帶 cookie 的 HTTP」而不 `goto`＋展開 DOM。
- **加車**：現況仍是 DOM 點「加采购车」。live 請求是詳情頁 `addcargo` + 字串化 `goodsParams`（specId／offerId／qty）。**尚未**實作 HTTP client；要再抓形狀時，操作者對 **1 sku** 手動點一次即可，禁止觀察清單整批加車。
- **改量**：現況是 CDP 改 InputNumber。PC 車沒有獨立 `updateQuantity`；mutate XHR 是 Ultron `astoreservice.async`。腳本**不會**自動改量；`--watch-seconds` 期間操作者可自行改 1 列。
- **刪列**：現況是 CDP 點刪除。PC 車沒有獨立 `deleteItem` API 名稱；mutate XHR 仍是同一個 Ultron `astoreservice.async`，靠 `events.deleteClick[]`／`deleteItem` 區分。腳本**不會**自動點刪；`--watch-seconds` 期間操作者可自行刪 1 列。

## 操作者 live 再抓（可選；加／量／刪已確認）

1. 本機 Chrome remote debugging，已登入 1688；**埠＝你正在點的 profile**（computerUse 常見 `:9227`）。
2. 先跑預設（掛全部分頁、再開購物車）→ 確認 `render`／`asyncload` 出現在 `capture.json`。
3. 若要再看加車：`--offer-url` 開 1 個 offer **分頁**，在 watch 視窗於**詳情頁**手動點一次「加采购车」（不要用 restocker 批次）。
4. 若要再看改量：在**購物車分頁**手動改 1 個 sku 的數量（不要跑 `mutate --i-approve-set-qty`）。
5. 若要再看刪列：在**購物車分頁**手動刪 1 列（不要跑 `mutate --i-approve-remove`）。Recorder 預設**不**自動點刪。
6. 分類器應標出 `addcargo`、Ultron 改量 `.async`，以及帶 `deleteClick`／`deleteItem` 的刪列 `.async`。仍不要在本 PR 寫 production client。mtop sign／cookie／Ultron compress 尚未逆向成可重放客戶端。

## 測試

```bash
python -m unittest tests.test_cart_network_recon
```

不需 Playwright、不需 1688。Fixture 在 `tests/fixtures/1688_cart_network/sample_capture.json`（合成包、無真實 cookie；形狀對齊 confirmed addcargo + Ultron async 改量／刪列）。
