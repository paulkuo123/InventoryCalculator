# SKU mapping

The SKU mapping workbench lists Golden Table models that have a 1688 URL, shows mapping summary tiles, and lets a seller scan 1688 or approve candidates. The default verification path only loads the summary and queue. It does not scan 1688, run AI, or write approvals.

## Sub-features

- `map-load` shows `1688 SKU Mapping 工作台` with SKU 審核 selected.
- `map-summary` fills `#summary` with 全部有 1688 URL 型號 and 目前需要補貨.
- `map-queue` fills `#queue` or shows `#empty` for the current `#status` filter (default `待處理`).
- `map-filter` uses `#query`, `#status`, `#tier`, `#reload` without starting a scan.
- `map-scan-live` is `#scanAll` / `#scanVisiblePage` — may open 1688, default-forbidden.
- `map-approve` is `#batchApprove` or per-card 核准選取 SKU — writes golden / db, default-forbidden.

## How to get to it (user POV)

- Choose **1688 SKU Mapping** on the inventory home header.
- Open `http://127.0.0.1:8080/sku-mapping.html` directly.
- Choose **1688 SKU Mapping** from `/golden-import.html`.

## Driving it with control-inventory

Preconditions:

- `control-inventory doctor` reports `"healthy": true`.
- `golden_table.json` exists in the repo (it is tracked). `procurement.db` may be created on first summary read; do not commit it.
- 1688 login is not required for summary/queue.

- **Open workbench.** Open the mapping page. Run `control-inventory drive sku-mapping`. The `h1` contains `SKU Mapping`.
- **Wait for summary.** Wait until `#summary` contains `全部有 1688 URL 型號`. Tiles include `已核准` and `有候選待核准`.
- **Queue or empty.** `#queue` has `.card` rows, or `#empty` is visible with `目前沒有符合篩選條件的型號`. Either is valid. `#pagination` reports a `共 N 筆` count when rows exist.
- **Second view.** Run `control-inventory http GET /api/sku-mapping/summary`. JSON `status` is `success` and `urlModels` is a number. Optional: `control-inventory http GET '/api/sku-mapping/queue?status=review&page=1&pageSize=5'`.
- **Do not scan.** Leave `#scanAll`, `#scanVisiblePage`, `#reanalyzeExisting`, `#batchApprove`, and `#checkUrlHealth` unclicked.
- **Proof.** Keep `sku-mapping.png`, `sku-mapping.dom.txt`, `sku-mapping.summary.json`, and `proof.json`. The screenshot shows the heading and the summary tiles.

## Gotchas

- `#scanAll` is not dry-run. Unchecked 強制重抓 still opens 1688 when a snapshot is missing or older than seven days. Use `#reanalyzeExisting` only if a later map entry explicitly covers snapshot-only rebuilds.
- Default `#status` is `待處理`. An empty queue does not mean mapping is broken; switch to `全部` only as a read-only filter change (`#status` then `#reload`).
- `#batchApprove` writes approved SKUs into `golden_table.json`. That is product SoT. Default recipes never click it.
- First load can take several seconds while `SkuMappingService` opens `procurement.db`. Wait for the summary text, not a fixed sleep.
- URL 管理 (`#urlManagerTab`) is a second tab. Default proof stays on SKU 審核.
