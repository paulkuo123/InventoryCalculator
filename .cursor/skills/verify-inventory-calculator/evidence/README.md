# Verification evidence

Proof artifacts from `control-inventory` land here. Cleanup never deletes this directory.

Layout:

```text
evidence/<runId>/<feature>/
  doctor.json
  proof.json
  *.png
  *.dom.txt
  *.json    # second-view GET bodies
```

`<runId>` is the UTC stamp created by `control-inventory launch` (also stored in `/tmp/inventory-verify/state.json`).

Canonical executed proof (survived cleanup; `golden_table.json` hash unchanged):

```text
evidence/20260910T153349Z/inventory-dashboard/
```

Earlier pass `evidence/20260910T153042Z/` also drove the dashboard; its doctor still called `/api/sku-mapping/summary` (side effect restored, see that run's `RUN.md`).

Do not put `cookies.json`, `.env.local`, OpenAI keys, or browser profiles in this tree.
