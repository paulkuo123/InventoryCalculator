"""Offline, cart-first reconciliation and an evidence-backed operation journal.

This module NEVER connects to a browser or calls the additive restock API.
Snapshots must come from permitted observation of the in-app browser. A prepared
operation is an instruction, not evidence that a cart write has happened.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from restock_batch import (
    STATUS_COMPLETED, STATUS_COMPLETED_GAPS, STATUS_NEEDS_RECONCILE,
    STATUS_REVIEW, STATUS_RUNNING, load_state, new_run_id, save_state,
)
from restock_rules import calculated_restock_details, target_months_for_product
from shopee_products_import import atomic_write_json


MODE = "cart_final_quantity"
CONFIRMED = {"matched", "corrected"}
SCHOOLBAG_RE = re.compile(r"(?:書包|书包)(?!\s*(?:掛件|挂件|吊飾|吊饰|掛飾|挂饰|鑰匙|钥匙))")


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode()).hexdigest()


def timestamp(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("capturedAt 必須包含時區")
    return result


def integer(value, field):
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        raise ValueError(f"{field} 必須是非負整數，不能把未知值當成零")
    return int(value)


def identity(raw):
    url = str(raw.get("url") or "").strip()
    parts = urlsplit(url)
    match = re.fullmatch(r"/offer/(\d+)\.html", parts.path)
    if parts.scheme not in {"http", "https"} or parts.hostname != "detail.1688.com" or not match or parts.username or parts.port:
        raise ValueError("URL 必須是確定的 detail.1688.com/offer/<ID>.html")
    offer = match[1]
    if raw.get("offerId") and str(raw["offerId"]) != offer:
        raise ValueError("offerId 與 URL 不符")
    specs = raw.get("specs") or []
    if not isinstance(specs, list) or any(not isinstance(x, str) or not x.strip() for x in specs):
        raise ValueError("specs 必須是完整規格的非空字串陣列")
    sku = str(raw.get("skuId") or "").strip()
    if not sku and not specs:
        raise ValueError("缺少 SKU ID 與完整規格")
    return {"offerId": offer, "skuId": sku, "specs": [x.strip() for x in specs],
            "url": f"https://detail.1688.com/offer/{offer}.html"}


def same_sku(left, right):
    if left["offerId"] != right["offerId"]:
        return False
    if left["skuId"] and right["skuId"]:
        return left["skuId"] == right["skuId"]
    # Exact complete dimensions only. No fuzzy name or partial phone-model match.
    return bool(left["specs"]) and left["specs"] == right["specs"]


def snapshot(raw):
    """Reject partial reads; collapse repeated observations of the SAME cart line."""
    if raw.get("complete") is not True or not raw.get("snapshotId") or not raw.get("evidence"):
        raise ValueError("購物車快照需要 complete=true、snapshotId 與讀取證據")
    timestamp(raw.get("capturedAt"))
    if not isinstance(raw.get("rows"), list):
        raise ValueError("購物車快照缺少 rows")
    rows, seen = [], {}
    for raw_row in raw["rows"]:
        row = {**identity(raw_row), "lineId": str(raw_row.get("lineId") or ""),
               "quantity": integer(raw_row.get("quantity"), "quantity"),
               "productName": str(raw_row.get("productName") or "")}
        if not row["lineId"] or row["quantity"] == 0:
            raise ValueError("購物車列需要穩定 lineId 與正數數量；不存在的型號應省略")
        previous = seen.get(row["lineId"])
        if previous and previous != row:
            raise ValueError("同一購物車列在讀取期間改變，請重新建立完整基線")
        if not previous:
            rows.append(row)
            seen[row["lineId"]] = row
    return {"snapshotId": str(raw["snapshotId"]), "capturedAt": raw["capturedAt"],
            "evidence": str(raw["evidence"]), "complete": True, "rows": rows}


def quantity_in(observed, binding):
    rows = [r for r in observed["rows"] if same_sku(r, binding)]
    if len(rows) > 1:
        raise ValueError("同一採購 SKU 有多筆購物車列，需人工釐清")
    if rows and rows[0]["specs"] and binding["specs"] and rows[0]["specs"] != binding["specs"]:
        raise ValueError("購物車 SKU ID 相同但完整規格不符，需人工釐清")
    return (rows[0]["quantity"], rows[0]["lineId"]) if rows else (0, None)


def binding_for(model):
    return identity({"url": model.get("阿里巴巴商品URL"), "offerId": model.get("1688_offer_id"),
                     "skuId": model.get("1688_sku_id"),
                     "specs": [model[k] for k in ("1688_sku_name", "1688_sku_second_name") if model.get(k)]})


def source_snapshot(path):
    path = Path(path).resolve()
    with path.open("rb") as handle:
        before = path.stat()
        data = handle.read()
        after = path.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise ValueError(f"來源讀取期間改變：{path}")
    payload = json.loads(data)
    if not isinstance(payload, dict) or not payload:
        raise ValueError(f"來源必須為非空商品字典：{path}")
    return {"path": str(path), "mtime": datetime.fromtimestamp(after.st_mtime, timezone.utc).isoformat(),
            "sha256": hashlib.sha256(data).hexdigest(), "contentSha256": digest(payload),
            "refreshedInThisRun": False, "productCount": len(payload)}, payload


def calculation(product, model):
    # Malformed inventory must never become a zero recommendation/removal.
    for field in ("商品庫存", "月銷量"):
        value = model.get(field)
        if value is None or value == "" or isinstance(value, bool):
            raise ValueError(f"缺少或無效的 {field}")
        number = float(value)
        if not math.isfinite(number) or number < 0 or (field == "商品庫存" and not number.is_integer()):
            raise ValueError(f"無效的 {field}")
    for obj, field in ((model, "已售出數量"), (product, "已售出總數量"), (product, "總月銷量")):
        if obj.get(field) not in (None, ""):
            number = float(obj[field])
            if not math.isfinite(number) or number < 0:
                raise ValueError(f"無效的 {field}")
    return calculated_restock_details(product, model, target_months_for_product(product.get("商品名稱"), model_name=model.get("型號名稱")))


def schoolbag_boundary(products, golden, baseline, index):
    for position, row in enumerate(baseline["rows"]):
        names = [row.get("productName", "")]
        for pid, _, binding in index:
            if same_sku(row, binding):
                names.extend([(products.get(pid) or {}).get("商品名稱", ""),
                              (golden.get(pid) or {}).get("商品名稱", "")])
        matched = next((name for name in names if SCHOOLBAG_RE.search(name)), None)
        if matched:
            return {"rule": "stop_before_first_schoolbag", "found": True,
                    "originalIndex": position, "lineId": row["lineId"],
                    "productName": matched, "url": row["url"],
                    "excludedCartRows": len(baseline["rows"]) - position}
    return {"rule": "stop_before_first_schoolbag", "found": False,
            "originalIndex": len(baseline["rows"]), "excludedCartRows": 0}


def build_manifest(products, golden, baseline, sources=None, frozen_product_ids=None, frozen_stop_boundary=None):
    baseline = snapshot(baseline)
    index = []
    for pid, product in golden.items():
        for model in product.get("型號") or []:
            try:
                index.append((str(pid), model, binding_for(model)))
            except (ValueError, TypeError):
                continue
    boundary = deepcopy(frozen_stop_boundary) if frozen_stop_boundary is not None else schoolbag_boundary(products, golden, baseline, index)
    prefix = baseline["rows"][:boundary["originalIndex"]]
    protected = baseline["rows"][boundary["originalIndex"]:]
    scope, gaps, ambiguous_rows = [], [], []
    for position, row in enumerate(prefix):
        matches = [(pid, model) for pid, model, binding in index if same_sku(row, binding)]
        pids = {pid for pid, _ in matches}
        if len(pids) != 1:
            reason = "購物車無對應" if not pids else "購物車對應多個蝦皮商品"
            gaps.append({"cartOrder": position, **row, "reason": reason,
                         "candidateProductIds": sorted(pids)})
            ambiguous_rows.append(row)
            continue
        pid = next(iter(pids))
        if pid not in products:
            gaps.append({"cartOrder": position, **row, "reason": "蝦皮來源缺少此商品", "productId": pid})
        elif pid not in scope:
            scope.append(pid)
    if frozen_product_ids is not None:
        scope = list(frozen_product_ids)  # A mapping revision cannot expand the original scope.
    items = []
    for pid in scope:
        product = products[pid]
        models = product.get("型號") or []
        for model_index, model in enumerate(models):
            sid = str(model.get("規格ID") or "")
            source = {"productId": pid, "specId": sid, "productName": product.get("商品名稱", ""),
                      "modelName": model.get("型號名稱", "")}
            reasons = []
            if not sid or sum(str(m.get("規格ID") or "") == sid for m in models) != 1:
                reasons.append("蝦皮規格 ID 缺少或重複")
            try:
                source.update(calculation(product, model))
            except (ValueError, TypeError, OverflowError) as exc:
                reasons.append(f"庫存資料無效：{exc}")
            mapped = [m for m in (golden.get(pid) or {}).get("型號", []) if sid and str(m.get("規格ID") or "") == sid]
            binding = None
            if len(mapped) != 1:
                reasons.append("Golden Table 規格 ID 對應缺少或重複")
            else:
                source["mappingBefore"] = {k: v for k, v in mapped[0].items()
                                            if k.startswith("1688_") or k == "阿里巴巴商品URL"}
                try:
                    binding = binding_for(mapped[0])
                except (ValueError, TypeError) as exc:
                    reasons.append(f"mapping／URL 待查：{exc}")
                if mapped[0].get("1688_mapping_status") != "approved":
                    reasons.append("mapping 尚未核准")
                if not mapped[0].get("1688_sku_name"):
                    reasons.append("mapping 缺少規格名稱")
            old_qty, line_id = None, None
            if binding:
                try:
                    old_qty, line_id = quantity_in(baseline, binding)
                except ValueError as exc:
                    reasons.append(str(exc))
                if any(same_sku(binding, row) for row in ambiguous_rows):
                    reasons.append("此 SKU 的原始購物車對應不唯一")
                if any(p not in scope and same_sku(binding, b) for p, _, b in index):
                    reasons.append("此採購 SKU 另有範圍外蝦皮商品共用")
            existing = [i for i in items if binding and i["binding"] and same_sku(binding, i["binding"])]
            if existing:
                entry = existing[0]
                if entry["binding"]["specs"] != binding["specs"]:
                    reasons.append("共用 SKU ID 的完整規格互相衝突")
                entry["sources"].append(source)
                entry["blockers"].extend(reasons)
                entry["sharedSku"] = True
                entry["targetQty"] = (entry["targetQty"] + source["suggestedQty"]
                                      if entry["targetQty"] is not None and "suggestedQty" in source else None)
                continue
            items.append({"itemId": f"item-{len(items) + 1:04d}", "binding": binding,
                          "sources": [source], "blockers": reasons, "sharedSku": False,
                          "originalQty": old_qty, "baselineLineId": line_id,
                          "targetQty": source.get("suggestedQty"),
                          "productOrder": scope.index(pid), "modelOrder": model_index})
    # Even a sibling of a scoped product must not modify a row at/after the stop.
    excluded_items = [i for i in items if i["binding"] and any(same_sku(i["binding"], r) for r in protected)]
    items = [i for i in items if i not in excluded_items]
    # Within each scoped product, check its original cart rows first, then siblings.
    line_order = {row["lineId"]: n for n, row in enumerate(baseline["rows"])}
    items.sort(key=lambda i: (i["productOrder"], line_order.get(i["baselineLineId"], len(line_order)), i["modelOrder"]))
    for item in items:
        item["blockers"] = list(dict.fromkeys(item["blockers"]))
        item["difference"] = (item["targetQty"] - item["originalQty"]
                              if item["targetQty"] is not None and item["originalQty"] is not None else None)
        item["cause"] = {"confirmed": "僅確認數量差異；尚未證實漏加原因", "hypothesis": None}
    return {"schemaVersion": 1, "mode": MODE, "createdAt": now(), "sources": sources or {},
            "baseline": baseline, "productIds": scope, "items": items, "gaps": gaps,
            "stopBoundary": boundary, "excludedItemsAfterStop": excluded_items,
            "scopeRule": "original cart BEFORE first schoolbag -> uniquely matched Shopee products -> sibling models; no recursion; never modify original rows at/after schoolbag"}


def create_run(directory, products_path, golden_path, baseline):
    directory = Path(directory)
    if directory.exists():
        raise ValueError("任務目錄已存在，請續跑或使用新目錄")
    pmeta, products = source_snapshot(products_path)
    gmeta, golden = source_snapshot(golden_path)
    manifest = build_manifest(products, golden, baseline, {"shopee": pmeta, "golden": gmeta})
    directory.mkdir(parents=True)
    atomic_write_json(directory / "shopee-frozen.json", products)
    atomic_write_json(directory / "golden-frozen.json", golden)
    state = {"runId": directory.name, "mode": MODE, "createdAt": now(), "events": [], "revision": 0}
    install_manifest(directory, state, manifest)
    return state


def install_manifest(directory, state, manifest):
    sha = digest(manifest)
    atomic_write_json(Path(directory) / f"manifest-{sha}.json", manifest)
    state.update({"manifestSha256": sha, "status": STATUS_REVIEW, "approval": None,
                  "inflight": None, "lastObservation": state.get("lastObservation", manifest["baseline"]["capturedAt"]),
                  "usedSnapshots": state.get("usedSnapshots", [manifest["baseline"]["snapshotId"]]), "finalAudit": None,
                  "items": {i["itemId"]: {"status": "blocked" if i["blockers"] else "pending",
                            "reason": "; ".join(i["blockers"]), "finalQty": None,
                            "verifiedAt": None} for i in manifest["items"]}})
    persist(directory, state, manifest)


@contextmanager
def locked_run(directory):
    """One local journal writer. Never reuse the additive worker's run directory."""
    import fcntl
    directory = Path(directory)
    with (directory / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("另一個程序正在更新此核對任務") from exc
        state = load_state(directory)
        if state.get("mode") != MODE:
            raise ValueError("不能使用累加式補貨任務作為最終數量核對任務")
        sha = state["manifestSha256"]
        if not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise ValueError("manifest 雜湊無效")
        manifest = json.loads((directory / f"manifest-{sha}.json").read_text())
        if digest(manifest) != sha:
            raise ValueError("manifest 已被修改，拒絕續跑")
        yield state, manifest


def event(state, action, **details):
    state["revision"] += 1
    state["events"].append({"revision": state["revision"], "at": now(), "action": action, **details})


def persist(directory, state, manifest):
    saved = save_state(Path(directory), state)
    state.update(saved)
    report = build_report(state, manifest)
    atomic_write_json(Path(directory) / "report.json", report)
    # Replace the HTML atomically too, so periodic refresh never sees a half page.
    target = Path(directory) / "report.html"
    temporary = target.with_suffix(".html.tmp")
    temporary.write_text(render_report(report), encoding="utf-8")
    temporary.replace(target)


def approve(directory, manifest_sha, *, evidence, allow_removals=False, shared_sku_evidence=None):
    if not str(evidence).strip():
        raise ValueError("需要具體變更清單的使用者核准紀錄")
    with locked_run(directory) as (state, manifest):
        if manifest_sha != state["manifestSha256"] or state["status"] != STATUS_REVIEW:
            raise ValueError("只能核准目前 review 版本的 manifest")
        shared = shared_sku_evidence or {}
        valid_ids = {i["itemId"] for i in manifest["items"] if i["sharedSku"]}
        if set(shared) - valid_ids or any(not isinstance(v, str) or not v.strip() for v in shared.values()):
            raise ValueError("共用 SKU 核准需逐項提供需求非重複的證據")
        state["approval"] = {"manifestSha256": manifest_sha, "evidence": evidence, "at": now(),
                             "allowRemovals": allow_removals, "sharedSkuEvidence": shared}
        state["status"] = STATUS_RUNNING
        event(state, "approved", evidence=evidence)
        persist(directory, state, manifest)


def accept_observation(state, raw):
    observed = snapshot(raw)
    if observed["snapshotId"] in state["usedSnapshots"] or timestamp(observed["capturedAt"]) <= timestamp(state["lastObservation"]):
        raise ValueError("必須重新讀取購物車，不能重用舊快照")
    age = (datetime.now(timezone.utc) - timestamp(observed["capturedAt"])).total_seconds()
    if age < -30 or age > 300:
        raise ValueError("操作用快照需為最近五分鐘的讀取，且不能來自未來")
    state["lastObservation"] = observed["capturedAt"]
    state["usedSnapshots"].append(observed["snapshotId"])
    return observed


def catalog_check(item, raw, observed):
    """A live page must establish exact identity, units and order restrictions."""
    if not raw.get("evidence") or not raw.get("capturedAt"):
        raise ValueError("缺少商品頁完整規格／包裝單位的讀取證據")
    age = (timestamp(observed["capturedAt"]) - timestamp(raw["capturedAt"])).total_seconds()
    if age < 0 or age > 300:
        raise ValueError("商品頁證據需在購物車回讀前五分鐘內重新確認")
    live = identity(raw)
    if not same_sku(item["binding"], live) or live["specs"] != item["binding"]["specs"]:
        raise ValueError("商品頁 SKU 或完整規格與 mapping 不符，需修正 mapping")
    if integer(raw.get("unitsPerCartUnit"), "unitsPerCartUnit") != 1:
        raise ValueError("包裝單位不是確認的一件對一件，需人工換算")
    minimum = integer(raw.get("minQuantity"), "minQuantity")
    step = integer(raw.get("quantityStep"), "quantityStep")
    if step == 0:
        raise ValueError("quantityStep 必須大於零")
    target = item["targetQty"]
    if target > 0 and raw.get("available") is not True:
        raise ValueError("商品頁未確認可供貨")
    if target > 0 and (target < minimum or target % step):
        raise ValueError("目標量不符合起訂量或包裝倍數；不自行提高數量")
    return live


def prepare_next(directory, raw_observation, catalogs):
    """Durably mark ONE instruction in flight, before the browser could act."""
    with locked_run(directory) as (state, manifest):
        if not state.get("approval") or state["approval"]["manifestSha256"] != state["manifestSha256"]:
            raise ValueError("需先核准此版本的具體變更清單")
        if state["inflight"] or state["status"] != STATUS_RUNNING:
            raise ValueError("有未確認結果或任務已停止，請先回讀核對；禁止重送")
        observed = accept_observation(state, raw_observation)
        event(state, "before_snapshot", snapshot=observed)
        for item in manifest["items"]:
            result = state["items"][item["itemId"]]
            if result["status"] != "pending":
                continue
            try:
                before, line_id = quantity_in(observed, item["binding"])
            except ValueError as exc:
                result.update(status="blocked", reason=str(exc))
                continue
            target = item["targetQty"]
            if item["sharedSku"] and item["itemId"] not in state["approval"]["sharedSkuEvidence"]:
                result.update(status="blocked", reason="共用 SKU 需求尚未確認非重複")
                continue
            if before == target == 0:
                result.update(status="matched", finalQty=0, verifiedAt=observed["capturedAt"], reason="零需求型號確實不在購物車")
                continue
            if target == 0 and not state["approval"]["allowRemovals"]:
                result.update(status="blocked", reason="零建議量，待集中核准移除")
                continue
            if item["itemId"] not in catalogs:
                instruction = {"action": "inspect_catalog", "itemId": item["itemId"],
                               "binding": item["binding"], "targetQty": target}
                event(state, "catalog_required", itemId=item["itemId"])
                persist(directory, state, manifest)
                return instruction
            try:
                live = catalog_check(item, catalogs.get(item["itemId"], {}), observed)
            except (ValueError, TypeError) as exc:
                result.update(status="blocked", reason=str(exc))
                continue
            result["catalogEvidence"] = deepcopy(catalogs[item["itemId"]])
            if before == target:
                result.update(status="matched", finalQty=before, verifiedAt=observed["capturedAt"], reason="完整規格、單位及重新讀回數量吻合")
                continue
            operation = {"operationId": new_run_id(), "itemId": item["itemId"],
                         "manifestSha256": state["manifestSha256"], "binding": live,
                         "action": "remove" if target == 0 else "set_quantity" if line_id else "add_missing",
                         "lineId": line_id, "beforeQty": before, "targetQty": target,
                         "delta": target - before, "beforeSnapshotId": observed["snapshotId"],
                         "catalogEvidence": deepcopy(catalogs[item["itemId"]])}
            state["inflight"] = operation
            result.update(status="unverified", finalQty=None, verifiedAt=None, reason="已準備操作，尚未取得操作後讀回")
            event(state, "prepared", operation=operation)
            persist(directory, state, manifest)
            return operation
        event(state, "awaiting_final_audit")
        persist(directory, state, manifest)
        return None


def record_after(directory, operation_id, raw_observation, *, settled=False, resolution_evidence=""):
    """An ambiguous submit stays paused until a newer, settled read resolves it.

    settled is an explicit operator assertion that no request remains in flight;
    it cannot be inferred from a success toast or a timeout.
    """
    with locked_run(directory) as (state, manifest):
        operation = state.get("inflight")
        if not operation or operation["operationId"] != operation_id:
            raise ValueError("operationId 與待核對操作不符")
        observed = accept_observation(state, raw_observation)
        result = state["items"][operation["itemId"]]
        try:
            after, _ = quantity_in(observed, operation["binding"])
            error = ""
        except ValueError as exc:
            after, error = None, str(exc)
        event(state, "after_snapshot", operationId=operation_id, snapshot=observed,
              settled=settled, resolutionEvidence=resolution_evidence)
        result.update(finalQty=after, verifiedAt=observed["capturedAt"])
        if after == operation["targetQty"]:
            result.update(status="corrected", reason="操作後逐 SKU 讀回恰好等於目標量")
            state.update(inflight=None, status=STATUS_RUNNING)
        elif settled and resolution_evidence.strip() and after is not None:
            result.update(status="pending", reason="已確認無待完成請求；下次依新讀數重算差額")
            state.update(inflight=None, status=STATUS_RUNNING)
        else:
            result.update(status="unverified", reason=error or "讀回數量不符；禁止盲目重送")
            state["status"] = STATUS_NEEDS_RECONCILE
        persist(directory, state, manifest)
        return deepcopy(result)


def final_audit(directory, raw_observation):
    with locked_run(directory) as (state, manifest):
        if state["inflight"] or not state.get("approval"):
            raise ValueError("尚有未確認操作或尚未核准，不能完成任務")
        observed = accept_observation(state, raw_observation)
        discrepancies = []
        for item in manifest["items"]:
            result = state["items"][item["itemId"]]
            if result["status"] == "blocked":
                continue
            if result["status"] not in CONFIRMED:
                discrepancies.append(item["itemId"])
                continue
            try:
                actual, _ = quantity_in(observed, item["binding"])
            except ValueError:
                actual = None
            result.update(finalQty=actual, verifiedAt=observed["capturedAt"])
            if actual != item["targetQty"]:
                result.update(status="pending", reason="最終重讀發現數量變動，需重新核對")
                discrepancies.append(item["itemId"])
            else:
                result.update(status="corrected" if result["status"] == "corrected" else "matched", reason="最終完整購物車讀回吻合")
        # Preserve out-of-scope initial rows and flag unexpected additions/removals.
        managed = [i["binding"] for i in manifest["items"] if i["binding"] and state["items"][i["itemId"]]["status"] != "blocked"]
        def unmanaged(cart):
            return [r for r in cart["rows"] if not any(same_sku(r, b) for b in managed)]
        baseline_unmanaged = {r["lineId"]: r for r in unmanaged(manifest["baseline"])}
        final_unmanaged = {r["lineId"]: r for r in unmanaged(observed)}
        drift = baseline_unmanaged != final_unmanaged
        state["finalAudit"] = {"snapshot": observed, "discrepancies": discrepancies, "unmanagedCartChanged": drift}
        gaps = bool(manifest["gaps"]) or any(r["status"] == "blocked" for r in state["items"].values()) or drift
        state["status"] = STATUS_RUNNING if discrepancies else STATUS_COMPLETED_GAPS if gaps else STATUS_COMPLETED
        event(state, "final_audit", discrepancies=discrepancies, unmanagedCartChanged=drift)
        persist(directory, state, manifest)
        return build_report(state, manifest)


def replan_mapping(directory, golden_path, *, evidence):
    """After an independently verified mapping repair, review a new immutable version.

    This function does not modify Golden Table. The existing mapping service owns
    that write. Scope, original cart, and Shopee data remain frozen.
    """
    if not evidence.strip():
        raise ValueError("需記錄 mapping 修正的確定證據")
    with locked_run(directory) as (state, old):
        if state["inflight"]:
            raise ValueError("先核對未完成操作，才能更新 mapping 計畫")
        products = json.loads((Path(directory) / "shopee-frozen.json").read_text())
        if digest(products) != old["sources"]["shopee"]["contentSha256"]:
            raise ValueError("凍結的蝦皮資料已改變")
        meta, golden = source_snapshot(golden_path)
        manifest = build_manifest(products, golden, old["baseline"],
                                  {**old["sources"], "golden": meta}, old["productIds"], old["stopBoundary"])
        manifest["parentManifestSha256"] = state["manifestSha256"]
        manifest["mappingRevisionEvidence"] = evidence
        atomic_write_json(Path(directory) / f"golden-{meta['contentSha256']}.json", golden)
        event(state, "mapping_replan", evidence=evidence, previousManifestSha256=state["manifestSha256"])
        install_manifest(directory, state, manifest)


def note_cause(directory, item_id, *, explanation, evidence="", confirmed=False):
    if not explanation.strip() or (confirmed and not evidence.strip()):
        raise ValueError("需原因說明；已證實原因必須附上程式、紀錄或頁面證據")
    with locked_run(directory) as (state, manifest):
        if item_id not in state["items"]:
            raise ValueError("未知 itemId")
        diagnosis = {"explanation": explanation, "evidence": evidence,
                     "certainty": "confirmed" if confirmed else "hypothesis"}
        state["items"][item_id]["diagnosis"] = diagnosis
        event(state, "diagnosis", itemId=item_id, manifestSha256=state["manifestSha256"], **diagnosis)
        persist(directory, state, manifest)


def build_report(state, manifest):
    rows = [{**deepcopy(item), **state["items"][item["itemId"]]} for item in manifest["items"]]
    counts = {name: sum(r["status"] == name for r in rows) for name in ("matched", "corrected", "blocked", "unverified", "pending")}
    return {"runId": state["runId"], "mode": MODE, "status": state["status"], "updatedAt": state.get("updatedAt"),
            "manifestSha256": state["manifestSha256"], "sources": manifest["sources"],
            "productIds": manifest["productIds"], "total": len(rows),
            "completed": counts["matched"] + counts["corrected"] + counts["blocked"],
            "counts": counts, "currentOperation": state["inflight"], "approval": state["approval"],
            "finalAudit": state["finalAudit"], "items": rows, "gaps": manifest["gaps"],
            "stopBoundary": manifest["stopBoundary"], "excludedItemsAfterStop": manifest["excludedItemsAfterStop"],
            "events": state["events"]}


def render_report(report):
    esc = lambda value: html.escape("—" if value is None else str(value), quote=True)
    labels = {"review": "待審核", "running": "核對中", "completed": "已完成",
              "completed_with_gaps": "已完成，仍有待查項目", "needs_reconciliation": "暫停，需回讀核對",
              "matched": "已吻合", "corrected": "已修正", "blocked": "待查", "unverified": "未驗證", "pending": "待核對"}
    body = []
    for item in report["items"]:
        names = "<br>".join(esc(f"{s['productName']} / {s['modelName']} ({s['productId']}/{s['specId']})") for s in item["sources"])
        numbers = "<br>".join(esc(f"月銷 {s.get('effectiveMonthlySales', '未知')} × {s.get('targetMonths', '未知')} 月；庫存 {s.get('currentStock', '未知')}；缺口 {s.get('rawShortage', '未知')} → {s.get('suggestedQty', '未知')}") for s in item["sources"])
        binding = item["binding"]
        link = (f'<a href="{esc(binding["url"])}" rel="noreferrer">{esc(" / ".join(binding["specs"]) or binding["skuId"])}</a>' if binding else "待確認網址")
        diagnosis = item.get("diagnosis") or {}
        cause = ("已證實：" if diagnosis.get("certainty") == "confirmed" else "推測：") + diagnosis["explanation"] if diagnosis else "漏加原因尚未證實"
        body.append(f'<tr><td>{esc(item["itemId"])}<br>{names}</td><td>{numbers}</td><td>{esc(item["originalQty"])}</td><td>{esc(item["targetQty"])}</td><td>{esc(item["finalQty"])}</td><td>{esc(labels[item["status"]])}<br>{esc(item["reason"])}<br>{esc(cause)}</td><td>{link}</td></tr>')
    gaps = "".join(f'<li>{esc(g.get("lineId"))}：{esc(g["reason"])} · {esc(g.get("url"))}</li>' for g in report["gaps"])
    sources = "".join(f'<li>{esc(k)}：{esc(v["path"])}；修改時間 {esc(v["mtime"])}；SHA256 {esc(v["sha256"])}；本次未重爬</li>' for k, v in report["sources"].items())
    current = report["currentOperation"]
    current_text = f'{current["itemId"]}：{current["beforeQty"]} → {current["targetQty"]}（尚未驗證）' if current else "無待回讀操作"
    refresh = '<meta http-equiv="refresh" content="5">' if report["status"] not in {STATUS_COMPLETED, STATUS_COMPLETED_GAPS} else ""
    demo = '<p style="background:#fff0cc;padding:16px"><strong>模擬資料示範：不是你的購物車，未執行任何實際加購。</strong></p>' if report.get("demo") else ""
    boundary = report["stopBoundary"]
    stop_text = (f'停止於原購物車第 {boundary["originalIndex"] + 1} 列：{boundary["productName"]}。書包本身及後方 {boundary["excludedCartRows"]} 列（含書包）不處理。'
                 if boundary["found"] else '原始基線未找到書包；本次範圍至購物車底端。')
    return f'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">{refresh}
<title>1688 購物車核對</title><style>body{{font-family:system-ui;margin:24px;color:#183044;background:#f5f7fa}}header{{position:sticky;top:0;background:#fff;padding:16px;border-bottom:3px solid #168379}}table{{border-collapse:collapse;width:100%;background:white}}td,th{{border:1px solid #d4dce4;padding:10px;text-align:left;vertical-align:top}}td{{overflow-wrap:anywhere}}progress{{width:100%}}small{{overflow-wrap:anywhere}}</style></head><body>
<header>{demo}<h1>1688 購物車逐項核對</h1><p>{esc(labels.get(report['status'], report['status']))} · 已處理 {report['completed']} / {report['total']} · 已吻合 {report['counts']['matched']} · 已修正 {report['counts']['corrected']} · 待查 {report['counts']['blocked']} · 未驗證 {report['counts']['unverified']}</p>
<progress value="{report['completed']}" max="{max(report['total'], 1)}"></progress><p>{esc(current_text)}</p></header>
<p>手機殼 3 個月，其餘 4 個月。目標為購物車最終總數；完成需最終完整重讀。</p>
<p><strong>{esc(stop_text)}</strong></p>
<small>任務 {esc(report['runId'])} · 清單版本 {esc(report['manifestSha256'])}<ul>{sources}</ul></small>
<table><thead><tr><th>商品／型號</th><th>計算</th><th>原數量</th><th>目標量</th><th>讀回量</th><th>結果／原因</th><th>1688</th></tr></thead><tbody>{''.join(body)}</tbody></table>
<h2>購物車未能唯一對應的項目</h2><ul>{gaps or '<li>無</li>'}</ul>
<p>數量差異本身不能證明漏加原因；操作與讀回證據保存在同目錄 report.json。</p></body></html>'''
