# Inventory dashboard

The inventory home page identifies 莉莉安蝦皮庫存管理系統, shows the overall stock dashboard, and offers search plus local JSON/cookie import. Opening `/` does not load `shopee_products.json` into the table; the dashboard stays at `尚未分析` until the user imports a file or runs live search.

## Sub-features

- `dash-load` shows the branded header, empty dashboard, and search controls on first paint.
- `dash-nav` exposes header links to products, inbound, SKU mapping, Golden import, and ads.
- `dash-bootstrap` exposes `GET /api/home/bootstrap` as the read-only snapshot view (may report a missing `shopee_products.json`).
- `dash-import-local` is the real user path that writes gitignored `shopee_products.json` via `#shopeeProductsImportButton` — not part of the default proof.
- `dash-search-live` is `#searchButton` / `GET /search` — live Shopee crawler, default-forbidden.

## How to get to it (user POV)

- Run `python main.py` (verification: `control-inventory launch`) and open `http://127.0.0.1:8080/`.
- Choose **返回庫存首頁** from `/ads.html` or `/inbound.html`.
- Choose **庫存首頁** from `/sku-mapping.html`, `/products.html`, or `/golden-import.html`.

## Driving it with control-inventory

Preconditions:

- `control-inventory doctor` reports `"healthy": true` at `http://127.0.0.1:8080`.
- This run owns the pid in `/tmp/inventory-verify/state.json`.
- Default proof does not require `shopee_products.json` or `cookies.json`.

- **Open home.** Open the inventory dashboard. Run `control-inventory drive inventory-dashboard`. The page `h1` reads `莉莉安蝦皮庫存管理系統`.
- **Empty dashboard.** Read the summary card. `#overallStatusText` is `尚未分析` and `#restockModelsCount` is `—` when no catalog has been imported or crawled in this browser session.
- **Search controls present.** Confirm `#searchInput` and `#searchButton` exist. Do not click `#searchButton` and do not press Enter in `#searchInput`.
- **Header navigation.** Confirm links `a[href="/products.html"]`, `a[href="/inbound.html"]`, `a[href="/sku-mapping.html"]`, `a[href="/golden-import.html"]`, and `a[href="/ads.html"]`.
- **Bootstrap GET.** Read the snapshot API. Run `control-inventory http GET /api/home/bootstrap --out .cursor/skills/verify-inventory-calculator/evidence/<runId>/inventory-dashboard/home.bootstrap.json`. HTTP 200. JSON `status` is `success` when `shopee_products.json` exists, or `error` with message containing `找不到 shopee_products.json` when it does not. Either outcome is valid empty-dashboard proof.
- **Proof.** Keep `home.png`, `home.dom.txt`, `home.bootstrap.json`, and `proof.json` from the drive. The screenshot shows the heading and the dashboard card. Cleanup must leave these files in place.

## Gotchas

- `#searchButton` always hits `/search`, which starts `crawler.py` against Shopee. An empty keyword is still a live crawl.
- The homepage does not auto-call `/api/home/bootstrap` to fill the table. A successful bootstrap GET does not change `#overallStatusText`.
- `#shopeeProductsImportButton` writes repo-root `shopee_products.json`. If you use that path, restore or delete the file afterwards; do not commit it.
- `#cookieImportButton` writes `cookies.json`. Never type cookie JSON into the page during verification.
- `python main.py` without `INVENTORY_SKIP_BROWSER=1` opens a desktop browser and will kill whatever already bound 8080.
- `#openBatchRestockButton` starts the 1688 cart batch. Leave it untouched.
