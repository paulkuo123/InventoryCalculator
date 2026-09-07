# Phase 1 locked write paths

These paths can no longer write or auto-approve Golden Table SKUs.

| Path | How locked | Status |
|---|---|---|
| `POST /api/alibaba/sku-review/apply` | Handler calls `reject_locked_path` before any JSON write | **410 Gone** |
| `POST /api/golden-table/model-alibaba` with `applyScope=overwrite_all` | `reject_overwrite_all` before models are copied | **410 Gone** |
| Product editor `mappingApproved: Boolean(skuName)` | UI always sends `mappingApproved: false`; API ignores the flag and never promotes to `approved` | blocked |
| `alibaba_sku_mapper.py --apply-high-confidence` | `main()` calls `reject_legacy_mapper` | **SystemExit** |
| `alibaba_phone_case_mapper.py --apply-high-confidence` / `--apply-safe-primary-only` / `--propagate-existing-primary` | same | **SystemExit** |

Allowed writes:

- `/api/sku-mapping/decisions` and `/api/sku-mapping/source-review` **only** with `confirmWrite=true` and `confirmPhrase=WRITE_GOLDEN`, plus `logs/golden_write_audit.jsonl`
- `applyScope=url_offer_all` still updates URL/Offer only (PR #38)
- `applyScope=single` may edit URL/SKU names but **cannot** set `1688_mapping_status=approved`

Raw `approved` without `1688_phase1_verified_at` is not purchasable / not reverse_audit certain. Existing `1688_verified_at` from legacy auto-approvals is ignored. This PR does not bulk-rewrite `golden_table.json`.
