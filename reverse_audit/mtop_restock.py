"""Phase 3: wire restock / reverse_audit mutate onto Phase 1/2 signed mtop.

Transport switch lives in ``mtop_switch``. This module reuses
``AddCargoClient`` / ``UltronMutateClient`` / ``ReadCartClient``.

Session comes from already-logged-in Chrome via ``connect_over_cdp``
(or cookies already on a restocker page). Never launches or kills Chrome.

Approve is the *existing* Path A / Path B flag, not ``--via-mtop``.
Without approve, clients only build payloads and must not POST.

On mtop errors: fail-closed. Do **not** silently fall back to DOM clicks
(that can double-add). Operator unsets the switch to use DOM instead.

Forbids checkout / clear-cart / Golden writes / auto_approve.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from reverse_audit.mtop_addcargo import AddCargoClient, parse_offer_id
from reverse_audit.mtop_http import (
    MutateSafetyError,
    Transport,
    mask_for_log,
)
from reverse_audit.mtop_read_cart import CartLine, MtopCallError, ReadCartClient
from reverse_audit.mtop_session import (
    CdpUnavailableError,
    MtopSession,
    NotLoggedInError,
    SessionError,
    load_session_from_cdp,
    session_from_playwright_cookies,
)
from reverse_audit.mtop_sku_map import SkuMapError
from reverse_audit.mtop_ultron_mutate import ItemNodeError, UltronMutateClient
from reverse_audit.mutate import (
    load_csv_rows,
    load_order_keys_from_live,
    load_uncertain_keys_from_csv,
    plan_remove_rows,
    plan_set_qty_rows,
)

LogFn = Callable[[str, Dict[str, Any]], None]


class MtopRestockError(RuntimeError):
    """Fail-closed restock/mutate HTTP error. Caller must not DOM-fallback."""


def session_from_page(page: Any) -> Optional[MtopSession]:
    """Build a session from an already-open Playwright page. Never launches."""
    if page is None:
        return None
    context = getattr(page, "context", None)
    if context is None or not hasattr(context, "cookies"):
        return None
    try:
        cookies = context.cookies()
    except Exception as exc:  # noqa: BLE001 — page may be a test double
        raise MtopRestockError(
            f"卡在登入／session：page cookies unreadable ({type(exc).__name__})"
        ) from exc
    session = session_from_playwright_cookies(cookies, source="restocker-page")
    return session if session.cookies else None


def load_mtop_session(
    *,
    page: Any = None,
    session: Optional[MtopSession] = None,
    cdp: Optional[str] = None,
    load_session: Optional[Callable[[], MtopSession]] = None,
) -> MtopSession:
    """Prefer an injected session, then page cookies, then connect_over_cdp."""
    if session is not None:
        return session
    if load_session is not None:
        return load_session()
    from_page = session_from_page(page)
    if from_page is not None and from_page.token():
        return from_page
    try:
        return load_session_from_cdp(cdp)
    except CdpUnavailableError as exc:
        if from_page is not None:
            # Cookies present but no _m_h5_tk and CDP also failed.
            raise MtopRestockError(
                "卡在登入／session：page cookies missing _m_h5_tk and CDP "
                "attach failed. Do not fall back to DOM (risk of double-add). "
                f"{exc}"
            ) from exc
        raise MtopRestockError(
            "卡在登入／session：need already-logged-in Chrome via "
            "connect_over_cdp (never launch/kill Chrome). "
            f"{exc}"
        ) from exc


def _offer_id_of(item: Dict[str, Any]) -> str:
    raw = (
        item.get("offer_id")
        or item.get("alibabaOfferId")
        or item.get("offerId")
        or item.get("alibaba_url")
        or item.get("alibabaUrl")
        or ""
    )
    try:
        return str(parse_offer_id(raw))
    except MutateSafetyError as exc:
        raise MtopRestockError(f"卡在業務參數：{exc}") from exc


def _sku_id_of(item: Dict[str, Any]) -> str:
    return str(
        item.get("sku_id")
        or item.get("alibabaSkuId")
        or item.get("skuId")
        or ""
    ).strip()


def _qty_of(item: Dict[str, Any], *keys: str) -> int:
    for key in keys:
        raw = item.get(key)
        if raw in (None, ""):
            continue
        try:
            qty = int(float(str(raw).replace(",", "").strip()))
        except (TypeError, ValueError) as exc:
            raise MtopRestockError(f"卡在業務參數：bad qty {raw!r}") from exc
        if qty < 1:
            raise MtopRestockError("卡在業務參數：quantity must be >= 1")
        return qty
    raise MtopRestockError("卡在業務參數：quantity is required")


def _spec_id_of(item: Dict[str, Any]) -> str:
    return str(item.get("spec_id") or item.get("alibabaSpecId") or "").strip()


def _page_html(page: Any, detail_html: Optional[str] = None) -> Optional[str]:
    if detail_html is not None:
        return detail_html
    if page is None or not hasattr(page, "content"):
        return None
    try:
        html = page.content()
    except Exception as exc:  # noqa: BLE001
        raise MtopRestockError(
            f"卡在業務參數：cannot read offer HTML ({type(exc).__name__})"
        ) from exc
    return str(html or "")


def _qty_on_lines(lines: Sequence[CartLine], offer_id: str, sku_id: str) -> int:
    total = 0
    for line in lines:
        if str(line.offerId) == str(offer_id) and (
            not sku_id or str(line.skuId) == str(sku_id)
        ):
            total += int(line.qty or 0)
    return total


def read_cart_lines(
    session: MtopSession,
    *,
    transport: Optional[Transport] = None,
    reader: Optional[ReadCartClient] = None,
) -> List[CartLine]:
    client = reader or ReadCartClient(session, transport=transport)
    return list(client.read_cart().lines)


@dataclass
class ItemMutateResult:
    action: str
    offer_id: str = ""
    sku_id: str = ""
    spec_id: str = ""
    cart_id: str = ""
    quantity: Optional[int] = None
    posted: bool = False
    ok: bool = False
    error: str = ""
    ret: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return mask_for_log(
            {
                "action": self.action,
                "offerId": self.offer_id,
                "skuId": self.sku_id,
                "specId": self.spec_id,
                "cartId": self.cart_id,
                "quantity": self.quantity,
                "posted": self.posted,
                "ok": self.ok,
                "error": self.error,
                "ret": list(self.ret),
            }
        )


def add_one(
    item: Dict[str, Any],
    *,
    approve: bool,
    session: Optional[MtopSession] = None,
    transport: Optional[Transport] = None,
    detail_html: Optional[str] = None,
    fetch_html: Optional[Callable[[str], str]] = None,
    client: Optional[AddCargoClient] = None,
) -> ItemMutateResult:
    """Add one SKU via addcargo. Without approve, builds payload and does not POST."""
    offer_id = _offer_id_of(item)
    sku_id = _sku_id_of(item)
    spec_id = _spec_id_of(item)
    qty = _qty_of(item, "quantity", "restockQty", "expected_qty", "qty")
    result = ItemMutateResult(
        action="addcargo",
        offer_id=offer_id,
        sku_id=sku_id,
        spec_id=spec_id,
        quantity=qty,
    )
    worker = client or AddCargoClient(session, transport=transport)
    try:
        plan = worker.plan(
            offer_id=offer_id,
            quantity=qty,
            spec_id=spec_id or None,
            sku_id=sku_id or None,
            detail_html=detail_html,
            fetch_detail=bool(fetch_html) and not spec_id,
            fetch_html=fetch_html,
        )
        result.spec_id = plan.spec_id
        worker.execute(plan, approve=bool(approve))
        result.posted = bool(plan.posted)
        result.ret = list(plan.ret)
        result.ok = bool(plan.posted) if approve else True
        if approve and not plan.posted:
            result.ok = False
            result.error = "approve set but addcargo did not POST"
        return result
    except (MutateSafetyError, SkuMapError, MtopCallError, SessionError, NotLoggedInError) as exc:
        result.ok = False
        if isinstance(exc, NotLoggedInError):
            result.error = f"卡在登入／session：{exc}"
        elif isinstance(exc, MtopCallError) and getattr(exc, "kind", "") == "bad_sign":
            result.error = f"卡在簽章：{exc}"
        elif isinstance(exc, MtopCallError):
            result.error = f"卡在業務參數／風控／Ultron 包：{exc}"
        else:
            result.error = f"卡在業務參數：{exc}"
        return result


def set_qty_one(
    *,
    cart_id: str,
    quantity: Any,
    approve: bool,
    session: Optional[MtopSession] = None,
    transport: Optional[Transport] = None,
    fixture_payload: Any = None,
    client: Optional[UltronMutateClient] = None,
) -> ItemMutateResult:
    result = ItemMutateResult(action="set-qty", cart_id=str(cart_id), quantity=None)
    worker = client or UltronMutateClient(session, transport=transport)
    try:
        plan = worker.plan_set_qty(
            cart_id=cart_id,
            quantity=quantity,
            fixture_payload=fixture_payload,
        )
        result.quantity = plan.quantity
        worker.execute(plan, approve=bool(approve))
        result.posted = bool(plan.posted)
        result.ret = list(plan.ret)
        result.ok = bool(plan.posted) if approve else True
        return result
    except (
        MutateSafetyError,
        ItemNodeError,
        MtopCallError,
        SessionError,
        NotLoggedInError,
    ) as exc:
        result.ok = False
        result.error = f"卡在 Ultron 改量：{exc}"
        if isinstance(exc, NotLoggedInError):
            result.error = f"卡在登入／session：{exc}"
        elif isinstance(exc, MtopCallError) and exc.kind == "bad_sign":
            result.error = f"卡在簽章：{exc}"
        return result


def remove_one(
    *,
    cart_id: str,
    approve: bool,
    session: Optional[MtopSession] = None,
    transport: Optional[Transport] = None,
    fixture_payload: Any = None,
    client: Optional[UltronMutateClient] = None,
) -> ItemMutateResult:
    result = ItemMutateResult(action="remove", cart_id=str(cart_id))
    worker = client or UltronMutateClient(session, transport=transport)
    try:
        plan = worker.plan_remove(cart_id=cart_id, fixture_payload=fixture_payload)
        worker.execute(plan, approve=bool(approve))
        result.posted = bool(plan.posted)
        result.ret = list(plan.ret)
        result.ok = bool(plan.posted) if approve else True
        return result
    except (
        MutateSafetyError,
        ItemNodeError,
        MtopCallError,
        SessionError,
        NotLoggedInError,
    ) as exc:
        result.ok = False
        result.error = f"卡在 Ultron 刪列：{exc}"
        if isinstance(exc, NotLoggedInError):
            result.error = f"卡在登入／session：{exc}"
        elif isinstance(exc, MtopCallError) and exc.kind == "bad_sign":
            result.error = f"卡在簽章：{exc}"
        return result


def add_items(
    items: Sequence[Dict[str, Any]],
    *,
    approve: bool,
    session: Optional[MtopSession] = None,
    transport: Optional[Transport] = None,
    detail_html: Optional[str] = None,
    fetch_html: Optional[Callable[[str], str]] = None,
    reconcile: bool = True,
    reader: Optional[ReadCartClient] = None,
    log: Optional[LogFn] = None,
) -> Dict[str, Any]:
    """Add each item with one addcargo call. No batch-add API. No DOM click."""
    results: List[ItemMutateResult] = []
    posted_any = False
    if not approve:
        for item in items:
            one = add_one(
                item,
                approve=False,
                session=session,
                transport=transport,
                detail_html=detail_html,
                fetch_html=fetch_html,
            )
            results.append(one)
        return {
            "ok": False,
            "status": "refused",
            "mode": "mtop_addcargo",
            "posted": False,
            "message": "refusing POST without approve flag (via-mtop is not an approve)",
            "itemCount": len(items),
            "quantityTotal": sum(int(r.quantity or 0) for r in results),
            "results": [r.as_dict() for r in results],
            "didNotFallbackToDom": True,
        }

    before_lines: List[CartLine] = []
    if reconcile and session is not None:
        try:
            before_lines = read_cart_lines(session, transport=transport, reader=reader)
        except (MtopCallError, SessionError) as exc:
            if log:
                log("mtop_read_cart_before_failed", {"message": str(exc)})
            before_lines = []

    for item in items:
        one = add_one(
            item,
            approve=True,
            session=session,
            transport=transport,
            detail_html=detail_html,
            fetch_html=fetch_html,
        )
        posted_any = posted_any or one.posted
        results.append(one)
        if log:
            log("mtop_addcargo", one.as_dict())

    after_lines: Optional[List[CartLine]] = None
    unverified = False
    if reconcile and posted_any and session is not None:
        try:
            after_lines = read_cart_lines(session, transport=transport, reader=reader)
        except (MtopCallError, SessionError) as exc:
            unverified = True
            if log:
                log("mtop_read_cart_after_failed", {"message": str(exc)})

    if after_lines is not None:
        for one in results:
            if not one.posted:
                continue
            before_qty = _qty_on_lines(before_lines, one.offer_id, one.sku_id)
            after_qty = _qty_on_lines(after_lines, one.offer_id, one.sku_id)
            if after_qty - before_qty < int(one.quantity or 0):
                # Already POSTed — do not retry / do not DOM-click (double-add).
                unverified = True
                one.error = (
                    f"read_cart delta {after_qty - before_qty} < expected {one.quantity} "
                    "(unverified; not retrying)"
                )

    failed = [r for r in results if not r.ok]
    if failed:
        status = "failed"
        ok = False
        message = failed[0].error or "mtop addcargo failed (fail-closed; not falling back to DOM)"
    elif unverified:
        status = "clicked_unverified"
        ok = True
        message = "addcargo POST ok; read_cart did not confirm qty (not retrying, not falling back to DOM)"
    else:
        status = "success"
        ok = True
        message = "mtop addcargo ok"

    return {
        "ok": ok,
        "status": status,
        "mode": "mtop_addcargo",
        "posted": posted_any,
        "message": message,
        "itemCount": len(items),
        "modelNames": [str(item.get("modelName") or item.get("sku_name") or "") for item in items],
        "quantityTotal": sum(int(r.quantity or 0) for r in results),
        "results": [r.as_dict() for r in results],
        "didNotFallbackToDom": True,
        "attempts": [
            {
                "attempt": 1,
                "clickResult": {"ok": posted_any, "method": "mtop-addcargo"},
                "feedback": {"status": status, "message": message},
            }
        ],
    }


def restock_add_via_mtop(
    page: Any,
    cart_items: Sequence[Dict[str, Any]],
    debug: Any = None,
    *,
    approve: bool = True,
    session: Optional[MtopSession] = None,
    transport: Optional[Transport] = None,
    fetch_html: Optional[Callable[[str], str]] = None,
    load_session: Optional[Callable[[], MtopSession]] = None,
) -> Dict[str, Any]:
    """Path A hook: replace DOM 加采购车 with per-SKU addcargo."""
    log: Optional[LogFn] = debug.log if debug is not None and hasattr(debug, "log") else None
    try:
        sess = load_mtop_session(page=page, session=session, load_session=load_session)
        html = _page_html(page)
        return add_items(
            cart_items,
            approve=approve,
            session=sess,
            transport=transport,
            detail_html=html,
            fetch_html=fetch_html,
            log=log,
        )
    except MtopRestockError as exc:
        if log:
            log("mtop_restock_fail_closed", {"message": str(exc)})
        return {
            "ok": False,
            "status": "failed",
            "mode": "mtop_addcargo",
            "posted": False,
            "message": str(exc),
            "itemCount": len(cart_items),
            "modelNames": [str(item.get("modelName") or "") for item in cart_items],
            "quantityTotal": sum(int(item.get("quantity") or 0) for item in cart_items),
            "didNotFallbackToDom": True,
        }


def _write_result(out_dir: Path, name: str, payload: Dict[str, Any]) -> None:
    path = Path(out_dir) / name
    path.write_text(
        json.dumps(mask_for_log(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_add_via_mtop(
    out_dir: Path,
    *,
    approve: bool,
    session: Optional[MtopSession] = None,
    transport: Optional[Transport] = None,
    fetch_html: Optional[Callable[[str], str]] = None,
    detail_html: Optional[str] = None,
    load_session: Optional[Callable[[], MtopSession]] = None,
    reader: Optional[ReadCartClient] = None,
) -> int:
    """Path B add: missing_to_add.csv via addcargo. Existing approve still required."""
    out_dir = Path(out_dir)
    rows = load_csv_rows(out_dir / "missing_to_add.csv")
    if not rows:
        print("[mutate:add:mtop] missing_to_add.csv empty — nothing to add", flush=True)
        _write_result(out_dir, "mutate_mtop_add_result.json", {"status": "noop", "posted": False})
        return 0
    items = [
        {
            "offer_id": row.get("offer_id"),
            "sku_id": row.get("sku_id"),
            "expected_qty": row.get("expected_qty"),
            "alibaba_url": row.get("alibaba_url"),
            "sku_name": row.get("sku_name"),
        }
        for row in rows
    ]
    if not approve:
        summary = add_items(items, approve=False, session=session, transport=transport)
        _write_result(out_dir, "mutate_mtop_add_result.json", summary)
        print("[mutate:add:mtop] refusing POST without --i-approve-mutate", flush=True)
        return 2
    sess = load_mtop_session(session=session, load_session=load_session)
    summary = add_items(
        items,
        approve=True,
        session=sess,
        transport=transport,
        detail_html=detail_html,
        fetch_html=fetch_html,
        reader=reader,
    )
    _write_result(out_dir, "mutate_mtop_add_result.json", summary)
    print(
        f"[mutate:add:mtop] posted={summary.get('posted')} status={summary.get('status')}",
        flush=True,
    )
    return 0 if summary.get("ok") else 1


def run_set_qty_via_mtop(
    out_dir: Path,
    *,
    approve: bool,
    session: Optional[MtopSession] = None,
    transport: Optional[Transport] = None,
    fixture_payload: Any = None,
    load_session: Optional[Callable[[], MtopSession]] = None,
    client: Optional[UltronMutateClient] = None,
) -> int:
    out_dir = Path(out_dir)
    accepted, skipped = plan_set_qty_rows(out_dir)
    results: List[Dict[str, Any]] = []
    if not approve:
        payload = {
            "status": "refused",
            "posted": False,
            "message": "refusing POST without --i-approve-set-qty",
            "accepted": accepted,
            "skipped": skipped,
        }
        _write_result(out_dir, "mutate_mtop_set_qty_result.json", payload)
        print("[mutate:set-qty:mtop] refusing POST without --i-approve-set-qty", flush=True)
        return 2
    if not accepted:
        _write_result(
            out_dir,
            "mutate_mtop_set_qty_result.json",
            {"status": "noop", "posted": False, "skipped": skipped},
        )
        print("[mutate:set-qty:mtop] no accepted rows — nothing to change", flush=True)
        return 0
    sess = session
    worker = client
    if worker is None:
        sess = load_mtop_session(session=session, load_session=load_session)
        worker = UltronMutateClient(sess, transport=transport)
    posted_any = False
    failed = False
    for row in accepted:
        one = set_qty_one(
            cart_id=str(row.get("cart_id") or ""),
            quantity=row.get("target_qty"),
            approve=True,
            session=sess,
            transport=transport,
            fixture_payload=fixture_payload,
            client=worker,
        )
        posted_any = posted_any or one.posted
        failed = failed or not one.ok
        results.append(one.as_dict())
        print(
            f"[mutate:set-qty:mtop] cartId={one.cart_id} qty={one.quantity} "
            f"posted={one.posted} ok={one.ok}",
            flush=True,
        )
        if fixture_payload is None:
            # Live: next op must re-read Ultron render (cart changed).
            pass
    payload = {
        "status": "failed" if failed else "success",
        "posted": posted_any,
        "results": results,
        "skipped": skipped,
        "didNotFallbackToDom": True,
    }
    _write_result(out_dir, "mutate_mtop_set_qty_result.json", payload)
    return 1 if failed else 0


def run_remove_via_mtop(
    out_dir: Path,
    *,
    approve: bool,
    session: Optional[MtopSession] = None,
    transport: Optional[Transport] = None,
    fixture_payload: Any = None,
    load_session: Optional[Callable[[], MtopSession]] = None,
    client: Optional[UltronMutateClient] = None,
) -> int:
    out_dir = Path(out_dir)
    order_keys = load_order_keys_from_live(out_dir)
    uncertain_keys = load_uncertain_keys_from_csv(out_dir)
    accepted, skipped = plan_remove_rows(
        out_dir, order_keys=order_keys, uncertain_keys=uncertain_keys
    )
    if not approve:
        payload = {
            "status": "refused",
            "posted": False,
            "message": "refusing POST without --i-approve-remove",
            "accepted": accepted,
            "skipped": skipped,
        }
        _write_result(out_dir, "mutate_mtop_remove_result.json", payload)
        print("[mutate:remove:mtop] refusing POST without --i-approve-remove", flush=True)
        return 2
    if not accepted:
        _write_result(
            out_dir,
            "mutate_mtop_remove_result.json",
            {"status": "noop", "posted": False, "skipped": skipped},
        )
        print("[mutate:remove:mtop] no accepted removable rows — nothing to delete", flush=True)
        return 0
    sess = session
    worker = client
    if worker is None:
        sess = load_mtop_session(session=session, load_session=load_session)
        worker = UltronMutateClient(sess, transport=transport)
    posted_any = False
    failed = False
    results: List[Dict[str, Any]] = []
    for row in accepted:
        one = remove_one(
            cart_id=str(row.get("cart_id") or ""),
            approve=True,
            session=sess,
            transport=transport,
            fixture_payload=fixture_payload,
            client=worker,
        )
        posted_any = posted_any or one.posted
        failed = failed or not one.ok
        results.append(one.as_dict())
        print(
            f"[mutate:remove:mtop] cartId={one.cart_id} posted={one.posted} ok={one.ok}",
            flush=True,
        )
    payload = {
        "status": "failed" if failed else "success",
        "posted": posted_any,
        "results": results,
        "skipped": skipped,
        "didNotFallbackToDom": True,
    }
    _write_result(out_dir, "mutate_mtop_remove_result.json", payload)
    return 1 if failed else 0
