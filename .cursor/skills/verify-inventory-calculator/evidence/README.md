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

Do not put `cookies.json`, `.env.local`, OpenAI keys, or browser profiles in this tree.
