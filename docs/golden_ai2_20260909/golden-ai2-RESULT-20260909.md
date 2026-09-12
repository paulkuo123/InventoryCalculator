# Golden AI-2 P2 url_suspect CDP live-health RESULT（2026-09-09）

## 任務
對 AI-1 切出的 **P2 `url_suspect`＋應補** 做 signed-in CDP 活頁複檢：109 型號列／應補約 860，去重後 **36 個獨立 1688 offer URL**（已排序 `suggested_qty DESC`）。  
P1 無 URL、P3 mapping_suspect 不在範圍。

## 約束（皆遵守）
- **未** merge main
- **未** 修改 `golden_table.json`
- **未** 加車／改購物車／點購買
- 方法為 **cdp/browser**（非 raw HTTP）
- 未碰到 login／captcha，故未中途 STOP、未呼叫 `request_box_help`

## Golden 未動
- 路徑：`golden_table.json`
- SHA-256：**`8a95064fbe116f9e6ead24837ced1511ce34a9f3ac69358d9c2e0fb585ab3d2e`**（跑前＝跑後）
- `git diff main -- golden_table.json` 空

## 瀏覽器
- CDP：`http://127.0.0.1:9232`（chrome-profile-10，DISPLAY :10）
- Browser：Chrome/151.0.7922.169
- 重用已開 1688 分頁；未新開未登入 Chrome

## 批次

| 項目 | 值 |
|---|---|
| 批次 | **1 of 1** |
| 本批 unique offers | **36 / 36** |
| P2 剩餘 unique URL | **0** |
| url_suspect 全體 unique（含無應補） | 72；其餘非 P2 不在本任務 |

## 計數

| health | n |
|---|---:|
| alive | **11** |
| dead | **23** |
| off_shelf | **2** |
| waf | 0 |
| login_wall | 0 |
| oid_mismatch | 0 |
| error | 0 |
| **total** | **36** |

dead 拆分：`www.1688.com` `notfound` 首頁 **15**；`wrongpage.html` **8**。  
off_shelf：詳情頁明確下架 **2**（734419757382 口红化妆包應補 90；635124351042 侧边小熊壳應補 5）。

## Alive 11 offer_id
675106205147, 896158447887, 616560873695, 674778394852, 742408973734, 838528968671, 624135914858, 689652914948, 704268485591, 713047372087, 740525848630

## 分類修正（notfound 首頁）
初判有 15 筆因首頁推薦卡出現 price 選擇器被標 `alive`。CDP `location.href` 實為  
`https://www.1688.com/?spm=a260k.24848612.notfound&theme=index`（無 `/offer/`）。  
已依 CDP24 精神改標 **`dead`**（offer 不存在；首頁不是該商品）。腳本 `_ai2_cdp_health_check.py` 已加上 `notfound` 首頁規則。

## 產檔
- `/workspace/_handoff/golden_ai2_health_20260909.csv`
- `/workspace/_handoff/golden_ai2_SUMMARY_20260909.md`
- `/workspace/_handoff/golden-ai2-RESULT-20260909.md`
- 腳本：`/workspace/_handoff/_ai2_cdp_health_check.py`

## Docs PR #44（保持 OPEN）
- URL：https://github.com/paulkuo123/InventoryCalculator/pull/44
- Branch：`docs/golden-ai2-cdp-p2-20260909`
- Commit：`c25480e`
- 路徑：`docs/golden_ai2_20260909/`
- State：**OPEN**（**未 merge**）

## 建議下一刀
1. **11 筆 alive**：可進 SKU／補貨對帳（另開 mutate 任務）；優先應補 20 的捲邊襪、帆布袋。
2. **25 筆 dead／off_shelf**：找替代 offer（計劃 C），優先口紅化妝包（90）與杯套（60）。
3. P1 `no_url` 仍無 URL，不在 AI-2 範圍。
4. P3 `mapping_suspect` restock → AI-4。
