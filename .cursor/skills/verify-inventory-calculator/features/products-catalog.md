# Products catalog

The products page searches Golden Table in-process. It does not run the Shopee crawler. A user can then edit 1688 URL / SKU mappings; those saves write `golden_table.json` and are out of the default proof.

## Sub-features

- `catalog-load` shows `商品資料管理` and the search form.
- `catalog-search` queries `#productSearchInput` and reports `找到 N 個商品` on `#productSearchStatus`.
- `catalog-results` renders matching product cards in `#productResults`.
- `catalog-save` is `#saveSkuMappingButton` / `#saveAlibabaEditButton` — writes Golden Table, default-forbidden.

## How to get to it (user POV)

- Choose **商品資料** on the inventory home header.
- Open `http://127.0.0.1:8080/products.html` directly.

## Driving it with control-inventory

Preconditions:

- `control-inventory doctor` reports `"healthy": true`.
- Tracked `golden_table.json` is present (repo includes it). Query `S6` matches at least the 純色棉襪 S6 product.

- **Open catalog.** Open the products page. Run `control-inventory drive products-catalog`. The `h1` contains `商品資料`.
- **Search locally.** The recipe fills `#productSearchInput` with `S6` and clicks `#productSearchButton`. `#productSearchStatus` contains `找到` and a product count. Status copy includes `正在直接讀取 Golden Table` only while the request is in flight.
- **Second view.** Run `control-inventory http GET '/api/golden-table/catalog?query=S6&limit=3'`. JSON `status` is `success` and `totalMatches` is greater than zero. Product names in `products` include `S6` or 棉襪.
- **Do not save.** Leave mapping / 1688 edit modals closed. Do not click `#saveSkuMappingButton` or `#saveAlibabaEditButton`.
- **Proof.** Keep `products.png`, `products.dom.txt`, `products.catalog.json`, and `proof.json`. The screenshot shows the heading, the query, and at least one result or the `找到` status.

## Gotchas

- This search is not the home-page `#searchButton`. Home search crawls Shopee; this page only reads Golden Table.
- Empty query still hits `/api/golden-table/catalog`. Prefer a concrete token such as `S6` so the proof names a real product.
- Saving from the SKU or 1688 modal overwrites tracked `golden_table.json`. Do not use those controls to "make the screenshot nicer".
- Images load from Shopee CDNs. A broken thumbnail is not a failed catalog search; assert `#productSearchStatus` and the GET body.
