# Golden Table Mapping Phase 0 — 歷史採購／圖片搜尋依賴（2026-09-07）

## 結論

| 依賴 | 狀態 | 能否當 Phase 1 證據 |
|---|---|---|
| 1688 開放平台訂單 API | **憑證未設定 + 端點未實作** | 否 |
| `purchase_drafts`／`purchase_orders` | **表在、列為 0** | 否 |
| 到貨入庫訂單 | **本機 DB 尚未建立 inbound_* 表** | 否 |
| 以圖搜圖（1688／淘寶） | **程式不存在** | 否 |
| 已核准 offer 快照 | **可用（221 offer／241 快照）** | 僅限「已有 URL」的商品 |
| ego-lite live 開頁 | **程式在；本機無 `ego-browser`** | 這台機器上不可用 |
| AI 視覺比對（SKU mapping） | **程式在；本機無 API key** | 這台機器上不可用 |
| 舊 CLI 1688 爬蟲 | 程式在；依賴 Playwright + 登入 | 可當備援，易被驗證碼打斷 |

沒有「歷史採購紀錄 → 反查正確 SKU」的現成資料面。Phase 1 的來源鑑定目前只能靠：人工記憶、現有 URL／快照、蝦皮圖 vs 1688 候選圖（工作台已並排）、以及之後若補上的採購／入庫資料。

## 歷史採購

`procurement_store.py` 設計了草稿、待付款訂單、在途：

- `purchase_drafts` / `purchase_draft_lines`
- `purchase_orders` / `purchase_order_lines`
- `alibaba_bindings`

本機 `procurement.db`：

- drafts / orders / order_lines = **0**
- bindings = 4108（從 golden 同步來的對照，不是真實下單紀錄）

`alibaba_client.py` 是 placeholder：`create_pending_order`／`get_order_status` 在憑證齊全時仍 `NotImplementedError`。`auth_status()` 在缺 `ALIBABA_APP_KEY`／`SECRET`／`ACCESS_TOKEN` 時回 `missing_credentials`。

本工作區：

- **沒有** `.env.local`
- 環境變數中 **沒有** 1688／OpenAI／xAI／Gemini／Deepseek key

因此「用過去 1688 訂單反查這次該買哪個 SKU」**做不到**。入庫模組（`inbound_store.py`）理論上會從真實訂單頁抓 offer/sku，但這份 DB 還沒 init inbound 表，等於沒用過。

反向查核 `reports/reverse_audit_YYYYMMDD/` 有購物車與待付款／待發貨／待收貨 freeze（gitignore）。那是**當下四池**，不是完整歷史採購帳，且含 live dump，不應提交。

## 圖片搜尋／1688 查找

倉庫內 **沒有** 以圖搜圖、1688 搜尋 API、或淘寶／1688 圖像檢索 helper。

現有「圖」相關能力：

1. **工作台並排**：蝦皮 `型號圖片網址`／`商品圖片網址` vs 候選 `image_url`（來自 offer 快照）。只比對「已有 URL 的那一頁」上的 SKU 圖，**不會**幫缺 URL 的列去搜新店。
2. **AI 初判**：`sku_mapping_service` 可把蝦皮圖 + 候選圖送給視覺模型。本機無 key，且只在「已有 snapshot」時有意義。
3. **Live scan**：`ego_browser_1688.py` 開 `detail.1688.com/offer/{id}.html` 抽 `skuMapOriginal`。需要 ego-lite 登入態；這台 PATH 上沒有 `ego-browser`。未安裝時採購車可退 Chrome／Playwright（`ALIBABA_RESTOCK_BROWSER=auto`）。
4. **舊 mapper**：`alibaba_sku_mapper.py`、`alibaba_phone_case_mapper.py` 用 Playwright 開**已知 URL**，不搜新商品。

缺 URL 的 945 型號（69 個商品全無 URL）**沒有**自動找店工具。Phase 2 規格中的「缺／錯 URL 候選、有上限重搜」目前是空的。

## 建議（僅筆記）

- 不要假設能從 `procurement.db` 還原歷史 SKU。若庭安有 1688 訂單後台，需另做唯讀匯入（訂單號列表或匯出檔），不可發明。
- 圖片搜尋若要做，是新依賴（登入、反爬、費用），應有界、人工觸發，且結果不得直接寫 golden。
- Phase 1 可先用現有快照 + 人工貼 URL；把「無 URL／多來源」丟進問題清單即可。
