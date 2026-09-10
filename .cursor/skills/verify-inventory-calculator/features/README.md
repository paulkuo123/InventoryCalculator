# InventoryCalculator verification map

This directory is the maintained source for verifying the user-facing behavior of 莉莉安蝦皮庫存管理系統. Read the index before driving the app, then use the matching feature file as the recipe.

## Baseline preconditions

- Launch with `.cursor/skills/verify-inventory-calculator/scripts/control-inventory launch`.
- Require `control-inventory doctor` to report `"healthy": true` and `baseUrl` `http://127.0.0.1:8080`.
- Never drive an instance that was not started by this verification run. Port 8080 is shared and hardcoded.
- Prefer the read-only recipes. Do not click `#searchButton`, `#adsExportButton`, `#importOrderButton`, `#applyButton`, `#scanAll`, or `#batchApprove`.
- Do not paste `cookies.json`, `.env.local`, or API keys into the session or into evidence.
- `shopee_products.json` is gitignored. A missing file is a valid empty-dashboard precondition, not a failed launch.
- Restore any local file you write (`shopee_products.json`, `procurement.db` is created on SKU-mapping reads). Do not remove proof artifacts during cleanup.

## Driving conventions

- Start every recipe from the baseline state unless its preconditions say otherwise.
- Prefer the ids and accessible names in this map over CSS position or tab order.
- Treat every `control-inventory` command as literal.
- Browser actions go through `control-inventory drive` or `control-inventory screenshot`.
- HTTP checks go through `control-inventory http GET`. POST is refused by the helper.
- Cleanup removes the server pid only. Evidence stays under `.cursor/skills/verify-inventory-calculator/evidence/<runId>/`.

## Proof and skip reporting

- Capture the user action and the resulting state, not only the final screen.
- UI proof includes a DOM text snapshot and a screenshot with 莉莉安蝦皮庫存管理系統 (or the page's own `h1`) visible.
- HTTP proof includes the GET path, status code, and JSON body.
- Mutation proof is out of scope for the default recipes. If a later run must import local JSON, restore the original file and keep a second GET of `/api/home/bootstrap`.
- Record the feature id and entry point with every artifact.
- Report an unreachable path with the attempted command and the unmet precondition (usually missing Shopee cookies, 1688 login, or `SHOPEE_INBOUND_WRITE_ENABLED`).
- Do not report a skipped live entry point as verified through a local GET.

## Feature entry contract

Each feature file starts with an H1 title and one paragraph describing the user-visible behavior. It then uses exactly four H2 sections in this order.

1. `Sub-features` lists short IDs with one line for each behavior.
2. `How to get to it (user POV)` lists every user entry point.
3. `Driving it with control-inventory` starts with `Preconditions:` and uses labeled bullets that pair each user action with an exact command and observable result.
4. `Gotchas` lists traps that can waste or invalidate a verification run.

Keep implementation details out of the map. Name only user paths, stable handles, required state, commands, and observable proof.

## Features

- [Inventory dashboard](./inventory-dashboard.md) covers the home page load, empty dashboard, header navigation, and the forbidden live search control.
- [Ads workbench](./ads-workbench.md) covers `/ads.html` identity, OpenAI status, and the export/analyze controls that must not be clicked in the default proof.
- [Inbound](./inbound.md) covers `/inbound.html` read-only status and the gated Shopee write switch.
- [SKU mapping](./sku-mapping.md) covers `/sku-mapping.html` summary and queue load without scanning 1688 or approving mappings.
- [Products catalog](./products-catalog.md) covers `/products.html` local Golden Table search without saving edits.
