---
name: verify-inventory-calculator
description: "Drive 莉莉安蝦皮庫存管理系統 (InventoryCalculator) through its local HTTP UI at http://127.0.0.1:8080 — launch python main.py, doctor the instance, exercise a mapped page the way a user would, and capture evidence. Use when proving inventory dashboard, ads, inbound, SKU mapping, or products-catalog behavior."
---

# Verify InventoryCalculator

Agent-facing control skill for the local HTTP UI of **莉莉安蝦皮庫存管理系統**. Read this file, then the matching file under `features/`. Drive the real pages at `http://127.0.0.1:8080`. Do not invent a second server, do not use `file://` HTML, and do not call internal JS setters to fake a loaded catalog.

Helper (executable, invocation below):

```bash
.cursor/skills/verify-inventory-calculator/scripts/control-inventory
```

Run it from any cwd. It locates the repo root from its own path.

## Surface

Primary: local `http.server` UI started by `python main.py` on **port 8080**.

| Path | Page |
|---|---|
| `/` (`index.html`) | Inventory dashboard, search, cookie/JSON import, restock toolbar |
| `/ads.html` | Shopee ads export + AI analysis workbench |
| `/inbound.html` | 1688 arrival → Shopee inbound |
| `/sku-mapping.html` | 1688 SKU mapping review |
| `/products.html` | Golden Table product catalog editor |
| `/golden-import.html` | Add Shopee products into Golden Table |

Secondary surfaces (not this skill's default drive): `python -m reverse_audit` CLI, `python calculator.py` PyQt tool, `python3 telegram_bot.py`, Playwright crawlers in `crawler.py` / `alibaba_restocker.py`. Point at those only when the change lives there; still refuse live cart/inventory mutation unless the user explicitly asked for a live proof.

## Isolate

**Two instances cannot run side by side.** `PORT = 8080` is hardcoded in `main.py`. `start_server()` calls `kill_process_on_port(8080)` before bind, so a naive second `python main.py` would steal and kill whoever already holds the port.

- If 8080 is already listening, **do not launch**. Do not drive that shared instance.
- `control-inventory launch` refuses when the port is taken or when a recorded verification pid is still alive.
- There is no data-dir flag. The server always reads `golden_table.json`, `shopee_products.json`, `procurement.db`, and `watchlists/` from the repo root.
- Never copy cookies, `.env.local`, or browser profiles into evidence.

## Launch

Preconditions: Python 3.8+, repo root as cwd for `python main.py`, `pip install -r requirements.txt`, `python3 -m playwright install chromium` (needed for Drive, not for the HTTP server itself).

```bash
.cursor/skills/verify-inventory-calculator/scripts/control-inventory launch
```

This is exactly:

1. Refuse if `127.0.0.1:8080` is in use.
2. `INVENTORY_SKIP_BROWSER=1 python main.py` (skips `webbrowser.open`).
3. Record pid / run id at `/tmp/inventory-verify/state.json`.
4. Wait until `GET http://127.0.0.1:8080/` returns HTML containing `莉莉安蝦皮庫存管理系統`.

Ready signal: that GET succeeds and doctor (below) reports `"healthy": true`.

Do **not** run bare `python main.py` for verification: it opens a desktop browser and will kill any process already on 8080.

Teardown is Cleanup, not Ctrl+C on a guessed pid.

## Doctor

Run this first, and again whenever the instance does something surprising:

```bash
.cursor/skills/verify-inventory-calculator/scripts/control-inventory doctor
```

Doctor is read-only. It must show all of:

- Recorded pid is alive and `/proc/<pid>/cmdline` contains `main.py`
- Port 8080 is in use
- `GET /` is 200 and the body names 莉莉安蝦皮庫存管理系統
- `GET /ads.html`, `/inbound.html`, `/sku-mapping.html`, `/products.html` are 200
- `GET /api/inbound/status` returns JSON with `"status": "success"`
- `golden_table.json` SHA-256 matches the hash recorded at launch

Exit code 2 means do not drive. Relaunch only an instance this helper started.

`GET /api/home/bootstrap` returning `"status": "error"` with `找不到 shopee_products.json` is **healthy enough to drive the empty dashboard**. That file is gitignored and often absent. Do not treat a missing snapshot as a failed doctor.

**Do not** doctor with `GET /api/sku-mapping/summary` or `/api/sku-mapping/queue`. Those GETs construct `SkuMappingService`, which runs `_repair_unverified_approvals()` and can rewrite tracked `golden_table.json` (approved → `missing` / `legacy_repair`). The helper refuses those paths unless `--i-approve-golden-write`.

## Drive

Harness: Playwright Chromium through `control-inventory`, plus `control-inventory http GET <path>` for read-only APIs.

```bash
.cursor/skills/verify-inventory-calculator/scripts/control-inventory drive inventory-dashboard
.cursor/skills/verify-inventory-calculator/scripts/control-inventory drive ads-workbench
.cursor/skills/verify-inventory-calculator/scripts/control-inventory drive inbound
.cursor/skills/verify-inventory-calculator/scripts/control-inventory drive sku-mapping
.cursor/skills/verify-inventory-calculator/scripts/control-inventory drive products-catalog
```

Ad-hoc GET (POST is refused):

```bash
.cursor/skills/verify-inventory-calculator/scripts/control-inventory http GET /api/home/bootstrap --out /tmp/home.bootstrap.json
.cursor/skills/verify-inventory-calculator/scripts/control-inventory http GET /api/inbound/status
.cursor/skills/verify-inventory-calculator/scripts/control-inventory http GET /openai_status
.cursor/skills/verify-inventory-calculator/scripts/control-inventory http GET '/api/golden-table/catalog?query=S6&limit=3'
```

Ad-hoc screenshot of a path we already know is safe:

```bash
.cursor/skills/verify-inventory-calculator/scripts/control-inventory screenshot / --out .cursor/skills/verify-inventory-calculator/evidence/scratch/home.png --wait h1
```

Stable handles (use these, not coordinates):

| Control | Handle |
|---|---|
| App identity | `h1` text `莉莉安蝦皮庫存管理系統` |
| Dashboard status | `#overallStatusText` (empty load: `尚未分析`) |
| Dashboard restock count | `#restockModelsCount` |
| Keyword search (LIVE crawler — do not click) | `#searchInput`, `#searchButton` |
| Local JSON import (writes `shopee_products.json`) | `#shopeeProductsFile`, `#shopeeProductsImportButton` |
| Advanced filter (client-side, after a catalog is loaded) | `#advancedSearchInput`, `#advancedSearchButton` |
| Ads OpenAI banner | `#openaiStatus` |
| Ads export (LIVE Shopee) | `#adsExportButton` |
| Ads analyze (reads local CSV; AI calls OpenAI) | `#adsAnalyzeButton`, `#includeAiAnalysis` |
| Inbound banner | `#systemStatus` |
| Inbound import / apply (LIVE) | `#orderReference`, `#importOrderButton`, `#applyButton` |
| SKU summary / queue | `#summary`, `#queue`, `#query`, `#reload` — **do not** hit `/api/sku-mapping/*` in the default proof |
| SKU live scan / approve (LIVE or writes golden) | `#scanAll`, `#batchApprove` |
| Products catalog search (local Golden Table) | `#productSearchInput`, `#productSearchButton`, `#productSearchStatus` |

Default recipes are **read-only**. They never click the live/mutation selectors listed in Helpers.

Live Shopee cookies, 1688 login, OpenAI keys, and Chrome profiles stay on the operator machine. If a change can only be proven after seller-center login, document the unmet auth precondition and prove the local UI path instead. Do not paste secrets into this skill, into evidence, or into chat.

## Evidence

Proof artifacts go to:

```text
.cursor/skills/verify-inventory-calculator/evidence/<runId>/<feature>/
```

`<runId>` is the UTC stamp recorded by `launch` (also in `/tmp/inventory-verify/state.json`). Cleanup **must not** delete this tree.

Each drive writes at least:

- `doctor.json` — the doctor report taken immediately before the drive
- `<feature>.png` or `home.png` — full-page screenshot with the app identity visible
- `<feature>.dom.txt` — URL, `<title>`, `h1`, and the observed status strings
- a GET body used as the second view (`home.bootstrap.json`, `inbound.status.json`, …)
- `proof.json` — feature id, run id, and the assertions that passed

Proof standards:

- Exercise the real user path (open the page, use the labeled control). Do not assign `window.lastSearchResults` in DevTools to fake a catalog.
- Capture the action and the resulting state, not only a final screenshot.
- Verify a second read-only view (HTTP GET of the same data the page just rendered).
- Do not trust a button named dry-run / 預覽 / 唯讀 without observing it: inbound `#previewButton` still opens Shopee to read live stock; `#scanAll` still opens 1688 unless a fresh snapshot exists.
- Mocks are allowed only inside `tests/` unit files. This skill drives the running `main.py`.

## Cleanup

```bash
.cursor/skills/verify-inventory-calculator/scripts/control-inventory cleanup
```

Cleanup sends `GET /shutdown` to the recorded instance, waits, then SIGTERM/SIGKILL **that pid only**. It removes `/tmp/inventory-verify/state.json`. It never deletes `evidence/`. After cleanup, confirm the proof files still exist at the path printed as `evidenceKeptAt`.

If a drive fails, run cleanup before the next launch so 8080 is not left occupied.

Do not recover a wedged UI by calling `kill_process_on_port` or `pkill -f main.py`.

## Helpers

Script: `.cursor/skills/verify-inventory-calculator/scripts/control-inventory` (executable Python).

| Command | What it does |
|---|---|
| `control-inventory launch` | Start `INVENTORY_SKIP_BROWSER=1 python main.py` if 8080 is free |
| `control-inventory doctor` | Read-only ownership + page/API check |
| `control-inventory http GET <path>` | Fetch JSON/HTML from the launched instance |
| `control-inventory drive <feature>` | Playwright recipe for one mapped feature |
| `control-inventory screenshot <path> --out FILE [--wait SEL]` | One-off page capture |
| `control-inventory cleanup` | Tear down the recorded pid; keep evidence |

Forbidden unless the user explicitly asked for a live Shopee/1688 proof (the helper refuses these selectors without `--i-approve-live`, and the built-in recipes never pass that flag): `#searchButton`, `#cookieImportButton`, `#shopeeProductsImportButton`, `#openBatchRestockButton`, `#adsExportButton`, `#adsAnalyzeButton`, `#importOrderButton`, `#previewButton`, `#applyButton`, `#scanAll`, `#scanVisiblePage`, `#batchApprove`, `#checkUrlHealth`, and the other ids listed in `FORBIDDEN_SELECTORS` inside the helper.

`GET /api/sku-mapping/*` is also refused (constructs `SkuMappingService` and may rewrite `golden_table.json`) unless `--i-approve-golden-write`. Doctor records `golden_table.json` SHA-256 at launch and fails if it changes.

Maintenance: `/maintain-verification-skill` keeps this map honest when pages or APIs change. Do not edit product code while only maintaining this skill.
