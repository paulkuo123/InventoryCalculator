# SKU mapping

The SKU mapping workbench lists Golden Table models that have a 1688 URL, shows mapping summary tiles, and lets a seller scan 1688 or approve candidates. Opening the page in a real browser calls `GET /api/sku-mapping/summary`. That GET constructs `SkuMappingService` but must **not** rewrite tracked `golden_table.json`. Scan / approve remain write paths.

## Sub-features

- `map-shell` shows `1688 SKU Mapping 工作台`, SKU 審核 selected, and `#scanAll`.
- `map-summary` fills `#summary` from `GET /api/sku-mapping/summary`.
- `map-queue` fills `#queue` from `GET /api/sku-mapping/queue`.
- `map-filter` uses `#query`, `#status`, `#tier`, `#reload` without starting a scan.
- `map-scan-live` is `#scanAll` / `#scanVisiblePage` — may open 1688, default-forbidden.
- `map-approve` is `#batchApprove` or per-card 核准選取 SKU — writes golden / db, default-forbidden.

## How to get to it (user POV)

- Choose **1688 SKU Mapping** on the inventory home header.
- Open `http://127.0.0.1:8080/sku-mapping.html` directly.
- Choose **1688 SKU Mapping** from `/golden-import.html`.

## Driving it with control-inventory

Preconditions:

- `control-inventory doctor` reports `"healthy": true` and `goldenTable.unchanged`.
- `golden_table.json` exists in the repo (it is tracked).
- 1688 login is not required for the summary/queue proof.

- **Open workbench.** Run `control-inventory drive sku-mapping`. Playwright loads `/sku-mapping.html` and waits until `#summary` contains `已核准`. The `h1` contains `SKU Mapping`. `#scanAll` contains `重建全部名稱 mapping`.
- **Second view.** The recipe GETs `/api/sku-mapping/summary` and keeps `sku-mapping.summary.json`. `status` must be `success`.
- **Do not scan or approve.** Leave `#scanAll`, `#scanVisiblePage`, `#reanalyzeExisting`, `#batchApprove`, and `#checkUrlHealth` unclicked.
- **Proof.** Keep `sku-mapping.png`, `sku-mapping.dom.txt`, `sku-mapping.summary.json`, and `proof.json`. `proof.json` must include `goldenUnchanged: true`. `golden_table.json` SHA-256 must still match launch.

## Gotchas

- Read-path construction must not call `_repair_unverified_approvals()` or `_backfill_legacy_fields()`. Those can flip `1688_mapping_status` from `approved` to `missing` with `1688_mapping_source=legacy_repair`. Run that only via `python -m sku_mapping_service repair` or `SKU_MAPPING_REPAIR_GOLDEN=1`.
- Loading `/sku-mapping.html` in a normal browser triggers `GET /api/sku-mapping/summary` from `sku-mapping.js`. That is now the default proof.
- `#scanAll` is not dry-run. Unchecked 強制重抓 still opens 1688 when a snapshot is missing or older than seven days.
- `#batchApprove` writes approved SKUs into `golden_table.json`. Default recipes never click it.
- URL 管理 (`#urlManagerTab`) hits `/api/sku-mapping/url-groups` (also a GET). Stay on SKU 審核 unless proving URL groups; still do not click `#checkUrlHealth`.
