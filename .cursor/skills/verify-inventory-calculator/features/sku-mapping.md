# SKU mapping

The SKU mapping workbench lists Golden Table models that have a 1688 URL, shows mapping summary tiles, and lets a seller scan 1688 or approve candidates. Opening the page in a real browser calls `GET /api/sku-mapping/summary`, which constructs `SkuMappingService` and can rewrite tracked `golden_table.json`. The default verification path therefore proves only the static workbench shell, with those APIs aborted.

## Sub-features

- `map-shell` shows `1688 SKU Mapping 工作台`, SKU 審核 selected, and `#scanAll`.
- `map-summary` fills `#summary` from `GET /api/sku-mapping/summary` — **not default**; this GET writes `golden_table.json`.
- `map-queue` fills `#queue` from `GET /api/sku-mapping/queue` — **not default**; same constructor side effect.
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
- `golden_table.json` exists in the repo (it is tracked). Do not let this recipe construct `SkuMappingService`.
- 1688 login is not required for the shell proof.

- **Open workbench shell.** Run `control-inventory drive sku-mapping`. Playwright aborts `**/api/sku-mapping/**` before navigation. The `h1` contains `SKU Mapping`. `#scanAll` contains `重建全部名稱 mapping`.
- **Second view.** The recipe GETs static `/sku-mapping.html` (file serve only) and keeps lines that name the heading and `#scanAll`. Do **not** run `control-inventory http GET /api/sku-mapping/summary`.
- **Do not scan or approve.** Leave `#scanAll`, `#scanVisiblePage`, `#reanalyzeExisting`, `#batchApprove`, and `#checkUrlHealth` unclicked.
- **Proof.** Keep `sku-mapping.png`, `sku-mapping.dom.txt`, `sku-mapping.html.snippet.txt`, and `proof.json`. `proof.json` must include `blockedSkuMappingApis: true`. `golden_table.json` SHA-256 must still match launch.

## Gotchas

- `GET /api/sku-mapping/summary` is not read-only. `SkuMappingService.__init__` calls `_repair_unverified_approvals()`, which can flip `1688_mapping_status` from `approved` to `missing` with `1688_mapping_source=legacy_repair` and write `golden_table.json`.
- Loading `/sku-mapping.html` in a normal browser triggers that GET from `sku-mapping.js`. The default recipe aborts the API on purpose; an error/empty `#summary` is expected.
- A full queue/summary proof requires `--i-approve-golden-write` and an explicit user request. Restore `golden_table.json` from git afterwards unless the write was intended.
- `#scanAll` is not dry-run. Unchecked 強制重抓 still opens 1688 when a snapshot is missing or older than seven days.
- `#batchApprove` writes approved SKUs into `golden_table.json`. Default recipes never click it.
- URL 管理 (`#urlManagerTab`) also hits `/api/sku-mapping/url-groups`. Stay on SKU 審核 with APIs aborted.
