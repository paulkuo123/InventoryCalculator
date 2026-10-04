# Golden 對照表批次寫入

代理只能提出要改的列。人另存一份決策檔並簽核之後，才能用命令列寫入。沒有網頁按鈕，也沒有新的網址。

這支程式不自己改寫對照表的格式。真正落筆仍走現有的三個流程：

- 第 1 層改連結：`commit_url_change`
- 第 2、3 層核准或改成指定規格：`_apply_decision`（它先做網頁審核同一套狀態、候選與第二規格檢查，再呼叫 `_write_approved_mapping`）
- 標成停售：`_write_status_mapping`

沒有加上 `--apply` 時，不會改 `golden_table.json`，也不會改資料庫裡的對照建議、審核紀錄或 1688 綁定。

## 提案檔

路徑：`proposals/<批次編號>.json`。批次編號的樣子是 `B-20261004-01`（西元年月日加兩位數）。

```json
{
  "batch_id": "B-20261004-01",
  "rows": [
    {
      "batch_id": "B-20261004-01",
      "layer": 2,
      "product_id": "p-cup",
      "spec_id": "cup-red",
      "old_values": {
        "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html",
        "1688_sku_name": ""
      },
      "new_values": {"1688_sku_name": "紅色"},
      "evidence": [
        {"type": "snapshot", "source": "人工抄下的快照編號", "captured_at": "2026-10-04T10:00:00Z"}
      ],
      "confidence": 0.8,
      "agent_version": "agent-1",
      "golden_sha_at_proposal": "64碼SHA256"
    }
  ]
}
```

上面的 `old_values` 只示範兩個欄位。真正的提案必須把這一層要求的舊值欄位全部列上，不能多也不能少。請用 `snapshot_old_values` 產生，不要手抄漏欄位。

一列就是一個規格。一批最多 50 列。商品編號和規格編號不能含斜線。不能有沒寫在上面的欄位，也不能有重複列。對照表裡找不到的列會被拒絕。

層級以寫提案當下的那一列為準：

| 層級 | 現況 | 新值只能有這些欄位 |
|---|---|---|
| 1 | 沒有 1688 連結，也還沒有規格名稱 | 只有 `阿里巴巴商品URL` |
| 2 | 已有連結，但沒有 `1688_sku_name` | `1688_sku_id`、`1688_sku_name`、`1688_sku_second_name`、`1688_spec_text` |
| 3 | 已經有規格名稱 | 與第 2 層相同 |

第 2、3 層的新值一定要有規格名稱。狀態、來源、驗證時間、指紋、維度數、offer 編號不能寫進新值，那些是既有流程自己算的。

`old_values` 不是「想改的欄位」，而是這一列當時的舊值。第 1 層必須含現有連結更新流程會一併改寫的每一個欄位（含商品名稱、連結、規格欄位、狀態、來源、驗證時間）。第 2、3 層除了規格欄位，還要含連結，避免連結已被人改過卻還去寫規格。缺的欄位請填空字串 `""`，數字要和對照表裡的型別一樣。程式有 `snapshot_old_values(model, layer)` 可照這一列組出這組舊值。

證據至少一筆，每筆只能有 `type`、`source`、`captured_at`。信心值是 0 到 1。`golden_sha_at_proposal` 是提案當下整份 `golden_table.json` 的 SHA-256。同一批每一列都要相同。

提案檔寫好之後就只讀。套用過程若發現檔案被改，會停止。

## 決策檔

人另外做一份，不要寫進提案檔。代理不要填簽核。

```json
{
  "batch_id": "B-20261004-01",
  "sign_off": {
    "reviewer": "王小明",
    "signed_at": "2026-10-04T18:30:00+08:00"
  },
  "rows": [
    {"product_id": "p-cup", "spec_id": "cup-red", "decision": "approve"},
    {
      "product_id": "p-cup",
      "spec_id": "cup-blue",
      "decision": "replace",
      "chosen_values": {"1688_sku_name": "藍色", "1688_sku_id": "sku-blue"}
    },
    {"product_id": "p-cup", "spec_id": "cup-green", "decision": "skip"},
    {"product_id": "p-cup", "spec_id": "cup-white", "decision": "discontinued"}
  ]
}
```

`sign_off.reviewer` 是簽核人，`signed_at` 是簽核時間（ISO 8601）。空白或整段缺漏，套用會拒絕。

每一列的 `decision` 只能是：

- `approve`：採用提案的新值
- `replace`：改用 `chosen_values`。沒寫到的規格欄位會當成空白，不是跟提案合併
- `skip`：這一列不寫
- `discontinued`：標成停售

決策必須剛好涵蓋提案的每一列。

## 命令

在專案目錄執行。`--base-dir` 是放著 `golden_table.json` 的資料夾。練習請用暫存資料夾，不要拿正式檔試指令。

```bash
python -m golden_batch_writer validate proposals/B-20261004-01.json --base-dir /你的資料夾

python -m golden_batch_writer dry-run proposals/B-20261004-01.json \
  --decision decisions/B-20261004-01.json --base-dir /你的資料夾

python -m golden_batch_writer apply proposals/B-20261004-01.json \
  --decision decisions/B-20261004-01.json --base-dir /你的資料夾

python -m golden_batch_writer apply proposals/B-20261004-01.json \
  --decision decisions/B-20261004-01.json --base-dir /你的資料夾 --apply

python -m golden_batch_writer verify --batch-id B-20261004-01 --base-dir /你的資料夾

python -m golden_batch_writer rollback --batch-id B-20261004-01 \
  --mode whole --base-dir /你的資料夾 --apply

python -m golden_batch_writer rollback --batch-id B-20261004-01 \
  --mode per-row --base-dir /你的資料夾 --apply
```

`apply` 不加 `--apply` 時只做預覽。預覽會寫差異檔，不會改對照表。預覽和第 1 層一樣，會先確認資料庫裡已經有一筆成功、而且連結相符的快照。第 2、3 層的預覽也會核對規格。

真的套用時，必須在終端機手動輸入批次編號，不能用管線代填。第 1 層每一列還要再輸入一次 `商品編號/規格編號`，一次一列。任何一列打錯，整批停止，而且還沒開始寫。這些確認之前只用唯讀連線看快照和建議，不會建立 `SkuMappingService`，也不會改 `procurement.db` 的位元組。人打對批次編號之後，才開啟服務、取資料庫快照、開始寫。

第 2、3 層要核准或改規格時，規格名稱組合必須已經在該 offer 的快照或候選裡。建議狀態若是過期、等待登入、錯誤或疑似停售，會被拒絕。有第二規格卻沒填第二規格名稱，也會被拒絕。這幾條和網頁審核的 `_apply_decision` 是同一套檢查，不會另外放寬。

還原同樣要 `--apply`，並且在終端機輸入批次編號。`--mode whole` 是整批，`--mode per-row` 是逐列。逐列可用重複的 `--spec 商品編號/規格編號` 指定範圍；省略就是這一批預計要寫的每一列。若寫到一半被強制中止，批次紀錄會停在 `applying`，但預計要動的列已經先記在 `rows_planned`。這種狀態可以用整批備份還原，不必先有寫入後檢查碼。已寫完的批次則仍要檢查碼相符，否則請改走逐列。

## 備份與批次紀錄

套用前會在 `backups/batches/<批次編號>/` 放下這些檔案：

- `golden_table.json`：寫入前的整份複本
- `diff.json`：每一列的寫入前內容、預計寫入的值
- `sqlite_snapshot.json`：將被改到的對照建議、審核紀錄、候選規格、綁定，以及這幾列牽到的採購草稿
- `batch_record.json`：提案檔與決策檔的 SHA-256、寫入前後對照表的 SHA-256、提案時的整檔檢查碼是否和寫入前相符（`golden_sha_matches_proposal`）、簽核人、時間、寫入前先記下的 `rows_planned`、實際寫到的列與用了哪個既有流程

既有流程仍會在對照表旁邊留下 `golden_table.json.backup_before_*`，而且照舊只留最新 3 份（`GOLDEN_BACKUP_KEEP` 沒有改）。批次目錄的檔名和位置都不在那個清理規則裡。`housekeeping.py` 只會刪呼叫時指定的檔名樣式，不會掃這批備份。

寫入後的 `verify` 會核對：型號列數沒變、不在這一批裡的列沒變、紀錄裡的前後檢查碼還在，並且把寫入後的實際內容和決策檔比對。略過的列必須和寫入前相同；停售的列狀態必須是停售；第 1 層的連結必須和決策相同；第 2、3 層必須是已核准，而且決策有寫的規格名稱、第二規格、SKU 編號要和實際內容相同。不一致的項目會列在批次紀錄的 `verification.problems`。

## 還原

兩種都要把三樣東西放回去：`golden_table.json`、`sku_mapping_suggestions`／`sku_mapping_reviews`（連同這次會動到的候選規格），以及 `_sync_alibaba_binding` 寫進 `alibaba_bindings` 的結果。第 1 層若把未送出的採購草稿標成阻擋，還原時一併放回。

- 整批：若這一批已經寫完，先看現在的對照表檢查碼是不是紀錄裡的「寫入後」檢查碼。相同才整份用備份蓋回。不同就拒絕，並請改走逐列。若狀態還是 `applying`（寫到一半被中止），沒有寫入後檢查碼也能整份用批次備份蓋回，範圍是寫入前記下的那些列。
- 逐列：只把點名的規格換回差異檔裡的寫入前內容，資料庫也只動這幾列。若換完之後整份內容和備份一樣，就直接寫回當時的位元組，所以緊接著還原可以和寫入前逐位元組相同。若別的列事後被改過，只換回點名的列，並用和既有流程相同的縮排重新存檔，這時整份檔案不會和最初的位元組相同。

## 和既有流程不一致的地方

規格若和程式打架，這次採用較安全的解釋，沒有另寫一套 Golden 寫入。

1. 第 1 層的提案只能指定商品連結。`commit_url_change` 仍會寫入商品名稱、offer 編號、指紋，把規格欄位清掉，並把狀態改成待確認。它不會在這一步核准規格。同一商品裡舊連結相同的其他規格會被算進版本檢查，但命令只提交人確認過的那一列，不會連帶改它們。
2. 第 1 層一定要資料庫裡已經有一筆成功、且連結相符的 1688 商品快照。沒有就整批停止。這支程式不會自己去抓網頁，也不會造一筆假快照。
3. `commit_url_change` 可能把尚未送出、而且含有這一列的採購草稿標成阻擋。這是既有流程的行為。批次還原會把那些草稿列放回寫入前。
4. 第 2、3 層不直接把決策裡的規格做成候選再送進 `_write_approved_mapping`。它先走 `_apply_decision`：建議狀態不可用、規格組合不在快照或候選、第一規格是空白、或有第二規格卻沒填第二規格名稱，都會拒絕。通過之後才由 `_write_approved_mapping` 寫入，並由它算出狀態、來源、驗證時間、指紋、offer 編號和維度數。提案不能指定這些衍生欄位。
5. 停售沒有寫在連結更新和核准這兩個進入點裡，但程式裡已有 `_write_status_mapping`。停售走它。若資料庫還沒有這一列的對照建議，就拒絕，不會為了代理補一筆建議。沒有連結、也沒有規格名稱的第 1 層通常還沒有建議列，因此不能在這一批裡直接標停售。
6. 開啟 `SkuMappingService` 時，它會把已有連結或規格名稱的列登記進資料庫，這一步本來就不改對照表，但會改 `procurement.db` 的位元組。因此確認批次編號之前只開唯讀連線。人打對編號之後才開啟服務。批次快照取在這一步之後、真正寫入之前。還原回到這個快照，不會取消服務開啟時本來就會做的登記。批次編號打錯或不是終端機而拒絕時，服務還沒開啟，資料庫位元組保持原樣。
7. `_sync_alibaba_binding` 固定寫資料夾裡的 `procurement.db`，不看另外指定的資料庫路徑。批次工具因此也只用這個檔。服務若指向別的資料庫，套用會拒絕，避免對照建議和綁定分家、還原不完整。
8. 舊值比對的是提案列出的那些欄位，不是整份檔案。整份檢查碼不同、但目標列的舊值沒變，仍可預覽、也可套用。目標列上那些欄位有任何一個被改過，就視為過期，必須重提。
