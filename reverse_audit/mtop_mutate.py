"""Phase 2 CLI: signed mtop addcargo / Ultron set-qty / delete.

Default is dry-run (build + print a masked payload). POST happens only with
exactly one explicit flag:

  --i-approve-add-one
  --i-approve-set-qty
  --i-approve-remove-one

Usage:
  python -m reverse_audit.mtop_mutate add --offer-id ID --spec-id SPEC --qty 1
  python -m reverse_audit.mtop_mutate add --offer-id ID --sku-id SKU --detail-html FILE --qty 1
  python -m reverse_audit.mtop_mutate set-qty --cart-id ID --qty N --fixture ultron_render_model.json
  python -m reverse_audit.mtop_mutate remove --cart-id ID --fixture ultron_render_model.json

Live one-op (operator machine, already-logged-in Chrome; CI must stay offline):
  python -m reverse_audit.mtop_mutate add --offer-id ID --spec-id SPEC --qty 1 \\
      --cdp http://127.0.0.1:9227 --i-approve-add-one
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from reverse_audit.mtop_addcargo import APPROVE_ADD_ONE, AddCargoClient, parse_offer_id
from reverse_audit.mtop_http import (
    EXIT_OK,
    EXIT_USAGE,
    MutateSafetyError,
    dumps_pretty,
    exit_for_exc,
)
from reverse_audit.mtop_read_cart import ForbiddenApiError, MtopCallError
from reverse_audit.mtop_session import (
    CdpUnavailableError,
    NotLoggedInError,
    SessionError,
    load_cookie_jar,
    load_session_from_cdp,
)
from reverse_audit.mtop_sku_map import SkuMapError
from reverse_audit.mtop_ultron_mutate import (
    APPROVE_REMOVE_ONE,
    APPROVE_SET_QTY,
    ItemNodeError,
    UltronMutateClient,
)


def _add_session_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cdp",
        nargs="?",
        const="",
        default=None,
        help=(
            "Already-logged-in Chrome via connect_over_cdp. Optional URL "
            "(else ALIBABA_RESTOCK_CDP, then 9227 / 9223). Never launches Chrome."
        ),
    )
    parser.add_argument(
        "--cookie-jar",
        dest="cookie_jar",
        default=None,
        help="Local Netscape / Playwright JSON / Cookie-header export (keep out of git).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Print the masked plan as JSON",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Write the same masked JSON (still no cookies / tokens / signs)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m reverse_audit.mtop_mutate",
        description=(
            "Phase 2 signed 1688 mtop mutate (addcargo / Ultron set-qty / delete). "
            "Default dry-run: build a masked payload and do NOT POST. "
            "Live POST requires the matching one-op flag. "
            "Forbids checkout / payment / clear-cart / batch-add."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add_p = sub.add_parser("add", help="addcargo one SKU (default dry-run)")
    _add_session_args(add_p)
    add_p.add_argument(
        "--offer-id",
        dest="offer_id",
        default=None,
        help="1688 offerId (digits). Or pass --offer-url.",
    )
    add_p.add_argument(
        "--offer-url",
        dest="offer_url",
        default=None,
        help="detail.1688.com/offer/<id>.html (parsed to offerId; still one SKU)",
    )
    add_p.add_argument("--spec-id", dest="spec_id", default=None, help="SKU specId (preferred)")
    add_p.add_argument(
        "--sku-id",
        dest="sku_id",
        default=None,
        help="If no --spec-id: resolve via detail HTML skuMapOriginal",
    )
    add_p.add_argument(
        "--detail-html",
        dest="detail_html",
        default=None,
        help="Local offer HTML for skuId→specId (CI / offline)",
    )
    add_p.add_argument(
        "--fetch-detail",
        action="store_true",
        help="Read-only GET of detail.1688.com/offer/<id>.html to parse skuMapOriginal",
    )
    add_p.add_argument("--qty", dest="qty", required=True, help="This add quantity (>= 1)")
    add_p.add_argument(
        APPROVE_ADD_ONE,
        action="store_true",
        dest="approve_add_one",
        help="POST addcargo once. Without this flag the client only prints the payload.",
    )

    qty_p = sub.add_parser("set-qty", help="Ultron async set quantity (default dry-run)")
    _add_session_args(qty_p)
    qty_p.add_argument("--cart-id", dest="cart_id", required=True, help="Cart line cartId")
    qty_p.add_argument("--qty", dest="qty", required=True, help="New absolute quantity (>= 1)")
    qty_p.add_argument(
        "--fixture",
        default=None,
        help="Phase 1 render JSON with full Ultron model (endpoint/linkage/hierarchy/data). Required for dry-run without --cdp.",
    )
    qty_p.add_argument(
        "--address-id",
        dest="address_id",
        default=None,
        help="Optional render addressId when reading live (do not commit).",
    )
    qty_p.add_argument(
        APPROVE_SET_QTY,
        action="store_true",
        dest="approve_set_qty",
        help="POST Ultron set-qty once. Without this flag the client only prints the payload.",
    )

    rm_p = sub.add_parser("remove", help="Ultron async deleteClick one line (default dry-run)")
    _add_session_args(rm_p)
    rm_p.add_argument("--cart-id", dest="cart_id", required=True, help="Cart line cartId")
    rm_p.add_argument(
        "--fixture",
        default=None,
        help="Phase 1 render JSON with full Ultron model (endpoint/linkage/hierarchy/data). Required for dry-run without --cdp.",
    )
    rm_p.add_argument(
        "--address-id",
        dest="address_id",
        default=None,
        help="Optional render addressId when reading live (do not commit).",
    )
    rm_p.add_argument(
        APPROVE_REMOVE_ONE,
        action="store_true",
        dest="approve_remove_one",
        help="POST Ultron deleteClick once. Without this flag the client only prints the payload.",
    )
    return parser


def _load_session(args: argparse.Namespace):
    if args.cookie_jar:
        return load_cookie_jar(args.cookie_jar)
    if args.cdp is not None:
        return load_session_from_cdp(args.cdp)
    return None


def _load_fixture(path: Optional[str], *, required: bool) -> Any:
    if not path:
        if required:
            raise MutateSafetyError(
                "need --fixture (Phase 1 render with full Ultron model) for dry-run, "
                "or --cdp/--cookie-jar to read live render. "
                "Will not invent endpoint / linkage / hierarchy."
            )
        return None
    fixture_path = Path(path)
    if not fixture_path.is_file():
        raise MutateSafetyError(f"fixture not found: {fixture_path}")
    return json.loads(fixture_path.read_text(encoding="utf-8"))


def _write_out(path: str, payload: Any) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dumps_pretty(payload) + "\n", encoding="utf-8")


def _emit(preview: dict, args: argparse.Namespace) -> None:
    if args.out:
        _write_out(args.out, preview)
    if args.as_json:
        print(dumps_pretty(preview))
        return
    posted = bool(preview.get("posted"))
    dry = bool(preview.get("dryRun"))
    action = preview.get("action")
    flag = preview.get("approveFlag")
    print(f"# phase2 {action} {'posted' if posted else 'dry-run'}")
    print(f"# posted={str(posted).lower()} dryRun={str(dry).lower()} approveFlag={flag}")
    print(f"# api={preview.get('api')}")
    if action == "addcargo":
        print(
            f"# offerId={preview.get('offerId')} specId={preview.get('specId')} "
            f"qty={preview.get('quantity')}"
        )
    else:
        print(
            f"# cartId={preview.get('cartId')} qty={preview.get('quantity')} "
            f"deleteClick={preview.get('hasDeleteClick')}"
        )
    if not posted:
        print(f"# not posted — pass {flag} to POST once (operator machine only; CI stays offline)")
    print(dumps_pretty(preview))


def _preview_token(session) -> str:
    if session is None:
        return ""
    try:
        return session.token()
    except Exception:  # noqa: BLE001 — preview never needs a hard fail here
        return ""


def run_add(args: argparse.Namespace) -> int:
    session = _load_session(args)
    offer_raw = args.offer_id or args.offer_url
    if not offer_raw:
        raise MutateSafetyError("need --offer-id or --offer-url")
    offer_id = parse_offer_id(offer_raw)
    client = AddCargoClient(session)
    plan = client.plan(
        offer_id=offer_id,
        quantity=args.qty,
        spec_id=args.spec_id,
        sku_id=args.sku_id,
        detail_html_path=args.detail_html,
        fetch_detail=bool(args.fetch_detail),
    )
    client.execute(plan, approve=bool(args.approve_add_one))
    preview = plan.preview(token=_preview_token(session), t=str(client.now_ms()))
    _emit(preview, args)
    return EXIT_OK


def run_ultron(args: argparse.Namespace, *, action: str) -> int:
    session = _load_session(args)
    approve = bool(
        getattr(args, "approve_set_qty", False) or getattr(args, "approve_remove_one", False)
    )
    live_item = session is not None and not args.fixture
    fixture_payload = _load_fixture(
        args.fixture,
        required=not live_item,
    )
    client = UltronMutateClient(session, address_id=getattr(args, "address_id", None))
    if action == "set-qty":
        plan = client.plan_set_qty(
            cart_id=args.cart_id,
            quantity=args.qty,
            fixture_payload=fixture_payload,
        )
    else:
        plan = client.plan_remove(
            cart_id=args.cart_id,
            fixture_payload=fixture_payload,
        )
    client.execute(plan, approve=approve)
    preview = plan.preview(token=_preview_token(session), t=str(client.now_ms()))
    _emit(preview, args)
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "read":
        from reverse_audit.mtop_read_cart import main as read_main

        return read_main(argv[1:])
    args = build_parser().parse_args(argv)
    if getattr(args, "cdp", None) is not None and str(args.cdp).strip():
        os.environ["ALIBABA_RESTOCK_CDP"] = str(args.cdp).strip()

    approve_add = bool(getattr(args, "approve_add_one", False))
    approve_qty = bool(getattr(args, "approve_set_qty", False))
    approve_rm = bool(getattr(args, "approve_remove_one", False))
    if sum(bool(x) for x in (approve_add, approve_qty, approve_rm)) > 1:
        print("error: pass at most one approve flag (one op)", file=sys.stderr)
        return EXIT_USAGE
    if args.command == "add" and (approve_qty or approve_rm):
        print("error: add only accepts --i-approve-add-one", file=sys.stderr)
        return EXIT_USAGE
    if args.command == "set-qty" and (approve_add or approve_rm):
        print("error: set-qty only accepts --i-approve-set-qty", file=sys.stderr)
        return EXIT_USAGE
    if args.command == "remove" and (approve_add or approve_qty):
        print("error: remove only accepts --i-approve-remove-one", file=sys.stderr)
        return EXIT_USAGE

    try:
        if args.command == "add":
            return run_add(args)
        if args.command == "set-qty":
            return run_ultron(args, action="set-qty")
        if args.command == "remove":
            return run_ultron(args, action="remove")
        print(f"error: unknown command {args.command}", file=sys.stderr)
        return EXIT_USAGE
    except (
        ForbiddenApiError,
        MutateSafetyError,
        SkuMapError,
        ItemNodeError,
        NotLoggedInError,
        CdpUnavailableError,
        MtopCallError,
        SessionError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exit_for_exc(exc)


if __name__ == "__main__":
    raise SystemExit(main())
