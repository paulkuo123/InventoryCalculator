# 1688 mtop 唯讀讀購物車（Phase 1）

**Phase 1 = 唯讀。** 這支 client 只重放已確認的讀車 mtop，把車內列印成遮罩後的行摘要。

**mutate HTTP = Phase 2**（[`docs/1688_mtop_mutate.md`](1688_mtop_mutate.md)）：預設 dry-run，POST 須 one-op 旗標。本模組仍**不實作、不呼叫** mutate。

整頁補貨／路 B mutate **接到**這套 HTTP 是 Phase 3（[`docs/1688_mtop_restock.md`](1688_mtop_restock.md)）：預設仍 DOM／CDP；`--via-mtop` 才改走。Phase 1 這支 CLI 維持唯讀。

庭安已 OK 開這個低爆破 Phase 1 骨架。不要把這支合進「會改車」的流程，也不要寫 `golden_table`／auto_approve／Golden #41–#46。

## 會做／不會做

| 會 | 不會 |
|---|---|
| 從**已經登入**的 Chrome 用 `connect_over_cdp` 讀 cookie／`_m_h5_tk` | `launch`／`launch_persistent_context`、殺掉 Chrome |
| 或讀本機 cookie-jar（Netscape／Playwright JSON／Cookie 標頭檔） | 把 cookie／token／sign 寫進 git、fixture、或未遮罩 log |
| 用常見 H5 mtop 簽章（`appKey=12574478`）打 `render/1.0` | 打 `addcargo`、Ultron `astoreservice.async` 改量／`deleteClick` |
| 需要完整列時再打 `asyncload/1.0`（預設 `pageNo=1`） | 結算、付款、清空車 |
| 印 `cartId`／`offerId`／`skuId`／`qty` 行摘要 | 當正式 production 購物車服務、寫 golden、auto_approve |
| `--dry-run --fixture` 離線解析（CI） | 假裝 cloud Linux 已登入 1688 |

對照 API（live 已確認，見 [`docs/1688_cart_network_recon.md`](1688_cart_network_recon.md)）：

| 流程 | API | Phase 1 |
|------|-----|---------|
| 讀整車 | `mtop.1688.buycenter.mtoppurchaseastoreservice.render/1.0` | **會打** |
| 讀更多列 | `…astoreservice.asyncload/1.0` | **會打**（可 `--no-asyncload`） |
| 加車 | `…mtoppurchaseservice.addcargo/1.0` | 禁止（見 Phase 2） |
| 改量 | Ultron `…astoreservice.async` + `fields.quantity` | 禁止（見 Phase 2） |
| 刪列 | 同一個 `.async` + `events.deleteClick[]`／`deleteItem` | 禁止（見 Phase 2） |
| 結算／付款 | — | 禁止 |

## 怎麼跑

```bash
# CI／離線（不需瀏覽器、不需 1688）
python -m reverse_audit.mtop_read_cart --dry-run
python -m reverse_audit.mtop_read_cart --fixture tests/fixtures/1688_mtop_read_cart/bundle.json

# 操作者本機：已登入的 Chrome + 對上「實際在點」的 profile 的 remote debugging
python -m reverse_audit.mtop_read_cart --cdp http://127.0.0.1:9227

# 或本機匯出的 cookie-jar（不要放進 repo）
python -m reverse_audit.mtop_read_cart --cookie-jar /tmp/1688.cookie-jar
```

CDP 與 freeze／recorder 相同：`--cdp` 或 `ALIBABA_RESTOCK_CDP`（例如 `http://127.0.0.1:9227`），未設時依序試 `9227`、`9223`。只 attach，不新開、不殺掉 Chrome。computerUse 常見是 `:9227`（`chrome-profile-5`），不是 `:9232`。

可選：`--json` 印 identity-only JSON；`--out PATH` 寫同一份（仍不含 cookie／token）；`--no-asyncload` 只打 render；`--address-id` 見下方。

## 請求 `data` 形狀（live）

`render` **必須**帶業務參數，否則 mtop 回 `FAIL_SYS_BIZPARAM_MISSED::缺少业务参数platformType`。預設（**不含**帳戶 `addressId`）：

```json
{"purchaseType":"main_purchase_type","platformType":"PC","cartPageOption":{}}
```

先這樣打。若 live 仍要 `addressId`，本機再帶，**不要**寫進 repo／fixture：

```bash
PYTHONPATH=. python -m reverse_audit.mtop_read_cart --cdp http://127.0.0.1:9227 --address-id '<from cart page>'
```

`asyncload` 預設是 `data.param`：`pageNo`、`platformType`、`purchaseType`、`needClean=false`、`hitNewPromotionExpressionAB=true`，以及從 **render 回包**組出的 `itemAsyncParams`（cartId／offerId／qty／seller…）。`buyerUserId` 優先從 render 取，否則用 session 的 `unb` cookie。`receiveAddressCityCode` 只在 render 裡有才帶。真實 `buyerUserId`／`addressId` **不要**寫死在程式或 fixture。

離開碼：`0` 成功；`1` 用法／fixture；`2` 連不上 CDP；`3` 未登入／缺 `_m_h5_tk`／session 過期；`4` 簽章失敗；`5` 其他 mtop 錯。

## Session 從哪來

1. **CDP（建議）**：本機已登入 1688 的 Chrome 開 remote debugging，client 用 `connect_over_cdp` 讀 context cookies。不開分頁、不點車、不改車。
2. **手動 cookie-jar**：在已登入的 `cart.1688.com` 匯出，放在 **git 外面**（例如 `/tmp/1688.cookie-jar`）。`.gitignore` 已擋 `*.cookie-jar`、`1688_cookies*.json`、`1688_cookies*.txt`。
   - Netscape `cookies.txt`（擴充套件「Get cookies.txt LOCALLY」）
   - Playwright／Chrome `storage_state`／cookies JSON 陣列
   - 一行 `Cookie:` 標頭（`_m_h5_tk=…; cookie2=…`）

**永遠不要**把真實 cookie、`_m_h5_tk`、sign 貼進文件、PR、fixture 或聊天。

## 簽章

對齊常見 h5api：

```text
token = _m_h5_tk 第一個 '_' 之前
sign  = md5(token + "&" + t + "&" + appKey + "&" + data)
appKey = 12574478   # 1688 H5 典型值，不是 secret
```

`t` 是毫秒時間戳；`data` 是會送出的 compact JSON 字串。缺 `_m_h5_tk` 或 `FAIL_SYS_SESSION_EXPIRED` → 未登入。`FAIL_SYS_ILLEGAL_ACCESS`／非法请求 → 簽章或 token 過期，請重匯 jar 或重掛同一個已登入 profile。

打 `h5api.m.1688.com` 時，同名 cookie（例如 `cookie2`、`_m_h5_tk`）**優先用 `.1688.com`／1688 子網域**，不讓 CDP 裡的 `.taobao.com`／`.tmall.com` 值贏。否則會出現「Chrome 明明有 1688 `_m_h5_tk`，HTTP 卻 `FAIL_SYS_SESSION_EXPIRED`」。沒有 1688 對應名稱的 taobao cookie 仍會帶上。

## 測試

```bash
python -m unittest tests.test_mtop_read_cart
```

不需 Playwright、不需 1688。Fixture 在 `tests/fixtures/1688_mtop_read_cart/`（合成包、無真實 cookie）。

## 與現有路徑的關係

- `alibaba_client.py` 仍是 Open Platform AOP stub（建單），**不是**這支 H5 mtop client。
- 讀車 freeze 仍走 CDP Network：`scripts/freeze_reverse_audit_pools_20260905.py`。
- Network 偵察／欄位表：`scripts/record_1688_cart_network.py` + [`docs/1688_cart_network_recon.md`](1688_cart_network_recon.md)。
- 加車／改量／刪列的 **HTTP one-op** 在 Phase 2：`python -m reverse_audit.mtop_mutate`（預設 dry-run；見 [`docs/1688_mtop_mutate.md`](1688_mtop_mutate.md)）。本模組仍只證明「帶 cookie 的 HTTP 讀車」。
- DOM／CDP 批次 mutate（`python -m reverse_audit mutate`）仍在；兩條路不要混。

## 水位

Phase 1 維持唯讀。Phase 2 mutate HTTP 已另開 PR，**預設不 POST**，且須庭安／操作者明確旗標；**不要 merge 進會自動改車的流程**。
