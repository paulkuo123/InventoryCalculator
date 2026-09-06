"""Offline reverse-restock dry-run (frozen sources + live pool JSONs).

Read-only: never mutates cart. shortfall → PAUSED (no auto qty fix).
Phase 1 also lists qty_excess + unexpected_in_cart (no PAUSE on excess).
Never invents URL/skuId into golden_table — uncertain (true missing fields)
are recorded only. Certain = approved + URL + (skuId OR usable name/spec);
unique cart name/spec match resolves live skuId for qty diff.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from home_bootstrap import (
    load_watchlist_exclusion_ids,
    merged_watchlist_exclusion_ids,
    parse_watchlist_payload,
    without_watchlist_exclusions,
)
from restock_rules import (
    calculated_restock_details,
    target_months_for_product,
)

from reverse_audit.paths import repo_root
from reverse_audit.util import (
    KNOWN_SOLDOUT,
    KNOWN_SOLDOUT_OFFERS,
    as_int,
    is_discontinued_sku,
    load_json,
    now_iso,
    offer_from_url,
    sha256_file,
    write_csv,
    write_csv_utf8_sig,
)

CONSOLIDATED_CSV_NAME = "補貨比對結果.csv"
CONSOLIDATED_FIELDS = [
    "類型",
    "蝦皮商品id",
    "蝦皮規格id",
    "蝦皮商品名稱",
    "型號",
    "應補數量",
    "車內數量",
    "差額說明",
    "1688網址",
    "1688_offer",
    "1688_sku",
    "備註",
]

SRC_NAMES = (
    "shopee_products.json",
    "golden_table.json",
    "personal_watchlist.json",
    "personal_watchlist_exclusions.json",
)


def source_map(root: Path) -> Dict[str, Path]:
    return {
        "shopee_products.json": root / "shopee_products.json",
        "golden_table.json": root / "golden_table.json",
        "personal_watchlist.json": root / "watchlists" / "personal_watchlist.json",
        "personal_watchlist_exclusions.json": root
        / "watchlists"
        / "personal_watchlist_exclusions.json",
    }


def freeze_sources(out_dir: Path, root: Optional[Path] = None) -> Dict[str, Any]:
    """Copy shopee/golden/watchlist into out_dir/sources with SHA256 manifest."""
    import shutil

    root = root or repo_root()
    sources = out_dir / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    manifest: Dict[str, Any] = {
        "frozenAt": now_iso(),
        "timezone": "Asia/Taipei",
        "files": {},
    }
    for name, src in source_map(root).items():
        if not src.exists():
            raise FileNotFoundError(f"missing source: {src}")
        dest = sources / name
        shutil.copy2(src, dest)
        digest = sha256_file(dest)
        manifest["files"][name] = {
            "sourcePath": str(src),
            "copiedTo": str(dest),
            "bytes": dest.stat().st_size,
            "sha256": digest,
        }
    (sources / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def golden_index(golden: Dict[str, Any], product_id: str) -> Tuple[Dict[str, Dict], Dict[str, Dict]]:
    entry = golden.get(str(product_id)) or {}
    by_spec: Dict[str, Dict] = {}
    by_name: Dict[str, Dict] = {}
    if not isinstance(entry, dict):
        return by_spec, by_name
    for gm in entry.get("型號") or []:
        if not isinstance(gm, dict):
            continue
        sid = str(gm.get("規格ID") or "").strip()
        nm = str(gm.get("型號名稱") or "").strip()
        if sid:
            by_spec[sid] = gm
        if nm:
            by_name[nm] = gm
    return by_spec, by_name


def classify_skip_reason(
    status: str,
    sku_name: str,
    offer_id: str,
    sku_id: str,
) -> Optional[str]:
    if status == "discontinued":
        return "mapping_status=discontinued"
    if is_discontinued_sku(sku_name):
        return f"discontinued_sku_name={sku_name}"
    if (offer_id, sku_id) in KNOWN_SOLDOUT:
        return "known_soldout_hidden(粉色愛心兔)"
    if offer_id in KNOWN_SOLDOUT_OFFERS and not sku_id:
        return "known_soldout_offer(粉色愛心兔)"
    if status in {"stale"} and is_discontinued_sku(sku_name):
        return "stale+discontinued"
    return None


def build_expected(
    products: Dict[str, Any],
    golden: Dict[str, Any],
    watch_ids: List[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    certain_rows: List[Dict[str, Any]] = []
    uncertain_rows: List[Dict[str, Any]] = []
    skip_rows: List[Dict[str, Any]] = []
    stats = {
        "watchlistAfterExclusions": len(watch_ids),
        "intersectionProductCount": 0,
        "modelsScanned": 0,
        "modelsNeedRestock": 0,
        "modelsNoNeed": 0,
    }

    products = {str(k): v for k, v in products.items()}
    golden = {str(k): v for k, v in golden.items()}
    product_ids = [pid for pid in watch_ids if pid in products and pid in golden]
    stats["intersectionProductCount"] = len(product_ids)
    watch_order = {pid: i for i, pid in enumerate(watch_ids)}

    for pid in product_ids:
        prod = products.get(pid) or {}
        if not isinstance(prod, dict):
            continue
        name = str(prod.get("商品名稱") or "")
        by_spec, by_name = golden_index(golden, pid)
        models = prod.get("型號") or []
        if isinstance(models, dict):
            models = list(models.values())
        for sm in models:
            if not isinstance(sm, dict):
                continue
            stats["modelsScanned"] += 1
            spec_id = str(sm.get("規格ID") or "").strip()
            model_name = str(sm.get("型號名稱") or "").strip()
            months = target_months_for_product(name, 4, model_name)
            details = calculated_restock_details(prod, sm, months)
            suggested = as_int(details.get("suggestedQty"))
            if suggested <= 0:
                stats["modelsNoNeed"] += 1
                continue
            stats["modelsNeedRestock"] += 1

            gm = by_spec.get(spec_id) or by_name.get(model_name) or {}
            url = str(gm.get("阿里巴巴商品URL") or sm.get("阿里巴巴商品URL") or "").strip()
            offer_id = str(gm.get("1688_offer_id") or "").strip() or offer_from_url(url)
            sku_id = str(gm.get("1688_sku_id") or "").strip()
            sku_name = str(gm.get("1688_sku_name") or "").strip()
            sku_second = str(gm.get("1688_sku_second_name") or "").strip()
            status = str(gm.get("1688_mapping_status") or "").strip()
            is_case = months == 3

            base = {
                "product_id": pid,
                "product_name": name,
                "spec_id": spec_id,
                "model_name": model_name,
                "is_phone_case": is_case,
                "target_months": months,
                "current_stock": details.get("currentStock"),
                "monthly_sales": details.get("monthlySales"),
                "effective_monthly_sales": details.get("effectiveMonthlySales"),
                "raw_shortage": details.get("rawShortage"),
                "suggested_qty": suggested,
                "alibaba_url": url,
                "offer_id": offer_id,
                "sku_id": sku_id,
                "sku_name": sku_name,
                "sku_second_name": sku_second,
                "mapping_status": status or ("missing" if not (url or sku_name) else ""),
                "watchlist_order": watch_order.get(pid, 10**9),
            }

            skip_reason = classify_skip_reason(status, sku_name, offer_id, sku_id)
            if skip_reason:
                row = dict(base)
                row["bucket"] = "skip"
                row["skip_reason"] = skip_reason
                skip_rows.append(row)
                continue

            url_ok = url.startswith("http")
            has_sku = bool(sku_id)
            has_name_spec = bool(sku_name)
            certain = status == "approved" and url_ok and (has_sku or has_name_spec)
            if certain:
                row = dict(base)
                row["bucket"] = "certain"
                row["certain_via"] = "sku_id" if has_sku else "name_spec"
                if not has_sku:
                    row["note"] = "approved+URL+name/spec；缺 skuId（qty 以唯一 name/spec 對車）"
                certain_rows.append(row)
            else:
                missing = []
                if status != "approved":
                    missing.append(f"status={status or 'empty'}")
                if not url_ok:
                    missing.append("url")
                if not has_sku and not has_name_spec:
                    missing.append("skuId+name/spec")
                elif not has_sku:
                    missing.append("skuId")
                row = dict(base)
                row["bucket"] = "uncertain"
                row["uncertain_reason"] = "缺欄:" + ",".join(missing)
                uncertain_rows.append(row)

    return certain_rows, uncertain_rows, skip_rows, stats


def _agg_bucket_key(row: Dict[str, Any]) -> Tuple[Any, ...]:
    """Aggregate by (offerId, skuId), or by name identity when skuId still empty."""
    oid = str(row.get("offer_id") or "").strip()
    sid = str(row.get("sku_id") or "").strip()
    if sid:
        return ("sku", oid, sid)
    return (
        "name",
        oid,
        str(row.get("sku_name") or "").strip(),
        str(row.get("sku_second_name") or "").strip(),
    )


def aggregate_certain(certain_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    agg: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for row in certain_rows:
        key = _agg_bucket_key(row)
        if key not in agg:
            sid = str(row.get("sku_id") or "").strip()
            agg[key] = {
                "offer_id": str(row.get("offer_id") or "").strip(),
                "sku_id": sid,
                "suggested_qty": 0,
                "is_phone_case": False,
                "watchlist_order": row.get("watchlist_order", 10**9),
                "alibaba_url": row.get("alibaba_url") or "",
                "sku_name": row.get("sku_name") or "",
                "sku_second_name": row.get("sku_second_name") or "",
                "certain_via": row.get("certain_via") or ("sku_id" if sid else "name_spec"),
                "sku_id_resolved_from_cart": bool(row.get("sku_id_resolved_from_cart")),
                "product_ids": [],
                "product_names": [],
                "model_names": [],
                "spec_ids": [],
                "sources": [],
            }
        a = agg[key]
        a["suggested_qty"] += as_int(row.get("suggested_qty"))
        a["is_phone_case"] = a["is_phone_case"] or bool(row.get("is_phone_case"))
        a["watchlist_order"] = min(a["watchlist_order"], as_int(row.get("watchlist_order")))
        if row.get("sku_id_resolved_from_cart"):
            a["sku_id_resolved_from_cart"] = True
        if row.get("product_id") and row["product_id"] not in a["product_ids"]:
            a["product_ids"].append(row["product_id"])
        if row.get("product_name") and row["product_name"] not in a["product_names"]:
            a["product_names"].append(row["product_name"])
        if row.get("model_name"):
            a["model_names"].append(row["model_name"])
        sid_shopee = str(row.get("spec_id") or "").strip()
        if sid_shopee and sid_shopee not in a["spec_ids"]:
            a["spec_ids"].append(sid_shopee)
        a["sources"].append(
            {
                "product_id": row.get("product_id"),
                "model_name": row.get("model_name"),
                "suggested_qty": row.get("suggested_qty"),
                "target_months": row.get("target_months"),
            }
        )
    out = list(agg.values())
    out.sort(
        key=lambda r: (
            -as_int(r.get("suggested_qty")),
            0 if r.get("is_phone_case") else 1,
            as_int(r.get("watchlist_order")),
            str(r.get("offer_id")),
            str(r.get("sku_id")),
            str(r.get("sku_name") or ""),
            str(r.get("sku_second_name") or ""),
        )
    )
    return out


def index_cart(cart: Dict[str, Any]) -> Tuple[Dict[Tuple[str, str], Dict[str, Any]], List[Dict[str, Any]]]:
    by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    ambiguous_cart: List[Dict[str, Any]] = []
    for item in cart.get("items") or []:
        if not isinstance(item, dict):
            continue
        oid = str(item.get("offerId") or "").strip()
        sid = str(item.get("skuId") or "").strip()
        qty = as_int(item.get("qty") if item.get("qty") is not None else item.get("quantity"))
        if not oid or not sid:
            ambiguous_cart.append(
                {
                    "kind": "cart_missing_ids",
                    "offer_id": oid,
                    "sku_id": sid,
                    "qty": qty,
                    "specText": item.get("specText") or item.get("skuName") or "",
                    "cartId": item.get("cartId") or "",
                    "note": "cart line missing offerId or skuId",
                }
            )
            continue
        key = (oid, sid)
        if key not in by_key:
            by_key[key] = {
                "offer_id": oid,
                "sku_id": sid,
                "qty": 0,
                "cartIds": [],
                "specTexts": [],
                "effective": bool(item.get("effective", True)),
            }
        by_key[key]["qty"] += qty
        cid = str(item.get("cartId") or "")
        if cid and cid not in by_key[key]["cartIds"]:
            by_key[key]["cartIds"].append(cid)
        spec = str(item.get("specText") or item.get("skuName") or "")
        if spec and spec not in by_key[key]["specTexts"]:
            by_key[key]["specTexts"].append(spec)
    return by_key, ambiguous_cart


def index_orders(
    pools: Dict[str, Dict[str, Any]],
) -> Tuple[Dict[Tuple[str, str], Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    ambiguous: List[Dict[str, Any]] = []
    validation: Dict[str, Any] = {"pools": {}, "risks": []}

    order_id_sets: Dict[str, set] = {}
    for pool_name, data in pools.items():
        labels = data.get("labels") or []
        order_ids = [str(x) for x in (data.get("orderIds") or [])]
        order_id_sets[pool_name] = set(order_ids)
        page_url = str(data.get("pageUrl") or "")
        validation["pools"][pool_name] = {
            "labels": labels,
            "nOrders": data.get("nOrders"),
            "nLines": data.get("nLines") or len(data.get("orders") or []),
            "orderIds": order_ids,
            "orderIdCount": len(order_ids),
            "complete": data.get("complete"),
            "pageUrl": page_url,
            "pageUrlLooksLikeWaitBuyerReceive": "orderStatus=waitbuyerreceive" in page_url,
        }
        for line in data.get("orders") or []:
            if not isinstance(line, dict):
                continue
            oid = str(line.get("offerId") or "").strip()
            sid = str(line.get("skuId") or "").strip()
            qty = as_int(line.get("qty"))
            order_id = str(line.get("orderId") or "")
            if not oid or not sid:
                ambiguous.append(
                    {
                        "kind": "order_missing_ids",
                        "pool": pool_name,
                        "offer_id": oid,
                        "sku_id": sid,
                        "orderId": order_id,
                        "qty": qty,
                        "specText": line.get("specText") or line.get("skuName") or "",
                        "note": "order line missing offerId or skuId",
                    }
                )
                continue
            key = (oid, sid)
            if key not in by_key:
                by_key[key] = {
                    "offer_id": oid,
                    "sku_id": sid,
                    "pools": [],
                    "orderIds": [],
                    "qty_sum": 0,
                    "lines": 0,
                }
            rec = by_key[key]
            if pool_name not in rec["pools"]:
                rec["pools"].append(pool_name)
            if order_id and order_id not in rec["orderIds"]:
                rec["orderIds"].append(order_id)
            rec["qty_sum"] += qty
            rec["lines"] += 1

    names = list(order_id_sets.keys())
    overlaps = {}
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            inter = sorted(order_id_sets[a] & order_id_sets[b])
            overlaps[f"{a}∩{b}"] = inter
            if inter:
                validation["risks"].append(f"orderId overlap {a}∩{b}: {inter}")
    validation["orderIdOverlaps"] = overlaps
    validation["orderIdsDistinct"] = all(len(v) == 0 for v in overlaps.values())

    label_sets = {k: set(v.get("labels") or []) for k, v in validation["pools"].items()}
    validation["labels"] = {k: sorted(v) for k, v in label_sets.items()}
    validation["labelsDistinct"] = len(set(tuple(sorted(s)) for s in label_sets.values())) == len(
        label_sets
    )

    wait_flags = [
        (k, v["pageUrlLooksLikeWaitBuyerReceive"], v["pageUrl"])
        for k, v in validation["pools"].items()
    ]
    if all(flag for _, flag, _ in wait_flags):
        validation["risks"].append(
            "ALL three order pool pageUrls contain orderStatus=waitbuyerreceive "
            "(capture URL may not reflect tab filter; rely on labels/orderIds for pool identity)"
        )
    validation["pageUrlRisk"] = all(flag for _, flag, _ in wait_flags)
    validation["poolsTrulyDifferent"] = bool(
        validation["orderIdsDistinct"] and validation["labelsDistinct"]
    )
    return by_key, ambiguous, validation


def _cart_ids_pipe(cart_hit: Optional[Dict[str, Any]]) -> str:
    if not cart_hit:
        return ""
    return "|".join(cart_hit.get("cartIds") or [])


def _multi_cart_line_fail(cart_hit: Optional[Dict[str, Any]]) -> bool:
    """Same (offerId, skuId) with multiple cart lines → whole key fails for later mutate."""
    if not cart_hit:
        return False
    return len(cart_hit.get("cartIds") or []) > 1


def expected_key_set(rows: List[Dict[str, Any]]) -> set:
    """Keys with both offer_id and sku_id (uncertain/skip protection boundaries)."""
    out = set()
    for row in rows:
        oid = str(row.get("offer_id") or "").strip()
        sid = str(row.get("sku_id") or "").strip()
        if oid and sid:
            out.add((oid, sid))
    return out


def _expected_as_restock_item(row: Dict[str, Any]) -> Dict[str, Any]:
    """Map reverse_audit expected fields onto restocker cart_line_matches_item keys."""
    return {
        # Name/spec resolve must not short-circuit on empty golden skuId vs cart skuId.
        "alibabaSkuId": "",
        "alibabaSkuName": str(row.get("sku_name") or "").strip(),
        "alibabaSkuSecondName": str(row.get("sku_second_name") or "").strip(),
        "alibabaUrl": str(row.get("alibaba_url") or "").strip(),
        "modelName": str(row.get("model_name") or "").strip(),
    }


def _cart_rec_as_match_line(
    offer_id: str,
    sku_id: str,
    cart_rec: Dict[str, Any],
) -> Dict[str, Any]:
    """Build a restocker-style cart line from an indexed cart aggregate."""
    specs = [str(s) for s in (cart_rec.get("specTexts") or []) if str(s).strip()]
    blob = " | ".join(specs)
    return {
        "offerId": str(offer_id or "").strip(),
        "skuId": str(sku_id or "").strip(),
        "skuName": blob,
        "skuSecondName": "",
        "specText": blob,
    }


def _name_spec_identity(row: Dict[str, Any]) -> Tuple[str, str, str]:
    return (
        str(row.get("offer_id") or "").strip(),
        str(row.get("sku_name") or "").strip(),
        str(row.get("sku_second_name") or "").strip(),
    )


def _cart_keys_matching_expected(
    row: Dict[str, Any],
    cart_by_key: Dict[Tuple[str, str], Dict[str, Any]],
) -> List[Tuple[str, str]]:
    """Cart keys that uniquely-or-not match an expected row via restocker name/spec."""
    from alibaba_restocker import cart_line_matches_item

    item = _expected_as_restock_item(row)
    row_offer = str(row.get("offer_id") or "").strip()
    matches: List[Tuple[str, str]] = []
    for (oid, sid), cart_rec in cart_by_key.items():
        if row_offer and oid != row_offer:
            continue
        line = _cart_rec_as_match_line(oid, sid, cart_rec)
        if cart_line_matches_item(line, item):
            matches.append((oid, sid))
    return matches


def resolve_certain_name_spec_sku_ids(
    certain_rows: List[Dict[str, Any]],
    cart_by_key: Dict[Tuple[str, str], Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], set]:
    """Fill empty certain skuId from unique cart name/spec match.

    Unique match → copy row with cart skuId (diff key only; never writes golden).
    Ambiguous (expected→many cart, or cart→many expected identities) → fail closed:
    omit from qty mutate set; return ambiguous records + cart keys.
    No cart match → keep empty skuId (still certain for missing listing).
    """
    with_sku: List[Dict[str, Any]] = []
    name_only: List[Dict[str, Any]] = []
    for row in certain_rows:
        if str(row.get("sku_id") or "").strip():
            with_sku.append(row)
        else:
            name_only.append(row)

    row_to_keys: List[List[Tuple[str, str]]] = []
    key_to_indices: Dict[Tuple[str, str], List[int]] = {}
    for i, row in enumerate(name_only):
        keys = _cart_keys_matching_expected(row, cart_by_key)
        row_to_keys.append(keys)
        for key in keys:
            key_to_indices.setdefault(key, []).append(i)

    resolved: List[Dict[str, Any]] = list(with_sku)
    ambiguous: List[Dict[str, Any]] = []
    ambiguous_cart_keys: set = set()

    for i, row in enumerate(name_only):
        keys = row_to_keys[i]
        if not keys:
            resolved.append(row)
            continue
        if len(keys) != 1:
            ambiguous_cart_keys.update(keys)
            ambiguous.append(
                {
                    "kind": "ambiguous_name_spec",
                    "offer_id": row.get("offer_id"),
                    "sku_id": "",
                    "sku_name": row.get("sku_name") or "",
                    "sku_second_name": row.get("sku_second_name") or "",
                    "cart_keys": "|".join(f"{o}/{s}" for o, s in keys),
                    "note": (
                        "name/spec certain 對上多筆車內 sku → fail closed"
                        "（不進 qty mutate／delete）"
                    ),
                }
            )
            continue
        key = keys[0]
        peer_ids = {
            _name_spec_identity(name_only[j]) for j in key_to_indices.get(key, [])
        }
        if len(peer_ids) > 1:
            ambiguous_cart_keys.add(key)
            ambiguous.append(
                {
                    "kind": "ambiguous_name_spec",
                    "offer_id": key[0],
                    "sku_id": key[1],
                    "sku_name": row.get("sku_name") or "",
                    "sku_second_name": row.get("sku_second_name") or "",
                    "specText": "|".join(
                        (cart_by_key.get(key) or {}).get("specTexts") or []
                    ),
                    "note": (
                        "一車列對上多筆 distinct name/spec certain → fail closed"
                        "（不進 qty mutate／delete）"
                    ),
                }
            )
            continue
        filled = dict(row)
        filled["sku_id"] = key[1]
        filled["sku_id_resolved_from_cart"] = True
        filled["note"] = (
            (str(row.get("note") or "").rstrip("；") + "；" if row.get("note") else "")
            + "skuId 由車內唯一 name/spec 對上（僅 diff，不回寫 golden）"
        )
        resolved.append(filled)

    return resolved, ambiguous, ambiguous_cart_keys


def diff_expected(
    agg_certain: List[Dict[str, Any]],
    cart_by_key: Dict[Tuple[str, str], Dict[str, Any]],
    order_by_key: Dict[Tuple[str, str], Dict[str, Any]],
) -> Tuple[List[Dict], List[Dict], List[Dict], List[Dict], bool]:
    """Diff certain aggregates vs cart/orders.

    Returns covered, missing, shortfall, qty_excess, paused.
    Excess is listed only (does NOT set paused). Order-covered keys never
    enter qty_excess even if cart_qty > expected.
    """
    covered: List[Dict] = []
    missing: List[Dict] = []
    shortfall: List[Dict] = []
    excess: List[Dict] = []
    paused = False

    for a in agg_certain:
        key = (str(a["offer_id"]), str(a["sku_id"]))
        expected = as_int(a["suggested_qty"])
        order_hit = order_by_key.get(key)
        cart_hit = cart_by_key.get(key)
        cart_qty = as_int(cart_hit["qty"]) if cart_hit else 0
        cart_ids = _cart_ids_pipe(cart_hit)
        multi_fail = _multi_cart_line_fail(cart_hit)

        base = {
            "offer_id": a["offer_id"],
            "sku_id": a["sku_id"],
            "sku_name": a.get("sku_name") or "",
            "sku_second_name": a.get("sku_second_name") or "",
            "alibaba_url": a.get("alibaba_url") or "",
            "expected_qty": expected,
            "is_phone_case": a.get("is_phone_case"),
            "product_ids": "|".join(a.get("product_ids") or []),
            "product_names": "|".join(a.get("product_names") or []),
            "model_names": "|".join(a.get("model_names") or []),
            "spec_ids": "|".join(a.get("spec_ids") or []),
            "source_count": len(a.get("sources") or []),
            "watchlist_order": a.get("watchlist_order"),
        }

        if order_hit:
            row = dict(base)
            row["coverage"] = "order"
            row["order_pools"] = "|".join(order_hit.get("pools") or [])
            row["order_ids"] = "|".join(order_hit.get("orderIds") or [])
            row["order_qty_sum"] = order_hit.get("qty_sum")
            row["cart_qty"] = cart_qty
            row["cart_ids"] = cart_ids
            row["note"] = "出現在訂單池即視為覆蓋（不問在途量）"
            if cart_qty > 0:
                row["note"] += "；車內仍有貨 → 另見 unexpected 可選清車"
            covered.append(row)
            continue

        if cart_hit and cart_qty > 0:
            if cart_qty >= expected:
                row = dict(base)
                row["coverage"] = "cart"
                row["cart_qty"] = cart_qty
                row["cart_ids"] = cart_ids
                row["delta"] = cart_qty - expected
                row["note"] = "車內數量≥應補"
                covered.append(row)
                if cart_qty > expected:
                    ex = dict(base)
                    ex["coverage"] = "cart_excess"
                    ex["cart_qty"] = cart_qty
                    ex["excess"] = cart_qty - expected
                    ex["target_qty"] = expected
                    ex["cart_ids"] = cart_ids
                    ex["specTexts"] = "|".join(cart_hit.get("specTexts") or [])
                    ex["multi_cart_line_fail"] = multi_fail
                    if multi_fail:
                        ex["note"] = (
                            "車內超量；同 key 多 cart 列 → 整 key fail（不自動改量）"
                        )
                    else:
                        ex["note"] = "車內超量；預設不自動改量（不 PAUSE）"
                    excess.append(ex)
            else:
                paused = True
                row = dict(base)
                row["coverage"] = "cart_shortfall"
                row["cart_qty"] = cart_qty
                row["shortfall"] = expected - cart_qty
                row["cart_ids"] = cart_ids
                row["multi_cart_line_fail"] = multi_fail
                row["note"] = "車內 0<qty<應補 → PAUSED（不改量，僅記錄）"
                if multi_fail:
                    row["note"] += "；同 key 多 cart 列 → 整 key fail"
                shortfall.append(row)
            continue

        row = dict(base)
        row["coverage"] = "missing"
        row["cart_qty"] = cart_qty
        row["note"] = "四處皆無 → dry-run 建議加車（尚未 mutate）"
        missing.append(row)

    return covered, missing, shortfall, excess, paused


def find_unexpected_in_cart(
    cart_by_key: Dict[Tuple[str, str], Dict[str, Any]],
    certain_keys: set,
    uncertain_keys: set,
    skip_keys: set,
    order_by_key: Dict[Tuple[str, str], Dict[str, Any]],
    ambiguous_name_keys: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Reverse-scan cart for keys not handled as certain expected diffs.

    certain boundary: keys in certain_keys are skipped unless order-covered
    and still in cart (optional clear candidates). uncertain/skip/order-pool
    hits are listed with removable=false. Ambiguous name/spec cart keys are
    fail-closed (removable=false). Unique name/spec matches are resolved into
    certain_keys before this scan — not listed as unexpected.
    Multi cart lines → fail whole key.
    """
    rows: List[Dict[str, Any]] = []
    ambiguous_name_keys = ambiguous_name_keys or set()

    for key, cart_rec in sorted(cart_by_key.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        oid, sid = key
        cart_qty = as_int(cart_rec.get("qty"))
        cart_ids = "|".join(cart_rec.get("cartIds") or [])
        spec_texts = "|".join(cart_rec.get("specTexts") or [])
        order_hit = order_by_key.get(key)
        order_pools = "|".join(order_hit.get("pools") or []) if order_hit else ""
        multi_fail = _multi_cart_line_fail(cart_rec)

        base = {
            "offer_id": oid,
            "sku_id": sid,
            "cart_qty": cart_qty,
            "cart_ids": cart_ids,
            "specTexts": spec_texts,
            "in_order_pools": order_pools,
            "in_uncertain_expected": key in uncertain_keys,
            "in_skip_expected": key in skip_keys,
            "multi_cart_line_fail": multi_fail,
        }

        if key in certain_keys:
            # Order-covered certain key still occupying cart → optional clear only.
            if order_hit and cart_qty > 0:
                row = dict(base)
                row["removable"] = False
                row["reason"] = "order_covered_still_in_cart"
                row["note"] = "訂單已覆蓋但仍佔車位；可選清車（禁止當誤加刪除）"
                if multi_fail:
                    row["note"] += "；同 key 多 cart 列 → 整 key fail"
                rows.append(row)
            continue

        row = dict(base)
        if key in uncertain_keys:
            row["removable"] = False
            row["reason"] = "uncertain_protected"
            row["note"] = "對上 uncertain expected → 永不 mutate"
        elif key in skip_keys:
            row["removable"] = False
            row["reason"] = "skip_protected"
            row["note"] = "對上 skip／已知售完等；列出但不刪"
        elif order_hit:
            row["removable"] = False
            row["reason"] = "order_pool_protected"
            row["note"] = "三池任一有同 key → 禁止當誤加刪除（知情列出）"
        elif key in ambiguous_name_keys:
            row["removable"] = False
            row["reason"] = "ambiguous_name_spec"
            row["note"] = (
                "name/spec 對上多筆 certain 或一列對多車 → fail closed（不可刪）"
            )
        else:
            row["removable"] = True
            row["reason"] = "not_in_certain_expected"
            row["note"] = "車內有、不在 certain 應補集合、且無保護"

        if multi_fail:
            row["removable"] = False
            if row["reason"] == "not_in_certain_expected":
                row["reason"] = "ambiguous_multi_cart_lines"
            row["note"] = (row.get("note") or "") + "；同 key 多 cart 列 → 整 key fail"
        rows.append(row)

    return rows


def collect_multi_cart_ambiguous(
    cart_by_key: Dict[Tuple[str, str], Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Mark every same-key multi cart-line group as ambiguous/fail."""
    out: List[Dict[str, Any]] = []
    for key, cart_rec in sorted(cart_by_key.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if not _multi_cart_line_fail(cart_rec):
            continue
        out.append(
            {
                "kind": "multi_cart_lines",
                "offer_id": key[0],
                "sku_id": key[1],
                "qty": as_int(cart_rec.get("qty")),
                "cartId": "|".join(cart_rec.get("cartIds") or []),
                "specText": "|".join(cart_rec.get("specTexts") or []),
                "note": "same (offerId,skuId) has multiple cart lines — whole key fail",
            }
        )
    return out


def _alibaba_url_for_row(row: Dict[str, Any]) -> str:
    url = str(row.get("alibaba_url") or "").strip()
    if url.startswith("http"):
        return url
    offer = str(row.get("offer_id") or "").strip()
    if offer:
        return f"https://detail.1688.com/offer/{offer}.html"
    return ""


def _unexpected_note(row: Dict[str, Any]) -> str:
    note = str(row.get("note") or "").strip()
    reason = str(row.get("reason") or "").strip()
    if note and reason and reason not in note:
        return f"{note}；{reason}"
    return note or reason


def build_consolidated_zh_rows(
    missing: List[Dict[str, Any]],
    shortfall: List[Dict[str, Any]],
    excess: List[Dict[str, Any]],
    unexpected: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Human-facing Traditional Chinese table (action items only; no covered)."""
    rows: List[Dict[str, Any]] = []

    for r in sorted(missing, key=lambda x: -as_int(x.get("expected_qty"))):
        eq = as_int(r.get("expected_qty"))
        rows.append(
            {
                "類型": "車裡缺少（建議加）",
                "蝦皮商品id": r.get("product_ids") or "",
                "蝦皮規格id": r.get("spec_ids") or "",
                "蝦皮商品名稱": r.get("product_names") or "",
                "型號": r.get("model_names") or "",
                "應補數量": eq,
                "車內數量": as_int(r.get("cart_qty")),
                "差額說明": f"建議加 {eq}",
                "1688網址": _alibaba_url_for_row(r),
                "1688_offer": r.get("offer_id") or "",
                "1688_sku": r.get("sku_id") or "",
                "備註": r.get("note") or "",
            }
        )

    for r in shortfall:
        sf = as_int(r.get("shortfall"))
        rows.append(
            {
                "類型": "車裡數量不足",
                "蝦皮商品id": r.get("product_ids") or "",
                "蝦皮規格id": r.get("spec_ids") or "",
                "蝦皮商品名稱": r.get("product_names") or "",
                "型號": r.get("model_names") or "",
                "應補數量": as_int(r.get("expected_qty")),
                "車內數量": as_int(r.get("cart_qty")),
                "差額說明": f"少 {sf}",
                "1688網址": _alibaba_url_for_row(r),
                "1688_offer": r.get("offer_id") or "",
                "1688_sku": r.get("sku_id") or "",
                "備註": r.get("note") or "",
            }
        )

    for r in excess:
        ex = as_int(r.get("excess"))
        rows.append(
            {
                "類型": "車裡數量過多",
                "蝦皮商品id": r.get("product_ids") or "",
                "蝦皮規格id": r.get("spec_ids") or "",
                "蝦皮商品名稱": r.get("product_names") or "",
                "型號": r.get("model_names") or "",
                "應補數量": as_int(r.get("expected_qty")),
                "車內數量": as_int(r.get("cart_qty")),
                "差額說明": f"多 {ex}",
                "1688網址": _alibaba_url_for_row(r),
                "1688_offer": r.get("offer_id") or "",
                "1688_sku": r.get("sku_id") or "",
                "備註": r.get("note") or "",
            }
        )

    removable = [r for r in unexpected if r.get("removable") is True]
    protected = [r for r in unexpected if r.get("removable") is not True]
    for group, type_label in (
        (removable, "車裡多出來（可能可刪）"),
        (protected, "車裡多出來（先不要刪）"),
    ):
        for r in group:
            cart_qty = as_int(r.get("cart_qty"))
            rows.append(
                {
                    "類型": type_label,
                    "蝦皮商品id": "",
                    "蝦皮規格id": "",
                    "蝦皮商品名稱": "",
                    "型號": r.get("specTexts") or "",
                    "應補數量": "",
                    "車內數量": cart_qty,
                    "差額說明": f"車內 {cart_qty}，不在應補清單",
                    "1688網址": _alibaba_url_for_row(r),
                    "1688_offer": r.get("offer_id") or "",
                    "1688_sku": r.get("sku_id") or "",
                    "備註": _unexpected_note(r),
                }
            )

    return rows


def run_dry_run(
    out_dir: Path,
    *,
    root: Optional[Path] = None,
    refreeze_sources: bool = True,
) -> Dict[str, Any]:
    """Run offline dry-run against frozen live_* JSONs in out_dir."""
    root = root or repo_root()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cart = load_json(out_dir / "live_cart.json")
    pay = load_json(out_dir / "live_orders_pending_pay.json")
    ship = load_json(out_dir / "live_orders_pending_ship.json")
    recv = load_json(out_dir / "live_orders_pending_receive.json")
    meta_path = out_dir / "snapshot_meta.json"
    meta = load_json(meta_path) if meta_path.exists() else {}

    for label, blob in [
        ("live_cart", cart),
        ("pending_pay", pay),
        ("pending_ship", ship),
        ("pending_receive", recv),
    ]:
        if not blob.get("complete"):
            raise SystemExit(f"refusing dry-run: {label} complete!=true")

    cart_n = len(cart.get("items") or [])
    sources_dir = out_dir / "sources"
    source_files_ready = all((sources_dir / name).exists() for name in SRC_NAMES)
    if refreeze_sources or not source_files_ready:
        manifest = freeze_sources(out_dir, root=root)
    elif (sources_dir / "manifest.json").exists():
        manifest = load_json(sources_dir / "manifest.json")
    else:
        manifest = {
            "frozenAt": None,
            "timezone": "Asia/Taipei",
            "files": {},
            "note": "reused existing sources/ without manifest",
        }

    products = load_json(sources_dir / "shopee_products.json")
    golden = load_json(sources_dir / "golden_table.json")
    watch_raw = load_json(sources_dir / "personal_watchlist.json")
    watchlist = parse_watchlist_payload(watch_raw)
    exclusion_ids = merged_watchlist_exclusion_ids(
        load_watchlist_exclusion_ids(sources_dir / "personal_watchlist_exclusions.json"),
        products,
    )
    filtered = without_watchlist_exclusions(watchlist["productIds"], exclusion_ids)
    watch_ids = filtered["productIds"]

    products = {str(k): v for k, v in products.items() if isinstance(v, dict)}
    golden = {str(k): v for k, v in golden.items() if isinstance(v, dict)}

    certain_rows, uncertain_rows, skip_rows, build_stats = build_expected(
        products, golden, watch_ids
    )
    cart_by_key, amb_cart = index_cart(cart)
    order_by_key, amb_orders, pool_validation = index_orders(
        {
            "pending_pay": pay,
            "pending_ship": ship,
            "pending_receive": recv,
        }
    )
    resolved_certain, name_ambiguous, ambiguous_name_keys = (
        resolve_certain_name_spec_sku_ids(certain_rows, cart_by_key)
    )
    agg_certain = aggregate_certain(resolved_certain)
    covered, missing, shortfall, excess, paused = diff_expected(
        agg_certain, cart_by_key, order_by_key
    )
    certain_keys = {
        (str(a["offer_id"]), str(a["sku_id"]))
        for a in agg_certain
        if str(a.get("offer_id") or "").strip() and str(a.get("sku_id") or "").strip()
    }
    uncertain_keys = expected_key_set(uncertain_rows)
    skip_keys = expected_key_set(skip_rows)
    unexpected = find_unexpected_in_cart(
        cart_by_key,
        certain_keys,
        uncertain_keys,
        skip_keys,
        order_by_key,
        ambiguous_name_keys=ambiguous_name_keys,
    )
    ambiguous = (
        amb_cart + amb_orders + collect_multi_cart_ambiguous(cart_by_key) + name_ambiguous
    )
    for a in agg_certain:
        if not a.get("offer_id") or not a.get("sku_id"):
            ambiguous.append(
                {
                    "kind": (
                        "name_spec_unresolved_sku"
                        if str(a.get("sku_name") or "").strip()
                        else "certain_missing_key"
                    ),
                    "offer_id": a.get("offer_id"),
                    "sku_id": a.get("sku_id"),
                    "sku_name": a.get("sku_name") or "",
                    "sku_second_name": a.get("sku_second_name") or "",
                    "note": (
                        "name/spec certain 車內無唯一對上 → 不發明 skuId"
                        if str(a.get("sku_name") or "").strip()
                        else "aggregated certain missing offer/sku"
                    ),
                }
            )

    certain_fields = [
        "product_id", "product_name", "spec_id", "model_name", "is_phone_case",
        "target_months", "current_stock", "monthly_sales", "effective_monthly_sales",
        "raw_shortage", "suggested_qty", "alibaba_url", "offer_id", "sku_id",
        "sku_name", "sku_second_name", "mapping_status", "watchlist_order", "bucket",
        "certain_via", "note",
    ]
    uncertain_fields = certain_fields + ["uncertain_reason"]
    skip_fields = certain_fields + ["skip_reason"]
    write_csv(out_dir / "expected_certain.csv", certain_rows, certain_fields)
    write_csv(out_dir / "expected_uncertain.csv", uncertain_rows, uncertain_fields)
    write_csv(out_dir / "expected_skip.csv", skip_rows, skip_fields)

    covered_fields = [
        "offer_id", "sku_id", "sku_name", "sku_second_name", "alibaba_url",
        "expected_qty", "coverage", "cart_qty", "order_pools", "order_ids",
        "order_qty_sum", "delta", "cart_ids", "is_phone_case",
        "product_ids", "product_names", "model_names", "spec_ids", "source_count",
        "watchlist_order", "note",
    ]
    missing_fields = [
        "offer_id", "sku_id", "sku_name", "sku_second_name", "alibaba_url",
        "expected_qty", "coverage", "cart_qty", "is_phone_case",
        "product_ids", "product_names", "model_names", "spec_ids", "source_count",
        "watchlist_order", "note",
    ]
    shortfall_fields = [
        "offer_id", "sku_id", "sku_name", "sku_second_name", "alibaba_url",
        "expected_qty", "cart_qty", "shortfall", "coverage", "cart_ids",
        "multi_cart_line_fail", "is_phone_case", "product_ids", "product_names",
        "model_names", "spec_ids", "source_count", "watchlist_order", "note",
    ]
    excess_fields = [
        "offer_id", "sku_id", "sku_name", "sku_second_name", "alibaba_url",
        "expected_qty", "cart_qty", "excess", "target_qty", "cart_ids",
        "specTexts", "multi_cart_line_fail", "is_phone_case",
        "product_ids", "product_names", "model_names", "spec_ids", "source_count",
        "watchlist_order", "coverage", "note",
    ]
    unexpected_fields = [
        "offer_id", "sku_id", "cart_qty", "cart_ids", "specTexts",
        "in_order_pools", "in_uncertain_expected", "in_skip_expected",
        "removable", "reason", "multi_cart_line_fail", "note",
    ]
    write_csv(out_dir / "covered.csv", covered, covered_fields)
    write_csv(out_dir / "missing_to_add.csv", missing, missing_fields)
    write_csv(out_dir / "qty_shortfall.csv", shortfall, shortfall_fields)
    write_csv(out_dir / "qty_excess.csv", excess, excess_fields)
    write_csv(out_dir / "unexpected_in_cart.csv", unexpected, unexpected_fields)
    write_csv(out_dir / "ambiguous.csv", ambiguous)

    consolidated = build_consolidated_zh_rows(missing, shortfall, excess, unexpected)
    consolidated_path = out_dir / CONSOLIDATED_CSV_NAME
    write_csv_utf8_sig(consolidated_path, consolidated, CONSOLIDATED_FIELDS)

    unexpected_removable = sum(1 for r in unexpected if r.get("removable") is True)
    unexpected_protected = len(unexpected) - unexpected_removable
    status = "PAUSED" if paused else "READY_FOR_APPROVAL"
    summary = {
        "generatedAt": now_iso(),
        "timezone": "Asia/Taipei",
        "mode": "offline_dry_run",
        "status": status,
        "paused": paused,
        "pauseReason": (
            "cart has 0<qty<expected for one or more certain SKUs; "
            "task paused — no auto qty fix"
            if paused
            else None
        ),
        "snapshot": {
            "capturedAt": meta.get("capturedAt"),
            "accountHint": meta.get("accountHint"),
            "cartHeaderSkuCount": (meta.get("cart") or {}).get("cartHeaderSkuCount"),
            "cartLineCount": cart_n,
            "orders": {
                "pending_pay": {
                    "nOrders": pay.get("nOrders"),
                    "nLines": pay.get("nLines"),
                    "complete": pay.get("complete"),
                },
                "pending_ship": {
                    "nOrders": ship.get("nOrders"),
                    "nLines": ship.get("nLines"),
                    "complete": ship.get("complete"),
                },
                "pending_receive": {
                    "nOrders": recv.get("nOrders"),
                    "nLines": recv.get("nLines"),
                    "complete": recv.get("complete"),
                },
            },
            "completeness": meta.get("completeness"),
        },
        "sources": manifest,
        "build": build_stats,
        "exclusions": {
            "count": len(exclusion_ids),
            "removedFromWatchlist": filtered.get("excluded"),
        },
        "expected": {
            "certain_model_rows": len(certain_rows),
            "certain_aggregated_offer_sku": len(agg_certain),
            "uncertain_model_rows": len(uncertain_rows),
            "skip_model_rows": len(skip_rows),
            "certain_qty_sum": sum(as_int(r.get("suggested_qty")) for r in agg_certain),
        },
        "diff": {
            "covered": len(covered),
            "covered_by_order": sum(1 for r in covered if r.get("coverage") == "order"),
            "covered_by_cart": sum(1 for r in covered if r.get("coverage") == "cart"),
            "missing_to_add": len(missing),
            "missing_qty_sum": sum(as_int(r.get("expected_qty")) for r in missing),
            "qty_shortfall": len(shortfall),
            "shortfall_qty_sum": sum(as_int(r.get("shortfall")) for r in shortfall),
            "qty_excess": len(excess),
            "excess_qty_sum": sum(as_int(r.get("excess")) for r in excess),
            "unexpected_in_cart": len(unexpected),
            "unexpected_removable": unexpected_removable,
            "unexpected_protected": unexpected_protected,
            "ambiguous": len(ambiguous),
        },
        "orderPoolValidation": pool_validation,
        "rules": {
            "scope": "watchlist ∩ products ∩ golden; exclusions via home_bootstrap; no schoolbag cutoff",
            "qty": "calculated_restock_details + target_months_for_product (case=3 / else=4); sum by (offerId,skuId)",
            "certain": (
                "approved + URL + (skuId OR usable name/spec)；"
                "缺 skuId 時以車內唯一 name/spec 解析 live skuId 進 qty diff"
            ),
            "uncertain": "缺欄（無 approved／URL，或既無 skuId 也無 usable name/spec）— never guess URL/skuId",
            "skip": "discontinued / 售完等 (+ known 粉色愛心兔)",
            "orderCoverage": "any pool same offer+sku = covered",
            "cartShortfall": "0<qty<expected → PAUSED; full list still produced; no qty mutate",
            "cartExcess": "cart > expected (non order-covered) → qty_excess.csv; list only, no PAUSE, no qty mutate",
            "unexpectedCart": (
                "reverse-scan cart; certain boundary（含唯一 name/spec 解析後的 key）；"
                "order/uncertain/skip protected (removable=false)；"
                "ambiguous name/spec → fail closed（ambiguous_name_spec，不可刪）；"
                "order-covered still in cart = optional clear; "
                "multi cart lines → whole key fail/ambiguous"
            ),
            "mutateFlags": (
                "--i-approve-mutate=add only; --i-approve-set-qty=shortfall UP+excess DOWN; "
                "--i-approve-remove=removable only; flags never imply each other"
            ),
        },
        "outputs": {
            "dir": str(out_dir),
            "primary_human_csv": str(consolidated_path),
            CONSOLIDATED_CSV_NAME: str(consolidated_path),
            "machine_csvs_note": (
                "English CSVs (missing_to_add / qty_* / unexpected_* / expected_* / "
                "covered / ambiguous) are machine/internal; mutate reads missing_to_add.csv"
            ),
            "missing_to_add.csv": str(out_dir / "missing_to_add.csv"),
            "qty_excess.csv": str(out_dir / "qty_excess.csv"),
            "unexpected_in_cart.csv": str(out_dir / "unexpected_in_cart.csv"),
            "dry_run_report.md": str(out_dir / "dry_run_report.md"),
            "dry_run_summary.json": str(out_dir / "dry_run_summary.json"),
        },
        "noCartMutate": True,
    }

    (out_dir / "dry_run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    def _preview(rows: List[Dict[str, Any]], n: int = 8) -> List[str]:
        out_lines: List[str] = []
        for r in rows[:n]:
            out_lines.append(
                f"  - `{r.get('offer_id')}` / `{r.get('sku_id')}` "
                f"qty={r.get('cart_qty')} reason={r.get('reason') or r.get('coverage')} "
                f"removable={r.get('removable', '')}"
            )
        if len(rows) > n:
            out_lines.append(f"  - …另有 {len(rows) - n} 筆")
        return out_lines

    lines: List[str] = []
    lines.append("# 反向補貨離線 dry-run 報告")
    lines.append("")
    lines.append(f"- 產生時間：{summary['generatedAt']}（Asia/Taipei）")
    lines.append(
        f"- 狀態：**{status}**"
        + (" — 車內不足，已暫停、不改量" if paused else " — 可待核准後 mutate")
    )
    lines.append(f"- 模式：offline dry-run（不加車／不改量）")
    lines.append(f"- 輸出目錄：`{out_dir}`")
    lines.append("")
    lines.append("## 人工交付（請先看這份）")
    lines.append(
        f"- **`{CONSOLIDATED_CSV_NAME}`**（UTF-8-SIG，Excel 可直接開）—"
        f" 共 {len(consolidated)} 筆待處理／需注意項目"
    )
    lines.append(
        "  （含：車裡缺少、數量不足、數量過多、車裡多出來；**不含**已覆蓋 covered）"
    )
    lines.append("")
    lines.append("## Diff 摘要（certain 聚合）")
    lines.append(
        f"- covered={len(covered)}；missing_to_add={len(missing)}；"
        f"qty_shortfall={len(shortfall)}；qty_excess={len(excess)}；"
        f"unexpected_in_cart={len(unexpected)} "
        f"(removable={unexpected_removable}/protected={unexpected_protected})；"
        f"ambiguous={len(ambiguous)}"
    )
    lines.append("")
    lines.append("## 車內超量／非預期（預覽）")
    lines.append(
        f"- qty_excess={len(excess)}（excess_qty_sum="
        f"{sum(as_int(r.get('excess')) for r in excess)}；不 PAUSE）"
    )
    lines.extend(_preview(excess))
    lines.append(
        f"- unexpected_in_cart={len(unexpected)} "
        f"（removable={unexpected_removable}；protected={unexpected_protected}）"
    )
    lines.extend(_preview(unexpected))
    lines.append("")
    lines.append("## 規則（定案）")
    for k, v in summary["rules"].items():
        lines.append(f"- **{k}**：{v}")
    lines.append("")
    lines.append("## 機器用／內部 CSV（一般人不用開）")
    lines.append(
        "- `missing_to_add.csv`、`qty_shortfall.csv`、`qty_excess.csv`、"
        "`unexpected_in_cart.csv`、`covered.csv`、`expected_*.csv`、`ambiguous.csv`"
    )
    lines.append(
        "- mutate：`--i-approve-mutate` 加車；`--i-approve-set-qty` 改量；"
        "`--i-approve-remove` 刪除（預設不刪，須庭安明確說刪）"
    )
    lines.append("")
    lines.append("---")
    lines.append(
        "下一步：`python -m reverse_audit mutate --dir <此目錄> --i-approve-mutate`；"
        "若 status=PAUSED 須先處理車內不足再繼續。`--i-approve-mutate` 僅加車；"
        "超量／非預期 Phase 1 只列出，不改量不刪除。"
    )
    lines.append("")
    (out_dir / "dry_run_report.md").write_text("\n".join(lines), encoding="utf-8")
    return summary
