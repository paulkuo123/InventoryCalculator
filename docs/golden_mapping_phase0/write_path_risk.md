# Golden Table Mapping Phase 0 — 正式寫入路徑與風險（2026-09-07）

**本階段不修改寫入路徑。** 下文只記錄現況，供 Phase 1 做寫入閘。

## 正式資料面

| 儲存 | 角色 | 列數（本機 2026-09-07） |
|---|---|---|
| `golden_table.json` | 納入 git 的正式 mapping + 庫存快取 | 355 商品／5918 型號 |
| `procurement.db` `alibaba_bindings` | 採購／入庫 binding | 4108（少於 golden 型號） |
| `procurement.db` `sku_mapping_suggestions` | 工作台建議／狀態 | 4980 |
| `shopee_products.json` | 蝦皮快照（gitignore） | 353 商品；golden 多 2 個商品 |

三份數字對不齊：bindings／suggestions 不是 golden 的完整投影。Phase 1 若只寫其中一份，另一份會漂。

## Golden 型號上與 mapping 相關的欄位

寫入時常見集合（`golden_import._MAPPING_FIELDS` 較完整）：

- 連結／來源：`阿里巴巴商品名稱`、`阿里巴巴商品URL`、`1688_offer_id`、`1688_offer_fingerprint`
- SKU：`1688_sku_id`、`1688_sku_name`、`1688_sku_second_name`、`1688_spec_text`、`1688_dimension_count`、`1688_mapping_fingerprint`
- 狀態：`1688_mapping_status`、`1688_mapping_source`、`1688_verified_at`
- 採購參數：`1688_min_order_qty`、`1688_package_multiple`、`1688_last_price_cny`

`1688_dimension_count` 在 4895／5918 列為空。`1688_sku_id` 在 2496 筆 approved 列為空。

## 寫入入口

```
UI / CLI                         API / 函式                              寫 golden?  寫 binding?  指紋?
SKU 工作台核准                   SkuMappingService._write_approved_mapping    是          是           offer+mapping
SKU 停售                         _write_status_mapping                       是          是           否（只改 status）
URL 管理確認更新                 commit_url_change                           是          是           offer；可連帶 approved
商品編輯儲存                     /api/golden-table/model-alibaba             是          是           有 sku_name 才 mapping fp
商品編輯「套用 URL 到所有規格」  apply_scope=url_offer_all (#38)             是          是           不碰 SKU
單型號／整商品改 SKU 名稱        model-1688-sku / product-1688-skus          是          是           approved 才更新 fp
新增 Golden 商品                 /api/golden-table/import/commit             是          是           完整
舊襪子 review 套用               /api/alibaba/sku-review/apply               是          否           只寫 sku_name
直接存 binding                   /api/alibaba/bindings                       否          是           呼叫端自帶
入庫成功                         inbound_store 同步庫存快取                  庫存欄     否           不應改 mapping
舊 CLI mapper                    alibaba_sku_mapper / phone_case_mapper      是          視實作       舊路徑
```

`SkuMappingService.decisions`：先寫 JSON（tmp + `os.replace`），再 sync binding 與 review 列；binding 失敗會把 golden **整檔還原**。這是目前最接近原子的路徑。

`model-alibaba`：備份成單一 `golden_table.json.bak`（會被下次覆寫），再直接 `open(..., "w")` 寫入，**沒有** tmp+replace，也沒有失敗還原。`apply_scope=overwrite_all` 仍被 API 接受，會把**同一個** sku_name／sku_id 覆到所有型號；UI 已改為只露出 `single` 與 `url_offer_all`。

`sku-review/apply`：預設 `overwrite=True`，只寫 `1688_sku_name`，不更新 status／id／fingerprint／binding。

## Phase 1 寫入閘必須面對的風險

1. **部分寫入／雙寫漂移**  
   golden 5918 vs bindings 4108 vs suggestions 4980。只核准 JSON 或只 upsert binding 都會讓採購車與工作台看到不同世界。

2. **兄弟型號覆寫**  
   - `url_offer_all`：改全商品 URL／offer，保留各規格 SKU（#38 已測）。來源層一改，舊 SKU 可能對不上新 offer。  
   - `overwrite_all`：API 仍可把單一 SKU 覆到所有規格。  
   - `commit_url_change`：預設影響「目前這條 URL 底下的型號」，可一次改多名兄弟。  
   - S6 純白（`16790492139`）已是同商品兩 offer：全商品套 URL 會毀掉刻意分流。

3. **指紋不完整**  
   - `1688_mapping_fingerprint` = offer + sku_name + second_name（**不含 sku_id**）。  
   - `1688_offer_fingerprint` 來自 snapshot；商品編輯／舊 apply 常不寫。  
   - URL 變更後舊 fingerprint 可能殘留或被清空，Phase 1 要用預覽時的 snapshot id 做 optimistic lock（`commit_url_change` 已有 `source_version`／fingerprint check）。

4. **自動核准**  
   `products.js`：`mappingApproved: Boolean(skuName)` → 填名稱即 `1688_mapping_status=approved`。  
   `procurement_store.upsert_binding`：有 sku_name 預設 approved。  
   reverse_audit：`approved + URL + (skuId OR name/spec)` → **certain**，缺 sku_id 仍可能加車。

5. **既有核准沉默當成終局**  
   3800 筆 approved 中：1267 完整待驗證、2513 不完整、20 衝突。工作台 `status=approved` 篩選會把它們藏在「已完成」。Phase 1 必須另開「待補驗證」隊列，不可只看 raw status。

6. **舊路徑旁路**  
   `/api/alibaba/sku-review/apply`、`/api/alibaba/bindings`、CLI mapper、直接改 JSON。寫入閘若只包 `decisions()`，旁路仍能改正式表。

7. **Backup 保留策略不一致**  
   `prune_golden_table_backups` 每種操作只留 3 份；`model-alibaba` 用單一 `.bak`。失敗復原能力取決於走哪條 API。

## 建議（僅文件，未改 code）

- 單一寫入門面：核准必須同時寫 golden + binding + review log，失敗整筆回滾。  
- 產品編輯只允許改 URL／offer（沿用 #38）或草稿 SKU；`approved` 只能由審核閘寫入。  
- 停用或加危險旗標：`overwrite_all`、`sku-review/apply`。  
- Phase 1 新增三層鑑定欄，不要覆用 `1688_mapping_status=approved` 表示「庭安已完成來源＋SKU 鑑定」。
