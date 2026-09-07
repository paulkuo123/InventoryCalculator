# Phase 1 信任分級（庭安 2026-09-07 選定，同日修訂）

審核／閘門粒度 = **型號（規格）列**，不是整商品。

## 重要：多來源不一定錯

同一蝦皮商品的不同型號，常會訂不同 1688 店家／offer。  
因此：**不可**因「商品層有多個 offer」就整商品打成不可採購。

工作台可標「同商品多來源（依型號）」作資訊；**不要**僅因 multi-offer 把已合格的型號踢出可採購。  
僅當**同一型號**自己對到互相矛盾的 offer／sku（列級 conflict）時，才擋該型號。

## 可採購／certain（自動信任，該型號）

該型號列同時滿足：

1. `1688_mapping_status=approved`（或同等）
2. 有有效 `1688_sku_id`
3. 該型號有明確 1688 URL／offer（可與同商品其他型號不同）
4. **該型號**無衝突標籤（例如 `shared_sku_conflict`、`sku_approved_conflict`）
5. 非 discontinued／sold-out
6. 來源／SKU 審核非 `rejected`

不要求「整商品只能一個 offer」。實作見 `mapping_procurement_gate.is_auto_trusted`。

## 必須待補驗證（該型號不可採購）

- 缺 `1688_sku_id`
- 該型號缺 URL／offer
- **該型號** conflict／rejected／證據不足
- incomplete 到無法下單（缺關鍵欄）

工作台可對缺欄列人工蓋 `1688_phase1_verified_at`（Phase 1 stamp）作為覆蓋路徑（例如 name/spec only）。

## 與舊欄位

- 舊的 `1688_verified_at`（legacy auto-approve）**單獨不算**信任。
- 寫入閘門（`confirmWrite` + `WRITE_GOLDEN`）與鎖定舊路徑規則不變；見 [locked_paths.md](locked_paths.md)。
