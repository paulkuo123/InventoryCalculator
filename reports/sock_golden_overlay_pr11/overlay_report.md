# 襪子 Golden Table 安全 overlay 報告（eric PR #11）

- 產生時間：`2026-09-05T17:55:38Z`
- 基底：`paulkuo123/InventoryCalculator` 目前 `main` 的 `golden_table.json`
- 來源：`eric0219c/InventoryCalculator@codex/restore-local-sock-golden-table`（PR #11）

## 計數

- **overlaid**：0
- **kept-main**：47
- **needs-human**：5
- 型號層 overlay 數：0
- `golden_table.json` 是否修改：否

## 規則

- 以 main 為底，絕不整份換成 eric 檔
- 只處理商品名稱含「襪」或「袜」的商品
- 僅在安全時 overlay 1688 URL／offer／sku／第二規格／mapping 欄位
- main 為停售／售完，或明顯較新且已驗證 → 保留 main
- 不確定 → 保留 main，並把 `product_id` 列入 needs-human
- 不改 restocker／UI／submit 策略

## needs-human product_id

- `16790492139` — URL 衝突，且該型號無法判定 main 明顯較新驗證勝出
- `18195479361` — main 第二規格遭「收藏加购／优先发货」污染；eric 無可用乾淨第二規格可安全 overlay
- `24527185955` — 同上（第二規格污染）
- `25083569908` — 同上（第二規格污染）
- `29559801000` — 同上（第二規格污染）

## overlaid product_id

_無_

## 說明

目前 main 襪子對應多已是 `approved`＋`1688_sku_id`＋`1688_verified_at`。  
eric PR #11 襪子物件多為 `missing`／`pending`，且常缺 `sku_id`／第二規格；依欄位級安全 overlay **不會**套用任何商品變更。  
第二規格遭 `均码【收藏加购优先发货】` 污染的商品列為 needs-human：直接套用 eric 會清掉 main 既有 `sku_id`，不安全。
