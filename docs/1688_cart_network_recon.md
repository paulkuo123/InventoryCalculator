# 1688 採購車 Network 偵察（讀車／加車／改量）

P0 偵察：**CDP Network recorder + 欄位對照**，不是正式 HTTP 購物車 client。  
目標是說清「讀車／加車／改量」各走哪些 mtop，之後才有機會少點 DOM。

相關現碼：

- 加車 DOM：`alibaba_restocker.click_add_to_cart`／`add_to_cart_with_retry`
- 讀車 freeze：`scripts/freeze_reverse_audit_pools_20260905.py` `capture_cart`（攔 `mtopPurchaseAstoreService`／buycenter+cart）
- 改量 DOM：`reverse_audit/cart_cdp_ops.set_line_quantity`（CDP attach，禁止 launch Chrome）

## 這支腳本會做／不會做

| 會 | 不會 |
|---|---|
| `connect_over_cdp` 掛上**已經開著**的 1688 Chrome | `launch`／`launch_persistent_context`、殺掉 Chrome |
| 預設只打開採購車、錄 Network、可選點「載入更多」（唯讀分頁） | 點「加采购车」、set-qty、刪列、清空車 |
| `--offer-url` 只開 **1 個 offer** 觀察 sku-selector | 批次加觀察清單 |
| `--dry-run --fixture` 離線解析（CI／cloud Linux） | 假裝 cloud 已登入 1688 |
| 輸出欄位表（URL／method／headers／body） | 實作可重放的 production HTTP client |

Live 錄製必須在 **Mac／操作者本機、已登入 1688、已開 remote debugging**。Cloud Linux **沒有** 1688 session；腳本與文件可先合進 `main`，真抓包等操作者本機 Chrome。

## 怎麼跑

CDP 與 freeze／mutate 相同：`ALIBABA_RESTOCK_CDP`（例如 `http://127.0.0.1:9227`），未設時依序試 `9223`、`9227`。只 attach，不新開 Chrome。

```bash
# CI／離線（不需瀏覽器、不需 1688）
python scripts/record_1688_cart_network.py --dry-run \
  --fixture tests/fixtures/1688_cart_network/sample_capture.json \
  --out /tmp/1688_cart_network_dry

# 操作者本機：已登入的 Chrome + remote debugging，錄讀車（預設唯讀）
export ALIBABA_RESTOCK_CDP=http://127.0.0.1:9227
python scripts/record_1688_cart_network.py --out reports/1688_cart_network_recon

# 可選：再開 1 個 offer 頁（仍不點加車）。操作者可在 --watch-seconds 內手動點 1 sku。
python scripts/record_1688_cart_network.py \
  --offer-url 'https://detail.1688.com/offer/OFFER_ID.html' \
  --watch-seconds 30 \
  --out reports/1688_cart_network_recon
```

產出（`reports/` 已 gitignore，勿提交 cookie／live dump）：

| 檔 | 說明 |
|---|---|
| `run_meta.json` | CDP、login wall、`didNotClickAddToCart` 等旗標 |
| `capture.json` | 已分類、header／sign 已遮罩的紀錄 |
| `field_table.json`／`field_table.md` | 讀車／加車／改量對照（含 observed api） |

離開碼：`0` 成功；`2` 連不上 CDP（應改跑本機 Chrome）；`3` 登入牆（不發明登入）。

## 欄位對照（程式分析 + freeze 已知形狀）

下列 URL 以 freeze 白名單與 offer 頁已知 mtop 形狀填入。加車／改量的**精確 live path** 標 **TODO_live**，等本機 CDP 補一筆 1 offer／1 sku。

| 流程 | 狀態 | 請求 URL / 匹配 | 方法 | 關鍵 headers | body 欄位（offerId, skuId, qty, cartId, …） |
|---|---|---|---|---|---|
| read_cart | **confirmed_from_freeze** | `https://h5api.m.1688.com/h5/mtop.1688.buycenter.mtoppurchaseastoreservice.render/1.0/`；freeze 另存 `.asyncload`／`.async` | GET jsonp 或 POST form（mtop `type=jsonp`／`data=`） | `referer=https://cart.1688.com/cart.htm`；`origin=https://cart.1688.com`；POST 時 `content-type=application/x-www-form-urlencoded`；`cookie`（session，**不要記錄值**） | query/form：`api, v, jsv, appKey, t, sign, data`；response `data.model`（JSON 字串）→ `data.item_<cartId>.fields`：`cartId, offerId, skuId, skuTitle, quantity, effective, sellerId`；`events[].fields.cartId`／`sourceCartId`／`cartIds` |
| add_to_cart | **candidate**（TODO_live 精確 path） | URL／api 含 `addCargo` 或 `MtopPurchaseService`（商品頁，freeze 購物車攔不到） | POST（`window.lib.mtop.request`）— TODO_live 精確 path | `referer=https://detail.1688.com/offer/<offerId>.html`；`origin=https://detail.1688.com`；`cookie`／mtop `sign`／`_m_h5_tk`（**不要記錄值**） | `data.goodsParams`：JSON 陣列 `{specId, offerId, quantity, flow, ext, selectedTradeServices}`；`flow` 多為 `general`；**skuId 不在 goodsParams**，需先用 sku_selector 把 skuId→specId；`quantity` 是本次加購量（伺服器可能併入既有 `cartId`） |
| change_qty | **TODO_live** | 候選：`mtoppurchaseastoreservice.{update,modify}`／`updateQuantity`／`updateCart` — **未證實** | TODO_live（很可能同款 mtop POST form） | `referer=https://cart.1688.com/cart.htm`；`cookie`／sign（**不要記錄值**） | 預期：`cartId` + `quantity`（絕對量，對齊 `cart_cdp_ops.set_line_quantity`）；`offerId`／`skuId` 可能一起送或只送 cartId。**TODO_live 確認欄位名** |
| sku_selector（加車輔助） | **candidate** | `wosc.queryofferskuselectormodel` | GET jsonp 或 POST — TODO_live 精確 path | `referer` 商品頁；`cookie` 遮罩 | `data.skuSelectorBizModel.skuInfoMap[skuId]`：`skuId, specId, specAttrs, price, canBookCount` |

### 與現有 DOM 路徑的落差

- **讀車**：freeze 已經吃 mtop body，不必再 GUI 化。本 recorder 用來固定 URL／header／`data=` 形狀，評估能否改「帶 cookie 的 HTTP」而不 `goto`＋展開 DOM。
- **加車**：現況仍是 DOM 點「加采购车」。候選請求是 `addCargo` + `goodsParams`（specId／offerId／qty）。**尚未**實作 HTTP client；要 live 形狀時，操作者對 **1 sku** 手動點一次即可，禁止觀察清單整批加車。
- **改量**：現況是 CDP 改 InputNumber。腳本**不會**自動改量；`--watch-seconds` 期間操作者可自行改 1 列，讓 Network 露出 TODO 端點。

## 操作者 live 補洞清單

1. 本機 Chrome remote debugging，已登入 1688。
2. 先跑預設（只開購物車）→ 確認 `render`／`asyncload` 出現在 `capture.json`。
3. 若要加車欄位：`--offer-url` 開 1 個 offer，在 watch 視窗**手動**點一次「加采购车」（不要用 restocker 批次）。
4. 若要改量欄位：在購物車**手動**改 1 個 sku 的數量（不要跑 `mutate --i-approve-set-qty`）。
5. 把 `field_table.md` 的 TODO_live 改成 observed api 字串；仍不要在本 PR 寫 production client。

## 測試

```bash
python -m unittest tests.test_cart_network_recon
```

不需 Playwright、不需 1688。Fixture 在 `tests/fixtures/1688_cart_network/sample_capture.json`（合成包、無真實 cookie）。
