# 1688 購物車最終數量核對

本機核對工具：`scripts/reconcile_cart.py`。它處理計算、review、進度、操作意圖、回讀證據及報告，**沒有瀏覽器連線、登入、加購、刪除、修改 mapping 或付款程式碼**。實際網站動作由 Agent 在政策允許的內建瀏覽器中操作；禁止用此工具繞過網站存取限制。`prepare` 回傳操作不代表已執行。

## Python 自動化評估（2026-09-05）

可以用 Python 做可靠的核對流程，現有核心已涵蓋來源凍結、計算、固定範圍、操作日誌、部分成功與續跑、逐 SKU 最終總數及報告。它目前是離線 CLI，尚不是一鍵執行購物車的工具。

下一階段應增加獨立的瀏覽器介面：透過允許的 ego-browser 工作空間讀取與操作網站，Python 只協調狀態。先實作唯讀擷取、逐列去重、延遲載入重試和「已完整讀到指定停止點」證據，再接入按 SKU 設定總數、新增差額、刪除零需求及重載驗證。現有快照契約要求完整購物車；若只讀到書包即停，需另明確定義範圍完整的快照契約，不能把部分 DOM 標記成完整購物車。

本輪實際操作顯示：輸入框有時追加文字，欄位顯示新數量不代表已儲存；送出後過早重載也可能中止更新。瀏覽器介面必須先確認請求已穩定，再重載讀回；遇到不明結果先核對，不能以成功提示或 timeout 決定重送。重新建立的購物車列也必須保留舊／新 lineId 對應。

目前的名稱規則只排除明確的書包配件詞，並非通用商品分類器。正式執行時仍需核對真正的停止商品及 offer ID，保存邊界；遇到名稱疑義不得默默越過。登入、驗證、使用者取得控制或存取政策阻擋時停止瀏覽器操作。

建議先完成唯讀與斷線續讀驗收，再以少量經核准的改量／新增／刪除案例驗證。2026-09-05 既有人工操作的日誌格式與本 CLI 不同，不可直接用 `init` 或 `prepare` 重跑既有任務目錄。

## 已實作的範圍

- 原始購物車順序，遇到第一個書包商品就停止，**書包本身及其後方列不處理**。以購物車 `productName` 或 SKU 對應的蝦皮／Golden 商品名稱含「書包／书包」辨識，排除緊接掛件、吊飾、掛飾、鑰匙等配件詞；基線未找到時才做到底端。
- 書包之前的購物車 → 唯一對應的蝦皮商品 ID → 其他型號，含其他 offer；新 offer 不再擴張蝦皮商品集合。即使是同蝦皮商品的型號，若原本位於書包之後，也排除不改。
- 庫存來源為本次凍結的 `shopee_products.json`；Golden Table 僅提供規格 ID、SKU、網址與核准狀態。缺少／無效库存不當成零，保存過去的建議量不参与計算。
- 共用 `restock_rules.py`：手機殼 3 個月，手機殼吊飾／掛繩及明確標記加購的型號與其餘商品 4 個月；歷史銷量保護、最近十位與條件式最低 5，和原 file-based launcher 一致。
- 原數量、目標總數、操作前讀數、差額與操作後讀數分開保存。過多調低，缺少才新增；零量移除須另外記錄核准。
- 同一 SKU 的多筆購物車列不猜分配；多個蝦皮型號共用採購 SKU 須先確認需求非重複。包裝單位不是一件對一件則待查，不自行換算或加到起訂量。
- 不自動合併名稱或繁簡近似；ID 不一致不能靠同名覆蓋。缺 mapping 的原因及原欄位保留供既有 mapping 工作台處理。

## 快照契約

首次先由上到下完整掃描購物車，包括延遲載入與底端確認，再建立基線。`lineId` 是購物車列的穩定識別，不能使用會變動的畫面索引；重複觀察同一列會去重，不同讀數則整份拒絕。

```json
{
  "snapshotId": "每次完整讀取唯一的識別碼",
  "capturedAt": "2026-09-05T10:00:00+08:00",
  "complete": true,
  "evidence": "內建瀏覽器讀取紀錄或截圖位置，含底端確認",
  "rows": [{
    "lineId": "穩定購物車列識別碼",
    "productName": "購物車顯示的商品名稱（包含書包停止點的辨識）",
    "url": "https://detail.1688.com/offer/123.html",
    "offerId": "123",
    "skuId": "456",
    "specs": ["黑色", "iPhone 15"],
    "quantity": 20
  }]
}
```

這只是格式示例，不能當成真實快照使用。URL 必須是確定的商品頁網址。`skuId` 若無法讀取可留空，但完整規格必须唯一相符；不得推測 SKU ID。不存在的型號省略整列，不以數量零偽造購物車列。操作快照需為最近 5 分鐘且時間晚於上次讀取，禁止重用 snapshotId。

商品頁確認資訊另存 `catalogs.json`，以 review 報告的 `itemId` 為鍵：

```json
{
  "item-0001": {
    "url": "https://detail.1688.com/offer/123.html",
    "offerId": "123",
    "skuId": "456",
    "specs": ["黑色", "iPhone 15"],
    "capturedAt": "2026-09-05T10:01:00+08:00",
    "evidence": "商品頁完整規格、單位、起訂規則的讀取紀錄",
    "unitsPerCartUnit": 1,
    "available": true,
    "minQuantity": 1,
    "quantityStep": 1
  }
}
```

所有值必須由實際頁面確認，不知道就省略並讓項目列待查。商品頁證據須在操作前購物車回讀的前 5 分鐘內；如遇整個 offer 的額外起訂限制而不能確認本次可操作，也應留待人工。原 mapping 與頁面完整規格不符時，不在購物車操作中偷偷替換 mapping。

## 操作順序

以下命令從工作目錄執行；`<run-dir>` 使用獨立的 `debug_snapshots/cart_reconciliations/<run-id>`，不要用現有累加補貨的 `restock_batches`。

1. `python3 scripts/reconcile_cart.py preflight --products <庫存檔> --golden <Golden檔>`：唯讀顯示來源時間／SHA256。未讀購物車時只會顯示等待基線，不產生假的核對任務。
2. `python3 scripts/reconcile_cart.py init --products <庫存檔> --golden <Golden檔> --baseline <完整快照.json> --run-dir <run-dir>`：凍結來源與範圍，建立 `manifest-<sha>.json`、`state.json`、`report.json`、`report.html`。
3. 將 `report.html` 的具體變更清單交使用者集中確認。得到確認後才執行 `approve --run-dir <run-dir> --manifest-sha256 <目前SHA> --evidence <核准紀錄>`；包含零量移除才加 `--allow-removals`。共用 SKU 要以 `--shared-sku-evidence <JSON檔>` 提供 `itemId -> 需求非重複的核對證據`。
4. 重新確認商品頁規格／單位，再讀完整購物車。`prepare --run-dir <run-dir> --observation <新快照.json> --catalogs <catalogs.json>` 只保存下一筆意圖並回傳 `operationId`、`lineId`、`action`、`beforeQty`、`targetQty`、`delta`。
   若下一個型號尚未提供商品頁資料，回傳 `inspect_catalog`，保留待核對並要求唯讀查看商品頁；這不是加購意圖，沒有 operationId。取得資料與新的購物車快照後再 prepare。明確提供了資料但規格／單位不明的項目才列待查。
5. Agent 使用允許的內建瀏覽器操作：`set_quantity` 直接設成 `targetQty`；`add_missing` 加入 `delta`；`remove` 僅適用已核准的零量項目。先确认眼前的 SKU／規格及數量仍與意圖相符，再操作。禁止把 `targetQty` 傳進既有的累加式補貨 API。
6. 完整重讀後執行 `record --run-dir <run-dir> --operation-id <ID> --observation <新快照.json>`。只有數量恰好等於目標才記錄成功。超加、部分結果、timeout 都不能重送。
7. 依序重複 4–6；`prepare` 回傳沒有下一筆操作時，用另一份新完整快照 `finalize --run-dir <run-dir> --observation <新快照.json>`，核對已處理 SKU 並檢查非操作範圍的變動。未完成資格核對的項目不能被 final audit 直接標成成功。

上述子命令皆接在 `python3 scripts/reconcile_cart.py` 後。`report --run-dir <run-dir>` 提供精簡進度；HTML 非終態時每 5 秒自動更新。`已處理` 包含待查，`已吻合`／`已修正` 才是驗證成功。

## 中斷、缺漏及修正

- 每次 `prepare` 已先將意圖寫入原子狀態檔。程序中斷也只能先 `record` 回讀，不能再次 prepare；即使瀏覽器操作未送出，也需先確認。
- 部分數量不符會保留 `inflight` 並暫停。再讀到目標可直接解鎖；若確認沒有未完成請求但數量仍不符，`record ... --settled --resolution-evidence <明確證據>` 解鎖為待核對。這不自動重送，下一次必須再讀一次後計算新差額。
- 登入／CAPTCHA、政策阻擋、購物車讀取失敗時，不以空陣列或舊快照替代，不產生下一筆操作。
- 確定的 mapping 修正由既有 `sku_mapping_service` review／decisions 流程處理，保留 suggestion version、候選 SKU 與頁面證據；此工具不直接改 Golden Table／procurement DB。完成後用 `replan-mapping --run-dir <run-dir> --golden <新Golden檔> --evidence <修正證據>` 產生新版本；保留原購物車、原蝦皮來源與原商品集合，撤銷舊清單核准並重新審閱。未確認操作存在時拒絕 replan。
- 停止點存於 `stopBoundary`（原位置、列 ID、名稱、URL），mapping 重審或續跑不得移動此邊界。報告列出停止點與排除項目；完成僅表示書包前範圍完成，不能宣稱已處理整台購物車。
- `note --run-dir <run-dir> --item-id <ID> --explanation <原因>` 預設記為推測。附 `--confirmed --evidence <測試、程式位置或歷史紀錄>` 才標記已證實；資料差異本身不算 bug 根因。
- 沒有在本次真正打開商品頁確認的 mapping，不宣稱已修正。未知 URL、無貨、規格與單位不明，都保留報告後繼續處理其他項目。

## 驗證

```text
python3 -m pytest -q tests/test_cart_reconciliation.py tests/test_restock_rules.py tests/test_run_watchlist_restock.py tests/test_restock_quantity_consistency.py tests/test_restock_batch.py
```

現有 launcher 的兩個整合測試依賴工作目錄內的本機 `shopee_products.json`（Git 忽略檔），獨立 worktree 須放置唯讀用途的副本。新核對測試使用臨時合成資料，不操作任何網站。實際購物車少量試跑及最終全面回讀仍是獨立驗收條件；本機測試通過不能替代。
