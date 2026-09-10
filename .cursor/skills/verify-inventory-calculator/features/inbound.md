# Inbound

The inbound workbench imports a 1688 order, previews Shopee stock changes, and can write Shopee inventory only when `SHOPEE_INBOUND_WRITE_ENABLED` is true. The default verification path loads the page and asserts the safe read-only banner. It does not import an order or click apply.

## Sub-features

- `inbound-load` shows `1688 到貨入庫` and the order-reference field.
- `inbound-readonly` shows `#systemStatus` as 安全唯讀模式 when write is disabled.
- `inbound-status-api` mirrors that flag on `GET /api/inbound/status`.
- `inbound-import-live` is `#importOrderButton` — opens 1688, default-forbidden.
- `inbound-preview-live` is `#previewButton` — reads live Shopee stock, default-forbidden.
- `inbound-apply-live` is `#applyButton` — writes Shopee inventory, default-forbidden.

## How to get to it (user POV)

- Choose **到貨入庫** on the inventory home header.
- Choose **到貨入庫** from `/sku-mapping.html` or `/products.html`.
- Open `http://127.0.0.1:8080/inbound.html` directly.

## Driving it with control-inventory

Preconditions:

- `control-inventory doctor` reports `"healthy": true`.
- Default proof expects write disabled (`SHOPEE_INBOUND_WRITE_ENABLED` unset or false). If write is enabled, record `writeEnabled: true` and still do not click `#applyButton`.
- Do not paste a 1688 order id unless the user asked for a live import proof.

- **Open inbound.** Open the inbound page. Run `control-inventory drive inbound`. The `h1` contains `到貨入庫`.
- **Wait for banner.** Wait until `#systemStatus strong` is no longer `正在檢查入庫狀態`. With write disabled the strong text is `安全唯讀模式` and the paragraph mentions `SHOPEE_INBOUND_WRITE_ENABLED`.
- **Apply stays disabled.** `#applyButton` remains disabled on the empty page (no receipt loaded).
- **Second view.** Run `control-inventory http GET /api/inbound/status`. JSON `status` is `success`. `writeEnabled` is `false` on the default path. `message` contains `唯讀模式`.
- **Proof.** Keep `inbound.png`, `inbound.dom.txt`, `inbound.status.json`, and `proof.json`. The screenshot shows the read-only banner and the unused `#orderReference` field.

## Gotchas

- `#previewButton` is labeled as a read of live stock. It still opens Shopee. Do not treat the label 讀取即時庫存 as a local dry-run.
- `#applyButton` stays disabled until a receipt exists, but it is still a live write control. Do not enable it by constructing a receipt in DevTools.
- A true write proof needs `.env.local` `SHOPEE_INBOUND_WRITE_ENABLED=true` plus seller login. Report that as `verified-unreachable` unless the user asked for a live inbound proof.
- Importing an order writes `procurement.db` (gitignored). Default recipes never start that job.
