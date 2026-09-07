# Golden Table Mapping Phase 0 — 約 20 個代表案例（2026-09-07）

選自**當前** `golden_table.json`，涵蓋四類主問題 + 多來源／包裝／同圖異材／既有核准疑似配錯。

**沒有提出任何假 SKU 或建議 URL。** `sku_status_zh` 僅分類現況。

| # | 類型 | product_id | 型號（規格ID） | 為何選 | 現況 |
|---|---|---|---|---|---|
| 1 | LINK 缺 URL | `22561129943` | USB to C 50公分（`255033522181`） | 充電線商品多規格皆無 1688 連結 | link=`url_missing`；sku=`missing`；raw=`missing` |
| 2 | LINK 缺 URL（商品不在最新蝦皮快照） | `5002617576` | 1.黃色（`8380678329`） | golden 有、`shopee_products.json` 無；四色皆無 URL | link=`url_missing`；flag=`not_in_shopee_products` |
| 3 | LINK 健康檢查失效 | `19666639659` | 煮茶（`108909267603`） | offer `644276542345` 健康檢查=invalid，影響 14 型號（含 `9778519442`） | link=`url_present_invalid`；同列 raw 多為 discontinued |
| 4 | SOURCE 多來源（7 個 offer） | `20488473843` | 充電器保護套／線套（任選一型號，如 missing 列） | 同一蝦皮商品 7 個 1688 URL；approved／missing／discontinued 混雜 | source=`source_multi_unverified`；product_offer_count=7 |
| 5 | SOURCE 多來源但全數 approved | `24530967554` | 例如 黑色幽靈 | 24 型號、6 個 offer，raw 全是 approved | 仍應待補驗證；不可當成來源已鑑定 |
| 6 | SOURCE 刻意分流（已知） | `16790492139` | 純白（`98491202820`） | S6：純白在 eric `703961968928`，兄弟型號在 main `682877407287`（PR #37） | raw=`approved`／manual；source=多來源。**不建議**對此商品做 url_offer_all |
| 7 | SOURCE 兄弟對照 | `16790492139` | 黑色（`98491202818`） | 與 #6 同商品不同 offer；展示多來源不一定是錯 | raw=`approved`；offer=`682877407287`；sku=`2349黑色` |
| 8 | SOURCE 頁疑似下架 | `23244459752` | 可愛兔兔（`167688526097`） | suggestion=`suspected_discontinued`，golden 仍 `missing`＋有 URL | source=`source_page_suspect`；sku=`missing` |
| 9 | SKU 有 URL 無對應 | `16049155079` | 紫色(單顆),11/12/12mini（`29198172309`） | 鏡頭貼：有連結但 mapping_status=missing（此類 894 列） | link 有值；sku=`missing` |
| 10 | SKU pending + URL 變更未完成 | `20979963902` | 黑色閃粉(一顆),15pro/15proMax（`250245705765`） | `mapping_source=url_change_pending`；閃粉鏡頭貼大量 pending | sku=`pending` |
| 11 | SKU stale | `24242091907` | 鏡面愛心,X/XS（`188264743040`） | 全表僅 2 筆 stale；同商品其他機型仍 approved | sku=`stale` |
| 12 | SKU discontinued | `19666639659` | 煮茶（`108909267603`） | 杯套停售列；236 筆 discontinued 的代表 | sku=`discontinued` |
| 13 | SKU 工作台 no_match（golden 未同步） | `13639407139` | 17 pro（`335093205073`） | suggestions=`no_match`（iPhone 17 新機型）；golden 列可能仍是 missing／其他 | 雙寫不一致案例 |
| 14 | 既有核准不完整（無 sku_id） | `21869109391` | 鬱金香（`405613043534`） | `legacy_user_approved`，有名稱無 sku_id；同商品其他型號有第二規格 | sku=`sku_approved_incomplete`；2496 筆 approved 無 sku_id |
| 15 | 既有核准衝突（同 sku_id 兩色） | `25083569908` | 淺灰／深灰（共用 `6205321050395`） | PR #35 已記錄：兩色都核准成「深灰」規格 | sku=`sku_approved_conflict`；**疑似配錯** |
| 16 | 既有核准衝突（圖案 vs 條紋） | `29819264743` | 斑點狗 vs 條紋斑點狗（共用 `5518614913442`） | S10 趣味襪：不同型號名稱共用同一 sku_id（另三對熊亦同） | sku=`sku_approved_conflict` |
| 17 | 包裝（裸裝／OPP） | `11979825338` | 透白色（Apple Pencil 筆尖套） | `1688_sku_name` 含「一粒裸装、散装、无opp袋装」；採購倍數／包裝鑑定 | flag=`packaging`；raw=`approved` |
| 18 | 包裝（三件套／袋裝） | `24362191497` | 黑色 三件套 | 蝦皮型號已寫「三件套」，1688 名稱含「珠光袋装」 | flag=`packaging` |
| 19 | 同圖異材（透明 vs 霧面） | `22439001227` | 高清透明款,iPad 7/8/9 代 vs 霧面抗指紋款,同尺寸 | 同一張型號圖給透明與霧面；目前多為 missing | flag=`same_image_diff_name`；**不可只靠圖選 SKU** |
| 20 | 同圖不同機型（非異材，對照用） | `18379321477` | 電鍍米白,15 pro 等 | 手機殼同色共用圖、第二規格才是機型；易被 AI／人工看成「同一 SKU」 | raw 多 `approved`；仍待補驗證第二規格 |
| 21 | 既有核准但缺第二規格 | `19960782914` | 黑色軍規,7/8/SE2/SE3 | 同商品多數有 second_name，此列 approved 僅 `1688_sku_name=黑色` | sku=`sku_approved_incomplete` |

表列 21 筆是為了把「同圖異材」與「同圖不同機型」分開，避免 Phase 1 把手機殼共用圖誤判成異材。

## 四類問題對照

| 主類型 | 上表 | 全表主桶（型號數） |
|---|---|---|
| LINK | 1–3 | 959 |
| SOURCE | 4–8 | 1437 |
| SKU | 9–13 | 2520 |
| EXISTING_APPROVAL（含不完整／衝突子類多落在 SKU 主桶） | 6, 7, 14–16, 20, 21 | 主桶 1002；另 incomplete 2513 + conflict 20 計入 SKU |

完整列請查 CSV：`product_id` + `model_id`。
