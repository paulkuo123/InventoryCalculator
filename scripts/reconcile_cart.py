#!/usr/bin/env python3
"""Local review/journal commands. No browser, login, network or cart write code."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cart_reconciliation import (
    approve, build_report, create_run, final_audit, locked_run, note_cause, prepare_next,
    record_after, replan_mapping, source_snapshot,
)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser("preflight", help="唯讀檢查本機來源，不建立虛構購物車基線")
    init = commands.add_parser("init", help="由完整購物車快照建立本機 review 任務")
    for command in (preflight, init):
        command.add_argument("--products", type=Path, required=True)
        command.add_argument("--golden", type=Path, required=True)
    init.add_argument("--baseline", type=Path, required=True)
    init.add_argument("--run-dir", type=Path, required=True)
    review = commands.add_parser("report", help="顯示任務進度；完整報告位於 report.html / report.json")
    approval = commands.add_parser("approve", help="僅在使用者核准具體清單後記錄核准")
    approval.add_argument("--manifest-sha256", required=True)
    approval.add_argument("--evidence", required=True)
    approval.add_argument("--allow-removals", action="store_true")
    approval.add_argument("--shared-sku-evidence", type=Path, help="itemId -> 已確認需求非重複的證據 JSON")
    prepare = commands.add_parser("prepare", help="保存下一筆操作意圖；不執行瀏覽器動作")
    prepare.add_argument("--observation", type=Path, required=True)
    prepare.add_argument("--catalogs", type=Path, required=True)
    after = commands.add_parser("record", help="依操作後新快照驗證，不接受成功提示代替快照")
    after.add_argument("--operation-id", required=True)
    after.add_argument("--observation", type=Path, required=True)
    after.add_argument("--settled", action="store_true", help="已確認無待完成請求；不會自動重送")
    after.add_argument("--resolution-evidence", default="")
    final = commands.add_parser("finalize", help="最終全面回讀；不相符項目回到待核對")
    final.add_argument("--observation", type=Path, required=True)
    replan = commands.add_parser("replan-mapping", help="mapping 經既有服務修正後，產生需重審的新版本")
    replan.add_argument("--golden", type=Path, required=True)
    replan.add_argument("--evidence", required=True)
    note = commands.add_parser("note", help="保存已證實原因或推測，不改購物車／mapping")
    note.add_argument("--item-id", required=True)
    note.add_argument("--explanation", required=True)
    note.add_argument("--evidence", default="")
    note.add_argument("--confirmed", action="store_true")
    for command in (review, approval, prepare, after, final, replan, note):
        command.add_argument("--run-dir", type=Path, required=True)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        output = None
        if args.command == "preflight":
            output = {"status": "awaiting_permitted_cart_baseline", "cartRead": False,
                      "cartModified": False, "sources": {"shopee": source_snapshot(args.products)[0],
                                                         "golden": source_snapshot(args.golden)[0]}}
        elif args.command == "init":
            create_run(args.run_dir, args.products, args.golden, read(args.baseline))
        elif args.command == "approve":
            approve(args.run_dir, args.manifest_sha256, evidence=args.evidence,
                    allow_removals=args.allow_removals,
                    shared_sku_evidence=read(args.shared_sku_evidence) if args.shared_sku_evidence else None)
        elif args.command == "prepare":
            operation = prepare_next(args.run_dir, read(args.observation), read(args.catalogs))
            output = {"browserActionExecuted": False, "operation": operation,
                      "next": ("先唯讀確認此商品頁，再用新購物車快照 prepare；不可加購" if operation and operation["action"] == "inspect_catalog"
                               else "僅在內建瀏覽器政策允許且核准範圍內執行，再用新快照 record" if operation else "需新完整快照 finalize")}
        elif args.command == "record":
            output = record_after(args.run_dir, args.operation_id, read(args.observation),
                                  settled=args.settled, resolution_evidence=args.resolution_evidence)
        elif args.command == "finalize":
            final_audit(args.run_dir, read(args.observation))
        elif args.command == "replan-mapping":
            replan_mapping(args.run_dir, args.golden, evidence=args.evidence)
        elif args.command == "note":
            note_cause(args.run_dir, args.item_id, explanation=args.explanation,
                       evidence=args.evidence, confirmed=args.confirmed)
        if output is None:
            with locked_run(args.run_dir) as (state, manifest):
                report = build_report(state, manifest)
                output = {key: report[key] for key in ("runId", "status", "manifestSha256", "total", "completed", "counts", "currentOperation")}
                output["reportHtml"] = str(args.run_dir.resolve() / "report.html")
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
